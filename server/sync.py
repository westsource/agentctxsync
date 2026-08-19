"""Sync domain: pull/push/status (API-Key protocol endpoints)."""
import asyncio
from datetime import datetime

import psycopg2.extras
from fastapi import APIRouter, Depends, HTTPException, Request

from auth import get_workspace_by_api_key
from db import _pg_val, get_conn, plan_limits, quota_check

router = APIRouter()
def log_audit(conn, event, user_id, workspace_id, device_id, code, detail):
    """Append one row to audit_log inside the caller's transaction."""
    c = conn.cursor()
    c.execute(
        "INSERT INTO audit_log (ts, event, user_id, workspace_id, device_id, code, detail) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s)",
        (datetime.now().timestamp(), event, user_id, workspace_id, device_id, code, detail))


@router.get("/health")
async def health():
    try:
        with get_conn() as conn:
            c = conn.cursor()
            c.execute("SELECT 1")
        return {"status": "ok", "service": "hermes-session-sync", "backend": "postgresql", "auth": "multi-tenant", "name": "Agent Context Sync"}
    except Exception as e:
        return {"status": "error", "detail": str(e)}

@router.post("/pull")
async def pull(request: Request, ws: dict = Depends(get_workspace_by_api_key)):
    body = await request.json()
    loop = asyncio.get_running_loop()
    # Sessions + their messages off the event loop: a page of sessions with
    # thousands of messages would otherwise block every other request.
    return await loop.run_in_executor(None, pull_sync, body, ws)


def pull_sync(body, ws):
    """Synchronous pull core, run off the event loop (see the /pull route)."""
    device_id = body.get("device_id", "unknown")
    last_sync_at = body.get("last_sync_at", 0)
    limit = body.get("limit", 50)
    offset = body.get("offset", 0)
    agent = body.get("agent")  # optional filter: only sessions of one agent
    wid = ws["workspace_id"]
    agent_clause = " AND agent_type = %s" if agent else ""
    agent_params = (agent,) if agent else ()
    with get_conn() as conn:
        c = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        c.execute(f"SELECT COUNT(*) AS cnt FROM sessions WHERE workspace_id = %s{agent_clause} AND COALESCE(hidden,0) = 0",
                  (wid,) + agent_params)
        total_sessions = c.fetchone()["cnt"]
        if last_sync_at == 0:
            c.execute(f"SELECT * FROM sessions WHERE workspace_id = %s{agent_clause} AND COALESCE(hidden,0) = 0 ORDER BY started_at DESC LIMIT %s OFFSET %s",
                      (wid,) + agent_params + (limit, offset))
        else:
            c.execute(f"SELECT * FROM sessions WHERE workspace_id = %s{agent_clause} AND COALESCE(hidden,0) = 0 AND (last_synced_at > %s OR started_at > %s) ORDER BY started_at DESC LIMIT %s OFFSET %s",
                      (wid,) + agent_params + (last_sync_at, last_sync_at, limit, offset))
        sessions = [dict(r) for r in c.fetchall()]
        # One query for ALL page messages instead of an N+1 loop per session;
        # grouped in memory by session (ORDER BY session_id keeps each
        # session's own messages timestamp-ordered, matching the old per-
        # session ORDER BY timestamp).
        if sessions:
            sids = [s["id"] for s in sessions]
            if last_sync_at == 0:
                c.execute("SELECT * FROM messages WHERE workspace_id = %s AND session_id = ANY(%s) AND COALESCE(hidden,0) = 0 ORDER BY session_id, timestamp",
                          (wid, sids))
            else:
                c.execute("SELECT * FROM messages WHERE workspace_id = %s AND session_id = ANY(%s) AND COALESCE(hidden,0) = 0 AND timestamp > %s ORDER BY session_id, timestamp",
                          (wid, sids, last_sync_at))
            by_sid = {}
            for m in c.fetchall():
                by_sid.setdefault(m["session_id"], []).append(dict(m))
            for s in sessions:
                s["messages"] = by_sid.get(s["id"], [])
    now = datetime.now().timestamp()
    return {"sync_at": now, "session_count": len(sessions),
            "total_sessions": total_sessions,
            "message_count": sum(len(s["messages"]) for s in sessions), "sessions": sessions}

