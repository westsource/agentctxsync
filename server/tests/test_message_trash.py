"""Message trash: single & bulk restore routes (workspace ownership guard).

Covers web_message_unhide_all: workspace ownership is checked before any
UPDATE; the bulk route restores every hidden message of the session and
flashes the restored count.
"""
import asyncio
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("HERMES_SYNC_PG_DSN", "postgresql://x:x@localhost:5432/x")
os.environ.setdefault("HERMES_SYNC_MASTER_KEY", "test-master-key")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import workspace  # noqa: E402


class FakeCursor:
    def __init__(self, fetchone_results=(), rowcount=3):
        self.fetchone_results = list(fetchone_results)
        self.rowcount = rowcount
        self.executed = []

    def execute(self, sql, params=None):
        self.executed.append((sql, params))

    def fetchone(self):
        return self.fetchone_results.pop(0) if self.fetchone_results else None


class FakeConn:
    def __init__(self, cursor):
        self._cursor = cursor

    def cursor(self, *a, **k):
        return self._cursor

    def commit(self):
        pass


class FakeCtx:
    def __init__(self, conn):
        self.conn = conn

    def __enter__(self):
        return self.conn

    def __exit__(self, *a):
        return False


class MessageUnhideAllTest(unittest.TestCase):
    def _call(self, user=None, owned=True):
        cursor = FakeCursor([(1,) if owned else None], rowcount=5)
        conn = FakeConn(cursor)
        captured = {}

        def fake_flash(response, message, category="success"):
            captured["flash"] = message

        patchers = [
            mock.patch.object(workspace, "get_current_user",
                              return_value=user,
                              side_effect=Exception("no user") if user is None else None),
            mock.patch.object(workspace, "get_conn", return_value=FakeCtx(conn)),
            mock.patch.object(workspace, "get_lang", return_value="zh-CN"),
            mock.patch.object(workspace, "get_translations",
                              return_value={"msg_unhide_all_ok": "已恢复 {0} 条消息"}),
            mock.patch.object(workspace, "make_flash", side_effect=fake_flash),
        ]
        for p in patchers:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in patchers])
        return asyncio.run(workspace.web_message_unhide_all(
            1, "s1", mock.MagicMock())), cursor, captured

    def test_not_logged_in_redirects_login(self):
        resp, _, _ = self._call(user=None)
        # RedirectResponse without explicit status_code defaults to 307.
        self.assertEqual(resp.status_code, 307)
        self.assertEqual(resp.headers["location"], "/web/login")

    def test_not_owner_redirects_dashboard_no_update(self):
        resp, cursor, _ = self._call(user={"sub": "9"}, owned=False)
        self.assertEqual(resp.status_code, 303)
        self.assertEqual(resp.headers["location"], "/web/")
        # Ownership SELECT ran, but no UPDATE.
        self.assertEqual(len(cursor.executed), 1)
        self.assertIn("SELECT id FROM workspaces", cursor.executed[0][0])

    def test_bulk_restore_updates_hidden_only(self):
        resp, cursor, captured = self._call(user={"sub": "7"}, owned=True)
        self.assertEqual(resp.status_code, 303)
        self.assertEqual(resp.headers["location"], "/web/workspace/1/session/s1")
        sql, params = cursor.executed[1]
        self.assertIn("UPDATE messages SET hidden = 0, hidden_at = NULL", sql)
        self.assertIn("hidden = 1", sql)
        self.assertEqual(params, (1, "s1"))
        # Flash carries the restored count.
        self.assertEqual(captured["flash"], "已恢复 5 条消息")


if __name__ == "__main__":
    unittest.main()
