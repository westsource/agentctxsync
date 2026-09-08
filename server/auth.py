"""Authentication domain: credentials, JWT, dependencies, login/register routes."""
import base64
import hashlib
import hmac
import json
import secrets
import time
from datetime import datetime

import psycopg2.extras

import captcha
import ratelimit

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from config import JWT_SECRET, MASTER_API_KEY, TOKEN_EXPIRE_HOURS
from db import get_conn
from render import render_page

router = APIRouter()

security = HTTPBearer(auto_error=False)

FORCED_PW_ALLOWLIST = {"/web/login", "/web/change-password", "/web/logout", "/web/register", "/web/set-language"}

# Self-service field limits. Enforced on write (registration / profile); the
# JWT carries username + display_name, so unbounded values would bloat the
# session cookie past browser limits and unbounded passwords would let one
# request pin a core with PBKDF2 over megabytes of input.
USERNAME_MAX = 32
DISPLAY_NAME_MAX = 64
PASSWORD_MAX = 128
# PBKDF2-HMAC-SHA256 iterations for NEW hashes (OWASP 2023: 600k). Each stored
# hash carries its own count (verify_password parses it), so legacy 100k
# hashes still verify and are upgraded lazily on next successful login.
PBKDF2_ITERATIONS = 600_000
_DEFAULT_WS_NAME = {"zh-CN": "默认工作空间", "en": "Default"}

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
def hash_password(password):
    salt = secrets.token_hex(16)
    h = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), PBKDF2_ITERATIONS)
    return f"pbkdf2:sha256:{PBKDF2_ITERATIONS}:{salt}:{h.hex()}"


def password_needs_upgrade(stored):
    """True when a stored hash uses fewer iterations than the current policy.

    Old hashes still verify (each hash carries its own iteration count); they
    are rehashed lazily on the next successful login.
    """
    parts = stored.split(":")
    try:
        return len(parts) == 5 and parts[0] == "pbkdf2" and int(parts[2]) < PBKDF2_ITERATIONS
    except ValueError:
        return False


def verify_password(password, stored):
    parts = stored.split(":")
    if len(parts) != 5 or parts[0] != "pbkdf2":
        return False
    _, algo, iterations, salt, stored_hash = parts
    h = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), int(iterations))
    return hmac.compare_digest(h.hex(), stored_hash)


def generate_api_key():
    return "ws_" + secrets.token_hex(24)

def client_ip(request):
    """Client IP for rate limiting. Requires uvicorn proxy headers behind a
    reverse proxy (see ratelimit.py) so this is the real client, not nginx."""
    return request.client.host if request.client else ""


def resolve_grant_plan(value):
    """Normalize a stored invite grant_plan. Unknown/bad values fall back to
    'unlimited' so registration never fails on a bad plan value; the operator
    fixes it afterwards."""
    return value if value in ("free", "unlimited") else "unlimited"


def audit_user_created(conn, user_id, workspace_id, code, ip):
    """Append a user_created audit row inside the caller's transaction.

    Mirrors sync.log_audit's column layout without importing sync (auth is
    imported by it). The row commits/rolls back with the registration.
    """
    c = conn.cursor()
    c.execute(
        "INSERT INTO audit_log (ts, event, user_id, workspace_id, device_id, code, detail) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s)",
        (datetime.now().timestamp(), "user_created", user_id, workspace_id, None,
         code or None, f"ip={ip}" if ip else ""))


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
@router.get("/")
async def root(request: Request):
    """Root: not logged in → landing page; logged in → dashboard."""
    try:
        get_current_user(request)
    except Exception:
        return await render_page("landing.html")
    return RedirectResponse(url="/web/")

@router.get("/web/login", response_class=HTMLResponse)
async def web_login(request: Request, error: str = ""):
    return await render_page("login.html", {"error": error})

