import os, json, hashlib, hmac, secrets, time, base64, io, zipfile, gzip, re, html
import markdown
from datetime import datetime
from contextlib import contextmanager
from fastapi import FastAPI, Request, HTTPException, Depends
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from fastapi.responses import HTMLResponse, RedirectResponse, Response
import psycopg2
import psycopg2.extras
# Agent clients (opencode, workbuddy, ...) send the canonical `meta` field as a
# plain dict; the sessions/messages tables store it in a jsonb column.
# register_default_jsonb() only handles the read direction -- without an
# adapter for the dict type every /push carrying meta 500s with
# "can't adapt type 'dict'". Register dict -> Json once, globally.
psycopg2.extensions.register_adapter(dict, psycopg2.extras.Json)

def _pg_val(v):
    """psycopg2 无法直接绑定 dict/list 参数时兜底：序列化为 JSON 字符串。
    配合上方 register_adapter(dict, Json) 双保险，确保 /push 携带 meta 等
    复合字段不会以 "can't adapt type 'dict'" 500。"""
    return json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else v
import uvicorn
import jinja2
from translations import get_translations

# ============================================================
# Configuration
# ============================================================

# Required secrets come from the environment ONLY -- no hardcoded fallbacks
# (a leaked default would silently weaken every deployment).
PG_DSN = os.environ.get("HERMES_SYNC_PG_DSN")
MASTER_API_KEY = os.environ.get("HERMES_SYNC_MASTER_KEY")
JWT_SECRET = os.environ.get("HERMES_SYNC_JWT_SECRET") or secrets.token_hex(32)
TOKEN_EXPIRE_HOURS = int(os.environ.get("HERMES_SYNC_TOKEN_EXPIRE", "24"))
# Canonical public address baked into shipped client packages and shown on
# the help page. When set, every client download (regardless of which
# address the request arrived on) gets this as its SYNC_SERVER default —
# the mechanism for migrating existing clients to a new domain. When empty,
# the per-request base_url is used ("download from X -> default X").
PUBLIC_URL = os.environ.get("HERMES_SYNC_PUBLIC_URL", "").strip().rstrip("/")


def _client_default_server(server_url: str) -> str:
    """SYNC_SERVER default shipped to clients: the configured public URL
    when set, otherwise the address the current request arrived on."""
    return PUBLIC_URL or server_url

_MISSING = [k for k, v in (("HERMES_SYNC_PG_DSN", PG_DSN),
                            ("HERMES_SYNC_MASTER_KEY", MASTER_API_KEY)) if not v]
if _MISSING:
    raise SystemExit(f"Missing required environment variable(s): {', '.join(_MISSING)}. "
                     f"See server/.env.example for the full list.")

app = FastAPI(title="Agent Context Sync")

from fastapi.staticfiles import StaticFiles
STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
security = HTTPBearer(auto_error=False)

# ============================================================
# Jinja2 Setup
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
    """Message bubble timestamp: HH:MM only (viewer design)."""
    if not value:
        return ""
    try:
        return datetime.fromtimestamp(float(value)).strftime("%H:%M")
    except:
        return str(value)

jinja_env.filters["timestamp_fmt"] = timestamp_fmt
jinja_env.filters["msg_time"] = msg_time_fmt


def get_lang():
    if _current_request:
        # Logged-in users follow their account preference (lang claim in the
        # JWT, set at login and refreshed on switch); guests fall back to the
        # cookie so the landing page remembers the last choice per browser.
        token = _current_request.cookies.get("hsync_token")
        payload = verify_jwt(token) if token else None
        if payload and payload.get("lang"):
            return payload["lang"]
        return _current_request.cookies.get("lang", "zh-CN")
    return "zh-CN"
def render(template_name, context=None):
    ctx = context or {}
    ctx["get_flashed_messages"] = get_flashed_messages
    lang = get_lang()
    ctx["lang"] = lang
    ctx["t"] = get_translations(lang)
    # Surface error/success query params (i18n keys) so templates can show them
    if _current_request is not None:
        q = _current_request.query_params
        if "error" not in ctx and q.get("error"):
            ctx["error"] = q.get("error")
        if "success" not in ctx and q.get("success"):
            ctx["success"] = q.get("success")
    tmpl = jinja_env.get_template(template_name)
    html = tmpl.render(ctx)
    return HTMLResponse(content=html)

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
    token = _current_request.cookies.get("_flash") if _current_request else None
    if not token:
        return []
    messages = _flash_store.pop(token, [])
    return messages

_current_request = None

@app.middleware("http")
async def flash_middleware(request: Request, call_next):
    global _current_request
    _current_request = request
    response = await call_next(request)
    _current_request = None
    return response

# /web/* paths reachable while a forced password change is pending.
FORCED_PW_ALLOWLIST = {"/web/login", "/web/change-password", "/web/logout", "/web/register", "/web/set-language"}

@app.middleware("http")
async def enforce_password_change(request: Request, call_next):
    """Block /web/* pages until a must_change_password user sets a new password."""
    path = request.url.path
    if path.startswith("/web/") and path not in FORCED_PW_ALLOWLIST:
        token = request.cookies.get("hsync_token")
        payload = verify_jwt(token) if token else None
        if payload:
            with get_conn() as conn:
                c = conn.cursor()
                c.execute("SELECT must_change_password FROM users WHERE id = %s", (payload["sub"],))
                row = c.fetchone()
                if row and row[0]:
                    return RedirectResponse(url="/web/change-password?forced=1", status_code=303)
    return await call_next(request)

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
# ============================================================

@contextmanager
def get_conn():
    conn = psycopg2.connect(PG_DSN)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


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
        # id is the canonical id (<profile>:<p_xxx> or bare for default).
        # slug is unique per (workspace, profile) so same-named projects in
        # the same profile merge on push; merged_into records the surviving id.
        c.execute("""CREATE TABLE IF NOT EXISTS projects (
            id TEXT, workspace_id INTEGER REFERENCES workspaces(id) ON DELETE CASCADE,
            slug TEXT NOT NULL, name TEXT NOT NULL, description TEXT,
            icon TEXT, color TEXT, board_slug TEXT, primary_path TEXT,
            created_at DOUBLE PRECISION, archived INTEGER DEFAULT 0,
            hidden INTEGER DEFAULT 0, hidden_at DOUBLE PRECISION,
            merged_into TEXT, agent_type TEXT DEFAULT 'hermes',
            PRIMARY KEY (workspace_id, id)
        )""")
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
        c.execute("""INSERT INTO quota_config (plan, max_sessions, allowed_agents)
            VALUES ('free', 200, NULL) ON CONFLICT (plan) DO NOTHING""")
        c.execute("""INSERT INTO quota_config (plan, max_sessions, allowed_agents)
            VALUES ('unlimited', NULL, NULL) ON CONFLICT (plan) DO NOTHING""")
        c.execute("SELECT COUNT(*) FROM users")
        if c.fetchone()[0] == 0:
            admin_pw = secrets.token_urlsafe(12)
            pw_hash = hash_password(admin_pw)
            now = datetime.now().timestamp()
            c.execute(
                "INSERT INTO users (username, password_hash, display_name, is_admin, created_at, must_change_password) VALUES (%s, %s, %s, %s, %s, %s)",
                ("admin", pw_hash, "Administrator", True, now, 1)
            )
            ws_key = generate_api_key()
            c.execute(
                "INSERT INTO workspaces (name, user_id, api_key, description, created_at) VALUES (%s, %s, %s, %s, %s)",
                ("Default", 1, ws_key, "Default workspace", now)
            )
            print(f"*** Created default admin user: admin / {admin_pw} ***")
            print("*** 首次登录将强制修改该初始密码（/web/change-password）***")
            print(f"*** Created default workspace with API key: {ws_key} ***")


