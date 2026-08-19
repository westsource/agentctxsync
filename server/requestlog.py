"""Per-request source logging.

Every request is logged with the client's real IP, the Host header it used
(domain vs IP:port), protocol, path and -- for sync endpoints -- the device
id. This distinguishes clients syncing through the public domain (nginx:
Host=www.agentctxsync.com, https) from direct IP:port access
(Host=47.95.214.236:8765, http).

Log lines carry a REQ prefix, e.g.:
  REQ src=203.0.113.7 host=www.agentctxsync.com proto=https method=POST
      path=/push status=200 ms=42 device=my-pc

The middleware reads the request body BEFORE passing it downstream;
starlette caches the body so handlers calling request.json() still see it.
"""
import json
import time
from urllib.parse import unquote

from fastapi import Request

# POST bodies carry device_id for these paths.
_SYNC_POST = {"/push", "/pull", "/api/projects/push", "/api/projects/pull"}


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
