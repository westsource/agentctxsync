"""Round-trip tests for the opencode adapter (JSON file store + idmap)."""

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from adapters.opencode import OpencodeAdapter  # noqa: E402


def make_fixture(storage: Path):
    sid = "ses_1234567890abcdef1234567890ab12cd"
    (storage / "session" / "info").mkdir(parents=True)
    (storage / "session" / "message" / sid).mkdir(parents=True)
    info = {"id": sid, "title": "Opencode Chat", "model": "claude-sonnet-4",
            "time": {"created": 1767300000.0, "updated": 1767300100.0},
            "projectID": "abc123"}
    (storage / "session" / "info" / f"{sid}.json").write_text(
        json.dumps(info), encoding="utf-8")
    mid = "msg_1234567890abcdef1234567890ab12cd"
    msg = {"id": mid, "sessionID": sid, "role": "user",
           "time": 1767300001.0,
           "content": [{"type": "text", "text": "hello opencode"}]}
    (storage / "session" / "message" / sid / f"{mid}.json").write_text(
        json.dumps(msg), encoding="utf-8")


class OpencodeAdapterTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.storage = Path(self.tmp.name)
        make_fixture(self.storage)

    def tearDown(self):
        self.tmp.cleanup()

    def test_read(self):
        a = OpencodeAdapter(storage_dir=self.storage)
        sessions = a.read_sessions()
        self.assertEqual(len(sessions), 1)
        s = sessions[0]
        self.assertTrue(s["id"].startswith("ses_"))
        self.assertEqual(s["title"], "Opencode Chat")
        self.assertEqual(s["model"], "claude-sonnet-4")
        self.assertEqual(s["started_at"], 1767300000.0)
        self.assertEqual(len(s["messages"]), 1)
        self.assertEqual(s["messages"][0]["content"], "hello opencode")
        self.assertEqual(s["messages"][0]["role"], "user")

    def test_write_foreign_session_uses_idmap(self):
        a = OpencodeAdapter(storage_dir=self.storage)
        foreign = [{
            "id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
            "started_at": 1767400000.0, "title": "From Hermes",
            "messages": [
                {"session_id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
                 "role": "assistant", "content": "sync me",
                 "timestamp": 1767400001.0}]}]
        first = a.write_sessions(foreign)
        self.assertEqual(first["imported"], 1)
        self.assertEqual(first["new_messages"], 1)
        # second write must reuse the same local id (dedupe stable)
        second = a.write_sessions(foreign)
        self.assertEqual(second["duplicates"], 1)
        self.assertEqual(second["new_messages"], 0)
        idmap = json.loads((self.storage / ".hermes-sync-idmap.json").read_text())
        self.assertIn("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee", idmap)
        local = idmap["aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"]
        self.assertTrue(local.startswith("ses_"))
        # round-trip: session readable with its canonical id
        sessions = a.read_sessions()
        found = [s for s in sessions if s["id"] == "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"]
        self.assertEqual(len(found), 1)
        self.assertEqual(len(found[0]["messages"]), 1)

    def test_write_existing_updates_in_place(self):
        a = OpencodeAdapter(storage_dir=self.storage)
        sessions = a.read_sessions()
        existing = sessions[0]
        existing["title"] = "Renamed"
        stats = a.write_sessions([existing])
        self.assertEqual(stats["updated"], 1)
        self.assertEqual(stats["imported"], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
