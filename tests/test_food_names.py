"""Display names must start with a capital letter.

63 of 274 logged names arrived lower-case ("aburi salmon sushi", "water",
"mate tea", "pizza slice") because the prompt only asked for brand names to be
capitalised. Google Health shows the English name verbatim, so those entries
looked sloppy beside the ones that happened to come back title-cased.
"""

import unittest

from coach import food
from tests import support


class CapitalizeFirst(unittest.TestCase):
    def test_lowercase_first_letter_is_raised(self):
        for given, expected in (
            ("aburi salmon sushi", "Aburi salmon sushi"),
            ("water", "Water"),
            ("mate tea", "Mate tea"),
            ("pizza slice", "Pizza slice"),
        ):
            self.assertEqual(food._capitalize_first(given), expected)

    def test_already_capitalised_is_untouched(self):
        for name in ("Wakame Seaweed Chicken Balls", "Boiled Egg (1 egg)"):
            self.assertEqual(food._capitalize_first(name), name)

    def test_brand_casing_survives(self):
        # .title() would produce "Mcdonald'S Fries" / "Iphone" — never use it.
        for name in ("McDonald's fries", "iPhone-shaped cake",
                     "7-Eleven iced coffee", "pH balanced water"):
            self.assertEqual(food._capitalize_first(name)[1:], name[1:])

    def test_thai_and_digits_are_left_alone(self):
        for name in ("น้ำเปล่า (500 มล.)", "2 boiled eggs", "🍜 ramen"):
            self.assertEqual(food._capitalize_first(name), name)

    def test_empty_input_is_safe(self):
        self.assertEqual(food._capitalize_first(""), "")
        self.assertIsNone(food._capitalize_first(None))


class NormalizeNames(unittest.TestCase):
    def test_every_name_field_is_normalised_and_trimmed(self):
        analysis = {"food_name_en": "  grilled chicken ", "food_name_local": "ไก่ย่าง",
                    "drink_name_en": "iced tea", "drink_name_local": "ชาเย็น",
                    "calories_kcal": 100}
        food._normalize_names(analysis)
        self.assertEqual(analysis["food_name_en"], "Grilled chicken")
        self.assertEqual(analysis["drink_name_en"], "Iced tea")
        self.assertEqual(analysis["food_name_local"], "ไก่ย่าง")
        self.assertEqual(analysis["calories_kcal"], 100)

    def test_non_dict_and_non_string_values_do_not_raise(self):
        self.assertIsNone(food._normalize_names(None))
        analysis = {"food_name_en": None, "drink_name_en": 42}
        food._normalize_names(analysis)
        self.assertIsNone(analysis["food_name_en"])


class GoogleHealthPayload(unittest.TestCase):
    """The value Google Health stores must be capitalised even when a caller
    built its own dict — adjustments and a caloric drink's nutrition twin do."""

    def test_food_display_name_is_capitalised(self):
        from datetime import datetime
        from zoneinfo import ZoneInfo
        point = food._build_nutrition_datapoint(
            {"food_name_en": "aburi salmon sushi", "calories_kcal": 300},
            datetime.now(ZoneInfo("Asia/Bangkok")))
        self.assertEqual(point["nutritionLog"]["foodDisplayName"], "Aburi salmon sushi")

    def test_display_name_stays_within_the_field_limit(self):
        from datetime import datetime
        from zoneinfo import ZoneInfo
        point = food._build_nutrition_datapoint(
            {"food_name_en": "a" * 300, "calories_kcal": 10},
            datetime.now(ZoneInfo("Asia/Bangkok")))
        self.assertEqual(len(point["nutritionLog"]["foodDisplayName"]), 100)


class PromptRule(unittest.TestCase):
    def test_both_prompts_ask_for_a_capital_letter(self):
        from coach.chat import CHAT_SYSTEM_PROMPT
        for prompt in (CHAT_SYSTEM_PROMPT, food.FOOD_VISION_PROMPT):
            self.assertIn("CAPITAL LETTER", prompt)


if __name__ == "__main__":
    unittest.main()
