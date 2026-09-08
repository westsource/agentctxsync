"""Open registration tests: route-level contract for /web/register.

Covers the rate gate, captcha gate, field validation, and the
single-transaction invite flow with fake DB connections, following the
pattern in test_quota.py (no live PostgreSQL needed). Failed submissions now
re-render the form (fields retained) instead of redirecting, so assertions
check the rendered context rather than a Location header.
"""
import asyncio
import itertools
import os
import sys
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import psycopg2

os.environ.setdefault("HERMES_SYNC_PG_DSN", "postgresql://x:x@localhost:5432/x")
os.environ.setdefault("HERMES_SYNC_MASTER_KEY", "test-master-key")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import auth  # noqa: E402

_IPS = itertools.count(1)


class FakeCursor:
    def __init__(self, rows=(), fail_sql=None):
        self.rows = list(rows)
        self.executed = []
        self.fail_sql = fail_sql

    def execute(self, sql, params=None):
        if self.fail_sql and self.fail_sql in sql:
            raise psycopg2.errors.UniqueViolation()
        self.executed.append((sql, params))

    def fetchone(self):
        return self.rows.pop(0) if self.rows else None


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

    def __exit__(self, *a):
        return False


class FakeRequest:
    def __init__(self, form_data, ip=None, lang=None):
        self._form = form_data
        self.client = SimpleNamespace(host=ip or f"198.51.100.{next(_IPS)}")
        self.cookies = {"lang": lang} if lang else {}

    async def form(self):
        return self._form


class FakeRendered:
    """Minimal stand-in for the HTMLResponse render_page would produce."""

    def __init__(self, ctx):
        self.ctx = ctx
        self.status_code = 200
        self.headers = {}


def valid_form(**over):
    data = {
        "username": "alice",
        "display_name": "Alice",
        "password": "secret1",
        "confirm_password": "secret1",
        "invite_code": "",
        "captcha_id": "cid-1",
        "captcha": "42",
    }
    data.update(over)
    return data


