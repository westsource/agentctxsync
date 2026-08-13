"""Round-trip tests for the codex adapter (rollout jsonl files)."""

import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

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
        self.assertEqual(s["id"], "codex:11111111-1111-1111-1111-111111111111")
        self.assertEqual(s["title"], "Codex Chat")
        self.assertEqual(s["model"], "openai")
        self.assertEqual(len(s["messages"]), 2)
        self.assertEqual(s["messages"][0]["role"], "user")
        self.assertEqual(s["messages"][0]["content"], "hello codex")
        self.assertEqual(s["messages"][0]["session_id"],
                         "codex:11111111-1111-1111-1111-111111111111")
        self.assertGreater(s["started_at"], 0)

    def test_write_new_and_dedupe(self):
        a = CodexAdapter(codex_home=self.home)
        foreign = [{
            "id": "codex:22222222-2222-2222-2222-222222222222",
            "started_at": 1767300000.0, "title": "Pushed",
            "messages": [
                {"session_id": "codex:22222222-2222-2222-2222-222222222222",
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
        self.assertIn("codex:22222222-2222-2222-2222-222222222222", sessions)
        self.assertEqual(len(sessions["codex:22222222-2222-2222-2222-222222222222"]["messages"]), 1)
        # title appended to the index
        idx = (self.home / "session_index.jsonl").read_text(encoding="utf-8")
        self.assertIn("Pushed", idx)


if __name__ == "__main__":
    unittest.main(verbosity=2)
