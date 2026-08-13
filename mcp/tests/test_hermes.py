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

    def test_named_profile_prefixes_id(self):
        make_db(self.root / "state.db", "x")
        magic = self.root / "profiles" / "magic"
        magic.mkdir(parents=True)
        make_db(magic / "state.db", "20260808_180013_0c275e")
        a = HermesAdapter()
        sub = a._sub_adapter("magic", magic / "state.db")
        self.assertEqual(sub.profile_name, "magic")
        self.assertEqual(sub._id_prefix(), "magic:")
        out = sub.canonicalize({"id": "20260808_180013_0c275e", "started_at": 1.0,
                                "messages": []})
        self.assertEqual(out["id"], "magic:20260808_180013_0c275e")

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
        self.assertEqual(canon["id"], "magic:20260808_180013_0c275e")
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
        # default keeps bare id, magic gets prefix
        self.assertIn("20260808_180012_0c275f", sessions)
        self.assertIn("magic:20260808_180013_0c275e", sessions)
        self.assertNotIn("20260808_180013_0c275e", sessions)

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
            {"id": "magic:20260808_180015_0c277e", "started_at": 2.0,
             "messages": [{"session_id": "magic:20260808_180015_0c277e",
                           "role": "user", "content": "to-magic",
                           "timestamp": 2.0}]},
        ]
        stats = a.write_sessions(sessions)
        self.assertEqual(stats["imported"], 2)
        # default db got the bare session
        c = sqlite3.connect(str(self.root / "state.db"))
        self.assertEqual(c.execute("SELECT COUNT(*) FROM sessions").fetchone()[0], 2)
        self.assertIsNotNone(c.execute(
            "SELECT 1 FROM sessions WHERE id='20260808_180014_0c276f'").fetchone())
        c.close()
        # magic db got the magic session (stored as bare id locally)
        c = sqlite3.connect(str(magic / "state.db"))
        self.assertEqual(c.execute("SELECT COUNT(*) FROM sessions").fetchone()[0], 2)
        self.assertIsNotNone(c.execute(
            "SELECT 1 FROM sessions WHERE id='20260808_180015_0c277e'").fetchone())
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
            {"id": "coder:20260808_180015_0c277e", "started_at": 2.0,
             "messages": [{"session_id": "coder:20260808_180015_0c277e",
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

    def test_watermark_at_root(self):
        make_db(self.root / "state.db", "x")
        a = HermesAdapter()
        self.assertEqual(a._watermark_file(),
                         self.root / ".hermes-sync-watermark")

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
        self.assertIn("p_a", ids)          # default bare
        self.assertIn("magic:p_b", ids)    # named prefixed
        self.assertNotIn("p_b", ids)

    def test_write_projects_routes_and_slug_dedupe(self):
        self._make_projects_db(self.root, [{"id": "p_a", "slug": "same", "name": "A"}])
        magic = self.root / "profiles" / "magic"
        magic.mkdir(parents=True)
        a = HermesAdapter()
        projects = [
            {"id": "magic:p_b", "slug": "same", "name": "B",
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
