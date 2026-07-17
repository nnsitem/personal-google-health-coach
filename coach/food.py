"""Food photo analysis and nutrition logging.

Flow: user sends a food photo on LINE → Gemini vision estimates the meal and
its nutrition → we write a NutritionLog data point to Google Health.

The estimate is approximate (vision-based), logged as "anonymous food" with a
manually-populated nutrient payload.
"""

import json
import logging
import re
from datetime import date, datetime, timedelta, timezone

from google import genai

from coach import db
from coach import gemini
from coach.config import GEMINI_API_KEY as DEFAULT_GEMINI_KEY, TZ
from coach.health_api import HealthAPIError, client_for_user

log = logging.getLogger(__name__)

FOOD_VISION_PROMPT = """\
You are a nutrition and hydration assistant. Look at the photo and decide whether
it shows FOOD (a meal/snack) or a DRINK (water, beverage).

Respond with ONLY a JSON object (no markdown, no prose).

If it's FOOD, use this shape:
{
  "type": "food",
  "food_name_en": "short food/meal name in ENGLISH",
  "food_name_local": "the same name in the user's language",
  "confidence": "high | medium | low",
  "calories_kcal": number,
  "protein_g": number,
  "total_carbohydrate_g": number,
  "total_fat_g": number,
  "notes": "one short sentence on assumptions (portion size, ingredients)"
}

If it's a DRINK (water bottle, glass, cup, etc.), use this shape:
{
  "type": "drink",
  "drink_name_en": "short drink name in ENGLISH (e.g. 'water bottle', 'iced coffee')",
  "drink_name_local": "the same name in the user's language",
  "confidence": "high | medium | low",
  "container_count": number,
  "volume_ml": number,
  "is_water": true or false,
  "calories_kcal": number,
  "protein_g": number,
  "total_carbohydrate_g": number,
  "total_fat_g": number,
  "notes": "one short sentence on assumptions (how many containers, size each)"
}

DRINK volume rules (important — read carefully):
- COUNT every drink container in the photo and put that number in "container_count".
  Two bottles = 2, three glasses = 3, etc.
- "volume_ml" is the TOTAL across ALL containers, not one.
- The user photographs a drink to log what they consumed. Count EVERY container
  at its full/normal serving size (e.g. a typical water bottle ≈ 500 ml, a small
  bottle ≈ 330 ml, a glass ≈ 250 ml), regardless of how full or empty it currently
  looks. An empty bottle means the user already drank it, so it STILL counts as one
  full serving. Do NOT reduce the volume based on the leftover liquid level.
- Example: two 500 ml water bottles (full, half, or empty) → container_count 2,
  volume_ml 1000.
- All the nutrition fields (calories, protein, etc.) must also be TOTALS across
  all containers.

Estimate realistic values for what's shown. If there is no drink container at all
in the photo, set "type" to "unknown".
"""


# Meal type based on the local log time (authoritative — a photo can't reliably
# tell breakfast from lunch, but the clock can).
def _infer_meal_type(now: datetime) -> str:
    h = now.hour
    if 5 <= h < 11:
        return "BREAKFAST"     # 05:00–11:00
    if 11 <= h < 14:
        return "LUNCH"         # 11:00–14:00
    if 14 <= h < 17:
        return "SNACK"         # 14:00–17:00 (afternoon snack)
    if 17 <= h < 21:
        return "DINNER"        # 17:00–21:00
    return "SNACK"             # late night / early morning


_MEAL_TYPES = {"BREAKFAST", "LUNCH", "DINNER", "SNACK"}

# Typical local time for a named meal — used to place a chat log ("log my
# breakfast: ...") at a sensible spot on the Google Health timeline when the
# user named the meal but not a clock time.
_MEAL_DEFAULT_TIME = {
    "BREAKFAST": (8, 0),
    "LUNCH": (12, 30),
    "SNACK": (15, 30),
    "DINNER": (19, 0),
}


def _explicit_meal_type(analysis: dict) -> str | None:
    """The user-stated meal type, if the analysis carries a valid one."""
    mt = str(analysis.get("meal_type") or "").strip().upper()
    return mt if mt in _MEAL_TYPES else None


