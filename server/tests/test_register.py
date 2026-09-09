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
        self.rowcount = 1

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
        self.base_url = "http://testserver/"

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

        async def fake_render(error, username="", display_name="", code="", email=""):
            ctx = {"error": error, "username": username,
                   "display_name": display_name, "code": code, "email": email}
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

    def test_mail_flow_creates_pending_without_workspace(self):
        """With SMTP configured, registration stores a pending-verify user
        (NO workspace / api key yet), issues a token, mails it, and lands the
        user on the verify page with a pending-state session cookie."""
        cursor = FakeCursor([None, (7,)])   # email-uniqueness pre-check: free; then user id
        conn = FakeConn(cursor)
        mailed = []

        def spy_allow(scope, key):
            return True

        patchers = [
            mock.patch.object(auth, "get_conn", return_value=FakeCtx(conn)),
            mock.patch.object(auth, "hash_password", return_value="HASH"),
            mock.patch.object(auth, "generate_api_key", return_value="ws_test"),
            mock.patch.object(auth.captcha, "verify", return_value=True),
            mock.patch.object(auth, "smtp_configured", return_value=True),
            mock.patch.object(auth.ratelimit, "allow", new=spy_allow),
            mock.patch.object(auth.mailer, "send_verification_mail",
                              side_effect=lambda to, link, lang: mailed.append((to, link, lang))),
        ]
        for p in patchers:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in patchers])
        form = valid_form(email="alice@example.com")
        resp = asyncio.run(auth.web_register_submit(FakeRequest(form, lang="en")))
        # Redirect to the verify page with an auto-login cookie.
        self.assertEqual(resp.status_code, 303)
        self.assertTrue(resp.headers["location"].startswith("/web/verify-email"))
        set_cookie = resp.headers.get("set-cookie", "")
        self.assertIn("hsync_token=", set_cookie)
        # User inserted in pending state; email held as pending only.
        sql, params = self._user_insert(cursor)
        self.assertIsNotNone(sql)
        self.assertIn("pending_email", sql)
        self.assertEqual(params[10], "alice@example.com")       # pending_email
        self.assertEqual(params[11], "alice@example.com")       # normalized
        self.assertEqual(params[12], auth.STATE_PENDING)        # state
        self.assertEqual(params[13], auth.AUTH_SOURCE_EMAIL)    # auth_source
        # No default workspace is created for a pending account, but the
        # verification token row is (digest only) and the mail went out.
        self.assertFalse(any("INSERT INTO workspaces" in s for s, _ in cursor.executed))
        self.assertTrue(any("INSERT INTO user_verification_tokens" in s
                            for s, _ in cursor.executed))
        self.assertEqual(len(mailed), 1)
        self.assertEqual(mailed[0][0], "alice@example.com")
        self.assertIn("/web/verify-email?token=", mailed[0][1])
        self.assertEqual(mailed[0][2], "en")

    def test_mail_flow_rejects_missing_email(self):
        cursor = FakeCursor([(7,)])
        conn = FakeConn(cursor)
        patchers = [
            mock.patch.object(auth, "get_conn", return_value=FakeCtx(conn)),
            mock.patch.object(auth.captcha, "verify", return_value=True),
            mock.patch.object(auth, "smtp_configured", return_value=True),
            mock.patch.object(auth.ratelimit, "allow", return_value=True),
        ]
        for p in patchers:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in patchers])
        rendered = []

        async def fake_render(error, username="", display_name="", code="", email=""):
            rendered.append(error)

        with mock.patch.object(auth, "render_register_page", new=fake_render):
            resp = asyncio.run(auth.web_register_submit(FakeRequest(valid_form(email=""))))
        self.assertEqual(rendered, ["register_email_required"])
        self.assertEqual(cursor.executed, [])


