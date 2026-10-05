"""Regression test for the viewer's thinking section (/web/workspace/.../session/...).

The template rendered ``reasoning_content`` -- a column only the hermes client
happens to push twice. Every adapter that follows the canonical contract
(``reasoning``; ADDING_AGENT.md) -- omp, opencode, workbuddy -- produced
assistant bubbles with no visible text and no thinking at all: the model's
reasoning-only steps showed up as empty messages. Pins that the canonical
``reasoning`` column is what gets rendered, with ``reasoning_content`` kept as
a legacy fallback.
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
from translations import get_translations  # noqa: E402


def _msg(mid, **over):
    row = {"id": mid, "role": "assistant", "content": "", "content_md": "",
           "timestamp": 1_791_000_000.0 + mid, "reasoning": None,
           "reasoning_content": None, "hidden": 0, "meta": None}
    row.update(over)
    return row


def _render(messages, lang="zh-CN"):
    ctx = {"lang": lang, "t": get_translations(lang),
           "get_flashed_messages": lambda: [],
           "user": {"sub": 1, "username": "rong", "display_name": "rong"},
           "workspaces": [], "active_page": "workspace_4",
           "ws": {"id": 4, "name": "默认空间"},
           "session": {"id": "s1", "title": "t", "agent_type": "omp"},
           "messages": messages, "total": len(messages), "page": 1,
           "pages": 1, "role": "all", "size": 20, "trash_count": 0}
    return render.jinja_env.get_template("session_messages.html").render(ctx)


def _bubble(html, mid):
    """HTML of one message's rendered card."""
    start = html.find(f'data-mid="{mid}"')
    end = html.find('data-mid="', start + 1)
    return re.sub(r"\s+", " ", html[start:end if end != -1 else start + 4000])


class ViewerReasoningTest(unittest.TestCase):
    def test_canonical_reasoning_is_rendered(self):
        # omp shape: thinking-only step -> content="", reasoning set,
        # reasoning_content never written by the adapter.
        html = _render([_msg(1, reasoning="先看看文件结构")])
        bubble = _bubble(html, 1)
        self.assertIn("思考过程", bubble)
        self.assertIn("先看看文件结构", bubble)

    def test_legacy_reasoning_content_still_rendered(self):
        html = _render([_msg(1, reasoning_content="legacy thinking")])
        bubble = _bubble(html, 1)
        self.assertIn("思考过程", bubble)
        self.assertIn("legacy thinking", bubble)

    def test_reasoning_alongside_text(self):
        html = _render([_msg(1, content="答好了", content_md="<p>答好了</p>",
                             reasoning="想了一下")])
        bubble = _bubble(html, 1)
        self.assertIn("思考过程", bubble)
        self.assertIn("想了一下", bubble)
        self.assertIn("答好了", bubble)

    def test_no_thinking_section_without_reasoning(self):
        html = _render([_msg(1, content="只有正文", content_md="<p>只有正文</p>")])
        bubble = _bubble(html, 1)
        self.assertNotIn("思考过程", bubble)

    def test_thinking_label_localized(self):
        html = _render([_msg(1, reasoning="thinking here")], lang="en")
        self.assertIn("Thinking", _bubble(html, 1))


if __name__ == "__main__":
    unittest.main()
