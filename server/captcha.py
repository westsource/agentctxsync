"""Self-hosted math CAPTCHA: zero external dependencies, reachable anywhere.

Why self-hosted: Google reCAPTCHA and hCaptcha are unreachable/unstable for
mainland-China users, and Cloudflare Turnstile is not reliable there either.
A simple arithmetic puzzle rendered as SVG needs no third-party account and
works on any deployment.

The puzzle is drawn as distorted vector strokes — no <text> nodes. An earlier
version emitted per-character <text>, which made the expression readable with
one regex (no image work at all, so the gate cost nothing to cross). Reading
the picture now means rasterise + OCR, i.e. real work per solve. That is a
cost gate for bulk abuse, not a humanity proof: a determined caller with an
OCR pipeline and a proxy pool still gets through, which is why the mail paths
also carry per-recipient limits and a daily budget.

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

# Glyphs as polylines in a unit box. Deliberately line-only: stroke width,
# rotation and jitter are what make the picture hard to read mechanically, and
# a line-only renderer needs no font, no library and no rasteriser.
_A = ((0.0, 0.0), (1.0, 0.0))     # top
_B = ((1.0, 0.0), (1.0, 0.5))     # upper right
_C = ((1.0, 0.5), (1.0, 1.0))     # lower right
_D = ((0.0, 1.0), (1.0, 1.0))     # bottom
_E = ((0.0, 0.5), (0.0, 1.0))     # lower left
_F = ((0.0, 0.0), (0.0, 0.5))     # upper left
_G = ((0.0, 0.5), (1.0, 0.5))     # middle

_GLYPHS = {
    "0": (_A, _B, _C, _D, _E, _F),
    "1": (_B, _C),
    "2": (_A, _B, _G, _E, _D),
    "3": (_A, _B, _G, _C, _D),
    "4": (_F, _G, _B, _C),
    "5": (_A, _F, _G, _C, _D),
    "6": (_A, _F, _G, _E, _C, _D),
    "7": (_A, _B, _C),
    "8": (_A, _B, _C, _D, _E, _F, _G),
    "9": (_A, _B, _C, _D, _F, _G),
    "+": (((0.5, 0.12), (0.5, 0.88)), ((0.12, 0.5), (0.88, 0.5))),
    "-": (((0.15, 0.5), (0.85, 0.5)),),
    "=": (((0.12, 0.34), (0.88, 0.34)), ((0.12, 0.66), (0.88, 0.66))),
    "?": (((0.08, 0.3), (0.34, 0.04), (0.76, 0.08), (0.94, 0.4)),
          ((0.5, 0.62), (0.5, 0.78)),
          ((0.5, 0.94), (0.5, 0.96))),
}
_GLYPH_W, _GLYPH_H = 20, 44
_HEIGHT = 56


def _new_expression():
    """(expression, answer) — the arithmetic behind one challenge.

    Split out so the generator can be pinned in tests without reading any
    rendered markup (the markup deliberately carries no readable text).
    """
    a = random.randint(10, 99)
    op = random.choice(("+", "-"))
    b = random.randint(1, 99) if op == "+" else random.randint(1, a)
    return f"{a} {op} {b} = ?", (a + b if op == "+" else a - b)


def new_challenge():
    """Generate (challenge_id, svg). Store the answer server-side."""
    expr, answer = _new_expression()
    cid = secrets.token_urlsafe(18)
    store[cid] = {"answer": answer, "expires": time.time() + _TTL_SECONDS}
    _sweep()
    return cid, _render_svg(expr)


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
    """Render the expression as distorted vector strokes.

    No <text> nodes anywhere: the markup must never contain the expression in
    a form a regex can read. Per glyph the strokes get a random rotation,
    vertical jitter and stroke width; the whole picture gets two crossing
    lines and a sprinkle of dots so a clean OCR pass is not free either.
    """
    stroke_colors = ("#1f2937", "#374151", "#4b5563", "#7c2d12", "#065f46")
    noise_colors = ("#cbd5e1", "#e2e8f0", "#94a3b8")
    dot_colors = ("#94a3b8", "#cbd5e1")
    paths = []
    x = 6
    for ch in text:
        rot = random.uniform(-16, 16)
        dy = random.uniform(-3, 3)
        width = random.uniform(2.2, 3.2)
        color = random.choice(stroke_colors)
        cx = x + _GLYPH_W / 2
        cy = _HEIGHT / 2 + dy
        for seg in _GLYPHS.get(ch, ()):
            d = " ".join(
                ("M" if i == 0 else "L")
                + f" {x + px * _GLYPH_W:.1f} {8 + dy + py * _GLYPH_H:.1f}"
                for i, (px, py) in enumerate(seg))
            paths.append(
                f'<path d="{d}" fill="none" stroke="{color}" '
                f'stroke-width="{width:.1f}" stroke-linecap="round" '
                f'transform="rotate({rot:.1f} {cx:.1f} {cy:.1f})"/>')
        x += _GLYPH_W + random.randint(2, 6)
    w = x + 6
    stripes = "".join(
        f'<line x1="{random.randint(0, w)}" y1="{random.randint(0, _HEIGHT)}" '
        f'x2="{random.randint(0, w)}" y2="{random.randint(0, _HEIGHT)}" '
        f'stroke="{random.choice(noise_colors)}" stroke-width="1.5"/>'
        for _ in range(2)
    )
    dots = "".join(
        f'<circle cx="{random.randint(0, w)}" cy="{random.randint(0, _HEIGHT)}" '
        f'r="0.8" fill="{random.choice(dot_colors)}"/>'
        for _ in range(40)
    )
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{_HEIGHT}" '
        f'viewBox="0 0 {w} {_HEIGHT}">'
        f'<rect width="{w}" height="{_HEIGHT}" fill="#f8fafc" rx="8"/>'
        f'{stripes}{dots}{"".join(paths)}</svg>'
    )
