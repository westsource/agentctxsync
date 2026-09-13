"""Root-shaped paths never enter the shared project pool.

A project folder is what the Web prefix-matches a session ``cwd`` against
(``server/workspace.py::_session_for_project_match``), so a drive root or the
user home would turn its card into a catch-all bucket -- and the server's
``project_folders`` can only ever be unioned, never removed, so the mistake
would be permanent. Decision record: docs/ARCHITECTURE.md
"根目录（home / 盘符根）不入共享项目池".
"""
import importlib.util
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from adapters.base import (is_root_project_path,  # noqa: E402
                           strip_root_project_paths)

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "hide-root-projects.py"


def _load_script():
    spec = importlib.util.spec_from_file_location("hide_root_projects", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class RootPathPredicateTest(unittest.TestCase):
    def test_roots_and_homes_are_root_shaped(self):
        for p in ("D:", "D:\\", "D:/", "c:/", "C:/Users", "C:/Users/rong",
                  "C:\\Users\\rong", "/", "/root", "/home", "/home/x",
                  "/Users", "/Users/x", "E:/"):
            self.assertTrue(is_root_project_path(p), p)

    def test_real_directories_are_not_root_shaped(self):
        for p in ("C:/Users/rong/Documents", "D:/work/x", "E:/OpenCode/x",
                  "/home/x/proj", "/Users/x/Documents", "/var/log",
                  "/root/x", "C:/UsersX", "", None, "relative/dir"):
            self.assertFalse(is_root_project_path(p), p)


class StripProjectPathsTest(unittest.TestCase):
    def test_root_paths_dropped_and_root_only_project_dropped(self):
        mixed = {"id": "p1", "name": "P1", "primary_path": "C:/Users/rong",
                 "folders": [{"path": "C:\\Users\\rong"},
                             {"path": "E:/OpenCode/proj"}]}
        out = strip_root_project_paths(mixed)
        self.assertIsNone(out["primary_path"])       # never rewritten
        self.assertEqual([f["path"] for f in out["folders"]],
                         ["E:/OpenCode/proj"])
        self.assertEqual(mixed["primary_path"], "C:/Users/rong")  # not mutated

        self.assertIsNone(strip_root_project_paths(
            {"id": "p2", "primary_path": "D:/", "folders": []}))

    def test_primary_only_project_keeps_a_folder_identity(self):
        p = {"id": "p3", "primary_path": "E:/a/b", "folders": []}
        self.assertEqual(strip_root_project_paths(p)["primary_path"], "E:/a/b")

    def test_pathless_project_passes_through(self):
        # filtering roots must not invent a drop rule for a project that never
        # carried a path (nothing to prefix-match either way)
        p = {"id": "p4", "slug": "s4", "name": "P4", "folders": []}
        self.assertEqual(strip_root_project_paths(p), p)


class ScriptPredicateTest(unittest.TestCase):
    """The operator script carries its own copy (mcp/ is not importable as a
    package in a server deployment); drift would hide the wrong projects."""

    def test_script_predicate_matches_client_predicate(self):
        mod = _load_script()
        cases = ["D:", "D:\\", "D:/", "c:/", "C:/Users", "C:/Users/rong",
                 "C:\\Users\\rong", "/", "/root", "/home", "/home/x",
                 "/Users", "/Users/x", "E:/",
                 "C:/Users/rong/Documents", "D:/work/x", "E:/OpenCode/x",
                 "/home/x/proj", "/Users/x/Documents", "/var/log", "/root/x",
                 "C:/UsersX", "relative/dir"]
        for c in cases:
            self.assertEqual(mod.is_root_project_path(c),
                             is_root_project_path(c), c)


if __name__ == "__main__":
    unittest.main()
