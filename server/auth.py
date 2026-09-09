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
import emailverify
import mailer
import ratelimit

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from config import (JWT_SECRET, MASTER_API_KEY, PUBLIC_URL, TOKEN_EXPIRE_HOURS,
                    smtp_configured)
from db import get_conn
from render import render_page

router = APIRouter()

security = HTTPBearer(auto_error=False)

# Account-state machine constants.
# PENDING = new registration awaiting email verification (locked to the
# verify page until activated); everything else (legacy accounts + verified /
# admin-created ones) uses the system normally. Legacy accounts that predate
# the feature keep every existing capability; the ONLY gated action for an
# unverified account is creating a NEW workspace (existing workspaces, their
# API keys and sync are never touched).
STATE_PENDING = "PENDING_EMAIL_VERIFICATION"
STATE_ACTIVE = "ACTIVE"
AUTH_SOURCE_EMAIL = "EMAIL_REQUIRED"

# Pages reachable before the forced password change.
PW_ONLY_ALLOWED = {"/web/login", "/web/change-password", "/web/logout",
                   "/web/register", "/web/set-language",
                   "/web/forgot", "/web/reset"}
# Pages reachable while the account is waiting for email verification
# (waiting/confirm page + the security-email management page).
VERIFY_ALLOWED = PW_ONLY_ALLOWED | {"/web/verify-email", "/web/email"}

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

async def enforce_account_state(request: Request, call_next):
    """Gate /web/* pages on account state.

    Three layers, checked per request against the DB (single row read):
    1. PENDING_EMAIL_VERIFICATION (feature live only) → only the verify /
       email-management pages, otherwise back to /web/verify-email.
    2. must_change_password (legacy behavior) → change-password page.
    3. Otherwise pass through.
    """
    path = request.url.path
    if not path.startswith("/web/"):
        return await call_next(request)
    token = request.cookies.get("hsync_token")
    payload = verify_jwt(token) if token else None
    if payload:
        with get_conn() as conn:
            c = conn.cursor()
            c.execute("SELECT must_change_password, account_state FROM users "
                      "WHERE id = %s", (payload["sub"],))
            row = c.fetchone()
        if row:
            must_change, state = row
            pending = (smtp_configured() and state == STATE_PENDING
                       and path not in VERIFY_ALLOWED)
            if pending:
                return RedirectResponse(url="/web/verify-email", status_code=303)
            if must_change and path not in PW_ONLY_ALLOWED:
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


def audit_event(conn, event, user_id, detail="", workspace_id=None, code=None):
    """Append one audit_log row inside the caller's transaction.

    Mirrors sync.log_audit's column layout without importing sync (auth is
    imported by it). The row commits/rolls back with its transaction.
    """
    c = conn.cursor()
    c.execute(
        "INSERT INTO audit_log (ts, event, user_id, workspace_id, device_id, code, detail) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s)",
        (datetime.now().timestamp(), event, user_id, workspace_id, None,
         code, detail))


def audit_user_created(conn, user_id, workspace_id, code, ip):
    """Registration audit row (kept for the original call sites)."""
    audit_event(conn, "user_created", user_id, f"ip={ip}" if ip else "",
                workspace_id, code)


def _mail_link(request, path, raw_token):
    """Absolute URL for token links in mail. Uses PUBLIC_URL when configured
    (236 sets HERMES_SYNC_PUBLIC_URL), else the request's own base."""
    base = PUBLIC_URL or str(request.base_url).rstrip("/")
    return f"{base}{path}?token={raw_token}"


def verify_link(request, raw_token):
    """Email-verification link (activation / binding)."""
    return _mail_link(request, "/web/verify-email", raw_token)


def password_reset_link(request, raw_token):
    """Password-reset link."""
    return _mail_link(request, "/web/reset", raw_token)


def _safe_next(request, default):
    """Validate a client-supplied 'next' path (same-origin redirect only)."""
    nxt = request.query_params.get("next", "").strip()
    if nxt.startswith("/") and not nxt.startswith("//"):
        return nxt
    return default


def email_home_for(state):
    """Where email-management forms return after success: the pending page
    for accounts still awaiting verification, else the security hub."""
    return "/web/email" if state == STATE_PENDING else "/web/security"


