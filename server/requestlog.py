"""Per-request source logging.

Every request is logged with the client's real IP, the Host header it used
(domain vs IP:port), protocol, path and -- for sync endpoints -- the device
id. This distinguishes clients syncing through the public domain (nginx:
Host=www.agentctxsync.com, https) from direct IP:port access
(Host=47.95.214.236:8765, http).

Log lines carry a REQ prefix, e.g.:
  REQ src=203.0.113.7 host=www.agentctxsync.com proto=https method=POST
      path=/push status=200 ms=42 device=my-pc

Access statistics: every counted request increments the daily counter in
the `access_stats` table, bucketed by channel -- 'domain' when the Host
header is a hostname, 'ip' when it is an IP literal (direct IP:port
access) -- and by kind -- 'web' for browser pages (/web/* and the /
landing page), 'api' for sync push/pull and the rest. The admin page
(/web/admin/access) shows the split. Static assets and health checks are
excluded so the counts reflect real usage.

The middleware reads the request body BEFORE passing it downstream;
starlette caches the body so handlers calling request.json() still see it.
"""
import ipaddress
import json
import time
from datetime import date
from urllib.parse import unquote

from fastapi import Request

from db import get_conn

# POST bodies carry device_id for these paths.
_SYNC_POST = {"/push", "/pull", "/api/projects/push", "/api/projects/pull"}

# Requests that are not real usage: asset files, health checks, favicon.
_SKIP_PREFIXES = ("/static/",)
_SKIP_PATHS = {"/health", "/favicon.ico"}


def classify_channel(host):
    """'domain' when Host is a hostname, 'ip' when it is an IP literal.

    Strips the port suffix (IPv4:port, [IPv6]:port, host:port). Direct
    machine access via 'localhost' counts as 'ip'.
    """
    h = (host or "").strip().lower()
    if not h:
        return "ip"
    if h.startswith("["):
        addr = h.split("]", 1)[0][1:]
    elif h.count(":") == 1:
        addr = h.rsplit(":", 1)[0]
    else:
        addr = h
    if addr == "localhost":
        return "ip"
    try:
        ipaddress.ip_address(addr)
    except ValueError:
        return "domain"
    return "ip"


def classify_kind(path):
    """'web' for browser pages (/web/* and the / landing page), else 'api'."""
    return "web" if path == "/" or path.startswith("/web/") else "api"


def _record_access(host, path):
    """Increment today's counter for the (channel, kind) of this request."""
    try:
        with get_conn() as conn:
            c = conn.cursor()
            c.execute(
                "INSERT INTO access_stats (stat_date, channel, kind, count) "
                "VALUES (%s, %s, %s, 1) "
                "ON CONFLICT (stat_date, channel, kind) "
                "DO UPDATE SET count = access_stats.count + 1",
                (date.today(), classify_channel(host), classify_kind(path)),
            )
    except Exception:
        pass  # statistics must never break the request path


async def request_log_middleware(request: Request, call_next):
    body = None
    if request.method == "POST" and request.url.path in _SYNC_POST:
        try:
            body = await request.body()
        except Exception:
            body = None

    start = time.monotonic()
    status = 0
    try:
        response = await call_next(request)
        status = response.status_code
        return response
    finally:
        try:
            host = request.headers.get("host", "")
            path = request.url.path
            if not path.startswith(_SKIP_PREFIXES) and path not in _SKIP_PATHS:
                _record_access(host, path)
            device_id = ""
            if body:
                try:
                    device_id = str(json.loads(body).get("device_id", ""))
                except Exception:
                    pass
            if not device_id and request.url.path.startswith("/status/"):
                device_id = unquote(request.url.path.rsplit("/", 1)[-1])
            fwd = request.headers.get("x-forwarded-for")
            ip = (fwd.split(",")[0].strip() if fwd
                  else (request.client.host if request.client else "?"))
            line = (f"REQ src={ip} host={request.headers.get('host', '')} "
                    f"proto={request.url.scheme} method={request.method} "
                    f"path={request.url.path} status={status} "
                    f"ms={int((time.monotonic() - start) * 1000)}")
            if device_id:
                line += f" device={device_id}"
            print(line, flush=True)
        except Exception:
            pass  # logging must never break the request path
