import json
import os
import re
import signal
import socket
import subprocess
import threading
import time
from collections import deque

import httpx
import yaml

from . import db, net

DATA_DIR = os.environ.get("DATA_DIR", "/data")
CONF_DIR = os.path.join(DATA_DIR, "conf")
CRED_DIR = os.path.join(DATA_DIR, "creds")
os.makedirs(CONF_DIR, exist_ok=True)
os.makedirs(CRED_DIR, exist_ok=True)

VENV_BIN = os.environ.get("CLOUDFLARED", "cloudflared")
BASE_METRICS_PORT = int(os.environ.get("METRICS_BASE_PORT", "20241"))
PROTOCOL = os.environ.get("TUNNEL_PROTOCOL", "auto")

_lock = threading.RLock()
_procs = {}
_logs = {}
_readers = {}


def _log_path(tid):
    return os.path.join(DATA_DIR, "logs", f"{tid}.log")


def _append_log(tid, line):
    buf = _logs.setdefault(tid, deque(maxlen=1200))
    buf.append(line.rstrip("\n"))
    try:
        os.makedirs(os.path.join(DATA_DIR, "logs"), exist_ok=True)
        with open(_log_path(tid), "a", encoding="utf-8") as f:
            f.write(time.strftime("%H:%M:%S ") + line.rstrip("\n") + "\n")
    except OSError:
        pass


_bin_version = None


def cloudflared_version():
    """界面上显示 cloudflared 版本，顺便当作「二进制在不在」的自检。"""
    global _bin_version
    if _bin_version is None:
        try:
            out = subprocess.run([VENV_BIN, "--version"], capture_output=True,
                                 text=True, timeout=8)
            text = (out.stdout or out.stderr or "").strip().splitlines()
            _bin_version = text[0] if text else "未知"
        except Exception as e:
            _bin_version = f"不可用（{e}）"
    return _bin_version


def service_url(m):
    if m.get("scheme") in ("http", "https"):
        return f"{m['scheme']}://{net.resolve(m['host'])}:{int(m['port'])}"
    return m.get("scheme") or "http_status:404"


def render_config(tunnel, mappings):
    cf_id = tunnel["cf_id"]
    cred_path = os.path.join(CRED_DIR, f"{cf_id}.json")
    ingress = []
    for m in mappings:
        if not int(m.get("enabled", 1)):
            continue
        rule = {}
        if m.get("hostname"):
            rule["hostname"] = m["hostname"].strip().lower()
        if m.get("path"):
            rule["path"] = m["path"].strip()
        rule["service"] = service_url(m)
        origin = {}
        if int(m.get("no_tls_verify") or 0):
            origin["noTLSVerify"] = True
        if m.get("http_host_header"):
            origin["httpHostHeader"] = m["http_host_header"].strip()
        if origin:
            rule["originRequest"] = origin
        ingress.append(rule)
    ingress.append({"service": "http_status:404"})

    cfg = {
        "tunnel": cf_id,
        "credentials-file": cred_path.replace("\\", "/"),
        "no-autoupdate": True,
        "metrics": f"127.0.0.1:{int(tunnel['metrics_port'])}",
        "protocol": PROTOCOL,
        "ingress": ingress,
    }
    return yaml.safe_dump(cfg, sort_keys=False, allow_unicode=True)


