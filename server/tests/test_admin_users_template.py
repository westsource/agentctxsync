"""Regression test for the email-activation column of /web/admin/users.

The admin user table gained a 邮箱 column; a header cell was silently
overwritten while adding it, so the header row kept 7 cells against 8 body
cells and every column after 显示名 shifted by one. Rendering the real
template pins both the four activation states (verified / verified+email
change pending / pending only / unbound) and the header-to-cell alignment.
"""
import os
import re
import sys
import unittest
from pathlib import Path

os.environ.setdefault("HERMES_SYNC_PG_DSN", "postgresql://x:x@localhost:5432/x")
os.environ.setdefault("HERMES_SYNC_MASTER_KEY", "test-master-key")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import render  # noqa: E402
from translations import TRANSLATIONS, get_translations  # noqa: E402


def _user(uid, username, **over):
    row = {"id": uid, "username": username, "display_name": username.title(),
           "is_admin": False, "is_active": True, "ws_count": 0,
           "created_at": 1_700_000_000.0, "email": None, "email_normalized": None,
           "email_verified_at": None, "pending_email": None,
           "pending_email_normalized": None, "account_state": "ACTIVE",
           "auth_source": "LEGACY_USERNAME"}
    row.update(over)
    return row


# verified / verified + pending change / pending only / no email at all
ROWS = [
    _user(1, "alice", email="alice@example.com",
          email_normalized="alice@example.com", email_verified_at=1_700_000_000.0),
    _user(2, "bob", email="bob@old.com", email_normalized="bob@old.com",
          email_verified_at=1_700_000_000.0, pending_email="bob@new.com",
          pending_email_normalized="bob@new.com"),
    _user(3, "carol", pending_email="carol@example.com",
          pending_email_normalized="carol@example.com",
          account_state="PENDING_EMAIL_VERIFICATION"),
    _user(4, "dave"),
]


def _text(html):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html)).strip()


class AdminUsersEmailColumnTest(unittest.TestCase):
    def _render_table(self, rows=ROWS, lang="zh-CN"):
        html = render.jinja_env.get_template("admin_users.html").render({
            "lang": lang,
            "t": get_translations(lang),
            "get_flashed_messages": lambda: [],
            "user": {"sub": 1, "username": "admin", "display_name": "Admin",
                     "is_admin": True, "account_state": "ACTIVE"},
            "workspaces": [], "active_page": "admin_users", "users": rows,
        })
        table = re.search(r"<table.*?</table>", html, re.S).group(0)
        headers = [_text(h) for h in re.findall(r"<th[^>]*>(.*?)</th>", table, re.S)]
        body = re.findall(r"<tr class=\"hover:bg-\[#F6F5FA\][^>]*>(.*?)</tr>", table, re.S)
        cells = [[_text(c) for c in re.findall(r"<td[^>]*>(.*?)</td>", r, re.S)]
                 for r in body]
        return headers, cells

    def test_header_aligns_with_every_body_cell(self):
        headers, cells = self._render_table()
        self.assertIn("邮箱", headers)
        self.assertEqual(len(cells), len(ROWS))
        for row in cells:
            self.assertEqual(len(row), len(headers),
                             f"header/cell count drift: {headers} vs {row}")

    def test_email_activation_states(self):
        headers, cells = self._render_table()
        col = headers.index("邮箱")
        self.assertEqual(cells[0][col], "alice@example.com 已验证")
        # Pending change on an already verified address shows both.
        self.assertEqual(cells[1][col], "bob@old.com 已验证 等待验证: bob@new.com")
        self.assertEqual(cells[2][col], "carol@example.com 等待验证")
        self.assertEqual(cells[3][col], "未绑定")

    def test_email_states_localized(self):
        headers, cells = self._render_table(lang="en")
        col = headers.index("Email")
        self.assertEqual(cells[0][col], "alice@example.com Verified")
        self.assertEqual(cells[1][col], "bob@old.com Verified Awaiting verification: bob@new.com")
        self.assertEqual(cells[2][col], "carol@example.com Awaiting verification")
        self.assertEqual(cells[3][col], "Not bound")

    def test_email_keys_exist_in_every_language(self):
        for lang in TRANSLATIONS:
            t = get_translations(lang)
            for key in ("admin_email", "email_verified_yes",
                        "email_pending_status", "email_verified_no"):
                self.assertIn(key, t, f"{key} missing in {lang}")


if __name__ == "__main__":
    unittest.main()
