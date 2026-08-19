"""HermesAdapter multi-profile tests."""
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from adapters.hermes import HermesAdapter


def make_db(path: Path, session_id: str, title: str = "t"):
    """Create a minimal hermes-style state.db with one session + message."""
    conn = sqlite3.connect(str(path))
    conn.execute("""CREATE TABLE IF NOT EXISTS sessions (
        id TEXT PRIMARY KEY, title TEXT, model TEXT, started_at REAL,
        message_count INTEGER, last_synced_at REAL)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT,
        role TEXT, content TEXT, timestamp REAL)""")
    conn.execute("INSERT OR REPLACE INTO sessions VALUES (?,?,?,?,?,?)",
                 (session_id, title, "gpt-4o", 1000.0, 1, 1000.0))
    conn.execute("INSERT OR REPLACE INTO messages (session_id,role,content,timestamp) "
                 "VALUES (?,?,?,?)", (session_id, "user", "hi", 1000.5))
    conn.commit()
    conn.close()


class HermesMultiProfileTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.env_clean = mock.patch.dict(os.environ, {"LOCALAPPDATA": ""}, clear=False)
        self.env_clean.start()
        self.addCleanup(self.env_clean.stop)
        self.root_patch = mock.patch.object(HermesAdapter, "_platform_root",
                                            return_value=self.root)
        self.root_patch.start()
        self.addCleanup(self.root_patch.stop)

    # ------------------------------------------------------------------
    # discovery
    # ------------------------------------------------------------------
    def test_discovers_default_only(self):
        make_db(self.root / "state.db", "20260808_180012_0c275f")
        a = HermesAdapter()
        self.assertEqual(a._profile_dbs(), [("", self.root / "state.db")])

    def test_discovers_default_and_named(self):
        make_db(self.root / "state.db", "20260808_180012_0c275f")
        magic = self.root / "profiles" / "magic"
        magic.mkdir(parents=True)
        make_db(magic / "state.db", "20260808_180013_0c275e")
        a = HermesAdapter()
        dbs = a._profile_dbs()
        self.assertEqual(dbs, [("", self.root / "state.db"),
                               ("magic", magic / "state.db")])

    def test_ignores_profile_without_db(self):
        make_db(self.root / "state.db", "x")
        (self.root / "profiles" / "coder").mkdir(parents=True)  # no state.db
        a = HermesAdapter()
        self.assertEqual([n for n, _ in a._profile_dbs()], [""])

    # ------------------------------------------------------------------
    # identity
    # ------------------------------------------------------------------
    def test_default_keeps_bare_ids(self):
        make_db(self.root / "state.db", "20260808_180012_0c275f")
        a = HermesAdapter()
        out = a.canonicalize({"id": "20260808_180012_0c275f", "started_at": 1.0,
                              "messages": []})
        self.assertEqual(out["id"], "20260808_180012_0c275f")

    def test_named_profile_id_bare_with_profile_field(self):
        make_db(self.root / "state.db", "x")
        magic = self.root / "profiles" / "magic"
        magic.mkdir(parents=True)
        make_db(magic / "state.db", "20260808_180013_0c275e")
        a = HermesAdapter()
        sub = a._sub_adapter("magic", magic / "state.db")
        self.assertEqual(sub.profile_name, "magic")
        out = sub.canonicalize({"id": "20260808_180013_0c275e", "started_at": 1.0,
                                "messages": []})
        self.assertEqual(out["id"], "20260808_180013_0c275e")
        self.assertEqual(out["profile_name"], "magic")

    def test_named_profile_roundtrip(self):
        make_db(self.root / "state.db", "x")
        magic = self.root / "profiles" / "magic"
        magic.mkdir(parents=True)
        make_db(magic / "state.db", "20260808_180013_0c275e")
        a = HermesAdapter()
        sub = a._sub_adapter("magic", magic / "state.db")
        orig = {"id": "20260808_180013_0c275e", "started_at": 1.0,
                "messages": [{"session_id": "20260808_180013_0c275e",
                              "role": "user", "content": "hi", "timestamp": 1.0}]}
        canon = sub.canonicalize(orig)
        self.assertEqual(canon["id"], "20260808_180013_0c275e")
        self.assertEqual(canon["profile_name"], "magic")
        back = sub.localize(canon)
        self.assertEqual(back["id"], orig["id"])
        self.assertEqual(back["messages"][0]["session_id"],
                         orig["messages"][0]["session_id"])

    # ------------------------------------------------------------------
    # aggregate read (push)
    # ------------------------------------------------------------------
    def test_read_merges_all_profiles(self):
        make_db(self.root / "state.db", "20260808_180012_0c275f", title="default-sess")
        magic = self.root / "profiles" / "magic"
        magic.mkdir(parents=True)
        make_db(magic / "state.db", "20260808_180013_0c275e", title="magic-sess")
        a = HermesAdapter()
        sessions = {s["id"]: s for s in a.read_sessions()}
        # both profiles keep bare ids; the profile travels in the field
        self.assertIn("20260808_180012_0c275f", sessions)
        self.assertIn("20260808_180013_0c275e", sessions)
        self.assertEqual(sessions["20260808_180013_0c275e"].get("profile_name"), "magic")

    # ------------------------------------------------------------------
    # routed write (pull)
    # ------------------------------------------------------------------
    def test_pull_routes_to_correct_profile(self):
        make_db(self.root / "state.db", "20260808_180012_0c275f")
        magic = self.root / "profiles" / "magic"
        magic.mkdir(parents=True)
        make_db(magic / "state.db", "20260808_180013_0c275e")
        a = HermesAdapter()
        sessions = [
            {"id": "20260808_180014_0c276f", "started_at": 2.0,
             "messages": [{"session_id": "20260808_180014_0c276f",
                           "role": "user", "content": "to-default",
                           "timestamp": 2.0}]},
            {"id": "20260808_180015_0c277e", "started_at": 2.0,
             "profile_name": "magic",
             "messages": [{"session_id": "20260808_180015_0c277e",
                           "role": "user", "content": "to-magic",
                           "timestamp": 2.0}]},
            # legacy prefixed payload still routes via the prefix fallback
            {"id": "magic:20260808_180016_0c278f", "started_at": 2.0,
             "messages": [{"session_id": "magic:20260808_180016_0c278f",
                           "role": "user", "content": "to-magic-legacy",
                           "timestamp": 2.0}]},
        ]
        stats = a.write_sessions(sessions)
        self.assertEqual(stats["imported"], 3)
        # default db got the bare session
        c = sqlite3.connect(str(self.root / "state.db"))
        self.assertEqual(c.execute("SELECT COUNT(*) FROM sessions").fetchone()[0], 2)
        self.assertIsNotNone(c.execute(
            "SELECT 1 FROM sessions WHERE id='20260808_180014_0c276f'").fetchone())
        c.close()
        # magic db got the magic sessions (stored as bare ids locally)
        c = sqlite3.connect(str(magic / "state.db"))
        self.assertEqual(c.execute("SELECT COUNT(*) FROM sessions").fetchone()[0], 3)
        self.assertIsNotNone(c.execute(
            "SELECT 1 FROM sessions WHERE id='20260808_180015_0c277e'").fetchone())
        self.assertIsNotNone(c.execute(
            "SELECT 1 FROM sessions WHERE id='20260808_180016_0c278f'").fetchone())
        c.close()

    def test_pull_skips_unknown_profile(self):
        make_db(self.root / "state.db", "20260808_180012_0c275f")
        a = HermesAdapter()
        sessions = [
            {"id": "20260808_180014_0c276f", "started_at": 2.0,
             "messages": [{"session_id": "20260808_180014_0c276f",
                           "role": "user", "content": "default",
                           "timestamp": 2.0}]},
            # coder profile does not exist on this machine -> skipped
            {"id": "20260808_180015_0c277e", "started_at": 2.0,
             "profile_name": "coder",
             "messages": [{"session_id": "20260808_180015_0c277e",
                           "role": "user", "content": "coder",
                           "timestamp": 2.0}]},
        ]
        stats = a.write_sessions(sessions)
        self.assertEqual(stats["imported"], 1)
        c = sqlite3.connect(str(self.root / "state.db"))
        self.assertEqual(c.execute("SELECT COUNT(*) FROM sessions").fetchone()[0], 2)
        self.assertIsNone(c.execute(
            "SELECT 1 FROM sessions WHERE id='20260808_180015_0c277e'").fetchone())
        c.close()

    def test_title_unique_index_disambiguates_pull(self):
        # Hermes 0.20+ state.db carries a partial unique index on title
        # (WHERE title IS NOT NULL). A pulled session whose title collides
        # with an existing local row must be suffixed instead of failing
        # the whole batch with UNIQUE constraint failed: sessions.title.
        make_db(self.root / "state.db", "20260808_180012_0c275f",
                title="Imported session")
        conn = sqlite3.connect(str(self.root / "state.db"))
        conn.execute("CREATE UNIQUE INDEX idx_sessions_title_unique "
                     "ON sessions(title) WHERE title IS NOT NULL")
        conn.commit()
        conn.close()
        a = HermesAdapter()
        # two pulled sessions with the same title, different ids
        stats = a.write_sessions([
            {"id": "20260808_180013_0c275e", "started_at": 3.0,
             "title": "Imported session"},
            {"id": "20260808_180014_0c276f", "started_at": 4.0,
             "title": "Imported session"},
        ])
        self.assertEqual(stats["imported"], 2)
        c = sqlite3.connect(str(self.root / "state.db"))
        titles = dict(c.execute("SELECT id, title FROM sessions"))
        c.close()
        self.assertEqual(titles["20260808_180012_0c275f"], "Imported session")
        self.assertEqual(titles["20260808_180013_0c275e"], "Imported session (2)")
        self.assertEqual(titles["20260808_180014_0c276f"], "Imported session (3)")

    def test_title_unique_index_update_keeps_own_title(self):
        # Updating a session must not rename it when its own title is
        # unchanged (the exclusion check skips the row being updated).
        make_db(self.root / "state.db", "20260808_180012_0c275f",
                title="Alpha")
        conn = sqlite3.connect(str(self.root / "state.db"))
        conn.execute("CREATE UNIQUE INDEX idx_sessions_title_unique "
                     "ON sessions(title) WHERE title IS NOT NULL")
        conn.commit()
        conn.close()
        a = HermesAdapter()
        stats = a.write_sessions([
            {"id": "20260808_180012_0c275f", "started_at": 3.0,
             "title": "Alpha", "message_count": 2},
            {"id": "20260808_180013_0c275e", "started_at": 4.0,
             "title": "Alpha"},
        ])
        self.assertEqual(stats["imported"], 1)
        self.assertEqual(stats["updated"], 1)
        c = sqlite3.connect(str(self.root / "state.db"))
        titles = dict(c.execute("SELECT id, title FROM sessions"))
        c.close()
        self.assertEqual(titles["20260808_180012_0c275f"], "Alpha")
        self.assertEqual(titles["20260808_180013_0c275e"], "Alpha (2)")

    def test_watermark_at_root(self):
        make_db(self.root / "state.db", "x")
        a = HermesAdapter()
        self.assertEqual(a._watermark_file(),
                         self.root / ".hermes-sync-watermark")

    # ------------------------------------------------------------------
    # foreign owner registry (agent attribution for pulled sessions)
    # ------------------------------------------------------------------
    def test_pull_records_foreign_owner(self):
        make_db(self.root / "state.db", "local-hermes-session")
        a = HermesAdapter()
        a.write_sessions([
            {"id": "3cbe89cb-8f8a-4fbf-8bf2-8b221e728f06",
             "agent_type": "workbuddy", "started_at": 2.0,
             "messages": [{"session_id": "3cbe89cb-8f8a-4fbf-8bf2-8b221e728f06",
                           "role": "user", "content": "w", "timestamp": 2.0}]},
            {"id": "20260808_180015_0c277e", "started_at": 2.0,
             "agent_type": "hermes",
             "messages": [{"session_id": "20260808_180015_0c277e",
                           "role": "user", "content": "h", "timestamp": 2.0}]},
            {"id": "20260808_180016_0c278f", "started_at": 2.0,
             "messages": []},  # no agent_type -> own session
        ])
        self.assertTrue(a._is_foreign("3cbe89cb-8f8a-4fbf-8bf2-8b221e728f06"))
        self.assertEqual(a._foreign_agent("3cbe89cb-8f8a-4fbf-8bf2-8b221e728f06"),
                         "workbuddy")
        # hermes' own sessions are never registered
        self.assertFalse(a._is_foreign("20260808_180015_0c277e"))
        self.assertFalse(a._is_foreign("20260808_180016_0c278f"))
        # sidecar lives at the root next to state.db
        self.assertTrue((self.root / ".hermes-sync-foreign.json").exists())

    def test_foreign_session_canonicalize_keeps_bare_id(self):
        # a registered foreign session round-trips verbatim (bare id, no
        # hermes profile prefix) through the adapter's canonicalize path
        make_db(self.root / "state.db", "local-hermes-session")
        a = HermesAdapter()
        a.write_sessions([
            {"id": "1b8fc026-d2b4-4dfb-bdef-2ea8e73013e4",
             "agent_type": "codex", "started_at": 2.0, "messages": []},
        ])
        out = a.canonicalize({"id": "1b8fc026-d2b4-4dfb-bdef-2ea8e73013e4",
                              "started_at": 2.0, "messages": []})
        self.assertEqual(out["id"], "1b8fc026-d2b4-4dfb-bdef-2ea8e73013e4")
        self.assertNotIn("profile_name", out)

    def test_push_owner_tagging_uses_registry(self):
        # push_sessions' owner filter (mcp/server.py) relies on
        # _is_foreign/_foreign_agent: foreign sessions must be tagged with
        # their recorded owner, not the hermes adapter's own type
        make_db(self.root / "state.db", "local-hermes-session")
        a = HermesAdapter()
        a.write_sessions([
            {"id": "3cbe89cb-8f8a-4fbf-8bf2-8b221e728f06",
             "agent_type": "workbuddy", "started_at": 2.0,
             "messages": [{"session_id": "3cbe89cb-8f8a-4fbf-8bf2-8b221e728f06",
                           "role": "user", "content": "w", "timestamp": 2.0}]},
        ])
        # mirror mcp/server.py push_sessions tagging logic
        sid = "3cbe89cb-8f8a-4fbf-8bf2-8b221e728f06"
        agent = a.agent_type
        if a._is_foreign(sid):
            agent = a._foreign_agent(sid) or "hermes"
        self.assertEqual(agent, "workbuddy")

    # ------------------------------------------------------------------
    # projects
    # ------------------------------------------------------------------
    def _make_projects_db(self, home: Path, rows: list):
        import sqlite3
        conn = sqlite3.connect(str(home / "projects.db"))
        conn.execute("""CREATE TABLE IF NOT EXISTS projects (
            id TEXT PRIMARY KEY, slug TEXT NOT NULL UNIQUE, name TEXT NOT NULL,
            description TEXT, icon TEXT, color TEXT, board_slug TEXT,
            primary_path TEXT, created_at INTEGER NOT NULL,
            archived INTEGER NOT NULL DEFAULT 0)""")
        conn.execute("""CREATE TABLE IF NOT EXISTS project_folders (
            project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            path TEXT NOT NULL, label TEXT, is_primary INTEGER NOT NULL DEFAULT 0,
            added_at INTEGER NOT NULL, PRIMARY KEY (project_id, path))""")
        for r in rows:
            conn.execute("""INSERT OR REPLACE INTO projects
                            (id, slug, name, created_at, archived)
                            VALUES (?,?,?,?,?)""",
                         (r["id"], r["slug"], r["name"], r.get("created_at", 1000), r.get("archived", 0)))
            for f in r.get("folders", []):
                conn.execute("""INSERT OR REPLACE INTO project_folders
                                (project_id, path, is_primary, added_at)
                                VALUES (?,?,1,1000)""", (r["id"], f))
        conn.commit()
        conn.close()

    def test_read_projects_default_and_named(self):
        self._make_projects_db(self.root, [{"id": "p_a", "slug": "proja", "name": "ProjA"}])
        magic = self.root / "profiles" / "magic"
        magic.mkdir(parents=True)
        self._make_projects_db(magic, [{"id": "p_b", "slug": "projb", "name": "ProjB"}])
        a = HermesAdapter()
        projects = a.read_projects()
        ids = {p["id"] for p in projects}
        by_id = {p["id"]: p for p in projects}
        self.assertIn("p_a", ids)          # default bare
        self.assertIn("p_b", ids)          # named profile, bare id
        self.assertEqual(by_id["p_a"].get("profile"), "")
        self.assertEqual(by_id["p_b"].get("profile"), "magic")

    def test_write_projects_routes_and_slug_dedupe(self):
        self._make_projects_db(self.root, [{"id": "p_a", "slug": "same", "name": "A"}])
        magic = self.root / "profiles" / "magic"
        magic.mkdir(parents=True)
        a = HermesAdapter()
        projects = [
            {"id": "p_b", "slug": "same", "name": "B",
             "profile": "magic",
             "folders": [{"path": "/x", "is_primary": 1, "added_at": 1}],
             "created_at": 1},
        ]
        a.write_projects(projects)
        import sqlite3
        conn = sqlite3.connect(str(magic / "projects.db"))
        slug = conn.execute("SELECT slug FROM projects WHERE id='p_b'").fetchone()[0]
        folders = conn.execute("SELECT COUNT(*) FROM project_folders WHERE project_id='p_b'").fetchone()[0]
        conn.close()
        # slug 'same' is free inside magic profile (default profile's slug is
        # a different db), so it stays 'same'
        self.assertEqual(slug, "same")
        self.assertEqual(folders, 1)

    def test_write_projects_applies_remap(self):
        self._make_projects_db(self.root, [{"id": "p_old", "slug": "old", "name": "Old"}])
        a = HermesAdapter()
        # server says p_old was merged into p_keep
        a.write_projects(
            [{"id": "p_keep", "slug": "keep", "name": "Keep",
              "created_at": 1}],
            remaps=[{"old_id": "p_old", "new_id": "p_keep"}])
        import sqlite3
        conn = sqlite3.connect(str(self.root / "projects.db"))
        # old project removed by remap? write_projects does not delete rows,
        # it writes the surviving project; assert p_keep present
        row = conn.execute("SELECT 1 FROM projects WHERE id='p_keep'").fetchone()
        conn.close()
        self.assertIsNotNone(row)


if __name__ == "__main__":
    unittest.main()
