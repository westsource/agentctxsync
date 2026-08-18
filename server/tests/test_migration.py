"""Functional test for scripts/migrate-id-scheme.py against a scratch PG
schema. Skips when PostgreSQL is unreachable (CI has no database)."""
import os
import subprocess
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "migrate-id-scheme.py"
BASE_DSN = os.environ.get(
    "HERMES_SYNC_PG_DSN",
    "postgresql://agentctxsync:agentctxsync@localhost:5432/agentctxsync")


def _pg_up() -> bool:
    try:
        import psycopg2
        pg = psycopg2.connect(BASE_DSN, connect_timeout=3)
        pg.close()
        return True
    except Exception:
        return False


@unittest.skipUnless(_pg_up(), "PostgreSQL not reachable")
class MigrationTest(unittest.TestCase):
    SCHEMA = f"migtest_{os.getpid()}"

    @classmethod
    def setUpClass(cls):
        import psycopg2
        pg = psycopg2.connect(BASE_DSN)
        cur = pg.cursor()
        cur.execute(f'CREATE SCHEMA "{cls.SCHEMA}"')
        cur.execute(f'''CREATE TABLE "{cls.SCHEMA}".sessions (
            id TEXT, workspace_id INTEGER, title TEXT,
            agent_type TEXT DEFAULT 'hermes', profile_name TEXT,
            PRIMARY KEY (workspace_id, id))''')
        cur.execute(f'''CREATE TABLE "{cls.SCHEMA}".messages (
            id SERIAL, session_id TEXT, workspace_id INTEGER,
            role TEXT, content TEXT, timestamp DOUBLE PRECISION,
            agent_type TEXT DEFAULT 'hermes',
            PRIMARY KEY (workspace_id, session_id, id))''')
        cur.execute(f'''CREATE TABLE "{cls.SCHEMA}".projects (
            id TEXT, workspace_id INTEGER, slug TEXT, name TEXT,
            created_at DOUBLE PRECISION, agent_type TEXT DEFAULT 'hermes',
            profile TEXT DEFAULT '', PRIMARY KEY (workspace_id, id))''')
        cur.execute(f'''CREATE TABLE "{cls.SCHEMA}".project_folders (
            workspace_id INTEGER, project_id TEXT, path TEXT,
            PRIMARY KEY (workspace_id, project_id, path))''')
        cur.execute(f'''CREATE TABLE "{cls.SCHEMA}".project_remap (
            workspace_id INTEGER, old_id TEXT, new_id TEXT,
            PRIMARY KEY (workspace_id, old_id))''')
        cls.dsn = (BASE_DSN + "?options=-csearch_path%3D"
                   + cls.SCHEMA)
        pg.commit()
        pg.close()

    @classmethod
    def tearDownClass(cls):
        import psycopg2
        pg = psycopg2.connect(BASE_DSN)
        cur = pg.cursor()
        cur.execute(f'DROP SCHEMA IF EXISTS "{cls.SCHEMA}" CASCADE')
        pg.commit()
        pg.close()

    def setUp(self):
        import psycopg2
        pg = psycopg2.connect(self.dsn)
        cur = pg.cursor()
        for t in ("project_remap", "project_folders", "projects",
                  "messages", "sessions"):
            cur.execute(f'DELETE FROM "{t}"')
        pg.commit()
        pg.close()

    def _seed(self, rows):
        import psycopg2
        pg = psycopg2.connect(self.dsn)
        cur = pg.cursor()
        for sql, params in rows:
            cur.execute(sql, params)
        pg.commit()
        pg.close()

    def _run(self, *extra):
        return subprocess.run(
            [sys.executable, str(SCRIPT), "--dsn", self.dsn, *extra],
            capture_output=True, text=True, cwd=str(REPO))

    def test_migrate_normalizes_prefixed_ids(self):
        self._seed([
            (f'INSERT INTO sessions (id, workspace_id, title) VALUES '
             f"('codex:019fc071-fab4-7661-9a0b-2afaa65cbb31', 1, 'c'),"
             f"('magic:20260809_120000_testabc', 1, 'm'),"
             f"('workbuddy:1b8fc026-d2b4-4dfb-bdef-2ea8e73013e4', 1, 'w'),"
             f"('20260808_180012_0c275f', 1, 'bare')", ()),
            (f'INSERT INTO messages (session_id, workspace_id, role, content, timestamp) '
             f"VALUES ('codex:019fc071-fab4-7661-9a0b-2afaa65cbb31', 1, 'user', 'x', 1.0)",
             ()),
            (f"INSERT INTO projects (id, workspace_id, slug, name, created_at) "
             f"VALUES ('magic:p_abc', 1, 'p1', 'P1', 1.0), ('p_bare', 1, 'p2', 'P2', 2.0)",
             ()),
            (f'INSERT INTO project_folders (workspace_id, project_id, path) '
             f"VALUES (1, 'magic:p_abc', '/x')", ()),
            (f"INSERT INTO project_remap (workspace_id, old_id, new_id) "
             f"VALUES (1, 'magic:p_old', 'magic:p_abc')", ()),
        ])
        r = self._run("--apply", "--workspace", "1")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("0 collision(s)", r.stdout)
        import psycopg2
        pg = psycopg2.connect(self.dsn)
        cur = pg.cursor()
        cur.execute("SELECT id, agent_type, profile_name FROM sessions ORDER BY id")
        rows = cur.fetchall()
        self.assertEqual(
            rows,
            [("019fc071-fab4-7661-9a0b-2afaa65cbb31", "codex", ""),
             ("1b8fc026-d2b4-4dfb-bdef-2ea8e73013e4", "workbuddy", ""),
             ("20260808_180012_0c275f", "hermes", None),
             ("20260809_120000_testabc", "hermes", "magic")])
        cur.execute("SELECT session_id FROM messages")
        self.assertEqual(cur.fetchall(), [("019fc071-fab4-7661-9a0b-2afaa65cbb31",)])
        cur.execute("SELECT id, profile FROM projects ORDER BY id")
        self.assertEqual(cur.fetchall(), [("p_abc", "magic"), ("p_bare", "")])
        cur.execute("SELECT project_id FROM project_folders")
        self.assertEqual(cur.fetchall(), [("p_abc",)])
        cur.execute("SELECT old_id, new_id FROM project_remap")
        self.assertEqual(cur.fetchall(), [("p_old", "p_abc")])
        pg.close()

    def test_collision_reported_and_skipped(self):
        self._seed([
            (f"INSERT INTO sessions (id, workspace_id, title) VALUES "
             f"('20260808_205157_c272fe', 1, 'existing'),"
             f"('magic:20260808_205157_c272fe', 1, 'prefixed')", ()),
        ])
        r = self._run("--apply", "--workspace", "1")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("1 collision(s)", r.stdout)
        self.assertIn("COLLISIONS", r.stdout)
        import psycopg2
        pg = psycopg2.connect(self.dsn)
        cur = pg.cursor()
        cur.execute("SELECT id FROM sessions ORDER BY id")
        # the colliding prefixed row stays untouched (nothing merged)
        self.assertEqual(cur.fetchall(),
                         [("20260808_205157_c272fe",),
                          ("magic:20260808_205157_c272fe",)])
        pg.close()


if __name__ == "__main__":
    unittest.main()
