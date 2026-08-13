"""
Agent Contexts Sync MCP Server (multi-agent)
- Adapter framework: HERMES_SYNC_AGENT selects the local store adapter
  (hermes | codex | opencode | reasonix | openclaw); every agent gets its
  own deployment (each instance manages one local store)
- Auto-pull on startup (background); bootstrap push when remote is empty
- Periodic sync every N minutes (HERMES_SYNC_INTERVAL)
- Tools: sync_status / sync_pull / sync_push / sync_full (+ hermes_sync_*
  aliases for backwards compatibility)
- Authenticated via workspace API key
"""

import asyncio
import json
import os
import sys
import urllib.request
import urllib.error
from pathlib import Path

# Allow running as ``python <abs path>/server.py`` from ANY working
# directory: host agents (Hermes, reasonix plugins, ...) do not guarantee
# the cwd is the mcp/ folder, and ``from adapters import ...`` below needs
# this package on sys.path.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent

from adapters import get_adapter, available_agents
import updater

SYNC_SERVER = os.environ.get("HERMES_SYNC_SERVER", "http://localhost:8765")
SYNC_API_KEY = os.environ.get("HERMES_SYNC_API_KEY", "hsk_placeholder")
SYNC_INTERVAL = int(os.environ.get("HERMES_SYNC_INTERVAL", "300"))
# Background auto-sync (startup pull + periodic sync) can be disabled so the
# client never competes for the local store locks; manual tool calls still
# work. Hermes on SQLite < 3.51.3 uses journal_mode=DELETE where a write
# blocks concurrent readers -- set to 0 to keep Hermes' own reads (e.g.
# session.resume) entirely lock-free.
AUTO_SYNC = os.environ.get("HERMES_SYNC_AUTO_SYNC", "1") != "0"
# Client auto-update: check once shortly after startup, then every
# HERMES_SYNC_UPDATE_INTERVAL seconds (default 24h). Files are replaced in
# the background and take effect on the next agent restart. Set
# HERMES_SYNC_AUTO_UPDATE=0 to disable.
AUTO_UPDATE = os.environ.get("HERMES_SYNC_AUTO_UPDATE", "1") != "0"
UPDATE_INTERVAL = int(os.environ.get("HERMES_SYNC_UPDATE_INTERVAL", "86400"))
MCP_DIR = Path(__file__).resolve().parent
VERSION_FILE = MCP_DIR / updater.VERSION_FILE_NAME
AGENT = os.environ.get("HERMES_SYNC_AGENT", "hermes")
if AGENT not in available_agents():
    sys.stderr.write(
        f"[hermes-sync] Unknown agent {AGENT!r}; "
        f"falling back to 'hermes'. Known agents: {available_agents()}\n")
    AGENT = "hermes"
adapter = get_adapter(AGENT)
DEVICE_ID = f"local-{os.environ.get('COMPUTERNAME', 'unknown')}"

# Single-writer guard for the BACKGROUND sync loops (startup pull + periodic
# sync). The Hermes desktop app runs two `serve` instances (Hermes.exe →
# serve(venv) → serve(.hermes-runtime)), each spawning its own copy of this
# MCP server — so two processes can otherwise run the sync concurrently and
# race on the same local store. Only the process holding the lockfile runs
# the background loops; the other skips. Explicit tool calls are NOT guarded.
# Lock files are per-agent so independent agent deployments never block each
# other (hermes keeps its legacy lock name).
_lock_name = "hermes-sync" if AGENT == "hermes" else f"hermes-sync-{AGENT}"
LOCK_FILE = Path(os.environ.get(
    "HERMES_SYNC_LOCK_FILE",
    str(Path.home() / "AppData/Local/hermes" / (_lock_name + ".lock"))))
UPDATE_LOCK_FILE = Path(os.environ.get(
    "HERMES_SYNC_UPDATE_LOCK_FILE",
    str(Path.home() / "AppData/Local/hermes" / (_lock_name + "-update.lock"))))

def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False

def _try_acquire_lock(lock_path: Path | None = None) -> bool:
    """Create the lockfile atomically (O_EXCL). Steal it if the previous
    holder's PID is dead (crashed process would otherwise block sync forever)."""
    lock_path = lock_path or LOCK_FILE
    try:
        fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
        return True
    except FileExistsError:
        try:
            holder = int(lock_path.read_text().strip())
        except Exception:
            return False
        if not _pid_alive(holder):
            try:
                lock_path.unlink()
            except OSError:
                return False
            return _try_acquire_lock(lock_path)
        return False

