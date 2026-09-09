"""Per-IP request rate limiting (in-process, single-worker).

Same deployment constraint as captcha.py: counters live in this process, so
the server must run single-process (main.py runs plain uvicorn.run; the
captcha module documents the same invariant). Keying is by
request.client.host; deployments behind a reverse proxy MUST run uvicorn
with proxy headers enabled (main.py already passes proxy_headers=True, which
trusts X-Forwarded-For only from 127.0.0.1 by default) so client.host is the
real client IP — otherwise every user shares the proxy IP and the limits
cannot distinguish them (and blindly trusting an unvalidated X-Forwarded-For
would let a direct attacker rotate headers to bypass the limit).

Buckets are fixed windows: (window_start, count). A bucket whose window
elapsed resets on next use. Under memory pressure the whole table is cleared
(coarse but safe: an attacker saturating distinct IPs has already won the
memory game, and 20k entries is tiny).
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

_LIMITS = {
    "register": (REGISTER_LIMIT, REGISTER_WINDOW),
    "login": (LOGIN_LIMIT, LOGIN_WINDOW),
    "captcha": (CAPTCHA_LIMIT, CAPTCHA_WINDOW),
    "email": (EMAIL_LIMIT, EMAIL_WINDOW),
    "forgot": (FORGOT_LIMIT, FORGOT_WINDOW),
}
_MAX_BUCKETS = 20000

_buckets = {}  # (scope, key) -> [window_start, count]
_lock = threading.Lock()


def allow(scope, key, now=None):
    """True when (scope, key) still has budget inside its current window.

    Consumes one unit on success. ``now`` is injectable for tests.
    Unknown scope falls back to unlimited (True).
    """
    limit_window = _LIMITS.get(scope)
    if limit_window is None:
        return True
    limit, window = limit_window
    now = time.time() if now is None else now
    with _lock:
        b = _buckets.get((scope, key))
        if b is None:
            if len(_buckets) >= _MAX_BUCKETS:
                _buckets.clear()
            b = _buckets[(scope, key)] = [now, 0]
        elif now - b[0] >= window:
            b[0] = now
            b[1] = 0
        if b[1] >= limit:
            return False
        b[1] += 1
        return True


def reset():
    """Drop all counters (tests)."""
    with _lock:
        _buckets.clear()
