"""Path separator alignment tests (mcp/adapters/base.py).

Pins the client pull-write behavior: pull-side paths that differ from an
existing local path only by separator (and, on Windows, case) are rewritten
to the local spelling so writes merge instead of splitting the same path.
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from adapters.base import (  # noqa: E402
    _path_key,
    align_path_to_local,
    build_path_map,
)


class PathKeyTest(unittest.TestCase):
    def test_separators_normalized(self):
        # same logical path -> same key regardless of separator spelling
        self.assertEqual(_path_key(r"E:\a\b"), _path_key("E:/a/b"))
        self.assertEqual(_path_key("E:/a/b"), _path_key(r"E:\a\b"))
        self.assertEqual(_path_key(r"C:\x\y"), _path_key("C:/x/y"))


class BuildPathMapTest(unittest.TestCase):
    def test_collapses_separator_spellings(self):
        m = build_path_map([r"E:\a\b", "E:/c"])
        # both spellings map back to the same local path value (first-seen)
        self.assertEqual(align_path_to_local("E:/a/b", [r"E:\a\b"]), r"E:\a\b")
        self.assertEqual(m[_path_key("E:/a/b")], r"E:\a\b")
        self.assertEqual(m[_path_key("E:/c")], "E:/c")
        # non-str / empty are skipped
        self.assertEqual(build_path_map([123, "", None]), {})


class AlignPathToLocalTest(unittest.TestCase):
    def test_uses_local_spelling_when_equivalent(self):
        local = [r"E:\OpenCode\agentctxsync"]
        self.assertEqual(align_path_to_local("E:/OpenCode/agentctxsync", local),
                         r"E:\OpenCode\agentctxsync")
        # already local spelling passes through unchanged
        self.assertEqual(align_path_to_local(r"E:\OpenCode\agentctxsync", local),
                         r"E:\OpenCode\agentctxsync")

    def test_unknown_path_passes_through(self):
        self.assertEqual(align_path_to_local("E:/brand/new/path", [r"E:\a"]),
                         "E:/brand/new/path")

    def test_empty_and_nonstr_untouched(self):
        self.assertEqual(align_path_to_local("", [r"E:\a"]), "")
        self.assertEqual(align_path_to_local(None, [r"E:\a"]), None)


if __name__ == "__main__":
    unittest.main(verbosity=2)
