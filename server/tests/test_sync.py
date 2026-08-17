"""/push and /pull behavioral tests with a scripted fake DB.

These are the regression net for the sync hot path: they pin down the
observable contract (counters, dedup semantics, agent_type/hidden
protection, id allocation, pull filtering/merging). The fake DB routes
by SQL shape only, so the batch-optimization rewrite keeps the same
assertions.
"""
import asyncio
import os
import re
import sys
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("HERMES_SYNC_PG_DSN", "postgresql://x:x@localhost:5432/x")
os.environ.setdefault("HERMES_SYNC_MASTER_KEY", "test-master-key")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import sync  # noqa: E402


class ScriptedCursor:
    """Routes execute() by SQL regex to scripted results; records everything."""

    def __init__(self):
        self.executed = []
        self.rowcount = 0
        self._scripts = []
        self._next = iter(())

    def add(self, pattern, rows=None, rowcount=None, key=None):
        """Route by regex; `key` (callable(params)->lookup key) selects from
        `rows` when given as a {lookup_key: rows} dict."""
        self._scripts.append((re.compile(pattern), rows, rowcount, key))
        return self

    def execute(self, sql, params=None):
        self.executed.append((sql, params))
        self.rowcount = 0
        for pat, rows, rc, key in self._scripts:
            if pat.search(sql):
                if rc is not None:
                    self.rowcount = rc
                if rows is None:
                    self._next = iter(())
                elif key is not None:
                    self._next = iter(rows.get(key(params), ()))
                else:
                    self._next = iter(rows or ())
                return
        self._next = iter(())

    def fetchone(self):
        return next(self._next, None)

    def fetchall(self):
        return list(self._next)


class FakeConn:
    def __init__(self, cursor):
        self._cursor = cursor

    def cursor(self, *a, **k):
        return self._cursor

    def commit(self):
        pass

    def rollback(self):
        pass


class FakeCtx:
    """Stand-in for the get_conn() context manager."""

    def __init__(self, conn):
        self.conn = conn

    def __enter__(self):
        return self.conn

    def __exit__(self, *a):
        return False


class JsonRequest:
    def __init__(self, payload):
        self._payload = payload

    async def json(self):
        return self._payload


SESS_COLS = ("id", "workspace_id", "title", "agent_type", "meta", "hidden",
             "pinned", "profile_name", "last_synced_at", "archived")
MSG_COLS = ("id", "session_id", "workspace_id", "role", "content", "timestamp",
            "agent_type", "meta", "hidden")


def run(coro):
    return asyncio.run(coro)


def insert_rows(cur, table):
    """INSERT INTO <table> (cols) VALUES ... -> list of {col: value} dicts.

    Only works for single-row statements (sessions); multi-row batched
    inserts embed their values in the SQL text (execute_values).
    """
    rows = []
    for sql, params in cur.executed:
        if not sql.startswith(f"INSERT INTO {table}") or params is None:
            continue
        m = re.search(r"\(([^)]+)\) VALUES", sql)
        cols = [c.strip() for c in m.group(1).split(",")]
        rows.append(dict(zip(cols, params)))
    return rows


def update_set_cols(cur, table):
    """Column names in `UPDATE <table> SET a = %s, b = %s WHERE ...`."""
    cols = []
    for sql, _ in cur.executed:
        if sql.startswith(f"UPDATE {table}"):
            m = re.search(r"SET (.+?) WHERE", sql)
            cols = [c.split("=")[0].strip() for c in m.group(1).split(",")]
    return cols


def fake_execute_values(cur, sql, argslist, template=None, page_size=100, fetch=False):
    """Stand-in for psycopg2.extras.execute_values: record the batched rows
    and emit the statement with an empty RETURNING (so the push's conflict
    retry path lands each row via the scripted cursor)."""
    cur.executed.append((sql, None))
    cur.rowcount = len(argslist)
    cur.batch_rows = list(argslist)
    cur._next = iter(())