def _num(x) -> float:
    """Lenient numeric coercion for model-produced values ('450', 450, None)."""
    try:
        return float(x or 0)
    except (TypeError, ValueError):
        return 0.0


def _resolve_log_time(analysis: dict, tz) -> datetime:
    """When the log happened, in the user's local time.

    Priority: an explicit "time" (HH:MM, optionally with "date" YYYY-MM-DD for
    a previous day) → the named meal's typical hour → now. Never in the
    future: a claimed time ahead of the clock falls back to now, which also
    keeps photo logs (no time/meal hints) exactly as before.
    """
    now = datetime.now(tz)
    dt = None

    m = re.fullmatch(r"(\d{1,2}):(\d{2})", str(analysis.get("time") or "").strip())
    if m and int(m.group(1)) < 24 and int(m.group(2)) < 60:
        base = now.date()
        try:
            d = str(analysis.get("date") or "").strip()
            if d:
                base = date.fromisoformat(d)
        except ValueError:
            pass
        dt = datetime(base.year, base.month, base.day,
                      int(m.group(1)), int(m.group(2)), tzinfo=tz)
    else:
        meal_type = _explicit_meal_type(analysis)
        if meal_type:
            h, minute = _MEAL_DEFAULT_TIME[meal_type]
            dt = now.replace(hour=h, minute=minute, second=0, microsecond=0)

    if dt is None or dt > now:
        return now
    return dt


def _get_language(user_id: str) -> str:
    """The user's preferred language as a display name for prompting Gemini
    (e.g. 'Thai', 'English'). Delegates to the shared resolver in db.
    """
    return db.get_user_language(user_id)


def _lang_code(language: str) -> str:
    """Normalize a language name/code to 'th' or 'en' for label lookup."""
    l = language.strip().lower()
    if l.startswith("th") or "thai" in l or "ไทย" in l:
        return "th"
    return "en"


def analyze_food_image(user_id: str, image_bytes: bytes, mime_type: str = "image/jpeg",
                       language: str = "English") -> dict | None:
    """Run Gemini vision on the image and return a nutrition estimate dict.

    Returns None if analysis fails or the image isn't food.
    """
    user = db.get_user(user_id)
    api_key = (user.get("gemini_api_key") if user else None) or DEFAULT_GEMINI_KEY
    if not api_key:
        raise RuntimeError("No Gemini API key configured")

    # The '*_en' name must be English (used for the Google Health log); the
    # '*_local' name and 'notes' must be in the user's language (for the reply).
    prompt = FOOD_VISION_PROMPT + (
        f"\n\nThe user's language is {language}. Write '*_local' fields and 'notes' "
        f"in {language}. Always keep '*_en' fields in English, and keep all JSON keys "
        "and the 'type' value exactly as specified in English."
    )

    image_part = genai.types.Part.from_bytes(data=image_bytes, mime_type=mime_type)
    try:
        # Shorter budget than scheduled jobs — a person is waiting in chat.
        text = gemini.generate(
            api_key, contents=[prompt, image_part],
            max_output_tokens=1024, max_wait=60,
        )
    except gemini.GeminiUnavailable:
        # Capacity outage, not a vision failure — let the caller tell the user
        # honestly instead of replying "I can't tell if this is food".
        raise
    except Exception:
        log.exception("food vision failed")
        return None
    data = _extract_json(text)
    if data and data.get("type") in ("food", "drink"):
        return data
    return None


def _extract_json(text: str) -> dict | None:
    if not text:
        return None
    if "```" in text:
        # strip fences
        start = text.find("```")
        start = text.find("\n", start) + 1
        end = text.find("```", start)
        if end > start:
            text = text[start:end]
    brace_start = text.find("{")
    brace_end = text.rfind("}") + 1
    if brace_start >= 0 and brace_end > brace_start:
        try:
            return json.loads(text[brace_start:brace_end])
        except (json.JSONDecodeError, ValueError):
            return None
    return None


