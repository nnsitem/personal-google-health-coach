"""Daily totals. Google Health aggregates every writer, so a mirror app once
made the progress card read 820,981 kcal against a 3,200 target."""

import unittest

from coach import food
from tests import support


class _Rollup:
    def __init__(self, kcal=0, ml=0, protein=0, carbs=0, fat=0):
        self.kcal, self.ml = kcal, ml
        self.protein, self.carbs, self.fat = protein, carbs, fat

    def daily_rollup(self, data_type, start, end):
        if data_type == "nutrition-log":
            if not any((self.kcal, self.protein, self.carbs, self.fat)):
                return []
            return [{"nutritionLog": {
                "energy": {"kcalSum": self.kcal},
                "totalCarbohydrate": {"gramsSum": self.carbs},
                "totalFat": {"gramsSum": self.fat},
                "nutrients": [{"nutrient": "PROTEIN", "quantity": {"gramsSum": self.protein}}]}}]
        return [{"hydrationLog": {"amountConsumed": {"millilitersSum": self.ml}}}] if self.ml else []


class _Broken:
    def daily_rollup(self, *a):
        raise RuntimeError("API down")


class TotalsBase(unittest.TestCase):
    def setUp(self):
        self.uid = support.new_user()
        support.set_targets(self.uid)
        support.add_food_log(self.uid, kcal=140, protein=15, carbs=16, fat=2)
        support.add_food_log(self.uid, ml=300, kind="drink", name="water")
        self._original = food.client_for_user
        self.addCleanup(setattr, food, "client_for_user", self._original)

    def _totals(self, client):
        food.client_for_user = lambda uid: client
        return food._today_nutrition_totals(self.uid)


class PlausibilityGuard(TotalsBase):
    def test_detects_the_real_incident_values(self):
        implausible = food._implausible_totals(self.uid, {
            "kcal": 820981, "protein_g": 46382, "carbs_g": 91866,
            "fat_g": 29970, "water_ml": 1200})
        self.assertEqual(sorted(implausible), ["carbs_g", "fat_g", "kcal", "protein_g"])

    def test_a_normal_day_passes(self):
        self.assertEqual(food._implausible_totals(self.uid, {
            "kcal": 2100, "protein_g": 130, "carbs_g": 260,
            "fat_g": 70, "water_ml": 2500}), [])

    def test_a_genuinely_big_day_passes(self):
        # 3x the target is a real (if rare) day; only absurd multiples are data faults.
        self.assertEqual(food._implausible_totals(self.uid, {
            "kcal": 9600, "protein_g": 400, "carbs_g": 1000,
            "fat_g": 200, "water_ml": 7000}), [])

    def test_corrupt_rollup_falls_back_to_our_history(self):
        totals = self._totals(_Rollup(kcal=748135, protein=41975, carbs=83061, fat=27678, ml=300))
        self.assertEqual(totals["kcal"], 140)


class SourceMerge(TotalsBase):
    """Google Health holds our writes PLUS anything logged elsewhere, so it
    should never read lower than our own log."""

    def test_higher_external_total_wins(self):
        totals = self._totals(_Rollup(kcal=2500, ml=2000))
        self.assertEqual(totals["kcal"], 2500)
        self.assertEqual(totals["water_ml"], 2000)

    def test_wiped_store_falls_back_to_our_floor(self):
        # Deleting nutrition in Health Connect removes every app's rows, ours
        # included; the card must not drop to zero for meals we logged.
        totals = self._totals(_Rollup())
        self.assertEqual(totals["kcal"], 140)
        self.assertEqual(totals["water_ml"], 300)
        self.assertEqual(totals["protein_g"], 15)

    def test_compared_per_metric(self):
        totals = self._totals(_Rollup(kcal=2500, ml=0))
        self.assertEqual(totals["kcal"], 2500)
        self.assertEqual(totals["water_ml"], 300)

    def test_api_failure_falls_back_to_our_history(self):
        self.assertEqual(self._totals(_Broken())["kcal"], 140)


class LocalHistory(TotalsBase):
    def test_reads_our_own_logged_rows(self):
        from coach import db
        totals = food._local_today_totals(self.uid, db.user_tz(db.get_user(self.uid)))
        self.assertEqual(totals, {"kcal": 140, "protein_g": 15, "fat_g": 2,
                                  "carbs_g": 16, "water_ml": 300})


if __name__ == "__main__":
    unittest.main()
