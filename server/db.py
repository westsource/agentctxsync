"""Database access: connection pool, schema init, quota policy queries."""
import json
import secrets
import time
import time
from contextlib import contextmanager
from datetime import datetime

import psycopg2
import psycopg2.extras

from config import PG_DSN
def _pg_val(v):
    """psycopg2 无法直接绑定 dict/list 参数时兜底：序列化为 JSON 字符串。
    配合上方 register_adapter(dict, Json) 双保险，确保 /push 携带 meta 等
    复合字段不会以 "can't adapt type 'dict'" 500。"""
    return json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else v


def normalize_path_sep(value):
    """Server-side canonical path: replace backslashes with '/'. Windows
    clients report native (`E:\\a\\b`) paths; the server stores and returns
    the cross-platform `/` form for sessions cwd/git_repo_root and project
    paths. Non-str values pass through untouched."""
    return value.replace("\\", "/") if isinstance(value, str) else value

# ============================================================
# Configuration
# ============================================================

_pool = None

def _get_pool():
    """Lazily-created psycopg2 ThreadedConnectionPool.

    Created on first use, never at import time: the CI import smoke test
    stubs psycopg2 without the pool module, and module-level consumers must
    load the app without a database. ThreadedConnectionPool is thread-safe
    for getconn/putconn, which matters once the sync handlers run inside
    executor threads (see /push and /pull). maxconn 20 comfortably covers
    the default executor pool (min(32, cpu+4)) on a single process.
    """
    global _pool
    if _pool is None:
        from psycopg2.pool import ThreadedConnectionPool
        _pool = ThreadedConnectionPool(1, 20, PG_DSN)
    return _pool


def _close_pool():
    global _pool
    if _pool is not None:
        try:
            _pool.closeall()
        finally:
            _pool = None


@contextmanager
def get_conn():
    pool = _get_pool()
    conn = pool.getconn()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        try:
            pool.putconn(conn)
        except Exception:
            # Connection died while checked out (network drop / PG restart):
            # discard it so the pool never re-serves a broken handle.
            try:
                pool.putconn(conn, close=True)
            except Exception:
                pass


