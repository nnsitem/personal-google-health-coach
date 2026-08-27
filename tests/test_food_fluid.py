"""A drinkable item logged as FOOD used to lose its volume entirely.

From the live history: type=drink entries carried both figures, type=food ones
carried only calories —

    drink  บูสต์จูส                              350 kcal   500 ml
    drink  สมูทตี้บูสต์จูสผสมเวย์โปรตีน            350 kcal   500 ml
    food   สมูทตี้สับปะรดและเสาวรส (Boost)        350 kcal     -
    food   เวย์โปรตีน (300 มล.)                   230 kcal     -   <- volume in the NAME

The food path wrote nutrition only, and LOG_FOOD had no volume field at all, so
whether the fluid counted came down to how the model happened to classify a
smoothie.
"""

import json
import unittest

from coach import db, food
from tests import support


class LiquidFood(unittest.TestCase):
    def setUp(self):
        self.uid = support.new_user()
        self.writes = []
        # Restore on teardown: these are module-level names, and leaving them
        # stubbed leaks into every later test module.
        for name in ("log_food_to_health", "log_hydration_to_health",
                     "_today_nutrition_totals"):
            self.addCleanup(setattr, food, name, getattr(food, name))
        food.log_food_to_health = lambda uid, a: (
            self.writes.append("nutrition") or (True, "nutrition/1"))
        food.log_hydration_to_health = lambda uid, a: (
            self.writes.append("hydration") or (True, "hydration/1"))
        food._today_nutrition_totals = lambda uid: dict(
            kcal=0, protein_g=0, fat_g=0, carbs_g=0, water_ml=0)

    def _log(self, **fields):
        payload = {"food_name_local": "test", "calories_kcal": 350,
                   "protein_g": 28, "coaching_suggestion": "✨ x", **fields}
        return food.log_chat_entry(self.uid, "food", payload)

    def test_volume_on_a_food_entry_is_recorded_as_fluid(self):
        self._log(volume_ml=500)
        self.assertIn("nutrition", self.writes)
        self.assertIn("hydration", self.writes)

    def test_both_points_are_stored_so_a_delete_removes_both(self):
        _, rowid = self._log(volume_ml=500)
        with db.connect() as conn:
            content = json.loads(conn.execute(
                "SELECT content FROM insights WHERE rowid = ?", (rowid,)).fetchone()["content"])
        self.assertEqual(sorted(content["health_point_names"]),
                         ["hydration/1", "nutrition/1"])

    def test_solid_food_records_no_fluid(self):
        self._log(volume_ml=None)
        self.assertNotIn("hydration", self.writes)

    def test_zero_volume_records_no_fluid(self):
        self._log(volume_ml=0)
        self.assertNotIn("hydration", self.writes)


class FluidCounting(unittest.TestCase):
    def test_water_total_counts_a_smoothie_logged_as_food(self):
        # It used to require type=="drink", so the same smoothie counted for
        # 0 ml depending only on how it had been classified.
        uid = support.new_user()
        support.add_food_log(uid, kcal=350, ml=500, kind="food", name="สมูทตี้")
        totals = food._local_today_totals(uid, db.user_tz(db.get_user(uid)))
        self.assertEqual(totals["water_ml"], 500)
        self.assertEqual(totals["kcal"], 350)

    def test_solid_food_adds_no_water(self):
        uid = support.new_user()
        support.add_food_log(uid, kcal=500, kind="food", name="ข้าวผัด")
        totals = food._local_today_totals(uid, db.user_tz(db.get_user(uid)))
        self.assertEqual(totals["water_ml"], 0)


class PromptContract(unittest.TestCase):
    """Both prompts must offer the field, or the model has nowhere to put it."""

    def test_food_schemas_expose_volume_ml(self):
        from coach.chat import CHAT_SYSTEM_PROMPT
        self.assertIn('"volume_ml": null', CHAT_SYSTEM_PROMPT)      # LOG_FOOD example
        self.assertIn('"volume_ml": number or null', food.FOOD_VISION_PROMPT)

    def test_both_prompts_route_drinkables_to_the_drink_shape(self):
        from coach.chat import CHAT_SYSTEM_PROMPT
        for prompt in (CHAT_SYSTEM_PROMPT, food.FOOD_VISION_PROMPT):
            self.assertIn("smoothie", prompt.lower())


if __name__ == "__main__":
    unittest.main()
