"""Server configuration: environment variables and derived constants."""
import os
import secrets
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

# ---- Email verification (OPTIONAL). The whole feature is dormant until a
# working SMTP channel is configured; without it registrations and logins
# behave exactly as before (self-hosted deployments must not deadlock).
# Credentials live in the environment, never in the repo. mainland-friendly
# providers: Aliyun DirectMail / QQ / 163 (needs a verified sender).
SMTP_HOST = os.environ.get("HERMES_SYNC_SMTP_HOST", "").strip()
SMTP_PORT = int(os.environ.get("HERMES_SYNC_SMTP_PORT", "465") or 465)
SMTP_USER = os.environ.get("HERMES_SYNC_SMTP_USER", "").strip()
SMTP_PASSWORD = os.environ.get("HERMES_SYNC_SMTP_PASSWORD", "")
SMTP_FROM = os.environ.get("HERMES_SYNC_SMTP_FROM", "").strip()


def smtp_configured() -> bool:
    """Feature switch: email verification is live only when a mail channel
    (host + credentials + sender) is configured."""
    return bool(SMTP_HOST and SMTP_USER and SMTP_PASSWORD and SMTP_FROM)


def _client_default_server(server_url: str) -> str:
    """SYNC_SERVER default shipped to clients: the configured public URL
    when set, otherwise the address the current request arrived on."""
    return PUBLIC_URL or server_url

_MISSING = [k for k, v in (("HERMES_SYNC_PG_DSN", PG_DSN),
                            ("HERMES_SYNC_MASTER_KEY", MASTER_API_KEY)) if not v]
if _MISSING:
    raise SystemExit(f"Missing required environment variable(s): {', '.join(_MISSING)}. "
                     f"See server/.env.example for the full list.")