def init_db():
    with get_conn() as conn:
        c = conn.cursor()
        c.execute("""CREATE TABLE IF NOT EXISTS users (
            id SERIAL PRIMARY KEY,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            display_name TEXT,
            is_admin BOOLEAN DEFAULT FALSE,
            is_active BOOLEAN DEFAULT TRUE,
            created_at DOUBLE PRECISION,
            last_login_at DOUBLE PRECISION,
            must_change_password INTEGER DEFAULT 0,
            lang TEXT DEFAULT 'zh-CN'
        )""")
        c.execute("""CREATE TABLE IF NOT EXISTS workspaces (
            id SERIAL PRIMARY KEY,
            name TEXT NOT NULL,
            user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
            api_key TEXT UNIQUE NOT NULL,
            description TEXT,
            created_at DOUBLE PRECISION,
            UNIQUE(user_id, name)
        )""")
        c.execute("""CREATE TABLE IF NOT EXISTS invites (
            id SERIAL PRIMARY KEY,
            code TEXT UNIQUE NOT NULL,
            created_by INTEGER REFERENCES users(id),
            used INTEGER DEFAULT 0,
            used_by INTEGER,
            revoked INTEGER DEFAULT 0,
            expires_at DOUBLE PRECISION,
            note TEXT,
            created_at DOUBLE PRECISION
        )""")
        c.execute("""CREATE TABLE IF NOT EXISTS sessions (
            id TEXT, workspace_id INTEGER REFERENCES workspaces(id) ON DELETE CASCADE,
            user_id TEXT, source TEXT, model TEXT, model_config TEXT,
            system_prompt TEXT, parent_session_id TEXT, started_at DOUBLE PRECISION,
            ended_at DOUBLE PRECISION, end_reason TEXT, message_count INTEGER DEFAULT 0,
            tool_call_count INTEGER DEFAULT 0, input_tokens INTEGER DEFAULT 0,
            output_tokens INTEGER DEFAULT 0, cache_read_tokens INTEGER DEFAULT 0,
            cache_write_tokens INTEGER DEFAULT 0, reasoning_tokens INTEGER DEFAULT 0,
            cwd TEXT, git_branch TEXT, git_repo_root TEXT, billing_provider TEXT,
            billing_base_url TEXT, billing_mode TEXT, estimated_cost_usd DOUBLE PRECISION,
            actual_cost_usd DOUBLE PRECISION, cost_status TEXT, cost_source TEXT,
            pricing_version TEXT, title TEXT, api_call_count INTEGER DEFAULT 0,
            handoff_state TEXT, handoff_platform TEXT, handoff_error TEXT,
            rewind_count INTEGER DEFAULT 0, archived INTEGER DEFAULT 0,
            session_key TEXT, chat_id TEXT, chat_type TEXT, thread_id TEXT,
            compression_failure_cooldown_until DOUBLE PRECISION,
            compression_failure_error TEXT, display_name TEXT, origin_json TEXT,
            expiry_finalized INTEGER DEFAULT 0, compression_fallback_streak INTEGER DEFAULT 0,
            profile_name TEXT, compression_ineffective_count INTEGER DEFAULT 0,
            pinned INTEGER DEFAULT 0, last_synced_at DOUBLE PRECISION,
            agent_type TEXT DEFAULT 'hermes', meta JSONB,
            rev BIGINT NOT NULL DEFAULT 0, field_rev JSONB NOT NULL DEFAULT '{}'::jsonb,
            PRIMARY KEY (workspace_id, id)
        )""")
        c.execute("""CREATE TABLE IF NOT EXISTS messages (
            id INTEGER, session_id TEXT NOT NULL, workspace_id INTEGER REFERENCES workspaces(id) ON DELETE CASCADE,
            user_id TEXT, role TEXT,
            content TEXT, tool_call_id TEXT, tool_calls TEXT, tool_name TEXT,
            timestamp DOUBLE PRECISION, token_count INTEGER, finish_reason TEXT,
            reasoning TEXT, reasoning_content TEXT, reasoning_details TEXT,
            codex_reasoning_items TEXT, codex_message_items TEXT,
            platform_message_id TEXT, observed INTEGER DEFAULT 0, active INTEGER DEFAULT 1,
            compacted INTEGER DEFAULT 0, effect_disposition TEXT, api_content TEXT,
            display_kind TEXT, display_metadata TEXT,
            agent_type TEXT DEFAULT 'hermes', meta JSONB,
            PRIMARY KEY (workspace_id, session_id, id)
        )""")
        c.execute("""CREATE TABLE IF NOT EXISTS sync_state (
            device_id TEXT, workspace_id INTEGER REFERENCES workspaces(id) ON DELETE CASCADE,
            last_sync_at DOUBLE PRECISION,
            sessions_synced INTEGER DEFAULT 0, messages_synced INTEGER DEFAULT 0,
            PRIMARY KEY (device_id, workspace_id)
        )""")
        # Projects (hermes per-profile project store synced across devices).
        # id is the bare canonical id; profile holds the hermes profile
        # ('' = default). slug is unique per (workspace, profile) so
        # same-named projects in the same profile merge on push; merged_into
        # records the surviving id. Legacy prefixed ids (<profile>:<p_xxx>)
        # are split into profile + bare id by the id-scheme migration.
        c.execute("""CREATE TABLE IF NOT EXISTS projects (
            id TEXT, workspace_id INTEGER REFERENCES workspaces(id) ON DELETE CASCADE,
            slug TEXT NOT NULL, name TEXT NOT NULL, description TEXT,
            icon TEXT, color TEXT, board_slug TEXT, primary_path TEXT,
            created_at DOUBLE PRECISION, archived INTEGER DEFAULT 0,
            hidden INTEGER DEFAULT 0, hidden_at DOUBLE PRECISION,
            merged_into TEXT, agent_type TEXT DEFAULT 'hermes',
            profile TEXT DEFAULT '',
            rev BIGINT NOT NULL DEFAULT 0, field_rev JSONB NOT NULL DEFAULT '{}'::jsonb,
            PRIMARY KEY (workspace_id, id)
        )""")
        # id-scheme upgrade: existing deployments get projects.profile added
        # in place (CREATE TABLE above includes it for fresh installs).
        c.execute("ALTER TABLE projects ADD COLUMN IF NOT EXISTS profile TEXT DEFAULT ''")
        # field-level optimistic concurrency (mirrors sessions): rev is the
        # project-wide logical version, field_rev the per-field map.
        c.execute("ALTER TABLE projects ADD COLUMN IF NOT EXISTS rev BIGINT NOT NULL DEFAULT 0")
        c.execute("ALTER TABLE projects ADD COLUMN IF NOT EXISTS field_rev JSONB NOT NULL DEFAULT '{}'::jsonb")
        c.execute("""CREATE TABLE IF NOT EXISTS project_folders (
            workspace_id INTEGER REFERENCES workspaces(id) ON DELETE CASCADE,
            project_id TEXT NOT NULL, path TEXT NOT NULL, label TEXT,
            is_primary INTEGER DEFAULT 0, added_at DOUBLE PRECISION,
            PRIMARY KEY (workspace_id, project_id, path)
        )""")
        c.execute("""CREATE TABLE IF NOT EXISTS project_remap (
            workspace_id INTEGER REFERENCES workspaces(id) ON DELETE CASCADE,
            old_id TEXT NOT NULL, new_id TEXT NOT NULL,
            PRIMARY KEY (workspace_id, old_id)
        )""")
        # Idempotent multi-agent migration: existing deployments get the
        # agent_type/meta columns added in place (CREATE TABLE above already
        # includes them for fresh installs). agent_type distinguishes the
        # origin agent; meta carries agent-specific fields as JSONB.
        c.execute("ALTER TABLE sessions ADD COLUMN IF NOT EXISTS agent_type TEXT DEFAULT 'hermes'")
        c.execute("ALTER TABLE sessions ADD COLUMN IF NOT EXISTS meta JSONB")
        c.execute("ALTER TABLE messages ADD COLUMN IF NOT EXISTS agent_type TEXT DEFAULT 'hermes'")
        c.execute("ALTER TABLE messages ADD COLUMN IF NOT EXISTS meta JSONB")
        # Soft-hide (not delete): hidden rows stay in place for clients that
        # already hold them, but /pull stops delivering them and the Web UI
        # hides them by default. Restore = set hidden=0 (fully reversible).
        c.execute("ALTER TABLE sessions ADD COLUMN IF NOT EXISTS hidden INTEGER DEFAULT 0")
        c.execute("ALTER TABLE sessions ADD COLUMN IF NOT EXISTS hidden_at DOUBLE PRECISION")
        c.execute("ALTER TABLE messages ADD COLUMN IF NOT EXISTS hidden INTEGER DEFAULT 0")
        c.execute("ALTER TABLE messages ADD COLUMN IF NOT EXISTS hidden_at DOUBLE PRECISION")
        # Field-level optimistic concurrency (see ARCHITECTURE.md decision
        # record): rev is the session-wide monotonic version, field_rev the
        # per-field map of the rev at which each field was last accepted.
        # Baseline 0 = "never written under the new merge logic" (no history
        # reconstruction needed -- clients anchor lazily on first contact).
        c.execute("ALTER TABLE sessions ADD COLUMN IF NOT EXISTS rev BIGINT NOT NULL DEFAULT 0")
        c.execute("ALTER TABLE sessions ADD COLUMN IF NOT EXISTS field_rev JSONB NOT NULL DEFAULT '{}'::jsonb")
        # Concurrent pushes from two devices can race past the SELECT-based
        # message dedup (its key snapshot is taken per request), inserting the
        # same (session_id, role, timestamp) triple twice under different ids.
        # A partial unique index makes the insert itself idempotent; the
        # cleanup runs only when the index is missing (i.e. once per upgrade)
        # and keeps the earliest row per triple.
        c.execute("""DO $$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_indexes
                           WHERE indexname = 'uq_messages_dedup') THEN
                DELETE FROM messages a USING messages b
                WHERE a.workspace_id = b.workspace_id
                  AND a.session_id = b.session_id
                  AND a.role IS NOT NULL AND a.timestamp IS NOT NULL
                  AND a.role = b.role AND a.timestamp = b.timestamp
                  AND a.id > b.id;
                CREATE UNIQUE INDEX uq_messages_dedup
                    ON messages (workspace_id, session_id, role, timestamp)
                    WHERE role IS NOT NULL AND timestamp IS NOT NULL;
            END IF;
        END $$""")
        c.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS must_change_password INTEGER DEFAULT 0")
        # Account-level language preference (Web UI): persisted on the user so
        # it follows the account across devices; landing page still uses the
        # cookie. Read via the lang claim inside the JWT.
        c.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS lang TEXT DEFAULT 'zh-CN'")
        # ---- Quota / plan (generic enforcement, policy lives in DB) ----
        # plan: 'free' | 'unlimited'. Existing rows default to 'free'. The
        # operator (private ops backend) writes plan/quota_config directly to
        # the DB; the server only READS them on push, so policy changes apply
        # immediately with no API coupling and no restart.
        c.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS plan TEXT DEFAULT 'free'")
        # grant_plan: plan granted to a user who registers with this invite.
        c.execute("ALTER TABLE invites ADD COLUMN IF NOT EXISTS grant_plan TEXT DEFAULT 'unlimited'")
        # Per-plan limits. max_sessions NULL = unlimited; allowed_agents NULL
        # or empty = every agent allowed. The default 'free' cap of 200 keeps
        # the mechanism useful out of the box; the allowlist stays open until
        # an operator configures one.
        c.execute("""CREATE TABLE IF NOT EXISTS quota_config (
            plan TEXT PRIMARY KEY,
            max_sessions INTEGER,
            allowed_agents TEXT[]
        )""")
        # Operational audit trail: quota rejections + plan changes. Read by
        # the private ops backend; the open-source side only writes to it.
        c.execute("""CREATE TABLE IF NOT EXISTS audit_log (
            id SERIAL PRIMARY KEY,
            ts DOUBLE PRECISION,
            event TEXT,
            user_id INTEGER,
            workspace_id INTEGER,
            device_id TEXT,
            code TEXT,
            detail TEXT
        )""")
        c.execute("CREATE INDEX IF NOT EXISTS idx_audit_log_user ON audit_log(user_id)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_audit_log_ts ON audit_log(ts)")
        # Access statistics: daily request counts bucketed by channel
        # ('domain' = hostname Host header, 'ip' = IP-literal Host header)
        # and kind ('web' = browser pages, 'api' = sync push/pull & friends).
        # Written by the requestlog middleware, read by /web/admin/access.
        c.execute("""CREATE TABLE IF NOT EXISTS access_stats (
            stat_date DATE NOT NULL,
            channel TEXT NOT NULL,
            kind TEXT NOT NULL,
            count INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (stat_date, channel, kind)
        )""")
        # Upgrade path for deployments that shipped before the kind column:
        # add it, backfill legacy rows as 'api' (their web/api split is not
        # recoverable), and rebuild the primary key to include kind.
        c.execute("ALTER TABLE access_stats ADD COLUMN IF NOT EXISTS kind TEXT")
        c.execute("""DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_index i
                JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = ANY(i.indkey)
                WHERE i.indrelid = 'access_stats'::regclass AND i.indisprimary AND a.attname = 'kind'
            ) THEN
                ALTER TABLE access_stats DROP CONSTRAINT access_stats_pkey;
                ALTER TABLE access_stats ALTER COLUMN kind SET DEFAULT 'api';
                UPDATE access_stats SET kind = 'api' WHERE kind IS NULL;
                ALTER TABLE access_stats ALTER COLUMN kind SET NOT NULL;
                ALTER TABLE access_stats ADD PRIMARY KEY (stat_date, channel, kind);
            END IF;
        END $$""")
        # Per-device access: which sync client (device_id from /push /pull
        # /status bodies/paths) talked through the domain vs direct IP,
        # aggregated per day. Written by the requestlog middleware, read by
        # the admin access-devices drill-down (/web/admin/access/devices).
        c.execute("""CREATE TABLE IF NOT EXISTS access_device (
            stat_date DATE NOT NULL,
            device_id TEXT NOT NULL,
            agent TEXT NOT NULL DEFAULT 'unknown',
            channel TEXT NOT NULL,
            count INTEGER NOT NULL DEFAULT 0,
            last_seen DOUBLE PRECISION NOT NULL DEFAULT 0,
            client_version TEXT,
            PRIMARY KEY (stat_date, device_id, agent, channel)
        )""")
        # Agent column tracks which HERMES_SYNC_AGENT a device row belongs
        # to (a device can run several agents, each with its own MCP version);
        # added after initial release, so migrate existing tables. Legacy
        # rows get agent='unknown'. Rebuilds the primary key to include
        # agent, mirroring the access_stats.kind migration below.
        c.execute("ALTER TABLE access_device ADD COLUMN IF NOT EXISTS agent TEXT "
                  "DEFAULT 'unknown'")
        # MCP client version reported at last sync (requestlog upsert);
        # added before the agent column, kept idempotent for old deployments.
        c.execute("ALTER TABLE access_device ADD COLUMN IF NOT EXISTS client_version TEXT")
        c.execute("""DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_index i
                JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = ANY(i.indkey)
                WHERE i.indrelid = 'access_device'::regclass AND i.indisprimary AND a.attname = 'agent'
            ) THEN
                UPDATE access_device SET agent = 'unknown' WHERE agent IS NULL;
                ALTER TABLE access_device ALTER COLUMN agent SET NOT NULL;
                ALTER TABLE access_device DROP CONSTRAINT access_device_pkey;
                ALTER TABLE access_device ADD PRIMARY KEY (stat_date, device_id, agent, channel);
            END IF;
        END $$""")
        # User feedback ("问题反馈"): logged-in users submit issues/suggestions.
        # Admins list every row and can mark resolved; users see only their own.
        c.execute("""CREATE TABLE IF NOT EXISTS feedback (
            id SERIAL PRIMARY KEY,
            user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
            title TEXT NOT NULL,
            content TEXT NOT NULL,
            category TEXT NOT NULL DEFAULT 'other',
            status TEXT NOT NULL DEFAULT 'open',
            created_at DOUBLE PRECISION,
            resolved_at DOUBLE PRECISION,
            resolved_by INTEGER
        )""")
        # Global search: pg_trgm GIN indexes accelerate ILIKE on message
        # content and session titles (see docs/SEARCH.md). Idempotent; the
        # extension ships in the pgvector image and standard PG.
        c.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
        c.execute("""CREATE INDEX IF NOT EXISTS idx_messages_content_trgm
            ON messages USING gin (content gin_trgm_ops)""")
        c.execute("""CREATE INDEX IF NOT EXISTS idx_sessions_title_trgm
            ON sessions USING gin (title gin_trgm_ops)""")
        # Default 'free' cap raised 200 -> 300. DO UPDATE with a guard on the
        # old default so an operator's hand-set value is never overwritten;
        # fresh databases just get 300 directly.
        c.execute("""INSERT INTO quota_config (plan, max_sessions, allowed_agents)
            VALUES ('free', 300, NULL)
            ON CONFLICT (plan) DO UPDATE SET max_sessions = 300
            WHERE quota_config.max_sessions = 200""")
        c.execute("""INSERT INTO quota_config (plan, max_sessions, allowed_agents)
            VALUES ('unlimited', NULL, NULL) ON CONFLICT (plan) DO NOTHING""")
        c.execute("SELECT COUNT(*) FROM users")
        if c.fetchone()[0] == 0:
            admin_pw = secrets.token_urlsafe(12)
            from auth import hash_password  # 函数内: 避免 db->auth 循环
            pw_hash = hash_password(admin_pw)
            now = datetime.now().timestamp()
            c.execute(
                "INSERT INTO users (username, password_hash, display_name, is_admin, created_at, must_change_password) VALUES (%s, %s, %s, %s, %s, %s)",
                ("admin", pw_hash, "Administrator", True, now, 1)
            )
            from auth import generate_api_key  # 函数内: 避免 db->auth 循环
            ws_key = generate_api_key()
            c.execute(
                "INSERT INTO workspaces (name, user_id, api_key, description, created_at) VALUES (%s, %s, %s, %s, %s)",
                ("Default", 1, ws_key, "Default workspace", now)
            )
            print(f"*** Created default admin user: admin / {admin_pw} ***")
            print("*** 首次登录将强制修改该初始密码（/web/change-password）***")
            print(f"*** Created default workspace with API key: {ws_key} ***")


