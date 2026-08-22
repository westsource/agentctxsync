"""Onboarding help domain: help page + client package download."""
import psycopg2.extras

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from agents import AGENTS
from config import _client_default_server
from auth import get_current_user
from client_update import (CLIENT_VERSION, PUBLIC_AGENTS, _build_client_zip,
                           _build_readme, _client_archive_files)
from db import get_conn, get_nav_workspaces, get_user_workspaces
from render import get_lang, render_page

router = APIRouter()
@router.get("/web/help-hermes")
async def web_help_hermes_legacy(request: Request):
    """Legacy route: keep old links working via a permanent redirect."""
    return RedirectResponse(url="/web/help", status_code=301)

@router.get("/web/help", response_class=HTMLResponse)
async def web_help(request: Request):
    try:
        user = get_current_user(request)
    except:
        return RedirectResponse(url="/web/login")
    nav_ws = get_nav_workspaces(user["sub"])
    ws_list = get_user_workspaces(user["sub"])
    # The address shown/shipped: configured public URL, else this request's.
    server_url = _client_default_server(str(request.base_url).rstrip("/"))
    lang = get_lang()
    # agents registry uses "zh"/"en" keys; get_lang() returns "zh-CN"/"en"
    agent_lang = "en" if lang.startswith("en") else "zh"
    # Per-agent install cards: pick the language, substitute the register
    # command into <REGISTER> steps and keep placeholders for user values.
    agents_ctx = {}
    for key, a in AGENTS.items():
        entry = {"label": a["label"], "desc": a["desc"][agent_lang],
                 "store": a["store"][agent_lang], "verify": a["verify"],
                 "uninstall": a.get("uninstall", {}).get(agent_lang, ""),
                 "downloadable": key in PUBLIC_AGENTS,
                 # register template with a __WS_KEY__ placeholder that the
                 # help page fills in client-side with the selected
                 # workspace's API key (wizard step 2).
                 "register": a["register"][agent_lang]
                     .replace("<KEY>", "__WS_KEY__")
                     .replace("<SERVER>", server_url)}
        steps = []
        for step in a["install"][agent_lang]:
            s = dict(step)
            if "code" in s and s["code"] == "<REGISTER>":
                s["code"] = a["register"][agent_lang] \
                    .replace("<KEY>", "<YOUR_API_KEY>") \
                    .replace("<SERVER>", server_url)
            steps.append(s)
        entry["install"] = steps
        agents_ctx[key] = entry
    ctx = {"user": user, "workspaces": nav_ws, "active_page": "help_hermes",
           "ws_list": ws_list, "server_url": server_url, "agents": agents_ctx,
           "client_version": CLIENT_VERSION}
    return await render_page("help_hermes.html", ctx)

@router.get("/web/download/mcp-client")
async def web_download_mcp_client(request: Request, ws_id: int = 0, agent: str = "hermes"):
    try:
        user = get_current_user(request)
    except:
        return RedirectResponse(url="/web/login")
    if agent not in PUBLIC_AGENTS:
        raise HTTPException(status_code=404, detail=f"Agent {agent!r} is not publicly released yet")
    with get_conn() as conn:
        c = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        if ws_id:
            c.execute("SELECT * FROM workspaces WHERE id = %s AND user_id = %s", (ws_id, user["sub"]))
        else:
            c.execute("SELECT * FROM workspaces WHERE user_id = %s ORDER BY created_at DESC LIMIT 1", (user["sub"],))
        ws = c.fetchone()
    if not ws:
        return RedirectResponse(url="/web/help")
    server_url = str(request.base_url).rstrip("/")
    default_server = _client_default_server(server_url)
    data = _build_client_zip(
        agent, default_server,
        readme=_build_readme(agent, "<YOUR_API_KEY>", ws["name"], default_server))
    return Response(
        content=data,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="agentctxsync-mcp-client-{agent}.zip"'},
    )


# ============================================================
# Client auto-update distribution (Sync API auth)
