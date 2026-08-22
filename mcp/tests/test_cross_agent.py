"""Cross-agent sync tests: sessions produced by one adapter's local store
must land in another adapter's local store with stable identity.

These run adapter-to-adapter (no server): the canonical session dict is the
wire format, so pushing (read from A) into B exercises the same conversion
the server round-trip performs.
"""

import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from adapters.hermes import HermesAdapter  # noqa: E402
from adapters.opencode import OpencodeAdapter  # noqa: E402
from adapters.reasonix import ReasonixAdapter  # noqa: E402


def make_hermes_db(path: Path):
    conn = sqlite3.connect(str(path))
    conn.execute("""CREATE TABLE sessions (
        id TEXT PRIMARY KEY, title TEXT, model TEXT, started_at REAL,
        message_count INTEGER, last_synced_at REAL)""")
    conn.execute("""CREATE TABLE messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT,
        role TEXT, content TEXT, timestamp REAL)""")
    conn.execute("INSERT INTO sessions VALUES ('hermes-uuid-1','Hermes Chat','gpt-4o',1000.0,2,1000.0)")
    conn.execute("INSERT INTO messages (session_id,role,content,timestamp) VALUES "
                 "('hermes-uuid-1','user','from hermes',1000.5),"
                 "('hermes-uuid-1','assistant','ok',1001.0)")
    conn.commit()
    conn.close()


class CrossAgentTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.hermes_db = Path(self.tmp.name) / "state.db"
        make_hermes_db(self.hermes_db)

    def tearDown(self):
        self.tmp.cleanup()

    def _hermes_sessions(self):
        return HermesAdapter(db_path=self.hermes_db).read_sessions()

    def test_hermes_to_opencode_idmapped(self):
        storage = Path(self.tmp.name) / "storage"
        (storage / "session" / "info").mkdir(parents=True)
        a = OpencodeAdapter(storage_dir=storage)
        stats = a.write_sessions(self._hermes_sessions())
        self.assertEqual(stats["imported"], 1)
        back = {s["id"]: s for s in a.read_sessions()}
        self.assertIn("hermes-uuid-1", back)  # canonical id restored via idmap
        self.assertEqual(back["hermes-uuid-1"]["messages"][1]["content"], "ok")

    def test_hermes_to_reasonix(self):
        sessions_dir = Path(self.tmp.name) / "reasonix-sessions"
        sessions_dir.mkdir()
        a = ReasonixAdapter(sessions_dir=sessions_dir)
        stats = a.write_sessions(self._hermes_sessions())
        self.assertEqual(stats["imported"], 1)
        # the hermes session lands in the local store (displayable) and is
        # read back too (push view): a foreign session continued locally
        # must push its locally-added messages; push_sessions tags it by
        # owner and the server dedupes, so re-pushing is idempotent.
        self.assertTrue((sessions_dir / "hermes-uuid-1.jsonl").exists())
        back = {s["id"]: s for s in a.read_sessions()}
        self.assertIn("hermes-uuid-1", back)


if __name__ == "__main__":
    unittest.main(verbosity=2)