def plan_limits(plan, conn=None):
    """Read (max_sessions, allowed_agents) for a plan from quota_config.

    A missing row (e.g. an unknown plan value) resolves to NO limits so the
    sync service fails open instead of blocking legitimate pushes. Pass an
    open connection to stay inside the caller's transaction.
    """
    def _query(c):
        c.execute("SELECT max_sessions, allowed_agents FROM quota_config WHERE plan = %s", (plan,))
        return c.fetchone()
    if conn is not None:
        row = _query(conn.cursor())
    else:
        with get_conn() as conn:
            row = _query(conn.cursor())
    if not row:
        return None, None
    return row[0], row[1]


def quota_check(max_sessions, allowed_agents, existing_count, new_agents):
    """Pure quota gate for a push's NEW sessions.

    Returns (allowed, error_code).
    - Agent allowlist: every new agent must be listed (empty/None = allow all).
    - Session cap: existing active + new sessions must stay within
      max_sessions (None = unlimited).
    """
    if allowed_agents:
        for ag in new_agents:
            if ag not in allowed_agents:
                return False, "agent_not_allowed"
    if max_sessions is not None and existing_count + len(new_agents) > max_sessions:
        return False, "quota_exceeded_sessions"
    return True, None


def rel_sync_label(last_sync):
    """相对同步时间标签：刚刚 / X 分钟前 / X 小时前 / X 天前 / 尚未同步"""
    if not last_sync:
        return "尚未同步"
    diff = max(0, int(time.time() - last_sync))
    if diff < 60:
        return "刚刚同步"
    if diff < 3600:
        return f"{diff // 60} 分钟前同步"
    if diff < 86400:
        return f"{diff // 3600} 小时前同步"
    return f"{diff // 86400} 天前同步"

