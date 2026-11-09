import base64
import hashlib
import hmac
import ipaddress
import os
import re
import socket
import sys
import time
from typing import Optional

import httpx
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import db, net, runner, scan
from .cf import Cloudflare, CloudflareError, decode_token

def _web_dir():
    """容器 / 源码 / PyInstaller 打包后的 exe 都能找到前端文件。"""
    env = os.environ.get("TD_WEB_DIR")
    if env and os.path.isdir(env):
        return env
    if getattr(sys, "frozen", False):
        return os.path.join(getattr(sys, "_MEIPASS", os.path.dirname(sys.executable)), "web")
    return os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "web")


WEB_DIR = _web_dir()
SESSION_COOKIE = "td_session"
SESSION_TTL = 7 * 24 * 3600
VERSION = "1.0.6"

app = FastAPI(title="TunnelDeck", docs_url=None, redoc_url=None)
db.init()

if db.get_setting("session_secret") is None:
    db.set_setting("session_secret", base64.urlsafe_b64encode(os.urandom(32)).decode())


# ------------------------------------------------------------------ auth
def panel_password():
    """环境变量优先；没设就用面板里初始化时写进数据库的密码。"""
    return os.environ.get("PANEL_PASSWORD") or db.get_setting("panel_password")


def password_locked():
    return bool(os.environ.get("PANEL_PASSWORD"))


def needs_setup():
    return (not password_locked()) and (not db.get_setting("panel_password"))


def hash_password(pw):
    salt = os.urandom(16).hex()
    digest = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt.encode(), 120000).hex()
    return f"pbkdf2_sha256$120000${salt}${digest}"


def verify_password(pw):
    env = os.environ.get("PANEL_PASSWORD")
    if env:
        return hmac.compare_digest((pw or "").strip(), env)
    stored = db.get_setting("panel_password") or ""
    if not stored.startswith("pbkdf2_sha256$"):
        return False
    _, iters, salt, digest = stored.split("$")
    calc = hashlib.pbkdf2_hmac("sha256", (pw or "").strip().encode(), salt.encode(), int(iters)).hex()
    return hmac.compare_digest(calc, digest)


def client_is_local(request):
    """首次初始化只允许本机/内网访问，避免公网被人抢注。"""
    try:
        addr = request.client.host if request.client else ""
        ip = ipaddress.ip_address(addr)
    except Exception:
        return False
    return ip.is_loopback or ip.is_private


def sign(value):
    key = db.get_setting("session_secret", "").encode()
    return hmac.new(key, value.encode(), hashlib.sha256).hexdigest()


def make_session():
    exp = str(int(time.time() + SESSION_TTL))
    return f"{exp}.{sign(exp)}"


def session_valid(token):
    if not token or "." not in token:
        return False
    exp, sig = token.rsplit(".", 1)
    if not exp.isdigit() or int(exp) < time.time():
        return False
    return hmac.compare_digest(sig, sign(exp))


def authenticated(request: Request):
    return session_valid(request.cookies.get(SESSION_COOKIE))


def require_auth(request: Request):
    if not authenticated(request):
        raise HTTPException(status_code=401, detail="请先登录")


# ------------------------------------------------------------------ helpers
def get_cf(require_token=True):
    token = os.environ.get("CF_API_TOKEN") or db.get_setting("cf_api_token", "")
    account = os.environ.get("CF_ACCOUNT_ID") or db.get_setting("cf_account_id", "")
    if require_token and not token:
        raise HTTPException(status_code=400, detail="还没有配置 Cloudflare API Token，请先到设置里填写")
    return Cloudflare(token, account)


def has_token():
    return bool(os.environ.get("CF_API_TOKEN") or db.get_setting("cf_api_token", ""))


def mapping_public_url(m):
    if not m.get("hostname"):
        return ""
    path = m.get("path") or ""
    return f"https://{m['hostname']}{path}"


