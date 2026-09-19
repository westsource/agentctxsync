"""Outbound-mail budget tests (mailer): daily counting, cap, fail-open.

The budget is the deployment's protection against a caller-triggered burst
spending the mail provider's daily quota — which would silently stop
activation and password-reset mail for every user, not just for the target of
the burst. Counters live in mail_stats, so a restart cannot refill them.
"""
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("HERMES_SYNC_PG_DSN", "postgresql://x:x@localhost:5432/x")
os.environ.setdefault("HERMES_SYNC_MASTER_KEY", "test-master-key")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import db  # noqa: E402
import mailer  # noqa: E402


class _Cursor:
    def __init__(self, count):
        self.count = count
        self.executed = []

    def execute(self, sql, params=None):
        self.executed.append((sql, params))

    def fetchone(self):
        return (self.count,)


class _Ctx:
    def __init__(self, cursor):
        self._cursor = cursor

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def cursor(self, *a, **k):
        return self._cursor


class MailBudgetTest(unittest.TestCase):
    def setUp(self):
        self.cursor = _Cursor(0)

    def _db(self, exc=None):
        if exc:
            return mock.patch.object(db, "get_conn", side_effect=exc)
        return mock.patch.object(db, "get_conn", return_value=_Ctx(self.cursor))

    def test_allows_up_to_the_cap_then_refuses(self):
        with mock.patch.object(mailer, "MAIL_DAILY_CAP", 2):
            for total in (1, 2):
                self.cursor.count = total
                self.assertTrue(mailer._budget_allow(), total)
            self.cursor.count = 3
            with self._db():
                self.assertFalse(mailer._budget_allow())
        kinds = [p[1] for s, p in self.cursor.executed if "mail_stats" in s]
        self.assertIn("rejected", kinds)   # ops signal for the burst

    def test_zero_cap_disables_counting_entirely(self):
        with mock.patch.object(mailer, "MAIL_DAILY_CAP", 0):
            with mock.patch.object(db, "get_conn",
                                   side_effect=AssertionError("must not be called")):
                self.assertTrue(mailer._budget_allow())

    def test_counter_outage_fails_open(self):
        with mock.patch.object(mailer, "MAIL_DAILY_CAP", 5):
            with self._db(exc=RuntimeError("pg down")):
                self.assertTrue(mailer._budget_allow())

    def test_send_refuses_without_touching_smtp_when_the_budget_is_spent(self):
        with mock.patch.object(mailer, "smtp_configured", return_value=True), \
                mock.patch.object(mailer, "_budget_allow", return_value=False), \
                mock.patch.object(mailer, "_smtp_connect") as connect:
            with self.assertRaises(mailer.MailerError) as ctx:
                mailer.send_mail("a@example.com", "s", "b")
        self.assertIn("budget", str(ctx.exception))
        connect.assert_not_called()

    def test_failed_smtp_send_is_counted(self):
        with mock.patch.object(mailer, "smtp_configured", return_value=True), \
                mock.patch.object(mailer, "_budget_allow", return_value=True), \
                self._db(), \
                mock.patch.object(mailer, "_smtp_connect",
                                  side_effect=OSError("no route")):
            with self.assertRaises(mailer.MailerError):
                mailer.send_mail("a@example.com", "s", "b")
        kinds = [p[1] for s, p in self.cursor.executed if "mail_stats" in s]
        self.assertIn(mailer.KIND_FAILED, kinds)


if __name__ == "__main__":
    unittest.main()