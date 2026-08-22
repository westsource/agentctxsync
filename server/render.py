"""Template rendering, flash messages, request-scoped context."""
import asyncio
import contextvars
from datetime import datetime
import os
import secrets

import jinja2
from fastapi import Request
from fastapi.responses import HTMLResponse

from db import get_conn, plan_limits
from translations import get_translations
# ============================================================

TEMPLATE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates")
jinja_env = jinja2.Environment(
    loader=jinja2.FileSystemLoader(TEMPLATE_DIR),
    autoescape=jinja2.select_autoescape(["html"]),
)

def timestamp_fmt(value):
    if not value:
        return "-"
    try:
        return datetime.fromtimestamp(float(value)).strftime("%Y-%m-%d %H:%M")
    except:
        return str(value)

def msg_time_fmt(value):
    """Message bubble timestamp. Fallback rendered server-side in the
    server's local timezone; the viewer page overrides it client-side
    with the browser's local timezone via the data-ts attribute."""
    if not value:
        return ""
    try:
        return datetime.fromtimestamp(float(value)).strftime("%Y-%m-%d %H:%M:%S")
    except:
        return str(value)

jinja_env.filters["timestamp_fmt"] = timestamp_fmt
jinja_env.filters["msg_time"] = msg_time_fmt


def get_lang():
    request = _current_request_var.get()
    if request:
        # Logged-in users follow their account preference (lang claim in the
        # JWT, set at login and refreshed on switch); guests fall back to the
        # cookie so the landing page remembers the last choice per browser.
        from auth import verify_jwt  # 函数内: 避免 render->auth 循环
        token = request.cookies.get("hsync_token")
        payload = verify_jwt(token) if token else None
        if payload and payload.get("lang"):
            return payload["lang"]
        return request.cookies.get("lang", "zh-CN")
    return "zh-CN"
def render(template_name, context=None):
    ctx = context or {}
    ctx["get_flashed_messages"] = get_flashed_messages
    lang = get_lang()
    ctx["lang"] = lang
    ctx["t"] = get_translations(lang)
    # Surface error/success query params (i18n keys) so templates can show them
    request = _current_request_var.get()
    if request is not None:
        q = request.query_params
        if "error" not in ctx and q.get("error"):
            ctx["error"] = q.get("error")
        if "success" not in ctx and q.get("success"):
            ctx["success"] = q.get("success")
    # Global quota for the sidebar: every logged-in page sees plan/usage.
    # Routers may still pass their own (setdefault keeps theirs).
    ctx.setdefault("quota", _sidebar_quota())
    tmpl = jinja_env.get_template(template_name)
    html = tmpl.render(ctx)
    # Dynamic HTML must not be heuristically cached by browsers/proxies: a
    # stale landing/help page (e.g. an agent rename) would keep showing old
    # text until a hard refresh. Static assets are served separately.
    return HTMLResponse(content=html,
                        headers={"Cache-Control": "no-store"})


def _sidebar_quota():
    """Plan + usage for the sidebar, or None when it must stay hidden.

    Returns None for guests and for deployments with no limited plan
    reachable (no free users, no free-granting invites), so the sidebar
    block is invisible there. Runs on every logged-in page render — a few
    indexed queries per request, acceptable at this scale.
    """
    from auth import verify_jwt  # 函数内: 避免 render->auth 循环
    from invites import quota_ui_active  # 函数内: 避免 render->invites 循环
    request = _current_request_var.get()
    if request is None:
        return None
    token = request.cookies.get("hsync_token")
    payload = verify_jwt(token) if token else None
    if not payload or not payload.get("sub"):
        return None
    with get_conn() as conn:
        if not quota_ui_active(conn):
            return None
        c = conn.cursor()
        c.execute("SELECT plan FROM users WHERE id = %s", (payload["sub"],))
        prow = c.fetchone()
        plan = (prow[0] if prow else None) or "free"
        max_sessions, _ = plan_limits(plan, conn)
        c.execute("""SELECT COUNT(*) FROM sessions s
                     JOIN workspaces w ON s.workspace_id = w.id
                     WHERE w.user_id = %s AND s.archived = 0""", (payload["sub"],))
        active_count = c.fetchone()[0]
    return {"plan": plan, "max_sessions": max_sessions, "active_count": active_count}


async def render_page(template_name, context=None):
    """render() off the event loop.

    Jinja rendering of large pages (session viewer, all-sessions list) can
    take tens of milliseconds; running it in the default executor keeps the
    event loop free for other requests. contextvars.copy_context() carries
    the per-request ContextVar (_current_request_var) into the worker thread
    so language / flash / query-param lookups keep working there.
    """
    ctx = contextvars.copy_context()
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, lambda: ctx.run(render, template_name, context))

# ============================================================
# Flash Messages
# ============================================================

_flash_store = {}

def flash(response, message, category="success"):
    token = secrets.token_hex(16)
    if not hasattr(response, "set_cookie"):
        pass
    _flash_store.setdefault(token, []).append({"message": message, "category": category})
    response.set_cookie(key="_flash", value=token, httponly=True, max_age=10)

def get_flashed_messages():
    from starlette.requests import Request as StarletteRequest
    request = _current_request_var.get()
    token = request.cookies.get("_flash") if request else None
    if not token:
        return []
    messages = _flash_store.pop(token, [])
    return messages

# Request-scoped context: carries the current HTTP request into template
# rendering / language / flash lookups WITHOUT a process-global. A plain
# global races under FastAPI's async concurrency (request A's await lets
# request B overwrite it, so A resumes reading B's cookies/flash) -- the
# ContextVar is per-task and is also propagated into executor threads via
# contextvars.copy_context() (see render_page).
_current_request_var = contextvars.ContextVar("current_request", default=None)

async def flash_middleware(request: Request, call_next):
    token = _current_request_var.set(request)
    try:
        return await call_next(request)
    finally:
        _current_request_var.reset(token)

# /web/* paths reachable while a forced password change is pending.
def make_flash(response, message, category="success"):
    token = secrets.token_hex(16)
    _flash_store[token] = [{"message": message, "category": category}]
    response.set_cookie(key="_flash", value=token, httponly=True, max_age=10)
    return response

# Make get_flashed_messages available in templates
def template_context():
    return {"get_flashed_messages": get_flashed_messages}

# ============================================================
# Database