def serialize_mapping(m):
    d = dict(m)
    d["url"] = mapping_public_url(m)
    d["service"] = runner.service_url(m)
    d["enabled"] = bool(int(d.get("enabled", 1)))
    d["no_tls_verify"] = bool(int(d.get("no_tls_verify") or 0))
    return d


HOST_RE = re.compile(r"^[a-z0-9]([a-z0-9-_]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-_]{0,61}[a-z0-9])?)+$")


def normalize_host(hostname):
    h = (hostname or "").strip().lower()
    h = h.replace("https://", "").replace("http://", "").split("/")[0]
    if not h:
        return ""
    if not HOST_RE.match(h):
        raise HTTPException(status_code=400, detail=f"域名格式不正确：{h}")
    return h


def tunnel_public(t, with_status=True):
    mappings = db.list_mappings(t["id"])
    d = {
        "id": t["id"],
        "name": t["name"],
        "cf_id": t["cf_id"],
        "mode": t.get("mode") or "local",
        "enabled": bool(int(t.get("enabled", 1))),
        "autostart": bool(int(t.get("autostart", 1))),
        "metrics_port": t.get("metrics_port"),
        "mapping_count": len(mappings),
        "hosts": [m["hostname"] for m in mappings if m.get("hostname")],
    }
    if with_status:
        d["status"] = runner.status(t)
    return d


def app_state(tunnels=None):
    tunnels = tunnels if tunnels is not None else db.list_tunnels()
    return {
        "authenticated": True,
        "version": VERSION,
        "settings": {
            "has_token": has_token(),
            "account_id": os.environ.get("CF_ACCOUNT_ID") or db.get_setting("cf_account_id", ""),
            "auto_dns": db.get_setting("auto_dns", "1") == "1",
            "loopback_alias": net.alias_enabled(),
            "password_locked": password_locked(),
            "cf_env_locked": bool(os.environ.get("CF_API_TOKEN")),
        },
        "tunnels": [tunnel_public(t) for t in tunnels],
        "mappings": {t["id"]: [serialize_mapping(m) for m in db.list_mappings(t["id"])]
                     for t in tunnels},
    }


# ------------------------------------------------------------------ models
class LoginBody(BaseModel):
    password: str


class SetupBody(BaseModel):
    password: str
    account_id: Optional[str] = None
    api_token: Optional[str] = None


class SettingsBody(BaseModel):
    cf_api_token: Optional[str] = None
    cf_account_id: Optional[str] = None
    auto_dns: Optional[bool] = None
    loopback_alias: Optional[bool] = None
    panel_password: Optional[str] = None


class TunnelBody(BaseModel):
    name: str


class ImportBody(BaseModel):
    token: str
    name: Optional[str] = None


class TunnelPatch(BaseModel):
    name: Optional[str] = None
    autostart: Optional[bool] = None


class MappingBody(BaseModel):
    hostname: Optional[str] = ""
    path: Optional[str] = ""
    scheme: str = "http"
    host: str = "127.0.0.1"
    port: Optional[int] = None
    no_tls_verify: bool = False
    http_host_header: Optional[str] = ""
    note: Optional[str] = ""


class MappingPatch(BaseModel):
    hostname: Optional[str] = None
    path: Optional[str] = None
    scheme: Optional[str] = None
    host: Optional[str] = None
    port: Optional[int] = None
    no_tls_verify: Optional[bool] = None
    http_host_header: Optional[str] = None
    note: Optional[str] = None
    enabled: Optional[bool] = None


class CheckBody(BaseModel):
    host: str
    port: int


class ScanBody(BaseModel):
    host: str = "127.0.0.1"
    mode: str = "common"          # common | range | full
    start: Optional[int] = None
    end: Optional[int] = None
    timeout: float = 0.35


# ------------------------------------------------------------------ routes
@app.exception_handler(CloudflareError)
async def cf_error_handler(request: Request, exc: CloudflareError):
    return JSONResponse(status_code=400, content={"detail": exc.message})