def get_user_workspaces(user_id):
    with get_conn() as conn:
        c = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        c.execute("SELECT * FROM workspaces WHERE user_id = %s ORDER BY created_at DESC", (user_id,))
        workspaces = c.fetchall()
    result = []
    for ws in workspaces:
        with get_conn() as conn:
            c = conn.cursor()
            c.execute("""
                SELECT
                    (SELECT COUNT(*) FROM sessions WHERE workspace_id = %s) AS sc,
                    (SELECT COUNT(DISTINCT device_id) FROM sync_state WHERE workspace_id = %s) AS dc,
                    (SELECT COUNT(*) FROM messages WHERE workspace_id = %s) AS mc,
                    (SELECT MAX(last_sync_at) FROM sync_state WHERE workspace_id = %s) AS last_sync
            """, (ws["id"], ws["id"], ws["id"], ws["id"]))
            row = c.fetchone()
            sc, dc, mc, last_sync = row[0], row[1], row[2], row[3]
            if not last_sync:
                c.execute("SELECT MAX(last_synced_at) FROM sessions WHERE workspace_id = %s", (ws["id"],))
                last_sync = c.fetchone()[0]
        result.append({"id": ws["id"], "name": ws["name"], "api_key": ws["api_key"],
                        "description": ws.get("description", ""), "session_count": sc, "device_count": dc,
                        "message_count": mc, "last_sync_at": last_sync, "sync_label": rel_sync_label(last_sync)})
    return result

def get_nav_workspaces(user_id):
    with get_conn() as conn:
        c = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        c.execute("SELECT id, name FROM workspaces WHERE user_id = %s ORDER BY name", (user_id,))
        return [dict(r) for r in c.fetchall()]
