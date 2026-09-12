"""Field-format contract for every write path that stores username,
display_name or password.

The rules live in auth.py (single source) and are consumed by the register
routes, the reset/change-password flows, the admin user forms and the REST
password endpoint. Two invariants matter and are both pinned here:

1. Usernames are ASCII-only and write-once. Login compares the identifier
   verbatim (`WHERE username = %s`), so a Unicode username could impersonate
   an existing account with a homograph; accounts created before the rule
   (including CJK names) must keep working, which is why the strictness only
   applies at the write entry points.
2. A password that passes one writer must pass all of them — otherwise the
   policy is bypassable by registering (or receiving) a compliant password and
   then changing it to a blank one.

Route tests drive the real handlers with the fake-connection pattern from
test_register.py; rejecting input before any DB work is part of the contract.
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
import admin  # noqa: E402
import auth  # noqa: E402
import workspace  # noqa: E402
from translations import TRANSLATIONS, get_translations  # noqa: E402

from fastapi import HTTPException  # noqa: E402


class UsernameFormatTest(unittest.TestCase):
    def test_plain_ascii_names_accepted(self):
        for name in ("alice", "a", "A1", "alice.bob", "alice_bob", "alice-bob",
                     "9lives", "a" * 32):
            self.assertTrue(auth.username_format_ok(name), name)

    def test_separator_cannot_start_a_name(self):
        # '-' / '.' / '_' first would allow "-x" and the bare '..' entry.
        for name in ("-alice", ".alice", "_alice", ".", "..", "-"):
            self.assertFalse(auth.username_format_ok(name), name)

    def test_non_ascii_and_separators_rejected(self):
        # 'аlice' starts with U+0430 CYRILLIC SMALL LETTER A, not 'a'.
        for name in ("аlice", "Аlice", "阿里", "alicе", "alice bob", "alice@x",
                     "alice/bob", "alice+bob", "alice!", "àlice", "alice\nx",
                     "a" * 33):
            self.assertFalse(auth.username_format_ok(name), repr(name))

    def test_control_chars_rejected_in_display_name(self):
        for name in ("Alice 张", "Alice", ""):
            self.assertTrue(auth.display_name_format_ok(name), repr(name))
        for name in ("a\nb", "a\r\nb", "a\x00b", "a\x1fb", "a\x7fb", "a\tb"):
            self.assertFalse(auth.display_name_format_ok(name), repr(name))

    def test_whitespace_only_password_rejected(self):
        for pw in ("secret1", " s ", "  x  ", "\u00a0x"):
            self.assertTrue(auth.password_format_ok(pw), repr(pw))
        for pw in ("", "      ", "\t\t\t\t\t\t", "\n\n\n\n\n\n", " \t\n\r "):
            self.assertFalse(auth.password_format_ok(pw), repr(pw))


class PasswordWriterPolicyTest(unittest.TestCase):
    """A whitespace-only password must be refused by every writer."""

    def test_web_change_password_refuses_blank(self):
        patchers = [mock.patch.object(
            auth, "get_current_user",
            return_value={"sub": "7", "username": "alice", "is_admin": False})]
        for p in patchers:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in patchers])
        request = SimpleNamespace(query_params={}, form=None)

        async def form():
            return {"old_password": "old-secret", "new_password": " " * 6,
                    "confirm_password": " " * 6}

        request.form = form
        resp = asyncio.run(auth.web_change_password(request))
        self.assertEqual(resp.status_code, 303)
        self.assertEqual(resp.headers["location"],
                         "/web/change-password?forced=1&error=pwd_blank")

    def test_rest_change_password_refuses_blank(self):
        class FakeRequest:
            async def json(self):
                return {"old_password": "old-secret", "new_password": " " * 6}

        # The blank rule fires before get_conn(), so no DB fake is needed; if
        # it ever moves after the connection this raises instead of passing.
        with self.assertRaises(HTTPException) as ctx:
            asyncio.run(workspace.api_change_password(
                FakeRequest(), user={"sub": "7"}))
        self.assertEqual(ctx.exception.status_code, 400)

    def test_register_refuses_blank(self):
        rendered = []
        conn_calls = []

        async def fake_render(error, username="", display_name="", code="", email=""):
            rendered.append(error)
            return SimpleNamespace(status_code=200)

        patchers = [
            mock.patch.object(auth.captcha, "verify", return_value=True),
            mock.patch.object(auth.ratelimit, "allow", return_value=True),
            mock.patch.object(auth, "render_register_page", new=fake_render),
            mock.patch.object(auth, "get_conn",
                              side_effect=lambda *a, **k: conn_calls.append(1)),
        ]
        for p in patchers:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in patchers])
        form = {"username": "alice", "display_name": "Alice",
                "password": " " * 6, "confirm_password": " " * 6,
                "invite_code": "", "captcha_id": "cid", "captcha": "42"}
        request = SimpleNamespace(form=None, cookies={},
                                  client=SimpleNamespace(host="203.0.113.7"))

        async def grab():
            return form

        request.form = grab
        asyncio.run(auth.web_register_submit(request))
        self.assertEqual(rendered, ["pwd_blank"])
        self.assertEqual(conn_calls, [])


class AdminUserWritePolicyTest(unittest.TestCase):
    """Admin-created accounts must satisfy the same field contract as
    self-service registrations — otherwise the admin form is a bypass."""

    def _post(self, handler, form, uid=None):
        patchers = [mock.patch.object(
            admin, "get_current_user",
            return_value={"sub": "1", "username": "admin", "is_admin": True})]
        for p in patchers:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in patchers])
        request = SimpleNamespace(query_params={}, form=None)

        async def grab():
            return form

        request.form = grab
        # Path params precede `request` in the FastAPI signatures.
        if uid is None:
            return asyncio.run(handler(request))
        return asyncio.run(handler(uid, request))

    def test_create_rejects_non_ascii_username_before_db(self):
        with mock.patch.object(admin, "get_conn") as conn:
            resp = self._post(admin.web_create_user, {
                "username": "阿里", "display_name": "阿里",
                "password": "secret1"})
        self.assertEqual(resp.status_code, 303)
        self.assertEqual(resp.headers["location"], "/web/admin/users")
        conn.assert_not_called()

    def test_create_rejects_over_long_password_before_db(self):
        with mock.patch.object(admin, "get_conn") as conn:
            self._post(admin.web_create_user, {
                "username": "alice", "display_name": "Alice",
                "password": "p" * 129})
        conn.assert_not_called()

    def test_create_accepts_ascii_username(self):
        cursor = mock.MagicMock()
        conn = mock.MagicMock()
        conn.cursor.return_value = cursor
        ctx = mock.MagicMock()
        ctx.__enter__ = lambda s: conn
        ctx.__exit__ = lambda *a: False
        with mock.patch.object(admin, "get_conn", return_value=ctx), \
             mock.patch.object(admin, "hash_password", return_value="HASH"):
            resp = self._post(admin.web_create_user, {
                "username": "alice", "display_name": "Alice",
                "password": "secret1"})
        self.assertEqual(resp.status_code, 303)
        sql, params = cursor.execute.call_args[0]
        self.assertIn("INSERT INTO users", sql)
        self.assertEqual(params[0], "alice")

    def test_edit_rejects_control_chars_in_display_name(self):
        with mock.patch.object(admin, "get_conn") as conn:
            self._post(admin.web_edit_user, {
                "display_name": "bad\nname", "new_password": ""}, uid=7)
        conn.assert_not_called()

    def test_edit_rejects_short_new_password_instead_of_ignoring_it(self):
        # Previously a 4-char password was silently dropped (no change); now it
        # is refused, so the admin sees the change did not happen.
        with mock.patch.object(admin, "get_conn") as conn:
            self._post(admin.web_edit_user, {
                "display_name": "Alice", "new_password": "abcd"}, uid=7)
        conn.assert_not_called()


class ValidationMessageTest(unittest.TestCase):
    def test_new_error_keys_localized_everywhere(self):
        for lang in TRANSLATIONS:
            t = get_translations(lang)
            for key in ("username_invalid", "display_invalid", "pwd_blank"):
                self.assertIn(key, t, f"{key} missing in {lang}")


if __name__ == "__main__":
    unittest.main()
