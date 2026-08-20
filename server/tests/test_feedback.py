"""Regression tests for the user-feedback domain (/web/feedback).

Covers: submit inserts an 'open' row with normalized category, empty
title/description is rejected without a write, and the admin-only resolve
route toggles status (non-admins are bounced before touching the DB).
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
import feedback  # noqa: E402


class FakeCursor:
    def __init__(self, fetchone_result=None):
        self.fetchone_result = fetchone_result
        self.executed = []

    def execute(self, sql, params=None):
        self.executed.append((sql, params))

    def fetchone(self):
        return self.fetchone_result


class FakeConn:
    def __init__(self, cursor):
        self._cursor = cursor

    def cursor(self, cursor_factory=None):
        return self._cursor


class FakeCtx:
    """Stand-in for the get_conn() context manager."""

    def __init__(self, conn):
        self.conn = conn

    def __enter__(self):
        return self.conn

    def __exit__(self, *a):
        return False


class FakeRequest:
    def __init__(self, form_data):
        self._form = form_data

    async def form(self):
        return self._form


class FeedbackSubmitTest(unittest.TestCase):
    def _submit(self, form):
        cursor = FakeCursor()
        flashes = []

        def fake_flash(resp, message, category):
            flashes.append((message, category))
            return resp

        patchers = [
            mock.patch.object(feedback, "get_current_user",
                              return_value={"sub": 10, "is_admin": False}),
            mock.patch.object(feedback, "get_conn", return_value=FakeCtx(FakeConn(cursor))),
            mock.patch.object(feedback, "make_flash", side_effect=fake_flash),
        ]
        for p in patchers:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in patchers])
        resp = asyncio.run(feedback.web_feedback_submit(FakeRequest(form)))
        return resp, cursor, flashes

    def test_submit_inserts_open_row(self):
        resp, cursor, flashes = self._submit(
            {"title": "标题", "content": "内容", "category": "bug"})
        self.assertEqual(resp.status_code, 303)
        self.assertEqual(resp.headers["location"], "/web/feedback")
        sql, params = cursor.executed[0]
        self.assertIn("INSERT INTO feedback", sql)
        self.assertIn("'open'", sql)
        self.assertEqual(params[0], 10)       # user sub
        self.assertEqual(params[1], "标题")
        self.assertEqual(params[2], "内容")
        self.assertEqual(params[3], "bug")
        self.assertEqual(flashes[0][1], "success")

    def test_submit_rejects_empty_fields(self):
        resp, cursor, flashes = self._submit(
            {"title": "  ", "content": "", "category": "bug"})
        self.assertEqual(resp.status_code, 303)
        self.assertEqual(cursor.executed, [])  # no write
        self.assertEqual(flashes[0][1], "error")

    def test_submit_normalizes_unknown_category(self):
        resp, cursor, _ = self._submit(
            {"title": "t", "content": "c", "category": "invalid"})
        self.assertEqual(cursor.executed[0][1][3], "other")


class FeedbackResolveTest(unittest.TestCase):
    def _resolve(self, is_admin, fetchone_result=("open",)):
        cursor = FakeCursor(fetchone_result)
        patchers = [
            mock.patch.object(feedback, "get_current_user",
                              return_value={"sub": 1, "is_admin": is_admin}),
            mock.patch.object(feedback, "get_conn", return_value=FakeCtx(FakeConn(cursor))),
        ]
        for p in patchers:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in patchers])
        resp = asyncio.run(feedback.web_feedback_resolve(5, mock.MagicMock()))
        return resp, cursor

    def test_admin_resolves_open_feedback(self):
        resp, cursor = self._resolve(True, ("open",))
        self.assertEqual(resp.status_code, 303)
        updates = [sql for sql, _ in cursor.executed if "UPDATE feedback" in sql]
        self.assertTrue(updates)
        self.assertIn("'resolved'", updates[0])

    def test_admin_reopens_resolved_feedback(self):
        resp, cursor = self._resolve(True, ("resolved",))
        updates = [sql for sql, _ in cursor.executed if "UPDATE feedback" in sql]
        self.assertIn("'open'", updates[0])

    def test_non_admin_bounced_before_db(self):
        resp, cursor = self._resolve(False)
        self.assertEqual(resp.status_code, 303)
        self.assertEqual(resp.headers["location"], "/web/")
        self.assertEqual(cursor.executed, [])


if __name__ == "__main__":
    unittest.main()
