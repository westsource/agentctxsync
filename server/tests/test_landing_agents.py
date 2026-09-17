"""Regression test for the landing page's agent count.

The 开源 section's "支持的 Agent" tile, the hero terminal mock and the meta
description all advertise how many agents the project supports. Those numbers
were hardcoded, so shipping a new adapter (the 7th) left the landing page
still claiming 6 — and the two prose enumerations still omitted Oh My Pi.

The count is now derived from `PUBLIC_AGENTS` (the whitelist that decides
which agents are actually released) in `auth.root`, and the enumerations are
kept in sync by these tests: every assertion below recomputes the expected
value from the whitelist, so adding an agent without updating the page fails
the suite instead of shipping a stale number.
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
import agents  # noqa: E402
import auth  # noqa: E402
import render  # noqa: E402
from client_update import PUBLIC_AGENTS  # noqa: E402
from translations import TRANSLATIONS, get_translations  # noqa: E402

from starlette.requests import Request  # noqa: E402

COUNT = len(PUBLIC_AGENTS)
# Copy that enumerates the supported agents in prose.
AGENT_LISTS = ("lp_meta_desc", "lp_feat1_d", "lp_agents_sub")

SCOPE = {"type": "http", "method": "GET", "path": "/", "query_string": b"",
         "scheme": "http", "server": ("test", 80), "client": ("127.0.0.1", 1),
         "root_path": "", "http_version": "1.1", "headers": []}


def _guest_request(lang):
    scope = dict(SCOPE, headers=[(b"cookie", f"lang={lang}".encode())])
    return Request(scope)


def _landing(lang="zh-CN"):
    """The page a logged-out visitor gets from the real route."""
    request = _guest_request(lang)
    with mock.patch.object(render, "_current_request_var",
                           mock.MagicMock(get=lambda: request)):
        resp = asyncio.run(auth.root(request))
    return resp.body.decode("utf-8")


def _text(html):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html)).strip()


class LandingAgentCountTest(unittest.TestCase):
    def _oss_agents_tile(self, html):
        m = re.search(r'<div class="num">([^<]+)</div><div class="lbl">\s*'
                      r'(?:支持的 Agent|supported agents)\s*</div>', html)
        self.assertIsNotNone(m, "支持的 Agent tile not found in rendered page")
        return m.group(1)

    def test_oss_tile_shows_live_agent_count(self):
        for lang in ("zh-CN", "en"):
            with self.subTest(lang=lang):
                # Was the literal "6+" regardless of the whitelist.
                self.assertEqual(self._oss_agents_tile(_landing(lang)), f"{COUNT}+")

    def test_terminal_mock_and_meta_description_use_the_same_count(self):
        expected = {
            "zh-CN": (f"· {COUNT} 个 Agent 共享", f"{COUNT} 个 AI Agent"),
            "en": (f"· shared by {COUNT} agents", f"{COUNT} AI agents"),
        }
        for lang, (term, meta_phrase) in expected.items():
            with self.subTest(lang=lang):
                html = _landing(lang)
                term_line = _text(re.search(r'<span class="b">(.*?)</span>',
                                            html, re.S).group(1))
                self.assertIn(term, term_line)
                meta = re.search(r'<meta name="description" content="([^"]+)"',
                                 html).group(1)
                self.assertIn(meta_phrase, meta)

    def test_no_unsubstituted_placeholder(self):
        for lang in ("zh-CN", "en"):
            with self.subTest(lang=lang):
                self.assertNotIn("{0}", _landing(lang))

    def test_every_public_agent_is_named_in_the_copy(self):
        # A newly whitelisted agent must be named, not folded into "other
        # agents": the page is the product's supported-agent claim.
        for lang in TRANSLATIONS:
            t = get_translations(lang)
            for key in AGENT_LISTS:
                with self.subTest(lang=lang, key=key):
                    for agent in PUBLIC_AGENTS:
                        label = agents.AGENTS[agent]["label"]
                        self.assertIn(label, t[key], f"{lang}:{key} omits {label}")


if __name__ == "__main__":
    unittest.main()
