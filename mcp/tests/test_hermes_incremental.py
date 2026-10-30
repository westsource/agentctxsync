"""Incremental reads and the optional Hermes-backend write path.

Covers:
* ``read_session_headers`` / ``session_fingerprints`` / ``read_sessions(ids=)``
  -- the push inventory answers from SQL aggregates instead of materializing
  every message (a 700 MB state.db made that a multi-second read whose
  snapshot also blocked the host's WAL from resetting).
* Sub-agent folding: a child's messages must change the ROOT's fingerprint and
  a partial read must pull the root and its subtree together.
* New sessions going through ``HERMES_SYNC_HERMES_API`` (Hermes' own
  ``POST /api/sessions/import``) with a local-SQLite fallback; existing
  sessions keep the local append path because Hermes' import skips ids that
  already exist.
"""
import json
import os
import sqlite3
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from adapters.hermes import HermesAdapter


def make_state_db(path: Path, rows, *, parent=None):
    """Minimal hermes state.db: ``rows`` = [(sid, [(role, content, ts)])]."""
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.execute("""CREATE TABLE IF NOT EXISTS sessions (
        id TEXT PRIMARY KEY, title TEXT, source TEXT, model TEXT,
        started_at REAL, message_count INTEGER, profile_name TEXT,
        parent_session_id TEXT)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, role TEXT,
        content TEXT, timestamp REAL)""")
    for sid, msgs in rows:
        conn.execute("INSERT INTO sessions (id, title, source, model, "
                     "started_at, message_count, profile_name, "
                     "parent_session_id) VALUES (?,?,?,?,?,?,?,?)",
                     (sid, f"t-{sid}", "cli", "m", 1000.0, len(msgs), "",
                      (parent or {}).get(sid)))
        for role, content, ts in msgs:
            conn.execute("INSERT INTO messages (session_id, role, content, "
                         "timestamp) VALUES (?,?,?,?)",
                         (sid, role, content, ts))
    conn.commit()
    conn.close()


