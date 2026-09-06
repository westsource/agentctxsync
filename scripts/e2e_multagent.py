#!/usr/bin/env python3
"""
End-to-end multi-agent cross-sync test against a running server.

Usage:
    python scripts/e2e_multagent.py http://localhost:8765 <workspace_api_key>

Verifies the full loop: dsh (official DeepSeek Harness) adapter push ->
server (PostgreSQL) -> opencode adapter pull -> local store, and the
reverse direction, with stable identity and idempotent re-push. (The
legacy codex engine was removed 2026-09-06 and merged into dsh.)
"""

import json
import sqlite3
import sys
import tempfile
import time
import urllib.request
import urllib.error
import uuid
from pathlib import Path

# allow running from repo root (mcp package lives at <root>/mcp)
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "mcp"))

from adapters.dsh import DshAdapter  # noqa: E402
from adapters.opencode import OpencodeAdapter  # noqa: E402
from adapters.hermes import HermesAdapter  # noqa: E402


def api_call(server, api_key, method, path, data=None):
    url = f"{server}{path}"
    headers = {"Authorization": f"Bearer {api_key}",
               "Content-Type": "application/json"}
    body = json.dumps(data).encode() if data else None
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode())


def make_hermes_db(path: Path):
    conn = sqlite3.connect(str(path))
    conn.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, session_title TEXT, "
                 "started_at REAL, profile_name TEXT, agent_type TEXT)")
    conn.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, "
                 "session_id TEXT, role TEXT, content TEXT, timestamp REAL)")
    conn.execute("INSERT INTO sessions VALUES "
                 "('hermes-e2e-1', 'E2E Hermes', 1000.0, 'default', 'hermes')")
    conn.execute("INSERT INTO messages (session_id,role,content,timestamp) VALUES "
                 "('hermes-e2e-1','user','hello from hermes',1000.5),"
                 "('hermes-e2e-1','assistant','hi back',1001.0)")
    conn.commit()
    conn.close()


def make_dsh_fixture(root: Path) -> str:
    """One dsh-format session in a temp store (plain jsonl when the running
    python lacks zstandard; the adapter handles both)."""
    sid = f"session-{uuid.uuid4()}"
    a = DshAdapter(sessions_root=root / "sessions", storages_root=root / "storages")
    a.write_sessions([{
        "id": sid, "started_at": 1000.0, "cwd": str(root),
        "title": "DSH E2E",
        "messages": [{"session_id": sid, "role": "user",
                      "content": "hello from dsh", "timestamp": 1001.0}],
    }])
    return sid


def main():
    if len(sys.argv) < 3:
        print(__doc__)
        sys.exit(1)
    server = sys.argv[1].rstrip("/")
    api_key = sys.argv[2]

    tmp = Path(tempfile.mkdtemp(prefix="e2e-multiagent-"))
    passed = 0

    # ---- 1. hermes -> server -> dsh -------------------------------------
    hermes_db = tmp / "hermes" / "state.db"
    hermes_db.parent.mkdir(parents=True)
    make_hermes_db(hermes_db)
    hermes_sessions = HermesAdapter(db_path=hermes_db).read_sessions()
    for s in hermes_sessions:
        s["agent_type"] = "hermes"
    r = api_call(server, api_key, "POST", "/push",
                 {"device_id": "e2e-hermes-device", "sessions": hermes_sessions})
    print("hermes push ->", {k: v for k, v in r.items() if k != "sync_at"})
    assert r.get("imported", 0) == 1, r
    passed += 1

    dsh_home = tmp / "dsh-home"
    make_dsh_fixture(dsh_home)
    dsh = DshAdapter(sessions_root=dsh_home / "sessions",
                     storages_root=dsh_home / "storages")
    dsh_sessions = dsh.read_sessions()
    for s in dsh_sessions:
        s["agent_type"] = "dsh"
    r = api_call(server, api_key, "POST", "/push",
                 {"device_id": "e2e-dsh-device", "sessions": dsh_sessions})
    print("dsh push ->", {k: v for k, v in r.items() if k != "sync_at"})
    assert r.get("imported", 0) == 1, r
    passed += 1

    # dsh pulls everything (its own + hermes' bare-id session)
    r = api_call(server, api_key, "POST", "/pull",
                 {"device_id": "e2e-dsh-device", "last_sync_at": 0,
                  "limit": 50, "offset": 0})
    print("pull ->", r.get("total_sessions"), "sessions on server")
    assert r.get("total_sessions", 0) == 2, r
    stats = dsh.write_sessions(r["sessions"])
    print("dsh local write ->", stats)
    assert stats["imported"] == 1 and stats["new_messages"] == 2, stats
    back = {s["id"]: s for s in dsh.read_sessions()}
    assert "hermes-e2e-1" in back, list(back)  # bare id preserved
    assert any(m["content"] == "hello from hermes"
               for m in back["hermes-e2e-1"]["messages"])
    passed += 1

    # idempotent re-pull
    stats2 = dsh.write_sessions(r["sessions"])
    assert stats2["duplicates"] >= 2, stats2
    passed += 1

    # ---- 2. dsh -> server -> opencode -----------------------------------
    storage = tmp / "opencode-storage"
    (storage / "session" / "info").mkdir(parents=True)
    opencode = OpencodeAdapter(storage_dir=storage)
    stats = opencode.write_sessions(dsh.read_sessions())
    print("opencode local write (from dsh local incl. hermes) ->", stats)
    assert stats["imported"] == 2, stats
    passed += 1

    # opencode pushes its local store; server must dedupe against existing
    oc_sessions = opencode.read_sessions()
    for s in oc_sessions:
        s["agent_type"] = "opencode"
    r = api_call(server, api_key, "POST", "/push",
                 {"device_id": "e2e-opencode-device", "sessions": oc_sessions})
    print("opencode push ->", {k: v for k, v in r.items() if k != "sync_at"})
    assert r.get("imported", 0) == 0, r  # all already on the server
    assert r.get("updated", 0) == 2, r
    passed += 1

    # opencode pulls back; hermes bare-id session must keep its identity
    r = api_call(server, api_key, "POST", "/pull",
                 {"device_id": "e2e-opencode-device", "last_sync_at": 0,
                  "limit": 50, "offset": 0})
    stats = opencode.write_sessions(r["sessions"])
    print("opencode re-pull write ->", stats)
    back = {s["id"]: s for s in opencode.read_sessions()}
    assert "hermes-e2e-1" in back
    assert any(k.startswith("session-") for k in back), list(back)
    passed += 1

    # ---- 3. status endpoint reflects totals -----------------------------
    r = api_call(server, api_key, "GET", "/status/e2e-hermes-device")
    print("status ->", r)
    assert r.get("total_sessions", 0) == 2, r
    assert r.get("total_messages", 0) == 3, r  # hermes 2 msgs + dsh 1 msg
    passed += 1

    print(f"\nALL {passed} E2E CHECKS PASSED")


if __name__ == "__main__":
    main()