class PushTest(unittest.TestCase):
    def _push(self, sessions, existing_ids=(), msg_keys=(), msg_contents=(),
              next_id=5, ws=None):
        """msg_keys: existing (sid, role, timestamp) triples on the server.
        msg_contents: existing (sid, role, content) triples on the server."""
        content_by_sid = {}
        for sid, role, content in msg_contents:
            content_by_sid.setdefault(sid, []).append((role, content))
        cur = (ScriptedCursor()
               .add(r"SELECT id FROM sessions", [(i,) for i in existing_ids])
               .add(r"information_schema\.columns.*sessions",
                    [(c,) for c in SESS_COLS])
               .add(r"information_schema\.columns.*messages",
                    [(c,) for c in MSG_COLS])
               # batched dedup-key prefetch (one query for the whole push)
               .add(r"SELECT session_id, role, timestamp FROM messages",
                    list(msg_keys))
               # batched next-id prefetch per session (GROUP BY)
               .add(r"GROUP BY session_id",
                    [(s["id"], next_id) for s in sessions])
               # lazy content fallback map, fetched per session
               .add(r"SELECT role, content FROM messages", content_by_sid,
                    key=lambda p: p[1])
               # legacy id-based dedup check (role/timestamp missing)
               .add(r"SELECT 1 FROM messages", [])
               # conflict retry (single-row, %s placeholders) always lands
               .add(r"INSERT INTO messages.*VALUES \(%s", rows=[("s1", 0)])
               # batched multi-row insert: nothing pre-exists (RETURNING empty)
               .add(r"INSERT INTO messages", rows=[]))
        conn = FakeConn(cur)
        with mock.patch.object(sync, "get_conn", return_value=FakeCtx(conn)), \
             mock.patch("psycopg2.extras.execute_values",
                        side_effect=fake_execute_values):
            resp = run(sync.push(JsonRequest({"device_id": "dev1",
                                             "sessions": sessions}),
                                ws or {"workspace_id": 1, "user_id": None}))
        return resp, cur

    def test_new_session_and_messages_inserted(self):
        sessions = [{"id": "s1", "title": "hi",
                     "messages": [{"role": "user", "content": "a", "timestamp": 1.0},
                                  {"role": "assistant", "content": "b", "timestamp": 2.0}]}]
        resp, cur = self._push(sessions)
        self.assertEqual(resp["imported"], 1)
        self.assertEqual(resp["updated"], 0)
        self.assertEqual(resp["new_messages"], 2)
        self.assertEqual(resp["duplicates"], 0)
        # batched multi-row insert with RETURNING happened (conflict retries
        # are single-row "VALUES (%s" statements and are excluded)
        batch = [s for s, _ in cur.executed
                 if s.startswith("INSERT INTO messages")
                 and "RETURNING session_id, id" in s
                 and "VALUES (%s" not in s]
        self.assertEqual(len(batch), 1)
        # both messages went into the single batch (not per-message inserts)
        self.assertEqual(len(cur.batch_rows), 2)
        # auto_id path: the id column is allocated client-side (in the batch)
        m = re.search(r"INSERT INTO messages \(([^)]+)\)", batch[0])
        self.assertIn("id", [c.strip() for c in m.group(1).split(",")])
        # session row carries agent_type + workspace_id
        sess_rows = insert_rows(cur, "sessions")
        self.assertEqual(len(sess_rows), 1)
        self.assertEqual(sess_rows[0]["agent_type"], "hermes")
        self.assertEqual(sess_rows[0]["workspace_id"], 1)
        # sync_state upsert always happens
        self.assertTrue(any(s.startswith("INSERT INTO sync_state") for s, _ in cur.executed))

    def test_existing_session_updates_never_touch_agent_type_or_hidden(self):
        sessions = [{"id": "s1", "title": "new title", "agent_type": "codex",
                     "hidden": 1, "messages": []}]
        resp, cur = self._push(sessions, existing_ids=("s1",))
        self.assertEqual(resp["updated"], 1)
        self.assertEqual(resp["imported"], 0)
        cols = update_set_cols(cur, "sessions")
        self.assertNotIn("agent_type", cols)
        self.assertNotIn("hidden", cols)
        self.assertIn("last_synced_at", cols)

    def test_duplicate_message_deduped_by_triple(self):
        sessions = [{"id": "s1", "title": "t",
                     "messages": [{"role": "user", "content": "dup", "timestamp": 1.0},
                                  {"role": "user", "content": "dup", "timestamp": 1.0}]}]
        # both messages collide with the already-present (session, role, timestamp)
        resp, cur = self._push(sessions, msg_keys={("s1", "user", 1.0)})
        self.assertEqual(resp["new_messages"], 0)
        self.assertEqual(resp["duplicates"], 2)
        self.assertFalse(any(s.startswith("INSERT INTO messages") for s, _ in cur.executed))

    def test_content_fallback_dedupes_rebuilt_session(self):
        # same role+content but a regenerated timestamp -> still a duplicate
        sessions = [{"id": "s1", "title": "t",
                     "messages": [{"role": "user", "content": "same", "timestamp": 99.0}]}]
        resp, cur = self._push(sessions, msg_keys={("s1", "user", 1.0)},
                               msg_contents={("s1", "user", "same")})
        self.assertEqual(resp["new_messages"], 0)
        self.assertEqual(resp["duplicates"], 1)
        self.assertFalse(any(s.startswith("INSERT INTO messages") for s, _ in cur.executed))

    def test_quota_gate_skipped_for_master_key(self):
        # user_id None (master key) -> no quota queries at all
        sessions = [{"id": "s1", "messages": []}]
        resp, cur = self._push(sessions)
        self.assertEqual(resp["imported"], 1)
        self.assertFalse(any("quota_config" in s for s, _ in cur.executed))
        self.assertFalse(any("FROM users" in s for s, _ in cur.executed))