@router.post("/web/login", response_class=HTMLResponse)
async def web_login_post(request: Request):
    body = await request.form()
    username = body.get("username", "")
    password = body.get("password", "")
    if not ratelimit.allow("login", client_ip(request)):
        return await render_page("login.html", {"error": "login_rate_limited"})
    with get_conn() as conn:
        c = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        c.execute("SELECT * FROM users WHERE username = %s AND is_active = TRUE", (username,))
        user = c.fetchone()
        if not user or not verify_password(password, user["password_hash"]):
            return await render_page("login.html", {"error": "login_invalid"})
        now = datetime.now().timestamp()
        if password_needs_upgrade(user["password_hash"]):
            c.execute("UPDATE users SET password_hash = %s WHERE id = %s",
                      (hash_password(password), user["id"]))
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

async def render_register_page(error, username="", display_name="", code=""):
    """Register page with a fresh captcha challenge. Failed POSTs render this
    instead of redirecting so the user keeps what they typed (passwords are
    never echoed back)."""
    captcha_id, captcha_svg = captcha.new_challenge()
    return await render_page("register.html", {"error": error, "code": code,
                                               "username": username,
                                               "display_name": display_name,
                                               "captcha_id": captcha_id,
                                               "captcha_svg": captcha_svg})


@router.get("/web/register", response_class=HTMLResponse)
async def web_register_page(request: Request, error: str = ""):
    # Pre-fill the invite code from a shared registration link (?code=...)
    code = request.query_params.get("code", "")
    return await render_register_page(error, code=code)


@router.get("/web/captcha/new")
async def web_captcha_new(request: Request):
    """Fresh challenge for the register form's refresh button."""
    if not ratelimit.allow("captcha", client_ip(request)):
        return JSONResponse({"id": "", "svg": ""}, status_code=429)
    captcha_id, captcha_svg = captcha.new_challenge()
    return {"id": captcha_id, "svg": captcha_svg}


@router.post("/web/register", response_class=HTMLResponse)
async def web_register_submit(request: Request):
    body = await request.form()
    username = body.get("username", "").strip()
    display_name = body.get("display_name", "").strip() or username
    password = body.get("password", "")
    confirm = body.get("confirm_password", "")
    code = body.get("invite_code", "").strip().upper()
    ip = client_ip(request)
    # Per-IP gate first: cheapest anti-abuse check, no captcha/DB touch.
    if not ratelimit.allow("register", ip):
        return await render_register_page("register_rate_limited", username, display_name, code)
    # Math CAPTCHA gate next.
    if not captcha.verify(body.get("captcha_id", ""), body.get("captcha", "")):
        return await render_register_page("register_captcha_failed", username, display_name, code)
    if not username:
        return await render_register_page("register_username_required", username, display_name, code)
    if len(username) > USERNAME_MAX:
        return await render_register_page("register_username_too_long", username, display_name, code)
    if len(display_name) > DISPLAY_NAME_MAX:
        return await render_register_page("display_too_long", username, display_name, code)
    if len(password) < 6:
        return await render_register_page("pwd_short", username, display_name, code)
    if len(password) > PASSWORD_MAX:
        return await render_register_page("pwd_too_long", username, display_name, code)
    if password != confirm:
        return await render_register_page("pwd_mismatch", username, display_name, code)
    now = datetime.now().timestamp()
    # Adopt the guest's landing-page language choice (mirrors login) so the
    # account preference and the auto-created workspace name match it.
    cookie_lang = request.cookies.get("lang")
    lang = cookie_lang if cookie_lang in ("zh-CN", "en") else "zh-CN"
    # Single transaction: lock the invite row FOR UPDATE, validate it, derive
    # the granted plan, then insert user + default workspace and consume the
    # invite in one commit. A failed/raced invite never leaves a user behind,
    # so the old create-then-compensating-delete dance is gone.
    error_key = None
    with get_conn() as conn:
        c = conn.cursor()
        grant_plan = "free"
        if code:
            c.execute("SELECT id, used, revoked, expires_at, grant_plan FROM invites "
                      "WHERE code = %s FOR UPDATE", (code,))
            inv = c.fetchone()
            if not inv or inv[2]:
                error_key = "register_invalid_code"
            elif inv[1]:
                error_key = "register_used_code"
            elif inv[3] and inv[3] < now:
                error_key = "register_expired_code"
            else:
                grant_plan = resolve_grant_plan(inv[4])
            if error_key:
                conn.rollback()
        if not error_key:
            try:
                c.execute("INSERT INTO users (username, password_hash, display_name, is_admin, "
                          "created_at, plan, lang) VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id",
                          (username, hash_password(password), display_name, False, now, grant_plan, lang))
            except psycopg2.errors.UniqueViolation:
                error_key = "register_user_exists"
                conn.rollback()
            if not error_key:
                user_id = c.fetchone()[0]
                # Auto-create a default workspace for the new user.
                c.execute("INSERT INTO workspaces (name, user_id, api_key, description, created_at) "
                          "VALUES (%s, %s, %s, %s, %s) RETURNING id",
                          (_DEFAULT_WS_NAME[lang], user_id, generate_api_key(), "", now))
                ws_id = c.fetchone()[0]
                if code:
                    c.execute("UPDATE invites SET used = 1, used_by = %s WHERE id = %s",
                              (user_id, inv[0]))
                audit_user_created(conn, user_id, ws_id, code, ip)
    if error_key:
        return await render_register_page(error_key, username, display_name, code)
    return RedirectResponse(url="/web/login?success=register_success", status_code=303)

