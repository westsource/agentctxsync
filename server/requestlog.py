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

Per-device rows: sync requests carrying a device_id also bump that
device's daily (device, channel) counter in `access_device`, recording the
client's reported MCP version (POST body field `client_version`) so the
admin drill-down shows each device's version at last sync. Requests
without a version keep the previously recorded one.

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


def _record_access(host, path, device_id="", client_version=""):
    """Increment today's counters for this request.

    Always bumps the (channel, kind) bucket in access_stats; when the
    request carries a sync client's device_id it also bumps that device's
    (channel) row in access_device so the admin drill-down can answer which
    machines sync through the domain vs direct IP. A non-empty
    client_version reported by the client is stored on the device row
    (latest wins); an empty one leaves the previously recorded version
    untouched.
    """
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
            if device_id:
                c.execute(
                    "INSERT INTO access_device "
                    "(stat_date, device_id, channel, count, last_seen, client_version) "
                    "VALUES (%s, %s, %s, 1, %s, %s) "
                    "ON CONFLICT (stat_date, device_id, channel) "
                    "DO UPDATE SET count = access_device.count + 1, "
                    "last_seen = EXCLUDED.last_seen, "
                    "client_version = COALESCE(EXCLUDED.client_version, "
                    "access_device.client_version)",
                    (date.today(), device_id, classify_channel(host),
                     time.time(), client_version or None),
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
            device_id = ""
            client_version = ""
            if body:
                try:
                    data = json.loads(body)
                    device_id = str(data.get("device_id", ""))
                    client_version = str(data.get("client_version", ""))
                except Exception:
                    pass
            if not device_id and path.startswith("/status/"):
                device_id = unquote(path.rsplit("/", 1)[-1])
            if not path.startswith(_SKIP_PREFIXES) and path not in _SKIP_PATHS:
                _record_access(host, path, device_id, client_version)
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
