"""Workout plan progression. `week` was written once as 1 and never advanced."""

import json
import unittest
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from coach import db, plans
from tests import support

TZ = ZoneInfo("Asia/Bangkok")


def _plan(days_ago, duration_weeks=6, **extra):
    created = (datetime.now(TZ) - timedelta(days=days_ago, hours=1)).isoformat()
    return {"name": "test plan", "duration_weeks": duration_weeks,
            "created": created, "schedule": [], **extra}


class ElapsedProgress(unittest.TestCase):
    def test_week_follows_the_calendar(self):
        for days, expected in ((0, 1), (6, 1), (7, 2), (13, 2), (14, 3), (27, 4)):
            self.assertEqual(plans._elapsed_progress(_plan(days), TZ)[0], expected,
                             f"day {days}")

    def test_clamped_and_marked_complete_past_the_end(self):
        self.assertEqual(plans._elapsed_progress(_plan(90, duration_weeks=6), TZ),
                         (6, "completed"))

    def test_unknown_duration_never_completes(self):
        plan = _plan(10)
        plan.pop("duration_weeks")
        self.assertEqual(plans._elapsed_progress(plan, TZ), (2, "in_progress"))

    def test_missing_or_broken_created_falls_back_to_week_one(self):
        self.assertEqual(plans._elapsed_progress({}, TZ), (1, "in_progress"))
        self.assertEqual(plans._elapsed_progress({"created": "not a date"}, TZ),
                         (1, "in_progress"))


class PersistedWeek(unittest.TestCase):
    """ai.build_daily_snapshot, chat._get_goals and weekly.build_weekly_snapshot
    all read the goals row raw, so a stale stored week contradicted the brief."""

    def test_derived_week_is_written_back(self):
        uid = support.new_user()
        stored = _plan(27)
        stored["week"] = 1
        with db.connect() as conn:
            conn.execute("INSERT INTO goals (user_id, key, value_json, updated_at) "
                         "VALUES (?, 'workout_plan', ?, datetime('now'))",
                         (uid, json.dumps(stored)))

        self.assertEqual(plans.get_current_plan(uid, tz=TZ)["week"], 4)

        with db.connect() as conn:
            row = conn.execute("SELECT value_json FROM goals WHERE user_id = ? AND key = 'workout_plan'",
                               (uid,)).fetchone()
        self.assertEqual(json.loads(row["value_json"])["week"], 4)

    def test_no_plan_returns_none(self):
        self.assertIsNone(plans.get_current_plan(support.new_user(), tz=TZ))


class TodaysWorkout(unittest.TestCase):
    def test_names_the_current_week(self):
        uid = support.new_user()
        today_name = datetime.now(TZ).strftime("%A")
        plan = _plan(27)
        plan["schedule"] = [{"day": today_name, "workout": "Upper body", "duration_min": 40}]
        with db.connect() as conn:
            conn.execute("INSERT INTO goals (user_id, key, value_json, updated_at) "
                         "VALUES (?, 'workout_plan', ?, datetime('now'))",
                         (uid, json.dumps(plan)))
        text = plans.get_today_workout(uid)
        self.assertIn("Upper body", text)
        self.assertIn("week 4 of 6", text)

    def test_finished_plan_says_so(self):
        uid = support.new_user()
        plan = _plan(90)
        plan["schedule"] = [{"day": datetime.now(TZ).strftime("%A"), "workout": "Run", "duration_min": 30}]
        with db.connect() as conn:
            conn.execute("INSERT INTO goals (user_id, key, value_json, updated_at) "
                         "VALUES (?, 'workout_plan', ?, datetime('now'))",
                         (uid, json.dumps(plan)))
        self.assertIn("plan finished", plans.get_today_workout(uid))


if __name__ == "__main__":
    unittest.main()