def write_credentials(tunnel):
    if tunnel.get("mode") == "token":
        return None
    payload = {
        "AccountTag": tunnel.get("account_tag") or "",
        "TunnelSecret": tunnel.get("secret") or "",
        "TunnelID": tunnel["cf_id"],
    }
    path = os.path.join(CRED_DIR, f"{tunnel['cf_id']}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return path


def write_config(tunnel, mappings):
    path = os.path.join(CONF_DIR, f"{tunnel['id']}.yml")
    with open(path, "w", encoding="utf-8") as f:
        f.write(render_config(tunnel, mappings))
    return path


def _next_metrics_port():
    used = {int(t["metrics_port"]) for t in db.list_tunnels() if t.get("metrics_port")}
    port = BASE_METRICS_PORT
    while port in used:
        port += 1
    return port


def _port_free(port):
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind(("127.0.0.1", int(port)))
        return True
    except OSError:
        return False


def assign_metrics_port(tunnel):
    port = tunnel.get("metrics_port")
    if port and _port_free(port):
        return port
    port = BASE_METRICS_PORT
    used = {int(t["metrics_port"]) for t in db.list_tunnels() if t.get("metrics_port")}
    while port in used or not _port_free(port):
        port += 1
    if tunnel.get("metrics_port"):
        # 老进程还占着端口（比如上一份程序被杀但 cloudflared 留了下来）
        _append_log(tunnel["id"], f"[panel] metrics 端口 {tunnel['metrics_port']} 被占用，改用 {port}")
    db.update_tunnel(tunnel["id"], metrics_port=port)
    tunnel["metrics_port"] = port
    return port


def stop_all():
    """退出前把本进程拉起的 cloudflared 全部收掉，别留孤儿占端口。"""
    for tid in list(_procs.keys()):
        try:
            stop(tid, timeout=3)
        except Exception:
            pass


def _reader(proc, tid):
    try:
        for line in proc.stdout:
            _append_log(tid, line)
    except Exception as e:  # 管道被关掉 / 解码出错
        _append_log(tid, f"[panel] 日志读取结束：{e}")
    finally:
        try:
            rc = proc.wait(timeout=3)
        except Exception:
            rc = proc.poll()
        _append_log(tid, f"[panel] cloudflared 已退出，退出码 {rc}")


def is_running(tid):
    with _lock:
        p = _procs.get(tid)
        return bool(p and p.poll() is None)


def start(tunnel, mappings=None, restart=False):
    tid = tunnel["id"]
    if not tunnel.get("metrics_port") or not _port_free(tunnel["metrics_port"]):
        assign_metrics_port(tunnel)
    if tunnel.get("mode") == "token" and tunnel.get("token"):
        cmd = [VENV_BIN, "--no-autoupdate", "tunnel", "--metrics",
               f"127.0.0.1:{int(tunnel['metrics_port'])}", "run", "--token", tunnel["token"]]
    else:
        write_credentials(tunnel)
        mappings = mappings if mappings is not None else db.list_mappings(tid)
        conf = write_config(tunnel, mappings)
        cmd = [VENV_BIN, "--config", conf, "tunnel", "run", tunnel["cf_id"]]

    with _lock:
        old = _procs.get(tid)
        if old and old.poll() is None:
            if not restart:
                return {"running": True, "started": False}
            try:
                old.send_signal(signal.SIGTERM)
                old.wait(timeout=8)
            except Exception:
                old.kill()
        _logs[tid] = deque(maxlen=1200)
        _append_log(tid, f"[panel] 启动：{' '.join(cmd[:4])} ...")
        try:
            popen_kw = dict(stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, bufsize=1,
                            encoding="utf-8", errors="replace")
            if os.name == "nt":
                # 原生 exe 是窗口程序，不给 cloudflared 弹控制台窗口
                popen_kw["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            proc = subprocess.Popen(cmd, **popen_kw)
        except FileNotFoundError:
            _append_log(tid, "[panel] 找不到 cloudflared 可执行文件，请确认镜像构建正确")
            return {"running": False, "error": "cloudflared 未安装"}
        _procs[tid] = proc
        t = threading.Thread(target=_reader, args=(proc, tid), daemon=True)
        t.start()
        _readers[tid] = t
        return {"running": True, "started": True, "pid": proc.pid}


def stop(tid, timeout=10):
    with _lock:
        p = _procs.get(tid)
        if not p or p.poll() is not None:
            return {"running": False, "stopped": False}
        try:
            p.terminate()
            p.wait(timeout=timeout)
        except Exception:
            try:
                p.kill()
            except Exception:
                pass
        _append_log(tid, "[panel] 已停止")
        return {"running": False, "stopped": True}


def restart(tunnel, mappings=None):
    stop(tunnel["id"])
    time.sleep(0.4)
    return start(tunnel, mappings, restart=False)


def ready(tunnel):
    port = tunnel.get("metrics_port")
    if not port:
        return False
    try:
        r = httpx.get(f"http://127.0.0.1:{int(port)}/ready", timeout=2.0)
        return r.status_code == 200
    except Exception:
        return False


def status(tunnel):
    tid = tunnel["id"]
    with _lock:
        p = _procs.get(tid)
        running = bool(p and p.poll() is None)
        code = p.poll() if p else None
    connected = ready(tunnel) if running else False
    if running and connected:
        state = "connected"
    elif running:
        state = "connecting"
    elif code not in (None, 0):
        state = "error"
    else:
        state = "stopped"
    tail = list(_logs.get(tid, []))[-6:]
    return {"state": state, "running": running, "connected": connected,
            "exit_code": code, "metrics_port": tunnel.get("metrics_port"), "tail": tail}


def logs_since(tid, since=0):
    buf = _logs.get(tid, [])
    start_idx = max(0, min(int(since or 0), len(buf)))
    # deque has no absolute index; use offset from the tail
    total = len(buf)
    items = list(buf)[start_idx:]
    return {"total": total, "lines": items}


def read_log_file_tail(tid, limit=200):
    path = _log_path(tid)
    if not os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()[-int(limit):]
        return [ln.rstrip("\n") for ln in lines]
    except OSError:
        return []
