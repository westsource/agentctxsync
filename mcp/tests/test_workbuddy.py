"""Round-trip tests for the workbuddy adapter (JSONL + workbuddy.db).

Fixture mirrors the real WorkBuddy 5.3.13 store layout verified on
2026-08-16: ~/.workbuddy/projects/<slug>/<conversationId>.jsonl events plus
a SQLite workbuddy.db ``sessions`` table (epoch-ms timestamps). All paths
live inside the tempdir so tests never touch the real ~/.workbuddy.
"""

import json
import sqlite3
import sys
import tempfile
import unittest
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from adapters.workbuddy import WorkBuddyAdapter  # noqa: E402

USER_ID = "c5a8ab07-321f-4d75-9336-b130b422582c"
SID = "aaaaaaaa-1111-2222-3333-444444444444"
TS_MS = 1786804436000  # epoch ms


def make_db(home: Path, cwd: str, title: str = "Fixture chat"):
    home.mkdir(parents=True, exist_ok=True)
    db = home / "workbuddy.db"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE sessions ("
        " id TEXT PRIMARY KEY, cwd TEXT NOT NULL, user_id TEXT NOT NULL,"
        " title TEXT, custom_title TEXT, status TEXT NOT NULL,"
        " created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL,"
        " deleted_at INTEGER, is_playground INTEGER NOT NULL,"
        " source_mode TEXT, is_background_automation INTEGER, mode TEXT,"
        " model TEXT, expert_id TEXT, expert_locale TEXT,"
        " expert_runtime_identity TEXT, expert_marketplace TEXT,"
        " permission_mode TEXT, last_activity_at INTEGER,"
        " use_sandbox_cli INTEGER, project_id TEXT, plugin_context_json TEXT,"
        " last_user_prompt_expert_selection TEXT)")
    conn.execute(
        "INSERT INTO sessions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,"
        "?,?,?,?,?)",
        (SID, cwd, USER_ID, title, None, "completed", TS_MS, TS_MS + 5000,
         None, 0, "design", None, "design", "custom-local:deepseek-v4-flash",
         None, None, None, None, None, TS_MS + 5000, None, None, None, None))
    conn.commit()
    conn.close()


def write_jsonl(home: Path, cwd: str, events: list[dict]) -> Path:
    slug = WorkBuddyAdapter.slugify(cwd)
    p = home / "projects" / slug / f"{SID}.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("\n".join(json.dumps(e, ensure_ascii=False)
                           for e in events) + "\n", encoding="utf-8")
    return p


def make_fixture(home: Path, cwd: str) -> Path:
    """Full local store: db row + jsonl with every event type."""
    make_db(home, cwd)
    events = [
        {"timestamp": TS_MS, "type": "ai-title", "aiTitle": "Fixture chat",
         "sessionId": SID, "cwd": cwd},
        {"id": str(uuid.uuid4()), "timestamp": TS_MS + 1000, "type": "message",
         "role": "user", "status": "completed",
         "content": [{"type": "input_text", "text": "hello workbuddy"}],
         "sessionId": SID, "cwd": cwd},
        {"id": str(uuid.uuid4()), "timestamp": TS_MS + 2000,
         "type": "reasoning", "content": [],
         "rawContent": [{"type": "reasoning_text", "text": "thinking hard"}],
         "sessionId": SID, "cwd": cwd},
        {"id": str(uuid.uuid4()), "timestamp": TS_MS + 3000, "type": "message",
         "role": "assistant", "status": "completed",
         "content": [{"type": "output_text", "text": "hi there"}],
         "sessionId": SID, "cwd": cwd},
        {"id": str(uuid.uuid4()), "timestamp": TS_MS + 4000,
         "type": "function_call", "callId": "call_abc", "name": "Skill",
         "arguments": '{"skill": "x"}', "sessionId": SID, "cwd": cwd},
        {"id": str(uuid.uuid4()), "timestamp": TS_MS + 5000,
         "type": "function_call_result", "callId": "call_abc", "name": "Skill",
         "status": "completed", "output": {"type": "text", "text": "done"},
         "sessionId": SID, "cwd": cwd},
    ]
    write_jsonl(home, cwd, events)
    return home


