"""Admin domain: user and workspace management (admin only)."""
from datetime import date, datetime

import psycopg2.extras

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from auth import get_current_user, hash_password, require_admin
from db import get_conn, get_nav_workspaces
from render import render_page

router = APIRouter()
@router.get("/web/admin/users", response_class=HTMLResponse)
async def web_admin_users(request: Request):
    try:
        user = get_current_user(request)
        if not user.get("is_admin"):
            return RedirectResponse(url="/web/")
    except:
        return RedirectResponse(url="/web/login")
    nav_ws = get_nav_workspaces(user["sub"])
    with get_conn() as conn:
        c = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        c.execute("SELECT u.*, COUNT(w.id) as ws_count FROM users u LEFT JOIN workspaces w ON w.user_id = u.id GROUP BY u.id ORDER BY u.created_at DESC")
        users = [dict(r) for r in c.fetchall()]
    ctx = {"user": user, "workspaces": nav_ws, "active_page": "admin_users", "users": users}
    return await render_page("admin_users.html", ctx)

@router.post("/web/admin/user/create", response_class=HTMLResponse)
async def web_create_user(request: Request):
    try:
        admin = get_current_user(request)
        if not admin.get("is_admin"):
            return RedirectResponse(url="/web/")
    except:
        return RedirectResponse(url="/web/login")
    body = await request.form()
    username = body.get("username", "").strip()
    display_name = body.get("display_name", "").strip() or username
    password = body.get("password", "")
    is_admin = body.get("is_admin") == "true"
    if not username or len(password) < 6:
        return RedirectResponse(url="/web/admin/users", status_code=303)
    with get_conn() as conn:
        c = conn.cursor()
        try:
            c.execute("INSERT INTO users (username, password_hash, display_name, is_admin, created_at) VALUES (%s, %s, %s, %s, %s)",
                      (username, hash_password(password), display_name, is_admin, datetime.now().timestamp()))
        except Exception:
            return RedirectResponse(url="/web/admin/users", status_code=303)
    return RedirectResponse(url="/web/admin/users", status_code=303)

@router.get("/web/admin/user/{uid}/edit", response_class=HTMLResponse)
async def web_edit_user_form(uid: int, request: Request):
    try:
        admin = get_current_user(request)
        if not admin.get("is_admin"):
            return RedirectResponse(url="/web/")
    except:
        return RedirectResponse(url="/web/login")
    return RedirectResponse(url="/web/admin/users", status_code=303)

@router.post("/web/admin/user/{uid}/edit", response_class=HTMLResponse)
async def web_edit_user(uid: int, request: Request):
    try:
        admin = get_current_user(request)
        if not admin.get("is_admin"):
            return RedirectResponse(url="/web/")
    except:
        return RedirectResponse(url="/web/login")
    body = await request.form()
    display_name = body.get("display_name", "").strip()
    new_password = body.get("new_password", "").strip()
    is_admin = body.get("is_admin") == "true"
    with get_conn() as conn:
        c = conn.cursor()
        c.execute("SELECT username FROM users WHERE id = %s", (uid,))
        target = c.fetchone()
        if target and target[0] == "admin":
            is_admin = True
        if new_password and len(new_password) >= 6:
            c.execute("UPDATE users SET display_name = %s, password_hash = %s, is_admin = %s WHERE id = %s",
                      (display_name, hash_password(new_password), is_admin, uid))
        else:
            c.execute("UPDATE users SET display_name = %s, is_admin = %s WHERE id = %s",
                      (display_name, is_admin, uid))
    return RedirectResponse(url="/web/admin/users", status_code=303)

@router.get("/web/admin/user/{uid}/toggle")
async def web_toggle_user(uid: int, request: Request):
    try:
        user = get_current_user(request)
        if not user.get("is_admin"):
            return RedirectResponse(url="/web/")
    except:
        return RedirectResponse(url="/web/login")
    with get_conn() as conn:
        c = conn.cursor()
        c.execute("UPDATE users SET is_active = NOT is_active WHERE id = %s", (uid,))
    return RedirectResponse(url="/web/admin/users", status_code=303)

@router.get("/web/admin/workspaces", response_class=HTMLResponse)
async def web_admin_workspaces(request: Request):
    try:
        user = get_current_user(request)
        if not user.get("is_admin"):
            return RedirectResponse(url="/web/")
    except:
        return RedirectResponse(url="/web/login")
    nav_ws = get_nav_workspaces(user["sub"])
    with get_conn() as conn:
        c = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        # api_key intentionally excluded: keys belong to the workspace owner.
        c.execute("""SELECT w.id, w.name, w.user_id, w.description, w.created_at,
            u.username as owner_username, u.display_name as owner_name,
            (SELECT COUNT(*) FROM sessions s WHERE s.workspace_id = w.id) as session_count,
            (SELECT COUNT(*) FROM messages m WHERE m.workspace_id = w.id) as message_count,
            (SELECT MAX(st.last_sync_at) FROM sync_state st WHERE st.workspace_id = w.id) as last_sync_at
            FROM workspaces w JOIN users u ON w.user_id = u.id ORDER BY w.created_at DESC""")
        all_ws = [dict(r) for r in c.fetchall()]
        c.execute("SELECT COUNT(*) as cnt FROM sessions")
        ts = c.fetchone()["cnt"]
        c.execute("SELECT COUNT(*) as cnt FROM messages")
        tm = c.fetchone()["cnt"]
    ctx = {"user": user, "workspaces": nav_ws, "active_page": "admin_workspaces",
           "all_workspaces": all_ws, "total_sessions": ts, "total_messages": tm}
    return await render_page("admin_workspaces.html", ctx)

