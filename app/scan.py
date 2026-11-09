"""端口扫描：从面板容器视角探测目标主机上的开放端口，并粗判协议。"""
import asyncio
import ssl
import time

COMMON_PORTS = [
    21, 22, 23, 25, 53, 67, 69, 80, 81, 88, 110, 111, 123, 135, 137, 139, 143, 161, 389,
    443, 445, 465, 500, 512, 513, 514, 548, 554, 587, 623, 631, 636, 873, 902, 989, 990,
    993, 995, 1080, 1099, 1194, 1433, 1521, 1701, 1723, 1883, 2049, 2181, 2375, 2376,
    3000, 3128, 3260, 3306, 3389, 4369, 5000, 5001, 5060, 5200, 5432, 5555, 5601, 5672,
    5900, 5985, 5986, 6000, 6379, 6443, 7000, 7077, 8000, 8006, 8008, 8080, 8081, 8085,
    8088, 8090, 8091, 8096, 8123, 8161, 8200, 8443, 8448, 8686, 8880, 8888, 9000, 9001,
    9042, 9090, 9091, 9092, 9200, 9300, 9443, 9419, 10000, 10250, 11211, 15672, 27017,
    32400, 50000, 51820,
]

SERVICE_MAP = {
    21: "FTP", 22: "SSH", 23: "Telnet", 25: "SMTP", 53: "DNS", 67: "DHCP", 69: "TFTP",
    80: "Web", 81: "Web", 88: "Kerberos", 110: "POP3", 111: "RPC", 123: "NTP", 135: "RPC",
    137: "NetBIOS", 139: "NetBIOS", 143: "IMAP", 161: "SNMP", 389: "LDAP", 443: "Web TLS",
    445: "SMB", 465: "SMTPS", 500: "IPSec", 548: "AFP", 554: "RTSP", 587: "SMTP", 623: "IPMI",
    631: "CUPS", 636: "LDAPS", 873: "rsync", 902: "VMware", 989: "FTPS", 990: "FTPS",
    993: "IMAPS", 995: "POP3S", 1080: "SOCKS", 1194: "OpenVPN", 1433: "MSSQL", 1521: "Oracle",
    1701: "L2TP", 1723: "PPTP", 1883: "MQTT", 2049: "NFS", 2181: "ZooKeeper", 2375: "Docker API",
    2376: "Docker TLS", 3000: "Web", 3128: "Squid", 3260: "iSCSI", 3306: "MySQL",
    3389: "RDP", 4369: "Erlang", 5000: "Web", 5001: "Web TLS", 5060: "SIP", 5200: "Web",
    5432: "PostgreSQL", 5555: "ADB", 5601: "Kibana", 5672: "RabbitMQ", 5900: "VNC",
    5985: "WinRM", 5986: "WinRM TLS", 6000: "X11", 6379: "Redis", 6443: "K8s API",
    7000: "Web", 7077: "Spark", 8000: "Web", 8006: "Proxmox", 8008: "Web", 8080: "Web",
    8081: "Web", 8085: "Web", 8088: "Web", 8090: "Web", 8091: "Web", 8096: "Jellyfin",
    8123: "Home Assistant", 8161: "ActiveMQ", 8200: "Vault", 8443: "Web TLS",
    8448: "Matrix", 8686: "Web", 8880: "Web", 8888: "Web", 9000: "Web", 9001: "Web",
    9042: "Cassandra", 9090: "Web", 9091: "Web", 9092: "Kafka", 9200: "Elasticsearch",
    9300: "Elasticsearch", 9443: "Web TLS", 9419: "Web", 10000: "Webmin", 10250: "K8s kubelet",
    11211: "Memcached", 15672: "RabbitMQ 管理", 27017: "MongoDB", 32400: "Plex",
    50000: "Web", 51820: "WireGuard",
}

_ssl_ctx = None


def _ctx():
    global _ssl_ctx
    if _ssl_ctx is None:
        c = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        c.check_hostname = False
        c.verify_mode = ssl.CERT_NONE
        _ssl_ctx = c
    return _ssl_ctx


async def _tcp_open(host, port, timeout):
    try:
        reader, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout)
    except (asyncio.TimeoutError, OSError, ValueError):
        return False
    try:
        writer.close()
        await asyncio.wait_for(writer.wait_closed(), 0.5)
    except Exception:
        pass
    return True


async def _probe(host, port):
    """先按明文 HTTP 探一次，再试 TLS，都不像就按裸 TCP 处理。"""
    try:
        reader, writer = await asyncio.wait_for(asyncio.open_connection(host, port), 2.0)
        req = f"HEAD / HTTP/1.1\r\nHost: {host}\r\nConnection: close\r\n\r\n".encode()
        writer.write(req)
        await writer.drain()
        data = await asyncio.wait_for(reader.read(256), 1.5)
        writer.close()
        if data.startswith(b"HTTP/"):
            status = data.split(b"\r\n")[0].decode("latin1", "replace")
            return "http", status
    except Exception:
        pass
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port, ssl=_ctx(), server_hostname=host), 2.5)
        data = b""
        try:
            writer.write(f"HEAD / HTTP/1.1\r\nHost: {host}\r\nConnection: close\r\n\r\n".encode())
            await writer.drain()
            data = await asyncio.wait_for(reader.read(256), 1.5)
        except Exception:
            pass
        writer.close()
        if data.startswith(b"HTTP/"):
            return "https", data.split(b"\r\n")[0].decode("latin1", "replace")
        # TLS 通了但不像 HTTP（RDP、LDAPS、SMTPS 之类），不能当 Web 映射
        return "tcp", "TLS 服务，非 HTTP"
    except Exception:
        return "tcp", ""


def service_name(port):
    return SERVICE_MAP.get(port, "TCP")


async def scan_ports(host, ports, timeout=0.35, concurrency=300):
    started = time.time()
    sem = asyncio.Semaphore(concurrency)
    opened = []

    async def one(p):
        async with sem:
            if await _tcp_open(host, p, timeout):
                opened.append(p)

    await asyncio.gather(*(one(p) for p in ports))
    opened.sort()

    out = []
    if opened:
        sem2 = asyncio.Semaphore(12)

        async def probe(p):
            async with sem2:
                scheme, detail = await _probe(host, p)
                out.append({"port": p, "service": service_name(p),
                            "scheme": scheme, "detail": detail})

        await asyncio.gather(*(probe(p) for p in opened))
        out.sort(key=lambda x: x["port"])
    return out, int((time.time() - started) * 1000)