class _StubBackend(BaseHTTPRequestHandler):
    """Records import requests; answers with ``status``/``payload``."""

    status = 200
    payload = {"ok": True, "imported": 1, "skipped": 0,
               "imported_ids": [], "skipped_ids": [], "errors": []}
    requests = []

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")
        type(self).requests.append((self.path, body,
                                    self.headers.get("X-Hermes-Session-Token")))
        data = json.dumps(self.payload).encode()
        self.send_response(type(self).status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


class _AdapterCase(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        patch = mock.patch.object(HermesAdapter, "_platform_root",
                                  return_value=self.root)
        patch.start()
        self.addCleanup(patch.stop)
        env = mock.patch.dict(os.environ, {"LOCALAPPDATA": "",
                                           "HERMES_SYNC_HERMES_API": ""},
                              clear=False)
        env.start()
        self.addCleanup(env.stop)
        _StubBackend.requests = []
        _StubBackend.status = 200
        _StubBackend.payload = {"ok": True, "imported": 1, "skipped": 0,
                                "imported_ids": [], "skipped_ids": [],
                                "errors": []}

    def default_db(self, rows, parent=None):
        make_state_db(self.root / "state.db", rows, parent=parent)


class IncrementalReadTest(_AdapterCase):
    def test_headers_carry_no_messages_and_fingerprints_match_full_read(self):
        self.default_db([("s1", [("user", "hi", 10.0),
                                 ("assistant", "yo", 11.0)]),
                         ("s2", [("user", "x", 20.0)])])
        a = HermesAdapter()
        headers = a.read_session_headers()
        self.assertEqual({h["id"] for h in headers}, {"s1", "s2"})
        self.assertTrue(all("messages" not in h for h in headers))

        fps = a.session_fingerprints()
        self.assertEqual(fps["s1"], (2, 11.0))
        self.assertEqual(fps["s2"], (1, 20.0))
        for s in a.read_sessions():
            self.assertEqual(fps[s["id"]],
                             (len(s["messages"]),
                              max(m["timestamp"] for m in s["messages"])))

    def test_partial_read_returns_only_requested_sessions(self):
        self.default_db([("s1", [("user", "a", 1.0)]),
                         ("s2", [("user", "b", 2.0)]),
                         ("s3", [("user", "c", 3.0)])])
        a = HermesAdapter()
        got = a.read_sessions(ids=["s1", "s3"])
        self.assertEqual({s["id"] for s in got}, {"s1", "s3"})
        self.assertEqual(a.read_sessions(ids=[]), [])

    def test_child_change_rolls_into_root_fingerprint_and_read(self):
        """A sub-agent's own messages live under the child id; the push emits
        the folded ROOT, so the root fingerprint must cover the subtree."""
        self.default_db([("root", [("user", "a", 1.0),
                                   ("assistant", "b", 2.0)]),
                         ("child", [("user", "c", 5.0)])],
                        parent={"child": "root"})
        a = HermesAdapter()
        fps = a.session_fingerprints()
        self.assertEqual(fps, {"root": (3, 5.0)})

        folded = a.read_sessions(ids=["root"])
        self.assertEqual([s["id"] for s in folded], ["root"])
        self.assertEqual(len(folded[0]["messages"]), 3)
        self.assertEqual(folded[0]["messages"][-1]["session_id"], "root")

        # Requesting only the child still yields the root it folds into.
        by_child = a.read_sessions(ids=["child"])
        self.assertEqual([s["id"] for s in by_child], ["root"])


class ApiWriteTest(_AdapterCase):
    def setUp(self):
        super().setUp()
        self.default_db([("kept", [("user", "old", 1.0)])])
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _StubBackend)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.shutdown)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def _session(self, sid, profile=None):
        s = {"id": sid, "started_at": 1000.0, "title": f"t-{sid}",
             "agent_type": "hermes",
             "messages": [{"session_id": sid, "role": "user",
                           "content": "hello", "timestamp": 1000.5}]}
        if profile:
            s["profile_name"] = profile
        return s

    def _row_count(self, sid):
        conn = sqlite3.connect(str(self.root / "state.db"))
        try:
            return conn.execute("SELECT COUNT(*) FROM sessions WHERE id = ?",
                                (sid,)).fetchone()[0]
        finally:
            conn.close()

    def test_new_session_is_imported_by_hermes_and_not_written_locally(self):
        with mock.patch.dict(os.environ,
                             {"HERMES_SYNC_HERMES_API": self.base}):
            stats = HermesAdapter().write_sessions([self._session("new1")])
        self.assertEqual(stats["imported"], 1)
        self.assertEqual(len(_StubBackend.requests), 1)
        path, body, _token = _StubBackend.requests[0]
        self.assertEqual(path, "/api/sessions/import")
        self.assertEqual(body["expected_revision"], 0)
        self.assertIsNone(body["profile"])
        self.assertTrue(body["request_id"])
        self.assertEqual([s["id"] for s in body["sessions"]], ["new1"])
        self.assertEqual(self._row_count("new1"), 0)

    def test_existing_session_appends_locally_without_calling_hermes(self):
        with mock.patch.dict(os.environ,
                             {"HERMES_SYNC_HERMES_API": self.base}):
            stats = HermesAdapter().write_sessions([self._session("kept")])
        self.assertEqual(_StubBackend.requests, [])
        self.assertEqual(stats["imported"] + stats["updated"], 1)
        self.assertEqual(self._row_count("kept"), 1)

    def test_api_failure_falls_back_to_the_local_writer(self):
        _StubBackend.status = 500
        with mock.patch.dict(os.environ,
                             {"HERMES_SYNC_HERMES_API": self.base}):
            stats = HermesAdapter().write_sessions([self._session("new2")])
        self.assertEqual(len(_StubBackend.requests), 1)
        self.assertEqual(self._row_count("new2"), 1)
        self.assertEqual(stats["imported"], 1)

    def test_named_profile_is_sent_as_the_profile(self):
        magic = self.root / "profiles" / "magic"
        make_state_db(magic / "state.db", [("exists-in-magic",
                                            [("user", "x", 1.0)])])
        with mock.patch.dict(os.environ,
                             {"HERMES_SYNC_HERMES_API": self.base}):
            HermesAdapter().write_sessions(
                [self._session("new3", profile="magic")])
        self.assertEqual(_StubBackend.requests[0][1]["profile"], "magic")