def _build_nutrition_datapoint(analysis: dict, now: datetime) -> dict:
    """Build a Google Health NutritionLog DataPoint from the analysis.

    Logged as anonymous food (manual nutrients + energy + macros).
    """
    # A meal type the user stated wins; otherwise derive it from the log time.
    meal_type = _explicit_meal_type(analysis) or _infer_meal_type(now)

    interval = _interval_at(now)

    calories = float(analysis.get("calories_kcal") or 0)
    protein = float(analysis.get("protein_g") or 0)
    carbs = float(analysis.get("total_carbohydrate_g") or 0)
    fat = float(analysis.get("total_fat_g") or 0)

    # English name for the Google Health log (falls back to local, then generic)
    food_name_en = (
        analysis.get("food_name_en")
        or analysis.get("food_name_local")
        or "logged meal"
    )

    # NutritionLog anonymous-food payload. Energy in kcal, macros in grams.
    nutrition_log = {
        "foodDisplayName": food_name_en[:100],
        "mealType": meal_type,
        "interval": interval,
        "energy": {"kcal": calories},
        "totalCarbohydrate": {"grams": carbs},
        "totalFat": {"grams": fat},
        "nutrients": [
            {"nutrient": "PROTEIN", "quantity": {"grams": protein}},
        ],
    }

    return {
        # MANUAL = user-entered; more accurate than UNKNOWN and may surface a
        # timeline card in the Google Health app.
        "dataSource": {"recordingMethod": "MANUAL"},
        "nutritionLog": nutrition_log,
    }


def _interval_at(dt: datetime) -> dict:
    """Build a 1-minute interval ending at `dt` (tz-aware), with its UTC offset."""
    end_dt = dt.astimezone(timezone.utc)
    start_dt = end_dt - timedelta(minutes=1)
    offset_seconds = int(dt.utcoffset().total_seconds()) if dt.utcoffset() else 0
    utc_offset = f"{offset_seconds}s"
    return {
        "startTime": start_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "endTime": end_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "startUtcOffset": utc_offset,
        "endUtcOffset": utc_offset,
    }


def _build_hydration_datapoint(analysis: dict, tz=None) -> dict:
    """Build a Google Health HydrationLog DataPoint (volume in milliliters)."""
    interval = _interval_at(_resolve_log_time(analysis, tz or TZ))
    volume_ml = float(analysis.get("volume_ml") or 0)
    return {
        "dataSource": {"recordingMethod": "MANUAL"},
        "hydrationLog": {
            "interval": interval,
            "amountConsumed": {"milliliters": volume_ml},
        },
    }


def log_food_to_health(user_id: str, analysis: dict) -> bool:
    """Write the analyzed meal to Google Health as a nutrition-log data point.

    Returns True on success, False on failure.
    """
    # Log time follows the USER's local clock, unless the analysis carries an
    # explicit time or a named meal (chat logs: "log my breakfast ...").
    now = _resolve_log_time(analysis, db.user_tz(db.get_user(user_id)))
    data_point = _build_nutrition_datapoint(analysis, now)

    try:
        client = client_for_user(user_id)
        client.create_data_point("nutrition-log", data_point)
        log.info("logged nutrition to Google Health: %s",
                 analysis.get("food_name_en") or analysis.get("food_name_local"))
        return True
    except HealthAPIError as e:
        log.error("failed to write nutrition-log to Google Health: %s", e)
        return False


def log_hydration_to_health(user_id: str, analysis: dict) -> bool:
    """Write the analyzed drink to Google Health as a hydration-log data point."""
    data_point = _build_hydration_datapoint(analysis, db.user_tz(db.get_user(user_id)))
    try:
        client = client_for_user(user_id)
        client.create_data_point("hydration-log", data_point)
        log.info("logged hydration to Google Health: %s ml", analysis.get("volume_ml"))
        return True
    except HealthAPIError as e:
        log.error("failed to write hydration-log to Google Health: %s", e)
        return False


def _store_food_log(user_id: str, analysis: dict, synced: bool) -> None:
    """Record the food log locally (for history + weekly reports)."""
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO insights (user_id, ts, kind, content, delivered) VALUES (?, datetime('now'), 'food_log', ?, ?)",
            (user_id, json.dumps({**analysis, "synced_to_health": synced}), 1),
        )


