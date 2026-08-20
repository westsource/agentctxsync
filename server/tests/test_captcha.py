"""Self-hosted math CAPTCHA tests: generation, verification, single-use, TTL."""
import os
import re
import sys
import time
import unittest
from pathlib import Path

os.environ.setdefault("HERMES_SYNC_PG_DSN", "postgresql://x:x@localhost:5432/x")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import captcha  # noqa: E402


class CaptchaTest(unittest.TestCase):
    def setUp(self):
        captcha.store.clear()

    def test_new_challenge_returns_id_and_svg(self):
        cid, svg = captcha.new_challenge()
        self.assertTrue(cid)
        self.assertIn("<svg", svg)
        self.assertIn(cid, captcha.store)

    def test_svg_contains_expression_markup(self):
        _, svg = captcha.new_challenge()
        self.assertIn("<text", svg)
        # Per-char <text> nodes: join them to recover the expression.
        expr = "".join(re.findall(r"<text[^>]*>([^<]+)</text>", svg))
        self.assertIn("=", expr)
        self.assertIn("?", expr)

    def test_verify_accepts_correct_answer_once(self):
        cid, _ = captcha.new_challenge()
        answer = captcha.store[cid]["answer"]
        self.assertTrue(captcha.verify(cid, str(answer)))
        # Single-use: a second attempt fails even with the right answer.
        self.assertFalse(captcha.verify(cid, str(answer)))

    def test_verify_rejects_wrong_answer_and_burns_it(self):
        cid, _ = captcha.new_challenge()
        answer = captcha.store[cid]["answer"]
        wrong = str(answer + 1)
        self.assertFalse(captcha.verify(cid, wrong))
        # The wrong attempt consumed the challenge.
        self.assertFalse(captcha.verify(cid, str(answer)))

    def test_verify_rejects_unknown_or_empty_id(self):
        self.assertFalse(captcha.verify("", "1"))
        self.assertFalse(captcha.verify("nope", "1"))

    def test_verify_rejects_expired_challenge(self):
        cid, _ = captcha.new_challenge()
        captcha.store[cid]["expires"] = time.time() - 1
        self.assertFalse(captcha.verify(cid, "1"))

    def test_verify_rejects_garbage_input(self):
        cid, _ = captcha.new_challenge()
        self.assertFalse(captcha.verify(cid, "abc"))
        self.assertFalse(captcha.verify(cid, None))

    def test_answers_are_actual_arithmetic(self):
        # Every rendered expression must match the stored answer.
        for _ in range(50):
            cid, svg = captcha.new_challenge()
            expr = "".join(re.findall(r"<text[^>]*>([^<]+)</text>", svg))
            left, right = expr.split("=")
            a, op, b = left.split()
            expected = int(a) + int(b) if op == "+" else int(a) - int(b)
            self.assertEqual(expected, captcha.store[cid]["answer"])
            self.assertEqual(right.strip(), "?")


if __name__ == "__main__":
    unittest.main()