@router.get("/web/change-password", response_class=HTMLResponse)
async def web_change_password_page(request: Request):
    try:
        user = get_current_user(request)
    except:
        return RedirectResponse(url="/web/login")
    return await render_page("change_password.html", {"user": user,
                                           "forced": request.query_params.get("forced") == "1"})

@router.post("/web/change-password", response_class=HTMLResponse)
async def web_change_password(request: Request):
    try:
        user = get_current_user(request)
    except:
        return RedirectResponse(url="/web/login")
    body = await request.form()
    old_pw = body.get("old_password", "")
    new_pw = body.get("new_password", "")
    confirm = body.get("confirm_password", "")
    if new_pw != confirm:
        return RedirectResponse(url="/web/change-password?forced=1&error=pwd_mismatch", status_code=303)
    if len(new_pw) < 6:
        return RedirectResponse(url="/web/change-password?forced=1&error=pwd_short", status_code=303)
    if len(new_pw) > PASSWORD_MAX:
        return RedirectResponse(url="/web/change-password?forced=1&error=pwd_too_long", status_code=303)
    with get_conn() as conn:
        c = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        c.execute("SELECT password_hash FROM users WHERE id = %s", (user["sub"],))
        u = c.fetchone()
        if not u or not verify_password(old_pw, u["password_hash"]):
            return RedirectResponse(url="/web/change-password?forced=1&error=old_pwd_wrong", status_code=303)
        c.execute("UPDATE users SET password_hash = %s, must_change_password = 0 WHERE id = %s", (hash_password(new_pw), user["sub"]))
    return RedirectResponse(url="/web/?success=pwd_changed", status_code=303)

@router.post("/web/update-profile", response_class=HTMLResponse)
async def web_update_profile(request: Request):
    """Update own profile: display name, optional password, admin flag.
    Mirrors the admin edit-user behavior; main admin is protected, and
    non-admin users can never grant themselves the admin role.
    Changing the password requires verifying the current one."""
    try:
        user = get_current_user(request)
    except:
        return RedirectResponse(url="/web/login")
    body = await request.form()
    display_name = body.get("display_name", "").strip()
    new_password = body.get("new_password", "")  # do not strip, keep parity with change-password
    old_password = body.get("old_password", "")
    is_admin = body.get("is_admin") == "true"
    uid = user["sub"]
    if len(display_name) > DISPLAY_NAME_MAX:
        return RedirectResponse(url="/web/?error=display_too_long", status_code=303)
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
            if len(new_password) > PASSWORD_MAX:
                return RedirectResponse(url="/web/?error=pwd_too_long", status_code=303)
            if not verify_password(old_password, password_hash):
                return RedirectResponse(url="/web/?error=old_pwd_wrong", status_code=303)
            c.execute("UPDATE users SET display_name = %s, password_hash = %s, is_admin = %s, must_change_password = 0 WHERE id = %s",
                      (display_name, hash_password(new_password), is_admin, uid))
        else:
            c.execute("UPDATE users SET display_name = %s, is_admin = %s WHERE id = %s",
                      (display_name, is_admin, uid))
    return RedirectResponse(url="/web/?success=profile_updated", status_code=303)