def hash_password(password):
    salt = secrets.token_hex(16)
    h = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 100000)
    return f"pbkdf2:sha256:100000:{salt}:{h.hex()}"


def verify_password(password, stored):
    parts = stored.split(":")
    if len(parts) != 5 or parts[0] != "pbkdf2":
        return False
    _, algo, iterations, salt, stored_hash = parts
    h = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), int(iterations))
    return hmac.compare_digest(h.hex(), stored_hash)


def generate_api_key():
    return "ws_" + secrets.token_hex(24)

def generate_invite_code():
    return "HSYNC-" + secrets.token_hex(4).upper()

def consume_invite(code, user_id):
    """Atomically mark a single-use invite as used.

    Returns an i18n error key (``register_*``) on failure, or None on success.
    The UPDATE guards the race: only an unused, unrevoked, non-expired invite
    can be consumed, and the rowcount check catches concurrent registration.
    """
    now = datetime.now().timestamp()
    with get_conn() as conn:
        c = conn.cursor()
        c.execute("SELECT id, used, revoked, expires_at FROM invites WHERE code = %s", (code,))
        row = c.fetchone()
        if not row or row[2]:
            return "register_invalid_code"
        if row[1]:
            return "register_used_code"
        if row[3] and row[3] < now:
            return "register_expired_code"
        c.execute("UPDATE invites SET used = 1, used_by = %s WHERE id = %s AND used = 0 AND revoked = 0",
                  (user_id, row[0]))
        if c.rowcount != 1:
            return "register_used_code"
    return None


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


def log_audit(conn, event, user_id, workspace_id, device_id, code, detail):
    """Append one row to audit_log inside the caller's transaction."""
    c = conn.cursor()
    c.execute(
        "INSERT INTO audit_log (ts, event, user_id, workspace_id, device_id, code, detail) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s)",
        (datetime.now().timestamp(), event, user_id, workspace_id, device_id, code, detail))


def invite_grant_plan(code):
    """Plan granted to a user registering with this invite (default unlimited).

    Unknown or invalid values fall back to 'unlimited' so registration never
    fails because of a bad plan value; the operator fixes it afterwards.
    """
    grant_plan = "unlimited"
    with get_conn() as conn:
        c = conn.cursor()
        c.execute("SELECT grant_plan FROM invites WHERE code = %s AND used = 0 AND revoked = 0", (code,))
        r = c.fetchone()
        if r and r[0] in ("free", "unlimited"):
            grant_plan = r[0]
    return grant_plan


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
        c.execute("SELECT 1 FROM invites WHERE revoked = 0 AND grant_plan != 'unlimited' LIMIT 1")
        return c.fetchone() is not None
    if conn is not None:
        return _query(conn.cursor())
    with get_conn() as conn:
        return _query(conn.cursor())


# ============================================================
# Authentication
# ============================================================

def create_jwt(user_id, username, is_admin, display_name="", lang="zh-CN"):
    payload = {
        "sub": str(user_id),
        "username": username,
        "is_admin": is_admin,
        "display_name": display_name,
        "lang": lang,
        "iat": int(time.time()),
        "exp": int(time.time()) + TOKEN_EXPIRE_HOURS * 3600,
    }
    header = base64.urlsafe_b64encode(json.dumps({"alg": "HS256", "typ": "JWT"}).encode()).rstrip(b"=").decode()
    body = base64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b"=").decode()
    sig = hmac.new(JWT_SECRET.encode(), f"{header}.{body}".encode(), hashlib.sha256).digest()
    signature = base64.urlsafe_b64encode(sig).rstrip(b"=").decode()
    return f"{header}.{body}.{signature}"


def verify_jwt(token):
    try:
        parts = token.split(".")
        if len(parts) != 3:
            return None
        header, body, signature = parts
        expected_sig = hmac.new(JWT_SECRET.encode(), f"{header}.{body}".encode(), hashlib.sha256).digest()
        expected_b64 = base64.urlsafe_b64encode(expected_sig).rstrip(b"=").decode()
        if not hmac.compare_digest(signature, expected_b64):
            return None
        padding = 4 - len(body) % 4
        if padding != 4:
            body += "=" * padding
        payload = json.loads(base64.urlsafe_b64decode(body))
        if payload.get("exp", 0) < time.time():
            return None
        return payload
    except Exception:
        return None


def get_current_user(request: Request):
    token = request.cookies.get("hsync_token")
    if not token:
        raise HTTPException(status_code=302, headers={"Location": "/web/login"})
    payload = verify_jwt(token)
    if not payload:
        raise HTTPException(status_code=302, headers={"Location": "/web/login"})
    return payload


def get_workspace_by_api_key(credentials: HTTPAuthorizationCredentials = Depends(security)):
    if not credentials:
        raise HTTPException(status_code=401, detail="API key required")
    key = credentials.credentials
    if key == MASTER_API_KEY:
        return {"workspace_id": None, "user_id": None, "is_master": True}
    with get_conn() as conn:
        c = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        c.execute("SELECT w.id as workspace_id, w.user_id, w.name FROM workspaces w WHERE w.api_key = %s", (key,))
        ws = c.fetchone()
        if not ws:
            raise HTTPException(status_code=401, detail="Invalid API key")
        return {"workspace_id": ws["workspace_id"], "user_id": ws["user_id"], "is_master": False}

def require_admin(user: dict = Depends(get_current_user)):
    if not user.get("is_admin"):
        raise HTTPException(status_code=403, detail="Admin access required")
    return user


# ============================================================
# Web UI Routes
# ============================================================

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

@app.get("/")
async def root(request: Request):
    """Root: not logged in → landing page; logged in → dashboard."""
    try:
        get_current_user(request)
    except Exception:
        return render("landing.html")
    return RedirectResponse(url="/web/")

@app.get("/web/login", response_class=HTMLResponse)
async def web_login(request: Request, error: str = ""):
    return render("login.html", {"error": error})

@app.post("/web/login", response_class=HTMLResponse)
async def web_login_post(request: Request):
    from fastapi import Form
    body = await request.form()
    username = body.get("username", "")
    password = body.get("password", "")
    with get_conn() as conn:
        c = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        c.execute("SELECT * FROM users WHERE username = %s AND is_active = TRUE", (username,))
        user = c.fetchone()
        if not user or not verify_password(password, user["password_hash"]):
            return render("login.html", {"error": "login_invalid"})
        now = datetime.now().timestamp()
        c.execute("UPDATE users SET last_login_at = %s WHERE id = %s", (now, user["id"]))
        # If the guest picked a language on the landing page (lang cookie),
        # adopt it as the account preference so the logged-in session (JWT
        # lang claim) follows it; otherwise keep the stored preference.
        cookie_lang = request.cookies.get("lang")
        if cookie_lang not in ("zh-CN", "en"):
            cookie_lang = None
        lang = cookie_lang or user.get("lang", "zh-CN")
        if cookie_lang and cookie_lang != user.get("lang"):
            c.execute("UPDATE users SET lang = %s WHERE id = %s",
                      (cookie_lang, user["id"]))
        token = create_jwt(user["id"], user["username"], user.get("is_admin", False), user.get("display_name", ""), lang)
        target = "/web/change-password?forced=1" if user.get("must_change_password") else "/web/"
        response = RedirectResponse(url=target, status_code=303)
        response.set_cookie(key="hsync_token", value=token, httponly=True, max_age=TOKEN_EXPIRE_HOURS * 3600, samesite="lax")
        return response