@app.exception_handler(HTTPException)
async def http_error_handler(request: Request, exc: HTTPException):
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})


@app.get("/healthz")
def healthz():
    return {"ok": True}


@app.post("/api/login")
def login(body: LoginBody, response: Response):
    if needs_setup():
        raise HTTPException(status_code=400, detail="面板还没初始化，请先设置访问密码")
    if not verify_password(body.password):
        raise HTTPException(status_code=401, detail="密码不正确")
    response.set_cookie(SESSION_COOKIE, make_session(), max_age=SESSION_TTL,
                        httponly=True, samesite="lax", path="/")
    return {"ok": True}


@app.post("/api/setup")
def setup(body: SetupBody, request: Request, response: Response):
    if not needs_setup():
        raise HTTPException(status_code=400, detail="已经初始化过了，直接在登录页输密码")
    if not client_is_local(request):
        raise HTTPException(status_code=403,
                            detail="首次初始化只允许本机/内网访问；远程部署请改用 .env 的 PANEL_PASSWORD")
    pw = (body.password or "").strip()
    if len(pw) < 6:
        raise HTTPException(status_code=400, detail="密码至少 6 位")
    db.set_setting("panel_password", hash_password(pw))
    if body.api_token and body.api_token.strip() and not os.environ.get("CF_API_TOKEN"):
        db.set_setting("cf_api_token", body.api_token.strip())
    if body.account_id and body.account_id.strip():
        db.set_setting("cf_account_id", body.account_id.strip())
    response.set_cookie(SESSION_COOKIE, make_session(), max_age=SESSION_TTL,
                        httponly=True, samesite="lax", path="/")
    result = {"ok": True}
    if has_token():
        try:
            cf = get_cf()
            cf.verify()
            cf.zones(force=True)
            result["verified"] = True
        except CloudflareError as e:
            result["verified"] = False
            result["warning"] = f"Token 校验未通过：{e.message}"
    return result


@app.post("/api/logout")
def logout(response: Response):
    response.delete_cookie(SESSION_COOKIE, path="/")
    return {"ok": True}


@app.get("/api/state")
def state(request: Request):
    if not authenticated(request):
        return {"authenticated": False, "needs_setup": needs_setup(),
                "settings": {"password_locked": password_locked()}}
    return app_state()


@app.get("/api/zones")
def zones(request: Request):
    require_auth(request)
    cf = get_cf()
    try:
        return {"zones": cf.zones(force=True)}
    except CloudflareError as e:
        raise HTTPException(status_code=400, detail=f"读取域名失败：{e.message}")


@app.post("/api/settings")
def save_settings(body: SettingsBody, request: Request):
    require_auth(request)
    if body.cf_api_token is not None and not os.environ.get("CF_API_TOKEN"):
        db.set_setting("cf_api_token", body.cf_api_token.strip())
    if body.cf_account_id is not None:
        db.set_setting("cf_account_id", body.cf_account_id.strip())
    if body.auto_dns is not None:
        db.set_setting("auto_dns", "1" if body.auto_dns else "0")
    if body.loopback_alias is not None:
        db.set_setting("loopback_means_host", "1" if body.loopback_alias else "0")
    if body.panel_password:
        if password_locked():
            raise HTTPException(status_code=400, detail="面板密码由 .env 的 PANEL_PASSWORD 固定，改那里")
        if len(body.panel_password.strip()) < 6:
            raise HTTPException(status_code=400, detail="密码至少 6 位")
        db.set_setting("panel_password", hash_password(body.panel_password.strip()))

    result = {"ok": True}
    if has_token():
        try:
            cf = get_cf()
            cf.verify()
            cf.zones(force=True)
            result["verified"] = True
        except CloudflareError as e:
            result["verified"] = False
            result["warning"] = f"Token 校验未通过：{e.message}"
    return result


