"""Nudge rules. Both bugs here shipped silently for the app's whole life."""

import json
import unittest
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from coach import db, nudges
from tests import support

TZ = ZoneInfo("Asia/Bangkok")


def _at(hour, minute):
    return datetime.now(TZ).replace(hour=hour, minute=minute, second=0, microsecond=0)


class BedtimeReminder(unittest.TestCase):
    """Never fired once in production: the rule required minute < 30 while the
    scheduler runs the check at :35, and the qualifying window compared a raw
    clock hour, excluding everyone who falls asleep after midnight."""

    def _user_sleeping_at(self, *local_hours):
        uid = support.new_user()
        with db.connect() as conn:
            for i, h in enumerate(local_hours):
                start = datetime.now(TZ).replace(hour=int(h), minute=int((h % 1) * 60),
                                                 second=0, microsecond=0) - timedelta(days=i + 1)
                end = start + timedelta(hours=7)
                conn.execute(
                    "INSERT INTO sleep_sessions (user_id, start, end, stages_json) VALUES (?, ?, ?, ?)",
                    (uid, start.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
                     end.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"), "[]"))
        return uid

    def test_fires_at_the_scheduled_minute(self):
        uid = self._user_sleeping_at(22.5, 23.0)
        self.assertIsNotNone(nudges._rule_bedtime_reminder(uid, _at(21, 35)),
                             "the nudge check only ever runs at :35")

    def test_fires_anywhere_in_the_21_hour(self):
        uid = self._user_sleeping_at(22.5, 23.0)
        for minute in (0, 35, 59):
            self.assertIsNotNone(nudges._rule_bedtime_reminder(uid, _at(21, minute)))

    def test_silent_outside_the_21_hour(self):
        uid = self._user_sleeping_at(22.5, 23.0)
        for hour in (20, 22):
            self.assertIsNone(nudges._rule_bedtime_reminder(uid, _at(hour, 35)))

    def test_after_midnight_sleepers_qualify(self):
        # 00:52 and 01:27 average to ~1.1 on a raw clock — the old window
        # (21.5-23.5) skipped exactly the late sleepers this nudge is for.
        uid = self._user_sleeping_at(0.87, 1.45, 1.05)
        result = nudges._rule_bedtime_reminder(uid, _at(21, 35))
        self.assertIsNotNone(result)
        self.assertIn("01:", result["condition"])

    def test_early_sleepers_excluded(self):
        uid = self._user_sleeping_at(19.0, 19.5)
        self.assertIsNone(nudges._rule_bedtime_reminder(uid, _at(21, 35)))


class NightHourScale(unittest.TestCase):
    def test_after_midnight_shifts_past_24(self):
        self.assertEqual(nudges._night_hour(_at(22, 30)), 22.5)
        self.assertEqual(nudges._night_hour(_at(1, 30)), 25.5)

    def test_formats_back_to_a_clock(self):
        self.assertEqual(nudges._fmt_night_hour(25.5), "01:30")
        self.assertEqual(nudges._fmt_night_hour(22.5), "22:30")


class LowSteps(unittest.TestCase):
    """The condition asserted a hard-coded "you usually average over 6,000" at a
    user whose real average was 9,454."""

    def setUp(self):
        self.uid = support.new_user()
        db.upsert_metric(self.uid, datetime.now(TZ).date().isoformat(), None,
                         "steps", {"steps": {"countSum": 1200}}, source="test")

    def _fire(self, baseline):
        original = nudges._avg_steps
        nudges._avg_steps = lambda uid, days=30: baseline
        try:
            return nudges._rule_low_steps(self.uid, _at(15, 35))
        finally:
            nudges._avg_steps = original

    def test_quotes_the_real_average(self):
        result = self._fire(9454.0)
        self.assertIn("9,454", result["condition"])
        self.assertNotIn("over 6,000", result["condition"])

    def test_silent_when_the_users_own_average_is_low(self):
        # 3,000 steps is a normal day for them, not a shortfall.
        self.assertIsNone(self._fire(3200.0))

    def test_says_so_when_there_is_no_history(self):
        self.assertIn("no step history", self._fire(None)["condition"])

    def test_silent_before_the_afternoon(self):
        self.assertIsNone(nudges._rule_low_steps(self.uid, _at(9, 35)))


if __name__ == "__main__":
    unittest.main()