@router.post("/push")
async def push(request: Request, ws: dict = Depends(get_workspace_by_api_key)):
    body = await request.json()
    loop = asyncio.get_running_loop()
    # The heavy DB work (quota gate, upserts, batched dedup + inserts) runs
    # off the event loop so a big push never stalls other clients.
    return await loop.run_in_executor(None, push_sync, body, ws)


# Legacy canonical-id prefixes (id-scheme upgrade inbound compat): old
# clients push ids like "codex:<uuid>", "magic:<bare>", "workbuddy:<uuid>".
# Storage now uses bare ids with agent_type/profile_name columns; map the
# prefix into the fields. Bare ids return (None, None, id) and are left
# untouched (new clients already carry attribution in the payload).
_AGENT_ID_PREFIXES = {
    "codex:": "codex", "opencode:": "opencode", "reasonix:": "reasonix",
    "openclaw:": "openclaw", "workbuddy:": "workbuddy",
}


def _split_inbound_id(cid: str):
    """-> (agent_type|None, profile_name|None, bare_id) for a pushed id."""
    if ":" not in cid:
        return None, None, cid
    prefix, bare = cid.split(":", 1)
    agent = _AGENT_ID_PREFIXES.get(prefix + ":")
    if agent:
        return agent, None, bare
    if prefix == "default":
        return "hermes", "", bare
    return "hermes", prefix, bare  # hermes profile prefix (legacy scheme)