@router.get("/web/admin/access", response_class=HTMLResponse)
async def web_admin_access(request: Request):
    try:
        user = get_current_user(request)
        if not user.get("is_admin"):
            return RedirectResponse(url="/web/")
    except:
        return RedirectResponse(url="/web/login")
    nav_ws = get_nav_workspaces(user["sub"])
    with get_conn() as conn:
        c = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        c.execute("""SELECT stat_date, channel, kind, count FROM access_stats
                     ORDER BY stat_date DESC""")
        rows = [dict(r) for r in c.fetchall()]
    days = {}
    for r in rows:
        d = days.setdefault(r["stat_date"], {
            "date": r["stat_date"],
            "web_domain": 0, "api_domain": 0, "web_ip": 0, "api_ip": 0,
            "total": 0})
        key = f'{r["kind"]}_{r["channel"]}'
        if key in d:
            d[key] = r["count"]
            d["total"] += r["count"]
    today_iso = date.today().isoformat()
    today = days.get(today_iso) or {
        "date": today_iso, "web_domain": 0, "api_domain": 0,
        "web_ip": 0, "api_ip": 0, "total": 0}
    ctx = {"user": user, "workspaces": nav_ws, "active_page": "admin_access",
           "days": list(days.values()), "today": today,
           "total_web_domain": sum(d["web_domain"] for d in days.values()),
           "total_api_domain": sum(d["api_domain"] for d in days.values()),
           "total_web_ip": sum(d["web_ip"] for d in days.values()),
           "total_api_ip": sum(d["api_ip"] for d in days.values())}
    return await render_page("admin_access.html", ctx)


@router.get("/web/admin/access/devices", response_class=HTMLResponse)
async def web_admin_access_devices(request: Request):
    """Per-device API access drill-down: which sync machines talk through
    the domain vs direct IP today (access_device rows)."""
    try:
        user = get_current_user(request)
        if not user.get("is_admin"):
            return RedirectResponse(url="/web/")
    except:
        return RedirectResponse(url="/web/login")
    nav_ws = get_nav_workspaces(user["sub"])
    with get_conn() as conn:
        c = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        # Postgres resolves bare output-column names in ORDER BY, but NOT
        # aliases used inside an ORDER BY expression -- so order over a
        # subquery that already materialized the alias columns.
        c.execute("""SELECT * FROM (
                            SELECT device_id,
                                   COALESCE(SUM(count) FILTER (WHERE channel = 'domain'), 0) AS domain_count,
                                   COALESCE(SUM(count) FILTER (WHERE channel = 'ip'), 0) AS ip_count,
                                   MAX(last_seen) AS last_seen
                            FROM access_device
                            WHERE stat_date = %s
                            GROUP BY device_id
                     ) t ORDER BY domain_count + ip_count DESC""",
                  (date.today(),))
        devices = [dict(r) for r in c.fetchall()]
    ctx = {"user": user, "workspaces": nav_ws, "active_page": "admin_access",
           "devices": devices, "day": date.today().isoformat()}
    return await render_page("admin_access_devices.html", ctx)

@router.get("/web/admin/invites", response_class=HTMLResponse)
async def web_admin_invites_old(request: Request):
    return RedirectResponse(url="/web/invites", status_code=303)

@router.get("/api/admin/users")
async def api_admin_users(user: dict = Depends(require_admin)):
    with get_conn() as conn:
        c = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        c.execute("SELECT u.*, COUNT(w.id) as ws_count FROM users u LEFT JOIN workspaces w ON w.user_id = u.id GROUP BY u.id ORDER BY u.created_at DESC")
        return [dict(r) for r in c.fetchall()]

@router.post("/api/admin/users/{uid}/toggle")
async def api_admin_toggle_user(uid: int, user: dict = Depends(require_admin)):
    with get_conn() as conn:
        c = conn.cursor()
        c.execute("UPDATE users SET is_active = NOT is_active WHERE id = %s", (uid,))
    return {"success": True}

@router.get("/api/admin/workspaces")
async def api_admin_workspaces(user: dict = Depends(require_admin)):
    with get_conn() as conn:
        c = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        # Never expose api_key to admins: keys belong to the workspace owner.
        c.execute("""SELECT w.id, w.name, w.user_id, w.description, w.created_at,
            u.username as owner,
            (SELECT MAX(st.last_sync_at) FROM sync_state st WHERE st.workspace_id = w.id) as last_sync_at
            FROM workspaces w JOIN users u ON w.user_id = u.id ORDER BY w.created_at DESC""")
        return [dict(r) for r in c.fetchall()]

