"""Case-insensitive session<->project association test (S1 fix).

Windows drive letters/paths are case-insensitive (`d:` == `D:`), but a plain
SQL `=`/`LIKE` is case-sensitive, so a session whose stored `cwd` casing
differs from the project folder path (e.g. lowercase drive letter) was hidden
from the project's "关联会话" on the server web. The match helper must fold
case and avoid LIKE wildcard chars.
"""
import asyncio
import json
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

import psycopg2

os.environ.setdefault("HERMES_SYNC_PG_DSN", "postgresql://x:x@localhost:5432/x")
os.environ.setdefault("HERMES_SYNC_MASTER_KEY", "test-master-key")
os.environ.setdefault("HERMES_SYNC_JWT_SECRET", "test-jwt-secret")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import workspace  # noqa: E402


class FakeCursor:
    """Rows queued per fetchone(); an empty queue raises like psycopg2 does
    for a statement that returned no result set (e.g. an INSERT without
    RETURNING) -- that shape is the regression under test."""

    def __init__(self, fetchone_results=()):
        self.fetchone_results = list(fetchone_results)
        self.executed = []
        self.rowcount = 1

    def execute(self, sql, params=None):
        self.executed.append((sql, params))

    def fetchone(self):
        if not self.fetchone_results:
            raise psycopg2.ProgrammingError("no results to fetch")
        return self.fetchone_results.pop(0)


class FakeConn:
    def __init__(self, cursor):
        self._cursor = cursor
        self.rolled_back = False

    def cursor(self, *a, **k):
        return self._cursor

    def rollback(self):
        self.rolled_back = True


class FakeCtx:
    """Stand-in for the get_conn() context manager."""

    def __init__(self, conn):
        self.conn = conn

    def __enter__(self):
        return self.conn

    def __exit__(self, *exc):
        return False


class FakeRequest:
    def __init__(self, raw):
        self._raw = raw

    async def json(self):
        return json.loads(self._raw)


class ApiCreateWorkspaceTest(unittest.TestCase):
    """POST /api/workspaces must answer with the id of the workspace it made.

    It used to INSERT without RETURNING and then call fetchone() twice outside
    the connection block, so every request raised
    psycopg2.ProgrammingError("no results to fetch") *after* the row had
    already been committed: clients got a 500 while the workspace appeared --
    and retrying minted duplicates."""

    def _create(self, rows=((7,),)):
        cursor = FakeCursor(rows)
        conn = FakeConn(cursor)
        patchers = [
            mock.patch.object(workspace, "get_conn", return_value=FakeCtx(conn)),
            mock.patch.object(workspace, "generate_api_key", return_value="ws_new"),
            mock.patch.object(workspace, "email_action_allowed", return_value=True),
        ]
        for p in patchers:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in patchers])
        result = asyncio.run(workspace.api_create_workspace(
            FakeRequest('{"name": "巡检空间", "description": "d"}'),
            user={"sub": 7, "is_admin": False}))
        return result, cursor

    def test_returns_created_workspace_id(self):
        result, cursor = self._create()
        self.assertEqual(result["id"], 7)
        self.assertEqual(result["name"], "巡检空间")
        self.assertEqual(result["api_key"], "ws_new")
        insert = next(p for s, p in cursor.executed if "INSERT INTO workspaces" in s)
        self.assertEqual(insert, ("巡检空间", 7, "ws_new", "d", mock.ANY))

    def test_missing_name_is_rejected_before_any_insert(self):
        cursor = FakeCursor()
        conn = FakeConn(cursor)
        patchers = [
            mock.patch.object(workspace, "get_conn", return_value=FakeCtx(conn)),
            mock.patch.object(workspace, "email_action_allowed", return_value=True),
        ]
        for p in patchers:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in patchers])
        with self.assertRaises(Exception) as ctx:
            asyncio.run(workspace.api_create_workspace(
                FakeRequest('{"name": "  "}'), user={"sub": 7, "is_admin": False}))
        self.assertEqual(getattr(ctx.exception, "status_code", None), 400)
        self.assertEqual(cursor.executed, [])


class ProjectSessionMatchTest(unittest.TestCase):
    def test_sql_is_case_insensitive_and_wildcard_safe(self):
        sql, params = workspace._session_for_project_match(
            "D:/work/2026新疆公路数字底座")
        self.assertIn("LOWER(cwd)", sql)     # case-insensitive
        self.assertNotIn("LIKE", sql)        # no wildcard chars in path
        exact, base, lenf, fwd, lenb, bwd = params
        self.assertEqual(exact, "d:/work/2026新疆公路数字底座")
        self.assertEqual(base, "d:/work/2026新疆公路数字底座")
        self.assertEqual(fwd, "d:/work/2026新疆公路数字底座/")
        self.assertEqual(bwd, "d:/work/2026新疆公路数字底座\\")
        self.assertEqual((lenf, lenb), (len(fwd), len(bwd)))

    def test_windows_case_variants_are_equivalent(self):
        # `D:` (folder) vs `d:` (session cwd) must produce the same params.
        a = workspace._session_for_project_match("D:/work/2026新疆公路数字底座")
        b = workspace._session_for_project_match("d:/work/2026新疆公路数字底座")
        self.assertEqual(a[1], b[1])

    def test_trailing_separator_negated_for_prefix(self):
        sql, params = workspace._session_for_project_match("D:/work/X/")
        _, base, lenf, fwd, lenb, bwd = params
        self.assertEqual(base, "d:/work/x")     # trailing sep stripped
        self.assertEqual(fwd, "d:/work/x/")      # under-prefix is path + '/'
        self.assertEqual(lenf, len(fwd))
        self.assertEqual(bwd, "d:/work/x\\")
        self.assertEqual(lenb, len(bwd))


if __name__ == "__main__":
    unittest.main()
