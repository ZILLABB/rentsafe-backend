"""Tests for the moderation pre-check core (Section III)."""

import unittest

from app.core import moderation


class ModerationTests(unittest.TestCase):
    def test_clean_text_passes(self):
        res = moderation.check_text(
            "Lovely quiet compound, water is clean.",
            "Floods a bit in October, negotiate the rent.",
        )
        self.assertEqual(res.status, "clean")
        self.assertFalse(res.needs_human)

    def test_risky_language_flagged(self):
        res = moderation.check_text("The agent is a fraudster who took my money")
        self.assertTrue(res.needs_human)
        self.assertIn("legally_risky_language", res.reasons)

    def test_collected_and_vanished_flagged(self):
        res = moderation.check_text("He collected and vanished after I paid")
        self.assertIn("legally_risky_language", res.reasons)

    def test_profanity_flagged(self):
        res = moderation.check_text("this place is shit")
        self.assertIn("profanity", res.reasons)

    def test_velocity_flag(self):
        res = moderation.check_submission(
            texts=["nice place", "all good"], user_reviews_last_24h=3
        )
        self.assertIn("velocity", res.reasons)
        self.assertTrue(res.needs_human)

    def test_duplicate_flag(self):
        res = moderation.check_submission(
            texts=["nice"], user_reviews_last_24h=0, duplicate_of_existing=True
        )
        self.assertIn("duplicate_text", res.reasons)

    def test_clean_submission(self):
        res = moderation.check_submission(
            texts=["great estate", "watch the drainage"], user_reviews_last_24h=0
        )
        self.assertEqual(res.status, "clean")


if __name__ == "__main__":
    unittest.main()
