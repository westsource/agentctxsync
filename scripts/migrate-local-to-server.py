#!/usr/bin/env python3
"""
Agent Context Sync - SQLite to PostgreSQL Migration Script
Migrates local Hermes state.db sessions to the remote sync server.
Usage:
  python migrate-local-to-server.py <workspace_api_key> [server_url]
"""

import sqlite3
import json
import sys
import time
import urllib.request
import urllib.error
import platform
from pathlib import Path

# Config
SYNC_SERVER = sys.argv[2] if len(sys.argv) > 2 else "http://localhost:8765"
SYNC_API_KEY = sys.argv[1] if len(sys.argv) > 1 else None
HERMES_DIR = Path.home() / "AppData/Local/hermes"
LOCAL_DB = HERMES_DIR / "state.db"
DEVICE_ID = f"local-{platform.node()}"

if not SYNC_API_KEY:
    print("Usage: python migrate-local-to-server.py <workspace_api_key> [server_url]")
    print("  Get your workspace API key from your sync server web UI (http://<SERVER>:8765/web/)")
    sys.exit(1)


def api_call(method, path, data=None):
    url = f"{SYNC_SERVER}{path}"
    headers = {"Authorization": f"Bearer {SYNC_API_KEY}", "Content-Type": "application/json"}
    body = json.dumps(data).encode() if data else None
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        resp = urllib.request.urlopen(req, timeout=30)
        return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        return {"error": e.code, "detail": e.read().decode()}
    except Exception as e:
        return {"error": str(e)}


def migrate():
    if not LOCAL_DB.exists():
        print(f"ERROR: Local DB not found: {LOCAL_DB}")
        sys.exit(1)

    conn = sqlite3.connect(str(LOCAL_DB))
    conn.row_factory = sqlite3.Row
    c = conn.cursor()

    print(f"Server: {SYNC_SERVER}")
    print(f"Local DB: {LOCAL_DB}")
    print(f"Device ID: {DEVICE_ID}")

    # Verify API key
    status = api_call("GET", f"/status/{DEVICE_ID}")
    if "error" in status:
        print(f"ERROR: API key rejected - {status}")
        print("Make sure you are using a workspace API key (starts with 'hsk_')")
        sys.exit(1)
    print(f"Connected to workspace. Status: {status}")

    # Count local data
    c.execute("SELECT COUNT(*) FROM sessions")
    total_sessions = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM messages")
    total_messages = c.fetchone()[0]
    print(f"Local: {total_sessions} sessions, {total_messages} messages")

    # Get all sessions
    c.execute("SELECT * FROM sessions ORDER BY started_at DESC")
    local_cols = {row[1] for row in c.execute("PRAGMA table_info(sessions)").fetchall()}

    sessions_data = []
    for row in c.fetchall():
        s = dict(row)
        sid = s["id"]
        c.execute("SELECT * FROM messages WHERE session_id = ? ORDER BY timestamp", (sid,))
        messages = [dict(m) for m in c.fetchall()]
        s_filtered = {k: v for k, v in s.items() if k in local_cols}
        s_filtered["messages"] = messages
        sessions_data.append(s_filtered)

    conn.close()

    # Push in batches
    batch_size = 10
    for i in range(0, len(sessions_data), batch_size):
        batch = sessions_data[i:i+batch_size]
        result = api_call("POST", "/push", {
            "device_id": DEVICE_ID,
            "sessions": batch
        })
        imported = result.get("imported", 0)
        updated = result.get("updated", 0)
        new_msgs = result.get("new_messages", 0)
        print(f"  Batch {i//batch_size + 1}: +{imported} sessions, ~{updated} updated, +{new_msgs} messages")

    # Verify
    status = api_call("GET", f"/status/{DEVICE_ID}")
    print(f"\nRemote: {status.get('total_sessions', 0)} sessions, {status.get('total_messages', 0)} messages")
    print("Migration complete!")


if __name__ == "__main__":
    migrate()
