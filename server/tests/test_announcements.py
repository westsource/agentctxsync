"""Announcement banner: the server side of it.

The feed itself is static content fetched by the BROWSER, so what the server
owns is narrow and must not drift:

1. off by default — an unconfigured deployment must render nothing AND run no
   extra queries (the banner may not become a hidden per-page cost);
2. when configured, the feed URL and this user's dismissed ids reach the page,
   so the banner cannot flash for something already read;
3. dismissal is per user and idempotent, and rejects ids the feed contract
   does not allow.
"""
import asyncio
import importlib
import json
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("HERMES_SYNC_PG_DSN", "postgresql://x:x@localhost:5432/x")
os.environ.setdefault("HERMES_SYNC_MASTER_KEY", "test-master-key")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import render  # noqa: E402
from translations import TRANSLATIONS, get_translations  # noqa: E402

FEED = "https://www.agentctxsync.com/announcements.json"


class FakeCursor:
    def __init__(self, rows=()):
        self.rows = list(rows)
        self.executed = []
        self.rowcount = 0

    def execute(self, sql, params=None):
        self.executed.append((sql, params))
        if sql.strip().upper().startswith("INSERT"):
            self.rowcount = 1

    def fetchall(self):
        return self.rows

    def fetchone(self):
        return self.rows[0] if self.rows else None


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


class FakeRequest:
    def __init__(self, cookie=None):
        self.cookies = {"hsync_token": cookie} if cookie else {}
        self.query_params = {}
        self.url = type("U", (), {"path": "/web/"})()


def _with_feed_url(url):
    """Reload render/config with HERMES_SYNC_ANNOUNCEMENTS_URL set or unset."""
    os.environ["HERMES_SYNC_ANNOUNCEMENTS_URL"] = url
    import config
    importlib.reload(config)
    importlib.reload(render)
    return render


class DismissedIdsTest(unittest.TestCase):
    def tearDown(self):
        os.environ.pop("HERMES_SYNC_ANNOUNCEMENTS_URL", None)
        import config
        importlib.reload(config)
        importlib.reload(render)

    def test_off_by_default_runs_no_query(self):
        r = _with_feed_url("")
        with mock.patch.object(r, "get_conn") as conn:
            self.assertEqual(r._dismissed_announcements(), [])
        conn.assert_not_called()

    def test_returns_ids_for_the_logged_in_user(self):
        r = _with_feed_url(FEED)
        cur = FakeCursor(rows=[("2026-09-18-oh-my-pi",), ("2026-09-01-x",)])
        req = FakeRequest(cookie="token")
        with mock.patch.object(r, "_current_request_var",
                               mock.MagicMock(get=lambda: req)), \
             mock.patch.object(r, "get_conn",
                               return_value=FakeCtx(FakeConn(cur))), \
             mock.patch.dict(sys.modules, {"auth": mock.MagicMock(
                 verify_jwt=lambda t: {"sub": "7"})}):
            self.assertEqual(r._dismissed_announcements(),
                             ["2026-09-18-oh-my-pi", "2026-09-01-x"])
        sql, params = cur.executed[0]
        self.assertIn("FROM announcement_dismissals", sql)
        self.assertEqual(params, (7,), "user id must come from the session, not the client")

    def test_guest_gets_no_query(self):
        r = _with_feed_url(FEED)
        req = FakeRequest()  # no cookie
        with mock.patch.object(r, "_current_request_var",
                               mock.MagicMock(get=lambda: req)), \
             mock.patch.object(r, "get_conn") as conn:
            self.assertEqual(r._dismissed_announcements(), [])
        conn.assert_not_called()

    def test_db_failure_never_breaks_the_page(self):
        r = _with_feed_url(FEED)
        req = FakeRequest(cookie="token")
        with mock.patch.object(r, "_current_request_var",
                               mock.MagicMock(get=lambda: req)), \
             mock.patch.object(r, "get_conn", side_effect=RuntimeError("pg down")), \
             mock.patch.dict(sys.modules, {"auth": mock.MagicMock(
                 verify_jwt=lambda t: {"sub": "7"})}):
            self.assertEqual(r._dismissed_announcements(), [])


