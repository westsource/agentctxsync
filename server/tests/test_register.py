"""Open registration tests: invite code is optional on /web/register.

Covers the route-level contract with a fake DB connection, following the
pattern in test_quota.py (no live PostgreSQL needed).
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
import auth  # noqa: E402


class FakeCursor:
    def __init__(self, fetchone_results=()):
        self.rows = list(fetchone_results)
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


class FakeRequest:
    def __init__(self, form_data):
        self._form = form_data

    async def form(self):
        return self._form


class RegisterSubmitTest(unittest.TestCase):
    def _submit(self, form, consume_result=None, captcha_ok=True):
        cursor = FakeCursor([(7,)])
        conn = FakeConn(cursor)
        patchers = [
            mock.patch.object(auth, "get_conn", return_value=FakeCtx(conn)),
            mock.patch.object(auth, "hash_password", return_value="HASH"),
            mock.patch.object(auth, "generate_api_key", return_value="ws_test"),
            mock.patch.object(auth, "invite_grant_plan", return_value="unlimited"),
            mock.patch.object(auth, "consume_invite", return_value=consume_result),
            mock.patch.object(auth.captcha, "verify", return_value=captcha_ok),
        ]
        for p in patchers:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in patchers])
        return asyncio.run(auth.web_register_submit(FakeRequest(form))), cursor

    def test_register_without_invite_succeeds(self):
        resp, cursor = self._submit({
            "username": "alice",
            "display_name": "Alice",
            "password": "secret1",
            "confirm_password": "secret1",
            "invite_code": "",
            "captcha_id": "cid-1",
            "captcha": "42",
        })
        self.assertEqual(resp.status_code, 303)
        self.assertEqual(resp.headers["location"], "/web/login?success=register_success")
        insert_sql, insert_params = cursor.executed[0]
        self.assertIn("INSERT INTO users", insert_sql)
        self.assertEqual(insert_params[3], False)  # is_admin
        self.assertEqual(insert_params[5], "free")  # plan: open registration grants 'free'
        # Default workspace auto-created for the new user.
        self.assertTrue(any("INSERT INTO workspaces" in sql for sql, _ in cursor.executed))
        # No invite path touched: neither plan lookup nor consumption.
        auth.invite_grant_plan.assert_not_called()
        auth.consume_invite.assert_not_called()
        # Captcha was verified.
        auth.captcha.verify.assert_called_once()

    def test_wrong_captcha_rejected(self):
        resp, cursor = self._submit({
            "username": "dave",
            "password": "secret1",
            "confirm_password": "secret1",
            "invite_code": "",
            "captcha_id": "cid-9",
            "captcha": "9999",
        }, captcha_ok=False)
        self.assertEqual(resp.status_code, 303)
        self.assertEqual(resp.headers["location"], "/web/register?error=register_captcha_failed")
        # Gate runs before any DB work: no queries at all.
        self.assertEqual(cursor.executed, [])

    def test_register_with_invite_still_consumes_it(self):
        resp, cursor = self._submit({
            "username": "bob",
            "password": "secret1",
            "confirm_password": "secret1",
            "invite_code": "HSYNC-ABC",
            "captcha_id": "cid-2",
            "captcha": "42",
        })
        self.assertEqual(resp.status_code, 303)
        self.assertEqual(resp.headers["location"], "/web/login?success=register_success")
        auth.invite_grant_plan.assert_called_once_with("HSYNC-ABC")
        auth.consume_invite.assert_called_once_with("HSYNC-ABC", 7)

    def test_invalid_invite_rolls_back_user(self):
        resp, cursor = self._submit({
            "username": "carol",
            "password": "secret1",
            "confirm_password": "secret1",
            "invite_code": "HSYNC-BAD",
            "captcha_id": "cid-3",
            "captcha": "42",
        }, consume_result="register_invalid_code")
        self.assertEqual(resp.status_code, 303)
        self.assertEqual(resp.headers["location"], "/web/register?error=register_invalid_code")
        self.assertTrue(any("DELETE FROM users" in sql for sql, _ in cursor.executed))

    def test_invalid_input_still_rejected(self):
        resp, cursor = self._submit({
            "username": "",
            "password": "secret1",
            "confirm_password": "secret1",
            "invite_code": "",
            "captcha_id": "cid-4",
            "captcha": "42",
        })
        self.assertEqual(resp.status_code, 303)
        self.assertEqual(resp.headers["location"], "/web/register?error=register_invalid_input")


if __name__ == "__main__":
    unittest.main()