class EmailActivationTest(unittest.TestCase):
    """POST /web/verify-email activates the account and creates the default
    workspace exactly when the pending account has none yet (recommended
    onboarding: activate -> dashboard is immediately sync-capable)."""

    def _confirm(self):
        raw = "activation-token-raw"
        now = time.time()
        # Row stream in fetchone order:
        # 1 token lookup row (tuple), 2 user row FOR UPDATE (dict),
        # 3 email-uniqueness (None = free), 4 workspace-exists (None = none),
        # 5 default workspace insert RETURNING id.
        rows = [
            (11, 7, "verify_email", "alice@example.com", now + 1800, None),
            {"id": 7, "username": "alice", "display_name": "Alice",
             "is_admin": False, "lang": "zh-CN", "must_change_password": 0,
             "account_state": auth.STATE_PENDING,
             "auth_source": auth.AUTH_SOURCE_EMAIL,
             "email": None,
             "pending_email": "alice@example.com",
             "pending_email_normalized": "alice@example.com"},
            None, None,
            (1,),
        ]
        cursor = FakeCursor(rows)
        conn = FakeConn(cursor)
        patchers = [
            mock.patch.object(auth, "get_conn", return_value=FakeCtx(conn)),
            mock.patch.object(auth, "generate_api_key", return_value="ws_new"),
            mock.patch.object(auth, "smtp_configured", return_value=True),
            mock.patch.object(auth.emailverify, "token_digest",
                              return_value="digest-of-" + raw),
        ]
        for p in patchers:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in patchers])
        form = {"token": raw}
        resp = asyncio.run(auth.web_verify_email_confirm(FakeRequest(form)))
        return resp, cursor, conn

    def test_activation_creates_default_workspace_and_logs_in(self):
        resp, cursor, conn = self._confirm()
        self.assertEqual(resp.status_code, 303)
        self.assertEqual(resp.headers["location"], "/web/")
        self.assertIn("hsync_token=", resp.headers.get("set-cookie", ""))
        # Email moved from pending to verified; account activated.
        user_update = next(p for s, p in cursor.executed
                           if s.startswith("UPDATE users SET email = pending_email"))
        self.assertEqual(user_update[0] is not None, True)  # verified_at
        self.assertEqual(user_update[1], auth.STATE_ACTIVE)
        self.assertEqual(user_update[2], 7)                 # user id
        # Exactly one default workspace insert (localized zh name).
        ws_inserts = [p for s, p in cursor.executed if "INSERT INTO workspaces" in s]
        self.assertEqual(len(ws_inserts), 1)
        self.assertEqual(ws_inserts[0][0], "默认工作空间")
        # Token consumed and the activation audited.
        self.assertTrue(any("consumed_at" in s and "user_verification_tokens" in s
                            for s, _ in cursor.executed))
        audit = next(p for s, p in cursor.executed if "INSERT INTO audit_log" in s)
        self.assertEqual(audit[1], "email_verified")
        self.assertFalse(conn.rolled_back)

    def test_activation_keeps_existing_workspace(self):
        """A user who already owns a workspace (e.g. legacy binding) must NOT
        get a second default one: workspace-exists check returns a row."""
        raw = "activation-token-raw"
        now = time.time()
        rows = [
            (11, 7, "verify_email", "alice@example.com", now + 1800, None),
            {"id": 7, "username": "alice", "display_name": "Alice",
             "is_admin": False, "lang": "zh-CN", "must_change_password": 0,
             "account_state": "LEGACY_UNVERIFIED",
             "auth_source": "LEGACY_USERNAME",
             "email": "old@example.com",
             "pending_email": "alice@example.com",
             "pending_email_normalized": "alice@example.com"},
            None,
            (9,),  # workspace already exists -> no insert
        ]
        cursor = FakeCursor(rows)
        conn = FakeConn(cursor)
        patchers = [
            mock.patch.object(auth, "get_conn", return_value=FakeCtx(conn)),
            mock.patch.object(auth, "generate_api_key", return_value="ws_new"),
            mock.patch.object(auth, "smtp_configured", return_value=True),
            mock.patch.object(auth.emailverify, "token_digest",
                              return_value="digest-of-" + raw),
        ]
        for p in patchers:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in patchers])
        resp = asyncio.run(auth.web_verify_email_confirm(
            FakeRequest({"token": raw})))
        self.assertEqual(resp.headers["location"], "/web/")
        ws_inserts = [p for s, p in cursor.executed if "INSERT INTO workspaces" in s]
        self.assertEqual(ws_inserts, [])


