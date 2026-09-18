"""The sidebar's landing-page link, and the route behaviour it depends on.

`/` still means "dashboard for a logged-in user" — but the sidebar now links to
the PUBLIC landing page, and on a deployment with no static site in front of
the app that link would bounce a logged-in visitor straight back to the
dashboard (a click that visibly does nothing). `?landing=1` is the explicit
request that opts out of the redirect; where nginx serves `/` from files the
query is ignored entirely, so the same link shows the public page there too.
"""
import asyncio
import os
import re
import sys
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("HERMES_SYNC_PG_DSN", "postgresql://x:x@localhost:5432/x")
os.environ.setdefault("HERMES_SYNC_MASTER_KEY", "test-master-key")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import auth  # noqa: E402
import render  # noqa: E402
from translations import TRANSLATIONS, get_translations  # noqa: E402


class _Request:
    def __init__(self, query=""):
        self.query_params = dict(re.findall(r"([^=&]+)=([^&]*)", query))
        self.cookies = {}
        self.state = type("S", (), {})()
        self.headers = {}


def _call(query="", logged_in=True):
    """root() with a stubbed renderer, returning (template_or_redirect, ctx)."""
    captured = {}

    async def fake_render(template_name, ctx=None):
        captured["template"] = template_name
        captured["ctx"] = ctx or {}
        return "rendered"

    request = _Request(query)
    with mock.patch.object(auth, "render_page", side_effect=fake_render), \
         mock.patch.object(auth, "get_current_user",
                           side_effect=None if logged_in else Exception("guest"),
                           return_value={"sub": "7"}):
        out = asyncio.run(auth.root(request))
    if out != "rendered":
        captured["redirect"] = out.headers.get("location")
    return captured


class LandingRouteTest(unittest.TestCase):
    def test_guest_gets_the_landing_page(self):
        self.assertEqual(_call(logged_in=False)["template"], "landing.html")

    def test_logged_in_still_redirects_by_default(self):
        out = _call()
        self.assertEqual(out.get("redirect"), "/web/")

    def test_logged_in_with_the_flag_gets_the_landing_page(self):
        out = _call("landing=1")
        self.assertEqual(out["template"], "landing.html")
        self.assertIn("agent_count", out["ctx"])

    def test_other_query_values_do_not_bypass_the_redirect(self):
        for query in ("landing=0", "landing=", "other=1", "landing=1x"):
            with self.subTest(query=query):
                self.assertEqual(_call(query).get("redirect"), "/web/")


class SidebarLinkTest(unittest.TestCase):
    def _html(self, lang="zh-CN"):
        return render.jinja_env.get_template("base.html").render(
            get_flashed_messages=lambda: [], lang=lang,
            t=get_translations(lang),
            user={"sub": 1, "username": "u", "display_name": "U", "is_admin": False},
            workspaces=[], active_page="dashboard", quota=None,
            announcement_feed_url="", announcement_dismissed=[])

    def test_website_link_is_external_and_requests_the_landing(self):
        html = self._html()
        m = re.search(r'<a href="([^"]*landing=1[^"]*)"([^>]*)>', html)
        self.assertIsNotNone(m, "no sidebar link to the landing page")
        attrs = m.group(2)
        self.assertIn('target="_blank"', attrs,
                      "leaving the app must not lose the user's place")
        self.assertIn('rel="noopener noreferrer"', attrs)

    def test_website_link_sits_next_to_the_github_link(self):
        html = self._html()
        site = html.index("landing=1")
        gh = html.index("github.com/westsource/agentctxsync/issues")
        self.assertLess(site, gh, "outbound links should form one group")

    def test_labels_localized(self):
        self.assertIn("官网", self._html("zh-CN"))
        for lang in TRANSLATIONS:
            self.assertIn("nav_website", get_translations(lang), lang)


if __name__ == "__main__":
    unittest.main()