@app.get("/web/register", response_class=HTMLResponse)
async def web_register_page(request: Request, error: str = ""):
    # Pre-fill the invite code from a shared registration link (?code=...)
    code = request.query_params.get("code", "")
    return render("register.html", {"error": error, "code": code})

@app.post("/web/register", response_class=HTMLResponse)
async def web_register_submit(request: Request):
    from fastapi import Form
    body = await request.form()
    username = body.get("username", "").strip()
    display_name = body.get("display_name", "").strip() or username
    password = body.get("password", "")
    confirm = body.get("confirm_password", "")
    code = body.get("invite_code", "").strip()
    if not username or len(password) < 6 or not code:
        return RedirectResponse(url="/web/register?error=register_invalid_input", status_code=303)
    if password != confirm:
        return RedirectResponse(url="/web/register?error=pwd_mismatch", status_code=303)
    now = datetime.now().timestamp()
    # Resolve the plan granted by this invite BEFORE creating the user; the
    # invite is consumed afterwards (a failed consume rolls the user back).
    grant_plan = invite_grant_plan(code)
    with get_conn() as conn:
        c = conn.cursor()
        try:
            c.execute("INSERT INTO users (username, password_hash, display_name, is_admin, created_at, plan) "
                      "VALUES (%s, %s, %s, %s, %s, %s) RETURNING id",
                      (username, hash_password(password), display_name, False, now, grant_plan))
        except psycopg2.errors.UniqueViolation:
            return RedirectResponse(url="/web/register?error=register_user_exists", status_code=303)
        user_id = c.fetchone()[0]
        # Auto-create a default workspace for the new user.
        api_key = generate_api_key()
        c.execute("INSERT INTO workspaces (name, user_id, api_key, description, created_at) "
                  "VALUES (%s, %s, %s, %s, %s)",
                  ("默认工作空间", user_id, api_key, "", now))
    # Consume the invite AFTER the user exists; compensating delete on race.
    err = consume_invite(code, user_id)
    if err:
        with get_conn() as conn:
            c = conn.cursor()
            c.execute("DELETE FROM users WHERE id = %s", (user_id,))
        return RedirectResponse(url=f"/web/register?error={err}", status_code=303)
    return RedirectResponse(url="/web/login?success=register_success", status_code=303)

