"""目标主机的语义修正。

面板跑在容器里，容器内的 127.0.0.1 是容器自己，不是用户口中的「这台 NAS / 这台电脑」。
用户填 127.0.0.1 / localhost 时，按宿主机的意思来处理：
能解析到 host.docker.internal（bridge 模式 + host-gateway）就替换过去；
host 网络模式下解析不到，127.0.0.1 本来就是宿主机，原样返回。
"""

import os
import socket

from . import db

LOOPBACK = {"127.0.0.1", "localhost", "::1", "0.0.0.0"}
HOST_GATEWAY = "host.docker.internal"

_gateway_ok = None


def native_mode():
    """Windows / 本机直接跑（非容器）时为 True：127.0.0.1 本来就是这台电脑。"""
    return os.environ.get("TD_NATIVE") == "1"


def gateway_reachable():
    global _gateway_ok
    if _gateway_ok is None:
        try:
            socket.getaddrinfo(HOST_GATEWAY, 8787, proto=socket.IPPROTO_TCP)
            _gateway_ok = True
        except Exception:
            _gateway_ok = False
    return _gateway_ok


def alias_enabled():
    # 原生运行时 127.0.0.1 就是这台电脑，默认不做替换；容器版本默认替换到宿主机。
    default = "0" if native_mode() else "1"
    return db.get_setting("loopback_means_host", default) == "1"


def resolve(host):
    h = (host or "").strip()
    if not h:
        return h
    if h.lower() not in LOOPBACK:
        return h
    if not alias_enabled():
        return h
    return HOST_GATEWAY if gateway_reachable() else h


def gateway_ip():
    """host.docker.internal 解析出的 IP，用来在界面上说清「扫的到底是谁」。"""
    try:
        infos = socket.getaddrinfo(HOST_GATEWAY, 8787, proto=socket.IPPROTO_TCP)
        for item in infos:
            addr = item[4][0]
            if ":" not in addr:
                return addr
        return infos[0][4][0]
    except Exception:
        return None


def self_ip():
    """容器自己的地址（不真正发包，只为让内核挑出口 IP）。"""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except Exception:
        return None
    finally:
        s.close()


def self_name():
    return socket.gethostname()
