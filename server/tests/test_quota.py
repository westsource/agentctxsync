"""Quota mechanism tests: pure decision logic + mocked-DB helpers.

The DB-backed integration (init_db migration on a live PostgreSQL and the
end-to-end /push gate) runs in the e2e phase; here we cover the decision
logic and the DB read helpers with a fake connection so the suite runs
without a database.
"""
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("HERMES_SYNC_PG_DSN", "postgresql://x:x@localhost:5432/x")
os.environ.setdefault("HERMES_SYNC_MASTER_KEY", "test-master-key")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import server as srv  # noqa: E402


class FakeCursor:
    def __init__(self, rows):
        self.rows = list(rows)
        self.executed = []

    def execute(self, sql, params=None):
        self.executed.append((sql, params))

    def fetchone(self):
        return self.rows.pop(0) if self.rows else None


class FakeConn:
    def __init__(self, cursor):
        self._cursor = cursor

    def cursor(self, *a, **k):
        return self._cursor


class FakeCtx:
    """Stand-in for the get_conn() context manager."""

    def __init__(self, conn):
        self.conn = conn

    def __enter__(self):
        return self.conn

    def __exit__(self, *a):
        return False


class QuotaCheckTest(unittest.TestCase):
    """quota_check() is the pure gate used by /push."""

    def test_cap_rejects_overflow(self):
        ok, code = srv.quota_check(200, None, 199, ["hermes", "codex"])
        self.assertFalse(ok)
        self.assertEqual(code, "quota_exceeded_sessions")

    def test_cap_boundary_allows(self):
        ok, code = srv.quota_check(200, None, 198, ["hermes", "codex"])
        self.assertTrue(ok)
        self.assertIsNone(code)

    def test_allowlist_rejects_unknown_agent(self):
        ok, code = srv.quota_check(None, ["hermes"], 0, ["codex"])
        self.assertFalse(ok)
        self.assertEqual(code, "agent_not_allowed")

    def test_allowlist_allows_listed_agent(self):
        ok, code = srv.quota_check(None, ["hermes", "codex"], 0, ["codex"])
        self.assertTrue(ok)

    def test_unlimited_no_cap_no_allowlist(self):
        ok, code = srv.quota_check(None, None, 9999, ["hermes"])
        self.assertTrue(ok)

    def test_empty_allowlist_means_allow_all(self):
        ok, code = srv.quota_check(200, [], 0, ["anything"])
        self.assertTrue(ok)


class InviteGrantPlanTest(unittest.TestCase):
    def _run(self, row):
        conn = FakeConn(FakeCursor([row] if row is not None else []))
        with mock.patch.object(srv, "get_conn", return_value=FakeCtx(conn)):
            return srv.invite_grant_plan("HSYNC-TEST")

    def test_grant_free(self):
        self.assertEqual(self._run(("free",)), "free")

    def test_grant_unlimited(self):
        self.assertEqual(self._run(("unlimited",)), "unlimited")

    def test_missing_invite_defaults_unlimited(self):
        self.assertEqual(self._run(None), "unlimited")

    def test_unknown_value_falls_back_unlimited(self):
        self.assertEqual(self._run(("pro",)), "unlimited")


class PlanLimitsTest(unittest.TestCase):
    def test_known_plan_returns_limits(self):
        conn = FakeConn(FakeCursor([(200, ["hermes"])]))
        with mock.patch.object(srv, "get_conn", return_value=FakeCtx(conn)):
            self.assertEqual(srv.plan_limits("free"), (200, ["hermes"]))

    def test_unknown_plan_fails_open(self):
        conn = FakeConn(FakeCursor([]))
        with mock.patch.object(srv, "get_conn", return_value=FakeCtx(conn)):
            self.assertEqual(srv.plan_limits("pro"), (None, None))

    def test_explicit_conn_reused(self):
        conn = FakeConn(FakeCursor([(None, None)]))
        self.assertEqual(srv.plan_limits("unlimited", conn), (None, None))


class AuditLogTest(unittest.TestCase):
    def test_quota_rejection_writes_full_row(self):
        cur = FakeCursor([])
        conn = FakeConn(cur)
        srv.log_audit(conn, "quota_rejected", 7, 3, "dev-1",
                      "quota_exceeded_sessions", "plan=free active=200 new=1")
        sql, params = cur.executed[0]
        self.assertIn("INSERT INTO audit_log", sql)
        self.assertEqual(params[1:], ("quota_rejected", 7, 3, "dev-1",
                                      "quota_exceeded_sessions",
                                      "plan=free active=200 new=1"))
        self.assertIsInstance(params[0], float)  # ts


if __name__ == "__main__":
    unittest.main()
