"""Global search across the user's workspaces.

Tenant isolation (decision 2026-08-27, docs/SEARCH.md): session content is
always filtered by ``workspaces.user_id`` -- admins get no extra scope, per
the README guarantee "admins never read anyone's sessions". Tool messages
are excluded from content search (binary/API noise); hidden/archived
sessions and hidden messages are skipped. Backed by pg_trgm GIN indexes on
messages.content and sessions.title.
"""
import re

import psycopg2.extras

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from auth import get_current_user
from db import get_conn, rel_sync_label
from render import get_lang, render_page

router = APIRouter()

PAGE_SIZE = 20

# Escape LIKE wildcards so user input is matched literally.
_LIKE_ESC = re.compile(r"([\\%_])")


def _like_pattern(q: str) -> str:
    return "%" + _LIKE_ESC.sub(r"\\\1", q) + "%"


def _search(user_id, q: str, page: int):
    """Return (session_hits, message_hits, session_total, message_total)."""
    like = _like_pattern(q)
    offset = (page - 1) * PAGE_SIZE
    with get_conn() as conn:
        c = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        # Session title/id hits
        c.execute("""
            SELECT s.id, s.workspace_id, s.title, s.agent_type,
                   s.message_count, w.name AS workspace_name,
                   (SELECT MAX(m.timestamp) FROM messages m
                    WHERE m.session_id = s.id AND m.workspace_id = s.workspace_id
                      AND COALESCE(m.hidden,0) = 0) AS synced_at
            FROM sessions s
            JOIN workspaces w ON s.workspace_id = w.id
            WHERE w.user_id = %s
              AND COALESCE(s.hidden,0) = 0 AND COALESCE(s.archived,0) = 0
              AND (s.title ILIKE %s ESCAPE '\\' OR s.id ILIKE %s ESCAPE '\\')
            ORDER BY synced_at DESC NULLS LAST
            LIMIT %s OFFSET %s
        """, (user_id, like, like, PAGE_SIZE, offset))
        session_hits = [dict(r) for r in c.fetchall()]
        for r in session_hits:
            r["sync_label"] = rel_sync_label(r.get("synced_at"))
        c.execute("""
            SELECT COUNT(*) AS total
            FROM sessions s
            JOIN workspaces w ON s.workspace_id = w.id
            WHERE w.user_id = %s
              AND COALESCE(s.hidden,0) = 0 AND COALESCE(s.archived,0) = 0
              AND (s.title ILIKE %s ESCAPE '\\' OR s.id ILIKE %s ESCAPE '\\')
        """, (user_id, like, like))
        session_total = c.fetchone()["total"]

        # Message content hits (exclude tool messages)
        c.execute("""
            SELECT m.id, m.session_id, m.workspace_id, m.role, m.timestamp,
                   m.content, s.title, s.agent_type, w.name AS workspace_name
            FROM messages m
            JOIN sessions s ON s.id = m.session_id AND s.workspace_id = m.workspace_id
            JOIN workspaces w ON s.workspace_id = w.id
            WHERE w.user_id = %s
              AND COALESCE(m.hidden,0) = 0
              AND COALESCE(s.hidden,0) = 0 AND COALESCE(s.archived,0) = 0
              AND m.role <> 'tool'
              AND m.content ILIKE %s ESCAPE '\\'
            ORDER BY m.timestamp DESC
            LIMIT %s OFFSET %s
        """, (user_id, like, PAGE_SIZE, offset))
        message_hits = [dict(r) for r in c.fetchall()]
        c.execute("""
            SELECT COUNT(*) AS total
            FROM messages m
            JOIN sessions s ON s.id = m.session_id AND s.workspace_id = m.workspace_id
            JOIN workspaces w ON s.workspace_id = w.id
            WHERE w.user_id = %s
              AND COALESCE(m.hidden,0) = 0
              AND COALESCE(s.hidden,0) = 0 AND COALESCE(s.archived,0) = 0
              AND m.role <> 'tool'
              AND m.content ILIKE %s ESCAPE '\\'
        """, (user_id, like))
        message_total = c.fetchone()["total"]
    for m in message_hits:
        # compact preview around the first hit
        idx = m["content"].lower().find(q.lower())
        if idx >= 0:
            start = max(0, idx - 40)
            end = min(len(m["content"]), idx + len(q) + 60)
            snippet = m["content"][start:end]
            if start > 0:
                snippet = "…" + snippet
            if end < len(m["content"]):
                snippet += "…"
            m["snippet"] = snippet
        else:
            m["snippet"] = (m["content"] or "")[:140]
    return session_hits, message_hits, session_total, message_total


@router.get("/web/search", response_class=HTMLResponse)
async def web_search(request: Request):
    try:
        user = get_current_user(request)
    except Exception:
        return RedirectResponse(url="/web/login")
    q = (request.query_params.get("q") or "").strip()
    try:
        page = max(1, int(request.query_params.get("page", "1")))
    except ValueError:
        page = 1
    sessions_hit = messages_hit = []
    s_total = m_total = 0
    if q:
        sessions_hit, messages_hit, s_total, m_total = _search(
            user["sub"], q, page)
    ctx = {
        "user": user,
        "active_page": "search",
        "q": q,
        "page": page,
        "sessions": sessions_hit,
        "messages": messages_hit,
        "session_total": s_total,
        "message_total": m_total,
        "session_pages": max(1, -(-s_total // PAGE_SIZE)),
        "message_pages": max(1, -(-m_total // PAGE_SIZE)),
    }
    return await render_page("search.html", ctx)