# ---------------------------------------------------------------- tunnels
@app.post("/api/tunnels")
def create_tunnel(body: TunnelBody, request: Request):
    require_auth(request)
    name = (body.name or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="请填写连接名称")
    cf = get_cf()
    created = cf.create_tunnel(name)
    cf_id = created["id"]
    tok = created.get("token") or cf.get_tunnel_token(cf_id)
    info = decode_token(tok)
    t = db.add_tunnel(name=name, cf_id=cf_id, account_tag=info["account_tag"],
                      secret=info["secret"], token=tok, mode="local",
                      metrics_port=runner._next_metrics_port())
    runner.start(t)
    time.sleep(0.5)
    return {"tunnel": tunnel_public(t)}


@app.post("/api/tunnels/import")
def import_tunnel(body: ImportBody, request: Request):
    require_auth(request)
    token = (body.token or "").strip()
    if not token:
        raise HTTPException(status_code=400, detail="请粘贴 cloudflared 连接 Token")
    info = decode_token(body.token)
    cf_id = info["tunnel_id"]
    name = (body.name or "").strip()
    if not name:
        try:
            cf = get_cf()
            for t in cf.list_tunnels():
                if t["id"] == cf_id:
                    name = t["name"]
                    break
        except Exception:
            pass
    if db.get_tunnel_by_cf_id(cf_id):
        raise HTTPException(status_code=400, detail="这条连接已经在面板里了")
    t = db.add_tunnel(name=name or cf_id[:8], cf_id=cf_id, account_tag=info["account_tag"],
                      secret=info["secret"], token=body.token.strip(), mode="token",
                      metrics_port=runner._next_metrics_port())
    result = {"tunnel": tunnel_public(t), "remote": True, "warnings": []}
    try:
        cf = get_cf()
        if cf_id not in {x["id"] for x in cf.list_tunnels()}:
            result["remote"] = False
            result["warnings"].append(
                "这条隧道在当前 API Token 所属账号下查不到，可能来自另一个 Cloudflare 账号或已被删除："
                "面板能运行它、看日志，但「接管到面板」和自动 DNS 会失败。")
    except Exception:
        pass
    return result


@app.patch("/api/tunnels/{tid}")
def patch_tunnel(tid: str, body: TunnelPatch, request: Request):
    require_auth(request)
    t = db.get_tunnel(tid)
    if not t:
        raise HTTPException(status_code=404, detail="连接不存在")
    kw = {}
    if body.name is not None:
        kw["name"] = body.name.strip() or t["name"]
    if body.autostart is not None:
        kw["autostart"] = 1 if body.autostart else 0
    db.update_tunnel(tid, **kw)
    return {"tunnel": tunnel_public(db.get_tunnel(tid))}


@app.post("/api/tunnels/{tid}/takeover")
def takeover(tid: str, request: Request):
    """把 dashboard 托管的连接切成由本面板下发配置。"""
    require_auth(request)
    t = db.get_tunnel(tid)
    if not t:
        raise HTTPException(status_code=404, detail="连接不存在")
    cf = get_cf()
    try:
        cf._request("PATCH", f"/accounts/{cf.account_id}/cfd_tunnel/{t['cf_id']}",
                    json_body={"config_src": "local"})
    except CloudflareError as e:
        if e.code == 1002:
            raise HTTPException(status_code=400, detail=(
                "切换失败：Cloudflare 上找不到这条隧道（1002 Tunnel not found）。"
                "常见原因是这条连接用别的账号的 Token 导入，或隧道已在控制台被删除；"
                "这种情况只能运行它、不能接管或改配置。要由面板管理，请删除这条记录，"
                "再用当前 API Token 所属账号新建连接、或导入该账号下的隧道 Token。"))
        raise HTTPException(status_code=400, detail=f"切换失败：{e.message}")
    info = decode_token(t.get("token") or cf.get_tunnel_token(t["cf_id"]))
    db.update_tunnel(tid, mode="local", secret=info["secret"], account_tag=info["account_tag"])
    t = db.get_tunnel(tid)
    runner.restart(t)
    return {"tunnel": tunnel_public(t)}


