"""The weekly report quoted time in BED while every other surface reports time
ASLEEP, so the narrative contradicted the average printed on the same card."""

import json
import unittest
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from coach import db, weekly
from coach.stats import _asleep_minutes
from tests import support

TZ = ZoneInfo("Asia/Bangkok")


def _stage(start, minutes, kind):
    end = start + timedelta(minutes=minutes)
    return {"type": kind,
            "startTime": start.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "endTime": end.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}, end


class SleepHours(unittest.TestCase):
    def setUp(self):
        self.uid = support.new_user()
        start = (datetime.now(TZ) - timedelta(days=1)).replace(hour=23, minute=0,
                                                               second=0, microsecond=0)
        stages = []
        cursor = start
        for minutes, kind in ((90, "DEEP"), (200, "LIGHT"), (30, "AWAKE"), (100, "REM")):
            stage, cursor = _stage(cursor, minutes, kind)
            stages.append(stage)
        with db.connect() as conn:
            conn.execute("INSERT INTO sleep_sessions (user_id, start, end, stages_json) "
                         "VALUES (?, ?, ?, ?)",
                         (self.uid,
                          start.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                          cursor.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                          json.dumps(stages)))
        self.stages = stages

    def test_total_hours_excludes_awake(self):
        snapshot = weekly.build_weekly_snapshot(self.uid)
        session = snapshot["sleep_sessions"][0]
        self.assertEqual(session["total_hours"], 6.5)      # 90+200+100 asleep
        self.assertEqual(session["in_bed_hours"], 7.0)     # +30 awake
        self.assertEqual(session["awake_min"], 30)

    def test_matches_the_figure_stats_reports(self):
        snapshot = weekly.build_weekly_snapshot(self.uid)
        session = snapshot["sleep_sessions"][0]
        self.assertEqual(session["total_hours"],
                         round(_asleep_minutes(self.stages) / 60, 1))


class SleepSeries(unittest.TestCase):
    """The weekly report's 7-day sleep bar chart (added 2026-08-28, DESIGN-V3.md
    #3) — mirrors the existing steps chart via the same _mini_bar_chart
    component in flex.py, so this only needs to check the series shape/values
    are right, not the chart rendering itself."""

    def setUp(self):
        self.uid = support.new_user()

    def _report_labels(self):
        from coach.flex import report_labels
        return report_labels(db.get_user_language(self.uid))

    def test_seven_days_even_with_no_sleep_data(self):
        snapshot = weekly.build_weekly_snapshot(self.uid)
        vm = weekly._weekly_view_model(self.uid, snapshot, self._report_labels())
        self.assertEqual(len(vm["sleep_series"]), 7)
        self.assertTrue(all(hours == 0 for _, hours in vm["sleep_series"]))

    def test_a_logged_night_lands_on_its_own_date(self):
        two_nights_ago = (datetime.now(TZ) - timedelta(days=2)).replace(
            hour=23, minute=0, second=0, microsecond=0)
        stages = []
        cursor = two_nights_ago
        for minutes, kind in ((100, "DEEP"), (180, "LIGHT")):
            stage, cursor = _stage(cursor, minutes, kind)
            stages.append(stage)
        with db.connect() as conn:
            conn.execute("INSERT INTO sleep_sessions (user_id, start, end, stages_json) "
                         "VALUES (?, ?, ?, ?)",
                         (self.uid,
                          two_nights_ago.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                          cursor.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                          json.dumps(stages)))

        snapshot = weekly.build_weekly_snapshot(self.uid)
        vm = weekly._weekly_view_model(self.uid, snapshot, self._report_labels())
        hours_values = [h for _, h in vm["sleep_series"]]
        # 100+180 = 280 min asleep = 4.67h, on exactly one of the 7 days.
        self.assertEqual(sum(1 for h in hours_values if h > 0), 1)
        self.assertAlmostEqual(max(hours_values), round(280 / 60, 1))


if __name__ == "__main__":
    unittest.main()
