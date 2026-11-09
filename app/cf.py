import base64
import json
import time

import httpx

BASE = "https://api.cloudflare.com/client/v4"


class CloudflareError(Exception):
    def __init__(self, message, status=None, code=None):
        super().__init__(message)
        self.message = message
        self.status = status
        self.code = code


class Cloudflare:
    def __init__(self, token, account_id=""):
        self.token = (token or "").strip()
        self.account_id = (account_id or "").strip()
        self._zone_cache = None
        self._zone_cache_at = 0

    # ---------------------------------------------------------- low level
    def _request(self, method, path, json_body=None, params=None):
        headers = {"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"}
        try:
            r = httpx.request(method, BASE + path, headers=headers, json=json_body,
                              params=params, timeout=25)
        except httpx.HTTPError as e:
            raise CloudflareError(f"无法连接 Cloudflare API：{e}") from e
        try:
            data = r.json()
        except Exception:
            raise CloudflareError(f"Cloudflare 返回非 JSON 内容（HTTP {r.status_code}）", r.status_code)
        if not data.get("success"):
            errs = data.get("errors") or []
            msg = "; ".join(f"{e.get('code')}: {e.get('message')}" for e in errs) or "请求失败"
            raise CloudflareError(msg, r.status_code, errs[0].get("code") if errs else None)
        return data.get("result")

    def verify(self):
        return self._request("GET", "/user/tokens/verify")

    # ---------------------------------------------------------- zones
    def zones(self, force=False):
        if self._zone_cache is not None and not force and time.time() - self._zone_cache_at < 300:
            return self._zone_cache
        result, page = [], 1
        while True:
            batch = self._request("GET", "/zones", params={"per_page": 50, "page": page}) or []
            result.extend(batch)
            if len(batch) < 50:
                break
            page += 1
        self._zone_cache = [{"id": z["id"], "name": z["name"], "status": z.get("status")} for z in result]
        self._zone_cache_at = time.time()
        return self._zone_cache

    def find_zone(self, hostname):
        zones = self.zones()
        best = None
        for z in zones:
            name = z["name"].lower()
            if hostname.lower() == name or hostname.lower().endswith("." + name):
                if best is None or len(name) > len(best["name"]):
                    best = z
        return best

    # ---------------------------------------------------------- dns
    def get_cname(self, zone_id, hostname):
        records = self._request(
            "GET", f"/zones/{zone_id}/dns_records",
            params={"type": "CNAME", "name": hostname, "per_page": 5}) or []
        return records[0] if records else None

    def ensure_cname(self, hostname, tunnel_id):
        zone = self.find_zone(hostname)
        if not zone:
            raise CloudflareError(f"没有找到 {hostname} 所属的 Cloudflare 域名，请先在账号下添加该域名")
        content = f"{tunnel_id}.cfargotunnel.com"
        existing = self.get_cname(zone["id"], hostname)
        body = {"type": "CNAME", "name": hostname, "content": content, "proxied": True, "ttl": 1}
        if existing:
            if existing.get("content") == content and existing.get("proxied"):
                return zone["id"], existing["id"], False
            rec = self._request("PUT", f"/zones/{zone['id']}/dns_records/{existing['id']}", json_body=body)
            return zone["id"], rec["id"], True
        rec = self._request("POST", f"/zones/{zone['id']}/dns_records", json_body=body)
        return zone["id"], rec["id"], True

    def delete_dns(self, zone_id, record_id):
        return self._request("DELETE", f"/zones/{zone_id}/dns_records/{record_id}")

    # ---------------------------------------------------------- tunnels
    def list_tunnels(self):
        if not self.account_id:
            raise CloudflareError("还没有填写 Cloudflare Account ID")
        return self._request(
            "GET", f"/accounts/{self.account_id}/cfd_tunnel",
            params={"is_deleted": "false", "per_page": 50}) or []

    def create_tunnel(self, name, config_src="local"):
        if not self.account_id:
            raise CloudflareError("还没有填写 Cloudflare Account ID")
        return self._request(
            "POST", f"/accounts/{self.account_id}/cfd_tunnel",
            json_body={"name": name, "config_src": config_src})

    def get_tunnel_token(self, cf_id):
        return self._request("GET", f"/accounts/{self.account_id}/cfd_tunnel/{cf_id}/token")

    def delete_tunnel(self, cf_id):
        try:
            self._request("DELETE", f"/accounts/{self.account_id}/cfd_tunnel/{cf_id}/connections")
        except CloudflareError:
            pass
        return self._request("DELETE", f"/accounts/{self.account_id}/cfd_tunnel/{cf_id}")


def decode_token(token):
    """cloudflared token = base64(JSON {a: accountTag, t: tunnelID, s: tunnelSecret})"""
    token = (token or "").strip()
    try:
        padded = token + "=" * (-len(token) % 4)
        data = json.loads(base64.b64decode(padded).decode("utf-8"))
        if not data.get("t") or not data.get("s"):
            raise ValueError("缺少 t / s 字段")
        return {"account_tag": data.get("a", ""), "tunnel_id": data["t"], "secret": data["s"]}
    except Exception as e:
        raise CloudflareError(f"Token 格式不正确，无法解析（{e}）")
