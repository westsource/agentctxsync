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
    # 末尾追加哨兵消息：_bubble() 以「下一条消息」为边界截取单条消息的 HTML，
    # 最后一条消息没有后继时窗口会滑进页面尾部的 <script>（那里的注释/文案会
    # 污染断言）。哨兵让每条被测消息都有确定的右边界。
    messages = list(messages) + [_msg(999999, role="user", content="SENTINEL",
                                      content_md="<p>SENTINEL</p>")]
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
    """HTML of one message's rendered card (``_render`` appends the sentinel)."""
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


class ViewerThinkingOnlyToggleTest(unittest.TestCase):
    """「仅思考过程的消息」默认隐藏（有 reasoning、无正文的 assistant 步骤）。

    标记类挂在消息容器上，CSS ``.hide-think-only .think-only`` 负责隐藏，
    筛选条上的 #think-only-toggle 控制 <html> 上的 .hide-think-only。
    """

    def _toggle(self, html):
        m = re.search(r'<input[^>]*id="think-only-toggle"[^>]*>', html)
        self.assertIsNotNone(m, "筛选条缺少 #think-only-toggle")
        return m.group(0)

    def test_thinking_only_step_is_marked_and_toggle_defaults_to_hidden(self):
        html = _render([_msg(1, reasoning="先看看文件结构")])
        # 标记类在 data-mid 之前，故在整页 HTML 上匹配
        self.assertRegex(html, r'<div class="flex justify-start gap-3 think-only" data-mid="1">')
        # 勾选项默认勾上（= 隐藏），与页首脚本的默认值一致
        self.assertIn("checked", self._toggle(html))
        self.assertIn("隐藏仅思考过程的消息", html)

    def test_step_with_text_is_never_marked(self):
        # 有正文（哪怕同时有 reasoning）就不是「仅思考过程」的消息
        for over in ({"content": "只有正文", "content_md": "<p>只有正文</p>"},
                     {"content": "答好了", "content_md": "<p>答好了</p>",
                      "reasoning": "想了一下"}):
            self.assertNotRegex(_render([_msg(1, **over)]), r'gap-3 think-only')

    def test_whitespace_only_content_counts_as_thinking_only(self):
        html = _render([_msg(1, content="  \n ", content_md="", reasoning="空正文")])
        self.assertRegex(html, r'<div class="flex justify-start gap-3 think-only" data-mid="1">')

    def test_legacy_reasoning_content_step_also_hidden(self):
        html = _render([_msg(1, reasoning_content="legacy thinking")])
        self.assertRegex(html, r'<div class="flex justify-start gap-3 think-only" data-mid="1">')

    def test_toggle_label_localized(self):
        html = _render([_msg(1, reasoning="thinking here")], lang="en")
        self.assertIn("Hide thinking-only messages", html)

    def test_both_languages_carry_the_toggle_terms(self):
        from translations import TRANSLATIONS
        for lang in TRANSLATIONS:
            t = get_translations(lang)
            for key in ("msg_hide_thinking_only", "msg_hide_thinking_only_hint"):
                self.assertIn(key, t, f"{key} missing in {lang}")


if __name__ == "__main__":
    unittest.main()