def log_chat_entry(user_id: str, kind: str, analysis: dict | None) -> str:
    """Log a food/drink the user DESCRIBED in chat (no photo).

    `analysis` is the JSON the chat model emitted in a [LOG_FOOD]/[LOG_DRINK]
    directive — same shape as the vision output, plus optional meal_type /
    time / date fields. Returns a short localized status line to append to
    the coach's reply, so the visible confirmation reflects whether the
    Google Health write actually happened.
    """
    db.init_db()
    labels = LABELS.get(_lang_code(_get_language(user_id)), LABELS["en"])

    if not isinstance(analysis, dict):
        return labels["not_synced"]

    # The model writes these as JSON numbers, but be lenient ("450" etc.)
    for field in ("calories_kcal", "protein_g", "total_carbohydrate_g",
                  "total_fat_g", "volume_ml", "container_count"):
        if field in analysis:
            analysis[field] = _num(analysis[field])

    if kind == "drink":
        if round(analysis.get("volume_ml") or 0) <= 0:
            return labels["empty_drink"]
        synced_hydration = log_hydration_to_health(user_id, analysis)
        # Caloric drinks also count as nutrition, mirroring the photo flow.
        synced_nutrition = False
        if round(analysis.get("calories_kcal") or 0) > 10:
            synced_nutrition = log_food_to_health(user_id, {
                "food_name_en": analysis.get("drink_name_en")
                                or analysis.get("drink_name_local") or "drink",
                "calories_kcal": analysis.get("calories_kcal", 0),
                "protein_g": analysis.get("protein_g", 0),
                "total_carbohydrate_g": analysis.get("total_carbohydrate_g", 0),
                "total_fat_g": analysis.get("total_fat_g", 0),
                "meal_type": analysis.get("meal_type"),
                "time": analysis.get("time"),
                "date": analysis.get("date"),
            })
        _store_food_log(user_id, {**analysis, "type": "drink", "source": "chat"},
                        synced_hydration)
        if synced_hydration and synced_nutrition:
            return labels["synced_drink"] + " + " + labels["synced_food"]
        return labels["synced_drink"] if synced_hydration else labels["not_synced"]

    if round(analysis.get("calories_kcal") or 0) <= 0:
        return labels["empty_food"]
    synced = log_food_to_health(user_id, analysis)
    _store_food_log(user_id, {**analysis, "type": "food", "source": "chat"}, synced)
    return labels["synced_food"] if synced else labels["not_synced"]


def delete_last_log(user_id: str, kind: str = "food") -> str | None:
    """Delete the most recent nutrition-log or hydration-log entry from Google Health.

    kind: 'food' -> nutrition-log, 'drink' -> hydration-log.
    Returns the display name of what was deleted, or None if nothing found/failed.
    """
    from datetime import date, timedelta
    data_type = "hydration-log" if kind == "drink" else "nutrition-log"
    field = data_type.replace("-", "_")

    today = date.today()
    start = (today - timedelta(days=2)).isoformat()
    end = (today + timedelta(days=1)).isoformat()
    filter_str = (
        f'{field}.interval.civil_start_time >= "{start}" '
        f'AND {field}.interval.civil_start_time < "{end}"'
    )

    try:
        client = client_for_user(user_id)
        points = client.list_points(data_type, filter_str)
    except HealthAPIError as e:
        log.error("failed to list %s for delete: %s", data_type, e)
        return None

    if not points:
        return None

    # Points are returned newest-first (ordered by interval start desc).
    newest = points[0]
    name = newest.get("name")
    if not name:
        return None

    # Extract a display label for the confirmation message
    payload = newest.get(_camel(field), {})
    if data_type == "nutrition-log":
        label = payload.get("foodDisplayName", "last meal")
    else:
        ml = payload.get("amountConsumed", {}).get("milliliters", "")
        label = f"{round(float(ml))} ml" if ml != "" else "last drink"

    try:
        client.batch_delete_data_points(data_type, [name])
        log.info("deleted %s data point: %s (%s)", data_type, name, label)
        return label
    except HealthAPIError as e:
        log.error("failed to delete %s: %s", data_type, e)
        return None


def _camel(snake: str) -> str:
    parts = snake.split("_")
    return parts[0] + "".join(p.title() for p in parts[1:])


