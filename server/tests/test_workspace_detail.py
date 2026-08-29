"""Regression test for the workspace-detail agent capsule filter.

The capsule row renders links for every agent (including the Oh My Pi
adapter), so the backend whitelist must keep those values and compile them
into the `agent_type = %s` clause. A whitelist that drops an agent back to
"all" makes its capsule silently show every session instead of only its own.
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
    def __init__(self, conn):
        self.conn = conn

    def __enter__(self):
        return self.conn

    def __exit__(self, *a):
        return False


class FakeQueryParams(dict):
    def get(self, key, default=None):
        return dict.get(self, key, default)


class FakeRequest:
    def __init__(self, params):
        self.query_params = FakeQueryParams(params)


class WorkspaceDetailAgentFilterTest(unittest.TestCase):
    def _render(self, agent):
        ws_cursor = FakeCursor(fetchone_results=[{"id": 1, "name": "ws"}])
        # Main block fetchone order: count, trash_count, new_24h.
        main_cursor = FakeCursor(fetchone_results=[
            {"cnt": 1}, {"cnt": 0}, {"cnt": 0},
        ])
        conns = [FakeCtx(FakeConn(ws_cursor)),
                 FakeCtx(FakeConn(main_cursor))]

        captured = {}

        async def fake_render(template_name, ctx):
            captured.update(ctx)
            return object()

        patchers = [
            mock.patch.object(workspace, "get_current_user",
                              return_value={"sub": 1, "is_admin": False}),
            mock.patch.object(workspace, "get_nav_workspaces", return_value=[]),
            mock.patch.object(workspace, "get_conn", side_effect=conns),
            mock.patch.object(workspace, "render_page", side_effect=fake_render),
        ]
        for p in patchers:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in patchers])

        params = dict(agent=agent) if agent != "all" else {}
        asyncio.run(workspace.web_workspace_detail(
            1, FakeRequest(params)))
        sql, count_params = main_cursor.executed[0]
        return captured, sql, count_params

    def test_omp_capsule_filters_by_agent_type(self):
        captured, sql, count_params = self._render("omp")
        self.assertEqual(captured["agent"], "omp")
        self.assertIn("agent_type = %s", sql)
        self.assertEqual(count_params, [1, "omp"])

    def test_unknown_agent_resets_to_all(self):
        captured, sql, count_params = self._render("nope")
        self.assertEqual(captured["agent"], "all")
        self.assertNotIn("agent_type = %s", sql)
        self.assertEqual(count_params, [1])


if __name__ == "__main__":
    unittest.main()