def _release_lock(lock_path: Path | None = None):
    lock_path = lock_path or LOCK_FILE
    try:
        lock_path.unlink()
    except OSError:
        pass

server = Server("hermes-session-sync")

def log(msg):
    sys.stderr.write(f"[hermes-sync] {msg}\n")
    sys.stderr.flush()

def api_call(method, path, data=None):
    url = f"{SYNC_SERVER}{path}"
    headers = {"Authorization": f"Bearer {SYNC_API_KEY}", "Content-Type": "application/json",
               "User-Agent": "hermes-sync-client/1.0"}
    body = json.dumps(data).encode() if data else None
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        resp = urllib.request.urlopen(req, timeout=120)
        return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        return {"error": e.code, "detail": e.read().decode()}
    except Exception as e:
        return {"error": str(e)}

def pull_sessions(last_sync_at=None, limit=None):
    """Pull remote sessions into the local store via the agent adapter.

    ``last_sync_at=None`` (default) pulls incrementally from the adapter's
    own ``last_synced_at()`` watermark; pass 0 for a full pull. Paginates
    over the remote /pull endpoint so ALL sessions are fetched; ``limit=N``
    caps the total (the MCP tool uses this). Each page is written with
    stable (session_id, role, timestamp) dedupe in the adapter; remote
    message ids are dropped so the local store assigns fresh ids.
    """
    if adapter.discover() is None:
        return {"error": f"Local store not found for agent {AGENT}"}
    if last_sync_at is None:
        # Incremental from the local watermark, with a 5-min grace window:
        # the watermark is a LOCAL timestamp while remote last_synced_at
        # values are written by OTHER devices' clocks, so a strict cutoff
        # could silently skip sessions pushed by a clock-skewed peer.
        last_sync_at = max(0.0, adapter.last_synced_at() - 300)

    imported, new_messages = 0, 0
    total_remote = 0

    PAGE = 50
    fetched = 0
    prev_page_ids = None
    while True:
        page_limit = PAGE if limit is None else max(min(PAGE, limit - fetched), 0)
        if page_limit <= 0:
            break
        result = api_call("POST", "/pull", {
            "device_id": DEVICE_ID,
            "last_sync_at": last_sync_at, "limit": page_limit, "offset": fetched,
            "agent": AGENT,
        })
        if "error" in result:
            if fetched == 0:
                return result
            return {"imported": imported, "new_messages": new_messages,
                    "total_remote_sessions": total_remote,
                    "error": f"partial pull after {fetched} sessions: {result['error']}"}

        sessions = result.get("sessions", [])
        total_remote = result.get("total_sessions") or result.get("session_count") or 0
        if not sessions:
            break
        # Guard against a server that ignores `offset` and repeats the same
        # page forever (old deployments): stop when the page is unchanged.
        page_ids = tuple(s["id"] for s in sessions)
        if page_ids == prev_page_ids:
            break
        prev_page_ids = page_ids

        stats = adapter.write_sessions(sessions)
        imported += stats.get("imported", 0)
        new_messages += stats.get("new_messages", 0)

        fetched += len(sessions)
        if len(sessions) < page_limit:
            break  # last page
        if limit is not None and fetched >= limit:
            break

    if "error" not in result and result.get("sync_at"):
        adapter.save_sync_watermark(result["sync_at"])

    return {"imported": imported, "new_messages": new_messages,
            "total_remote_sessions": total_remote}

def push_sessions():
    if adapter.discover() is None:
        return {"error": f"Local store not found for agent {AGENT}"}

    sessions_data = adapter.read_sessions(limit=50)
    if not sessions_data:
        return {"message": "No local sessions to push"}
    # Tag every session with its agent type so the server can store it in
    # the shared workspace pool alongside other agents.
    for s in sessions_data:
        s["agent_type"] = adapter.agent_type

    # Batch pushes so each request stays small and fast: the remote server
    # does a per-message dedup SELECT for every row, so one giant request
    # (50 sessions, thousands of messages) can exceed the HTTP timeout as
    # the workspace grows (observed: 31s on a ~6k-message push with a 30s
    # timeout). Partial failures report how far we got.
    BATCH = 20
    totals = {"imported": 0, "updated": 0, "new_messages": 0, "sync_at": None}
    for i in range(0, len(sessions_data), BATCH):
        chunk = sessions_data[i:i + BATCH]
        result = api_call("POST", "/push", {"device_id": DEVICE_ID, "sessions": chunk})
        if "error" in result:
            if i == 0:
                return result
            return {**totals, "error": f"partial failure after {i} sessions: {result['error']}"}
        for k in ("imported", "updated", "new_messages"):
            totals[k] += result.get(k, 0)
        totals["sync_at"] = result.get("sync_at", totals["sync_at"])
    return totals

