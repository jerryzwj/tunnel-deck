import os
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager

DATA_DIR = os.environ.get("DATA_DIR", "/data")
DB_PATH = os.path.join(DATA_DIR, "tunnel-deck.db")

_lock = threading.RLock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings(
    key TEXT PRIMARY KEY,
    value TEXT
);
CREATE TABLE IF NOT EXISTS tunnels(
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    cf_id TEXT NOT NULL,
    account_tag TEXT,
    secret TEXT,
    token TEXT,
    mode TEXT DEFAULT 'local',
    enabled INTEGER DEFAULT 1,
    autostart INTEGER DEFAULT 1,
    metrics_port INTEGER,
    created_at TEXT,
    updated_at TEXT
);
CREATE TABLE IF NOT EXISTS mappings(
    id TEXT PRIMARY KEY,
    tunnel_id TEXT NOT NULL,
    sort INTEGER DEFAULT 0,
    hostname TEXT,
    path TEXT,
    scheme TEXT DEFAULT 'http',
    host TEXT,
    port INTEGER,
    no_tls_verify INTEGER DEFAULT 0,
    http_host_header TEXT,
    note TEXT,
    enabled INTEGER DEFAULT 1,
    zone_id TEXT,
    dns_record_id TEXT,
    created_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_map_tunnel ON mappings(tunnel_id, sort);
"""


def init():
    os.makedirs(DATA_DIR, exist_ok=True)
    with conn() as c:
        c.executescript(SCHEMA)
    if get_setting("auto_dns") is None:
        set_setting("auto_dns", "1")


@contextmanager
def conn():
    with _lock:
        c = sqlite3.connect(DB_PATH, timeout=15)
        c.row_factory = sqlite3.Row
        try:
            yield c
            c.commit()
        finally:
            c.close()


def now():
    return time.strftime("%Y-%m-%d %H:%M:%S")


def new_id():
    return uuid.uuid4().hex[:12]


# ---------------------------------------------------------------- settings
def get_setting(key, default=None):
    with conn() as c:
        row = c.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(key, value):
    with conn() as c:
        c.execute(
            "INSERT INTO settings(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, "" if value is None else str(value)),
        )


# ---------------------------------------------------------------- tunnels
def _row(r):
    return dict(r)


def list_tunnels():
    with conn() as c:
        rows = c.execute("SELECT * FROM tunnels ORDER BY created_at").fetchall()
    return [_row(r) for r in rows]


def get_tunnel(tid):
    with conn() as c:
        r = c.execute("SELECT * FROM tunnels WHERE id=?", (tid,)).fetchone()
    return _row(r) if r else None


def get_tunnel_by_cf_id(cf_id):
    with conn() as c:
        r = c.execute("SELECT * FROM tunnels WHERE cf_id=?", (cf_id,)).fetchone()
    return _row(r) if r else None


def add_tunnel(name, cf_id, account_tag="", secret="", token="", mode="local", metrics_port=None):
    tid = new_id()
    with conn() as c:
        c.execute(
            "INSERT INTO tunnels(id,name,cf_id,account_tag,secret,token,mode,enabled,autostart,"
            "metrics_port,created_at,updated_at) VALUES(?,?,?,?,?,?,?,1,1,?,?,?)",
            (tid, name, cf_id, account_tag, secret, token, mode, metrics_port, now(), now()),
        )
    return get_tunnel(tid)


def update_tunnel(tid, **kw):
    fields = {k: v for k, v in kw.items() if k in {
        "name", "enabled", "autostart", "metrics_port", "account_tag", "secret", "token", "mode"}}
    if not fields:
        return get_tunnel(tid)
    sets = ", ".join(f"{k}=?" for k in fields)
    with conn() as c:
        c.execute(f"UPDATE tunnels SET {sets}, updated_at=? WHERE id=?",
                  (*fields.values(), now(), tid))
    return get_tunnel(tid)


def delete_tunnel(tid):
    with conn() as c:
        c.execute("DELETE FROM mappings WHERE tunnel_id=?", (tid,))
        c.execute("DELETE FROM tunnels WHERE id=?", (tid,))


# ---------------------------------------------------------------- mappings
def list_mappings(tid=None):
    with conn() as c:
        if tid:
            rows = c.execute(
                "SELECT * FROM mappings WHERE tunnel_id=? ORDER BY sort, created_at", (tid,)).fetchall()
        else:
            rows = c.execute("SELECT * FROM mappings ORDER BY tunnel_id, sort, created_at").fetchall()
    return [_row(r) for r in rows]


def get_mapping(mid):
    with conn() as c:
        r = c.execute("SELECT * FROM mappings WHERE id=?", (mid,)).fetchone()
    return _row(r) if r else None


def add_mapping(tunnel_id, hostname="", path="", scheme="http", host="127.0.0.1",
                port=None, no_tls_verify=0, http_host_header="", note=""):
    mid = new_id()
    with conn() as c:
        sort = c.execute("SELECT COALESCE(MAX(sort),0)+1 FROM mappings WHERE tunnel_id=?",
                         (tunnel_id,)).fetchone()[0]
        c.execute(
            "INSERT INTO mappings(id,tunnel_id,sort,hostname,path,scheme,host,port,"
            "no_tls_verify,http_host_header,note,enabled,created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,1,?)",
            (mid, tunnel_id, sort, hostname, path, scheme, host, port,
             int(bool(no_tls_verify)), http_host_header, note, now()),
        )
    return get_mapping(mid)


def update_mapping(mid, **kw):
    fields = {k: v for k, v in kw.items() if k in {
        "hostname", "path", "scheme", "host", "port", "no_tls_verify",
        "http_host_header", "note", "enabled", "zone_id", "dns_record_id", "sort"}}
    if not fields:
        return get_mapping(mid)
    sets = ", ".join(f"{k}=?" for k in fields)
    with conn() as c:
        c.execute(f"UPDATE mappings SET {sets} WHERE id=?", (*fields.values(), mid))
    return get_mapping(mid)


def delete_mapping(mid):
    with conn() as c:
        c.execute("DELETE FROM mappings WHERE id=?", (mid,))
