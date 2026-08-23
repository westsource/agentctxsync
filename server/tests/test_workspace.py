"""Case-insensitive session<->project association test (S1 fix).

Windows drive letters/paths are case-insensitive (`d:` == `D:`), but a plain
SQL `=`/`LIKE` is case-sensitive, so a session whose stored `cwd` casing
differs from the project folder path (e.g. lowercase drive letter) was hidden
from the project's "关联会话" on the server web. The match helper must fold
case and avoid LIKE wildcard chars.
"""
import os
import sys
import unittest
from pathlib import Path

os.environ.setdefault("HERMES_SYNC_PG_DSN", "postgresql://x:x@localhost:5432/x")
os.environ.setdefault("HERMES_SYNC_MASTER_KEY", "test-master-key")
os.environ.setdefault("HERMES_SYNC_JWT_SECRET", "test-jwt-secret")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import workspace  # noqa: E402


class ProjectSessionMatchTest(unittest.TestCase):
    def test_sql_is_case_insensitive_and_wildcard_safe(self):
        sql, params = workspace._session_for_project_match(
            "D:/work/2026新疆公路数字底座")
        self.assertIn("LOWER(cwd)", sql)     # case-insensitive
        self.assertNotIn("LIKE", sql)        # no wildcard chars in path
        exact, base, lenf, fwd, lenb, bwd = params
        self.assertEqual(exact, "d:/work/2026新疆公路数字底座")
        self.assertEqual(base, "d:/work/2026新疆公路数字底座")
        self.assertEqual(fwd, "d:/work/2026新疆公路数字底座/")
        self.assertEqual(bwd, "d:/work/2026新疆公路数字底座\\")
        self.assertEqual((lenf, lenb), (len(fwd), len(bwd)))

    def test_windows_case_variants_are_equivalent(self):
        # `D:` (folder) vs `d:` (session cwd) must produce the same params.
        a = workspace._session_for_project_match("D:/work/2026新疆公路数字底座")
        b = workspace._session_for_project_match("d:/work/2026新疆公路数字底座")
        self.assertEqual(a[1], b[1])

    def test_trailing_separator_negated_for_prefix(self):
        sql, params = workspace._session_for_project_match("D:/work/X/")
        _, base, lenf, fwd, lenb, bwd = params
        self.assertEqual(base, "d:/work/x")     # trailing sep stripped
        self.assertEqual(fwd, "d:/work/x/")      # under-prefix is path + '/'
        self.assertEqual(lenf, len(fwd))
        self.assertEqual(bwd, "d:/work/x\\")
        self.assertEqual(lenb, len(bwd))


if __name__ == "__main__":
    unittest.main()
