"""Directive parsing and the missed-directive safety net.

The chat model is not reliable about appending [LOG_FOOD]: live, "เพิ่มมื้อเที่ยง
ไข่ต้ม 1 ฟอง" came back as a fluent "บันทึกเรียบร้อยแล้ว" with no directive, so
nothing reached Google Health while the user was told it had.
"""

import json
import unittest

from coach import chat
from tests import support


def _parse(text):
    return [(tag, inner) for tag, inner, _, _ in chat._scan_directives(text)]


def _strip(text):
    kept, cursor = [], 0
    for _, _, start, end in chat._scan_directives(text):
        kept.append(text[cursor:start])
        cursor = end
    kept.append(text[cursor:])
    return "".join(kept).strip()


class DirectiveScanner(unittest.TestCase):
    def test_normal_payload(self):
        tag, inner = _parse('ok [LOG_FOOD: {"food_name_en": "Egg", "calories_kcal": 75}]')[0]
        self.assertEqual(tag, "LOG_FOOD")
        self.assertEqual(json.loads(inner)["calories_kcal"], 75)

    def test_bracket_inside_a_string_value(self):
        # The old regex stopped at the first "]", truncating the JSON.
        text = ('[LOG_FOOD: {"food_name_en": "Egg [large]", '
                '"coaching_suggestion": "✨ ไข่ต้ม [ดี] ครับ", "calories_kcal": 75}]')
        payload = json.loads(_parse(text)[0][1])
        self.assertEqual(payload["food_name_en"], "Egg [large]")
        self.assertEqual(payload["calories_kcal"], 75)

    def test_missing_closing_bracket(self):
        # The old regex found no match at all and dropped the log silently.
        text = 'ok [LOG_DRINK: {"drink_name_en": "water", "volume_ml": 330}\nnext line'
        tag, inner = _parse(text)[0]
        self.assertEqual(tag, "LOG_DRINK")
        self.assertEqual(json.loads(inner)["volume_ml"], 330)

    def test_plain_tags(self):
        self.assertEqual(_parse("sure [MEMORY: language = Thai] and [DELETE_LAST: drink] done"),
                         [("MEMORY", "language = Thai"), ("DELETE_LAST", "drink")])

    def test_several_directives_in_one_reply(self):
        tags = [t for t, _ in _parse('[LOG_FOOD: {"a": 1}] [LOG_DRINK: {"b": 2}]')]
        self.assertEqual(tags, ["LOG_FOOD", "LOG_DRINK"])

    def test_directives_are_stripped_from_the_visible_reply(self):
        self.assertEqual(_strip("sure [MEMORY: language = Thai] and [DELETE_LAST: drink] done"),
                         "sure  and  done")

    def test_adjust_payload(self):
        inner = _parse('[ADJUST_LAST: {"kind": "drink", "times": 4}]')[0][1]
        self.assertEqual(json.loads(inner)["times"], 4)