@app.get("/web/", response_class=HTMLResponse)
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
    # 最近同步的会话（跨工作空间，按同步/开始时间倒序取 6 条）
    recent_sessions = []
    with get_conn() as conn:
        c = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        c.execute("""
            SELECT s.id, s.workspace_id, s.title, s.agent_type, s.message_count,
                   COALESCE(s.last_synced_at, s.started_at) AS synced_at,
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
    ctx = {"user": user, "workspaces": nav_ws, "active_page": "dashboard",
           "ws_list": ws_list, "total_sessions": total_sessions, "total_messages": total_messages,
           "quota": quota, "recent_sessions": recent_sessions}
    return render("dashboard.html", ctx)

@app.get("/web/all-sessions", response_class=HTMLResponse)
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
                   COALESCE(s.last_synced_at, s.started_at) AS synced_at,
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
    return render("all_sessions.html", ctx)

@app.get("/web/change-password", response_class=HTMLResponse)
async def web_change_password_page(request: Request):
    try:
        user = get_current_user(request)
    except:
        return RedirectResponse(url="/web/login")
    return render("change_password.html", {"user": user,
                                           "forced": request.query_params.get("forced") == "1"})

@app.post("/web/change-password", response_class=HTMLResponse)
async def web_change_password(request: Request):
    try:
        user = get_current_user(request)
    except:
        return RedirectResponse(url="/web/login")
    from fastapi import Form
    body = await request.form()
    old_pw = body.get("old_password", "")
    new_pw = body.get("new_password", "")
    confirm = body.get("confirm_password", "")
    if new_pw != confirm:
        return RedirectResponse(url="/web/change-password?forced=1&error=pwd_mismatch", status_code=303)
    if len(new_pw) < 6:
        return RedirectResponse(url="/web/change-password?forced=1&error=pwd_short", status_code=303)
    with get_conn() as conn:
        c = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        c.execute("SELECT password_hash FROM users WHERE id = %s", (user["sub"],))
        u = c.fetchone()
        if not u or not verify_password(old_pw, u["password_hash"]):
            return RedirectResponse(url="/web/change-password?forced=1&error=old_pwd_wrong", status_code=303)
        c.execute("UPDATE users SET password_hash = %s, must_change_password = 0 WHERE id = %s", (hash_password(new_pw), user["sub"]))
    return RedirectResponse(url="/web/?success=pwd_changed", status_code=303)

@app.post("/web/update-profile", response_class=HTMLResponse)
async def web_update_profile(request: Request):
    """Update own profile: display name, optional password, admin flag.
    Mirrors the admin edit-user behavior; main admin is protected, and
    non-admin users can never grant themselves the admin role.
    Changing the password requires verifying the current one."""
    try:
        user = get_current_user(request)
    except:
        return RedirectResponse(url="/web/login")
    from fastapi import Form
    body = await request.form()
    display_name = body.get("display_name", "").strip()
    new_password = body.get("new_password", "")  # do not strip, keep parity with change-password
    old_password = body.get("old_password", "")
    is_admin = body.get("is_admin") == "true"
    uid = user["sub"]
    with get_conn() as conn:
        c = conn.cursor()
        c.execute("SELECT username, password_hash FROM users WHERE id = %s", (uid,))
        row = c.fetchone()
        if not row:
            return RedirectResponse(url="/web/login")
        username, password_hash = row
        if username == "admin":
            is_admin = True  # main admin role is never removable
        if not user.get("is_admin"):
            is_admin = False  # no self-elevation
        if new_password:
            if len(new_password) < 6:
                return RedirectResponse(url="/web/?error=pwd_short", status_code=303)
            if not verify_password(old_password, password_hash):
                return RedirectResponse(url="/web/?error=old_pwd_wrong", status_code=303)
            c.execute("UPDATE users SET display_name = %s, password_hash = %s, is_admin = %s, must_change_password = 0 WHERE id = %s",
                      (display_name, hash_password(new_password), is_admin, uid))
        else:
            c.execute("UPDATE users SET display_name = %s, is_admin = %s WHERE id = %s",
                      (display_name, is_admin, uid))
    return RedirectResponse(url="/web/?success=profile_updated", status_code=303)

@app.get("/web/set-language/{lang}")
async def web_set_language(lang: str, request: Request):
    if lang not in ("zh-CN", "en"):
        lang = "zh-CN"
    referer = request.headers.get("referer", "/web/")
    response = RedirectResponse(url=referer, status_code=303)
    response.set_cookie(key="lang", value=lang, max_age=365*24*3600, samesite="lax")
    token = request.cookies.get("hsync_token")
    payload = verify_jwt(token) if token else None
    if payload:
        # Logged in: persist the preference on the account and re-issue the
        # JWT so the current session follows immediately (get_lang reads the
        # lang claim). Guests only get the cookie above.
        with get_conn() as conn:
            c = conn.cursor()
            c.execute("UPDATE users SET lang = %s WHERE id = %s", (lang, int(payload["sub"])))
        new_token = create_jwt(payload["sub"], payload.get("username", ""),
                               payload.get("is_admin", False), payload.get("display_name", ""), lang)
        response.set_cookie(key="hsync_token", value=new_token, httponly=True,
                            max_age=TOKEN_EXPIRE_HOURS * 3600, samesite="lax")
    return response
@app.get("/web/logout")
async def web_logout():
    response = RedirectResponse(url="/", status_code=303)
    response.delete_cookie("hsync_token")
    return response

@app.post("/web/workspace/create", response_class=HTMLResponse)
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

@app.post("/web/workspace/{ws_id}/update", response_class=HTMLResponse)
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

@app.get("/web/workspace/{ws_id}", response_class=HTMLResponse)
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
    # Profile filter: inferred from the session id prefix (hermes sessions).
    #   bare id or 'default:' prefix  -> default profile
    #   '<name>:' prefix              -> named profile
    # non-hermes agents are never filtered by profile.
    profile = request.query_params.get("profile", "all")
    if agent != "hermes":
        profile = "all"  # profile only applies to hermes sessions
    if profile == "all":
        profile_clause = ""
    elif profile == "default":
        profile_clause = ("AND agent_type = 'hermes' "
                          "AND (id NOT LIKE '%%:%%' OR id LIKE 'default:%%')")
    else:
        # named profile: validate the name to avoid SQL injection
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", profile):
            profile = "all"
            profile_clause = ""
        else:
            profile_clause = ("AND agent_type = 'hermes' "
                              f"AND id LIKE '{profile}:%%'")
    profile_sel = (""", CASE
                         WHEN agent_type <> 'hermes' THEN NULL
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
        # available profiles for the filter dropdown: hermes id prefixes
        c.execute("""SELECT DISTINCT split_part(id, ':', 1) AS pfx
                     FROM sessions WHERE workspace_id = %s AND agent_type = 'hermes'
                       AND id LIKE '%%:%%' AND id NOT LIKE 'default:%%'""", (ws_id,))
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
    return render("workspace_detail.html", ctx)

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

@app.get("/web/workspace/{ws_id}/session/{sid}", response_class=HTMLResponse)
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
    return render("session_messages.html", ctx)
def _msg_ts(value):
    try:
        return datetime.fromtimestamp(float(value)).strftime("%Y-%m-%d %H:%M") if value else "-"
    except Exception:
        return str(value) if value else "-"

@app.get("/web/workspace/{ws_id}/session/{sid}/export")
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

@app.get("/web/workspace/{ws_id}/export")
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

@app.post("/web/workspace/{ws_id}/session/{sid}/hide")
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

@app.post("/web/workspace/{ws_id}/session/{sid}/unhide")
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

@app.get("/web/workspace/{ws_id}/trash", response_class=HTMLResponse)
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
    return render("trash_sessions.html", {"user": user, "workspaces": nav_ws,
                                          "active_page": f"workspace_{ws_id}",
                                          "ws": dict(ws), "trash_sessions": trash_sessions})

@app.get("/web/workspace/{ws_id}/session/{sid}/trash", response_class=HTMLResponse)
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
    return render("trash_messages.html", {"user": user, "workspaces": nav_ws,
                                          "active_page": f"workspace_{ws_id}",
                                          "ws": dict(ws), "session": dict(sess),
                                          "trash_messages": trash_messages})

@app.post("/web/workspace/{ws_id}/session/{sid}/message/{mid}/hide")
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

@app.post("/web/workspace/{ws_id}/session/{sid}/message/{mid}/unhide")
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

@app.post("/web/workspace/{ws_id}/import")
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

@app.get("/web/workspace/{ws_id}/delete")
async def web_delete_workspace(ws_id: int, request: Request):
    try:
        user = get_current_user(request)
    except:
        return RedirectResponse(url="/web/login")
    with get_conn() as conn:
        c = conn.cursor()
        c.execute("DELETE FROM workspaces WHERE id = %s AND user_id = %s", (ws_id, user["sub"]))
    return RedirectResponse(url="/web/?success=ws_deleted", status_code=303)

@app.post("/web/workspace/{ws_id}/regen-key", response_class=HTMLResponse)
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

@app.get("/web/admin/users", response_class=HTMLResponse)
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
    return render("admin_users.html", ctx)

@app.post("/web/admin/user/create", response_class=HTMLResponse)
async def web_create_user(request: Request):
    try:
        admin = get_current_user(request)
        if not admin.get("is_admin"):
            return RedirectResponse(url="/web/")
    except:
        return RedirectResponse(url="/web/login")
    from fastapi import Form
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

@app.get("/web/admin/user/{uid}/edit", response_class=HTMLResponse)
async def web_edit_user_form(uid: int, request: Request):
    try:
        admin = get_current_user(request)
        if not admin.get("is_admin"):
            return RedirectResponse(url="/web/")
    except:
        return RedirectResponse(url="/web/login")
    return RedirectResponse(url="/web/admin/users", status_code=303)

@app.post("/web/admin/user/{uid}/edit", response_class=HTMLResponse)
async def web_edit_user(uid: int, request: Request):
    try:
        admin = get_current_user(request)
        if not admin.get("is_admin"):
            return RedirectResponse(url="/web/")
    except:
        return RedirectResponse(url="/web/login")
    from fastapi import Form
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

@app.get("/web/admin/user/{uid}/toggle")
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

@app.get("/web/admin/workspaces", response_class=HTMLResponse)
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
    return render("admin_workspaces.html", ctx)

@app.get("/web/invites", response_class=HTMLResponse)
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
    return render("admin_invites.html", ctx)

@app.get("/web/admin/invites", response_class=HTMLResponse)
async def web_admin_invites_old(request: Request):
    return RedirectResponse(url="/web/invites", status_code=303)

@app.post("/web/invite/create", response_class=HTMLResponse)
async def web_create_invite(request: Request):
    try:
        user = get_current_user(request)
    except:
        return RedirectResponse(url="/web/login")
    from fastapi import Form
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

@app.post("/web/invite/{inv_id}/revoke", response_class=HTMLResponse)
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


# ============================================================
# MCP Client Distribution (agent registry driven)
# ============================================================

from agents import AGENTS

# Only Hermes has been strictly tested end-to-end. The other agent adapters
# stay in AGENTS (registry) for development, but their MCP client
# distribution and help-page onboarding are taken offline until validated.
# Re-enable an agent by adding its key here.
PUBLIC_AGENTS = ("hermes", "workbuddy")

# The client source is the repository `mcp/` package. Two layouts exist:
#   repo:    <repo>/server/server.py  + <repo>/mcp/            (one level up)
#   deploy:  /opt/hermes-sync-mcp/server.py + /opt/hermes-sync-mcp/mcp/
#            (same directory, deploy-server.sh copies mcp/ next to server.py)
_SRV_DIR = os.path.dirname(os.path.abspath(__file__))
CLIENT_DIR = os.path.join(_SRV_DIR, "mcp") \
    if os.path.isdir(os.path.join(_SRV_DIR, "mcp")) \
    else os.path.join(os.path.dirname(_SRV_DIR), "mcp")

# Client distribution version. Bump this together with CLIENT_VERSION in
# mcp/server.py whenever the client package changes; clients compare it via
# /api/client/manifest and auto-update.
CLIENT_VERSION = "2026.08.17.1"

def _client_archive_files():
    """[(arcname, source_path)] for every file shipped in the client zip."""
    files = [("mcp/server.py", os.path.join(CLIENT_DIR, "server.py")),
             ("mcp/updater.py", os.path.join(CLIENT_DIR, "updater.py")),
             ("mcp/run.sh", os.path.join(CLIENT_DIR, "run.sh")),
             ("mcp/run.bat", os.path.join(CLIENT_DIR, "run.bat"))]
    ad = os.path.join(CLIENT_DIR, "adapters")
    if os.path.isdir(ad):
        for name in sorted(os.listdir(ad)):
            if name.endswith(".py"):
                files.append((f"mcp/adapters/{name}", os.path.join(ad, name)))
    return files

# The client zip ships mcp/server.py with two defaults rewritten at build
# time: SYNC_SERVER -> the serving server's address (configured PUBLIC_URL,
# or per-request base_url) and HERMES_SYNC_AGENT -> the agent the archive
# was downloaded for. The manifest hash must therefore be computed over the
# shipped bytes, not the raw repo file — otherwise client-side verification
# fails.
_SYNC_SERVER_RE = re.compile(
    r'(SYNC_SERVER = os\.environ\.get\("HERMES_SYNC_SERVER", )"[^"]*"')
_SYNC_AGENT_RE = re.compile(
    r'(AGENT = os\.environ\.get\("HERMES_SYNC_AGENT", )"[^"]*"')


def _ship_bytes(arcname: str, src: str, default_server: str,
                agent: str = "hermes") -> bytes:
    """Exact bytes shipped inside a client archive for one file."""
    if arcname == "mcp/server.py":
        text = open(src, encoding="utf-8").read()
        text = _SYNC_SERVER_RE.sub(
            lambda m: m.group(1) + json.dumps(default_server), text)
        text = _SYNC_AGENT_RE.sub(
            lambda m: m.group(1) + json.dumps(agent), text)
        return text.encode("utf-8")
    with open(src, "rb") as f:
        return f.read()


def _client_manifest_files(default_server: str, agent: str = "hermes"):
    """[{path, sha256, size}] for every shipped file, hashed over the
    bytes actually placed in the archive (server.py defaults rewritten)."""
    manifest = []
    for arcname, src in _client_archive_files():
        try:
            data = _ship_bytes(arcname, src, default_server, agent)
        except OSError:
            continue
        rel = arcname[len("mcp/"):] if arcname.startswith("mcp/") else arcname
        manifest.append({"path": rel, "sha256": hashlib.sha256(data).hexdigest(),
                         "size": len(data)})
    return manifest


def _build_client_zip(agent: str, default_server: str,
                      readme: str | None = None) -> bytes:
    """Client archive bytes (mcp/ package + manifest.json at the top level).
    ``readme``, when given, is written as mcp/README.md inside the archive."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for arcname, src in _client_archive_files():
            try:
                zf.writestr(arcname, _ship_bytes(arcname, src, default_server,
                                                 agent))
            except OSError:
                continue
        if readme:
            zf.writestr("mcp/README.md", readme)
        zf.writestr("manifest.json", json.dumps(
            {"version": CLIENT_VERSION, "files": _client_manifest_files(default_server, agent)},
            ensure_ascii=False))
    return buf.getvalue()

def _build_readme(agent_key, api_key, ws_name, server_url):
    """Build the in-archive README. ``api_key`` is a placeholder string
    (never a real key -- the archive may be shared)."""
    agent = AGENTS[agent_key]
    lang = get_lang()
    is_en = lang == "en"
    t_ = (lambda zh, en: en if is_en else zh)
    register = agent["register"]["en" if is_en else "zh"] \
        .replace("<KEY>", api_key).replace("<SERVER>", server_url)
    if is_en:
        return (
            f"# Agent Context Sync MCP Client ({agent['label']})\n\n"
            f"Workspace: **{ws_name}**  \u00b7  Sync server: `{server_url}`\n\n"
            "This client connects your agent to the sync server above so sessions "
            "sync across devices and across agents (Hermes / Codex / opencode / "
            "Reasonix / OpenClaw / WorkBuddy share the same workspace pool).\n\n"
            "## Files\n"
            "- `mcp/server.py` - the MCP server (stdio)\n"
            "- `mcp/run.sh` / `mcp/run.bat` - optional launchers\n"
            "- `mcp/adapters/` - per-agent local store adapters (loaded by server.py)\n\n"
            f"Agent: **{agent['label']}**  \u00b7  {agent['desc']['en']}\n\n"
            f"Local store: {agent['store']['en']}\n\n"
            "## Install\n"
            "1. Unzip this archive to a folder, e.g. `C:\\hermes-sync-mcp`.\n"
            "2. Register the MCP server for your agent (replace `<PYTHON>` with a "
            "Python 3.10+ interpreter, `<EXTRACT_DIR>` with the folder from step 1, "
            "and `<YOUR_API_KEY>` with your workspace API key from the help page):\n\n"
            "```bash\n" + register + "\n```\n\n"
            "## Remove (old version / cleanup)\n"
            f"```bash\n{agent.get('uninstall', {}).get('en', '')}\n```\n\n"
            "## Verify\n"
            f"```bash\n{agent['verify']}\n```\n"
            "Tools: `hermes_sync_status`, `hermes_sync_pull`, `hermes_sync_push`, `hermes_sync_full`\n\n"
            "The client pulls once on startup (bootstrapping local data when the "
            "remote workspace is empty), then auto-syncs every 300 seconds.\n\n"
            "## Auto-update\n"
            "The client checks for updates shortly after startup and then every "
            "24 hours; new files replace the old ones in place and take effect "
            "on the next agent restart (set HERMES_SYNC_AUTO_UPDATE=0 to "
            "disable, or HERMES_SYNC_UPDATE_INTERVAL to change the interval).\n"
        )
    return (
        f"# Hermes 会话同步 MCP 客户端（{agent['label']}）\n\n"
        f"工作空间：**{ws_name}**  \u00b7  同步服务器：`{server_url}`\n\n"
        "将该 Agent 接入同步服务器，实现跨设备、跨 Agent（Hermes / Codex / "
        "opencode / Reasonix / OpenClaw / WorkBuddy 共享同一工作空间会话池）的会话同步。\n\n"
        "## 文件说明\n"
        "- `mcp/server.py` — MCP 服务端程序（stdio 模式）\n"
        "- `mcp/run.sh` / `mcp/run.bat` — 可选启动脚本\n"
        "- `mcp/adapters/` — 各 Agent 本地存储适配器（由 server.py 加载）\n\n"
        f"Agent：**{agent['label']}**  \u00b7  {agent['desc']['zh']}\n\n"
        f"本地存储：{agent['store']['zh']}\n\n"
        "## 安装步骤\n"
        "1. 将本压缩包解压到任意目录，例如 `C:\\hermes-sync-mcp`。\n"
        "2. 按你的 Agent 注册 MCP Server（将 `<PYTHON>` 替换为 Python 3.10+ 解释器路径，"
        "`<EXTRACT_DIR>` 替换为第 1 步的目录，`<YOUR_API_KEY>` 替换为帮助页中的工作空间 "
        "API Key）：\n\n"
        "```bash\n" + register + "\n```\n\n"
        "## 移除旧版（重新安装或清理时）\n"
        f"```bash\n{agent.get('uninstall', {}).get('zh', '')}\n```\n\n"
        "## 验证\n"
        f"```bash\n{agent['verify']}\n```\n"
        "可用工具：`hermes_sync_status`、`hermes_sync_pull`、`hermes_sync_push`、`hermes_sync_full`\n\n"
        "客户端启动时自动拉取一次（远程为空时自动推送本地数据完成首次配对），"
        "之后每 300 秒自动同步一次。\n\n"
        "## 自动更新\n"
        "客户端启动后稍候即检查一次更新，之后每 24 小时检查一次；新文件就地替换，"
        "**重启 Agent 后生效**（设 `HERMES_SYNC_AUTO_UPDATE=0` 可关闭，"
        "或用 `HERMES_SYNC_UPDATE_INTERVAL` 调整检查间隔）。\n"
    )

@app.get("/web/help-hermes")
async def web_help_hermes_legacy(request: Request):
    """Legacy route: keep old links working via a permanent redirect."""
    return RedirectResponse(url="/web/help", status_code=301)

@app.get("/web/help", response_class=HTMLResponse)
async def web_help(request: Request):
    try:
        user = get_current_user(request)
    except:
        return RedirectResponse(url="/web/login")
    nav_ws = get_nav_workspaces(user["sub"])
    ws_list = get_user_workspaces(user["sub"])
    # The address shown/shipped: configured public URL, else this request's.
    server_url = _client_default_server(str(request.base_url).rstrip("/"))
    lang = get_lang()
    # agents registry uses "zh"/"en" keys; get_lang() returns "zh-CN"/"en"
    agent_lang = "en" if lang.startswith("en") else "zh"
    # Per-agent install cards: pick the language, substitute the register
    # command into <REGISTER> steps and keep placeholders for user values.
    agents_ctx = {}
    for key, a in AGENTS.items():
        entry = {"label": a["label"], "desc": a["desc"][agent_lang],
                 "store": a["store"][agent_lang], "verify": a["verify"],
                 "uninstall": a.get("uninstall", {}).get(agent_lang, ""),
                 "downloadable": key in PUBLIC_AGENTS,
                 # register template with a __WS_KEY__ placeholder that the
                 # help page fills in client-side with the selected
                 # workspace's API key (wizard step 2).
                 "register": a["register"][agent_lang]
                     .replace("<KEY>", "__WS_KEY__")
                     .replace("<SERVER>", server_url)}
        steps = []
        for step in a["install"][agent_lang]:
            s = dict(step)
            if "code" in s and s["code"] == "<REGISTER>":
                s["code"] = a["register"][agent_lang] \
                    .replace("<KEY>", "<YOUR_API_KEY>") \
                    .replace("<SERVER>", server_url)
            steps.append(s)
        entry["install"] = steps
        agents_ctx[key] = entry
    ctx = {"user": user, "workspaces": nav_ws, "active_page": "help_hermes",
           "ws_list": ws_list, "server_url": server_url, "agents": agents_ctx}
    return render("help_hermes.html", ctx)

@app.get("/web/download/mcp-client")
async def web_download_mcp_client(request: Request, ws_id: int = 0, agent: str = "hermes"):
    try:
        user = get_current_user(request)
    except:
        return RedirectResponse(url="/web/login")
    if agent not in PUBLIC_AGENTS:
        raise HTTPException(status_code=404, detail=f"Agent {agent!r} is not publicly released yet")
    with get_conn() as conn:
        c = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        if ws_id:
            c.execute("SELECT * FROM workspaces WHERE id = %s AND user_id = %s", (ws_id, user["sub"]))
        else:
            c.execute("SELECT * FROM workspaces WHERE user_id = %s ORDER BY created_at DESC LIMIT 1", (user["sub"],))
        ws = c.fetchone()
    if not ws:
        return RedirectResponse(url="/web/help")
    server_url = str(request.base_url).rstrip("/")
    default_server = _client_default_server(server_url)
    data = _build_client_zip(
        agent, default_server,
        readme=_build_readme(agent, "<YOUR_API_KEY>", ws["name"], default_server))
    return Response(
        content=data,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="agentctxsync-mcp-client-{agent}.zip"'},
    )


# ============================================================
# Client auto-update distribution (Sync API auth)
# ============================================================

@app.get("/api/client/manifest")
async def api_client_manifest(request: Request, agent: str = "hermes", v: str = "",
                              ws: dict = Depends(get_workspace_by_api_key)):
    """Lightweight version check: the client sends its local version via ``v``
    and gets update_available + the file manifest (sha256/size) without
    downloading the whole archive."""
    if agent not in PUBLIC_AGENTS:
        raise HTTPException(status_code=404, detail=f"Agent {agent!r} is not publicly released yet")
    default_server = _client_default_server(str(request.base_url).rstrip("/"))
    return {"version": CLIENT_VERSION,
            "update_available": v != CLIENT_VERSION,
            "files": _client_manifest_files(default_server, agent)}

@app.get("/api/client/download")
async def api_client_download(request: Request, agent: str = "hermes",
                              ws: dict = Depends(get_workspace_by_api_key)):
    """Client archive zip with an embedded manifest.json for verification."""
    if agent not in PUBLIC_AGENTS:
        raise HTTPException(status_code=404, detail=f"Agent {agent!r} is not publicly released yet")
    default_server = _client_default_server(str(request.base_url).rstrip("/"))
    data = _build_client_zip(agent, default_server)
    return Response(
        content=data,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="agentctxsync-mcp-client-{agent}.zip"'},
    )


# ============================================================
# API Routes (unchanged)
# ============================================================

@app.post("/api/auth/register")
async def api_register(request: Request, user: dict = Depends(require_admin)):
    body = await request.json()
    username = body.get("username", "").strip()
    password = body.get("password", "")
    display_name = body.get("display_name", username)
    is_admin = body.get("is_admin", False)
    if not username or len(password) < 6:
        raise HTTPException(status_code=400, detail="Username required, password >= 6 chars")
    with get_conn() as conn:
        c = conn.cursor()
        try:
            c.execute("INSERT INTO users (username, password_hash, display_name, is_admin, created_at) VALUES (%s, %s, %s, %s, %s)",
                      (username, hash_password(password), display_name, is_admin, datetime.now().timestamp()))
        except psycopg2.errors.UniqueViolation:
            raise HTTPException(status_code=409, detail="Username already exists")
    return {"success": True}

@app.post("/api/auth/login")
async def api_login(request: Request):
    body = await request.json()
    username = body.get("username", "")
    password = body.get("password", "")
    with get_conn() as conn:
        c = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        c.execute("SELECT * FROM users WHERE username = %s AND is_active = TRUE", (username,))
        user = c.fetchone()
        if not user or not verify_password(password, user["password_hash"]):
            raise HTTPException(status_code=401, detail="login_invalid")
        now = datetime.now().timestamp()
        c.execute("UPDATE users SET last_login_at = %s WHERE id = %s", (now, user["id"]))
        token = create_jwt(user["id"], user["username"], user.get("is_admin", False), lang=user.get("lang", "zh-CN"))
    return {"token": token, "username": username, "display_name": user.get("display_name"), "is_admin": user.get("is_admin"), "must_change_password": bool(user.get("must_change_password"))}

@app.get("/api/me")
async def api_me(user: dict = Depends(get_current_user)):
    return {"user_id": user["sub"], "username": user["username"], "is_admin": user.get("is_admin")}

@app.post("/api/me/change-password")
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

@app.get("/api/workspaces")
async def api_list_workspaces(user: dict = Depends(get_current_user)):
    with get_conn() as conn:
        c = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        c.execute("SELECT id, name, api_key, description, created_at FROM workspaces WHERE user_id = %s ORDER BY created_at DESC", (user["sub"],))
        return [dict(r) for r in c.fetchall()]

@app.post("/api/workspaces")
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

@app.delete("/api/workspaces/{ws_id}")
async def api_delete_workspace(ws_id: int, user: dict = Depends(get_current_user)):
    with get_conn() as conn:
        c = conn.cursor()
        c.execute("DELETE FROM workspaces WHERE id = %s AND user_id = %s", (ws_id, user["sub"]))
    return {"success": True}

@app.post("/api/workspaces/{ws_id}/regen-key")
async def api_regen_key(ws_id: int, user: dict = Depends(get_current_user)):
    new_key = generate_api_key()
    with get_conn() as conn:
        c = conn.cursor()
        c.execute("UPDATE workspaces SET api_key = %s WHERE id = %s AND user_id = %s", (new_key, ws_id, user["sub"]))
    return {"api_key": new_key}

@app.get("/api/admin/users")
async def api_admin_users(user: dict = Depends(require_admin)):
    with get_conn() as conn:
        c = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        c.execute("SELECT u.*, COUNT(w.id) as ws_count FROM users u LEFT JOIN workspaces w ON w.user_id = u.id GROUP BY u.id ORDER BY u.created_at DESC")
        return [dict(r) for r in c.fetchall()]

@app.post("/api/admin/users/{uid}/toggle")
async def api_admin_toggle_user(uid: int, user: dict = Depends(require_admin)):
    with get_conn() as conn:
        c = conn.cursor()
        c.execute("UPDATE users SET is_active = NOT is_active WHERE id = %s", (uid,))
    return {"success": True}

@app.get("/api/admin/workspaces")
async def api_admin_workspaces(user: dict = Depends(require_admin)):
    with get_conn() as conn:
        c = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        # Never expose api_key to admins: keys belong to the workspace owner.
        c.execute("""SELECT w.id, w.name, w.user_id, w.description, w.created_at,
            u.username as owner,
            (SELECT MAX(st.last_sync_at) FROM sync_state st WHERE st.workspace_id = w.id) as last_sync_at
            FROM workspaces w JOIN users u ON w.user_id = u.id ORDER BY w.created_at DESC""")
        return [dict(r) for r in c.fetchall()]

# ============================================================
# Sync API
# ============================================================

@app.get("/health")
async def health():
    try:
        with get_conn() as conn:
            c = conn.cursor()
            c.execute("SELECT 1")
        return {"status": "ok", "service": "hermes-session-sync", "backend": "postgresql", "auth": "multi-tenant", "name": "Agent Context Sync"}
    except Exception as e:
        return {"status": "error", "detail": str(e)}

@app.post("/pull")
async def pull(request: Request, ws: dict = Depends(get_workspace_by_api_key)):
    body = await request.json()
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
        sessions = []
        for row in c.fetchall():
            s = dict(row)
            sid = s["id"]
            if last_sync_at == 0:
                c.execute("SELECT * FROM messages WHERE session_id = %s AND workspace_id = %s AND COALESCE(hidden,0) = 0 ORDER BY timestamp", (sid, wid))
            else:
                c.execute("SELECT * FROM messages WHERE session_id = %s AND workspace_id = %s AND COALESCE(hidden,0) = 0 AND timestamp > %s ORDER BY timestamp",
                          (sid, wid, last_sync_at))
            s["messages"] = [dict(m) for m in c.fetchall()]
            sessions.append(s)
    now = datetime.now().timestamp()
    return {"sync_at": now, "session_count": len(sessions),
            "total_sessions": total_sessions,
            "message_count": sum(len(s["messages"]) for s in sessions), "sessions": sessions}

@app.post("/push")
async def push(request: Request, ws: dict = Depends(get_workspace_by_api_key)):
    body = await request.json()
    device_id = body.get("device_id", "unknown")
    sessions_data = body["sessions"]
    wid = ws["workspace_id"]
    now = datetime.now().timestamp()
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
                    c.execute("SELECT 1 FROM messages WHERE session_id=%s AND role=%s AND timestamp=%s AND workspace_id=%s",
                              (msid, role, ts, wid))
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
                # push as a duplicate instead of duplicating the row.
                content = msg.get("content")
                if role is not None and isinstance(content, str) and content:
                    c.execute("SELECT 1 FROM messages WHERE session_id=%s AND role=%s AND content=%s AND workspace_id=%s",
                              (msid, role, content, wid))
                    if c.fetchone():
                        dup_m += 1
                        continue
                md = {k: _pg_val(v) for k, v in msg.items()
                      if v is not None and k in msg_cols}
                md.setdefault("agent_type", session_agent)
                md["session_id"] = sid
                md["workspace_id"] = wid
                auto_id = "id" not in md
                if auto_id:
                    # messages.id is NOT NULL and clients without local ids
                    # (codex/reasonix/...) do not send one: allocate the next
                    # id for this session so the (session, id) key stays free.
                    c.execute("SELECT COALESCE(MAX(id), 0) + 1 FROM messages "
                              "WHERE session_id = %s AND workspace_id = %s",
                              (sid, wid))
                    md["id"] = c.fetchone()[0]
                cols = ", ".join(md.keys())
                ph = ", ".join(["%s"] * len(md))
                insert_sql = (f"INSERT INTO messages ({cols}) VALUES ({ph}) "
                              "ON CONFLICT (workspace_id, session_id, id) DO NOTHING")
                c.execute(insert_sql, list(md.values()))
                if c.rowcount == 0 and auto_id:
                    # concurrent push allocated the same id: retry with a
                    # fresh one (a few attempts, then give up as duplicate)
                    for _ in range(3):
                        c.execute("SELECT COALESCE(MAX(id), 0) + 1 FROM messages "
                                  "WHERE session_id = %s AND workspace_id = %s",
                                  (sid, wid))
                        md["id"] = c.fetchone()[0]
                        c.execute(insert_sql, list(md.values()))
                        if c.rowcount:
                            break
                    else:
                        dup_m += 1
                        continue
                elif c.rowcount == 0:
                    dup_m += 1
                    continue
                imp_m += 1
        c.execute("""INSERT INTO sync_state (device_id, workspace_id, last_sync_at, sessions_synced, messages_synced)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (device_id, workspace_id) DO UPDATE SET
                last_sync_at = EXCLUDED.last_sync_at,
                sessions_synced = sync_state.sessions_synced + EXCLUDED.sessions_synced,
                messages_synced = sync_state.messages_synced + EXCLUDED.messages_synced""",
            (device_id, wid, now, imp_s + upd_s, imp_m))
    return {"sync_at": now, "imported": imp_s, "updated": upd_s,
            "new_messages": imp_m, "duplicates": dup_m}

@app.post("/api/projects/push")
async def api_projects_push(request: Request, ws: dict = Depends(get_workspace_by_api_key)):
    """Upsert projects + folders. Same (workspace, profile, slug) with a
    different id merges: keep the earliest id, union folders, record remap."""
    body = await request.json()
    wid = ws["workspace_id"]
    projects = body.get("projects", []) or []
    now = datetime.now().timestamp()
    with get_conn() as conn:
        c = conn.cursor()
        imp = upd = merged = 0
        for p in projects:
            pid = p["id"]
            slug = p.get("slug") or p["name"]
            # profile = prefix before ':' ('' for default)
            profile = pid.split(":", 1)[0] if ":" in pid else ""
            # find existing same (workspace, profile, slug)
            c.execute("""SELECT id FROM projects
                         WHERE workspace_id = %s AND agent_type = %s
                           AND slug = %s AND (CASE WHEN id LIKE '%%:%%' THEN split_part(id,':',1) ELSE '' END) = %s
                         ORDER BY created_at ASC, id ASC LIMIT 1""",
                      (wid, "hermes", slug, profile))
            row = c.fetchone()
            if row and row[0] != pid:
                # merge into the existing (earliest) project
                keep = row[0]
                # union folders from the incoming project
                for f in p.get("folders", []):
                    c.execute("""INSERT INTO project_folders
                                 (workspace_id, project_id, path, label, is_primary, added_at)
                                 VALUES (%s,%s,%s,%s,%s,%s)
                                 ON CONFLICT (workspace_id, project_id, path) DO NOTHING""",
                              (wid, keep, f.get("path"), f.get("label"),
                               f.get("is_primary") or 0, f.get("added_at") or now))
                # record remap (idempotent) and drop the merged row
                c.execute("""INSERT INTO project_remap (workspace_id, old_id, new_id)
                             VALUES (%s,%s,%s)
                             ON CONFLICT (workspace_id, old_id) DO UPDATE SET new_id = EXCLUDED.new_id""",
                          (wid, pid, keep))
                c.execute("DELETE FROM projects WHERE workspace_id = %s AND id = %s", (wid, pid))
                c.execute("DELETE FROM project_folders WHERE workspace_id = %s AND project_id = %s", (wid, pid))
                merged += 1
                continue
            # upsert project
            cols = [k for k in ("slug","name","description","icon","color","board_slug",
                                "primary_path","created_at","archived") if p.get(k) is not None]
            if row:
                sets = ", ".join(f"{k} = %s" for k in cols)
                vals = [p[k] for k in cols] + [pid, wid]
                c.execute(f"UPDATE projects SET {sets} WHERE id = %s AND workspace_id = %s", vals)
                upd += 1
            else:
                cols_all = ["id","workspace_id","slug","name","created_at","archived","agent_type"]
                vals = [pid, wid, slug, p["name"], p.get("created_at") or now, p.get("archived") or 0, "hermes"]
                for k in ("description","icon","color","board_slug","primary_path"):
                    if p.get(k) is not None:
                        cols_all.append(k); vals.append(p[k])
                ph = ", ".join(["%s"] * len(vals))
                c.execute(f"INSERT INTO projects ({', '.join(cols_all)}) VALUES ({ph}) "
                          "ON CONFLICT (workspace_id, id) DO UPDATE SET "
                          + ", ".join(f"{k} = EXCLUDED.{k}" for k in cols_all if k not in ("id","workspace_id")),
                          vals)
                imp += 1
            # upsert folders incrementally: new paths are added, existing
            # ones updated (is_primary/label) — multi-device edits coexist
            # instead of last-writer-wins replacing the whole set.
            for f in p.get("folders", []):
                c.execute("""INSERT INTO project_folders
                             (workspace_id, project_id, path, label, is_primary, added_at)
                             VALUES (%s,%s,%s,%s,%s,%s)
                             ON CONFLICT (workspace_id, project_id, path) DO UPDATE SET
                               label = EXCLUDED.label,
                               is_primary = EXCLUDED.is_primary""",
                          (wid, pid, f.get("path"), f.get("label"),
                           f.get("is_primary") or 0, f.get("added_at") or now))
        conn.commit()
    return {"imported": imp, "updated": upd, "merged": merged}

@app.post("/api/projects/pull")
async def api_projects_pull(request: Request, ws: dict = Depends(get_workspace_by_api_key)):
    """Return visible projects + folders and pending remap records."""
    wid = ws["workspace_id"]
    with get_conn() as conn:
        c = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        c.execute("""SELECT * FROM projects
                     WHERE workspace_id = %s AND COALESCE(hidden,0) = 0
                     ORDER BY created_at DESC""", (wid,))
        projects = []
        for row in c.fetchall():
            p = dict(row)
            c.execute("""SELECT path, label, is_primary, added_at FROM project_folders
                         WHERE workspace_id = %s AND project_id = %s""", (wid, p["id"]))
            p["folders"] = [dict(r) for r in c.fetchall()]
            projects.append(p)
        c.execute("""SELECT old_id, new_id FROM project_remap
                     WHERE workspace_id = %s""", (wid,))
        remaps = [dict(r) for r in c.fetchall()]
    return {"projects": projects, "remaps": remaps}

@app.get("/status/{device_id}")
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

@app.get("/sessions")
async def list_sessions(ws: dict = Depends(get_workspace_by_api_key)):
    wid = ws["workspace_id"]
    with get_conn() as conn:
        c = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        c.execute("SELECT id, title, started_at, message_count, model, agent_type FROM sessions WHERE workspace_id=%s ORDER BY started_at DESC LIMIT 50", (wid,))
        return [dict(r) for r in c.fetchall()]

@app.get("/users")
async def list_users(ws: dict = Depends(get_workspace_by_api_key)):
    wid = ws["workspace_id"]
    with get_conn() as conn:
        c = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        c.execute("SELECT device_id, last_sync_at, sessions_synced, messages_synced FROM sync_state WHERE workspace_id = %s ORDER BY last_sync_at DESC", (wid,))
        return [dict(r) for r in c.fetchall()]

# ============================================================
# Startup
# ============================================================

if __name__ == "__main__":
    init_db()
    print(f"Backend: PostgreSQL (multi-tenant)")
    print(f"PG DSN: {PG_DSN.split('@')[1]}")
    print(f"Templates: {TEMPLATE_DIR}")
    print(f"Web UI: http://0.0.0.0:8765/web/")
    uvicorn.run(app, host="0.0.0.0", port=8765, log_level="info")
