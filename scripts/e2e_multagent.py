#!/usr/bin/env python3
"""
End-to-end multi-agent cross-sync test against a running server.

Usage:
    python scripts/e2e_multagent.py http://localhost:8765 <workspace_api_key>

Verifies the full loop: codex adapter push -> server (PostgreSQL) ->
opencode adapter pull -> local store, and the reverse direction, with
stable identity and idempotent re-push.
"""

import json
import sqlite3
import sys
import tempfile
import time
import urllib.request
import urllib.error
from pathlib import Path

# allow running from repo root (mcp package lives at <root>/mcp)
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "mcp"))

from adapters.codex import CodexAdapter  # noqa: E402
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
    conn.execute("""CREATE TABLE sessions (
        id TEXT PRIMARY KEY, title TEXT, model TEXT, started_at REAL,
        message_count INTEGER, last_synced_at REAL)""")
    conn.execute("""CREATE TABLE messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT,
        role TEXT, content TEXT, timestamp REAL)""")
    conn.execute("INSERT INTO sessions VALUES ('hermes-e2e-1','Hermes E2E','gpt-4o',1000.0,2,1000.0)")
    conn.execute("INSERT INTO messages (session_id,role,content,timestamp) VALUES "
                 "('hermes-e2e-1','user','hello from hermes',1000.5),"
                 "('hermes-e2e-1','assistant','hi back',1001.0)")
    conn.commit()
    conn.close()


def make_codex_fixture(home: Path):
    sess = home / "sessions"
    sess.mkdir(parents=True)
    import uuid
    uid = str(uuid.uuid4())
    meta = {"meta": {"id": uid, "timestamp": "2026-01-01T10:00:00+00:00",
                     "model_provider": "openai"}, "git": {}}
    line = {"type": "response_item", "payload": {
        "type": "message", "role": "user",
        "content": [{"type": "input_text", "text": "hello from codex"}]}}
    p = sess / f"rollout-2026-01-01T10-00-00-{uid}.jsonl"
    p.write_text(json.dumps(meta) + "\n" + json.dumps(line) + "\n", encoding="utf-8")
    (home / "session_index.jsonl").write_text(
        json.dumps({"id": uid, "thread_name": "Codex E2E",
                    "updated_at": "2026-01-01T10:00:00+00:00"}) + "\n",
        encoding="utf-8")
    return uid


def main():
    if len(sys.argv) < 3:
        print(__doc__)
        sys.exit(1)
    server = sys.argv[1].rstrip("/")
    api_key = sys.argv[2]

    tmp = Path(tempfile.mkdtemp(prefix="e2e-multiagent-"))
    passed = 0

    # ---- 1. hermes -> server -> codex -----------------------------------
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

    codex_home = tmp / "codex-home"
    make_codex_fixture(codex_home)
    codex = CodexAdapter(codex_home=codex_home)
    codex_sessions = codex.read_sessions()
    for s in codex_sessions:
        s["agent_type"] = "codex"
    r = api_call(server, api_key, "POST", "/push",
                 {"device_id": "e2e-codex-device", "sessions": codex_sessions})
    print("codex push ->", {k: v for k, v in r.items() if k != "sync_at"})
    assert r.get("imported", 0) == 1, r
    passed += 1

    # codex pulls everything (its own + hermes' bare-id session)
    r = api_call(server, api_key, "POST", "/pull",
                 {"device_id": "e2e-codex-device", "last_sync_at": 0,
                  "limit": 50, "offset": 0})
    print("pull ->", r.get("total_sessions"), "sessions on server")
    assert r.get("total_sessions", 0) == 2, r
    stats = codex.write_sessions(r["sessions"])
    print("codex local write ->", stats)
    assert stats["imported"] == 1 and stats["new_messages"] == 2, stats
    back = {s["id"]: s for s in codex.read_sessions()}
    assert "hermes-e2e-1" in back, list(back)  # bare id preserved
    assert any(m["content"] == "hello from hermes"
               for m in back["hermes-e2e-1"]["messages"])
    passed += 1

    # idempotent re-pull
    stats2 = codex.write_sessions(r["sessions"])
    assert stats2["duplicates"] >= 2, stats2
    passed += 1

    # ---- 2. codex -> server -> opencode --------------------------------
    storage = tmp / "opencode-storage"
    (storage / "session" / "info").mkdir(parents=True)
    opencode = OpencodeAdapter(storage_dir=storage)
    stats = opencode.write_sessions(codex.read_sessions())
    print("opencode local write (from codex local incl. hermes) ->", stats)
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
    assert any(k.startswith("codex:") for k in back), list(back)
    passed += 1

    # ---- 3. status endpoint reflects totals -----------------------------
    r = api_call(server, api_key, "GET", "/status/e2e-hermes-device")
    print("status ->", r)
    assert r.get("total_sessions", 0) == 2, r
    assert r.get("total_messages", 0) == 3, r  # hermes 2 msgs + codex 1 msg
    passed += 1

    print(f"\nALL {passed} E2E CHECKS PASSED")


if __name__ == "__main__":
    main()
