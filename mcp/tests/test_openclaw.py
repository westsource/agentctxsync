"""Round-trip tests for the openclaw adapter (probed SQLite schema)."""

import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from adapters.openclaw import OpenClawAdapter  # noqa: E402


def make_db(path: Path):
    conn = sqlite3.connect(str(path))
    conn.execute("""CREATE TABLE conversations (
        id TEXT PRIMARY KEY, title TEXT, model TEXT, started_at REAL)""")
    conn.execute("""CREATE TABLE messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT, conversation_id TEXT,
        role TEXT, content TEXT, timestamp REAL)""")
    conn.execute("INSERT INTO conversations VALUES ('conv-1','OpenClaw Chat','gpt-4o',1000.0)")
    conn.execute("INSERT INTO messages (conversation_id,role,content,timestamp) VALUES "
                 "('conv-1','user','hello openclaw',1000.5),"
                 "('conv-1','assistant','hi!',1001.0)")
    conn.commit()
    conn.close()


class OpenClawAdapterTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "openclaw-agent.sqlite"
        make_db(self.db)

    def tearDown(self):
        self.tmp.cleanup()

    def test_probe_and_read(self):
        a = OpenClawAdapter(db_path=self.db)
        self.assertEqual(a.table_sessions, "conversations")
        self.assertEqual(a.table_messages, "messages")
        sessions = a.read_sessions()
        self.assertEqual(len(sessions), 1)
        s = sessions[0]
        self.assertEqual(s["id"], "conv-1")
        self.assertEqual(s["title"], "OpenClaw Chat")
        self.assertEqual(s["started_at"], 1000.0)
        self.assertEqual(len(s["messages"]), 2)
        self.assertEqual(s["messages"][0]["content"], "hello openclaw")

    def test_write_and_dedupe(self):
        a = OpenClawAdapter(db_path=self.db)
        foreign = [{
            "id": "conv-2", "started_at": 2000.0, "title": "New conv",
            "messages": [
                {"session_id": "conv-2", "role": "user",
                 "content": "from codex", "timestamp": 2000.5}]}]
        first = a.write_sessions(foreign)
        self.assertEqual(first["imported"], 1)
        self.assertEqual(first["new_messages"], 1)
        second = a.write_sessions(foreign)
        self.assertEqual(second["updated"], 1)
        self.assertEqual(second["duplicates"], 1)
        self.assertEqual(second["new_messages"], 0)
        ids = [s["id"] for s in a.read_sessions()]
        self.assertIn("conv-2", ids)


if __name__ == "__main__":
    unittest.main(verbosity=2)