def email_action_allowed(user_id):
    """Gate for creating a NEW workspace only.

    Feature off → always allowed. ACTIVE (verified, or admin-provisioned)
    accounts → allowed. Any unverified account (legacy or pending) → blocked
    until it binds and verifies an email. Existing workspaces, their API
    keys, session sync and data access are NEVER gated.
    """
    if not smtp_configured():
        return True
    with get_conn() as conn:
        c = conn.cursor()
        c.execute("SELECT account_state, email_verified_at FROM users "
                  "WHERE id = %s", (user_id,))
        row = c.fetchone()
    if not row:
        return False
    state, verified_at = row
    return state == STATE_ACTIVE or verified_at is not None


def _find_login_user(c, identifier):
    """Resolve a login identifier on an open cursor: username FIRST (keeps
    legacy '@'-containing usernames working), then — only when the mail
    feature is live — a VERIFIED email. Failures stay indistinguishable."""
    c.execute("SELECT * FROM users WHERE username = %s AND is_active = TRUE",
              (identifier,))
    row = c.fetchone()
    if not row and smtp_configured() and "@" in identifier:
        norm = emailverify.normalize_email(identifier)
        if norm:
            c.execute("SELECT * FROM users WHERE email_normalized = %s "
                      "AND email_verified_at IS NOT NULL AND is_active = TRUE",
                      (norm,))
            row = c.fetchone()
    return row


# ============================================================