if __name__ == "__main__":
    unittest.main()


class GatewayWriteTest(_AdapterCase):
    """The gateway-RPC write path (Desktop: the gateway owns the store)."""

    def setUp(self):
        super().setUp()
        self.default_db([("kept", [("user", "old", 1.0)])])
        self.calls = []

        def fake_import(home, profile_home, sessions):
            self.calls.append({"home": str(home), "profile_home": str(profile_home),
                               "sessions": sessions})
            return {"ok": True, "supported": True,
                    "imported": len(sessions), "failed": []}

        patches = [mock.patch("adapters.hermes.gateway_import_sessions",
                              side_effect=fake_import),
                   mock.patch("adapters.hermes.gateway_writes_enabled",
                              return_value=True)]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def _session(self, sid, title="t"):
        return {"id": sid, "started_at": 1000.0, "title": title,
                "agent_type": "hermes",
                "messages": [{"session_id": sid, "role": "user",
                              "content": "hello", "timestamp": 1000.5}]}

    def _row_count(self, sid):
        conn = sqlite3.connect(str(self.root / "state.db"))
        try:
            return conn.execute("SELECT COUNT(*) FROM sessions WHERE id = ?",
                                (sid,)).fetchone()[0]
        finally:
            conn.close()

    def test_new_session_is_imported_through_the_gateway(self):
        stats = HermesAdapter().write_sessions([self._session("new1", "fresh")])
        self.assertEqual(stats["imported"], 1)
        self.assertEqual(self._row_count("new1"), 0)
        self.assertEqual([s["id"] for s in self.calls[0]["sessions"]], ["new1"])
        self.assertEqual(self.calls[0]["sessions"][0]["title"], "fresh")

    def test_existing_session_never_reaches_the_gateway(self):
        stats = HermesAdapter().write_sessions([self._session("kept")])
        self.assertEqual(self.calls, [])
        self.assertEqual(stats["updated"], 1)

    def test_title_collision_is_disambiguated_before_import(self):
        self.default_db([("other", [("user", "x", 1.0)])])
        conn = sqlite3.connect(str(self.root / "state.db"))
        conn.execute("UPDATE sessions SET title = 'dup' WHERE id = 'other'")
        conn.commit()
        conn.close()
        HermesAdapter().write_sessions([self._session("new2", "dup")])
        self.assertEqual(self.calls[0]["sessions"][0]["title"], "dup (2)")

    def test_gateway_refusal_falls_back_to_the_local_writer(self):
        with mock.patch("adapters.hermes.gateway_import_sessions",
                        return_value={"ok": False, "supported": False,
                                      "reason": "gateway_absent"}):
            stats = HermesAdapter().write_sessions([self._session("new3")])
        self.assertEqual(self._row_count("new3"), 1)
        self.assertEqual(stats["imported"], 1)

    def test_sessions_the_gateway_reported_failed_are_written_locally(self):
        with mock.patch("adapters.hermes.gateway_import_sessions",
                        return_value={"ok": True, "supported": True,
                                      "imported": 0, "failed": ["new4"]}):
            HermesAdapter().write_sessions([self._session("new4")])
        self.assertEqual(self._row_count("new4"), 1)

    def test_off_switch_never_calls_the_gateway(self):
        with mock.patch("adapters.hermes.gateway_writes_enabled",
                        return_value=False):
            HermesAdapter().write_sessions([self._session("new5")])
        self.assertEqual(self.calls, [])
        self.assertEqual(self._row_count("new5"), 1)