class LogIntentGate(unittest.TestCase):
    """Gates the extractor fallback. It CREATES entries, so a wrong yes would
    duplicate rather than rescale."""

    def test_record_requests_fire(self):
        for text in ("เพิ่มมื่อเช้า ไข่ต้ม 1 ฟอง",      # the user's exact typo
                     "เพิ่มมื้อเที่ยง ไข่ต้ม 1 ฟอง",     # the live failure
                     "ลงมื้อเที่ยง ไข่ต้ม 1 ฟอง",
                     "เพิ่มน้ำ 330 ml อีกสองขวดค่ะ",
                     "บันทึกน้ำ 1 แก้ว",
                     "บันทึกน้ำครึ่งแก้ว",
                     "จดข้าวผัด 1 จาน",
                     "กินไข่ต้ม 2 ฟอง",
                     "log 2 glasses of water",
                     "add lunch: chicken salad",
                     "I ate a boiled egg"):
            self.assertTrue(chat._looks_like_log_request(text), text)

    def test_revisions_do_not_fire(self):
        for text in ("กินไปแล้ว 4 รอบ", "แก้เป็น 750ml", "ปรับเป็น 3 เท่า",
                     "I had 4 of those", "only drank half", "make that 750 ml"):
            self.assertFalse(chat._looks_like_log_request(text), text)

    def test_questions_do_not_fire(self):
        for text in ("วันนี้กินไปกี่แคลแล้ว", "เมื่อคืนนอนเป็นยังไงบ้าง",
                     "How many calories did I have today?"):
            self.assertFalse(chat._looks_like_log_request(text), text)

    def test_other_intents_do_not_fire(self):
        for text in ("ลบรายการล่าสุด", "ลบรายการวันนี้ทั้งหมด", "สร้างแผนออกกำลังกายให้หน่อย"):
            self.assertFalse(chat._looks_like_log_request(text), text)

    def test_quote_reply_never_fires(self):
        # A quote-reply points at an entry that already exists.
        self.assertFalse(chat._looks_like_log_request("เพิ่มมื้อเช้า ไข่ต้ม 1 ฟอง",
                                                     is_quote_reply=True))

    def test_glass_is_not_read_as_a_change(self):
        # แก้ว ("glass") merely contains แก้ ("to change").
        self.assertTrue(chat._looks_like_log_request("บันทึกน้ำ 1 แก้ว"))


class DirectiveSideEffects(unittest.TestCase):
    def test_memory_is_saved(self):
        uid = support.new_user()
        chat._process_directives(uid, "noted [MEMORY: language = Thai]")
        self.assertEqual(chat._get_coach_memory(uid).get("language"), "Thai")

    def test_valid_targets_are_saved(self):
        uid = support.new_user()
        _, _, _, _, _, failures = chat._process_directives(
            uid, 'ok [SET_NUTRITION_TARGETS: {"kcal": 1800, "protein_g": 150}]')
        self.assertEqual(failures, [])
        self.assertEqual(chat._get_goals(uid)["daily_nutrition_targets"]["kcal"], 1800)

    def test_broken_targets_are_reported_not_swallowed(self):
        # Silently dropping this left the model's "your targets are updated"
        # reply standing over nothing saved.
        uid = support.new_user()
        _, _, _, _, _, failures = chat._process_directives(
            uid, "ok [SET_NUTRITION_TARGETS: {kcal: 1800,,}]")
        self.assertIn("nutrition_targets", failures)
        self.assertNotIn("daily_nutrition_targets", chat._get_goals(uid))

    def test_delete_today_kinds(self):
        uid = support.new_user()
        for inner, expected in (("all", "all"), ("food", "food"), ("drink", "drink")):
            *_, delete_today, _ = chat._process_directives(uid, f"[DELETE_TODAY: {inner}]")
            self.assertEqual(delete_today, expected)


class DeviceDataGate(unittest.TestCase):
    """The pre-chat sync is nine Google Health calls made before the user gets
    any answer — ~15s normally, 96s during the 2026-08-22 timeouts. A message
    that only records something has no use for step or sleep data."""

    def test_logging_and_editing_skip_the_sync(self):
        for text in ("เพิ่มน้ำ 250ml", "บันทึกน้ำ 1 แก้ว", "ลงมื้อเที่ยง ไข่ต้ม 1 ฟอง",
                     "จดข้าวผัด 1 จาน", "log 2 glasses of water",
                     "แก้เป็น 750ml", "ลบรายการล่าสุด", "ลบรายการวันนี้ทั้งหมด"):
            self.assertFalse(chat._needs_device_data(text), text)

    def test_questions_and_conversation_still_sync(self):
        for text in ("เมื่อคืนนอนเป็นยังไงบ้าง", "วันนี้เดินไปกี่ก้าว",
                     "วันนี้กินไปกี่แคลแล้ว", "สร้างแผนออกกำลังกายให้หน่อย",
                     "สวัสดีครับ"):
            self.assertTrue(chat._needs_device_data(text), text)

    def test_quote_reply_skips_the_sync(self):
        self.assertFalse(chat._needs_device_data("เพิ่มมื้อเช้า ไข่ต้ม", is_quote_reply=True))


if __name__ == "__main__":
    unittest.main()
