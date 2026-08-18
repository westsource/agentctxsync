"""Round-trip tests for the codex adapter (rollout jsonl files)."""

import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from adapters.codex import CodexAdapter  # noqa: E402


def make_fixture(home: Path):
    sess = home / "sessions"
    sess.mkdir(parents=True)
    meta = {
        "meta": {"id": "11111111-1111-1111-1111-111111111111",
                 "timestamp": "2026-01-01T10:00:00+00:00",
                 "model_provider": "openai"},
        "git": {}}
    line1 = {"type": "response_item", "payload": {
        "type": "message", "role": "user",
        "content": [{"type": "input_text", "text": "hello codex"}]}}
    line2 = {"type": "response_item", "payload": {
        "type": "message", "role": "assistant",
        "content": [{"type": "output_text", "text": "hi there"}]}}
    import json
    p = sess / f"rollout-2026-01-01T10-00-00-{meta['meta']['id']}.jsonl"
    p.write_text("\n".join(json.dumps(x) for x in (meta, line1, line2)) + "\n",
                 encoding="utf-8")
    # title index
    (home / "session_index.jsonl").write_text(
        json.dumps({"id": meta["meta"]["id"], "thread_name": "Codex Chat",
                    "updated_at": "2026-01-01T10:00:00+00:00"}) + "\n",
        encoding="utf-8")


class CodexAdapterTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        make_fixture(self.home)

    def tearDown(self):
        self.tmp.cleanup()

    def test_read(self):
        a = CodexAdapter(codex_home=self.home)
        sessions = a.read_sessions()
        self.assertEqual(len(sessions), 1)
        s = sessions[0]
        self.assertEqual(s["id"], "11111111-1111-1111-1111-111111111111")
        self.assertEqual(s["title"], "Codex Chat")
        self.assertEqual(s["model"], "openai")
        self.assertEqual(len(s["messages"]), 2)
        self.assertEqual(s["messages"][0]["role"], "user")
        self.assertEqual(s["messages"][0]["content"], "hello codex")
        self.assertEqual(s["messages"][0]["session_id"],
                         "11111111-1111-1111-1111-111111111111")
        self.assertGreater(s["started_at"], 0)

    def test_write_new_and_dedupe(self):
        a = CodexAdapter(codex_home=self.home)
        foreign = [{
            "id": "22222222-2222-2222-2222-222222222222",
            "started_at": 1767300000.0, "title": "Pushed",
            "messages": [
                {"session_id": "22222222-2222-2222-2222-222222222222",
                 "role": "user", "content": "from another device",
                 "timestamp": 1767300001.0}]}]
        first = a.write_sessions(foreign)
        self.assertEqual(first["imported"], 1)
        self.assertEqual(first["new_messages"], 1)
        second = a.write_sessions(foreign)
        self.assertEqual(second["updated"], 1)
        self.assertEqual(second["duplicates"], 1)
        self.assertEqual(second["new_messages"], 0)
        # round-trip: two sessions now, new one readable with prefix
        sessions = {s["id"]: s for s in a.read_sessions()}
        self.assertIn("22222222-2222-2222-2222-222222222222", sessions)
        self.assertEqual(len(sessions["22222222-2222-2222-2222-222222222222"]["messages"]), 1)
        # title appended to the index
        idx = (self.home / "session_index.jsonl").read_text(encoding="utf-8")
        self.assertIn("Pushed", idx)


def make_new_format_fixture(home: Path):
    """Codex Desktop 0.142+ layout: year/month/day partition + new line
    types (session_meta / event_msg / turn_context / compacted)."""
    import json
    d = home / "sessions" / "2026" / "06" / "29"
    d.mkdir(parents=True)
    lines = [
        {"timestamp": "2026-06-29T14:05:24.822Z", "type": "session_meta",
         "payload": {"session_id": "33333333-3333-3333-3333-333333333333",
                     "id": "33333333-3333-3333-3333-333333333333",
                     "timestamp": "2026-06-29T14:05:24.822Z",
                     "cwd": "C:\\work", "originator": "Codex Desktop",
                     "cli_version": "0.142.3", "model_provider": "custom"}},
        {"timestamp": "2026-06-29T14:05:24.823Z", "type": "event_msg",
         "payload": {"type": "task_started", "turn_id": "t1"}},
        {"timestamp": "2026-06-29T14:05:24.824Z", "type": "turn_context",
         "payload": {"turn_id": "t1", "cwd": "C:\\work",
                     "model": "deepseek-v4-pro"}},
        {"timestamp": "2026-06-29T14:05:24.825Z", "type": "response_item",
         "payload": {"type": "message", "role": "developer",
                     "content": [{"type": "input_text", "text": "sysprompt"}]}},
        {"timestamp": "2026-06-29T14:05:24.826Z", "type": "response_item",
         "payload": {"type": "message", "role": "user",
                     "content": [{"type": "input_text", "text": "hi"}]}},
        {"timestamp": "2026-06-29T14:05:24.827Z", "type": "response_item",
         "payload": {"type": "reasoning", "id": "rs_1",
                     "summary": [{"type": "summary_text", "text": "think"}]}},
        {"timestamp": "2026-06-29T14:05:24.828Z", "type": "response_item",
         "payload": {"type": "function_call", "id": "fc_1", "name": "shell",
                     "arguments": "{\"cmd\": \"ls\"}",
                     "call_id": "call_1"}},
        {"timestamp": "2026-06-29T14:05:24.829Z", "type": "response_item",
         "payload": {"type": "function_call_output", "call_id": "call_1",
                     "output": "file.txt"}},
        {"timestamp": "2026-06-29T14:05:24.830Z", "type": "response_item",
         "payload": {"type": "message", "role": "assistant",
                     "content": [{"type": "output_text", "text": "done"}]}},
        {"timestamp": "2026-06-29T16:51:58.566Z", "type": "compacted",
         "payload": {"message": "handoff summary text"}},
    ]
    p = d / "rollout-2026-06-29T14-05-24-33333333-3333-3333-3333-333333333333.jsonl"
    p.write_text("\n".join(json.dumps(x) for x in lines) + "\n", encoding="utf-8")
    (home / "session_index.jsonl").write_text(
        json.dumps({"id": "33333333-3333-3333-3333-333333333333",
                    "thread_name": "New Format Chat",
                    "updated_at": "2026-06-29T14:05:24+00:00"}) + "\n",
        encoding="utf-8")


class CodexNewFormatTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        make_new_format_fixture(self.home)

    def tearDown(self):
        self.tmp.cleanup()

    def test_read_new_format_nested_layout(self):
        a = CodexAdapter(codex_home=self.home)
        sessions = a.read_sessions()
        self.assertEqual(len(sessions), 1)
        s = sessions[0]
        self.assertEqual(s["id"], "33333333-3333-3333-3333-333333333333")
        self.assertEqual(s["title"], "New Format Chat")
        self.assertEqual(s["model"], "deepseek-v4-pro")   # from turn_context
        self.assertEqual(s["cwd"], "C:\\work")            # from session_meta
        msgs = s["messages"]
        # event_msg/turn_context/reasoning are not conversation: skipped
        self.assertEqual([m["role"] for m in msgs],
                         ["system", "user", "tool", "tool", "assistant", "assistant"])
        self.assertEqual(msgs[0]["content"], "sysprompt")  # developer -> system
        self.assertEqual(msgs[2]["role"], "tool")          # function_call
        self.assertEqual(msgs[2]["tool_name"], "shell")
        self.assertEqual(msgs[2]["tool_call_id"], "call_1")
        self.assertEqual(msgs[3]["content"], "file.txt")   # call output
        self.assertEqual(msgs[5]["content"], "handoff summary text")  # compacted
        # real timestamps from the top-level `timestamp` field, not synthetic
        expected = datetime(2026, 6, 29, 14, 5, 24, 826000, tzinfo=timezone.utc).timestamp()
        self.assertAlmostEqual(msgs[1]["timestamp"], expected, delta=1.0)
        # no junk empty messages
        self.assertTrue(all(m["content"] for m in msgs))

    def test_write_new_goes_into_partition(self):
        a = CodexAdapter(codex_home=self.home)
        foreign = [{
            "id": "44444444-4444-4444-4444-444444444444",
            "started_at": 1767300000.0, "title": "Partitioned",
            "messages": [
                {"session_id": "44444444-4444-4444-4444-444444444444",
                 "role": "user", "content": "hello", "timestamp": 1767300001.0}]}]
        stats = a.write_sessions(foreign)
        self.assertEqual(stats["imported"], 1)
        # file landed under a year/month/day partition, not sessions/ root
        new_files = list((self.home / "sessions").rglob("rollout-*.jsonl"))
        self.assertEqual(len(new_files), 2)
        newest = max(new_files, key=lambda p: p.stat().st_mtime)
        parts = newest.relative_to(self.home / "sessions").parts
        self.assertEqual(len(parts), 4)  # YYYY/MM/DD/file
        self.assertTrue(all(x.isdigit() for x in parts[:3]))
        # round-trip: new session readable
        sessions = {s["id"]: s for s in a.read_sessions()}
        self.assertIn("44444444-4444-4444-4444-444444444444", sessions)
        # update appends to the existing partitioned file (no duplicate)
        stats2 = a.write_sessions(foreign)
        self.assertEqual(stats2["updated"], 1)
        self.assertEqual(stats2["new_messages"], 0)
        self.assertEqual(len(list((self.home / "sessions").rglob("rollout-*.jsonl"))), 2)

    def test_foreign_colon_id_skipped_on_windows(self):
        # A ':' id would silently become an NTFS alternate data stream on
        # Windows (open() succeeds, content lands in a hidden stream the
        # adapter can never read back). The filename store must skip it
        # instead of half-writing it.
        a = CodexAdapter(codex_home=self.home)
        foreign = [{
            "id": "workbuddy:1b8fc026-d2b4-4dfb-bdef-2ea8e73013e4",
            "started_at": 1767300000.0, "title": "WB",
            "messages": [{"session_id": "workbuddy:1b8fc026-d2b4-4dfb-bdef-2ea8e73013e4",
                          "role": "user", "content": "x", "timestamp": 1.0}]}]
        with mock.patch("adapters.base.os.name", "nt"):
            stats = a.write_sessions(foreign)
        self.assertEqual(stats["imported"], 0)
        self.assertEqual(stats["new_messages"], 0)
        # no new file appeared next to the fixture (no half-written ADS stub)
        self.assertEqual(len(list((self.home / "sessions").rglob("rollout-*"))), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