@router.get("/web/set-language/{lang}")
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
@router.get("/web/logout")
async def web_logout():
    response = RedirectResponse(url="/", status_code=303)
    response.delete_cookie("hsync_token")
    return response

@router.post("/api/auth/register")
async def api_register(request: Request, user: dict = Depends(require_admin)):
    if not ratelimit.allow("register", client_ip(request)):
        raise HTTPException(status_code=429, detail="Too many registrations from this IP")
    body = await request.json()
    username = body.get("username", "").strip()
    password = body.get("password", "")
    display_name = str(body.get("display_name", username)).strip() or username
    is_admin = body.get("is_admin", False)
    lang = body.get("lang", "zh-CN")
    if lang not in ("zh-CN", "en"):
        lang = "zh-CN"
    if not username:
        raise HTTPException(status_code=400, detail="Username is required")
    if len(username) > USERNAME_MAX:
        raise HTTPException(status_code=400, detail=f"Username must be at most {USERNAME_MAX} characters")
    if len(display_name) > DISPLAY_NAME_MAX:
        raise HTTPException(status_code=400, detail=f"Display name must be at most {DISPLAY_NAME_MAX} characters")
    if len(password) < 6:
        raise HTTPException(status_code=400, detail="Password must be at least 6 characters")
    if len(password) > PASSWORD_MAX:
        raise HTTPException(status_code=400, detail=f"Password must be at most {PASSWORD_MAX} characters")
    now = datetime.now().timestamp()
    with get_conn() as conn:
        c = conn.cursor()
        try:
            c.execute("INSERT INTO users (username, password_hash, display_name, is_admin, created_at, lang) "
                      "VALUES (%s, %s, %s, %s, %s, %s) RETURNING id",
                      (username, hash_password(password), display_name, is_admin, now, lang))
        except psycopg2.errors.UniqueViolation:
            raise HTTPException(status_code=409, detail="Username already exists")
        uid = c.fetchone()[0]
        # Same default workspace as the web path so admin-created and
        # self-registered accounts start in an equivalent state.
        c.execute("INSERT INTO workspaces (name, user_id, api_key, description, created_at) "
                  "VALUES (%s, %s, %s, %s, %s) RETURNING id",
                  (_DEFAULT_WS_NAME[lang], uid, generate_api_key(), "", now))
        ws_id = c.fetchone()[0]
        audit_user_created(conn, uid, ws_id, None, client_ip(request))
    return {"success": True}

@router.post("/api/auth/login")
async def api_login(request: Request):
    body = await request.json()
    username = body.get("username", "")
    password = body.get("password", "")
    if not ratelimit.allow("login", client_ip(request)):
        raise HTTPException(status_code=429, detail="Too many login attempts from this IP")
    with get_conn() as conn:
        c = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        c.execute("SELECT * FROM users WHERE username = %s AND is_active = TRUE", (username,))
        user = c.fetchone()
        if not user or not verify_password(password, user["password_hash"]):
            raise HTTPException(status_code=401, detail="login_invalid")
        now = datetime.now().timestamp()
        if password_needs_upgrade(user["password_hash"]):
            c.execute("UPDATE users SET password_hash = %s WHERE id = %s",
                      (hash_password(password), user["id"]))
        c.execute("UPDATE users SET last_login_at = %s WHERE id = %s", (now, user["id"]))
        token = create_jwt(user["id"], user["username"], user.get("is_admin", False), lang=user.get("lang", "zh-CN"))
    return {"token": token, "username": username, "display_name": user.get("display_name"), "is_admin": user.get("is_admin"), "must_change_password": bool(user.get("must_change_password"))}

