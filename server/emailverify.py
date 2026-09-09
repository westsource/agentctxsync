"""Email verification domain: normalization, masking, one-time tokens.

Pure logic + cursor/connection primitives (no FastAPI, no config import), so
the auth routes can compose it inside their own transactions and tests can
drive it with fake cursors — same pattern as the rest of the server.

Token model (OWASP): the raw token is a CSPRNG value sent to the user; only
its SHA-256 digest is stored. Tokens are single-use (consumed on success),
expire after TOKEN_TTL seconds, and issuing a new one revokes previous live
tokens of the same (user, purpose). Verified emails are unique at the DB
level (partial unique index on email_normalized); the transactional verify
re-checks the uniqueness before committing so two accounts racing on the
same pending email cannot both win.
"""
import hashlib
import re
import secrets
import time

TOKEN_TTL = 30 * 60          # verification links valid 30 minutes
PURPOSE_VERIFY_EMAIL = "verify_email"
PURPOSE_RESET_PASSWORD = "reset_password"
EMAIL_MAX = 254

# Loose structural check only (OWASP): real ownership is proven by the mail
# round-trip, not by regex.
_EMAIL_RE = re.compile(r"^[^@\s<>]{1,64}@[^@\s<>]{1,190}$")


def normalize_email(value):
    """Canonical form for uniqueness/lookup: trimmed, lowercase. Returns None
    when the value is not a plausible email shape."""
    if value is None:
        return None
    s = str(value).strip()
    if len(s) > EMAIL_MAX or not _EMAIL_RE.match(s):
        return None
    return s.lower()


def mask_email(value):
    """a***@example.com style mask for display; plain value when too short."""
    if not value:
        return ""
    local, _, domain = str(value).partition("@")
    if not domain or len(local) <= 1:
        return str(value)
    return f"{local[0]}***@{domain}"


def new_token():
    """CSPRNG raw token sent to the user (Base64URL, >32 bytes entropy)."""
    return secrets.token_urlsafe(32)


def token_digest(raw):
    """SHA-256 digest stored server-side; the raw value never touches the DB."""
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def revoke_live_tokens(conn, user_id, purpose, now=None):
    """Mark every live (unconsumed) token of (user, purpose) as consumed —
    issued when a new token replaces the old one."""
    now = time.time() if now is None else now
    c = conn.cursor()
    c.execute("UPDATE user_verification_tokens SET consumed_at = %s "
              "WHERE user_id = %s AND purpose = %s AND consumed_at IS NULL",
              (now, user_id, purpose))


def issue_token(conn, user_id, purpose, email_normalized, ip="", now=None):
    """Create a fresh token for (user, purpose), revoking previous live ones.
    Returns the raw token to be emailed (the caller mails it; the DB keeps
    only the digest)."""
    now = time.time() if now is None else now
    raw = new_token()
    revoke_live_tokens(conn, user_id, purpose, now)
    c = conn.cursor()
    c.execute(
        "INSERT INTO user_verification_tokens "
        "(user_id, purpose, token_hash, email_normalized, expires_at, requested_ip, created_at) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s)",
        (user_id, purpose, token_digest(raw), email_normalized,
         now + TOKEN_TTL, ip or "", now))
    return raw


def lookup_token(conn, raw, purpose=PURPOSE_VERIFY_EMAIL):
    """Fetch a live unconsumed token row for the digest, or None."""
    c = conn.cursor()
    c.execute(
        "SELECT id, user_id, purpose, email_normalized, expires_at, consumed_at "
        "FROM user_verification_tokens WHERE token_hash = %s AND purpose = %s",
        (token_digest(raw), purpose))
    row = c.fetchone()
    if not row:
        return None
    return {"id": row[0], "user_id": row[1], "purpose": row[2],
            "email_normalized": row[3], "expires_at": row[4],
            "consumed_at": row[5]}


def consume_token(conn, token_id, now=None):
    now = time.time() if now is None else now
    c = conn.cursor()
    c.execute("UPDATE user_verification_tokens SET consumed_at = %s "
              "WHERE id = %s AND consumed_at IS NULL", (now, token_id))
    return c.rowcount == 1


def verified_email_taken(conn, email_normalized, exclude_user_id=None):
    """True when another (or, without exclude, any) user already holds the
    email as verified."""
    c = conn.cursor()
    if exclude_user_id is None:
        c.execute("SELECT 1 FROM users WHERE email_normalized = %s "
                  "AND email_verified_at IS NOT NULL LIMIT 1", (email_normalized,))
    else:
        c.execute("SELECT 1 FROM users WHERE email_normalized = %s "
                  "AND email_verified_at IS NOT NULL AND id <> %s LIMIT 1",
                  (email_normalized, exclude_user_id))
    return c.fetchone() is not None
