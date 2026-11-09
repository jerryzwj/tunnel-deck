import json
import os
import re
import signal
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
        "credentials-file": cred_path,
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


def assign_metrics_port(tunnel):
    if tunnel.get("metrics_port"):
        return tunnel["metrics_port"]
    port = _next_metrics_port()
    db.update_tunnel(tunnel["id"], metrics_port=port)
    return port


def _reader(proc, tid):
    try:
        for line in proc.stdout:
            _append_log(tid, line)
    except Exception as e:  # process closed
        _append_log(tid, f"[panel] 日志读取结束：{e}")
    finally:
        rc = proc.poll()
        _append_log(tid, f"[panel] cloudflared 已退出，退出码 {rc}")


def is_running(tid):
    with _lock:
        p = _procs.get(tid)
        return bool(p and p.poll() is None)


def start(tunnel, mappings=None, restart=False):
    tid = tunnel["id"]
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
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                    text=True, bufsize=1)
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