def full_sync():
    push_result = push_sessions()
    pull_result = pull_sessions()
    return {"push": push_result, "pull": pull_result}

def push_projects():
    """Push local projects (all profiles) to the server."""
    if adapter.discover() is None:
        return {"error": f"Local store not found for agent {AGENT}"}
    projects = adapter.read_projects()
    if not projects:
        return {"message": "No local projects to push"}
    result = api_call("POST", "/api/projects/push",
                      {"device_id": DEVICE_ID, "projects": projects})
    return result

def pull_projects():
    """Pull remote projects + remap records into local projects.db."""
    if adapter.discover() is None:
        return {"error": f"Local store not found for agent {AGENT}"}
    result = api_call("POST", "/api/projects/pull", {"device_id": DEVICE_ID})
    if "error" in result:
        return result
    stats = adapter.write_projects(result.get("projects", []),
                                   result.get("remaps", []))
    return {"imported": stats.get("imported", 0),
            "projects": len(result.get("projects", []))}

# Tool names: neutral `sync_*` for any agent; `hermes_sync_*` aliases kept
# for existing Hermes registrations.
TOOL_SPECS = [
    ("sync_status", "Show sync status: local store totals and remote server status."),
    ("sync_pull", "Pull latest sessions from remote server into the local store.",
     {"limit": {"type": "integer", "description": "Max sessions to pull (default: 50)"}}),
    ("sync_push", "Push local sessions to remote server."),
    ("sync_full", "Full sync: push local changes then pull remote changes."),
    ("project_push", "Push local projects (all profiles) to the remote server."),
    ("project_pull", "Pull remote projects into the local projects.db (applies remaps)."),
]

@server.list_tools()
async def list_tools() -> list[Tool]:
    tools = []
    for name, desc, *rest in TOOL_SPECS:
        props = rest[0] if rest else {}
        tools.append(Tool(name=name, description=desc,
                          inputSchema={"type": "object", "properties": props}))
        # aliases (hermes_sync_*)
        tools.append(Tool(name="hermes_" + name, description=desc + " (alias)",
                          inputSchema={"type": "object", "properties": props}))
    return tools

@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    loop = asyncio.get_event_loop()
    base = name[len("hermes_"):] if name.startswith("hermes_") else name
    if base == "sync_status":
        local = adapter.status()
        remote = api_call("GET", f"/status/{DEVICE_ID}")
        result = {"agent": adapter.agent_type, "local": local, "remote": remote}
        text = json.dumps(result, indent=2, ensure_ascii=False)
    elif base == "sync_pull":
        limit = arguments.get("limit", 50)
        result = await loop.run_in_executor(None, lambda: pull_sessions(limit=limit))
        text = json.dumps(result, indent=2, ensure_ascii=False)
    elif base == "sync_push":
        result = await loop.run_in_executor(None, push_sessions)
        text = json.dumps(result, indent=2, ensure_ascii=False)
    elif base == "sync_full":
        result = await loop.run_in_executor(None, full_sync)
        text = json.dumps(result, indent=2, ensure_ascii=False)
    elif base == "project_push":
        result = await loop.run_in_executor(None, push_projects)
        text = json.dumps(result, indent=2, ensure_ascii=False)
    elif base == "project_pull":
        result = await loop.run_in_executor(None, pull_projects)
        text = json.dumps(result, indent=2, ensure_ascii=False)
    else:
        raise ValueError(f"Unknown tool: {name}")
    return [TextContent(type="text", text=text)]

