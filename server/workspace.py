"""Workspace domain: dashboards, session viewer, workspace CRUD + REST API."""
import gzip
import html
import json
import re
import time
from datetime import datetime

import markdown
import psycopg2.extras
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from agents import AGENTS
from auth import generate_api_key, get_current_user, hash_password, verify_password
from db import (_pg_val, get_conn, get_nav_workspaces,
               get_user_workspaces, plan_limits, rel_sync_label)
from invites import quota_ui_active
from render import get_lang, get_translations, make_flash, render_page

router = APIRouter()
# ============================================================

@router.get("/web/", response_class=HTMLResponse)
async def web_dashboard(request: Request):
    try:
        user = get_current_user(request)
    except:
        return RedirectResponse(url="/web/login")
    nav_ws = get_nav_workspaces(user["sub"])
    ws_list = get_user_workspaces(user["sub"])
    with get_conn() as conn:
        c = conn.cursor()
        c.execute("SELECT COUNT(*) FROM sessions WHERE workspace_id IN (SELECT id FROM workspaces WHERE user_id = %s)", (user["sub"],))
        total_sessions = c.fetchone()[0]
        c.execute("SELECT COUNT(*) FROM messages WHERE workspace_id IN (SELECT id FROM workspaces WHERE user_id = %s)", (user["sub"],))
        total_messages = c.fetchone()[0]
        # Quota usage shown to the user (mirrors the /push gate: active only).
        # Hidden entirely when the deployment has no limited invite path
        # (invites/registrations all unlimited) — admins and users stay
        # unaware of the quota mechanism.
        quota = None
        if quota_ui_active(conn):
            c.execute("SELECT plan FROM users WHERE id = %s", (user["sub"],))
            prow = c.fetchone()
            plan = (prow[0] if prow else None) or "free"
            max_sessions, _ = plan_limits(plan, conn)
            c.execute("""SELECT COUNT(*) FROM sessions s
                         JOIN workspaces w ON s.workspace_id = w.id
                         WHERE w.user_id = %s AND s.archived = 0""", (user["sub"],))
            active_count = c.fetchone()[0]
            quota = {"plan": plan, "max_sessions": max_sessions, "active_count": active_count}
    # 最近同步的会话（跨工作空间，按最后消息时间倒序取 6 条）
    recent_sessions = []
    with get_conn() as conn:
        c = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        c.execute("""
            SELECT s.id, s.workspace_id, s.title, s.agent_type, s.message_count,
                   (SELECT MAX(m.timestamp) FROM messages m
                    WHERE m.session_id = s.id AND m.workspace_id = s.workspace_id
                      AND COALESCE(m.hidden,0) = 0) AS synced_at,
                   w.name AS workspace_name
            FROM sessions s
            JOIN workspaces w ON s.workspace_id = w.id
            WHERE w.user_id = %s AND COALESCE(s.hidden, 0) = 0 AND COALESCE(s.archived, 0) = 0
            ORDER BY synced_at DESC
            LIMIT 6
        """, (user["sub"],))
        for r in c.fetchall():
            r["sync_label"] = rel_sync_label(r.get("synced_at"))
            recent_sessions.append(r)
    # 曾同步的设备：管理员显示全域所有设备，普通用户仅显示自己的设备。
    is_admin = bool(user.get("is_admin"))
    scope_clause = "" if is_admin else "WHERE w.user_id = %s"
    params = () if is_admin else (user["sub"],)
    devices = []
    with get_conn() as conn:
        c = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        c.execute(f"""
            SELECT ss.device_id, ss.workspace_id, ss.last_sync_at,
                   ss.sessions_synced, ss.messages_synced,
                   w.name AS workspace_name,
                   COALESCE(u.display_name, u.username) AS user_display_name
            FROM sync_state ss
            JOIN workspaces w ON ss.workspace_id = w.id
            JOIN users u ON w.user_id = u.id
            {scope_clause}
            ORDER BY ss.last_sync_at DESC
        """, params)
        devices = [dict(r) for r in c.fetchall()]
    ctx = {"user": user, "workspaces": nav_ws, "active_page": "dashboard",
           "ws_list": ws_list, "total_sessions": total_sessions, "total_messages": total_messages,
           "quota": quota, "recent_sessions": recent_sessions, "devices": devices}
    return await render_page("dashboard.html", ctx)

