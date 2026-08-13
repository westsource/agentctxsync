"""Round-trip tests for the reasonix adapter (jsonl transcripts + locks)."""

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from adapters.reasonix import ReasonixAdapter  # noqa: E402


def make_fixture(sessions: Path):
    sessions.mkdir(parents=True)
    p = sessions / "rx-001.jsonl"
    p.write_text("\n".join([
        json.dumps({"role": "user", "content": "hello reasonix", "timestamp": 100.0}),
        json.dumps({"role": "assistant", "content": "hi", "timestamp": 101.0,
                    "tool_calls": [{"id": "t1", "name": "read"}]}),
    ]) + "\n", encoding="utf-8")


class ReasonixAdapterTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.sessions = Path(self.tmp.name) / "sessions"
        make_fixture(self.sessions)

    def tearDown(self):
        self.tmp.cleanup()

    def test_read(self):
        a = ReasonixAdapter(sessions_dir=self.sessions)
        sessions = a.read_sessions()
        self.assertEqual(len(sessions), 1)
        s = sessions[0]
        self.assertEqual(s["id"], "reasonix:rx-001")
        self.assertEqual(len(s["messages"]), 2)
        self.assertEqual(s["messages"][1]["role"], "assistant")
        self.assertEqual(s["messages"][1]["tool_calls"][0]["name"], "read")
        self.assertEqual(s["messages"][0]["session_id"], "reasonix:rx-001")

    def test_write_new_and_dedupe(self):
        a = ReasonixAdapter(sessions_dir=self.sessions)
        foreign = [{
            "id": "reasonix:rx-002", "started_at": 200.0, "title": "rx-002",
            "messages": [
                {"session_id": "reasonix:rx-002", "role": "user",
                 "content": "pushed", "timestamp": 200.5}]}]
        first = a.write_sessions(foreign)
        self.assertEqual(first["imported"], 1)
        second = a.write_sessions(foreign)
        self.assertEqual(second["updated"], 1)
        self.assertEqual(second["duplicates"], 1)
        self.assertEqual(second["new_messages"], 0)
        ids = [s["id"] for s in a.read_sessions()]
        self.assertIn("reasonix:rx-002", ids)

    def test_locked_session_skipped(self):
        # simulate a running reasonix holding the lock
        (self.sessions / "rx-001.jsonl.lock").write_text("12345")
        a = ReasonixAdapter(sessions_dir=self.sessions)
        stats = a.write_sessions([{
            "id": "reasonix:rx-001", "started_at": 100.0,
            "messages": [
                {"session_id": "reasonix:rx-001", "role": "user",
                 "content": "while running", "timestamp": 999.0}]}])
        self.assertEqual(stats["imported"], 0)
        self.assertEqual(stats["new_messages"], 0)
        # no lines appended
        content = (self.sessions / "rx-001.jsonl").read_text(encoding="utf-8")
        self.assertNotIn("while running", content)

    def test_foreign_sessions_not_pushed(self):
        # A session pulled from the remote (bare id, no reasonix: prefix)
        # lands in the local dir but must NOT be read back for push.
        self.tmp2 = tempfile.TemporaryDirectory()
        try:
            sessions = Path(self.tmp2.name) / "sessions"
            sessions.mkdir(parents=True)
            # reasonix's own session (prefixed) + one pulled from hermes (bare)
            (sessions / "rx-001.jsonl").write_text(
                json.dumps({"role": "user", "content": "mine", "timestamp": 1.0})
                + "\n", encoding="utf-8")
            hermes_file = sessions / "20260801_221942_0be785.jsonl"
            hermes_file.write_text(
                json.dumps({"role": "user", "content": "from hermes",
                            "timestamp": 2.0}) + "\n", encoding="utf-8")
            a = ReasonixAdapter(sessions_dir=sessions)
            # mark the hermes id as foreign (as write_sessions would)
            a.write_sessions([{
                "id": "20260801_221942_0be785", "started_at": 2.0,
                "title": "hermes session",
                "messages": [{"session_id": "20260801_221942_0be785",
                              "role": "user", "content": "from hermes",
                              "timestamp": 2.0}]}])
            ids = [s["id"] for s in a.read_sessions()]
            self.assertIn("reasonix:rx-001", ids)
            self.assertNotIn("20260801_221942_0be785", ids)
        finally:
            self.tmp2.cleanup()


if __name__ == "__main__":
    unittest.main(verbosity=2)
