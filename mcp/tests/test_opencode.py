"""Round-trip tests for the opencode adapter (SQLite opencode.db store).

opencode CLI/desktop share one SQLite opencode.db (session/message/part
tables). The adapter reads/writes that store; foreign sessions get fresh
ses_ ids via an idmap so dedupe stays stable and round-trip.
"""

import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from adapters.opencode import OpencodeAdapter  # noqa: E402

_SCHEMA = {
    "drizzle": (
        "CREATE TABLE project (id TEXT PRIMARY KEY, name TEXT, worktree TEXT);"
        "INSERT INTO project(id,name,worktree) VALUES('global',NULL,NULL),"
        " ('proj_a',NULL,'E:/OpenCode/MyProj');"
    ),
    "session": (
        "CREATE TABLE session (id TEXT PRIMARY KEY, project_id TEXT NOT NULL "
        "REFERENCES project(id), parent_id TEXT, slug TEXT NOT NULL, "
        "directory TEXT NOT NULL, title TEXT NOT NULL, version TEXT NOT NULL, "
        "share_url TEXT, summary_additions INTEGER, summary_deletions INTEGER, "
        "summary_files INTEGER, summary_diffs TEXT, revert TEXT, permission TEXT, "
        "time_created INTEGER NOT NULL, time_updated INTEGER NOT NULL, "
        "time_compacting INTEGER, time_archived INTEGER, workspace_id TEXT, "
        "path TEXT, agent TEXT, model TEXT, cost REAL NOT NULL DEFAULT 0, "
        "tokens_input INTEGER NOT NULL DEFAULT 0, tokens_output INTEGER NOT NULL "
        "DEFAULT 0, tokens_reasoning INTEGER NOT NULL DEFAULT 0, "
        "tokens_cache_read INTEGER NOT NULL DEFAULT 0, "
        "tokens_cache_write INTEGER NOT NULL DEFAULT 0, metadata TEXT)"
    ),
    "message": (
        "CREATE TABLE message (id TEXT PRIMARY KEY, session_id TEXT NOT NULL, "
        "time_created INTEGER NOT NULL, time_updated INTEGER NOT NULL, "
        "data TEXT NOT NULL)"
    ),
    "part": (
        "CREATE TABLE part (id TEXT PRIMARY KEY, message_id TEXT NOT NULL, "
        "session_id TEXT NOT NULL, time_created INTEGER NOT NULL, "
        "time_updated INTEGER NOT NULL, data TEXT NOT NULL)"
    ),
    "project_directory": (
        "CREATE TABLE project_directory (project_id TEXT NOT NULL, "
        "directory TEXT NOT NULL, type TEXT, strategy TEXT, "
        "time_created INTEGER)"
    ),
    "project_directory_data": (
        "INSERT INTO project_directory (project_id, directory, time_created) "
        "VALUES ('proj_a', 'E:/OpenCode/MyProj', 1), "
        "('proj_z', 'D:/other', 1)"
    ),
}


def make_db(path: Path):
    con = sqlite3.connect(path)
    con.execute("PRAGMA foreign_keys=ON")
    for ddl in (_SCHEMA["drizzle"], _SCHEMA["session"],
                _SCHEMA["message"], _SCHEMA["part"],
                _SCHEMA["project_directory"], _SCHEMA["project_directory_data"]):
        con.executescript(ddl)
    # one native opencode session (ses_...), 1 user + 1 assistant w/ tool
    con.execute(
        "INSERT INTO session (id, project_id, slug, directory, title, version, "
        "time_created, time_updated, cost, tokens_input, tokens_output, "
        "tokens_reasoning, tokens_cache_read, tokens_cache_write, agent, model) "
        "VALUES ('ses_000000000000000000000000000000', 'global', 'fixture', "
        "'D:/work/x', 'Opencode Chat', '1.17.15', 1767300000000, 1767300100000, "
        "0, 10, 20, 0, 0, 0, 'opencode', 'claude-sonnet-4')")
    con.execute(
        "INSERT INTO message (id, session_id, time_created, time_updated, data) "
        "VALUES ('msg_000000000000000000000000000000', "
        "'ses_000000000000000000000000000000', 1767300001000, 1767300001000,"
        "'{\"role\":\"user\",\"time\":{\"created\":1767300001000}}')")
    con.execute(
        "INSERT INTO part (id, message_id, session_id, time_created, "
        "time_updated, data) VALUES ('prt_000000000000000000000000000000', "
        "'msg_000000000000000000000000000000', "
        "'ses_000000000000000000000000000000', 1767300001000, 1767300001000, "
        "'{\"type\":\"text\",\"text\":\"hello opencode\"}')")
    con.commit()
    con.close()


class OpencodeAdapterTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "opencode.db"
        make_db(self.db)

    def tearDown(self):
        self.tmp.cleanup()

    def test_read_native_session(self):
        a = OpencodeAdapter(db_path=self.db)
        sessions = a.read_sessions()
        self.assertEqual(len(sessions), 1)
        s = sessions[0]
        self.assertEqual(s["id"], "ses_000000000000000000000000000000")
        self.assertEqual(s["title"], "Opencode Chat")
        self.assertEqual(s["model"], "claude-sonnet-4")
        self.assertEqual(s["cwd"], "D:/work/x")
        self.assertEqual(s["started_at"], 1767300000.0)
        self.assertEqual(len(s["messages"]), 1)
        self.assertEqual(s["messages"][0]["content"], "hello opencode")
        self.assertEqual(s["messages"][0]["role"], "user")

    def test_write_foreign_session_uses_idmap_and_roundtrips(self):
        a = OpencodeAdapter(db_path=self.db)
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
        # second write must reuse the local id (dedupe stable)
        second = a.write_sessions(foreign)
        self.assertEqual(second["duplicates"], 1)
        self.assertEqual(second["new_messages"], 0)
        idmap = json.loads(
            (self.db.with_name(".hermes-sync-idmap.json")).read_text())
        self.assertIn("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee", idmap)
        # round-trip: readable under its canonical id
        sessions = a.read_sessions()
        found = [s for s in sessions if s["id"] == "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"]
        self.assertEqual(len(found), 1)
        self.assertEqual(len(found[0]["messages"]), 1)
        self.assertEqual(found[0]["messages"][0]["content"], "sync me")
        # session row inserted with NOT NULL project_id (global default)
        con = sqlite3.connect(self.db)
        row = con.execute("SELECT project_id FROM session WHERE id=?",
                          (idmap["aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"],)).fetchone()
        self.assertEqual(row[0], "global")
        con.close()

    def test_write_wraps_plain_model_as_json_object(self):
        # opencode JSON-parses session.model; a bare string breaks its session
        # list. Any plain (non-JSON) model must be stored as a JSON object.
        a = OpencodeAdapter(db_path=self.db)
        a.write_sessions([{
            "id": "workbuddy:model-probe", "title": "M", "started_at": 1.0,
            "model": "deepseek-v4-flash",
            "messages": [{"session_id": "workbuddy:model-probe", "role": "user",
                          "content": "hi", "timestamp": 1.0}]}])
        con = sqlite3.connect(self.db)
        row = con.execute("SELECT model FROM session WHERE title='M'").fetchone()[0]
        con.close()
        parsed = json.loads(row)          # must be parseable JSON
        # opencode Model.Ref requires {id, providerID} -- missing providerID
        # breaks the whole session list ("Expected string, got undefined")
        self.assertEqual(parsed["id"], "deepseek-v4-flash")
        self.assertIsInstance(parsed.get("providerID"), str)

    def test_write_resolves_project_id_from_directory(self):
        # opencode desktop scopes its session list by project_id; a pulled
        # foreign session must land in the project its cwd belongs to (from
        # project_directory), NOT the 'global' bucket, or it won't display.
        a = OpencodeAdapter(db_path=self.db)
        a.write_sessions([{
            "id": "hermes:proj-probe", "title": "P", "started_at": 1.0,
            "cwd": "e:/opencode/myproj",   # case-insensitive match to proj_a
            "messages": [{"session_id": "hermes:proj-probe", "role": "user",
                          "content": "hi", "timestamp": 1.0}]}])
        idmap = json.loads(
            (self.db.with_name(".hermes-sync-idmap.json")).read_text())
        local = idmap["hermes:proj-probe"]
        con = sqlite3.connect(self.db)
        pid = con.execute("SELECT project_id FROM session WHERE id=?",
                          (local,)).fetchone()[0]
        con.close()
        self.assertEqual(pid, "proj_a", pid)

    def test_write_existing_updates_in_place(self):
        a = OpencodeAdapter(db_path=self.db)
        existing = a.read_sessions()[0]
        existing["title"] = "Renamed"
        stats = a.write_sessions([existing])
        self.assertEqual(stats["updated"], 1)
        self.assertEqual(stats["imported"], 0)
        self.assertEqual(a.read_sessions()[0]["title"], "Renamed")


if __name__ == "__main__":
    unittest.main(verbosity=2)
