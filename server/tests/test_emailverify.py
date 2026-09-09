"""Email-verification domain tests: normalization, masking, one-time tokens.

Pure-logic coverage for emailverify.py (no server stack). Token issue /
lookup / consume / revoke run against the fake-cursor pattern used across
server/tests.
"""
import os
import sys
import unittest
from pathlib import Path

os.environ.setdefault("HERMES_SYNC_PG_DSN", "postgresql://x:x@localhost:5432/x")
os.environ.setdefault("HERMES_SYNC_MASTER_KEY", "test-master-key")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import emailverify  # noqa: E402


class FakeCursor:
    def __init__(self, rows=()):
        self.rows = list(rows)
        self.executed = []
        self.rowcount = 1

    def execute(self, sql, params=None):
        self.executed.append((sql, params))

    def fetchone(self):
        return self.rows.pop(0) if self.rows else None


class FakeConn:
    def __init__(self, cursor=None):
        self._cursor = cursor or FakeCursor()

    def cursor(self, *a, **k):
        return self._cursor


class NormalizeTest(unittest.TestCase):
    def test_trims_and_lowercases(self):
        self.assertEqual(emailverify.normalize_email("  Alice@Example.COM "),
                         "alice@example.com")

    def test_accepts_plausible_shape(self):
        self.assertEqual(emailverify.normalize_email("a.b+c@sub.example.cn"),
                         "a.b+c@sub.example.cn")

    def test_rejects_garbage(self):
        for bad in ("", "no-at-sign", "a@", "@b", "a b@c.com",
                    "a@b c.com", "x" * 300 + "@example.com"):
            self.assertIsNone(emailverify.normalize_email(bad), bad)

    def test_rejects_none(self):
        self.assertIsNone(emailverify.normalize_email(None))

    def test_too_long_rejected(self):
        self.assertIsNone(emailverify.normalize_email("a@" + "b" * 300 + ".com"))


class MaskTest(unittest.TestCase):
    def test_masks_local_part(self):
        self.assertEqual(emailverify.mask_email("alice@example.com"),
                         "a***@example.com")

    def test_passthrough_short_or_missing_domain(self):
        self.assertEqual(emailverify.mask_email("x@example.com"), "x@example.com")
        self.assertEqual(emailverify.mask_email(""), "")
        self.assertEqual(emailverify.mask_email("no-domain"), "no-domain")


class TokenTest(unittest.TestCase):
    def test_issue_stores_digest_not_raw(self):
        conn = FakeConn()
        raw = emailverify.issue_token(conn, 7, emailverify.PURPOSE_VERIFY_EMAIL,
                                      "alice@example.com", "1.2.3.4")
        self.assertTrue(raw and len(raw) >= 40)
        # Previous live tokens revoked first, then the new digest inserted.
        revoke_sql, revoke_params = conn._cursor.executed[0]
        self.assertIn("UPDATE user_verification_tokens", revoke_sql)
        self.assertEqual(revoke_params[1], 7)
        insert_sql, insert_params = conn._cursor.executed[-1]
        self.assertIn("INSERT INTO user_verification_tokens", insert_sql)
        self.assertEqual(insert_params[0], 7)               # user_id
        self.assertEqual(insert_params[1], emailverify.PURPOSE_VERIFY_EMAIL)
        self.assertEqual(insert_params[2], emailverify.token_digest(raw))  # hash
        self.assertNotEqual(insert_params[2], raw)          # never plaintext

    def test_lookup_returns_live_row_for_digest(self):
        raw = "raw-token-value"
        conn = FakeConn(FakeCursor([
            (9, 7, emailverify.PURPOSE_VERIFY_EMAIL, "alice@example.com",
             1_700_000_000.0, None)]))
        tok = emailverify.lookup_token(conn, raw)
        self.assertEqual(tok["user_id"], 7)
        self.assertEqual(tok["email_normalized"], "alice@example.com")
        self.assertIsNone(tok["consumed_at"])
        # The lookup used the digest, not the raw value.
        self.assertIn(emailverify.token_digest(raw), conn._cursor.executed[0][1])

    def test_lookup_missing_returns_none(self):
        conn = FakeConn()
        self.assertIsNone(emailverify.lookup_token(conn, "nope"))

    def test_consume_marks_used(self):
        conn = FakeConn()
        self.assertTrue(emailverify.consume_token(conn, 9))
        sql, params = conn._cursor.executed[0]
        self.assertIn("consumed_at", sql)
        self.assertEqual(params[0] is not None, True)

    def test_taken_query_excludes_self(self):
        conn = FakeConn(FakeCursor([(1,)]))
        self.assertTrue(emailverify.verified_email_taken(conn, "x@y.z", 7))
        sql, params = conn._cursor.executed[-1]
        self.assertEqual(params[1], 7)

    def test_not_taken_when_free(self):
        conn = FakeConn()
        self.assertFalse(emailverify.verified_email_taken(conn, "x@y.z", 7))


class MailerOffTest(unittest.TestCase):
    def test_send_without_smtp_config_raises(self):
        import mailer
        # Test environment has no SMTP env vars; the module must refuse.
        with self.assertRaises(mailer.MailerError):
            mailer.send_mail("a@example.com", "s", "b")


if __name__ == "__main__":
    unittest.main()
