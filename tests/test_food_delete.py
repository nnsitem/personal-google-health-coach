"""Deletes must report what actually happened.

Google Health refuses to delete points another client wrote
(DATA_POINT_NOT_OWNED_BY_CLIENT) and rejects the WHOLE request on one foreign
name — so a mixed list removed nothing at all, including our own entries.
"""

import unittest

from coach import food
from coach.health_api import HealthAPIError
from tests import support


class _Store:
    """Ours are deletable; another app's are not, and poison the request."""

    def __init__(self, ours=0, foreign=0, package="nl.appyhapps.healthsync"):
        self.points = (
            [support.point(f"ours/{i}") for i in range(ours)]
            + [support.point(f"hs/{i}", package=package) for i in range(foreign)]
        )
        self.sent = []

    def list_points(self, data_type, filter_str):
        return list(self.points) if data_type == "nutrition-log" else []

    def batch_delete_data_points(self, data_type, names):
        self.sent += list(names)
        if any(n.startswith("hs/") for n in names):
            raise HealthAPIError(403, '{"reason":"DATA_POINT_NOT_OWNED_BY_CLIENT"}', "batchDelete")
        self.points = [p for p in self.points if p["name"] not in names]
        return {"requested": len(names), "deleted": len(names)}

    def data_points_still_exist(self, names):
        held = {p["name"] for p in self.points}
        return [n for n in names if n in held]


class DeleteTodayLogs(unittest.TestCase):
    def setUp(self):
        self.uid = support.new_user()
        self._original = food.client_for_user
        self.addCleanup(setattr, food, "client_for_user", self._original)

    def _run(self, store, kind="food"):
        food.client_for_user = lambda uid: store
        return food.delete_today_logs(self.uid, kind)

    def test_foreign_names_are_never_sent(self):
        store = _Store(ours=11, foreign=221)
        self._run(store)
        self.assertEqual(len(store.sent), 11)
        self.assertFalse([n for n in store.sent if n.startswith("hs/")])

    def test_our_own_points_still_get_deleted(self):
        store = _Store(ours=11, foreign=221)
        self._run(store)
        self.assertFalse([p for p in store.points if p["name"].startswith("ours/")])

    def test_names_the_app_holding_what_is_left(self):
        status = self._run(_Store(ours=2, foreign=19673))
        self.assertIn("19673", status)
        self.assertIn("nl.appyhapps.healthsync", status)

    def test_clean_delete_reports_no_leftover(self):
        status = self._run(_Store(ours=3, foreign=0))
        self.assertIn("3", status)
        self.assertNotIn("healthsync", status)

    def test_reports_count_actually_removed_not_attempted(self):
        # The old code reported len(names) — what it TRIED — so the user was
        # told "cleared N meals" whether or not anything went away.
        store = _Store(ours=0, foreign=500)
        status = self._run(store)
        self.assertIn("0", status)
        self.assertIn("500", status)

    def test_nothing_logged_today(self):
        status = self._run(_Store())
        labels = food.LABELS[food._lang_code(food._get_language(self.uid))]
        self.assertEqual(status, labels["nothing_today"])


class DeleteLogPoints(unittest.TestCase):
    def setUp(self):
        self.uid = support.new_user()
        self._original = food.client_for_user
        self.addCleanup(setattr, food, "client_for_user", self._original)

    def _verify_with(self, survives):
        class C:
            def batch_delete_data_points(self, data_type, names):
                return {"requested": len(names), "deleted": len(names)}

            def data_points_still_exist(self, names):
                return list(names) if survives else []

        food.client_for_user = lambda uid: C()
        content = {"health_point_names": ["users/x/dataTypes/nutrition-log/dataPoints/9"],
                   "synced_to_health": True}
        return food._delete_log_points(self.uid, content, "food")

    def test_true_only_when_the_points_are_really_gone(self):
        self.assertTrue(self._verify_with(survives=False))

    def test_false_when_a_point_survives(self):
        # A false True either drops local history while Google Health keeps the
        # point, or lets an adjustment re-log on top of the original.
        self.assertFalse(self._verify_with(survives=True))

    def test_nothing_synced_is_already_clean(self):
        self.assertTrue(food._delete_log_points(self.uid, {"synced_to_health": False}, "food"))


class ForeignDetection(unittest.TestCase):
    def test_our_writes_carry_no_package_name(self):
        self.assertIsNone(food._foreign_app(support.point("ours/1")))

    def test_another_app_is_identified(self):
        self.assertEqual(food._foreign_app(support.point("hs/1", package="x.y.z")), "x.y.z")


if __name__ == "__main__":
    unittest.main()
