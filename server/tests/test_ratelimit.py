"""Per-IP rate limiter tests (ratelimit.py): window semantics, isolation.

The limiter is pure in-process state with an injectable clock, so these tests
need no server stack or database.
"""
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import ratelimit  # noqa: E402


class RateLimitTest(unittest.TestCase):
    def setUp(self):
        ratelimit.reset()

    def test_allows_exactly_up_to_limit(self):
        for _ in range(ratelimit.REGISTER_LIMIT):
            self.assertTrue(ratelimit.allow("register", "1.2.3.4"))
        self.assertFalse(ratelimit.allow("register", "1.2.3.4"))

    def test_window_rollover_refills_budget(self):
        now = 1_000_000.0
        for _ in range(ratelimit.REGISTER_LIMIT):
            self.assertTrue(ratelimit.allow("register", "1.2.3.4", now=now))
        self.assertFalse(ratelimit.allow("register", "1.2.3.4", now=now))
        # A new window starts after the window length elapses.
        later = now + ratelimit.REGISTER_WINDOW + 1
        self.assertTrue(ratelimit.allow("register", "1.2.3.4", now=later))

    def test_keys_are_independent(self):
        for _ in range(ratelimit.REGISTER_LIMIT):
            ratelimit.allow("register", "1.2.3.4")
        self.assertTrue(ratelimit.allow("register", "5.6.7.8"))

    def test_scopes_are_independent(self):
        for _ in range(ratelimit.REGISTER_LIMIT):
            ratelimit.allow("register", "1.2.3.4")
        self.assertTrue(ratelimit.allow("login", "1.2.3.4"))
        self.assertTrue(ratelimit.allow("captcha", "1.2.3.4"))

    def test_unknown_scope_is_unlimited(self):
        self.assertTrue(ratelimit.allow("unknown-scope", "1.2.3.4"))
        self.assertTrue(ratelimit.allow("unknown-scope", "1.2.3.4"))

    def test_reset_clears_budget(self):
        for _ in range(ratelimit.REGISTER_LIMIT):
            ratelimit.allow("register", "1.2.3.4")
        self.assertFalse(ratelimit.allow("register", "1.2.3.4"))
        ratelimit.reset()
        self.assertTrue(ratelimit.allow("register", "1.2.3.4"))


if __name__ == "__main__":
    unittest.main()