class BannerContextTest(unittest.TestCase):
    def tearDown(self):
        os.environ.pop("HERMES_SYNC_ANNOUNCEMENTS_URL", None)
        import config
        importlib.reload(config)
        importlib.reload(render)

    def _render(self, url):
        r = _with_feed_url(url)
        captured = {}
        import jinja2
        env = jinja2.Environment(
            loader=jinja2.FileSystemLoader(str(Path(r.TEMPLATE_DIR))),
            autoescape=jinja2.select_autoescape(["html"]))
        with mock.patch.object(r, "get_flashed_messages", return_value=[]), \
             mock.patch.object(r, "_sidebar_quota", return_value=None), \
             mock.patch.object(r, "_dismissed_announcements", return_value=["old-one"]):
            html = env.get_template("base.html").render(
                get_flashed_messages=lambda: [], lang="zh-CN",
                t=get_translations("zh-CN"),
                user={"sub": 1, "username": "u", "display_name": "U", "is_admin": False},
                workspaces=[], active_page="dashboard", quota=None,
                announcement_feed_url=r.ANNOUNCEMENTS_URL,
                announcement_dismissed=r._dismissed_announcements())
        return html

    def test_banner_absent_when_unconfigured(self):
        html = self._render("")
        self.assertNotIn("acs-announce", html)

    def test_banner_configured_carries_url_and_seen_ids(self):
        html = self._render(FEED)
        self.assertIn("acs-announce", html)
        self.assertIn(FEED, html)
        self.assertIn("old-one", html)
        # The feed is data: it must never be injected as markup. Scope the
        # check to the banner block — the page has unrelated JS elsewhere.
        block = html[html.index('id="acs-announce"'):]
        block = block[:block.index("</script>")]
        self.assertIn("textContent", block)
        self.assertNotIn("innerHTML", block)


class DismissEndpointTest(unittest.TestCase):
    def _call(self, body, user=None):
        mod = importlib.import_module("announcements")
        request = mock.MagicMock()
        request.json = mock.AsyncMock(return_value=body)
        cur = FakeCursor()
        with mock.patch.object(mod, "get_current_user",
                               side_effect=None if user else Exception("guest"),
                               return_value=user), \
             mock.patch.object(mod, "get_conn",
                               return_value=FakeCtx(FakeConn(cur))):
            resp = asyncio.run(mod.dismiss(request))
        return resp, cur

    def test_dismiss_records_per_user_idempotently(self):
        resp, cur = self._call({"id": "2026-09-18-oh-my-pi"}, user={"sub": "7"})
        self.assertEqual(resp.status_code, 200)
        sql, params = cur.executed[0]
        self.assertIn("INSERT INTO announcement_dismissals", sql)
        self.assertIn("ON CONFLICT (announcement_id, user_id) DO NOTHING", sql)
        self.assertEqual(params[0], "2026-09-18-oh-my-pi")
        self.assertEqual(params[1], 7)

    def test_guest_is_rejected_without_touching_the_db(self):
        resp, cur = self._call({"id": "2026-09-18-oh-my-pi"}, user=None)
        self.assertEqual(resp.status_code, 401)
        self.assertEqual(cur.executed, [])

    def test_malformed_ids_are_rejected(self):
        for bad in ("", "   ", "UPPER", "../etc/passwd", "a" * 65, "with space",
                    "semi;colon", "2026-09-18-oh-my-pi'--"):
            with self.subTest(id=bad):
                resp, cur = self._call({"id": bad}, user={"sub": "7"})
                self.assertEqual(resp.status_code, 400, bad)
                self.assertEqual(cur.executed, [], bad)

    def test_missing_body_is_rejected(self):
        resp, _ = self._call(None, user={"sub": "7"})
        self.assertEqual(resp.status_code, 400)


class BannerKeysTest(unittest.TestCase):
    def test_keys_exist_in_every_language(self):
        for lang in TRANSLATIONS:
            t = get_translations(lang)
            for key in ("announcement_dismiss", "announcement_default_link"):
                self.assertIn(key, t, f"{key} missing in {lang}")


if __name__ == "__main__":
    unittest.main()