class RegisterSubmitTest(unittest.TestCase):
    def _submit(self, form, rows=(), captcha_ok=True, rate_ok=True,
                duplicate=False, ip=None, lang=None):
        cursor = FakeCursor(rows, fail_sql="INSERT INTO users" if duplicate else None)
        conn = FakeConn(cursor)
        rendered = []
        allow_calls = []

        def spy_allow(scope, key):
            allow_calls.append((scope, key))
            return rate_ok

        async def fake_render(error, username="", display_name="", code=""):
            ctx = {"error": error, "username": username,
                   "display_name": display_name, "code": code}
            rendered.append(ctx)
            return FakeRendered(ctx)

        patchers = [
            mock.patch.object(auth, "get_conn", return_value=FakeCtx(conn)),
            mock.patch.object(auth, "hash_password", return_value="HASH"),
            mock.patch.object(auth, "generate_api_key", return_value="ws_test"),
            mock.patch.object(auth.captcha, "verify", return_value=captcha_ok),
            mock.patch.object(auth, "render_register_page", new=fake_render),
            mock.patch.object(auth.ratelimit, "allow", new=spy_allow),
        ]
        for p in patchers:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in patchers])
        resp = asyncio.run(auth.web_register_submit(
            FakeRequest(form, ip=ip, lang=lang)))
        return resp, cursor, conn, rendered, allow_calls

    def _user_insert(self, cursor):
        """The (sql, params) of the first INSERT INTO users."""
        return next((p for p in cursor.executed if "INSERT INTO users" in p[0]),
                    (None, None))

    def test_register_without_invite_succeeds(self):
        resp, cursor, conn, rendered, allow_calls = self._submit(
            valid_form(), rows=[(7,), (1,)])
        self.assertEqual(resp.status_code, 303)
        self.assertEqual(resp.headers["location"], "/web/login?success=register_success")
        sql, params = self._user_insert(cursor)
        self.assertIsNotNone(sql)
        self.assertEqual(params[3], False)  # is_admin
        self.assertEqual(params[5], "free")  # plan: open registration grants 'free'
        self.assertEqual(params[6], "zh-CN")  # lang default
        # Default workspace auto-created for the new user (zh name).
        self.assertTrue(any("INSERT INTO workspaces" in s for s, _ in cursor.executed))
        self.assertIn("默认工作空间",
                      next(p[0] for s, p in cursor.executed if "INSERT INTO workspaces" in s))
        # No invite path touched: no FOR UPDATE lookup, no consumption.
        self.assertFalse(any("FOR UPDATE" in s for s, _ in cursor.executed))
        self.assertFalse(any(s.startswith("UPDATE invites") for s, _ in cursor.executed))
        # Registration is audited inside the same transaction.
        self.assertTrue(any("INSERT INTO audit_log" in s for s, _ in cursor.executed))
        # Captcha was verified and the per-IP register gate was consulted.
        auth.captcha.verify.assert_called_once()
        self.assertIn(("register", allow_calls[0][1]), allow_calls)
        self.assertEqual(rendered, [])

    def test_register_adopts_lang_cookie(self):
        resp, cursor, conn, rendered, _ = self._submit(
            valid_form(), rows=[(7,), (1,)], lang="en")
        self.assertEqual(resp.headers["location"], "/web/login?success=register_success")
        _, params = self._user_insert(cursor)
        self.assertEqual(params[6], "en")
        self.assertEqual(
            next(p[0] for s, p in cursor.executed if "INSERT INTO workspaces" in s),
            "Default")

    def test_register_with_invite_normalizes_and_consumes(self):
        # invite row (id=5, unused, not revoked, never expires, grants unlimited)
        resp, cursor, conn, rendered, _ = self._submit(
            valid_form(invite_code="hsync-abc"),
            rows=[(5, 0, 0, None, "unlimited"), (7,), (1,)])
        self.assertEqual(resp.headers["location"], "/web/login?success=register_success")
        # Input code is uppercased before the (locked) lookup.
        select_sql, select_params = cursor.executed[0]
        self.assertIn("FOR UPDATE", select_sql)
        self.assertEqual(select_params, ("HSYNC-ABC",))
        # Grant plan comes from the invite row; invite consumed with the user id.
        _, params = self._user_insert(cursor)
        self.assertEqual(params[5], "unlimited")
        consume = next(p for p in cursor.executed if p[0].startswith("UPDATE invites"))
        self.assertEqual(consume[1], (7, 5))
        self.assertFalse(conn.rolled_back)

    def test_register_with_free_grant_invite(self):
        resp, cursor, conn, rendered, _ = self._submit(
            valid_form(invite_code="HSYNC-FREE"),
            rows=[(5, 0, 0, None, "free"), (7,), (1,)])
        self.assertEqual(resp.headers["location"], "/web/login?success=register_success")
        _, params = self._user_insert(cursor)
        self.assertEqual(params[5], "free")

    def test_revoked_invite_rejected(self):
        resp, cursor, conn, rendered, _ = self._submit(
            valid_form(invite_code="HSYNC-REV"),
            rows=[(5, 0, 1, None, "unlimited")])
        self.assertEqual(rendered[0]["error"], "register_invalid_code")
        # No user/workspace insert ever ran and the transaction was rolled back.
        self.assertFalse(any("INSERT INTO users" in s for s, _ in cursor.executed))
        self.assertTrue(conn.rolled_back)

    def test_used_invite_rejected(self):
        resp, cursor, conn, rendered, _ = self._submit(
            valid_form(invite_code="HSYNC-USED"),
            rows=[(5, 1, 0, None, "unlimited")])
        self.assertEqual(rendered[0]["error"], "register_used_code")
        self.assertTrue(conn.rolled_back)

    def test_expired_invite_rejected(self):
        resp, cursor, conn, rendered, _ = self._submit(
            valid_form(invite_code="HSYNC-OLD"),
            rows=[(5, 0, 0, time.time() - 1, "unlimited")])
        self.assertEqual(rendered[0]["error"], "register_expired_code")
        self.assertTrue(conn.rolled_back)

    def test_duplicate_username_rejected(self):
        resp, cursor, conn, rendered, _ = self._submit(
            valid_form(), rows=[], duplicate=True)
        self.assertEqual(rendered[0]["error"], "register_user_exists")
        self.assertTrue(conn.rolled_back)

    def test_wrong_captcha_rejected_and_form_retained(self):
        resp, cursor, conn, rendered, _ = self._submit(
            valid_form(username="dave", display_name="Dave"), captcha_ok=False)
        # Re-rendered with the typed fields (never a redirect); no DB work.
        self.assertEqual(rendered[0]["error"], "register_captcha_failed")
        self.assertEqual(rendered[0]["username"], "dave")
        self.assertEqual(rendered[0]["display_name"], "Dave")
        self.assertEqual(cursor.executed, [])

    def test_username_required(self):
        resp, cursor, conn, rendered, _ = self._submit(
            valid_form(username="", display_name=""))
        self.assertEqual(rendered[0]["error"], "register_username_required")
        self.assertEqual(cursor.executed, [])

    def test_username_too_long_rejected(self):
        resp, cursor, conn, rendered, _ = self._submit(
            valid_form(username="x" * 33))
        self.assertEqual(rendered[0]["error"], "register_username_too_long")
        self.assertEqual(cursor.executed, [])

    def test_password_too_long_rejected(self):
        # Regression: an unbounded password would pin a core with PBKDF2.
        resp, cursor, conn, rendered, _ = self._submit(
            valid_form(password="p" * 129, confirm_password="p" * 129))
        self.assertEqual(rendered[0]["error"], "pwd_too_long")
        self.assertEqual(cursor.executed, [])

    def test_rate_limited_ip_gets_error_before_any_work(self):
        resp, cursor, conn, rendered, allow_calls = self._submit(
            valid_form(), rate_ok=False)
        self.assertEqual(rendered[0]["error"], "register_rate_limited")
        self.assertEqual(cursor.executed, [])
        auth.captcha.verify.assert_not_called()
        self.assertEqual(allow_calls[0][0], "register")


if __name__ == "__main__":
    unittest.main()
