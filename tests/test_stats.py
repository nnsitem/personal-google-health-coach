"""Metric sanity bounds. Every metric comes from a rollup that aggregates all
writers, so the duplication that inflated nutrition could inflate steps too."""

import unittest
from datetime import datetime
from zoneinfo import ZoneInfo

from coach import db, stats
from tests import support

TZ = ZoneInfo("Asia/Bangkok")


class SaneRanges(unittest.TestCase):
    def test_bounds_cover_every_extracted_type(self):
        self.assertEqual(set(stats._SANE_RANGE), set(stats._EXTRACTORS))

    def test_real_values_are_inside_the_bounds(self):
        observed = {"steps": 22051, "total-calories": 3117,
                    "daily-resting-heart-rate": 64, "daily-heart-rate-variability": 94,
                    "daily-oxygen-saturation": 96, "daily-respiratory-rate": 14}
        for dtype, value in observed.items():
            low, high = stats._SANE_RANGE[dtype]
            self.assertTrue(low <= value <= high, f"{dtype}={value}")

    def test_duplication_scale_values_are_outside(self):
        self.assertGreater(820981, stats._SANE_RANGE["steps"][1])
        self.assertGreater(61810, stats._SANE_RANGE["total-calories"][1])


class SeriesFiltering(unittest.TestCase):
    def test_implausible_days_are_dropped_from_the_series(self):
        uid = support.new_user()
        today = datetime.now(TZ).date().isoformat()
        db.upsert_metric(uid, today, None, "steps",
                         {"steps": {"countSum": 820981}}, source="test")
        series = stats._load_daily_series(uid, 7, TZ)
        self.assertNotIn(today, series["steps"])

    def test_plausible_days_are_kept(self):
        uid = support.new_user()
        today = datetime.now(TZ).date().isoformat()
        db.upsert_metric(uid, today, None, "steps",
                         {"steps": {"countSum": 9454}}, source="test")
        series = stats._load_daily_series(uid, 7, TZ)
        self.assertEqual(series["steps"][today], 9454)


if __name__ == "__main__":
    unittest.main()