@router.get("/web/all-sessions", response_class=HTMLResponse)
async def web_all_sessions(request: Request):
    """全部会话：跨工作空间统一列表，支持搜索/工作空间/Agent 筛选与分页。"""
    try:
        user = get_current_user(request)
    except:
        return RedirectResponse(url="/web/login")
    nav_ws = get_nav_workspaces(user["sub"])
    ws_options = get_user_workspaces(user["sub"])
    params = request.query_params
    q = (params.get("q") or "").strip()
    ws_filter = (params.get("ws") or "").strip()
    agent_filter = (params.get("agent") or "").strip()
    try:
        page = max(1, int(params.get("page") or 1))
    except (TypeError, ValueError):
        page = 1
    size = params.get("size") or "20"
    size = int(size) if size in ("20", "50", "100") else 20
    AGENT_OPTIONS = sorted(AGENTS)
    where = ["w.user_id = %s", "COALESCE(s.hidden, 0) = 0", "COALESCE(s.archived, 0) = 0"]
    args = [user["sub"]]
    if ws_filter and ws_filter.isdigit():
        where.append("s.workspace_id = %s")
        args.append(int(ws_filter))
    if agent_filter:
        where.append("s.agent_type = %s")
        args.append(agent_filter)
    if q:
        where.append("(s.title ILIKE %s OR s.id ILIKE %s)")
        like = f"%{q}%"
        args += [like, like]
    where_sql = " AND ".join(where)
    sessions = []
    with get_conn() as conn:
        c = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        c.execute(f"SELECT COUNT(*) AS total FROM sessions s JOIN workspaces w ON s.workspace_id = w.id WHERE {where_sql}", args)
        total = c.fetchone()["total"]
        c.execute(f"""
            SELECT s.id, s.workspace_id, s.title, s.agent_type, s.model, s.message_count,
                   (SELECT MAX(m.timestamp) FROM messages m
                    WHERE m.session_id = s.id AND m.workspace_id = s.workspace_id
                      AND COALESCE(m.hidden,0) = 0) AS synced_at,
                   w.name AS workspace_name
            FROM sessions s JOIN workspaces w ON s.workspace_id = w.id
            WHERE {where_sql}
            ORDER BY synced_at DESC
            LIMIT %s OFFSET %s
        """, args + [size, (page - 1) * size])
        for r in c.fetchall():
            r["sync_label"] = rel_sync_label(r.get("synced_at"))
            sessions.append(r)
    pages = max(1, (total + size - 1) // size)
    ctx = {"user": user, "workspaces": nav_ws, "active_page": "all_sessions",
           "sessions": sessions, "total": total, "pages": pages, "page": page, "size": size,
           "q": q, "ws_filter": ws_filter, "agent_filter": agent_filter,
           "ws_options": ws_options, "agent_options": AGENT_OPTIONS}
    return await render_page("all_sessions.html", ctx)

@router.post("/web/workspace/create", response_class=HTMLResponse)
async def web_create_workspace(request: Request):
    try:
        user = get_current_user(request)
    except:
        return RedirectResponse(url="/web/login")
    from fastapi import Form
    body = await request.form()
    name = body.get("name", "").strip()
    description = body.get("description", "").strip()
    if not name:
        return RedirectResponse(url="/web/", status_code=303)
    api_key = generate_api_key()
    now = datetime.now().timestamp()
    with get_conn() as conn:
        c = conn.cursor()
        try:
            c.execute("INSERT INTO workspaces (name, user_id, api_key, description, created_at) VALUES (%s, %s, %s, %s, %s)",
                      (name, user["sub"], api_key, description, now))
        except Exception:
            return RedirectResponse(url="/web/?error=ws_exists", status_code=303)
    return RedirectResponse(url="/web/?success=ws_created_msg", status_code=303)

@router.post("/web/workspace/{ws_id}/update", response_class=HTMLResponse)
async def web_update_workspace(ws_id: int, request: Request):
    """Rename / re-describe a workspace. Owners and admins only; the name
    keeps the UNIQUE(user_id, name) constraint (conflict -> ws_exists)."""
    try:
        user = get_current_user(request)
    except:
        return RedirectResponse(url="/web/login")
    body = await request.form()
    name = body.get("name", "").strip()
    description = body.get("description", "").strip()
    with get_conn() as conn:
        c = conn.cursor()
        if user.get("is_admin"):
            c.execute("SELECT id FROM workspaces WHERE id = %s", (ws_id,))
        else:
            c.execute("SELECT id FROM workspaces WHERE id = %s AND user_id = %s",
                      (ws_id, user["sub"]))
        if not c.fetchone():
            return RedirectResponse(url="/web/", status_code=303)
        try:
            if name:
                c.execute("UPDATE workspaces SET name = %s, description = %s WHERE id = %s",
                          (name, description, ws_id))
            else:
                c.execute("UPDATE workspaces SET description = %s WHERE id = %s",
                          (description, ws_id))
        except psycopg2.errors.UniqueViolation:
            return RedirectResponse(url="/web/?error=ws_exists", status_code=303)
    return RedirectResponse(url="/web/?success=ws_updated", status_code=303)

@router.get("/web/workspace/{ws_id}", response_class=HTMLResponse)
async def web_workspace_detail(ws_id: int, request: Request):
    try:
        user = get_current_user(request)
    except:
        return RedirectResponse(url="/web/login")
    nav_ws = get_nav_workspaces(user["sub"])
    with get_conn() as conn:
        c = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        # View access: workspace owner only. Admins have no read access to
        # other users' session lists or message contents.
        c.execute("SELECT * FROM workspaces WHERE id = %s AND user_id = %s", (ws_id, user["sub"]))
        ws = c.fetchone()
    if not ws:
        return RedirectResponse(url="/web/", status_code=303)
    # Sortable session list: whitelist column + direction, default last-updated desc.
    SORT_COLS = {"msg_count": "msg_count", "started_at": "started_at", "last_msg_at": "last_msg_at"}
    sort = request.query_params.get("sort", "last_msg_at")
    if sort not in SORT_COLS:
        sort = "last_msg_at"
    sort_col = SORT_COLS[sort]
    dir = request.query_params.get("dir", "desc")
    if dir not in ("asc", "desc"):
        dir = "desc"
    # Pagination: default 20 per page, clamped to 100 max.
    try:
        size = int(request.query_params.get("size", "20"))
    except ValueError:
        size = 20
    size = min(max(size, 1), 100)
    try:
        page = max(1, int(request.query_params.get("page", "1")))
    except ValueError:
        page = 1
    # Agent capsule filter: whitelist of known agent types.
    agent = request.query_params.get("agent", "all")
    if agent not in ("all", "hermes", "codex", "opencode", "reasonix", "openclaw"):
        agent = "all"
    if agent == "all":
        agent_clause = ""
    else:
        agent_clause = " AND agent_type = %s"
    # Profile filter (hermes sessions): the profile_name column is
    # authoritative; legacy prefixed ids (<name>:<bare>, default:<bare>)
    # are matched too so an unmigrated DB still filters correctly.
    #   bare id / '' / 'default:'  -> default profile
    #   '<name>:' / profile_name   -> named profile
    # non-hermes agents are never filtered by profile.
    profile = request.query_params.get("profile", "all")
    if agent != "hermes":
        profile = "all"  # profile only applies to hermes sessions
    if profile == "all":
        profile_clause = ""
    elif profile == "default":
        profile_clause = ("AND agent_type = 'hermes' "
                          "AND (COALESCE(profile_name,'') = '' "
                          "OR id LIKE 'default:%%')")
    else:
        # named profile: validate the name to avoid SQL injection
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", profile):
            profile = "all"
            profile_clause = ""
        else:
            profile_clause = ("AND agent_type = 'hermes' "
                              f"AND (profile_name = '{profile}' "
                              f"OR id LIKE '{profile}:%%')")
    profile_sel = (""", CASE
                         WHEN agent_type <> 'hermes' THEN NULL
                         WHEN COALESCE(profile_name,'') <> '' THEN profile_name
                         WHEN id LIKE 'default:%%' THEN 'default'
                         WHEN id LIKE '%%:%%' THEN split_part(id, ':', 1)
                         ELSE 'default' END AS profile""")
    with get_conn() as conn:
        c = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        # Deleted sessions are always hidden here (they live in the trash);
        # the trash pages are the only place to view/restore them.
        hide_clause = " AND COALESCE(hidden,0) = 0"
        q = (request.query_params.get("q") or "").strip()
        if q:
            # escape LIKE wildcards so user input is matched literally
            esc = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            q_clause = " AND (title ILIKE '%%' || %s || '%%' ESCAPE '\\' OR id ILIKE '%%' || %s || '%%' ESCAPE '\\')"
        else:
            esc = ""
            q_clause = ""
        params: list = [ws_id]
        if agent != "all":
            params.append(agent)
        if q:
            c.execute(f"SELECT COUNT(*) AS cnt FROM sessions WHERE workspace_id = %s {agent_clause}{profile_clause}{hide_clause}{q_clause}",
                      params + [esc, esc])
        else:
            c.execute(f"SELECT COUNT(*) AS cnt FROM sessions WHERE workspace_id = %s {agent_clause}{profile_clause}{hide_clause}",
                      params)
        total = c.fetchone()["cnt"]
        pages = max(1, (total + size - 1) // size)
        if page > pages:
            page = pages
        if q:
            c.execute(f"""SELECT s.*{profile_sel},
                         (SELECT MAX(m.timestamp) FROM messages m
                          WHERE m.session_id = s.id AND m.workspace_id = s.workspace_id AND COALESCE(m.hidden,0) = 0) AS last_msg_at,
                         (SELECT COUNT(*) FROM messages m
                          WHERE m.session_id = s.id AND m.workspace_id = s.workspace_id AND COALESCE(m.hidden,0) = 0) AS msg_count
                         FROM sessions s WHERE s.workspace_id = %s {agent_clause}{profile_clause}{hide_clause}{q_clause}
                         ORDER BY COALESCE(s.pinned,0) DESC, {sort_col} {dir} NULLS LAST, s.id
                         LIMIT {size} OFFSET %s""",
                      params + [esc, esc, (page - 1) * size])
        else:
            c.execute(f"""SELECT s.*{profile_sel},
                         (SELECT MAX(m.timestamp) FROM messages m
                          WHERE m.session_id = s.id AND m.workspace_id = s.workspace_id AND COALESCE(m.hidden,0) = 0) AS last_msg_at,
                         (SELECT COUNT(*) FROM messages m
                          WHERE m.session_id = s.id AND m.workspace_id = s.workspace_id AND COALESCE(m.hidden,0) = 0) AS msg_count
                         FROM sessions s WHERE s.workspace_id = %s {agent_clause}{profile_clause}{hide_clause}
                         ORDER BY COALESCE(s.pinned,0) DESC, {sort_col} {dir} NULLS LAST, s.id
                         LIMIT {size} OFFSET %s""", params + [(page - 1) * size])
        sessions = [dict(r) for r in c.fetchall()]
        # available profiles for the filter dropdown: hermes profile column
        # (plus legacy id prefixes on unmigrated DBs)
        c.execute("""SELECT DISTINCT profile_name AS pfx FROM sessions
                     WHERE workspace_id = %s AND agent_type = 'hermes'
                       AND COALESCE(profile_name,'') <> ''
                     UNION
                     SELECT DISTINCT split_part(id, ':', 1) AS pfx
                     FROM sessions WHERE workspace_id = %s AND agent_type = 'hermes'
                       AND id LIKE '%%:%%' AND id NOT LIKE 'default:%%'
                       AND COALESCE(profile_name,'') = ''""",
                  (ws_id, ws_id))
        profile_options = [r["pfx"] for r in c.fetchall()]
        c.execute("SELECT * FROM sync_state WHERE workspace_id = %s ORDER BY last_sync_at DESC", (ws_id,))
        devices = [dict(r) for r in c.fetchall()]
        # projects for this workspace (visible, with folders + matched sessions)
        c.execute("""SELECT * FROM projects WHERE workspace_id = %s
                     AND COALESCE(hidden,0) = 0
                     ORDER BY created_at DESC""", (ws_id,))
        projects = []
        for row in c.fetchall():
            p = dict(row)
            c.execute("""SELECT path, label, is_primary FROM project_folders
                         WHERE workspace_id = %s AND project_id = %s""", (ws_id, p["id"]))
            p["folders"] = [dict(r) for r in c.fetchall()]
            # match sessions whose cwd lives under one of the project folders
            # (prefix match, mirroring hermes project_for_path)
            seen: dict[str, str] = {}
            for f in p["folders"]:
                base = f["path"].rstrip("\\/")
                c.execute("""SELECT id, title FROM sessions
                             WHERE workspace_id = %s AND COALESCE(hidden,0) = 0
                               AND cwd IS NOT NULL AND cwd <> ''
                               AND (cwd = %s OR cwd LIKE %s OR cwd LIKE %s)
                             ORDER BY started_at DESC LIMIT 100""",
                          (ws_id, f["path"], base + "\\%", base + "/%"))
                for r in c.fetchall():
                    seen.setdefault(r["id"], r["title"])
            p["sessions"] = [{"id": k, "title": v} for k, v in seen.items()]
            p["session_count"] = len(p["sessions"])
            projects.append(p)
        # Deleted (soft-hidden) session count for the trash entry badge.
        c.execute("SELECT COUNT(*) AS cnt FROM sessions WHERE workspace_id = %s AND COALESCE(hidden,0) = 1", (ws_id,))
        trash_count = c.fetchone()["cnt"]
        # Sessions created within the last 24 hours (drives the "new" badge).
        # started_at is stored as unix epoch seconds (double precision).
        c.execute("SELECT COUNT(*) AS cnt FROM sessions WHERE workspace_id = %s "
                  "AND started_at >= EXTRACT(EPOCH FROM NOW() - INTERVAL '24 hours') "
                  "AND COALESCE(hidden,0) = 0", (ws_id,))
        new_24h = c.fetchone()["cnt"]
    ctx = {"user": user, "workspaces": nav_ws, "active_page": f"workspace_{ws_id}",
           "ws": dict(ws), "sessions": sessions, "devices": devices,
           "sort": sort, "dir": dir, "page": page, "pages": pages, "size": size, "total": total,
           "profile": profile, "profile_options": profile_options, "q": q,
           "agent": agent, "new_24h": new_24h,
           "trash_count": trash_count,
           "projects": projects}
    return await render_page("workspace_detail.html", ctx)

VALID_MSG_ROLES = {"user", "assistant", "tool", "system"}
_MD_EXT = ["fenced_code", "tables", "sane_lists"]

def md_to_html(text):
    """Render message content as Markdown. Raw HTML is escaped BEFORE
    processing, so user/LLM content can never inject markup (XSS-safe);
    any raw HTML inside a message is shown literally instead."""
    if not text:
        return ""
    try:
        body = markdown.markdown(html.escape(str(text), quote=True), extensions=_MD_EXT)
    except Exception:
        return ""
    # Open external links in a new tab.
    body = re.sub(r'<a href="(https?://[^"]+)"',
                  r'<a href="\1" target="_blank" rel="noopener noreferrer"', body)
    return body

@router.get("/web/workspace/{ws_id}/session/{sid}", response_class=HTMLResponse)
async def web_session_messages(ws_id: int, sid: str, request: Request):
    try:
        user = get_current_user(request)
    except:
        return RedirectResponse(url="/web/login")
    nav_ws = get_nav_workspaces(user["sub"])
    role = request.query_params.get("role", "")
    if role not in VALID_MSG_ROLES:
        role = ""
    page_param = request.query_params.get("page")
    # Page size: same control as the workspace session list (default 20, max 100).
    try:
        size = int(request.query_params.get("size", "20"))
    except ValueError:
        size = 20
    size = min(max(size, 1), 100)
    with get_conn() as conn:
        c = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        c.execute("SELECT * FROM workspaces WHERE id = %s AND user_id = %s", (ws_id, user["sub"]))
        ws = c.fetchone()
        if not ws:
            return RedirectResponse(url="/web/", status_code=303)
        c.execute("SELECT * FROM sessions WHERE id = %s AND workspace_id = %s", (sid, ws_id))
        sess = c.fetchone()
        if not sess:
            return RedirectResponse(url=f"/web/workspace/{ws_id}", status_code=303)
        where = "session_id = %s AND workspace_id = %s"
        params = [sid, ws_id]
        if role:
            where += " AND role = %s"
            params.append(role)
        # Deleted messages are always hidden here (they live in the trash).
        where += " AND COALESCE(hidden,0) = 0"
        q = (request.query_params.get("q") or "").strip()
        if q:
            esc = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            where += " AND content ILIKE '%%' || %s || '%%' ESCAPE '\\'"
            params.append(esc)
        c.execute(f"SELECT COUNT(*) AS cnt FROM messages WHERE {where}", params)
        total = c.fetchone()["cnt"]
        pages = max(1, (total + size - 1) // size)
        # Default to the LATEST page (newest messages), per the confirmed design
        # ("默认从最新看起"); an explicit ?page= still navigates anywhere.
        if page_param is None:
            page = pages
        else:
            try:
                page = max(1, int(page_param))
            except ValueError:
                page = pages
        if page > pages:
            page = pages
        c.execute(
            f"SELECT * FROM messages WHERE {where} ORDER BY timestamp ASC, id ASC LIMIT {size} OFFSET %s",
            params + [(page - 1) * size],
        )
        messages = [dict(r) for r in c.fetchall()]
    for m in messages:
        if m.get("role") in ("user", "assistant"):
            m["content_md"] = md_to_html(m.get("content"))
        else:
            m["content_md"] = ""
    with get_conn() as conn:
        c = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        c.execute("SELECT COUNT(*) AS cnt FROM messages WHERE session_id = %s AND workspace_id = %s AND COALESCE(hidden,0) = 1",
                  (sid, ws_id))
        trash_count = c.fetchone()["cnt"]
    ctx = {
        "user": user, "workspaces": nav_ws, "active_page": f"workspace_{ws_id}",
        "ws": dict(ws), "session": dict(sess), "messages": messages,
        "total": total, "page": page, "pages": pages, "role": role, "size": size,
        "q": q, "trash_count": trash_count,
        "sync_label": rel_sync_label(sess.get("last_synced_at")),
    }
    return await render_page("session_messages.html", ctx)
def _msg_ts(value):
    try:
        return datetime.fromtimestamp(float(value)).strftime("%Y-%m-%d %H:%M") if value else "-"
    except Exception:
        return str(value) if value else "-"

@router.get("/web/workspace/{ws_id}/session/{sid}/export")
async def web_session_export(ws_id: int, sid: str, request: Request):
    try:
        user = get_current_user(request)
    except:
        return RedirectResponse(url="/web/login")
    with get_conn() as conn:
        c = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        c.execute("SELECT * FROM workspaces WHERE id = %s AND user_id = %s", (ws_id, user["sub"]))
        ws = c.fetchone()
        if not ws:
            return RedirectResponse(url="/web/", status_code=303)
        c.execute("SELECT * FROM sessions WHERE id = %s AND workspace_id = %s", (sid, ws_id))
        sess = c.fetchone()
        if not sess:
            return RedirectResponse(url=f"/web/workspace/{ws_id}", status_code=303)
        c.execute("SELECT * FROM messages WHERE session_id = %s AND workspace_id = %s "
                  "ORDER BY timestamp ASC, id ASC", (sid, ws_id))
        messages = [dict(r) for r in c.fetchall()]
    t = get_translations(get_lang())
    role_names = {"user": t["msg_filter_user"], "assistant": t["msg_filter_assistant"],
                  "tool": t["msg_filter_tool"], "system": t["msg_filter_system"]}
    title = sess["title"] or sid
    lines = [f"# {title}", "",
             f"- {t['admin_workspace']}: {ws['name']}",
             f"- {t['ws_model']}: {sess['model'] or '-'}",
             f"- {t['msg_started']}: {_msg_ts(sess['started_at'])}",
             f"- {t['ws_messages']}: {len(messages)}", ""]
    for m in messages:
        ts = _msg_ts(m["timestamp"])
        role = role_names.get(m["role"], m["role"])
        if m["role"] == "tool":
            lines.append(f"## {role} · {m['tool_name'] or m['tool_call_id'] or 'tool'} ({ts})")
        else:
            lines.append(f"## {role} ({ts})")
        lines.append("")
        lines.append((m["content"] or "").strip() or "-")
        lines.append("")
    body = "\n".join(lines)
    fname = f"hermes-sync-session-{sid}.md"
    return Response(
        content=body,
        media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )

@router.get("/web/workspace/{ws_id}/export")
async def web_workspace_export(ws_id: int, request: Request):
    """Export every session and message of a workspace as JSON."""
    try:
        user = get_current_user(request)
    except:
        return RedirectResponse(url="/web/login")
    with get_conn() as conn:
        c = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        c.execute("SELECT * FROM workspaces WHERE id = %s AND user_id = %s", (ws_id, user["sub"]))
        ws = c.fetchone()
        if not ws:
            return RedirectResponse(url="/web/", status_code=303)
        c.execute("SELECT * FROM sessions WHERE workspace_id = %s ORDER BY started_at, id", (ws_id,))
        sessions = [dict(r) for r in c.fetchall()]
        for s in sessions:
            c.execute("SELECT * FROM messages WHERE session_id = %s AND workspace_id = %s "
                      "ORDER BY timestamp, id", (s["id"], ws_id))
            s["messages"] = [dict(r) for r in c.fetchall()]
    payload = {
        "format": "hermes-sync-sessions", "version": 1,
        "exported_at": datetime.now().timestamp(),
        "workspace_id": ws_id, "workspace_name": ws["name"],
        "sessions": sessions,
    }
    # Gzip in memory (no temp file on disk); browser saves the .json.gz.
    raw = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
    compressed = gzip.compress(raw, compresslevel=6)
    fname = f"hermes-sync-export-{ws_id}-{datetime.now().strftime('%Y%m%d')}.json.gz"
    return Response(
        content=compressed,
        media_type="application/gzip",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )

@router.post("/web/workspace/{ws_id}/session/{sid}/hide")
async def web_session_hide(ws_id: int, sid: str, request: Request):
    """Soft-hide a session: /pull stops delivering it, Web hides it by
    default. Fully reversible (see /unhide). Data is never deleted."""
    try:
        user = get_current_user(request)
    except:
        return RedirectResponse(url="/web/login")
    with get_conn() as conn:
        c = conn.cursor()
        c.execute("SELECT id FROM workspaces WHERE id = %s AND user_id = %s", (ws_id, user["sub"]))
        if not c.fetchone():
            return RedirectResponse(url="/web/", status_code=303)
        c.execute("UPDATE sessions SET hidden = 1, hidden_at = %s "
                  "WHERE id = %s AND workspace_id = %s",
                  (datetime.now().timestamp(), sid, ws_id))
        conn.commit()
    return RedirectResponse(url=f"/web/workspace/{ws_id}", status_code=303)

@router.post("/web/workspace/{ws_id}/session/{sid}/unhide")
async def web_session_unhide(ws_id: int, sid: str, request: Request):
    try:
        user = get_current_user(request)
    except:
        return RedirectResponse(url="/web/login")
    with get_conn() as conn:
        c = conn.cursor()
        c.execute("SELECT id FROM workspaces WHERE id = %s AND user_id = %s", (ws_id, user["sub"]))
        if not c.fetchone():
            return RedirectResponse(url="/web/", status_code=303)
        c.execute("UPDATE sessions SET hidden = 0, hidden_at = NULL "
                  "WHERE id = %s AND workspace_id = %s", (sid, ws_id))
        conn.commit()
    return RedirectResponse(url=f"/web/workspace/{ws_id}", status_code=303)

@router.get("/web/workspace/{ws_id}/trash", response_class=HTMLResponse)
async def web_workspace_trash(ws_id: int, request: Request):
    """Session trash: deleted (soft-hidden) sessions, fully recoverable."""
    try:
        user = get_current_user(request)
    except:
        return RedirectResponse(url="/web/login")
    nav_ws = get_nav_workspaces(user["sub"])
    with get_conn() as conn:
        c = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        # View access: workspace owner only (same policy as the session list).
        c.execute("SELECT * FROM workspaces WHERE id = %s AND user_id = %s", (ws_id, user["sub"]))
        ws = c.fetchone()
        if not ws:
            return RedirectResponse(url="/web/", status_code=303)
        c.execute("""SELECT s.*, (SELECT COUNT(*) FROM messages m
                       WHERE m.workspace_id = s.workspace_id AND m.session_id = s.id) AS message_count
                     FROM sessions s WHERE s.workspace_id = %s AND COALESCE(s.hidden,0) = 1
                     ORDER BY COALESCE(s.hidden_at, s.last_synced_at, s.started_at) DESC""", (ws_id,))
        trash_sessions = [dict(r) for r in c.fetchall()]
    return await render_page("trash_sessions.html", {"user": user, "workspaces": nav_ws,
                                          "active_page": f"workspace_{ws_id}",
                                          "ws": dict(ws), "trash_sessions": trash_sessions})

@router.get("/web/workspace/{ws_id}/session/{sid}/trash", response_class=HTMLResponse)
async def web_session_trash(ws_id: int, sid: str, request: Request):
    """Message trash: deleted (soft-hidden) messages of one session."""
    try:
        user = get_current_user(request)
    except:
        return RedirectResponse(url="/web/login")
    nav_ws = get_nav_workspaces(user["sub"])
    with get_conn() as conn:
        c = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        c.execute("SELECT * FROM workspaces WHERE id = %s AND user_id = %s", (ws_id, user["sub"]))
        ws = c.fetchone()
        if not ws:
            return RedirectResponse(url="/web/", status_code=303)
        c.execute("SELECT * FROM sessions WHERE id = %s AND workspace_id = %s", (sid, ws_id))
        sess = c.fetchone()
        if not sess:
            return RedirectResponse(url=f"/web/workspace/{ws_id}", status_code=303)
        c.execute("""SELECT * FROM messages
                     WHERE session_id = %s AND workspace_id = %s AND COALESCE(hidden,0) = 1
                     ORDER BY timestamp ASC, id ASC""", (sid, ws_id))
        trash_messages = [dict(r) for r in c.fetchall()]
    for m in trash_messages:
        if m.get("role") in ("user", "assistant"):
            m["content_md"] = md_to_html(m.get("content"))
        else:
            m["content_md"] = ""
    return await render_page("trash_messages.html", {"user": user, "workspaces": nav_ws,
                                          "active_page": f"workspace_{ws_id}",
                                          "ws": dict(ws), "session": dict(sess),
                                          "trash_messages": trash_messages})

@router.post("/web/workspace/{ws_id}/session/{sid}/message/{mid}/hide")
async def web_message_hide(ws_id: int, sid: str, mid: int, request: Request):
    try:
        user = get_current_user(request)
    except:
        return RedirectResponse(url="/web/login")
    with get_conn() as conn:
        c = conn.cursor()
        c.execute("SELECT id FROM workspaces WHERE id = %s AND user_id = %s", (ws_id, user["sub"]))
        if not c.fetchone():
            return RedirectResponse(url="/web/", status_code=303)
        c.execute("UPDATE messages SET hidden = 1, hidden_at = %s "
                  "WHERE id = %s AND session_id = %s AND workspace_id = %s",
                  (datetime.now().timestamp(), mid, sid, ws_id))
        conn.commit()
    resp = RedirectResponse(url=f"/web/workspace/{ws_id}/session/{sid}", status_code=303)
    t = get_translations(get_lang())
    make_flash(resp, t.get("msg_hidden_ok", "Message hidden"), "success")
    return resp

@router.post("/web/workspace/{ws_id}/session/{sid}/message/{mid}/unhide")
async def web_message_unhide(ws_id: int, sid: str, mid: int, request: Request):
    try:
        user = get_current_user(request)
    except:
        return RedirectResponse(url="/web/login")
    with get_conn() as conn:
        c = conn.cursor()
        c.execute("SELECT id FROM workspaces WHERE id = %s AND user_id = %s", (ws_id, user["sub"]))
        if not c.fetchone():
            return RedirectResponse(url="/web/", status_code=303)
        c.execute("UPDATE messages SET hidden = 0, hidden_at = NULL "
                  "WHERE id = %s AND session_id = %s AND workspace_id = %s",
                  (mid, sid, ws_id))
        conn.commit()
    resp = RedirectResponse(url=f"/web/workspace/{ws_id}/session/{sid}", status_code=303)
    t = get_translations(get_lang())
    make_flash(resp, t.get("msg_unhidden_ok", "Message restored"), "success")
    return resp

@router.post("/web/workspace/{ws_id}/import")
async def web_workspace_import(ws_id: int, request: Request):
    """Import a workspace export JSON. Merge semantics match /push: sessions
    upsert by id, messages dedupe on the (session_id, role, timestamp) triple."""
    try:
        user = get_current_user(request)
    except:
        return RedirectResponse(url="/web/login")
    # Import is a write to the workspace: owner only.
    with get_conn() as conn:
        c = conn.cursor()
        c.execute("SELECT id FROM workspaces WHERE id = %s AND user_id = %s", (ws_id, user["sub"]))
        if not c.fetchone():
            return RedirectResponse(url="/web/", status_code=303)
    t = get_translations(get_lang())
    form = await request.form()
    file = form.get("file")
    if file is None:
        resp = RedirectResponse(url=f"/web/workspace/{ws_id}", status_code=303)
        make_flash(resp, t["ws_import_invalid"], "error")
        return resp
    raw = await file.read()
    await file.close()  # release the upload spool (temp file when large)
    if raw[:2] == b"\x1f\x8b":  # gzip magic — accept .gz or plain JSON
        try:
            raw = gzip.decompress(raw)
        except Exception:
            resp = RedirectResponse(url=f"/web/workspace/{ws_id}", status_code=303)
            make_flash(resp, t["ws_import_invalid"], "error")
            return resp
    try:
        data = json.loads(raw.decode("utf-8"))
    except Exception:
        resp = RedirectResponse(url=f"/web/workspace/{ws_id}", status_code=303)
        make_flash(resp, t["ws_import_invalid"], "error")
        return resp
    if data.get("format") != "hermes-sync-sessions" or data.get("version") != 1:
        resp = RedirectResponse(url=f"/web/workspace/{ws_id}", status_code=303)
        make_flash(resp, t["ws_import_version"], "error")
        return resp
    sessions = data.get("sessions") or []
    imp_s = upd_s = imp_m = dup_m = 0
    now = datetime.now().timestamp()
    with get_conn() as conn:
        c = conn.cursor()
        c.execute("SELECT column_name FROM information_schema.columns WHERE table_name = 'sessions'")
        sess_cols = {r[0] for r in c.fetchall()}
        c.execute("SELECT column_name FROM information_schema.columns WHERE table_name = 'messages'")
        msg_cols = {r[0] for r in c.fetchall()}
        for session in sessions:
            if not isinstance(session, dict) or not session.get("id"):
                continue
            sid = session["id"]
            messages = session.get("messages") or []
            sd = {k: _pg_val(v) for k, v in session.items()
                  if k != "messages" and k in sess_cols and v is not None}
            sd.pop("workspace_id", None)
            sd.pop("user_id", None)
            c.execute("SELECT id FROM sessions WHERE id = %s AND workspace_id = %s", (sid, ws_id))
            if c.fetchone():
                sd.pop("id", None)
                if sd:
                    sd["last_synced_at"] = now
                    set_cl = ", ".join([f"{k} = %s" for k in sd.keys()])
                    c.execute(f"UPDATE sessions SET {set_cl} WHERE id = %s AND workspace_id = %s",
                              list(sd.values()) + [sid, ws_id])
                upd_s += 1
            else:
                sd["workspace_id"] = ws_id
                sd["last_synced_at"] = now
                cols = ", ".join(sd.keys())
                ph = ", ".join(["%s"] * len(sd))
                c.execute(f"INSERT INTO sessions ({cols}) VALUES ({ph})", list(sd.values()))
                imp_s += 1
            for msg in messages:
                if not isinstance(msg, dict):
                    continue
                role = msg.get("role")
                ts = msg.get("timestamp")
                md = {k: _pg_val(v) for k, v in msg.items() if k in msg_cols and v is not None}
                md["session_id"] = sid
                md["workspace_id"] = ws_id
                if role is not None and ts is not None:
                    c.execute("SELECT 1 FROM messages WHERE session_id=%s AND role=%s AND timestamp=%s AND workspace_id=%s",
                              (sid, role, ts, ws_id))
                    if c.fetchone():
                        dup_m += 1
                        continue
                cols = ", ".join(md.keys())
                ph = ", ".join(["%s"] * len(md))
                c.execute(f"INSERT INTO messages ({cols}) VALUES ({ph})", list(md.values()))
                imp_m += 1
    msg_text = t["ws_import_ok"] % (imp_s, upd_s, imp_m, dup_m)
    resp = RedirectResponse(url=f"/web/workspace/{ws_id}", status_code=303)
    make_flash(resp, msg_text, "success")
    return resp

@router.get("/web/workspace/{ws_id}/delete")
async def web_delete_workspace(ws_id: int, request: Request):
    try:
        user = get_current_user(request)
    except:
        return RedirectResponse(url="/web/login")
    with get_conn() as conn:
        c = conn.cursor()
        c.execute("DELETE FROM workspaces WHERE id = %s AND user_id = %s", (ws_id, user["sub"]))
    return RedirectResponse(url="/web/?success=ws_deleted", status_code=303)

@router.post("/web/workspace/{ws_id}/regen-key", response_class=HTMLResponse)
async def web_regen_key(ws_id: int, request: Request):
    try:
        user = get_current_user(request)
    except:
        return RedirectResponse(url="/web/login")
    new_key = generate_api_key()
    with get_conn() as conn:
        c = conn.cursor()
        c.execute("UPDATE workspaces SET api_key = %s WHERE id = %s AND user_id = %s", (new_key, ws_id, user["sub"]))
    return RedirectResponse(url=f"/web/workspace/{ws_id}", status_code=303)

@router.get("/api/me")
async def api_me(user: dict = Depends(get_current_user)):
    return {"user_id": user["sub"], "username": user["username"], "is_admin": user.get("is_admin")}

@router.post("/api/me/change-password")
async def api_change_password(request: Request, user: dict = Depends(get_current_user)):
    body = await request.json()
    old_pw = body.get("old_password", "")
    new_pw = body.get("new_password", "")
    if len(new_pw) < 6:
        raise HTTPException(status_code=400, detail="New password must be at least 6 characters")
    with get_conn() as conn:
        c = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        c.execute("SELECT password_hash FROM users WHERE id = %s", (user["sub"],))
        u = c.fetchone()
        if not u or not verify_password(old_pw, u["password_hash"]):
            raise HTTPException(status_code=401, detail="Invalid old password")
        c.execute("UPDATE users SET password_hash = %s, must_change_password = 0 WHERE id = %s", (hash_password(new_pw), user["sub"]))
    return {"success": True}

@router.get("/api/workspaces")
async def api_list_workspaces(user: dict = Depends(get_current_user)):
    with get_conn() as conn:
        c = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        c.execute("SELECT id, name, api_key, description, created_at FROM workspaces WHERE user_id = %s ORDER BY created_at DESC", (user["sub"],))
        return [dict(r) for r in c.fetchall()]

@router.post("/api/workspaces")
async def api_create_workspace(request: Request, user: dict = Depends(get_current_user)):
    body = await request.json()
    name = body.get("name", "").strip()
    description = body.get("description", "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="Name required")
    api_key = generate_api_key()
    now = datetime.now().timestamp()
    with get_conn() as conn:
        c = conn.cursor()
        try:
            c.execute("INSERT INTO workspaces (name, user_id, api_key, description, created_at) VALUES (%s, %s, %s, %s, %s)",
                      (name, user["sub"], api_key, description, now))
        except psycopg2.errors.UniqueViolation:
            raise HTTPException(status_code=409, detail="Workspace name already exists")
    return {"id": c.fetchone()[0] if c.fetchone() else None, "name": name, "api_key": api_key}

@router.delete("/api/workspaces/{ws_id}")
async def api_delete_workspace(ws_id: int, user: dict = Depends(get_current_user)):
    with get_conn() as conn:
        c = conn.cursor()
        c.execute("DELETE FROM workspaces WHERE id = %s AND user_id = %s", (ws_id, user["sub"]))
    return {"success": True}

@router.post("/api/workspaces/{ws_id}/regen-key")
async def api_regen_key(ws_id: int, user: dict = Depends(get_current_user)):
    new_key = generate_api_key()
    with get_conn() as conn:
        c = conn.cursor()
        c.execute("UPDATE workspaces SET api_key = %s WHERE id = %s AND user_id = %s", (new_key, ws_id, user["sub"]))
    return {"api_key": new_key}

