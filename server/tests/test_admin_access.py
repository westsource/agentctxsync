"""Regression test for /web/admin/access today-bucket computation.

The route builds `days` keyed by the `stat_date` column, which psycopg2
returns as a `datetime.date`. A previous bug looked up today's bucket with
`date.today().isoformat()` (a str), which never matches a date key, so the
top "today" cards silently rendered 0 even when the daily table below and
the all-time totals were non-zero.
"""
import asyncio
import os
import sys
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

os.environ.setdefault("HERMES_SYNC_PG_DSN", "postgresql://x:x@localhost:5432/x")
os.environ.setdefault("HERMES_SYNC_MASTER_KEY", "test-master-key")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import admin  # noqa: E402


class FakeCursor:
    def __init__(self, rows):
        self.rows = list(rows)

    def execute(self, sql, params=None):
        pass

    def fetchall(self):
        return self.rows


class FakeConn:
    def __init__(self, rows):
        self._rows = rows

    def cursor(self, cursor_factory=None):
        return FakeCursor(self._rows)


class FakeCtx:
    """Stand-in for the get_conn() context manager."""

    def __init__(self, conn):
        self.conn = conn

    def __enter__(self):
        return self.conn

    def __exit__(self, *a):
        return False


class AccessTodayTest(unittest.TestCase):
    def _render(self, rows):
        conn = FakeConn(rows)
        captured = {}

        async def fake_render(template_name, ctx):
            captured.update(ctx)
            return object()

        patchers = [
            mock.patch.object(admin, "get_current_user",
                              return_value={"sub": 1, "is_admin": True}),
            mock.patch.object(admin, "get_nav_workspaces", return_value=[]),
            mock.patch.object(admin, "get_conn", return_value=FakeCtx(conn)),
            mock.patch.object(admin, "render_page", side_effect=fake_render),
        ]
        for p in patchers:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in patchers])
        asyncio.run(admin.web_admin_access(mock.MagicMock()))
        return captured

    def test_today_buckets_show_real_counts(self):
        rows = [
            {"stat_date": date.today(), "channel": "domain", "kind": "web", "count": 4},
            {"stat_date": date.today(), "channel": "ip", "kind": "api", "count": 3},
        ]
        ctx = self._render(rows)
        self.assertEqual(ctx["today"]["web_domain"], 4)
        self.assertEqual(ctx["today"]["api_ip"], 3)
        self.assertEqual(ctx["total_web_domain"], 4)
        self.assertEqual(ctx["total_api_ip"], 3)

    def test_today_empty_when_no_rows(self):
        ctx = self._render([])
        self.assertEqual(ctx["today"]["total"], 0)
        self.assertEqual(ctx["today"]["web_domain"], 0)


if __name__ == "__main__":
    unittest.main()