def push_sync(body, ws):
    """Synchronous push core, run off the event loop (see the /push route)."""
    device_id = body.get("device_id", "unknown")
    sessions_data = body["sessions"]
    wid = ws["workspace_id"]
    now = datetime.now().timestamp()
    # Normalize legacy prefixed ids into the column scheme BEFORE the quota
    # gate and dedup-key snapshots: a legacy client re-pushing an existing
    # (post-migration) session must match the same bare-id row, not appear
    # as a brand-new session.
    for session in sessions_data:
        agent, profile, bare = _split_inbound_id(str(session.get("id", "")))
        if agent is not None:
            session["id"] = bare
            session["agent_type"] = agent
            session["profile_name"] = profile if profile is not None \
                else session.get("profile_name") or ""
            for m in session.get("messages", []):
                msid = m.get("session_id")
                if isinstance(msid, str) and ":" in msid:
                    m["session_id"] = _split_inbound_id(msid)[2]
    imp_s, imp_m, upd_s, dup_m = 0, 0, 0, 0
    with get_conn() as conn:
        c = conn.cursor()
        # ---- Quota gate: enforce plan limits on NEW session writes. ----
        # Existing sessions keep syncing (updates allowed); only new inserts
        # are gated, so lowering a quota never breaks an already-synced pool.
        # Master API key (user_id None) is never gated. Policy is read from
        # the DB on every push, so an operator's change applies immediately.
        if ws.get("user_id"):
            c.execute("SELECT id FROM sessions WHERE workspace_id = %s", (wid,))
            existing_ids = {r[0] for r in c.fetchall()}
            new_agents = [s.get("agent_type") or "hermes" for s in sessions_data
                          if s["id"] not in existing_ids]
            if new_agents:
                c.execute("SELECT plan FROM users WHERE id = %s", (ws["user_id"],))
                prow = c.fetchone()
                plan = (prow[0] if prow else None) or "free"
                max_sessions, allowed_agents = plan_limits(plan, conn)
                c.execute("""SELECT COUNT(*) FROM sessions s
                             JOIN workspaces w ON s.workspace_id = w.id
                             WHERE w.user_id = %s AND s.archived = 0""",
                          (ws["user_id"],))
                existing_count = c.fetchone()[0]
                ok, code = quota_check(max_sessions, allowed_agents,
                                       existing_count, new_agents)
                if not ok:
                    log_audit(conn, "quota_rejected", ws["user_id"], wid, device_id, code,
                              f"plan={plan} active={existing_count} new={len(new_agents)} agents={new_agents}")
                    # Commit the audit row BEFORE raising: the get_conn()
                    # context manager rolls back on exception, which would
                    # otherwise silently drop the rejection record.
                    conn.commit()
                    raise HTTPException(status_code=403, detail=code)
        # Only write columns that actually exist in the server schema: Hermes
        # (and other agents) evolve their local state.db with new columns
        # (e.g. system_prompt_hash) faster than this server's tables, and a
        # dynamic INSERT of an unknown column would 500 the whole batch.
        c.execute("SELECT column_name FROM information_schema.columns "
                  "WHERE table_name = 'sessions'")
        sess_cols = {r[0] for r in c.fetchall()}
        c.execute("SELECT column_name FROM information_schema.columns "
                  "WHERE table_name = 'messages'")
        msg_cols = {r[0] for r in c.fetchall()}
        # ---- Batch metadata for this push ----
        # Existing (session_id, role, timestamp) keys across the pushed
        # sessions: ONE query instead of one per message (a 10k-message push
        # used to issue several SELECTs per message before dedup could start).
        sess_ids = [s["id"] for s in sessions_data]
        c.execute("SELECT session_id, role, timestamp FROM messages "
                  "WHERE workspace_id = %s AND session_id = ANY(%s)",
                  (wid, sess_ids))
        msg_keys = set(c.fetchall())
        # Next auto-increment id per session for clients without local ids
        # (codex/reasonix/...): ONE GROUP BY query instead of MAX(id) per
        # message; ids are allocated in memory and a concurrent push stealing
        # one is detected via INSERT ... RETURNING and retried.
        c.execute("SELECT session_id, COALESCE(MAX(id), 0) + 1 FROM messages "
                  "WHERE workspace_id = %s AND session_id = ANY(%s) GROUP BY session_id",
                  (wid, sess_ids))
        next_ids = {sid: n for sid, n in c.fetchall()}
        for session in sessions_data:
            sid = session["id"]
            session_agent = session.get("agent_type") or "hermes"
            c.execute("SELECT id FROM sessions WHERE id = %s AND workspace_id = %s", (sid, wid))
            if c.fetchone():
                sd = {k: _pg_val(v) for k, v in session.items()
                      if k != "messages" and v is not None and k in sess_cols}
                # agent_type records which agent CREATED the session; it is
                # set once on INSERT and must never be overwritten by a
                # re-push. A client that pulled sessions from other agents
                # (reasonix/jsonl stores hold hermes sessions locally) would
                # otherwise re-mark them with its own agent_type and destroy
                # the server-side attribution (and legacy hermes clients
                # re-push what they pulled). UPDATE never touches it.
                sd.pop("agent_type", None)
                # hidden is a server-side soft-hide flag: a client re-pushing
                # a session it still holds must not reset it to visible.
                sd.pop("hidden", None)
                sd["last_synced_at"] = now
                set_cl = ", ".join([f"{k} = %s" for k in sd.keys()])
                c.execute(f"UPDATE sessions SET {set_cl} WHERE id = %s AND workspace_id = %s",
                          list(sd.values()) + [sid, wid])
                upd_s += 1
            else:
                sd = {k: _pg_val(v) for k, v in session.items()
                      if k != "messages" and v is not None and k in sess_cols}
                sd.setdefault("agent_type", session_agent)
                sd["last_synced_at"] = now
                sd["workspace_id"] = wid
                cols = ", ".join(sd.keys())
                ph = ", ".join(["%s"] * len(sd))
                c.execute(f"INSERT INTO sessions ({cols}) VALUES ({ph})", list(sd.values()))
                imp_s += 1
            # ---- messages: dedup in memory, batch insert per session ----
            new_msgs = []
            content_cache = {}
            for msg in session.get("messages", []):
                msid = msg.get("session_id", sid)
                role = msg.get("role")
                ts = msg.get("timestamp")
                # Identity is the (session_id, role, timestamp) triple, matching
                # the client's pull dedup. Local message ids are per-DB
                # autoincrement and get re-assigned after a pull, so a re-pushed
                # message would otherwise duplicate a row under a fresh id.
                # Fall back to the id check when role/timestamp are missing.
                if role is not None and ts is not None:
                    if (msid, role, ts) in msg_keys:
                        dup_m += 1
                        continue
                else:
                    mid = msg.get("id")
                    c.execute("SELECT 1 FROM messages WHERE session_id=%s AND id=%s AND workspace_id=%s",
                              (msid, mid, wid))
                    if c.fetchone():
                        dup_m += 1
                        continue
                # Content-level fallback: an agent that rebuilt a session
                # (e.g. hermes "message-alternation repair" after an
                # interrupted turn) re-generates timestamps with time.time(),
                # so the (role, timestamp) key no longer matches the original
                # rows even though the content is identical. If an identical
                # (role, content) already exists for this session, treat the
                # push as a duplicate instead of duplicating the row. The
                # (role -> contents) map is fetched lazily, once per session,
                # only when a key miss actually needs the check.
                #
                # Scope: hermes/reasonix keep full-role content dedup (their
                # session rebuilds rewrite tool rows too). Every OTHER agent
                # gets it for user/assistant rows only: cross-client re-pushes
                # (a foreign session pulled by another agent and pushed back
                # with a floating-point-shifted timestamp) must not create
                # duplicate rows, while tool rows keep the triple-only dedup
                # (codex tool outputs legitimately repeat identical text from
                # distinct calls).
                content = msg.get("content")
                dedup_text = content if isinstance(content, str) and content else None
                if dedup_text is None:
                    # Damaged rows: some clients push messages whose content
                    # was lost in a foreign-store round-trip but whose text
                    # survives as a bare string in meta (JSONB string, not
                    # object). Treat that text as the dedup key too so a
                    # re-push of the same damaged message is still caught.
                    meta_v = msg.get("meta")
                    if isinstance(meta_v, str) and meta_v.strip():
                        dedup_text = meta_v
                if dedup_text is not None and role is not None and (
                        session_agent in ("hermes", "reasonix")
                        or role in ("user", "assistant")):
                    if msid not in content_cache:
                        c.execute("SELECT role, content, meta FROM messages "
                                  "WHERE workspace_id = %s AND session_id = %s",
                                  (wid, msid))
                        by_role = {}
                        for r_role, r_content, r_meta in c.fetchall():
                            pool = set()
                            if isinstance(r_content, str) and r_content:
                                pool.add(r_content)
                            if isinstance(r_meta, str) and r_meta.strip():
                                pool.add(r_meta)
                            if pool:
                                by_role.setdefault(r_role, set()).update(pool)
                        content_cache[msid] = by_role
                    if dedup_text in content_cache[msid].get(role, ()):
                        dup_m += 1
                        continue
                md = {k: _pg_val(v) for k, v in msg.items()
                      if v is not None and k in msg_cols}
                md.setdefault("agent_type", session_agent)
                md["session_id"] = sid
                md["workspace_id"] = wid
                if "id" not in md:
                    md["id"] = next_ids.get(msid, 1)
                    next_ids[msid] = md["id"] + 1
                new_msgs.append(md)
            if new_msgs:
                # One multi-row statement per session (a single round trip
                # instead of one INSERT per message). ON CONFLICT DO NOTHING
                # catches both the (workspace_id, session_id, id) PK and the
                # uq_messages_dedup triple index (db.py); RETURNING reports
                # the rows that actually landed.
                from psycopg2.extras import execute_values
                cols = sorted({k for md in new_msgs for k in md})
                rows = [[md.get(k) for k in cols] for md in new_msgs]
                insert_sql = (f"INSERT INTO messages ({', '.join(cols)}) VALUES %s "
                              "ON CONFLICT DO NOTHING RETURNING session_id, id")
                execute_values(c, insert_sql, rows, page_size=500)
                inserted = set(c.fetchall())
                imp_m += len(inserted)
                # Rows skipped by ON CONFLICT: either a concurrent push stole
                # the in-memory-allocated id, or the (session_id, role,
                # timestamp) dedup triple already exists (raced past the
                # msg_keys snapshot taken at the start of this push). A real
                # duplicate is dropped; an id collision with a genuinely new
                # message is retried once with a fresh id.
                retry_sql = (f"INSERT INTO messages ({', '.join(cols)}) "
                             f"VALUES ({', '.join(['%s'] * len(cols))}) "
                             "ON CONFLICT DO NOTHING RETURNING session_id, id")
                for md in new_msgs:
                    if (md["session_id"], md["id"]) in inserted:
                        continue
                    c.execute("SELECT 1 FROM messages WHERE workspace_id=%s "
                              "AND session_id=%s AND role=%s AND timestamp=%s",
                              (wid, md["session_id"], md.get("role"),
                               md.get("timestamp")))
                    if c.fetchone():
                        dup_m += 1
                        continue
                    md["id"] = next_ids.get(md["session_id"], 1)
                    next_ids[md["session_id"]] = md["id"] + 1
                    c.execute(retry_sql, [md.get(k) for k in cols])
                    if c.fetchone():
                        imp_m += 1
                    else:
                        dup_m += 1
        c.execute("""INSERT INTO sync_state (device_id, workspace_id, last_sync_at, sessions_synced, messages_synced)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (device_id, workspace_id) DO UPDATE SET
                last_sync_at = EXCLUDED.last_sync_at,
                sessions_synced = sync_state.sessions_synced + EXCLUDED.sessions_synced,
                messages_synced = sync_state.messages_synced + EXCLUDED.messages_synced""",
            (device_id, wid, now, imp_s + upd_s, imp_m))
    return {"sync_at": now, "imported": imp_s, "updated": upd_s,
            "new_messages": imp_m, "duplicates": dup_m}