# Localized labels for the reply messages
LABELS = {
    "en": {
        "unclear": "🤔 I can't quite tell if this is food or a drink. Could you take a clearer photo?",
        "energy": "🔥 Energy",
        "protein": "💪 Protein",
        "carbs": "🍞 Carbs",
        "fat": "🥑 Fat",
        "volume": "🥤 Volume",
        "containers": "🧴 Containers",
        "synced_food": "✅ Logged to Google Health",
        "synced_drink": "✅ Hydration logged to Google Health",
        "not_synced": "⚠️ Analyzed, but couldn't save to Google Health",
        "low_conf": "(Estimate may be off — try a clearer photo for better accuracy)",
        "empty_drink": "🥤 This looks like an empty container, so I didn't log any hydration. Send a photo with a drink in it and I'll track it!",
        "empty_food": "🍽️ I couldn't estimate a real portion here, so nothing was logged. Try a clearer photo of the food.",
        "ai_busy": "⏳ The AI service is very busy right now, so I couldn't analyze your photo. Please try sending it again in a few minutes!",
        "quota_exhausted": "⛔ Your Gemini AI key has used up its free daily quota, so I can't analyze photos for now. It resets at midnight US Pacific time (~2pm Thailand time).",
    },
    "th": {
        "unclear": "🤔 ผมดูรูปนี้แล้วไม่แน่ใจว่าเป็นอาหารหรือเครื่องดื่ม ลองถ่ายให้ชัดขึ้นอีกนิดได้ไหมครับ?",
        "energy": "🔥 พลังงาน",
        "protein": "💪 โปรตีน",
        "carbs": "🍞 คาร์บ",
        "fat": "🥑 ไขมัน",
        "volume": "🥤 ปริมาณ",
        "containers": "🧴 จำนวนภาชนะ",
        "synced_food": "✅ บันทึกลง Google Health เรียบร้อยแล้ว",
        "synced_drink": "✅ บันทึกการดื่มน้ำลง Google Health แล้ว",
        "not_synced": "⚠️ วิเคราะห์สำเร็จ แต่ยังบันทึกลง Google Health ไม่ได้",
        "low_conf": "(ค่าประมาณอาจคลาดเคลื่อน ลองถ่ายชัด ๆ อีกครั้ง)",
        "empty_drink": "🥤 ดูเหมือนแก้ว/ขวดจะว่างเปล่า ผมเลยยังไม่ได้บันทึกนะครับ ถ้ามีน้ำอยู่ในภาพ ส่งมาใหม่ได้เลยครับ",
        "empty_food": "🍽️ ผมประเมินปริมาณอาหารไม่ได้ เลยยังไม่บันทึกครับ ลองถ่ายอาหารให้ชัดขึ้นอีกนิดนะครับ",
        "ai_busy": "⏳ ตอนนี้ระบบ AI มีผู้ใช้งานเยอะมาก ผมเลยยังวิเคราะห์รูปไม่ได้ครับ อีกสักครู่ลองส่งรูปมาใหม่นะครับ",
        "quota_exhausted": "⛔ คีย์ Gemini ของคุณใช้โควต้าฟรีของวันนี้หมดแล้ว ผมเลยวิเคราะห์รูปไม่ได้ชั่วคราวครับ โควต้าจะรีเซ็ตเที่ยงคืนเวลาแปซิฟิก (ราวบ่าย 2 เวลาไทย)",
    },
}


def handle_food_photo(user_id: str, image_bytes: bytes, mime_type: str = "image/jpeg") -> str:
    """Full flow: analyze image → log to Google Health → return a LINE reply.

    Handles both food (nutrition-log) and drinks (hydration-log).
    Reply language follows the user's stored preference.
    """
    db.init_db()

    language = _get_language(user_id)
    labels = LABELS.get(_lang_code(language), LABELS["en"])

    try:
        analysis = analyze_food_image(user_id, image_bytes, mime_type, language=language)
    except gemini.GeminiQuotaExhausted:
        return labels["quota_exhausted"]
    except gemini.GeminiUnavailable:
        return labels["ai_busy"]
    if not analysis or analysis.get("type") not in ("food", "drink"):
        return labels["unclear"]

    if analysis["type"] == "drink":
        return _handle_drink(user_id, analysis, labels)
    return _handle_food(user_id, analysis, labels)


