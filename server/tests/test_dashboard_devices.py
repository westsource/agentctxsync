"""Regression test for dashboard device-card scoping.

Admins see every device across the system (no user filter); non-admins see
only their own workspaces' devices. The card value and the modal both read
the same `devices` list, so this pins the query scope the route builds.
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
    def __init__(self, fetchone_results=(), fetchall_results=()):
        self.fetchone_results = list(fetchone_results)
        self.fetchall_results = list(fetchall_results)
        self.executed = []

    def execute(self, sql, params=None):
        self.executed.append((sql, params))

    def fetchone(self):
        return self.fetchone_results.pop(0) if self.fetchone_results else None

    def fetchall(self):
        return self.fetchall_results


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


DEVICE_ROW = {
    "device_id": "d1", "workspace_id": 1, "last_sync_at": 1.0,
    "sessions_synced": 3, "messages_synced": 7,
    "workspace_name": "ws", "user_display_name": "u1",
}


class DashboardDeviceScopeTest(unittest.TestCase):
    def _render(self, user):
        # Three get_conn() blocks: quota, recent_sessions, devices.
        quota_cursor = FakeCursor(fetchone_results=[(2,), (29,)])
        recent_cursor = FakeCursor(fetchall_results=[])
        devices_cursor = FakeCursor(fetchall_results=[dict(DEVICE_ROW)])
        conns = [FakeCtx(FakeConn(quota_cursor)),
                 FakeCtx(FakeConn(recent_cursor)),
                 FakeCtx(FakeConn(devices_cursor))]

        captured = {}

        async def fake_render(template_name, ctx):
            captured.update(ctx)
            return object()

        patchers = [
            mock.patch.object(workspace, "get_current_user", return_value=user),
            mock.patch.object(workspace, "get_nav_workspaces", return_value=[]),
            mock.patch.object(workspace, "get_user_workspaces", return_value=[]),
            mock.patch.object(workspace, "get_conn", side_effect=conns),
            mock.patch.object(workspace, "quota_ui_active", return_value=False),
            mock.patch.object(workspace, "render_page", side_effect=fake_render),
        ]
        for p in patchers:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in patchers])
        asyncio.run(workspace.web_dashboard(mock.MagicMock()))
        sql, params = devices_cursor.executed[0]
        return captured, sql, params

    def test_admin_sees_all_devices(self):
        captured, sql, params = self._render({"sub": 1, "is_admin": True})
        self.assertNotIn("WHERE w.user_id", sql)
        self.assertEqual(params, ())
        self.assertEqual(len(captured["devices"]), 1)

    def test_non_admin_sees_own_devices(self):
        captured, sql, params = self._render({"sub": 10, "is_admin": False})
        self.assertIn("WHERE w.user_id = %s", sql)
        self.assertEqual(params, (10,))
        self.assertEqual(len(captured["devices"]), 1)


if __name__ == "__main__":
    unittest.main()
