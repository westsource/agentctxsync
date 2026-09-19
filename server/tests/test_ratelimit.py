"""Per-IP rate limiter tests (ratelimit.py): window semantics, isolation.

The limiter is pure in-process state with an injectable clock, so these tests
need no server stack or database.
"""
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

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


class MailRecipientLimitTest(unittest.TestCase):
    """Recipient-side caps: the dimension a sender cannot multiply by
    rotating IPs (5 reset mails / 10 min / IP is 720 per mailbox per day)."""

    def setUp(self):
        ratelimit.reset()

    def test_both_windows_must_have_budget(self):
        t = 1_000_000.0
        key = "victim@example.com"
        self.assertTrue(ratelimit.allow("mail_recipient", key, now=t))
        # Second request inside the same minute: refused by the 1/minute window.
        self.assertFalse(ratelimit.allow("mail_recipient", key, now=t + 1))
        # Next minute refills the short window …
        self.assertTrue(ratelimit.allow("mail_recipient", key, now=t + 61))
        # … but the day window keeps counting: 5 mails, then the mailbox is done.
        self.assertTrue(ratelimit.allow("mail_recipient", key, now=t + 122))
        self.assertTrue(ratelimit.allow("mail_recipient", key, now=t + 183))
        self.assertTrue(ratelimit.allow("mail_recipient", key, now=t + 244))
        self.assertFalse(ratelimit.allow("mail_recipient", key, now=t + 305))
        # A full day later the mailbox is reachable again.
        self.assertTrue(ratelimit.allow("mail_recipient", key,
                                        now=t + ratelimit.MAIL_RECIPIENT_WINDOW_DAY + 306))

    def test_recipients_are_independent(self):
        self.assertTrue(ratelimit.allow("mail_recipient", "a@example.com"))
        self.assertTrue(ratelimit.allow("mail_recipient", "b@example.com"))

    def test_account_and_address_caps(self):
        for _ in range(ratelimit.MAIL_ACCOUNT_LIMIT):
            self.assertTrue(ratelimit.allow("mail_account", 7))
        self.assertFalse(ratelimit.allow("mail_account", 7))
        self.assertTrue(ratelimit.allow("mail_account", 8))  # per account
        self.assertTrue(ratelimit.allow("mail_address", "x@example.com"))
        self.assertFalse(ratelimit.allow("mail_address", "x@example.com"))
        self.assertTrue(ratelimit.allow("mail_address", "y@example.com"))


class BucketTablePressureTest(unittest.TestCase):
    """A caller churning distinct keys must not be able to reset everyone
    else's budget (the table used to be cleared wholesale when full)."""

    def setUp(self):
        ratelimit.reset()

    def test_full_table_fails_closed_then_recovers(self):
        t = 1_000_000.0
        with mock.patch.object(ratelimit, "_MAX_BUCKETS", 4):
            for key in "abcd":
                self.assertTrue(ratelimit.allow("register", key, now=t))
            # No room for a fifth live key: deny instead of resetting counters.
            self.assertFalse(ratelimit.allow("register", "e", now=t))
            self.assertFalse(ratelimit.allow("register", "a", now=t))
            # Once the entries expire they are evicted and service resumes.
            self.assertTrue(ratelimit.allow(
                "register", "e", now=t + ratelimit.REGISTER_WINDOW + 1))


if __name__ == "__main__":
    unittest.main()