def new_adapter(td: Path, cwd: str | None = None):
    """Adapter whose store lives inside the tempdir."""
    home = td / ".workbuddy"
    home.mkdir(parents=True, exist_ok=True)
    return WorkBuddyAdapter(home), home, cwd or (td / "WorkProject").as_posix()


class WorkBuddyReadTest(unittest.TestCase):
    def test_read_full_session(self):
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            cwd = (td / "WorkProject").as_posix()
            home = make_fixture(td / ".workbuddy", cwd)
            a = WorkBuddyAdapter(home)
            sessions = a.read_sessions()
            self.assertEqual(len(sessions), 1)
            s = sessions[0]
            self.assertEqual(s["id"], SID)
            self.assertEqual(s["title"], "Fixture chat")
            self.assertEqual(s["model"], "custom-local:deepseek-v4-flash")
            self.assertEqual(s["cwd"], cwd)
            self.assertAlmostEqual(s["started_at"], TS_MS / 1000.0, places=3)
            msgs = s["messages"]
            self.assertEqual(len(msgs), 5)
            self.assertEqual(msgs[0]["role"], "user")
            self.assertEqual(msgs[0]["content"], "hello workbuddy")
            self.assertEqual(msgs[1]["role"], "assistant")
            self.assertEqual(msgs[1]["reasoning"], "thinking hard")
            self.assertEqual(msgs[2]["content"], "hi there")
            self.assertEqual(msgs[3]["role"], "assistant")
            self.assertEqual(msgs[3]["tool_name"], "Skill")
            self.assertEqual(msgs[3]["tool_call_id"], "call_abc")
            self.assertEqual(msgs[4]["role"], "tool")
            self.assertEqual(msgs[4]["content"], "done")
            for m in msgs:
                self.assertEqual(m["session_id"], SID)

    def test_read_empty_store(self):
        with tempfile.TemporaryDirectory() as td:
            a, home, _ = new_adapter(Path(td))
            self.assertEqual(a.read_sessions(), [])
            self.assertEqual(a.status()["sessions"], 0)