@app.delete("/api/tunnels/{tid}")
def remove_tunnel(tid: str, request: Request, delete_cf: bool = False, delete_dns: bool = False):
    require_auth(request)
    t = db.get_tunnel(tid)
    if not t:
        raise HTTPException(status_code=404, detail="连接不存在")
    runner.stop(tid)
    warnings = []
    if delete_dns:
        cf = get_cf()
        for m in db.list_mappings(tid):
            if m.get("dns_record_id") and m.get("zone_id"):
                try:
                    cf.delete_dns(m["zone_id"], m["dns_record_id"])
                except CloudflareError as e:
                    warnings.append(e.message)
    if delete_cf:
        try:
            get_cf().delete_tunnel(t["cf_id"])
        except CloudflareError as e:
            warnings.append(f"删除 Cloudflare 隧道失败：{e.message}")
    db.delete_tunnel(tid)
    return {"ok": True, "warnings": warnings}


@app.post("/api/tunnels/{tid}/start")
def start_tunnel(tid: str, request: Request):
    require_auth(request)
    t = db.get_tunnel(tid)
    if not t:
        raise HTTPException(status_code=404, detail="连接不存在")
    runner.start(t)
    time.sleep(0.6)
    return {"tunnel": tunnel_public(db.get_tunnel(tid))}


@app.post("/api/tunnels/{tid}/stop")
def stop_tunnel(tid: str, request: Request):
    require_auth(request)
    t = db.get_tunnel(tid)
    if not t:
        raise HTTPException(status_code=404, detail="连接不存在")
    runner.stop(tid)
    return {"tunnel": tunnel_public(db.get_tunnel(tid))}


@app.post("/api/tunnels/{tid}/restart")
def restart_tunnel(tid: str, request: Request):
    require_auth(request)
    t = db.get_tunnel(tid)
    if not t:
        raise HTTPException(status_code=404, detail="连接不存在")
    runner.restart(t)
    time.sleep(0.6)
    return {"tunnel": tunnel_public(db.get_tunnel(tid))}


@app.get("/api/tunnels/{tid}/status")
def tunnel_status(tid: str, request: Request):
    require_auth(request)
    t = db.get_tunnel(tid)
    if not t:
        raise HTTPException(status_code=404, detail="连接不存在")
    return {"status": runner.status(t), "tunnel": tunnel_public(t)}


@app.get("/api/tunnels/{tid}/logs")
def tunnel_logs(tid: str, request: Request, offset: int = 0):
    require_auth(request)
    if not db.get_tunnel(tid):
        raise HTTPException(status_code=404, detail="连接不存在")
    data = runner.logs_since(tid, offset)
    if not data["lines"] and offset == 0:
        data["lines"] = runner.read_log_file_tail(tid, 80)
        data["total"] = len(data["lines"])
    return data


@app.get("/api/tunnels/{tid}/config")
def tunnel_config(tid: str, request: Request):
    require_auth(request)
    t = db.get_tunnel(tid)
    if not t:
        raise HTTPException(status_code=404, detail="连接不存在")
    return {"config": runner.render_config(t, db.list_mappings(tid))}