def create_jwt(user_id, username, is_admin, display_name="", lang="zh-CN",
               account_state=STATE_ACTIVE):
    payload = {
        "sub": str(user_id),
        "username": username,
        "is_admin": is_admin,
        "display_name": display_name,
        "lang": lang,
        "account_state": account_state,
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
        user = _find_login_user(c, username)
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
        state = user.get("account_state") or STATE_ACTIVE
        token = create_jwt(user["id"], user["username"], user.get("is_admin", False),
                           user.get("display_name", ""), lang, account_state=state)
        if smtp_configured() and state == STATE_PENDING:
            target = "/web/verify-email"
        else:
            target = "/web/change-password?forced=1" if user.get("must_change_password") else "/web/"
        response = RedirectResponse(url=target, status_code=303)
        response.set_cookie(key="hsync_token", value=token, httponly=True, max_age=TOKEN_EXPIRE_HOURS * 3600, samesite="lax")
        return response

async def render_register_page(error, username="", display_name="", code="", email=""):
    """Register page with a fresh captcha challenge. Failed POSTs render this
    instead of redirecting so the user keeps what they typed (passwords are
    never echoed back)."""
    captcha_id, captcha_svg = captcha.new_challenge()
    return await render_page("register.html", {"error": error, "code": code,
                                               "username": username,
                                               "display_name": display_name,
                                               "email": email,
                                               "mail_enabled": smtp_configured(),
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
    email = body.get("email", "").strip()
    mail_on = smtp_configured()
    email_norm = emailverify.normalize_email(email) if (mail_on and email) else None
    ip = client_ip(request)
    # Per-IP gate first: cheapest anti-abuse check, no captcha/DB touch.
    if not ratelimit.allow("register", ip):
        return await render_register_page("register_rate_limited", username, display_name, code, email)
    # Math CAPTCHA gate next.
    if not captcha.verify(body.get("captcha_id", ""), body.get("captcha", "")):
        return await render_register_page("register_captcha_failed", username, display_name, code, email)
    if not username:
        return await render_register_page("register_username_required", username, display_name, code, email)
    if len(username) > USERNAME_MAX:
        return await render_register_page("register_username_too_long", username, display_name, code, email)
    if len(display_name) > DISPLAY_NAME_MAX:
        return await render_register_page("display_too_long", username, display_name, code, email)
    if len(password) < 6:
        return await render_register_page("pwd_short", username, display_name, code, email)
    if len(password) > PASSWORD_MAX:
        return await render_register_page("pwd_too_long", username, display_name, code, email)
    if password != confirm:
        return await render_register_page("pwd_mismatch", username, display_name, code, email)
    # Email is mandatory ONLY while the mail feature is live; otherwise the
    # field does not exist on the form and registration behaves as before.
    if mail_on:
        if not email:
            return await render_register_page("register_email_required", username, display_name, code, email)
        if not email_norm:
            return await render_register_page("register_email_invalid", username, display_name, code, email)
    now = datetime.now().timestamp()
    # Adopt the guest's landing-page language choice (mirrors login) so the
    # account preference and the auto-created workspace name match it.
    cookie_lang = request.cookies.get("lang")
    lang = cookie_lang if cookie_lang in ("zh-CN", "en") else "zh-CN"
    # Single transaction: lock the invite row FOR UPDATE, validate it, derive
    # the granted plan, then insert the user (pending-verify state when the
    # mail feature is live, otherwise active with an auto-created default
    # workspace) and consume the invite in one commit.
    error_key = None
    raw_token = None
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
        if not error_key and mail_on:
            c.execute("SELECT 1 FROM users WHERE email_normalized = %s "
                      "AND email_verified_at IS NOT NULL LIMIT 1", (email_norm,))
            if c.fetchone():
                error_key = "register_email_in_use"
                conn.rollback()
        if not error_key:
            try:
                c.execute("INSERT INTO users (username, password_hash, display_name, is_admin, "
                          "created_at, plan, lang, email, email_normalized, email_verified_at, "
                          "pending_email, pending_email_normalized, account_state, auth_source) "
                          "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
                          "RETURNING id",
                          (username, hash_password(password), display_name, False, now,
                           grant_plan, lang, None, None, None,
                           email if mail_on else None,
                           email_norm if mail_on else None,
                           STATE_PENDING if mail_on else STATE_ACTIVE,
                           AUTH_SOURCE_EMAIL if mail_on else "LEGACY_USERNAME"))
            except psycopg2.errors.UniqueViolation:
                error_key = "register_user_exists"
                conn.rollback()
            if not error_key:
                user_id = c.fetchone()[0]
                ws_id = None
                if not mail_on:
                    # Auto-create a default workspace for the new user (mail
                    # flow creates it at activation instead, so a pending
                    # account never holds an API key).
                    c.execute("INSERT INTO workspaces (name, user_id, api_key, description, created_at) "
                              "VALUES (%s, %s, %s, %s, %s) RETURNING id",
                              (_DEFAULT_WS_NAME[lang], user_id, generate_api_key(), "", now))
                    ws_id = c.fetchone()[0]
                if code:
                    c.execute("UPDATE invites SET used = 1, used_by = %s WHERE id = %s",
                              (user_id, inv[0]))
                audit_user_created(conn, user_id, ws_id, code, ip)
                if mail_on:
                    raw_token = emailverify.issue_token(
                        conn, user_id, emailverify.PURPOSE_VERIFY_EMAIL, email_norm, ip)
    if error_key:
        return await render_register_page(error_key, username, display_name, code, email)
    if mail_on:
        # Commit done; deliver the mail now. A delivery failure must not fail
        # the registration — the waiting page offers resend/change instead.
        sent = True
        try:
            mailer.send_verification_mail(email, verify_link(request, raw_token), lang)
        except mailer.MailerError:
            sent = False
        token = create_jwt(user_id, username, False, display_name, lang,
                           account_state=STATE_PENDING)
        query = "" if sent else "?mail_failed=1"
        response = RedirectResponse(url=f"/web/verify-email{query}", status_code=303)
        response.set_cookie(key="hsync_token", value=token, httponly=True,
                            max_age=TOKEN_EXPIRE_HOURS * 3600, samesite="lax")
        return response
    return RedirectResponse(url="/web/login?success=register_success", status_code=303)


# ============================================================
# Email verification (optional feature; endpoints are inert without SMTP)
# ============================================================
def _user_email_state(user_id):
    """Full email/state row for the account/verify pages."""
    with get_conn() as conn:
        c = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        c.execute("SELECT id, username, display_name, lang, is_admin, email, "
                  "pending_email, email_verified_at, account_state, auth_source "
                  "FROM users WHERE id = %s", (user_id,))
        return c.fetchone()


@router.get("/web/verify-email", response_class=HTMLResponse)
async def web_verify_email_page(request: Request, error: str = ""):
    """Waiting page (pending accounts), confirmation page (token present) or
    invalid/expired states. The token is validated but NOT consumed here —
    the confirm POST below does that, so mail scanners can't burn it."""
    if not smtp_configured():
        return RedirectResponse(url="/", status_code=303)
    raw = request.query_params.get("token", "")
    payload = verify_jwt(request.cookies.get("hsync_token")) if request.cookies.get("hsync_token") else None
    ctx = {"error": error,
           "mail_failed": request.query_params.get("mail_failed") == "1"}
    if raw:
        with get_conn() as conn:
            tok = emailverify.lookup_token(conn, raw)
            if not tok or tok["consumed_at"] is not None:
                ctx["mode"] = "invalid"
            elif tok["expires_at"] < time.time():
                ctx["mode"] = "expired"
            else:
                c = conn.cursor()
                c.execute("SELECT username, pending_email, lang FROM users "
                          "WHERE id = %s", (tok["user_id"],))
                u = c.fetchone()
                if not u:
                    ctx["mode"] = "invalid"
                else:
                    ctx.update({"mode": "confirm", "token": raw,
                                "username": u[0],
                                "email": emailverify.mask_email(u[1] or tok["email_normalized"])})
        return await render_page("verify_email.html", ctx)
    if payload:
        row = _user_email_state(payload["sub"])
        if row and row["account_state"] == STATE_PENDING:
            ctx.update({"mode": "waiting",
                        "email": emailverify.mask_email(row["pending_email"] or "")})
            return await render_page("verify_email.html", ctx)
        return RedirectResponse(url="/web/email", status_code=303)
    return RedirectResponse(url="/web/login", status_code=303)


@router.post("/web/verify-email", response_class=HTMLResponse)
async def web_verify_email_confirm(request: Request):
    """Consume the token and activate the account inside one transaction.

    On success the default workspace is created for accounts that registered
    through the new email flow (they had none), the token is consumed, the
    user is auto-logged-in as ACTIVE and redirected to the dashboard.
    """
    if not smtp_configured():
        return RedirectResponse(url="/", status_code=303)
    body = await request.form()
    raw = body.get("token", "").strip()
    success_user = None
    mode = None
    now = time.time()
    with get_conn() as conn:
        c = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        tok = emailverify.lookup_token(conn, raw)
        if not tok or tok["consumed_at"] is not None:
            mode = "invalid"
        elif tok["expires_at"] < now:
            mode = "expired"
        else:
            c.execute("SELECT id, username, display_name, is_admin, lang, "
                      "must_change_password, account_state, auth_source, email, "
                      "pending_email, pending_email_normalized "
                      "FROM users WHERE id = %s FOR UPDATE", (tok["user_id"],))
            u = c.fetchone()
            if not u or (u["pending_email_normalized"] or "") != tok["email_normalized"]:
                mode = "invalid"
                conn.rollback()
            elif emailverify.verified_email_taken(conn, tok["email_normalized"], u["id"]):
                mode = "occupied"
                conn.rollback()
            else:
                c.execute("UPDATE users SET email = pending_email, "
                          "email_normalized = pending_email_normalized, "
                          "email_verified_at = %s, pending_email = NULL, "
                          "pending_email_normalized = NULL, account_state = %s "
                          "WHERE id = %s", (now, STATE_ACTIVE, u["id"]))
                ws_id = None
                if u["auth_source"] == AUTH_SOURCE_EMAIL:
                    c.execute("SELECT 1 FROM workspaces WHERE user_id = %s LIMIT 1",
                              (u["id"],))
                    if not c.fetchone():
                        c.execute("INSERT INTO workspaces (name, user_id, api_key, description, created_at) "
                                  "VALUES (%s, %s, %s, %s, %s) RETURNING id",
                                  (_DEFAULT_WS_NAME[u["lang"] or "zh-CN"],
                                   u["id"], generate_api_key(), "", now))
                        ws_id = c.fetchone()[0]
                emailverify.consume_token(conn, tok["id"], now)
                audit_event(conn, "email_verified", u["id"],
                            f"email={tok['email_normalized']}", ws_id)
                success_user = {"id": u["id"], "username": u["username"],
                                "display_name": u["display_name"],
                                "is_admin": u["is_admin"], "lang": u["lang"] or "zh-CN",
                                "must_change_password": u["must_change_password"],
                                "old_email": u["email"] or None,
                                "new_email": u["pending_email"]}
    if success_user:
        # Email change (not first-time activation): notify both addresses
        # that the security email moved. Best-effort; failures never roll back.
        if (success_user["old_email"] and success_user["old_email"] != success_user["new_email"]):
            try:
                mailer.send_email_changed_notice(success_user["old_email"],
                                                 success_user["new_email"],
                                                 success_user["lang"])
            except mailer.MailerError:
                pass
        token = create_jwt(success_user["id"], success_user["username"],
                           success_user["is_admin"], success_user["display_name"],
                           success_user["lang"], account_state=STATE_ACTIVE)
        target = "/web/change-password?forced=1" if success_user["must_change_password"] else "/web/"
        response = RedirectResponse(url=target, status_code=303)
        response.set_cookie(key="hsync_token", value=token, httponly=True,
                            max_age=TOKEN_EXPIRE_HOURS * 3600, samesite="lax")
        return response
    return await render_page("verify_email.html", {"mode": mode, "token": ""})


@router.get("/web/email", response_class=HTMLResponse)
async def web_email_page(request: Request, error: str = ""):
    """Email-management deep link. Pending accounts get the standalone page
    (they are locked out of the rest); everyone else is routed to the
    security hub (same query parameters carried over)."""
    try:
        user = get_current_user(request)
    except Exception:
        return RedirectResponse(url="/web/login")
    if not smtp_configured():
        return RedirectResponse(url="/web/", status_code=303)
    row = _user_email_state(user["sub"])
    if not row:
        return RedirectResponse(url="/web/login")
    if row["account_state"] == STATE_PENDING:
        return await render_page("account_email.html", {
            "user": user,
            "row": row,
            "error": error or request.query_params.get("error", ""),
            "sent": request.query_params.get("sent") == "1",
            "mail_failed": request.query_params.get("mail_failed") == "1",
        })
    target = _safe_next(request, "/web/security")
    qs = []
    for key in ("error", "sent", "mail_failed"):
        if request.query_params.get(key):
            qs.append(f"{key}={request.query_params[key]}")
    sep = "&" if "?" in target else "?"
    return RedirectResponse(url=target + (sep + "&".join(qs) if qs else ""),
                            status_code=303)


@router.post("/web/email/bind", response_class=HTMLResponse)
async def web_email_bind(request: Request):
    """Set a pending email (bind for legacy accounts / change for verified
    ones) and mail a verification link. The previous verified email stays
    authoritative until the new one is verified."""
    try:
        user = get_current_user(request)
    except Exception:
        return RedirectResponse(url="/web/login")
    if not smtp_configured():
        return RedirectResponse(url="/web/", status_code=303)
    ip = client_ip(request)
    if not ratelimit.allow("email", ip):
        return RedirectResponse(url="/web/email?error=email_rate_limited", status_code=303)
    body = await request.form()
    email = body.get("email", "").strip()
    old_password = body.get("old_password", "")
    norm = emailverify.normalize_email(email)
    if not email:
        return RedirectResponse(url="/web/email?error=register_email_required", status_code=303)
    if not norm:
        return RedirectResponse(url="/web/email?error=register_email_invalid", status_code=303)
    with get_conn() as conn:
        c = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        c.execute("SELECT id, username, password_hash, lang, account_state, pending_email "
                  "FROM users WHERE id = %s FOR UPDATE", (user["sub"],))
        u = c.fetchone()
        if not u or not verify_password(old_password, u["password_hash"]):
            return RedirectResponse(url="/web/email?error=old_pwd_wrong", status_code=303)
        if emailverify.verified_email_taken(conn, norm, u["id"]):
            conn.rollback()
            return RedirectResponse(url="/web/email?error=email_taken", status_code=303)
        c.execute("UPDATE users SET pending_email = %s, pending_email_normalized = %s "
                  "WHERE id = %s", (email, norm, u["id"]))
        raw_token = emailverify.issue_token(
            conn, u["id"], emailverify.PURPOSE_VERIFY_EMAIL, norm, ip)
        audit_event(conn, "email_pending_set", u["id"], f"email={norm}")
        state = u["account_state"]
        lang = u["lang"] or "zh-CN"
    try:
        mailer.send_verification_mail(email, verify_link(request, raw_token), lang)
    except mailer.MailerError:
        return RedirectResponse(url=email_home_for(state) + "?sent=1&mail_failed=1",
                                 status_code=303)
    return RedirectResponse(url=email_home_for(state) + "?sent=1", status_code=303)


@router.post("/web/email/resend", response_class=HTMLResponse)
async def web_email_resend(request: Request):
    """Re-issue (revoking the previous live one) and resend the verification
    mail for the current pending email."""
    try:
        user = get_current_user(request)
    except Exception:
        return RedirectResponse(url="/web/login")
    if not smtp_configured():
        return RedirectResponse(url="/web/", status_code=303)
    ip = client_ip(request)
    if not ratelimit.allow("email", ip):
        return RedirectResponse(url="/web/email?error=email_rate_limited", status_code=303)
    with get_conn() as conn:
        c = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        c.execute("SELECT id, lang, account_state, pending_email, pending_email_normalized "
                  "FROM users WHERE id = %s FOR UPDATE", (user["sub"],))
        u = c.fetchone()
        if not u or not u["pending_email_normalized"]:
            return RedirectResponse(url="/web/email?error=email_none_pending", status_code=303)
        raw_token = emailverify.issue_token(
            conn, u["id"], emailverify.PURPOSE_VERIFY_EMAIL,
            u["pending_email_normalized"], ip)
        audit_event(conn, "email_verify_resent", u["id"],
                    f"email={u['pending_email_normalized']}")
        pending = u["pending_email"]
        lang = u["lang"] or "zh-CN"
        state = u["account_state"]
    try:
        mailer.send_verification_mail(pending, verify_link(request, raw_token), lang)
    except mailer.MailerError:
        return RedirectResponse(url=email_home_for(state) + "?sent=1&mail_failed=1",
                                 status_code=303)
    return RedirectResponse(url=email_home_for(state) + "?sent=1", status_code=303)


@router.get("/web/security", response_class=HTMLResponse)
async def web_security_page(request: Request):
    """Security hub: change password / reset password / change email entries,
    each opening a dialog. A 'd' query param auto-opens the dialog (allowed
    ids only: dlgPwd / dlgReset / dlgEmail)."""
    try:
        user = get_current_user(request)
    except Exception:
        return RedirectResponse(url="/web/login")
    if not smtp_configured():
        return RedirectResponse(url="/web/", status_code=303)
    row = _user_email_state(user["sub"])
    if not row:
        return RedirectResponse(url="/web/login")
    d = request.query_params.get("d", "")
    if d not in ("dlgPwd", "dlgReset", "dlgEmail"):
        d = ""
    return await render_page("security.html", {
        "user": user,
        "row": row,
        "open_dialog": d,
        "error": request.query_params.get("error", ""),
        "success": request.query_params.get("success", ""),
        "sent": request.query_params.get("sent") == "1",
        "reset_sent": request.query_params.get("reset_sent") == "1",
        "mail_failed": request.query_params.get("mail_failed") == "1",
    })


@router.post("/web/security/reset-request", response_class=HTMLResponse)
async def web_security_reset_request(request: Request):
    """Send a password-reset mail to the account's VERIFIED security email
    (self-service reset entry for signed-in users). Token/flow identical to
    the guest /web/forgot path: purpose reset_password, one-time, 30 min."""
    try:
        user = get_current_user(request)
    except Exception:
        return RedirectResponse(url="/web/login")
    if not smtp_configured():
        return RedirectResponse(url="/web/security", status_code=303)
    ip = client_ip(request)
    if not ratelimit.allow("email", ip):
        return RedirectResponse(url="/web/security?d=dlgReset&error=email_rate_limited",
                                status_code=303)
    with get_conn() as conn:
        c = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        c.execute("SELECT id, lang, email, email_normalized, email_verified_at "
                  "FROM users WHERE id = %s", (user["sub"],))
        u = c.fetchone()
        if not u or u["email_verified_at"] is None or not u["email"]:
            return RedirectResponse(url="/web/security?d=dlgReset&error=reset_no_verified_email",
                                    status_code=303)
        raw_token = emailverify.issue_token(
            conn, u["id"], emailverify.PURPOSE_RESET_PASSWORD,
            u["email_normalized"], ip)
        audit_event(conn, "password_reset_requested", u["id"],
                    "password reset requested (security hub)")
        to_email = u["email"]
        lang = u["lang"] or "zh-CN"
    try:
        mailer.send_password_reset_mail(to_email,
                                        password_reset_link(request, raw_token),
                                        lang)
        return RedirectResponse(url="/web/security?d=dlgReset&reset_sent=1",
                                status_code=303)
    except mailer.MailerError:
        return RedirectResponse(url="/web/security?d=dlgReset&mail_failed=1",
                                status_code=303)


@router.get("/web/forgot", response_class=HTMLResponse)
async def web_forgot_page(request: Request):
    """Request a password-reset mail (guest page)."""
    if not smtp_configured():
        return await render_page("forgot.html", {"mode": "unavailable"})
    return await render_page("forgot.html", {"mode": "form"})


@router.post("/web/forgot", response_class=HTMLResponse)
async def web_forgot_submit(request: Request):
    """Anti-enumeration: every submitted identifier gets the SAME response.
    A reset mail is sent only when the identifier resolves to an account with
    a VERIFIED email; everything else is a silent no-op."""
    if not smtp_configured():
        return await render_page("forgot.html", {"mode": "unavailable"})
    if not ratelimit.allow("forgot", client_ip(request)):
        return await render_page("forgot.html", {"mode": "rate"})
    body = await request.form()
    identifier = body.get("username", "").strip()
    ip = client_ip(request)
    if identifier:
        with get_conn() as conn:
            c = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            u = _find_login_user(c, identifier)
            if (u and u["email_verified_at"] is not None and u["email"]):
                raw_token = emailverify.issue_token(
                    conn, u["id"], emailverify.PURPOSE_RESET_PASSWORD,
                    u["email_normalized"], ip)
                audit_event(conn, "password_reset_requested", u["id"],
                            "password reset requested")
                to_email = u["email"]
                lang = u["lang"] or "zh-CN"
        if u and u["email_verified_at"] is not None and u["email"]:
            try:
                mailer.send_password_reset_mail(to_email,
                                                password_reset_link(request, raw_token),
                                                lang)
            except mailer.MailerError:
                pass  # uniform response regardless of delivery outcome
    return await render_page("forgot.html", {"mode": "done"})


@router.get("/web/reset", response_class=HTMLResponse)
async def web_reset_page(request: Request):
    """Password-reset form (token shown, NOT consumed — mail scanners can't
    burn it)."""
    if not smtp_configured():
        return RedirectResponse(url="/web/login", status_code=303)
    raw = request.query_params.get("token", "").strip()
    if not raw:
        return RedirectResponse(url="/web/login", status_code=303)
    with get_conn() as conn:
        tok = emailverify.lookup_token(conn, raw, emailverify.PURPOSE_RESET_PASSWORD)
        if not tok or tok["consumed_at"] is not None:
            mode = "invalid"
        elif tok["expires_at"] < time.time():
            mode = "expired"
        else:
            mode = "form"
    if mode != "form":
        return await render_page("reset.html", {"mode": mode, "token": ""})
    return await render_page("reset.html", {"mode": "form", "token": raw})


@router.post("/web/reset", response_class=HTMLResponse)
async def web_reset_submit(request: Request):
    """Consume the reset token, verify the account still holds that verified
    email, and set the new password inside one transaction."""
    if not smtp_configured():
        return RedirectResponse(url="/web/login", status_code=303)
    body = await request.form()
    raw = body.get("token", "").strip()
    new_pw = body.get("new_password", "")
    confirm = body.get("confirm_password", "")
    if len(new_pw) < 6:
        return await render_page("reset.html",
                                 {"mode": "form", "token": raw, "error": "pwd_short"})
    if len(new_pw) > PASSWORD_MAX:
        return await render_page("reset.html",
                                 {"mode": "form", "token": raw, "error": "pwd_too_long"})
    if new_pw != confirm:
        return await render_page("reset.html",
                                 {"mode": "form", "token": raw, "error": "pwd_mismatch"})
    mode = None
    with get_conn() as conn:
        c = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        tok = emailverify.lookup_token(conn, raw, emailverify.PURPOSE_RESET_PASSWORD)
        if not tok or tok["consumed_at"] is not None:
            mode = "invalid"
        elif tok["expires_at"] < time.time():
            mode = "expired"
        else:
            c.execute("SELECT id, lang, email, email_normalized, email_verified_at "
                      "FROM users WHERE id = %s FOR UPDATE", (tok["user_id"],))
            u = c.fetchone()
            if (not u or u["email_verified_at"] is None
                    or u["email_normalized"] != tok["email_normalized"]):
                mode = "invalid"
                conn.rollback()
            else:
                c.execute("UPDATE users SET password_hash = %s, must_change_password = 0 "
                          "WHERE id = %s", (hash_password(new_pw), u["id"]))
                emailverify.consume_token(conn, tok["id"], time.time())
                audit_event(conn, "password_reset", u["id"], "password reset")
                mode = "done"
                lang = u["lang"] or "zh-CN"
    if mode == "done":
        return await render_page("reset.html", {"mode": "done", "token": ""})
    return await render_page("reset.html", {"mode": mode, "token": ""})


@router.get("/web/change-password", response_class=HTMLResponse)
async def web_change_password_page(request: Request):
    try:
        user = get_current_user(request)
    except:
        return RedirectResponse(url="/web/login")
    # The forced-change flow uses this standalone page; voluntary visits are
    # funneled into the security hub (single entry point).
    if request.query_params.get("forced") != "1":
        return RedirectResponse(url="/web/security", status_code=303)
    return await render_page("change_password.html", {"user": user,
                                           "forced": True})

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
    nxt = request.query_params.get("next", "").strip()
    local_next = (nxt.startswith("/") and not nxt.startswith("//")
                  and nxt != "/web/change-password")
    if new_pw != confirm:
        if local_next:
            return RedirectResponse(url=f"{nxt}?error=pwd_mismatch", status_code=303)
        return RedirectResponse(url="/web/change-password?forced=1&error=pwd_mismatch", status_code=303)
    if len(new_pw) < 6:
        if local_next:
            return RedirectResponse(url=f"{nxt}?error=pwd_short", status_code=303)
        return RedirectResponse(url="/web/change-password?forced=1&error=pwd_short", status_code=303)
    if len(new_pw) > PASSWORD_MAX:
        if local_next:
            return RedirectResponse(url=f"{nxt}?error=pwd_too_long", status_code=303)
        return RedirectResponse(url="/web/change-password?forced=1&error=pwd_too_long", status_code=303)
    with get_conn() as conn:
        c = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        c.execute("SELECT password_hash FROM users WHERE id = %s", (user["sub"],))
        u = c.fetchone()
        if not u or not verify_password(old_pw, u["password_hash"]):
            if local_next:
                return RedirectResponse(url=f"{nxt}?error=old_pwd_wrong", status_code=303)
            return RedirectResponse(url="/web/change-password?forced=1&error=old_pwd_wrong", status_code=303)
        c.execute("UPDATE users SET password_hash = %s, must_change_password = 0 WHERE id = %s",
                  (hash_password(new_pw), user["sub"]))
    if local_next:
        return RedirectResponse(url=f"{nxt}?success=pwd_changed", status_code=303)
    return RedirectResponse(url="/web/?success=pwd_changed", status_code=303)

@router.post("/web/update-profile", response_class=HTMLResponse)
async def web_update_profile(request: Request):
    """Update own display name only. Password and email changes moved to the
    security hub (/web/security dialogs); admin role is not touched here."""
    try:
        user = get_current_user(request)
    except:
        return RedirectResponse(url="/web/login")
    body = await request.form()
    display_name = body.get("display_name", "").strip()
    if len(display_name) > DISPLAY_NAME_MAX:
        return RedirectResponse(url="/web/?error=display_too_long", status_code=303)
    with get_conn() as conn:
        c = conn.cursor()
        c.execute("UPDATE users SET display_name = %s WHERE id = %s",
                  (display_name, user["sub"]))
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
                               payload.get("is_admin", False), payload.get("display_name", ""),
                               lang, account_state=payload.get("account_state", STATE_ACTIVE))
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
            c.execute("INSERT INTO users (username, password_hash, display_name, is_admin, created_at, "
                      "lang, email, email_normalized, email_verified_at, pending_email, "
                      "pending_email_normalized, account_state, auth_source) "
                      "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
                      (username, hash_password(password), display_name, is_admin, now, lang,
                       None, None, None, None, None, STATE_ACTIVE, "LEGACY_USERNAME"))
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
        user = _find_login_user(c, username)
        if not user or not verify_password(password, user["password_hash"]):
            raise HTTPException(status_code=401, detail="login_invalid")
        now = datetime.now().timestamp()
        if password_needs_upgrade(user["password_hash"]):
            c.execute("UPDATE users SET password_hash = %s WHERE id = %s",
                      (hash_password(password), user["id"]))
        c.execute("UPDATE users SET last_login_at = %s WHERE id = %s", (now, user["id"]))
        state = user.get("account_state") or STATE_ACTIVE
        token = create_jwt(user["id"], user["username"], user.get("is_admin", False),
                           lang=user.get("lang", "zh-CN"), account_state=state)
    return {"token": token, "username": username, "display_name": user.get("display_name"),
            "is_admin": user.get("is_admin"), "must_change_password": bool(user.get("must_change_password")),
            "account_state": state}

