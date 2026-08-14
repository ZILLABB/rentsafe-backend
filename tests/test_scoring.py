"""Tests for the score-aggregation core (Section III)."""

import datetime as dt
import unittest

from app.core import scoring


def _ratings(v: int) -> dict[str, int]:
    return {d: v for d in scoring.DIMENSIONS}


class RecencyWeightTests(unittest.TestCase):
    def test_bands(self):
        self.assertEqual(scoring.recency_weight(0), 1.0)
        self.assertEqual(scoring.recency_weight(12), 1.0)
        self.assertEqual(scoring.recency_weight(13), 0.75)
        self.assertEqual(scoring.recency_weight(24), 0.75)
        self.assertEqual(scoring.recency_weight(36), 0.5)
        self.assertEqual(scoring.recency_weight(37), 0.25)

    def test_months_between(self):
        self.assertEqual(
            scoring.months_between(dt.date(2023, 1, 15), dt.date(2024, 1, 15)), 12
        )
        self.assertEqual(
            scoring.months_between(dt.date(2023, 1, 20), dt.date(2023, 3, 10)), 1
        )
        # never negative
        self.assertEqual(
            scoring.months_between(dt.date(2025, 1, 1), dt.date(2024, 1, 1)), 0
        )


class AggregateTests(unittest.TestCase):
    AS_OF = dt.date(2024, 6, 1)

    def test_single_review_aggregate(self):
        r = scoring.ReviewScore(_ratings(4), dt.date(2024, 5, 1), verified=False)
        res = scoring.aggregate_property([r], as_of=self.AS_OF)
        self.assertEqual(res.overall, 4.0)
        self.assertEqual(res.counted, 1)
        self.assertEqual(res.per_dimension["power"], 4.0)

    def test_verified_review_weighted_more(self):
        # One fresh verified 5 vs one fresh unverified 1.
        verified = scoring.ReviewScore(_ratings(5), dt.date(2024, 5, 1), verified=True)
        plain = scoring.ReviewScore(_ratings(1), dt.date(2024, 5, 1), verified=False)
        res = scoring.aggregate_property([verified, plain], as_of=self.AS_OF)
        # Weighted: (1.5*5 + 1.0*1) / (1.5 + 1.0) = 8.5/2.5 = 3.4
        self.assertEqual(res.overall, 3.4)

    def test_old_review_weighted_less(self):
        fresh = scoring.ReviewScore(_ratings(5), dt.date(2024, 5, 1), verified=False)
        old = scoring.ReviewScore(_ratings(1), dt.date(2020, 1, 1), verified=False)
        res = scoring.aggregate_property([fresh, old], as_of=self.AS_OF)
        # old (>36mo) weight 0.25: (1.0*5 + 0.25*1)/1.25 = 5.25/1.25 = 4.2
        self.assertEqual(res.overall, 4.2)

    def test_management_change_excludes_prior(self):
        before = scoring.ReviewScore(_ratings(1), dt.date(2022, 1, 1), verified=False)
        after = scoring.ReviewScore(_ratings(5), dt.date(2024, 1, 1), verified=False)
        res = scoring.aggregate_property(
            [before, after], as_of=self.AS_OF, management_change=dt.date(2023, 1, 1)
        )
        self.assertEqual(res.overall, 5.0)
        self.assertEqual(res.counted, 1)
        self.assertEqual(res.excluded_prior_mgmt, 1)

    def test_no_reviews(self):
        res = scoring.aggregate_property([], as_of=self.AS_OF)
        self.assertIsNone(res.overall)
        self.assertEqual(res.counted, 0)


if __name__ == "__main__":
    unittest.main()
