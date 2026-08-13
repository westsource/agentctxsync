"""
Adapter template -- copy this file to mcp/adapters/<name>.py to support a
new agent. Follow the checklist in docs/ADDING_AGENT.md.

Steps to wire in a new agent:
    1. cp mcp/adapters/_template.py mcp/adapters/<name>.py
    2. set agent_type (must match AGENT_PREFIXES in base.py)
    3. implement discover / read_sessions / write_sessions / status
    4. expose the adapter class as ``Adapter`` (rename the class or add
       ``Adapter = <Name>Adapter`` -- the registry looks up module.Adapter)
    5. register "<name>: <module>" in mcp/adapters/__init__.py _ADAPTER_MODULES
    6. add a fixture + round-trip test in mcp/tests/
"""

import time
from pathlib import Path

from .base import Adapter


class MyAdapter(Adapter):  # rename to <Name>Adapter AND expose it as Adapter
    """<agent display name> local store adapter."""

    agent_type = "<name>"  # must exist in AGENT_PREFIXES

    # ------------------------------------------------------------------
    # Discovery: point at the agent's local store.
    # Research before implementing (docs/ADDING_AGENT.md checklist):
    #   * data directory (env override? XDG? %APPDATA%?)
    #   * file format (SQLite / JSONL / JSON files) and exact schema
    #   * session id format & where it lives
    #   * write constraints: append-only? lock files? atomic rename?
    #     index/backfill that needs refreshing? compression (.zst)?
    #   * encryption / integrity checks
    # ------------------------------------------------------------------
    def discover(self) -> Path | None:
        # e.g.:
        # d = Path(os.environ.get("<AGENT>_HOME", Path.home() / ".<name>"))
        # return d if d.exists() else None
        raise NotImplementedError

    # ------------------------------------------------------------------
    # Reading: local format -> canonical dicts (see base.py docstring).
    # Use self.canonicalize(local_session) to add the id prefix and fix
    # message session_ids. Return newest-first.
    # ------------------------------------------------------------------
    def read_sessions(self, limit: int | None = None) -> list[dict]:
        raise NotImplementedError

    # ------------------------------------------------------------------
    # Writing: canonical dicts -> local format.
    # Use self.localize(canonical_session) to strip the prefix.
    # Session upsert key = local id; message dedupe on
    # (session_id, role, timestamp); never reuse remote message ids.
    # Respect the agent's write constraints (append-only, atomic rename,
    # index files, lock files). Return
    #   {"imported": int, "updated": int, "new_messages": int,
    #    "duplicates": int}
    # ------------------------------------------------------------------
    def write_sessions(self, sessions: list[dict]) -> dict:
        raise NotImplementedError

    # ------------------------------------------------------------------
    # Status: local totals for /status and hermes_sync_status.
    # ------------------------------------------------------------------
    def status(self) -> dict:
        raise NotImplementedError


# Smoke-test harness (python -m mcp.adapters.<name>):
if __name__ == "__main__":
    a = Adapter()
    print("discover:", a.discover())
    print("status:", a.status())
    print("sessions:", len(a.read_sessions(limit=5)))