def _handle_food(user_id: str, analysis: dict, labels: dict) -> str:
    cal = round(float(analysis.get("calories_kcal") or 0))

    # Don't log if there's no real portion (e.g. empty plate / not food)
    if cal <= 0:
        log.info("food calories is 0 — skipping nutrition log")
        return labels["empty_food"]

    synced = log_food_to_health(user_id, analysis)
    _store_food_log(user_id, analysis, synced)

    # Show the localized name in the reply, English as fallback
    name = analysis.get("food_name_local") or analysis.get("food_name_en") or "meal"
    protein = round(float(analysis.get("protein_g") or 0))
    carbs = round(float(analysis.get("total_carbohydrate_g") or 0))
    fat = round(float(analysis.get("total_fat_g") or 0))
    confidence = analysis.get("confidence", "medium")

    lines = [
        f"🍽️ {name}",
        "",
        f"{labels['energy']}: 「{cal} kcal」",
        f"{labels['protein']}: 「{protein} g」",
        f"{labels['carbs']}: 「{carbs} g」",
        f"{labels['fat']}: 「{fat} g」",
    ]
    if analysis.get("notes"):
        lines.append("")
        lines.append(f"📝 {analysis['notes']}")
    lines.append("")
    lines.append(labels["synced_food"] if synced else labels["not_synced"])
    if confidence == "low":
        lines.append(labels["low_conf"])

    return "\n".join(lines)


def _handle_drink(user_id: str, analysis: dict, labels: dict) -> str:
    ml = round(float(analysis.get("volume_ml") or 0))

    # Don't log an empty container
    if ml <= 0:
        log.info("drink volume is 0 — skipping hydration log")
        return labels["empty_drink"]

    synced_hydration = log_hydration_to_health(user_id, analysis)

    # If the drink has significant calories/protein (e.g. protein shake, juice,
    # smoothie), also log it as a nutrition entry.
    cal = round(float(analysis.get("calories_kcal") or 0))
    synced_nutrition = False
    if cal > 10:
        # Build a food-like analysis dict for the nutrition log
        nutrition_analysis = {
            "food_name_en": analysis.get("drink_name_en") or analysis.get("drink_name_local") or "drink",
            "calories_kcal": analysis.get("calories_kcal", 0),
            "protein_g": analysis.get("protein_g", 0),
            "total_carbohydrate_g": analysis.get("total_carbohydrate_g", 0),
            "total_fat_g": analysis.get("total_fat_g", 0),
        }
        synced_nutrition = log_food_to_health(user_id, nutrition_analysis)

    _store_food_log(user_id, analysis, synced_hydration)

    name = analysis.get("drink_name_local") or analysis.get("drink_name_en") or "drink"
    protein = round(float(analysis.get("protein_g") or 0))
    carbs = round(float(analysis.get("total_carbohydrate_g") or 0))
    fat = round(float(analysis.get("total_fat_g") or 0))
    confidence = analysis.get("confidence", "medium")

    count = int(float(analysis.get("container_count") or 1))
    lines = [
        f"💧 {name}",
        "",
    ]
    if count > 1:
        lines.append(f"{labels['containers']}: 「{count}」")
    lines.append(f"{labels['volume']}: 「{ml} ml」")
    if cal > 0:
        lines.append(f"{labels['energy']}: 「{cal} kcal」")
    if protein > 0:
        lines.append(f"{labels['protein']}: 「{protein} g」")
    if carbs > 0:
        lines.append(f"{labels['carbs']}: 「{carbs} g」")
    if fat > 0:
        lines.append(f"{labels['fat']}: 「{fat} g」")
    if analysis.get("notes"):
        lines.append("")
        lines.append(f"📝 {analysis['notes']}")
    lines.append("")
    if synced_hydration and synced_nutrition:
        lines.append(labels["synced_drink"] + " + " + labels["synced_food"])
    elif synced_hydration:
        lines.append(labels["synced_drink"])
    else:
        lines.append(labels["not_synced"])
    if confidence == "low":
        lines.append(labels["low_conf"])

    return "\n".join(lines)


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    DEFAULT_USER_ID = "U1068a1b9c15b44e7ff1439bdefdeb5dc"

    if len(sys.argv) < 2:
        print("Usage: python -m coach.food <image_path>")
        sys.exit(1)
    with open(sys.argv[1], "rb") as f:
        img = f.read()
    print(handle_food_photo(DEFAULT_USER_ID, img))
