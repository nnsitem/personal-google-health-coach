"""Diet-tagged coach memory (e.g. "[MEMORY: diet:allergy = peanuts]") is
injected explicitly into the food vision prompt (DESIGN-V3.md #4) rather than
left for the model to notice on its own in a flat memory list — missing an
allergy here is a worse failure than missing an ordinary preference.
"""

import unittest
from unittest import mock

from coach import chat, food
from tests import support


class DietMemoryInjection(unittest.TestCase):
    def setUp(self):
        self.uid = support.new_user(gemini_api_key="fake-key")
        self.captured = {}

    def _fake_generate(self, api_key, contents, **kwargs):
        self.captured["prompt"] = contents[0]
        return '{"type":"food","name_en":"banana","coaching_suggestion":"ok"}'

    def test_diet_facts_are_injected_when_present(self):
        chat.save_memory(self.uid, "allergy", "peanuts", category="diet")
        with mock.patch("coach.food.gemini.generate", side_effect=self._fake_generate):
            food.analyze_food_images(self.uid, [(b"fake-bytes", "image/jpeg")])
        self.assertIn("peanuts", self.captured["prompt"])
        self.assertIn("allergy", self.captured["prompt"])

    def test_no_injection_when_there_are_no_diet_memories(self):
        with mock.patch("coach.food.gemini.generate", side_effect=self._fake_generate):
            food.analyze_food_images(self.uid, [(b"fake-bytes", "image/jpeg")])
        self.assertNotIn("Known dietary facts", self.captured["prompt"])

    def test_non_diet_memory_is_not_pulled_in(self):
        # A goal/preference memory must not leak into the food prompt through
        # this path — only "diet"-tagged entries are.
        chat.save_memory(self.uid, "target_weight", "70kg", category="goal")
        with mock.patch("coach.food.gemini.generate", side_effect=self._fake_generate):
            food.analyze_food_images(self.uid, [(b"fake-bytes", "image/jpeg")])
        self.assertNotIn("target_weight", self.captured["prompt"])
        self.assertNotIn("70kg", self.captured["prompt"])


if __name__ == "__main__":
    unittest.main()
