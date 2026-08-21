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


if __name__ == "__main__":
    unittest.main()
