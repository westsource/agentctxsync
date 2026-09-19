"""Per-key request rate limiting (in-process, single-worker).

Same deployment constraint as captcha.py: counters live in this process, so
the server must run single-process (main.py runs plain uvicorn.run; the
captcha module documents the same invariant). Most scopes are keyed by
request.client.host; deployments behind a reverse proxy MUST run uvicorn
with proxy headers enabled (main.py already passes proxy_headers=True, which
trusts X-Forwarded-For only from 127.0.0.1 by default) so client.host is the
real client IP — otherwise every user shares the proxy IP and the limits
cannot distinguish them (and blindly trusting an unvalidated X-Forwarded-For
would let a direct attacker rotate headers to bypass the limit).

IP buckets bound one *sender*. The outbound-mail scopes bound the *recipient*
side instead (mail_recipient / mail_account / mail_address), because a sender
can multiply IPs for free while the mailbox being spammed cannot: without
them, 5 reset mails / 10 min / IP is 720 per mailbox per day per IP, and
`mailer`'s daily budget is the only global ceiling.

Buckets are fixed windows: (window_start, count). A scope may carry several
windows (e.g. 1/minute AND 5/day) and every one of them must have budget. A
bucket whose window elapsed resets on next use. When the table is full of live
entries the request is denied (fail closed) after evicting everything expired:
the earlier behaviour — clearing the whole table — let any caller churning
distinct keys hand itself (and every other key) a fresh budget.
"""
import threading
import time

# (max events, window seconds) per scope. Tune here; generous defaults so a
# NATed office (many legit users behind one IP) is not locked out while
# scripted bulk registration / brute force is throttled.
REGISTER_LIMIT, REGISTER_WINDOW = 10, 600    # open registration per IP / 10 min
LOGIN_LIMIT, LOGIN_WINDOW = 30, 600          # login attempts per IP / 10 min
CAPTCHA_LIMIT, CAPTCHA_WINDOW = 30, 300      # fresh captcha challenges per IP / 5 min
EMAIL_LIMIT, EMAIL_WINDOW = 5, 600           # bind/resend verification mail per IP / 10 min
FORGOT_LIMIT, FORGOT_WINDOW = 5, 600         # password-reset requests per IP / 10 min

# Outbound-mail caps, keyed by what the mail is addressed to rather than by
# who asks. Tuned so a shared mailbox (family/team address) is never starved
# while a single destination cannot be flooded.
MAIL_RECIPIENT_LIMIT_MIN, MAIL_RECIPIENT_WINDOW_MIN = 1, 60     # per recipient / min
MAIL_RECIPIENT_LIMIT_DAY, MAIL_RECIPIENT_WINDOW_DAY = 5, 86400  # per recipient / day
MAIL_ACCOUNT_LIMIT, MAIL_ACCOUNT_WINDOW = 10, 3600   # per signed-in account / hour
MAIL_ADDRESS_LIMIT, MAIL_ADDRESS_WINDOW = 1, 600     # per caller-supplied address / 10 min

_LIMITS = {
    "register": ((REGISTER_LIMIT, REGISTER_WINDOW),),
    "login": ((LOGIN_LIMIT, LOGIN_WINDOW),),
    "captcha": ((CAPTCHA_LIMIT, CAPTCHA_WINDOW),),
    "email": ((EMAIL_LIMIT, EMAIL_WINDOW),),
    "forgot": ((FORGOT_LIMIT, FORGOT_WINDOW),),
    "mail_recipient": ((MAIL_RECIPIENT_LIMIT_MIN, MAIL_RECIPIENT_WINDOW_MIN),
                       (MAIL_RECIPIENT_LIMIT_DAY, MAIL_RECIPIENT_WINDOW_DAY)),
    "mail_account": ((MAIL_ACCOUNT_LIMIT, MAIL_ACCOUNT_WINDOW),),
    "mail_address": ((MAIL_ADDRESS_LIMIT, MAIL_ADDRESS_WINDOW),),
}
_MAX_BUCKETS = 20000

_buckets = {}  # (scope, key, window_index) -> [window_start, count]
_lock = threading.Lock()
_full_warned_at = 0.0


def _evict_expired(now):
    """Drop buckets whose window has elapsed. Caller holds the lock."""
    for k in [k for k, v in _buckets.items() if now - v[0] >= _LIMITS[k[0]][k[2]][1]]:
        _buckets.pop(k, None)


def allow(scope, key, now=None):
    """True when every window of (scope, key) still has budget.

    Consumes one unit in each window on success. ``now`` is injectable for
    tests. Unknown scope falls back to unlimited (True).
    """
    global _full_warned_at
    windows = _LIMITS.get(scope)
    if windows is None:
        return True
    now = time.time() if now is None else now
    with _lock:
        if len(_buckets) >= _MAX_BUCKETS:
            _evict_expired(now)
            if len(_buckets) >= _MAX_BUCKETS:
                if now - _full_warned_at > 60:
                    _full_warned_at = now
                    print(f"ratelimit: bucket table full ({_MAX_BUCKETS} live "
                          f"keys) — denying scope={scope}", flush=True)
                return False
        fresh = []
        for i, (limit, window) in enumerate(windows):
            bucket_key = (scope, key, i)
            b = _buckets.get(bucket_key)
            if b is None or now - b[0] >= window:
                b = _buckets[bucket_key] = [now, 0]
            if b[1] >= limit:
                return False
            fresh.append(b)
        for b in fresh:
            b[1] += 1
        return True


def reset():
    """Drop all counters (tests)."""
    with _lock:
        _buckets.clear()