class ForgotResetTest(unittest.TestCase):
    """Password recovery: uniform anti-enumeration response, reset only for
    verified-email accounts, one-time token consumed inside the reset tx."""

    def _render_capture(self, rendered):
        async def fake_render(tpl, ctx):
            rendered.append((tpl, ctx))
            return FakeRendered(ctx)
        return fake_render

    def test_forgot_unknown_identifier_uniform_done_no_mail(self):
        cursor = FakeCursor([None])  # user lookup -> not found
        conn = FakeConn(cursor)
        rendered = []
        patchers = [
            mock.patch.object(auth, "get_conn", return_value=FakeCtx(conn)),
            mock.patch.object(auth, "smtp_configured", return_value=True),
            mock.patch.object(auth.ratelimit, "allow", return_value=True),
            mock.patch.object(auth, "render_page", new=self._render_capture(rendered)),
            mock.patch.object(auth.mailer, "send_password_reset_mail"),
        ]
        for p in patchers:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in patchers])
        resp = asyncio.run(auth.web_forgot_submit(
            FakeRequest({"username": "ghost"})))
        tpl, ctx = rendered[0]
        self.assertEqual(ctx["mode"], "done")
        auth.mailer.send_password_reset_mail.assert_not_called()
        self.assertEqual(resp.status_code, 200)

    def test_forgot_verified_account_sends_reset_mail(self):
        rows = [{"id": 7, "username": "alice", "email": "alice@example.com",
                 "email_normalized": "alice@example.com",
                 "email_verified_at": 1_700_000_000.0, "lang": "zh-CN"}]
        cursor = FakeCursor(rows)
        conn = FakeConn(cursor)
        rendered = []
        patchers = [
            mock.patch.object(auth, "get_conn", return_value=FakeCtx(conn)),
            mock.patch.object(auth, "smtp_configured", return_value=True),
            mock.patch.object(auth.ratelimit, "allow", return_value=True),
            mock.patch.object(auth, "render_page", new=self._render_capture(rendered)),
            mock.patch.object(auth.mailer, "send_password_reset_mail"),
        ]
        for p in patchers:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in patchers])
        asyncio.run(auth.web_forgot_submit(FakeRequest({"username": "alice"})))
        auth.mailer.send_password_reset_mail.assert_called_once()
        to_email, link, lang = auth.mailer.send_password_reset_mail.call_args[0]
        self.assertEqual(to_email, "alice@example.com")
        self.assertIn("/web/reset?token=", link)
        # Token was issued under the reset purpose (digest only).
        self.assertTrue(any("INSERT INTO user_verification_tokens" in s
                            for s, _ in cursor.executed))

    def _reset_submit(self, rows):
        cursor = FakeCursor(rows)
        conn = FakeConn(cursor)
        rendered = []
        patchers = [
            mock.patch.object(auth, "get_conn", return_value=FakeCtx(conn)),
            mock.patch.object(auth, "smtp_configured", return_value=True),
            mock.patch.object(auth, "hash_password", return_value="NEWHASH"),
            mock.patch.object(auth.emailverify, "token_digest",
                              return_value="digest-of-raw"),
            mock.patch.object(auth, "render_page", new=self._render_capture(rendered)),
        ]
        for p in patchers:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in patchers])
        form = {"token": "raw", "new_password": "newsecret1",
                "confirm_password": "newsecret1"}
        resp = asyncio.run(auth.web_reset_submit(FakeRequest(form)))
        return resp, cursor, rendered

    def test_reset_sets_password_and_consumes_token(self):
        now = time.time()
        rows = [
            (22, 7, "reset_password", "alice@example.com", now + 1800, None),
            {"id": 7, "lang": "zh-CN", "email": "alice@example.com",
             "email_normalized": "alice@example.com",
             "email_verified_at": 1_700_000_000.0},
        ]
        resp, cursor, rendered = self._reset_submit(rows)
        tpl, ctx = rendered[0]
        self.assertEqual(ctx["mode"], "done")
        # Password hash updated, must_change cleared, token consumed, audited.
        update = next(p for s, p in cursor.executed
                      if s.startswith("UPDATE users SET password_hash"))
        self.assertEqual(update[0], "NEWHASH")
        self.assertTrue(any("consumed_at" in s and "user_verification_tokens" in s
                            for s, _ in cursor.executed))
        audit = next(p for s, p in cursor.executed if "INSERT INTO audit_log" in s)
        self.assertEqual(audit[1], "password_reset")

    def test_reset_rejects_unverified_account(self):
        now = time.time()
        rows = [
            (22, 7, "reset_password", "alice@example.com", now + 1800, None),
            {"id": 7, "lang": "zh-CN", "email": None,
             "email_normalized": None, "email_verified_at": None},
        ]
        resp, cursor, rendered = self._reset_submit(rows)
        self.assertEqual(rendered[0][1]["mode"], "invalid")
        self.assertFalse(any(s.startswith("UPDATE users SET password_hash")
                             for s, _ in cursor.executed))


class EmailGateTest(unittest.TestCase):
    """email_action_allowed gates ONLY new-workspace creation: ACTIVE or
    verified accounts pass; legacy/pending unverified accounts do not."""

    def _check(self, row):
        conn = FakeConn(FakeCursor([row] if row is not None else []))
        with mock.patch.object(auth, "smtp_configured", return_value=True), \
                mock.patch.object(auth, "get_conn", return_value=FakeCtx(conn)):
            return auth.email_action_allowed(7)

    def test_feature_off_allows_without_db(self):
        with mock.patch.object(auth, "smtp_configured", return_value=False):
            self.assertTrue(auth.email_action_allowed(7))

    def test_verified_email_allows(self):
        self.assertTrue(self._check(("LEGACY_UNVERIFIED", 1_700_000_000.0)))

    def test_active_admin_provisioned_allows(self):
        self.assertTrue(self._check(("ACTIVE", None)))

    def test_legacy_without_email_blocked(self):
        self.assertFalse(self._check(("LEGACY_UNVERIFIED", None)))

    def test_pending_blocked(self):
        self.assertFalse(self._check(("PENDING_EMAIL_VERIFICATION", None)))

    def test_missing_user_blocked(self):
        self.assertFalse(self._check(None))


if __name__ == "__main__":
    unittest.main()
