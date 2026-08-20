"""Self-hosted math CAPTCHA: zero external dependencies, reachable anywhere.

Why self-hosted: Google reCAPTCHA and hCaptcha are unreachable/unstable for
mainland-China users, and Cloudflare Turnstile is not reliable there either.
A simple arithmetic puzzle rendered as SVG needs no third-party account and
works on any deployment.

State: challenges live in an in-process dict with TTL, single-use. Safe as
long as the server runs single-process (main.py runs plain uvicorn.run).
Move to a shared store (Redis/DB) if the server ever runs multi-worker.
"""
import random
import secrets
import time

_TTL_SECONDS = 300          # challenge lifetime
_MAX_STORE = 10000          # hard cap, swept lazily

store = {}  # cid -> {"answer": int, "expires": float}


def new_challenge():
    """Generate (challenge_id, svg). Store the answer server-side."""
    a = random.randint(10, 99)
    op = random.choice(("+", "-"))
    b = random.randint(1, 99) if op == "+" else random.randint(1, a)
    answer = a + b if op == "+" else a - b
    cid = secrets.token_urlsafe(18)
    store[cid] = {"answer": answer, "expires": time.time() + _TTL_SECONDS}
    _sweep()
    return cid, _render_svg(f"{a} {op} {b} = ?")


def verify(cid, answer):
    """Consume the challenge; True only on the first correct answer in time.

    Pop-first makes it single-use: a wrong answer already burns the challenge,
    so brute-forcing a 2-digit result is pointless (fresh one on retry).
    """
    if not cid:
        return False
    entry = store.pop(cid, None)
    if entry is None or entry["expires"] < time.time():
        return False
    try:
        return int(str(answer).strip()) == entry["answer"]
    except (TypeError, ValueError):
        return False


def _sweep():
    if len(store) > _MAX_STORE:
        now = time.time()
        for k in [k for k, v in store.items() if v["expires"] < now]:
            store.pop(k, None)


def _render_svg(text):
    """Render the expression as noisy SVG (per-char rotation, stripes, dots).

    Answer OCR-proof enough for script-batch registration: an attacker would
    have to solve arithmetic, not just read text.
    """
    colors = ("#1f2937", "#374151", "#4b5563", "#7c2d12", "#065f46")
    stripe_colors = ("#cbd5e1", "#e2e8f0", "#94a3b8")
    dot_colors = ("#94a3b8", "#cbd5e1")

    x = 8
    h = 44
    elems = []
    for ch in text:
        rot = random.randint(-18, 18)
        dy = random.randint(-3, 3)
        size = random.randint(20, 26)
        elems.append(
            f'<text x="{x}" y="{30 + dy}" font-size="{size}" fill="{random.choice(colors)}" '
            f'transform="rotate({rot} {x} 30)" font-family="monospace" '
            f'font-weight="bold">{ch}</text>'
        )
        x += 16 + random.randint(0, 4)
    w = x + 10

    stripes = "".join(
        f'<line x1="{random.randint(0, w)}" y1="{random.randint(0, h)}" '
        f'x2="{random.randint(0, w)}" y2="{random.randint(0, h)}" '
        f'stroke="{random.choice(stripe_colors)}" stroke-width="1.5"/>'
        for _ in range(2)
    )
    dots = "".join(
        f'<circle cx="{random.randint(0, w)}" cy="{random.randint(0, h)}" r="0.8" '
        f'fill="{random.choice(dot_colors)}"/>'
        for _ in range(40)
    )
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" '
        f'viewBox="0 0 {w} {h}">'
        f'<rect width="{w}" height="{h}" fill="#f8fafc" rx="8"/>'
        f'{stripes}{dots}{"".join(elems)}</svg>'
    )
