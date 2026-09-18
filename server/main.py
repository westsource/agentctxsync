"""Application assembly: middleware, routers, startup."""
import os

import uvicorn
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

import admin
import announcements
import auth
import client_update
import db
import invites
import projects
import render
import requestlog
import search
import sync
import web_help
import workspace

app = FastAPI(title="Agent Context Sync")

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

# Middleware registration order mirrors the original single-file app:
# later-registered middleware wraps earlier ones, so enforce_account_state
# runs before flash_middleware exactly as before.
app.middleware("http")(render.flash_middleware)
app.middleware("http")(auth.enforce_account_state)
# Outermost: wraps everything, so every request gets a REQ log line.
app.middleware("http")(requestlog.request_log_middleware)


@app.on_event("shutdown")
def _shutdown_pool():
    db._close_pool()
for _mod in (auth, invites, workspace, admin, sync, projects,
             client_update, web_help, search, announcements):
    app.include_router(_mod.router)

if __name__ == "__main__":
    db.init_db()
    print("Backend: PostgreSQL (multi-tenant)")
    print(f"PG DSN: {db.PG_DSN.split('@')[1]}")
    print(f"Templates: {render.TEMPLATE_DIR}")
    print("Web UI: http://127.0.0.1:8765/web/")
    # Loopback only: the app is reachable exclusively through the reverse
    # proxy (nginx terminating TLS, or the self-hosted equivalent), so no
    # plaintext port is published on the public interface. proxy_headers then
    # makes uvicorn trust X-Forwarded-For from 127.0.0.1 only (its default),
    # which request.client.host — and the per-IP rate limits in ratelimit.py —
    # depend on. Fronting it with a proxy is therefore required, not optional.
    uvicorn.run(app, host="127.0.0.1", port=8765, log_level="info",
                proxy_headers=True)
