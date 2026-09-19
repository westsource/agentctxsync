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

# ---- Announcements (OPTIONAL, off by default). A public JSON feed (see
# agentctxsync_seo/announcements/) that the in-app banner shows to logged-in
# users; empty string = feature off. The BROWSER fetches it, never the server:
# a self-hosted box must not call out to the vendor, and that is also why this
# is opt-in rather than a default URL. Point it at whichever site you trust
# to author the messages (usually your own public site).
ANNOUNCEMENTS_URL = os.environ.get("HERMES_SYNC_ANNOUNCEMENTS_URL", "").strip()
if ANNOUNCEMENTS_URL and not ANNOUNCEMENTS_URL.startswith(("http://", "https://")):
    raise SystemExit("HERMES_SYNC_ANNOUNCEMENTS_URL must be an absolute "
                     f"http(s) URL, got {ANNOUNCEMENTS_URL!r}")

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
# Outbound-mail ceiling per calendar day, counted in the DB (mail_stats) so a
# restart cannot refill it. The sender is usually a personal mailbox with a
# provider-side daily quota: without a ceiling, one abuse burst of reset
# requests silently exhausts that quota and activation/reset mail stops
# reaching anybody. 0 disables the cap (not recommended).
MAIL_DAILY_CAP = int(os.environ.get("HERMES_SYNC_MAIL_DAILY_CAP", "200") or 0)


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
