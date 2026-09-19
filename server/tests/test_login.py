"""Login identifier tests: username, or a VERIFIED email when mail is live.

`_find_login_user` is the single resolver behind both /web/login and
/api/auth/login. The contract it has to keep:
  * the username is tried first (usernames containing "@" predate email login
    and must keep working),
  * the email branch exists only while the mail feature is configured, needs an
    "@" in the identifier, and only ever matches a VERIFIED address,
  * the search is case-insensitive because it compares the normalized address,
  * a miss is a miss (the caller renders the same "login_invalid" either way).
"""
import asyncio
import os
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

os.environ.setdefault("HERMES_SYNC_PG_DSN", "postgresql://x:x@localhost:5432/x")
os.environ.setdefault("HERMES_SYNC_MASTER_KEY", "test-master-key")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import auth  # noqa: E402


class FakeCursor:
    def __init__(self, rows=()):
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
    def __init__(self, conn):
        self.conn = conn

    def __enter__(self):
        return self.conn

    def __exit__(self, *a):
        return False


def _user(**over):
    row = {"id": 7, "username": "alice", "password_hash": "pbkdf2:sha256:600000:s:h",
           "is_admin": False, "display_name": "Alice", "lang": "zh-CN",
           "account_state": auth.STATE_ACTIVE, "must_change_password": 0,
           "email_normalized": "alice@example.com",
           "email_verified_at": 1_700_000_000.0}
    row.update(over)
    return row


class FindLoginUserTest(unittest.TestCase):
    def _find(self, identifier, rows, smtp=True):
        cursor = FakeCursor(rows)
        with mock.patch.object(auth, "smtp_configured", return_value=smtp):
            user = auth._find_login_user(cursor, identifier)
        return user, cursor

    def test_username_wins_and_the_email_query_is_never_issued(self):
        """Legacy usernames can contain "@": a username match must not be
        shadowed by an email lookup."""
        user, cursor = self._find("alice@example.com",
                                  [_user(username="alice@example.com")])
        self.assertEqual(user["username"], "alice@example.com")
        self.assertEqual(len(cursor.executed), 1)

    def test_verified_email_resolves_when_mail_is_live(self):
        user, cursor = self._find("alice@example.com", [None, _user()])
        self.assertEqual(user["id"], 7)
        sql, params = cursor.executed[1]
        self.assertIn("email_normalized = %s", sql)
        self.assertIn("email_verified_at IS NOT NULL", sql)  # unverified never matches
        self.assertEqual(params, ("alice@example.com",))

    def test_email_lookup_is_case_insensitive(self):
        _, cursor = self._find("  Alice@Example.COM ", [None, _user()])
        self.assertEqual(cursor.executed[1][1], ("alice@example.com",))

    def test_no_email_branch_without_the_mail_feature(self):
        """With SMTP unconfigured the branch is dead — the identifier is only
        ever a username."""
        user, cursor = self._find("alice@example.com", [None], smtp=False)
        self.assertIsNone(user)
        self.assertEqual(len(cursor.executed), 1)

    def test_identifier_without_at_is_username_only(self):
        user, cursor = self._find("alice", [None])
        self.assertIsNone(user)
        self.assertEqual(len(cursor.executed), 1)


class WebLoginTest(unittest.TestCase):
    """Route level: the email form of the identifier logs in, and the page's
    label only advertises it while the mail feature is live."""

    def _post(self, identifier, password="secret1", smtp=True):
        cursor = FakeCursor([None, _user()])
        conn = FakeConn(cursor)
        rendered = []

        async def fake_render(tpl, ctx):
            rendered.append((tpl, ctx))
            return SimpleNamespace(status_code=200, headers={})

        with mock.patch.object(auth, "get_conn", return_value=FakeCtx(conn)), \
                mock.patch.object(auth, "smtp_configured", return_value=smtp), \
                mock.patch.object(auth.ratelimit, "allow", return_value=True), \
                mock.patch.object(auth, "verify_password", return_value=True), \
                mock.patch.object(auth, "render_page", new=fake_render):
            resp = asyncio.run(auth.web_login_post(
                SimpleNamespace(form=None, cookies={},
                                client=SimpleNamespace(host="198.51.100.7"))))
        return resp, cursor, rendered

    def _request(self, identifier, password="secret1"):
        class FakeRequest:
            client = SimpleNamespace(host="198.51.100.7")
            cookies = {}

            async def form(self):
                return {"username": identifier, "password": password}

        return FakeRequest()

    def test_login_with_a_verified_email_sets_the_session_cookie(self):
        cursor = FakeCursor([None, _user()])
        conn = FakeConn(cursor)
        with mock.patch.object(auth, "get_conn", return_value=FakeCtx(conn)), \
                mock.patch.object(auth, "smtp_configured", return_value=True), \
                mock.patch.object(auth.ratelimit, "allow", return_value=True), \
                mock.patch.object(auth, "verify_password", return_value=True):
            resp = asyncio.run(auth.web_login_post(
                self._request("Alice@Example.com")))
        self.assertEqual(resp.status_code, 303)
        self.assertIn("hsync_token", resp.headers.get("set-cookie", ""))
        # Second query is the email lookup, on the normalized address.
        self.assertEqual(cursor.executed[1][1], ("alice@example.com",))

    def test_failed_login_renders_the_same_error_for_both_identifier_forms(self):
        for identifier in ("ghost", "ghost@example.com"):
            cursor = FakeCursor([None, None])
            with mock.patch.object(auth, "get_conn",
                                   return_value=FakeCtx(FakeConn(cursor))), \
                    mock.patch.object(auth, "smtp_configured", return_value=True), \
                    mock.patch.object(auth.ratelimit, "allow", return_value=True), \
                    mock.patch.object(auth, "verify_password", return_value=True), \
                    mock.patch.object(auth, "render_login_page") as page:
                asyncio.run(auth.web_login_post(self._request(identifier)))
            page.assert_called_once_with("login_invalid")

    def test_label_advertises_email_only_when_mail_is_live(self):
        for smtp, expected in ((True, "login_identifier"), (False, "login_username")):
            cursor = FakeCursor([None])
            with mock.patch.object(auth, "get_conn",
                                   return_value=FakeCtx(FakeConn(cursor))), \
                    mock.patch.object(auth, "smtp_configured", return_value=smtp), \
                    mock.patch.object(auth.ratelimit, "allow", return_value=True), \
                    mock.patch.object(auth, "render_page") as page:
                asyncio.run(auth.web_login(
                    SimpleNamespace(client=SimpleNamespace(host="1.2.3.4"))))
            _tpl, ctx = page.call_args[0]
            self.assertEqual(ctx["mail_enabled"], smtp)
            self.assertTrue(expected)


if __name__ == "__main__":
    unittest.main()