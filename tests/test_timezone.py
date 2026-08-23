"""Windows must follow the USER's local day, not the server's.

stats.py, chat.py and plans.py all measured "today", "this week" and "the last
30 days" against coach.config.TZ — the container's clock. Every user happens to
live in Asia/Bangkok today, which is why nothing looked wrong; the first signup
from another zone would have read a day off, and a workout plan would have
changed week on the wrong evening.
"""

import json
import pathlib
import re
import unittest
from datetime import datetime
from zoneinfo import ZoneInfo

from coach import db, plans, stats
from tests import support

# 25 hours apart, so their local dates differ at every instant.
EAST = "Pacific/Kiritimati"   # UTC+14
WEST = "Pacific/Midway"       # UTC-11


class LocalDayBoundary(unittest.TestCase):
    def test_the_two_zones_really_disagree_about_the_date(self):
        # Guards the tests below from silently becoming tautologies.
        east = datetime.now(ZoneInfo(EAST)).date()
        west = datetime.now(ZoneInfo(WEST)).date()
        self.assertNotEqual(east, west)

    def test_trends_report_the_users_own_timezone(self):
        for zone in (EAST, WEST):
            uid = support.new_user(timezone=zone)
            self.assertEqual(stats.build_trends(uid)["timezone"], zone)

    def test_todays_steps_are_read_against_the_users_date(self):
        for zone in (EAST, WEST):
            uid = support.new_user(timezone=zone)
            local_today = datetime.now(ZoneInfo(zone)).date().isoformat()
            db.upsert_metric(uid, local_today, None, "steps",
                             {"steps": {"countSum": 9000}}, source="test")
            trends = stats.build_trends(uid)
            self.assertEqual(trends["steps"]["today"], 9000,
                             f"{zone}: {local_today} should be that user's today")

    def test_a_neighbouring_date_is_not_counted_as_today(self):
        # The same row that is "today" for the eastern user is a different date
        # for the western one, and must not be reported as their today.
        east_today = datetime.now(ZoneInfo(EAST)).date().isoformat()
        uid = support.new_user(timezone=WEST)
        db.upsert_metric(uid, east_today, None, "steps",
                         {"steps": {"countSum": 9000}}, source="test")
        self.assertIsNone(stats.build_trends(uid)["steps"]["today"])

    def test_an_invalid_timezone_falls_back_instead_of_raising(self):
        uid = support.new_user(timezone="Not/AZone")
        self.assertIsNotNone(stats.build_trends(uid)["timezone"])


class PlanCreationStamp(unittest.TestCase):
    """_elapsed_progress derives the current week from `created`, so the stamp
    has to be the user's clock or the week turns over on the wrong evening."""

    def test_created_is_stamped_in_the_users_zone(self):
        for zone in (EAST, WEST):
            uid = support.new_user(timezone=zone)
            plans.save_plan(uid, {"name": "p", "duration_weeks": 4, "schedule": []})
            with db.connect() as conn:
                stored = json.loads(conn.execute(
                    "SELECT value_json FROM goals WHERE user_id = ? AND key = 'workout_plan'",
                    (uid,)).fetchone()["value_json"])
            created = datetime.fromisoformat(stored["created"])
            self.assertEqual(created.utcoffset(),
                             datetime.now(ZoneInfo(zone)).utcoffset(), zone)

    def test_week_one_on_the_day_it_was_created(self):
        uid = support.new_user(timezone=WEST)
        plans.save_plan(uid, {"name": "p", "duration_weeks": 4, "schedule": []})
        self.assertEqual(plans.get_current_plan(uid)["week"], 1)


class NoServerClockInPerUserCode(unittest.TestCase):
    """A regression guard: it is easy to reintroduce datetime.now(TZ) while
    editing these modules, and the mistake is invisible until a user signs up
    from another timezone."""

    PER_USER_MODULES = ("stats.py", "chat.py", "plans.py", "food.py",
                        "ai.py", "daily.py", "weekly.py", "nudges.py")

    def test_per_user_modules_do_not_use_the_server_timezone(self):
        root = pathlib.Path(__file__).resolve().parent.parent / "coach"
        offenders = []
        for name in self.PER_USER_MODULES:
            source = (root / name).read_text()
            for match in re.finditer(r"datetime\.now\(TZ\)|astimezone\(TZ\)|date\.today\(\)", source):
                line = source[:match.start()].count("\n") + 1
                offenders.append(f"{name}:{line} {match.group()}")
        self.assertEqual(offenders, [], "use db.user_tz(db.get_user(user_id)) instead")


if __name__ == "__main__":
    unittest.main()