# ---------------------------------------------------------------- mappings
@app.post("/api/tunnels/{tid}/mappings")
def add_mapping(tid: str, body: MappingBody, request: Request):
    require_auth(request)
    t = db.get_tunnel(tid)
    if not t:
        raise HTTPException(status_code=404, detail="连接不存在")
    if t.get("mode") != "local":
        raise HTTPException(status_code=400,
                            detail="这条连接仍由 Cloudflare 控制台托管，请先点「接管到面板」")
    hostname = normalize_host(body.hostname or "")
    if not hostname:
        raise HTTPException(status_code=400, detail="请填写要映射的域名")
    if body.port is None and body.scheme in ("http", "https"):
        raise HTTPException(status_code=400, detail="请填写本地端口")
    if body.port is not None and not (1 <= int(body.port) <= 65535):
        raise HTTPException(status_code=400, detail="端口必须在 1-65535 之间")
    host = (body.host or "127.0.0.1").strip()
    service = runner.service_url({"scheme": body.scheme, "host": host, "port": body.port})
    if body.scheme in ("http", "https"):
        for m in db.list_mappings(tid):
            if m["hostname"] == hostname and (m.get("path") or "") == (body.path or ""):
                raise HTTPException(status_code=400, detail=f"域名 {hostname} 已经映射过了")

    m = db.add_mapping(tid, hostname=hostname, path=(body.path or "").strip(),
                       scheme=body.scheme, host=host, port=body.port,
                       no_tls_verify=1 if body.no_tls_verify else 0,
                       http_host_header=(body.http_host_header or "").strip(),
                       note=(body.note or "").strip())
    warnings = []
    if db.get_setting("auto_dns", "1") == "1" and has_token():
        try:
            zone_id, rec_id, changed = get_cf().ensure_cname(hostname, t["cf_id"])
            m = db.update_mapping(m["id"], zone_id=zone_id, dns_record_id=rec_id)
        except CloudflareError as e:
            warnings.append(f"DNS 记录未自动创建：{e.message}")
    runner.restart(t)
    return {"mapping": serialize_mapping(m), "warnings": warnings, "public_url": mapping_public_url(m)}


@app.patch("/api/mappings/{mid}")
def patch_mapping(mid: str, body: MappingPatch, request: Request):
    require_auth(request)
    m = db.get_mapping(mid)
    if not m:
        raise HTTPException(status_code=404, detail="映射不存在")
    t = db.get_tunnel(m["tunnel_id"])
    kw = {}
    if body.hostname is not None:
        kw["hostname"] = normalize_host(body.hostname)
    if body.path is not None:
        kw["path"] = body.path.strip()
    if body.scheme is not None:
        kw["scheme"] = body.scheme
    if body.host is not None:
        kw["host"] = body.host.strip()
    if body.port is not None:
        kw["port"] = body.port
    if body.no_tls_verify is not None:
        kw["no_tls_verify"] = 1 if body.no_tls_verify else 0
    if body.http_host_header is not None:
        kw["http_host_header"] = body.http_host_header.strip()
    if body.note is not None:
        kw["note"] = body.note.strip()
    if body.enabled is not None:
        kw["enabled"] = 1 if body.enabled else 0
    updated = db.update_mapping(mid, **kw)
    runner.restart(t)
    return {"mapping": serialize_mapping(updated)}


@app.delete("/api/mappings/{mid}")
def remove_mapping(mid: str, request: Request, delete_dns: bool = True):
    require_auth(request)
    m = db.get_mapping(mid)
    if not m:
        raise HTTPException(status_code=404, detail="映射不存在")
    warnings = []
    if delete_dns and m.get("dns_record_id") and m.get("zone_id") and has_token():
        try:
            get_cf().delete_dns(m["zone_id"], m["dns_record_id"])
        except CloudflareError as e:
            warnings.append(f"DNS 记录删除失败：{e.message}")
    db.delete_mapping(mid)
    t = db.get_tunnel(m["tunnel_id"])
    if t:
        runner.restart(t)
    return {"ok": True, "warnings": warnings}


@app.post("/api/mappings/{mid}/dns")
def sync_mapping_dns(mid: str, request: Request):
    require_auth(request)
    m = db.get_mapping(mid)
    if not m:
        raise HTTPException(status_code=404, detail="映射不存在")
    t = db.get_tunnel(m["tunnel_id"])
    zone_id, rec_id, changed = get_cf().ensure_cname(m["hostname"], t["cf_id"])
    db.update_mapping(mid, zone_id=zone_id, dns_record_id=rec_id)
    return {"ok": True, "changed": changed, "record_id": rec_id}


