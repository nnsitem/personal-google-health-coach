"""Does the model put food, drink and the in-between cases in the right shape?

MAKES REAL API CALLS — a live Gemini key and a Google Health token are used, so
this is not part of the automatic suite. Google Health WRITES are stubbed; the
run costs Gemini quota and nothing else.

    docker compose run --rm -v "$PWD/coach:/app/coach" -v "$PWD/tests:/app/tests" \
        coach python tests/manual/classification.py

Why it exists: whether a smoothie's fluid was recorded used to depend on whether
the model happened to call it "food" or "drink" (a 350 kcal Boost smoothie
counted for 0 ml of hydration). The prompts now route anything drinkable to the
drink shape and allow a volume on food for soup-like items, and the food path
records fluid whenever a volume is present. This checks all three behaviours
against the model rather than assuming them.

Recorded result, 2026-08-23, gemini-pro-latest: 12/12 categories correct, and
the four hardest repeated 3x with no variation.
"""

import logging
import sys
import time

logging.disable(logging.WARNING)

from coach import chat, food, sync

USER = "U1068a1b9c15b44e7ff1439bdefdeb5dc"

# (message, expects calories, expects a volume)
CASES = [
    ("ลงมื้อเที่ยง ข้าวผัดไก่ 1 จาน", True, False),
    ("บันทึกไอศกรีมวานิลลา 1 ถ้วย", True, False),
    ("เพิ่มน้ำเปล่า 500 ml", False, True),
    ("เพิ่มน้ำ 2 แก้ว", False, True),
    ("เพิ่มน้ำปั่น Boost สับปะรดเสาวรส เพิ่มเวย์โปรตีน 1 แก้ว", True, True),
    ("บันทึกเวย์โปรตีน 1 สกู๊ป ผสมน้ำ 300 ml", True, True),
    ("ลงมื้อเย็น ต้มยำกุ้ง 1 ถ้วย", True, True),
    ("ลงข้าวต้มหมู 1 ถ้วย", True, True),
    ("เพิ่มนมสด 1 กล่อง 200 ml", True, True),
    ("บันทึกนมเปรี้ยว 1 ขวด", True, True),
    ("เพิ่มกาแฟลาเต้ร้อน 1 แก้ว", True, True),
    ("log a mango smoothie 400ml", True, True),
]

# Repeated to catch a model that is merely usually right.
REPEAT = [
    "เพิ่มน้ำปั่น Boost สับปะรดเสาวรส เพิ่มเวย์โปรตีน 1 แก้ว",
    "บันทึกเวย์โปรตีนเชค 1 แก้ว",
    "ลงมื้อเย็น ต้มยำกุ้ง 1 ถ้วย",
    "เพิ่มน้ำเปล่า 1 ขวด",
]


def _install_stubs(stored, writes):
    food.log_food_to_health = lambda uid, a: (
        writes.append("nutrition") or (True, "nutrition/1"))
    food.log_hydration_to_health = lambda uid, a: (
        writes.append("hydration") or (True, "hydration/1"))
    food._today_nutrition_totals = lambda uid: dict(
        kcal=800, protein_g=40, fat_g=20, carbs_g=90, water_ml=600)
    food._store_food_log = lambda uid, a, synced: (stored.append(a) or 1)
    chat._ensure_fresh_data = lambda uid: None
    sync.run_sync = lambda uid: None


def _log_once(text, stored, writes):
    stored.clear()
    writes.clear()
    started = time.time()
    chat.handle_message(USER, text)
    entry = stored[-1] if stored else {}
    return {
        "type": entry.get("type", "-"),
        "kcal": round(float(entry.get("calories_kcal") or 0)),
        "ml": round(float(entry.get("volume_ml") or 0)),
        "wrote": sorted(set(writes)),
        "seconds": time.time() - started,
    }


def main() -> int:
    stored, writes = [], []
    _install_stubs(stored, writes)

    print(f"{'message':46} {'type':6} {'kcal':>6} {'ml':>6}  wrote")
    print("-" * 96)
    passed = 0
    for text, wants_kcal, wants_ml in CASES:
        r = _log_once(text, stored, writes)
        ok = (r["kcal"] > 0) == wants_kcal and (r["ml"] > 0) == wants_ml
        passed += ok
        print(f"{text[:44]:46} {r['type']:6} {r['kcal']:6} {r['ml']:6}  "
              f"{','.join(w[:4] for w in r['wrote']) or '-':14} "
              f"{'OK' if ok else 'WRONG'}  ({r['seconds']:.0f}s)")
    print("-" * 96)
    print(f"{passed}/{len(CASES)} categories correct\n")

    print("Consistency, 3 runs each:")
    for text in REPEAT:
        runs = [_log_once(text, stored, writes) for _ in range(3)]
        volumes = [r["ml"] for r in runs]
        print(f"  {text[:50]:52} ml={volumes} "
              f"{'stable' if all(v > 0 for v in volumes) else 'VARIES'}")

    return 0 if passed == len(CASES) else 1


if __name__ == "__main__":
    sys.exit(main())
