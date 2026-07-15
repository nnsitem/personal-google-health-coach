"""Food photo analysis and nutrition logging.

Flow: user sends a food photo on LINE → Gemini vision estimates the meal and
its nutrition → we write a NutritionLog data point to Google Health.

The estimate is approximate (vision-based), logged as "anonymous food" with a
manually-populated nutrient payload.
"""

import json
import logging
import time
from datetime import datetime, timezone

from google import genai

from coach import db
from coach.config import GEMINI_API_KEY as DEFAULT_GEMINI_KEY, GEMINI_MODEL, GEMINI_FALLBACK_MODELS, TZ
from coach.health_api import HealthClient, HealthAPIError

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
  "volume_ml": number,
  "is_water": true or false,
  "calories_kcal": number,
  "protein_g": number,
  "total_carbohydrate_g": number,
  "total_fat_g": number,
  "notes": "one short sentence on assumptions (container size, fill level)"
}

Estimate realistic values for what's shown. Estimate volume_ml from the container
size and how full it looks. If you can't tell what it is, set "type" to "unknown".
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


def _get_language(user_id: str) -> str:
    """Read the user's preferred language from coach_memory. Returns a display
    name suitable for prompting Gemini (e.g. 'Thai', 'English'). Defaults to English.
    """
    try:
        with db.connect() as conn:
            row = conn.execute(
                "SELECT content FROM coach_memory WHERE user_id = ? AND lower(name) = 'language'",
                (user_id,),
            ).fetchone()
        if row and row["content"]:
            return row["content"].strip()
    except Exception:
        pass
    return "English"


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

    client = genai.Client(api_key=api_key)
    image_part = genai.types.Part.from_bytes(data=image_bytes, mime_type=mime_type)

    # The '*_en' name must be English (used for the Google Health log); the
    # '*_local' name and 'notes' must be in the user's language (for the reply).
    prompt = FOOD_VISION_PROMPT + (
        f"\n\nThe user's language is {language}. Write '*_local' fields and 'notes' "
        f"in {language}. Always keep '*_en' fields in English, and keep all JSON keys "
        "and the 'type' value exactly as specified in English."
    )

    models_to_try = [GEMINI_MODEL] + GEMINI_FALLBACK_MODELS
    for model in models_to_try:
        for attempt in range(3):
            try:
                response = client.models.generate_content(
                    model=model,
                    contents=[prompt, image_part],
                    config=genai.types.GenerateContentConfig(
                        max_output_tokens=1024,
                        thinking_config=genai.types.ThinkingConfig(thinking_budget=0),
                    ),
                )
                data = _extract_json(response.text)
                if data and data.get("type") in ("food", "drink"):
                    return data
            except Exception as e:
                if "503" in str(e) or "UNAVAILABLE" in str(e):
                    time.sleep(2 ** attempt)
                    continue
                elif "404" in str(e) or "NOT_FOUND" in str(e):
                    break
                else:
                    log.exception("food vision failed")
                    break
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
    # Always derive meal type from the actual log time.
    meal_type = _infer_meal_type(now)

    interval, _ = _interval_now()

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


def _interval_now() -> tuple[dict, datetime]:
    """Build a 1-minute interval ending now, with local UTC offset."""
    from datetime import timedelta
    now = datetime.now(TZ)
    end_dt = now.astimezone(timezone.utc)
    start_dt = end_dt - timedelta(minutes=1)
    offset_seconds = int(now.utcoffset().total_seconds()) if now.utcoffset() else 0
    utc_offset = f"{offset_seconds}s"
    interval = {
        "startTime": start_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "endTime": end_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "startUtcOffset": utc_offset,
        "endUtcOffset": utc_offset,
    }
    return interval, now


def _build_hydration_datapoint(analysis: dict) -> dict:
    """Build a Google Health HydrationLog DataPoint (volume in milliliters)."""
    interval, _ = _interval_now()
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
    now = datetime.now(TZ)
    data_point = _build_nutrition_datapoint(analysis, now)

    user = db.get_user(user_id)
    token_json = (user.get("google_token_json") if user else None) or None
    if not token_json:
        log.warning("user %s has no Google token — skipping nutrition write", user_id)
        return False
    try:
        client = HealthClient(token_json=token_json)
        client.create_data_point("nutrition-log", data_point)
        log.info("logged nutrition to Google Health: %s",
                 analysis.get("food_name_en") or analysis.get("food_name_local"))
        return True
    except HealthAPIError as e:
        log.error("failed to write nutrition-log to Google Health: %s", e)
        return False


def log_hydration_to_health(user_id: str, analysis: dict) -> bool:
    """Write the analyzed drink to Google Health as a hydration-log data point."""
    data_point = _build_hydration_datapoint(analysis)
    user = db.get_user(user_id)
    token_json = (user.get("google_token_json") if user else None) or None
    if not token_json:
        log.warning("user %s has no Google token — skipping hydration write", user_id)
        return False
    try:
        client = HealthClient(token_json=token_json)
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
        user = db.get_user(user_id)
        token_json = (user.get("google_token_json") if user else None) or None
        client = HealthClient(token_json=token_json)
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
        "synced_food": "✅ Logged to Google Health",
        "synced_drink": "✅ Hydration logged to Google Health",
        "not_synced": "⚠️ Analyzed, but couldn't save to Google Health",
        "low_conf": "(Estimate may be off — try a clearer photo for better accuracy)",
        "empty_drink": "🥤 This looks like an empty container, so I didn't log any hydration. Send a photo with a drink in it and I'll track it!",
        "empty_food": "🍽️ I couldn't estimate a real portion here, so nothing was logged. Try a clearer photo of the food.",
    },
    "th": {
        "unclear": "🤔 ผมดูรูปนี้แล้วไม่แน่ใจว่าเป็นอาหารหรือเครื่องดื่ม ลองถ่ายให้ชัดขึ้นอีกนิดได้ไหมครับ?",
        "energy": "🔥 พลังงาน",
        "protein": "💪 โปรตีน",
        "carbs": "🍞 คาร์บ",
        "fat": "🥑 ไขมัน",
        "volume": "🥤 ปริมาณ",
        "synced_food": "✅ บันทึกลง Google Health เรียบร้อยแล้ว",
        "synced_drink": "✅ บันทึกการดื่มน้ำลง Google Health แล้ว",
        "not_synced": "⚠️ วิเคราะห์สำเร็จ แต่ยังบันทึกลง Google Health ไม่ได้",
        "low_conf": "(ค่าประมาณอาจคลาดเคลื่อน ลองถ่ายชัด ๆ อีกครั้ง)",
        "empty_drink": "🥤 ดูเหมือนแก้ว/ขวดจะว่างเปล่า ผมเลยยังไม่ได้บันทึกนะครับ ถ้ามีน้ำอยู่ในภาพ ส่งมาใหม่ได้เลยครับ",
        "empty_food": "🍽️ ผมประเมินปริมาณอาหารไม่ได้ เลยยังไม่บันทึกครับ ลองถ่ายอาหารให้ชัดขึ้นอีกนิดนะครับ",
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

    analysis = analyze_food_image(user_id, image_bytes, mime_type, language=language)
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

    lines = [
        f"💧 {name}",
        "",
        f"{labels['volume']}: 「{ml} ml」",
    ]
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