@router.get("/status/{device_id}")
async def status(device_id: str, ws: dict = Depends(get_workspace_by_api_key)):
    wid = ws["workspace_id"]
    with get_conn() as conn:
        c = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        c.execute("SELECT * FROM sync_state WHERE device_id = %s AND workspace_id = %s", (device_id, wid))
        row = c.fetchone()
        c.execute("SELECT COUNT(*) as cnt FROM sessions WHERE workspace_id = %s", (wid,))
        ts = c.fetchone()["cnt"]
        c.execute("SELECT COUNT(*) as cnt FROM messages WHERE workspace_id = %s", (wid,))
        tm = c.fetchone()["cnt"]
    return {"device_id": device_id, "workspace_id": wid,
            "last_sync_at": dict(row)["last_sync_at"] if row else None,
            "total_sessions": ts, "total_messages": tm}

@router.get("/sessions")
async def list_sessions(ws: dict = Depends(get_workspace_by_api_key)):
    wid = ws["workspace_id"]
    with get_conn() as conn:
        c = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        c.execute("SELECT id, title, started_at, message_count, model, agent_type FROM sessions WHERE workspace_id=%s ORDER BY started_at DESC LIMIT 50", (wid,))
        return [dict(r) for r in c.fetchall()]

@router.get("/users")
async def list_users(ws: dict = Depends(get_workspace_by_api_key)):
    wid = ws["workspace_id"]
    with get_conn() as conn:
        c = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        c.execute("SELECT device_id, last_sync_at, sessions_synced, messages_synced FROM sync_state WHERE workspace_id = %s ORDER BY last_sync_at DESC", (wid,))
        return [dict(r) for r in c.fetchall()]

# ============================================================
# Startup