class PullTest(unittest.TestCase):
    def _pull(self, body, sessions, messages_by_sid):
        cur = (ScriptedCursor()
               .add(r"SELECT COUNT\(\*\) AS cnt", [{"cnt": 2}])
               .add(r"FROM sessions", sessions)
               # single batched message query for the whole page (ANY)
               .add(r"FROM messages",
                    {"all": [dict(m, session_id=sid)
                             for sid, ms in messages_by_sid.items() for m in ms]},
                    key=lambda p: "all"))
        conn = FakeConn(cur)
        with mock.patch.object(sync, "get_conn", return_value=FakeCtx(conn)):
            return run(sync.pull(JsonRequest(body), {"workspace_id": 1, "user_id": None})), cur

    def test_incremental_pull_filters_and_merges(self):
        sessions = [{"id": "a", "title": "A", "started_at": 10.0},
                    {"id": "b", "title": "B", "started_at": 9.0}]
        msgs = {"a": [{"role": "user", "content": "a1", "timestamp": 1.0},
                      {"role": "assistant", "content": "a2", "timestamp": 2.0}],
                "b": [{"role": "user", "content": "b1", "timestamp": 1.5}]}
        resp, cur = self._pull({"device_id": "d", "last_sync_at": 5.0}, sessions, msgs)
        self.assertEqual(resp["session_count"], 2)
        self.assertEqual(resp["total_sessions"], 2)
        self.assertEqual(resp["message_count"], 3)
        by_id = {s["id"]: s for s in resp["sessions"]}
        self.assertEqual([m["content"] for m in by_id["a"]["messages"]], ["a1", "a2"])
        self.assertEqual([m["content"] for m in by_id["b"]["messages"]], ["b1"])
        # incremental cutoff is applied to the session query
        sess_sql = [s for s, _ in cur.executed if "FROM sessions" in s and "ORDER BY" in s][0]
        self.assertIn("last_synced_at >", sess_sql)

    def test_agent_filter_applied_to_session_query(self):
        sessions = [{"id": "a", "title": "A"}]
        resp, cur = self._pull({"device_id": "d", "agent": "codex"}, sessions, {})
        sess_sql = [s for s, _ in cur.executed if "FROM sessions" in s and "ORDER BY" in s][0]
        self.assertIn("agent_type = %s", sess_sql)
        self.assertIn("codex", [p for s, p in cur.executed if "FROM sessions" in s and "ORDER BY" in s][0])


if __name__ == "__main__":
    unittest.main()