async def periodic_sync():
    if not AUTO_SYNC:
        log("Background periodic sync disabled (HERMES_SYNC_AUTO_SYNC=0)")
        return
    while True:
        await asyncio.sleep(SYNC_INTERVAL)
        if not _try_acquire_lock():
            log("Periodic sync skipped: another server process holds the lock")
            continue
        try:
            loop = asyncio.get_event_loop()
            result = await loop.run_in_executor(None, full_sync)
            imported = result.get("pull", {}).get("imported", 0)
            pushed = result.get("push", {}).get("imported", 0) + result.get("push", {}).get("updated", 0)
            log(f"Periodic sync: pulled {imported} sessions, pushed {pushed} sessions")
            # projects sync (same cycle, best-effort)
            try:
                pp = await loop.run_in_executor(None, push_projects)
                pl = await loop.run_in_executor(None, pull_projects)
                log(f"Projects sync: push={pp.get('imported', pp.get('updated', 0))}, "
                    f"pull={pl.get('projects', 0)}")
            except Exception as e:
                log(f"Projects sync error: {e}")
        except Exception as e:
            log(f"Periodic sync error: {e}")
        finally:
            _release_lock()

async def background_startup_sync():
    if not AUTO_SYNC:
        log("Background startup sync disabled (HERMES_SYNC_AUTO_SYNC=0)")
        return
    # Delay the first pull so the host agent's own startup/read burst (e.g.
    # Hermes session.resume) has finished before we take SQLite locks.
    await asyncio.sleep(8)
    log(f"Starting, auto-pulling from {SYNC_SERVER} (agent: {adapter.agent_type})...")
    if not _try_acquire_lock():
        log("Initial pull skipped: another server process holds the lock")
        return
    try:
        loop = asyncio.get_event_loop()
        watermark = adapter.last_synced_at()
        result = await loop.run_in_executor(None, lambda: pull_sessions())
        # Fresh-pairing bootstrap: when the local store has NEVER synced
        # (watermark == 0 the pull above is a full pull, so its total is the
        # real remote count) and the remote is empty, push the local data.
        # Retry the pull a couple of times to guard transient flakiness; a
        # false positive is harmless -- the server dedupes on
        # (session, role, timestamp), so an extra full push is a no-op.
        if (watermark == 0 and "error" not in result
                and result.get("total_remote_sessions") == 0):
            for _ in range(2):
                await asyncio.sleep(2)
                result = await loop.run_in_executor(None, lambda: pull_sessions())
                if "error" in result or result.get("total_remote_sessions", 0) > 0:
                    break
            if "error" not in result and result.get("total_remote_sessions") == 0:
                log("Remote workspace is empty; bootstrapping by pushing local data")
                push_result = await loop.run_in_executor(None, push_sessions)
                log(f"Bootstrap push: {push_result}")
        log(f"Initial pull: {result}")
    except Exception as e:
        log(f"Initial pull failed: {e}")
    finally:
        _release_lock()

def _run_update_check():
    if not AUTO_UPDATE:
        return
    if not _try_acquire_lock(UPDATE_LOCK_FILE):
        log("Update check skipped: another server process holds the update lock")
        return
    try:
        # synchronous urllib work; callers run this in an executor
        applied = updater.check_and_update(
            SYNC_SERVER, SYNC_API_KEY, AGENT, MCP_DIR, VERSION_FILE, log)
        if applied:
            log(f"Client updated to {updater.local_version(VERSION_FILE)}; "
                f"restart the agent to activate")
    except Exception as e:
        log(f"Update check error: {e}")
    finally:
        _release_lock(UPDATE_LOCK_FILE)

async def background_update_check():
    """Check for a client update once shortly after startup, then every
    UPDATE_INTERVAL seconds. Replaced files activate on agent restart."""
    if not AUTO_UPDATE:
        log("Client auto-update disabled (HERMES_SYNC_AUTO_UPDATE=0)")
        return
    await asyncio.sleep(15)  # after the host agent's startup burst
    await asyncio.get_event_loop().run_in_executor(None, _run_update_check)
    while True:
        await asyncio.sleep(UPDATE_INTERVAL)
        await asyncio.get_event_loop().run_in_executor(None, _run_update_check)

async def main():
    asyncio.create_task(background_startup_sync())
    asyncio.create_task(periodic_sync())
    asyncio.create_task(background_update_check())
    log(f"Device: {DEVICE_ID}")
    log(f"Agent: {adapter.agent_type} (local store: {adapter.discover()})")
    log(f"Periodic sync enabled: every {SYNC_INTERVAL}s ({SYNC_INTERVAL//60}min)")
    log(f"Client version: {updater.local_version(VERSION_FILE)} "
        f"(auto-update: {'on' if AUTO_UPDATE else 'off'}, "
        f"check every {UPDATE_INTERVAL//3600}h)")

    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())

if __name__ == "__main__":
    asyncio.run(main())
