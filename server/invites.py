"""Invite-code management domain (all logged-in users)."""
import secrets
from datetime import datetime

import psycopg2.extras

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from auth import get_current_user
from db import get_conn, get_nav_workspaces
from render import render_page

router = APIRouter()
def generate_invite_code():
    return "HSYNC-" + secrets.token_hex(4).upper()

def quota_ui_active(conn=None):
    """Whether the quota UI should be shown at all.

    The quota mechanism is meant for deployments where a limited plan is
    actually reachable through the invite/registration flow. When every
    non-revoked invite grants 'unlimited' (the default deployment), the
    dashboard usage panel and the invites grant-plan controls stay hidden:
    admins and users never see plan/quota information. Enforcement itself
    still applies server-side (an operator who later configures limits via
    quota_config or flips a user's plan directly gets enforcement without
    any UI change). Pass an open connection to stay inside a transaction.
    """
    def _query(c):
        # Show the quota UI when a limited plan is reachable: a free-granting
        # invite exists, OR open registration has produced free-plan users
        # (default since the no-invite registration now grants 'free').
        c.execute("SELECT 1 FROM invites WHERE revoked = 0 AND grant_plan != 'unlimited' LIMIT 1")
        if c.fetchone():
            return True
        c.execute("SELECT 1 FROM users WHERE plan = 'free' LIMIT 1")
        return c.fetchone() is not None
    if conn is not None:
        return _query(conn.cursor())
    with get_conn() as conn:
        return _query(conn.cursor())


# ============================================================
# Authentication
@router.get("/web/invites", response_class=HTMLResponse)
async def web_invites(request: Request):
    """Invite management for every logged-in user. Admins see all invites and
    their results; regular users see only the ones they created."""
    try:
        user = get_current_user(request)
    except:
        return RedirectResponse(url="/web/login")
    nav_ws = get_nav_workspaces(user["sub"])
    with get_conn() as conn:
        c = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        if user.get("is_admin"):
            c.execute("""SELECT i.*, u.username AS creator_name,
                         (SELECT username FROM users WHERE id = i.used_by) AS used_by_name
                         FROM invites i LEFT JOIN users u ON u.id = i.created_by
                         ORDER BY i.created_at DESC""")
        else:
            c.execute("""SELECT i.*, u.username AS creator_name,
                         (SELECT username FROM users WHERE id = i.used_by) AS used_by_name
                         FROM invites i LEFT JOIN users u ON u.id = i.created_by
                         WHERE i.created_by = %s
                         ORDER BY i.created_at DESC""", (user["sub"],))
        invites = [dict(r) for r in c.fetchall()]
        quota_ui = quota_ui_active(conn)
    ctx = {"user": user, "workspaces": nav_ws, "active_page": "admin_invites",
           "invites": invites, "now": datetime.now().timestamp(),
           "quota_ui": quota_ui,
           "base_url": str(request.base_url)}
    return await render_page("admin_invites.html", ctx)

@router.post("/web/invite/create", response_class=HTMLResponse)
async def web_create_invite(request: Request):
    try:
        user = get_current_user(request)
    except:
        return RedirectResponse(url="/web/login")
    body = await request.form()
    note = body.get("note", "").strip()
    grant_plan = body.get("grant_plan", "unlimited").strip() or "unlimited"
    if grant_plan not in ("free", "unlimited"):
        grant_plan = "unlimited"
    try:
        expiry_days = int(body.get("expiry_days", "0") or 0)
    except ValueError:
        expiry_days = 0
    code = generate_invite_code()
    now = datetime.now().timestamp()
    expires_at = now + expiry_days * 86400 if expiry_days > 0 else None
    with get_conn() as conn:
        c = conn.cursor()
        c.execute("INSERT INTO invites (code, created_by, expires_at, note, grant_plan, created_at) VALUES (%s, %s, %s, %s, %s, %s)",
                  (code, user["sub"], expires_at, note, grant_plan, now))
    return RedirectResponse(url="/web/invites", status_code=303)

@router.post("/web/invite/{inv_id}/revoke", response_class=HTMLResponse)
async def web_revoke_invite(inv_id: int, request: Request):
    try:
        user = get_current_user(request)
    except:
        return RedirectResponse(url="/web/login")
    with get_conn() as conn:
        c = conn.cursor()
        if user.get("is_admin"):
            c.execute("UPDATE invites SET revoked = 1 WHERE id = %s", (inv_id,))
        else:
            c.execute("UPDATE invites SET revoked = 1 WHERE id = %s AND created_by = %s", (inv_id, user["sub"]))
    return RedirectResponse(url="/web/invites", status_code=303)

