"""
Standalone periodic sync for OpenClaw.

OpenClaw lazily spawns bundled MCP servers (only when the agent calls one of
their tools), so HERMES_SYNC_AUTO_SYNC=1 on the MCP registration never fires
on its own -- the process is not alive between agent turns. This loop runs
the same sync engine (server.full_sync: pull-then-push, field-level merge,
watermark + dedupe) on a fixed interval regardless of OpenClaw activity, so
new sessions/messages land on the sync server automatically.

Usage:
    python mcp/auto-sync.py                # defaults from env / server.py
    HERMES_SYNC_INTERVAL=300 python mcp/auto-sync.py

Runs forever; stop with Ctrl-C. Share the same lock file as the MCP server,
so a manual sync_push via OpenClaw and this loop never run concurrently.
"""

import asyncio
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

# env must be set BEFORE importing server (module-level adapter + constants)
os.environ.setdefault("HERMES_SYNC_AGENT", "openclaw")
os.environ.setdefault("HERMES_SYNC_SERVER", "http://127.0.0.1:8765")
os.environ.setdefault("HERMES_SYNC_AUTO_SYNC", "1")
if not os.environ.get("HERMES_SYNC_API_KEY"):
    print("[auto-sync] ERROR: HERMES_SYNC_API_KEY is required "
          "(set it before running; never hardcode a workspace key)",
          file=sys.stderr)
    sys.exit(2)

import server  # noqa: E402  (module-level adapter init; no stdio run)

INTERVAL = max(60, int(os.environ.get("HERMES_SYNC_INTERVAL", "300")))


async def run_loop():
    loop = asyncio.get_event_loop()
    # first sync shortly after start (mirrors the MCP server's startup sync)
    await asyncio.sleep(8)
    while True:
        started = time.time()
        try:
            result = await loop.run_in_executor(None, server.full_sync)
            if isinstance(result, dict) and result.get("error"):
                print(f"[auto-sync] {time.strftime('%H:%M:%S')} error: "
                      f"{result['error']}")
            else:
                print(f"[auto-sync] {time.strftime('%H:%M:%S')} ok: {result}")
        except Exception as exc:  # keep the loop alive on transient failures
            print(f"[auto-sync] {time.strftime('%H:%M:%S')} exception: {exc}")
        elapsed = time.time() - started
        await asyncio.sleep(max(1, INTERVAL - elapsed))


if __name__ == "__main__":
    print(f"[auto-sync] agent={server.adapter.agent_type} "
          f"store={server.adapter.discover()} interval={INTERVAL}s")
    asyncio.run(run_loop())