class WorkBuddyWriteTest(unittest.TestCase):
    def _canonical(self, cwd: str, sid: str = SID,
                   title: str = "Written chat") -> dict:
        return {
            "id": sid,
            "started_at": TS_MS / 1000.0,
            "title": title,
            "model": "deepseek-v4-flash",
            "cwd": cwd,
            "messages": [
                {"session_id": sid, "role": "user",
                 "content": "from server", "timestamp": (TS_MS + 1000) / 1000.0},
                {"session_id": sid, "role": "assistant",
                 "content": "server reply",
                 "timestamp": (TS_MS + 2000) / 1000.0},
                {"session_id": sid, "role": "tool",
                 "content": "tool out", "tool_name": "search",
                 "tool_call_id": "call_zzz",
                 "timestamp": (TS_MS + 3000) / 1000.0},
            ],
        }

    def test_write_creates_store(self):
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            a, home, cwd = new_adapter(td)
            stats = a.write_sessions([self._canonical(cwd)])
            self.assertEqual(stats["imported"], 1)
            self.assertEqual(stats["new_messages"], 3)
            slug = WorkBuddyAdapter.slugify(cwd)
            proj = home / "projects" / slug
            self.assertTrue(proj.is_dir())
            # the cwd directory itself is auto-created (WorkBuddy requires it)
            self.assertTrue(Path(cwd).is_dir())
            jf = proj / f"{SID}.jsonl"
            self.assertTrue(jf.exists())
            events = [json.loads(l) for l in
                      jf.read_text(encoding="utf-8").splitlines()]
            self.assertEqual(events[0]["type"], "ai-title")
            self.assertEqual(events[0]["aiTitle"], "Written chat")
            kinds = [e["type"] for e in events]
            self.assertEqual(kinds.count("message"), 2)
            self.assertEqual(kinds.count("function_call_result"), 1)
            conn = sqlite3.connect(home / "workbuddy.db")
            row = conn.execute("SELECT id, cwd, user_id, title, status, "
                               "created_at, is_playground FROM sessions "
                               "WHERE id=?", (SID,)).fetchone()
            conn.close()
            self.assertIsNotNone(row)
            self.assertEqual(row[1], cwd)
            self.assertEqual(row[3], "Written chat")
            self.assertEqual(row[5], TS_MS)

    def test_roundtrip(self):
        with tempfile.TemporaryDirectory() as td:
            a, home, cwd = new_adapter(Path(td))
            a.write_sessions([self._canonical(cwd)])
            sessions = a.read_sessions()
            self.assertEqual(len(sessions), 1)
            s = sessions[0]
            self.assertEqual(s["title"], "Written chat")
            self.assertEqual(len(s["messages"]), 3)
            roles = [m["role"] for m in s["messages"]]
            self.assertEqual(roles, ["user", "assistant", "tool"])

    def test_append_to_existing_session(self):
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            cwd = (td / "WorkProject").as_posix()
            home = make_fixture(td / ".workbuddy", cwd)
            a = WorkBuddyAdapter(home)
            # same id; 3 messages matching the fixture's (role, ts) dedupe
            # keys (user@+1000, reasoning@+2000 -> assistant, result@+5000
            # -> tool) plus one genuinely new message
            c = self._canonical(cwd, title="Fixture chat")
            c["messages"] = [
                {"session_id": SID, "role": "user",
                 "content": "hello workbuddy",
                 "timestamp": (TS_MS + 1000) / 1000.0},
                {"session_id": SID, "role": "assistant",
                 "content": "",
                 "timestamp": (TS_MS + 2000) / 1000.0},
                {"session_id": f"workbuddy:{SID}", "role": "tool",
                 "content": "done", "tool_name": "Skill",
                 "tool_call_id": "call_abc",
                 "timestamp": (TS_MS + 5000) / 1000.0},
                {"session_id": SID, "role": "assistant",
                 "content": "one more", "timestamp": (TS_MS + 6000) / 1000.0},
            ]
            stats = a.write_sessions([c])
            self.assertEqual(stats["imported"], 0)
            self.assertEqual(stats["updated"], 1)
            self.assertEqual(stats["new_messages"], 1)
            self.assertEqual(stats["duplicates"], 3)
            sessions = a.read_sessions()
            self.assertEqual(len(sessions[0]["messages"]), 6)

    def test_idempotent(self):
        with tempfile.TemporaryDirectory() as td:
            a, home, cwd = new_adapter(Path(td))
            a.write_sessions([self._canonical(cwd)])
            stats = a.write_sessions([self._canonical(cwd)])
            self.assertEqual(stats["new_messages"], 0)
            self.assertGreater(stats["duplicates"], 0)
            self.assertEqual(stats["imported"], 0)

    def test_foreign_id_kept(self):
        """A codex:-prefixed session must keep its bare id locally."""
        with tempfile.TemporaryDirectory() as td:
            a, home, cwd = new_adapter(Path(td))
            fid = "11111111-1111-1111-1111-111111111111"
            c = self._canonical(cwd, sid=fid)
            c["id"] = f"codex:{fid}"
            for m in c["messages"]:
                m["session_id"] = c["id"]
            stats = a.write_sessions([c])
            self.assertEqual(stats["imported"], 1)
            slug = WorkBuddyAdapter.slugify(cwd)
            self.assertTrue((home / "projects" / slug / f"{fid}.jsonl").exists())
            sessions = a.read_sessions()
            self.assertEqual(sessions[0]["id"], fid)  # foreign id unchanged


class WorkBuddySlugTest(unittest.TestCase):
    def test_slugify(self):
        self.assertEqual(WorkBuddyAdapter.slugify(r"F:\OpenCode\agentctxsync"),
                         "f-OpenCode-agentctxsync")
        self.assertEqual(WorkBuddyAdapter.slugify(r"C:\Users\rong\HermesSyncTest"),
                         "c-Users-rong-HermesSyncTest")
        self.assertEqual(WorkBuddyAdapter.slugify("/home/user/proj"),
                         "home-user-proj")
        self.assertEqual(WorkBuddyAdapter.slugify("C:/Users/a/b"),
                         "c-Users-a-b")
        self.assertEqual(WorkBuddyAdapter.slugify("E:\\"), "e")
        self.assertEqual(WorkBuddyAdapter.slugify("E:/"), "e")


if __name__ == "__main__":
    unittest.main()