@app.post("/api/tunnels/{tid}/dns_sync")
def sync_tunnel_dns(tid: str, request: Request):
    require_auth(request)
    t = db.get_tunnel(tid)
    if not t:
        raise HTTPException(status_code=404, detail="连接不存在")
    cf = get_cf()
    done, failed = [], []
    for m in db.list_mappings(tid):
        if not m.get("hostname"):
            continue
        try:
            zone_id, rec_id, changed = cf.ensure_cname(m["hostname"], t["cf_id"])
            db.update_mapping(m["id"], zone_id=zone_id, dns_record_id=rec_id)
            done.append(m["hostname"])
        except CloudflareError as e:
            failed.append(f"{m['hostname']}: {e.message}")
    return {"ok": not failed, "synced": done, "failed": failed}


# ---------------------------------------------------------------- utilities
@app.post("/api/check")
def check_port(body: CheckBody, request: Request):
    require_auth(request)
    host = net.resolve((body.host or "").strip())
    port = int(body.port)
    started = time.time()
    try:
        with socket.create_connection((host, port), timeout=3):
            return {"ok": True, "latency_ms": int((time.time() - started) * 1000), "host": host}
    except OSError as e:
        return {"ok": False, "error": str(e), "host": host}


@app.post("/api/scan")
async def scan_ports(body: ScanBody, request: Request):
    """从容器视角扫描目标主机的开放端口，顺便判断 http/https。"""
    require_auth(request)
    host = net.resolve((body.host or "").strip() or "127.0.0.1")
    mode = body.mode if body.mode in ("common", "range", "full") else "common"
    if mode == "full":
        ports = list(range(1, 65536))
    elif mode == "range":
        start = max(1, int(body.start or 1))
        end = min(65535, int(body.end or 65535))
        if end < start:
            raise HTTPException(status_code=400, detail="端口范围不正确")
        if end - start > 20000:
            raise HTTPException(status_code=400, detail="一次最多扫 20000 个端口")
        ports = list(range(start, end + 1))
    else:
        ports = scan.COMMON_PORTS
    timeout = min(2.0, max(0.15, float(body.timeout or 0.35)))
    opened, ms = await scan.scan_ports(host, ports, timeout=timeout)
    return {"host": host, "mode": mode, "scanned": len(ports),
            "duration_ms": ms, "open": opened,
            "from": {"name": net.self_name(), "ip": net.self_ip(),
                     "gateway": net.gateway_ip()}}


@app.get("/api/info")
def panel_info(request: Request):
    """面板自己跑在哪台机器上——扫描结果属于这台机器，不是浏览器所在的那台。"""
    require_auth(request)
    return {"hostname": net.self_name(), "ip": net.self_ip(),
            "gateway": net.gateway_ip(), "loopback_alias": net.alias_enabled(),
            "mode": "native" if net.native_mode() else "docker",
            "cloudflared": runner.cloudflared_version(),
            "version": VERSION}


@app.post("/api/reveal")
def reveal_password(request: Request):
    """首次初始化时前端用来判断是否要显示初始化页；不再回显密码。"""
    return {"needs_setup": needs_setup(), "password": None}


# ---------------------------------------------------------------- static
if os.path.isdir(WEB_DIR):
    app.mount("/assets", StaticFiles(directory=WEB_DIR), name="assets")


@app.get("/")
def index():
    path = os.path.join(WEB_DIR, "index.html")
    if not os.path.exists(path):
        return JSONResponse({"detail": "前端文件缺失"}, status_code=500)
    return FileResponse(path, media_type="text/html; charset=utf-8")


@app.get("/favicon.svg")
def favicon():
    p = os.path.join(WEB_DIR, "favicon.svg")
    if os.path.exists(p):
        return FileResponse(p, media_type="image/svg+xml")
    raise HTTPException(status_code=404)


@app.on_event("startup")
def startup():
    for t in db.list_tunnels():
        if t.get("autostart") and t.get("enabled"):
            try:
                runner.start(t)
            except Exception:
                pass
