"""User attribution on the admin device drill-down (/web/admin/access/devices).

`access_device` rows are keyed by (device, agent, channel, user) because a
device_id is a client-declared string: one box can sync two accounts, and a
hostname can repeat across users. The page must therefore

1. show the owning user on every per-agent row (its whole point),
2. label pre-column rows (user_id = 0) as unattributed rather than blank, and
3. keep the summary badge counting AGENTS -- not rows -- so a device whose
   agent is used by two accounts still reads "1 个 Agent".
"""
import asyncio
import os
import sys
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

os.environ.setdefault("HERMES_SYNC_PG_DSN", "postgresql://x:x@localhost:5432/x")
os.environ.setdefault("HERMES_SYNC_MASTER_KEY", "test-master-key")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import admin  # noqa: E402
import render  # noqa: E402
from translations import TRANSLATIONS, get_translations  # noqa: E402


def _row(device, agent, user_id, domain=5, ip=0, version="2026.09.13.4",
         last_seen=1_700_000_000.0):
    return {"device_id": device, "agent": agent, "user_id": user_id,
            "domain_count": domain, "ip_count": ip, "last_seen": last_seen,
            "client_version": version}


class FakeCursor:
    def __init__(self, fetchall_results=()):
        self.fetchall_results = list(fetchall_results)
        self.executed = []

    def execute(self, sql, params=None):
        self.executed.append((sql, params))

    def fetchall(self):
        return self.fetchall_results.pop(0) if self.fetchall_results else []


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


ADMIN = {"sub": 1, "username": "admin", "display_name": "Admin",
         "is_admin": True, "account_state": "ACTIVE"}


class AccessDeviceUserTest(unittest.TestCase):
    def _render(self, rows, users=()):
        cursor = FakeCursor(fetchall_results=[rows, list(users)])
        captured = {}

        async def fake_render(template_name, ctx):
            captured.update(ctx)
            return object()

        patchers = [
            mock.patch.object(admin, "get_current_user", return_value=ADMIN),
            mock.patch.object(admin, "get_nav_workspaces", return_value=[]),
            mock.patch.object(admin, "get_conn",
                              return_value=FakeCtx(FakeConn(cursor))),
            mock.patch.object(admin, "render_page", side_effect=fake_render),
        ]
        for p in patchers:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in patchers])
        asyncio.run(admin.web_admin_access_devices(mock.MagicMock()))
        return captured, cursor

    def _html(self, ctx, lang="zh-CN"):
        return render.jinja_env.get_template("admin_access_devices.html").render(
            **{**ctx, "t": get_translations(lang), "lang": lang,
               "get_flashed_messages": lambda: []})

    def _text(self, html):
        import re
        return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html))

    def test_agent_row_shows_owning_user(self):
        ctx, _ = self._render(
            [_row("local-JWCLZ1", "dsh", 42), _row("local-JWCLZ1", "workbuddy", 42)],
            users=[{"id": 42, "username": "flm", "display_name": "风林流墨"}])
        text = self._text(self._html(ctx))
        self.assertEqual(text.count("风林流墨"), 2)  # one per agent row
        self.assertIn("dsh", text)
        self.assertIn("workbuddy", text)

    def test_two_users_on_one_device_stay_separate(self):
        ctx, _ = self._render(
            [_row("shared-box", "hermes", 1, domain=9),
             _row("shared-box", "hermes", 2, domain=3)],
            users=[{"id": 1, "username": "alice", "display_name": "Alice"},
                   {"id": 2, "username": "bob", "display_name": "Bob"}])
        device = ctx["devices"][0]
        self.assertEqual([c["agent"] for c in device["clients"]],
                         ["hermes", "hermes"])
        self.assertEqual([c["user"]["name"] for c in device["clients"]],
                         ["Alice", "Bob"])
        # summary badge counts the AGENT once, not the two rows
        self.assertEqual(len(device["agents"]), 1)
        html = self._html(ctx)
        self.assertIn("1 个 Agent", self._text(html))
        self.assertNotIn("2 个 Agent", self._text(html))

    def test_legacy_rows_labeled_unattributed(self):
        ctx, _ = self._render([_row("old-box", "hermes", 0)])
        html = self._html(ctx)
        self.assertIn("未归属", self._text(html))
        self.assertIsNone(ctx["devices"][0]["clients"][0]["user"])

    def test_deleted_account_keeps_a_placeholder(self):
        # statistics outlive accounts (no FK): the id must still be visible
        ctx, _ = self._render([_row("gone-box", "hermes", 99)], users=[])
        self.assertEqual(ctx["devices"][0]["clients"][0]["user"]["name"], "#99")
        self.assertIn("#99", self._text(self._html(ctx)))

    def test_query_groups_by_user(self):
        _, cursor = self._render([_row("d", "hermes", 5)], users=[])
        sql, _ = cursor.executed[0]
        self.assertIn("GROUP BY device_id, agent, user_id", sql)
        self.assertIn("user_id", sql)
        users_sql, params = cursor.executed[1]
        self.assertIn("FROM users", users_sql)
        self.assertEqual(params, ([5],))

    def test_display_name_falls_back_to_username(self):
        ctx, _ = self._render([_row("d", "hermes", 8)],
                              users=[{"id": 8, "username": "xowm",
                                      "display_name": ""}])
        self.assertEqual(ctx["devices"][0]["clients"][0]["user"]["name"], "xowm")

    def test_rows_ordered_by_activity(self):
        ctx, _ = self._render([_row("quiet", "hermes", 1, domain=1),
                               _row("busy", "hermes", 1, domain=50)],
                              users=[])
        self.assertEqual([d["device_id"] for d in ctx["devices"]],
                         ["busy", "quiet"])

    def test_user_keys_localized_everywhere(self):
        for lang in TRANSLATIONS:
            t = get_translations(lang)
            for key in ("accs_user", "accs_user_unknown", "accs_user_unknown_hint"):
                self.assertIn(key, t, f"{key} missing in {lang}")


if __name__ == "__main__":
    unittest.main()
