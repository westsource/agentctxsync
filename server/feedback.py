"""User feedback domain ("问题反馈"): logged-in users submit issues/suggestions.

Admins list every submission and can toggle its resolved status; regular
users see only their own. Feedback rows are otherwise immutable.
"""
from datetime import datetime

import psycopg2.extras

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from auth import get_current_user
from db import get_conn, get_nav_workspaces
from render import get_lang, get_translations, make_flash, render_page

router = APIRouter()

CATEGORIES = ("bug", "feature", "other")


@router.get("/web/feedback", response_class=HTMLResponse)
async def web_feedback(request: Request):
    try:
        user = get_current_user(request)
    except Exception:
        return RedirectResponse(url="/web/login")
    nav_ws = get_nav_workspaces(user["sub"])
    with get_conn() as conn:
        c = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        if user.get("is_admin"):
            c.execute("""
                SELECT f.*, COALESCE(u.display_name, u.username) AS author_name,
                       u.username AS author_username
                FROM feedback f JOIN users u ON f.user_id = u.id
                ORDER BY f.created_at DESC
            """)
        else:
            c.execute("""
                SELECT f.*, COALESCE(u.display_name, u.username) AS author_name,
                       u.username AS author_username
                FROM feedback f JOIN users u ON f.user_id = u.id
                WHERE f.user_id = %s
                ORDER BY f.created_at DESC
            """, (user["sub"],))
        feedback = [dict(r) for r in c.fetchall()]
    ctx = {"user": user, "workspaces": nav_ws, "active_page": "feedback",
           "feedback": feedback, "categories": CATEGORIES}
    return await render_page("feedback.html", ctx)


@router.post("/web/feedback/submit", response_class=HTMLResponse)
async def web_feedback_submit(request: Request):
    try:
        user = get_current_user(request)
    except Exception:
        return RedirectResponse(url="/web/login")
    body = await request.form()
    title = (body.get("title") or "").strip()
    content = (body.get("content") or "").strip()
    category = (body.get("category") or "other").strip()
    if category not in CATEGORIES:
        category = "other"
    t = get_translations(get_lang())
    if not title or not content:
        resp = RedirectResponse(url="/web/feedback", status_code=303)
        make_flash(resp, t.get("feedback_required", "Title and description are required"), "error")
        return resp
    now = datetime.now().timestamp()
    with get_conn() as conn:
        c = conn.cursor()
        c.execute(
            "INSERT INTO feedback (user_id, title, content, category, status, created_at) "
            "VALUES (%s, %s, %s, %s, 'open', %s)",
            (user["sub"], title, content, category, now))
    resp = RedirectResponse(url="/web/feedback", status_code=303)
    make_flash(resp, t.get("feedback_submit_ok", "Feedback submitted"), "success")
    return resp


@router.post("/web/feedback/{fid}/resolve", response_class=HTMLResponse)
async def web_feedback_resolve(fid: int, request: Request):
    """Admin-only status toggle: open -> resolved (record who/when), else reopen."""
    try:
        user = get_current_user(request)
        if not user.get("is_admin"):
            return RedirectResponse(url="/web/", status_code=303)
    except Exception:
        return RedirectResponse(url="/web/login")
    now = datetime.now().timestamp()
    with get_conn() as conn:
        c = conn.cursor()
        c.execute("SELECT status FROM feedback WHERE id = %s", (fid,))
        row = c.fetchone()
        if row and row[0] == "open":
            c.execute("UPDATE feedback SET status = 'resolved', resolved_at = %s, resolved_by = %s WHERE id = %s",
                      (now, user["sub"], fid))
        else:
            c.execute("UPDATE feedback SET status = 'open', resolved_at = NULL, resolved_by = NULL WHERE id = %s",
                      (fid,))
    return RedirectResponse(url="/web/feedback", status_code=303)
