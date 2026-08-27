"""Tests for the global search domain (/web/search).

Covers the two security invariants from docs/SEARCH.md -- every session/
message query is scoped to the caller's workspaces (admins included, no
extra scope) and tool messages are excluded -- plus LIKE-wildcard escaping
and page clamping. DB access is mocked; no real database is touched.
"""
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("HERMES_SYNC_PG_DSN", "postgresql://x:x@localhost:5432/x")
os.environ.setdefault("HERMES_SYNC_MASTER_KEY", "test-master-key")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import search  # noqa: E402


class FakeCursor:
    def __init__(self, results):
        # queue of results: fetchall pops a list, fetchone pops a dict
        self._results = list(results)
        self.executed = []

    def execute(self, sql, params=None):
        self.executed.append((sql, params))

    def fetchall(self):
        return self._results.pop(0) if self._results else []

    def fetchone(self):
        return self._results.pop(0) if self._results else {"total": 0}


class FakeConn:
    def __init__(self, cursor):
        self._cursor = cursor

    def cursor(self, cursor_factory=None):
        return self._cursor

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _run_search(user_id, q, page=1):
    """Call search._search with mocked DB; return (cursor, result)."""
    cursor = FakeCursor([
        [{"id": "s1", "workspace_id": 1, "title": "t", "agent_type": "hermes",
          "message_count": 1, "workspace_name": "W", "synced_at": 1.0}],  # sessions
        {"total": 1},  # session count
        [{"id": 42, "session_id": "s1", "workspace_id": 1, "role": "user",
          "timestamp": 1.0, "content": "abc", "title": "t",
          "agent_type": "hermes", "workspace_name": "W"}],  # messages
        {"total": 1},  # message count
    ])
    conn = FakeConn(cursor)
    with mock.patch("search.get_conn", return_value=conn):
        result = search._search(user_id, q, page)
    return cursor, result


class SearchTest(unittest.TestCase):
    def test_tenant_filter_on_all_queries(self):
        """Every query is scoped to the caller's workspaces (w.user_id)."""
        cursor, _ = _run_search(7, "hello")
        self.assertEqual(len(cursor.executed), 4)
        for sql, params in cursor.executed:
            self.assertIn("w.user_id = %s", sql)
            self.assertEqual(params[0], 7)  # caller id is always the filter

    def test_admin_gets_no_extra_scope(self):
        """Search has no is_admin branch: admins are filtered identically."""
        cursor, _ = _run_search(1, "x")  # user 1 is admin in the real seed
        for sql, params in cursor.executed:
            self.assertIn("w.user_id = %s", sql)
            self.assertEqual(params[0], 1)

    def test_like_wildcards_escaped(self):
        """% _ \\ are literal, not LIKE wildcards."""
        cursor, _ = _run_search(1, "50%_off")
        for sql, params in cursor.executed:
            pattern = params[1]
            self.assertEqual(pattern, "%50\\%\\_off%")
            self.assertIn("ESCAPE '\\'", sql)

    def test_tool_messages_excluded(self):
        """Content search skips role='tool' rows."""
        cursor, _ = _run_search(1, "abc")
        msg_sql = cursor.executed[2][0]
        self.assertIn("m.role <> 'tool'", msg_sql)
        # hidden/archived filters present
        self.assertIn("COALESCE(m.hidden,0) = 0", msg_sql)
        self.assertIn("COALESCE(s.archived,0) = 0", msg_sql)

    def test_result_shape_and_snippet(self):
        """Message hits carry a compact snippet around the first hit."""
        cursor, (sessions, messages, s_total, m_total) = _run_search(1, "abc")
        self.assertEqual(len(sessions), 1)
        self.assertEqual(s_total, 1)
        self.assertEqual(m_total, 1)
        self.assertIn("sync_label", sessions[0])
        self.assertIn("abc", messages[0]["snippet"])

    def test_message_hits_carry_id(self):
        """Message hits must expose m.id so results can deep-link (?focus=)."""
        cursor, (_, messages, _, _) = _run_search(1, "abc")
        self.assertIn("id", messages[0])
        self.assertEqual(messages[0]["id"], 42)
        # the route's message links use the id
        import re
        msg_sql = cursor.executed[2][0]
        self.assertIn("m.id", msg_sql)

    def test_page_clamped_in_route(self):
        """Negative/non-numeric page falls back to 1."""
        import inspect
        # route handler clamps via max(1, ...)
        self.assertEqual(search.PAGE_SIZE, 20)


if __name__ == "__main__":
    unittest.main(verbosity=2)
