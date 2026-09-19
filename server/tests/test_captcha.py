"""Self-hosted math CAPTCHA tests: generation, verification, single-use, TTL."""
import os
import re
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

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

    def test_svg_carries_no_character_data(self):
        """No <text> nodes and no character data at all — every byte in the
        markup is geometry. (The pre-2026-09-19 renderer emitted per-character
        <text>, so one regex recovered the equation with no image work.)"""
        for _ in range(50):
            _, svg = captcha.new_challenge()
            self.assertNotIn("<text", svg)
            self.assertEqual(re.sub(r"<[^>]*>", "", svg).strip(), "")

    def test_pinned_expression_is_not_recoverable(self):
        with mock.patch.object(captcha, "_new_expression",
                               return_value=("88 + 65 = ?", 153)):
            _, svg = captcha.new_challenge()
        self.assertNotIn("88 + 65", svg)
        self.assertNotIn("88+65", svg)
        self.assertNotIn("= ?", svg)

    def test_renderer_is_deterministic_in_shape(self):
        """Line-only rendering: every glyph is stroked paths, and different
        challenges differ (per-glyph rotation/jitter are random)."""
        svg_a = captcha._render_svg("12 + 34 = ?")
        svg_b = captcha._render_svg("12 + 34 = ?")
        self.assertIn("<path", svg_a)
        self.assertIn('stroke-linecap="round"', svg_a)
        self.assertNotEqual(svg_a, svg_b)
        # One glyph per non-space character.
        self.assertEqual(svg_a.count("<path"), sum(len(captcha._GLYPHS[c])
                                                   for c in "12+34=?"))

    def test_challenge_round_trip_through_the_expression_seam(self):
        """Pin the generator: the rendered puzzle and the stored answer must
        both come from the same expression."""
        with mock.patch.object(captcha, "_new_expression",
                               return_value=("88 + 65 = ?", 153)):
            cid, svg = captcha.new_challenge()
        self.assertIn("<path", svg)
        self.assertEqual(captcha.store[cid]["answer"], 153)
        self.assertTrue(captcha.verify(cid, "153"))
        self.assertFalse(captcha.verify(cid, "152"))

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

    def test_expression_generator_matches_its_answer(self):
        """Every generated expression must be answerable arithmetic (the
        renderer no longer exposes it, so the generator is checked directly)."""
        for _ in range(200):
            expr, answer = captcha._new_expression()
            left = expr.split("=")[0].strip()
            a, op, b = left.split()
            expected = int(a) + int(b) if op == "+" else int(a) - int(b)
            self.assertEqual(expected, answer, expr)
            self.assertGreaterEqual(answer, 0, expr)   # subtraction never goes negative


if __name__ == "__main__":
    unittest.main()
