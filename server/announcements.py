"""Announcement banner: the dismiss endpoint.

Content lives in an external JSON feed (config.ANNOUNCEMENTS_URL) that the
browser fetches directly — the server never calls out, so nothing here touches
the network. This module only records *dismissals*, so a message stays closed
for the user who closed it (and only them: the banner is per-user, never
per-deployment).
"""
import re
from datetime import datetime

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from auth import get_current_user
from db import get_conn

router = APIRouter()

# Same shape the SEO build enforces on the feed's ids; validated here too so a
# hostile/typo'd id cannot be stored (and so the table stays clean).
_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")


@router.post("/web/announcement/dismiss")
async def dismiss(request: Request):
    """Close one feed item for the current user. Idempotent."""
    try:
        user = get_current_user(request)
    except Exception:
        return JSONResponse({"ok": False, "error": "unauthenticated"},
                            status_code=401)
    try:
        body = await request.json()
    except Exception:
        body = None
    ann_id = str((body or {}).get("id", "")).strip()
    if not _ID_RE.match(ann_id):
        return JSONResponse({"ok": False, "error": "invalid id"}, status_code=400)
    with get_conn() as conn:
        c = conn.cursor()
        c.execute(
            "INSERT INTO announcement_dismissals "
            "(announcement_id, user_id, dismissed_at) VALUES (%s, %s, %s) "
            "ON CONFLICT (announcement_id, user_id) DO NOTHING",
            (ann_id, int(user["sub"]), datetime.now().timestamp()),
        )
    return JSONResponse({"ok": True, "id": ann_id})
