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
from coach.flex import FlexReply, build_log_bubble, COLOR_FOOD, COLOR_DRINK
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
  "coaching_suggestion": "REQUIRED, never omit or leave empty — see rules below",
  "calories_kcal": number,
  "protein_g": number,
  "total_carbohydrate_g": number,
  "total_fat_g": number,
  "volume_ml": number or null,
  "notes": "one short sentence on assumptions (portion size, ingredients)"
}

If it's a DRINK (water bottle, glass, cup, etc.), use this shape:
{
  "type": "drink",
  "drink_name_en": "short drink name in ENGLISH (e.g. 'water bottle', 'iced coffee')",
  "drink_name_local": "the same name in the user's language",
  "confidence": "high | medium | low",
  "coaching_suggestion": "REQUIRED, never omit or leave empty — see rules below",
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

Anything DRINKABLE is a DRINK, even when it is thick, blended, or a meal in
itself: smoothies, protein shakes, blended juices, milk, yoghurt drinks. Use
"type": "drink" and fill "volume_ml" — the drink shape records BOTH the fluid
and its nutrition, so nothing is lost. Classifying a smoothie as "food" throws
away the fluid: it is what made a 350 kcal Boost smoothie count for 0 ml of the
day's hydration.
If you do return "type": "food" for something that still contains a real
quantity of liquid (soup, congee, a bowl of broth), set "volume_ml" to the
drinkable portion so it is counted as fluid too. Leave it null for solid food.

"coaching_suggestion" rules — this field is REQUIRED in every response, food or
drink, and must never be an empty string:
- You'll be given the user's totals already logged today (before this item) and
  their daily targets. Use those REAL numbers, plus this item's own estimated
  nutrition, to decide what's most useful to say next.
- Display names ("*_name_en" / "*_name_local") must START WITH A CAPITAL LETTER
  and read like a menu item in Title Case — "Aburi Salmon Sushi", not "aburi
  salmon sushi"; "Iced Green Tea", not "iced green tea". Google Health shows the
  English name verbatim.
- This text appears on a card that doesn't otherwise show which item was just
  logged, so name the item. Format it as ONE natural sentence, ALWAYS starting
  with ✨ (this exact emoji, never ⭐/💡/🎉/💧 or any other): "✨ <item name>
  <stat label>「<value> <unit>」<descriptive coaching sentence>", e.g.
  "✨ น้ำเปล่า พลังงาน「0 kcal」ช่วยเติมความสดชื่นและรักษาสมดุลน้ำในร่างกายได้อย่างดีครับ"
  or "✨ ข้าวผัด โปรตีน「22 g」เหลืออีก 40g วันนี้ ลองเติมไข่ต้มหรืออกไก่มื้อถัดไปนะครับ".
  Pick whichever single stat (energy, or the nutrient furthest behind target)
  is most useful to name.
- Write a real, specific, encouraging sentence — NEVER a bare number with no
  context like "เหลืออีก 195ml"; always say why it matters or what to do next,
  the way the examples do. Not generic filler like "eat healthy" either.
- If a nutrient is clearly behind target, suggest one concrete food/drink for
  later today. If all targets are basically met, congratulate instead of
  naming a gap.
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


# Localized meal-slot labels for the log card tag line.
_MEAL_LABEL = {
    "en": {"BREAKFAST": "☀️ Breakfast", "LUNCH": "🍱 Lunch",
            "DINNER": "🌙 Dinner", "SNACK": "🍿 Snack"},
    "th": {"BREAKFAST": "☀️ มื้อเช้า", "LUNCH": "🍱 มื้อเที่ยง",
            "DINNER": "🌙 มื้อเย็น", "SNACK": "🍿 ของว่าง"},
}


def _meal_label_for(analysis: dict, lang: str) -> str | None:
    """Extract a displayable meal-slot label from the analysis."""
    meal_type = _explicit_meal_type(analysis)
    return _MEAL_LABEL.get(lang, _MEAL_LABEL["en"]).get(meal_type) if meal_type else None


def _today_nutrition_totals(user_id: str) -> dict:
    """Today's nutrition + hydration totals from Google Health, which aggregates
    every app that writes there — so food logged outside the coach still counts.

    That breadth is also the risk: the totals are only as trustworthy as the
    worst writer on the device. A Health Connect bridge
    (nl.appyhapps.healthsync) once re-imported this user's meals 19,673 times
    and the progress card read 820,981 kcal against a 3,200 target. Points from
    another client cannot even be deleted from here
    (DATA_POINT_NOT_OWNED_BY_CLIENT), so our own logged history is used as a
    check on it: a total past a sane multiple of the user's target is rejected
    outright, a milder mismatch is logged for the owner to investigate, and each
    metric finally takes the HIGHER of the two sources so an entry deleted or
    lost outside the coach can't make the card under-report.

    Returns {"kcal": int, "protein_g": int, "fat_g": int, "carbs_g": int, "water_ml": int}.
    """
    tz = db.user_tz(db.get_user(user_id))
    local = _local_today_totals(user_id, tz)

    today = datetime.now(tz).date()
    tomorrow = today + timedelta(days=1)
    external = {"kcal": 0, "protein_g": 0, "fat_g": 0, "carbs_g": 0, "water_ml": 0}
    try:
        client = client_for_user(user_id)
        for pt in client.daily_rollup("nutrition-log", today.isoformat(), tomorrow.isoformat()):
            nl = pt.get("nutritionLog", {})
            external["kcal"] += round(float(nl.get("energy", {}).get("kcalSum", 0)))
            external["carbs_g"] += round(float(nl.get("totalCarbohydrate", {}).get("gramsSum", 0)))
            external["fat_g"] += round(float(nl.get("totalFat", {}).get("gramsSum", 0)))
            for n in nl.get("nutrients", []):
                if n.get("nutrient") == "PROTEIN":
                    external["protein_g"] += round(float(n.get("quantity", {}).get("gramsSum", 0)))
        for pt in client.daily_rollup("hydration-log", today.isoformat(), tomorrow.isoformat()):
            hl = pt.get("hydrationLog", {})
            external["water_ml"] += round(float(hl.get("amountConsumed", {}).get("millilitersSum", 0)))
    except Exception:
        log.warning("Google Health rollup failed — falling back to our own logged history")
        return local

    implausible = _implausible_totals(user_id, external)
    if implausible:
        targets = _get_daily_targets(user_id)
        log.warning(
            "ignoring implausible Google Health totals (%s) — another app is "
            "duplicating entries; reporting our own history instead.",
            ", ".join(f"{k}={external[k]} vs target {targets.get(k)}" for k in implausible),
        )
        return local

    _warn_if_double_counted(external, local)

    # Per metric, take whichever source is higher. Google Health holds our own
    # writes PLUS anything logged in another app, so it should never read LOWER
    # than our log — when it does, the entry was removed outside the coach (a
    # Health Connect wipe deletes every app's data, ours included) or a write
    # silently failed. Our own history is the part we know happened, so it acts
    # as the floor rather than letting the card drop to zero for meals the user
    # definitely logged.
    merged = {k: max(external.get(k, 0), local.get(k, 0)) for k in local}
    below = [k for k in local if local[k] > external.get(k, 0)]
    if below:
        log.info("Google Health reads lower than our own log for %s — using our "
                 "figures as the floor", ", ".join(below))
    return merged


# Google Health should read at least as high as our own log (it contains ours
# plus anything logged elsewhere), but not a multiple of it. Past this ratio a
# mirror app is the likelier explanation than a big meal logged in another app.
_DUPLICATION_RATIO = 1.8


def _warn_if_double_counted(external: dict, local: dict) -> None:
    """Flag the case the implausibility guard is too coarse to catch.

    A bridge app that mirrors our entries 1:1 merely DOUBLES each total, which
    stays far below the 10x-of-target guard yet still misreports the day. This
    only logs — the user may genuinely have logged food in another app, so it
    must not silently change the number — but it leaves a trail naming the
    suspicion instead of quietly reporting inflated totals.
    """
    for key in ("kcal", "water_ml"):
        ours, theirs = local.get(key, 0), external.get(key, 0)
        if ours > 0 and theirs >= ours * _DUPLICATION_RATIO:
            log.warning(
                "Google Health reports %s=%s but the coach logged only %s today "
                "(%.1fx) — either food was logged in another app, or an app is "
                "duplicating our entries back into Google Health.",
                key, theirs, ours, theirs / ours,
            )


# A rollup this far past the user's own target isn't a big eating day, it's
# duplicated data. 10x leaves room for a genuinely loose target.
_IMPLAUSIBLE_FACTOR = 10


def _implausible_totals(user_id: str, totals: dict) -> list[str]:
    """Keys whose total exceeds any sane multiple of the user's own target."""
    targets = _get_daily_targets(user_id)
    return [key for key, target in targets.items()
            if target and totals.get(key, 0) > target * _IMPLAUSIBLE_FACTOR]


def _local_today_totals(user_id: str, tz) -> dict:
    """Today's totals from our own logged history (insights.food_log)."""
    totals = {"kcal": 0, "protein_g": 0, "fat_g": 0, "carbs_g": 0, "water_ml": 0}
    today_start = datetime.now(tz).replace(hour=0, minute=0, second=0, microsecond=0)
    cutoff_utc = today_start.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT content FROM insights WHERE user_id = ? AND kind = 'food_log' AND ts >= ?",
            (user_id, cutoff_utc),
        ).fetchall()
    for row in rows:
        try:
            c = json.loads(row["content"])
        except (json.JSONDecodeError, ValueError):
            continue
        totals["kcal"] += round(_num(c.get("calories_kcal")))
        totals["protein_g"] += round(_num(c.get("protein_g")))
        totals["fat_g"] += round(_num(c.get("total_fat_g")))
        totals["carbs_g"] += round(_num(c.get("total_carbohydrate_g")))
        # Any entry carrying a volume counts as fluid, not just type=="drink" —
        # a smoothie logged as food is still something the user drank.
        totals["water_ml"] += round(_num(c.get("volume_ml")))
    return totals


# Default daily nutrition targets (user can override via chat "ตั้งเป้า ...")
_DEFAULT_TARGETS = {
    "kcal": 2000,
    "protein_g": 120,
    "fat_g": 65,
    "carbs_g": 250,
    "water_ml": 2000,
}


def _get_daily_targets(user_id: str) -> dict:
    """Load the user's daily nutrition targets from the goals table.
    Falls back to _DEFAULT_TARGETS for any missing keys."""
    targets = dict(_DEFAULT_TARGETS)
    with db.connect() as conn:
        row = conn.execute(
            "SELECT value_json FROM goals WHERE user_id = ? AND key = 'daily_nutrition_targets'",
            (user_id,),
        ).fetchone()
    if row:
        try:
            user_targets = json.loads(row["value_json"])
            for k in targets:
                if k in user_targets and user_targets[k]:
                    targets[k] = int(float(user_targets[k]))
        except (json.JSONDecodeError, ValueError):
            pass
    return targets


def get_daily_progress(user_id: str) -> dict:
    """Get today's progress: current totals vs targets.

    Returns {"current": {...}, "targets": {...}, "lang": "th"|"en"}.
    """
    return {
        "current": _today_nutrition_totals(user_id),
        "targets": _get_daily_targets(user_id),
        "lang": _lang_code(_get_language(user_id)),
    }


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

    current = _today_nutrition_totals(user_id)
    targets = _get_daily_targets(user_id)
    totals_line = ", ".join(f"{k}: {current.get(k, 0)}/{v}" for k, v in targets.items())

    # The '*_en' name must be English (used for the Google Health log); the
    # '*_local' name, 'notes', and 'coaching_suggestion' must be in the user's
    # language (for the reply).
    prompt = FOOD_VISION_PROMPT + (
        f"\n\nThe user's language is {language}. Write '*_local' fields, 'notes', and "
        f"'coaching_suggestion' in {language}. Always keep '*_en' fields in English, "
        "and keep all JSON keys and the 'type' value exactly as specified in English.\n\n"
        f"Already logged today (before this item), vs daily target: {totals_line}"
    )

    # Bound the upload. Ordinary LINE photos (<=1.64 MP) are unaffected; this
    # exists so a full-resolution photo arriving through another path can't turn
    # one meal into a multi-megabyte request.
    from coach.images import VISION_MAX_EDGE, downscale
    image_bytes = downscale(image_bytes, VISION_MAX_EDGE, mime_type)
    image_part = genai.types.Part.from_bytes(data=image_bytes, mime_type=mime_type)
    try:
        # Shorter budget than scheduled jobs — a person is waiting in chat.
        text = gemini.generate(
            api_key, contents=[prompt, image_part],
            max_output_tokens=4096, max_wait=180, prefer_accuracy=True,
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
        if not data.get("coaching_suggestion"):
            log.warning("vision response missing coaching_suggestion (type=%s)", data.get("type"))
        return _normalize_names(data)
    return None


_NAME_FIELDS = ("food_name_en", "food_name_local", "drink_name_en", "drink_name_local")


def _capitalize_first(name: str) -> str:
    """Uppercase the first letter, leaving the rest untouched.

    23% of logged names arrived lower-case ("aburi salmon sushi", "water",
    "mate tea") because the prompt only ever asked for brand names to be
    capitalised. Google Health shows this string verbatim, so the entry looked
    sloppy next to the ones that happened to come back title-cased.

    Deliberately NOT .title(): that rewrites the whole string and would turn
    "McDonald's" into "Mcdonald'S" and "iced tea, 7-Eleven" into something
    worse. Only an ASCII lower-case first character is touched, so Thai (which
    has no case) and names already starting with a brand or digit are left
    exactly as the model wrote them.
    """
    if not name:
        return name
    first = name[0]
    return (first.upper() + name[1:]) if "a" <= first <= "z" else name


def _normalize_names(analysis: dict) -> dict:
    """Capitalise every display-name field in place. Returns the same dict."""
    if isinstance(analysis, dict):
        for field in _NAME_FIELDS:
            value = analysis.get(field)
            if isinstance(value, str):
                analysis[field] = _capitalize_first(value.strip())
    return analysis


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
        # Last line of defence: adjustments and a caloric drink's nutrition twin
        # build their own dict, so normalise here too rather than trusting every
        # caller to have done it.
        "foodDisplayName": _capitalize_first(food_name_en.strip())[:100],
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


def log_food_to_health(user_id: str, analysis: dict) -> tuple[bool, str | None]:
    """Write the analyzed meal to Google Health as a nutrition-log data point.

    Returns (success, resource_name). The resource name is stored with the
    log so a later targeted delete removes exactly this point.
    """
    # Log time follows the USER's local clock, unless the analysis carries an
    # explicit time or a named meal (chat logs: "log my breakfast ...").
    now = _resolve_log_time(analysis, db.user_tz(db.get_user(user_id)))
    data_point = _build_nutrition_datapoint(analysis, now)

    try:
        client = client_for_user(user_id)
        created = client.create_data_point("nutrition-log", data_point)
        log.info("logged nutrition to Google Health: %s",
                 analysis.get("food_name_en") or analysis.get("food_name_local"))
        return True, (created or {}).get("name")
    except HealthAPIError as e:
        log.error("failed to write nutrition-log to Google Health: %s", e)
        return False, None
    except Exception:
        # Anything other than HealthAPIError (token refresh errors, etc.) must
        # still resolve to a clean (False, None) — letting it propagate means
        # the caller's generic except falls back to a bare, uninformative
        # "⚠️" instead of the real "couldn't save" message (see chat.py).
        log.exception("unexpected error writing nutrition-log to Google Health")
        return False, None


def log_hydration_to_health(user_id: str, analysis: dict) -> tuple[bool, str | None]:
    """Write the analyzed drink to Google Health as a hydration-log data point.

    Returns (success, resource_name), like log_food_to_health.
    """
    data_point = _build_hydration_datapoint(analysis, db.user_tz(db.get_user(user_id)))
    try:
        client = client_for_user(user_id)
        created = client.create_data_point("hydration-log", data_point)
        log.info("logged hydration to Google Health: %s ml", analysis.get("volume_ml"))
        return True, (created or {}).get("name")
    except HealthAPIError as e:
        log.error("failed to write hydration-log to Google Health: %s", e)
        return False, None
    except Exception:
        log.exception("unexpected error writing hydration-log to Google Health")
        return False, None


def _log_fluid_for_food(user_id: str, analysis: dict) -> str | None:
    """Also record a FOOD entry's liquid portion as fluid intake.

    The drink path has always written both a hydration point and (when caloric)
    a nutrition point. The food path wrote nutrition only, so anything drinkable
    that the model happened to classify as "food" lost its volume entirely: a
    350 kcal Boost smoothie logged 0 ml of hydration, and protein shakes were
    stored with the volume sitting in the display name ("เวย์โปรตีน (300 มล.)")
    where nothing could read it. Returns the hydration point name, or None when
    there is no liquid to record.
    """
    if _num(analysis.get("volume_ml")) <= 0:
        return None
    synced, point = log_hydration_to_health(user_id, analysis)
    if synced:
        log.info("also logged %s ml of fluid for a food entry", analysis.get("volume_ml"))
    return point


def _store_food_log(user_id: str, analysis: dict, synced: bool) -> int:
    """Record the food log locally (for history + weekly reports).

    Returns the insights rowid, so callers can map the LINE confirmation
    message to this log for quote-reply targeting.
    """
    with db.connect() as conn:
        cur = conn.execute(
            "INSERT INTO insights (user_id, ts, kind, content, delivered) VALUES (?, datetime('now'), 'food_log', ?, ?)",
            (user_id, json.dumps({**analysis, "synced_to_health": synced}), 1),
        )
        return cur.lastrowid


def log_chat_entry(user_id: str, kind: str, analysis: dict | None) -> tuple[str | FlexReply, int | None]:
    """Log a food/drink the user DESCRIBED in chat (no photo).

    `analysis` is the JSON the chat model emitted in a [LOG_FOOD]/[LOG_DRINK]
    directive — same shape as the vision output, plus optional meal_type /
    time / date fields. Returns (reply, insights_rowid_or_None): reply is a
    FlexReply (log confirmation card, no hero image since there's no photo)
    when a real log was created, or a plain str status line for
    apology/error cases. The caller sends FlexReply as its own message and
    a plain str appended to the conversational reply text. The rowid lets
    the caller map the sent message for quote-replies.
    """
    db.init_db()
    labels = LABELS.get(_lang_code(_get_language(user_id)), LABELS["en"])

    if not isinstance(analysis, dict):
        return labels["not_synced"], None
    _normalize_names(analysis)

    # The model writes these as JSON numbers, but be lenient ("450" etc.)
    for field in ("calories_kcal", "protein_g", "total_carbohydrate_g",
                  "total_fat_g", "volume_ml", "container_count"):
        if field in analysis:
            analysis[field] = _num(analysis[field])

    if kind == "drink":
        if round(analysis.get("volume_ml") or 0) <= 0:
            return labels["empty_drink"], None
        synced_hydration, hydration_point = log_hydration_to_health(user_id, analysis)
        # Caloric drinks also count as nutrition, mirroring the photo flow.
        synced_nutrition, nutrition_point = False, None
        if round(analysis.get("calories_kcal") or 0) > 10:
            synced_nutrition, nutrition_point = log_food_to_health(user_id, {
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
        rowid = _store_food_log(
            user_id,
            {**analysis, "type": "drink", "source": "chat",
             "health_point_names": [n for n in (hydration_point, nutrition_point) if n]},
            synced_hydration,
        )
        sync_label = labels["synced"] if (synced_hydration or synced_nutrition) else labels["not_synced"]

        name = analysis.get("drink_name_local") or analysis.get("drink_name_en") or "drink"
        ml = round(float(analysis.get("volume_ml") or 0))
        cal = round(float(analysis.get("calories_kcal") or 0))
        protein = round(float(analysis.get("protein_g") or 0))
        carbs = round(float(analysis.get("total_carbohydrate_g") or 0))
        fat = round(float(analysis.get("total_fat_g") or 0))
        count = int(float(analysis.get("container_count") or 1))

        rows = []
        if count > 1:
            rows.append((labels["containers"], str(count)))
        if cal > 0:
            rows.append((labels["energy"], f"{cal} kcal"))
        if protein > 0:
            rows.append((labels["protein"], f"{protein} g"))
        if carbs > 0:
            rows.append((labels["carbs"], f"{carbs} g"))
        if fat > 0:
            rows.append((labels["fat"], f"{fat} g"))

        bubble = build_log_bubble(
            name=name, kicker=labels["kicker_drink"], accent_color=COLOR_DRINK,
            highlight=("🥤", f"{ml} ml"), rows=rows, notes=analysis.get("notes"),
            synced=synced_hydration or synced_nutrition, sync_label=sync_label,
            coaching_note=analysis.get("coaching_suggestion"),
        )
        return FlexReply(f"💧 {name}", bubble), rowid

    if round(analysis.get("calories_kcal") or 0) <= 0:
        return labels["empty_food"], None
    synced, point_name = log_food_to_health(user_id, analysis)
    fluid_point = _log_fluid_for_food(user_id, analysis)
    rowid = _store_food_log(
        user_id,
        {**analysis, "type": "food", "source": "chat",
         "health_point_names": [n for n in (point_name, fluid_point) if n]},
        synced,
    )

    name = analysis.get("food_name_local") or analysis.get("food_name_en") or "meal"
    cal = round(float(analysis.get("calories_kcal") or 0))
    protein = round(float(analysis.get("protein_g") or 0))
    carbs = round(float(analysis.get("total_carbohydrate_g") or 0))
    fat = round(float(analysis.get("total_fat_g") or 0))
    rows = [
        (labels["protein"], f"{protein} g"),
        (labels["carbs"], f"{carbs} g"),
        (labels["fat"], f"{fat} g"),
    ]
    bubble = build_log_bubble(
        name=name, kicker=labels["kicker_food"], accent_color=COLOR_FOOD,
        highlight=("🔥", f"{cal} kcal"), rows=rows, notes=analysis.get("notes"),
        synced=synced, sync_label=labels["synced"] if synced else labels["not_synced"],
        coaching_note=analysis.get("coaching_suggestion"),
    )
    return FlexReply(f"🍽️ {name} — {cal} kcal", bubble), rowid


def _delete_log_points(user_id: str, content: dict, kind: str) -> bool:
    """Remove the Google Health data points behind a stored log.

    Prefers the exact resource names captured at log time; logs stored before
    names were captured fall back to newest-point deletion (the pre-existing
    behavior). Returns True when Google Health no longer holds the points
    (including when nothing was ever synced).
    """
    names = content.get("health_point_names") or []
    if names:
        by_type: dict[str, list[str]] = {}
        for n in names:
            try:
                dtype = n.split("/dataTypes/")[1].split("/")[0]
            except IndexError:
                continue
            by_type.setdefault(dtype, []).append(n)
        if by_type:
            try:
                client = client_for_user(user_id)
                for dtype, ns in by_type.items():
                    client.batch_delete_data_points(dtype, ns)
                # VERIFY before claiming success. batchDelete returning 200 is
                # not proof: points written by another app survive it. Callers
                # use this return to decide whether to drop the local row
                # (delete) or to re-log a rescaled copy (adjust), so a false
                # True either desynchronizes the two stores or double-counts.
                still = client.data_points_still_exist(names)
                if still:
                    log.error("%d of %d points survived deletion (%s) — refusing "
                              "to report success", len(still), len(names), still[:3])
                    return False
                return True
            except HealthAPIError as e:
                log.error("failed to delete stored points for adjustment: %s", e)
                return False
            except Exception:
                log.exception("unexpected error deleting stored points for adjustment")
                return False
        # Stored names existed but none parsed into a data type — e.g. rows
        # written by a prior bug that captured the API's Operation name
        # instead of the created DataPoint's name. Silently reporting success
        # here would leave the original point untouched while a new one gets
        # created. Fall through to the newest-point fallback below instead.
        log.warning(
            "health_point_names present but unparseable (%r) — falling back "
            "to newest-point deletion", names,
        )

    if not content.get("synced_to_health"):
        return True  # nothing in Google Health to remove

    # Legacy row without stored names: newest-point deletion, like before.
    if delete_last_log(user_id, "drink" if kind == "drink" else "food") is None:
        return False
    if kind == "drink" and _num(content.get("calories_kcal")) > 10:
        delete_last_log(user_id, "food")  # caloric drink's nutrition twin, best-effort
    elif kind == "food" and _num(content.get("volume_ml")) > 0:
        delete_last_log(user_id, "drink")  # liquid food's hydration twin, best-effort
    return True


def delete_log(user_id: str, insight_rowid: int) -> str | None:
    """Delete a SPECIFIC stored log (resolved from a quote-reply): its Google
    Health point(s) and the local insights row. Returns a display label of
    what was removed, or None on failure/not found."""
    db.init_db()
    with db.connect() as conn:
        row = conn.execute(
            "SELECT rowid, content FROM insights "
            "WHERE user_id = ? AND kind = 'food_log' AND rowid = ?",
            (user_id, insight_rowid),
        ).fetchone()
    if not row:
        return None
    try:
        content = json.loads(row["content"])
    except (json.JSONDecodeError, ValueError):
        return None
    kind = "drink" if content.get("type") == "drink" else "food"

    if not _delete_log_points(user_id, content, kind):
        return None

    with db.connect() as conn:
        conn.execute("DELETE FROM insights WHERE rowid = ?", (row["rowid"],))
        conn.execute("DELETE FROM log_messages WHERE insight_rowid = ?", (row["rowid"],))

    if kind == "drink":
        ml = _num(content.get("volume_ml"))
        return (content.get("drink_name_local") or content.get("drink_name_en")
                or (f"{round(ml)} ml" if ml else "drink"))
    return (content.get("food_name_local") or content.get("food_name_en") or "meal")


def _foreign_app(point: dict) -> str | None:
    """The Android package that wrote this point, or None when we wrote it.

    Google Health refuses to delete anything we did not create:
        403 PERMISSION_DENIED / DATA_POINT_NOT_OWNED_BY_CLIENT
    and it rejects the WHOLE batchDelete request, so a single foreign name mixed
    into the list stops our own entries from being deleted too. Our writes go
    through the web client and carry no application.packageName; another app's
    do. Filter on that before asking to delete anything.
    """
    app = (point.get("dataSource", {}).get("application") or {})
    return app.get("packageName")


def delete_today_logs(user_id: str, kind: str = "all") -> str:
    """Delete ALL nutrition/hydration entries for the user's current local
    date — every Google Health point with today's civil date (including ones
    logged before local tracking existed) plus the local history rows.

    kind: 'food' | 'drink' | 'all'. Returns a localized status line.
    """
    db.init_db()
    labels = LABELS.get(_lang_code(_get_language(user_id)), LABELS["en"])

    tz = db.user_tz(db.get_user(user_id))
    now_local = datetime.now(tz)
    start = now_local.date().isoformat()
    end = (now_local.date() + timedelta(days=1)).isoformat()

    data_types = []
    if kind in ("food", "all"):
        data_types.append("nutrition-log")
    if kind in ("drink", "all"):
        data_types.append("hydration-log")

    counts = {"nutrition-log": 0, "hydration-log": 0}
    leftover: dict[str, int] = {}
    leftover_apps: set[str] = set()
    try:
        client = client_for_user(user_id)
        for data_type in data_types:
            field = data_type.replace("-", "_")
            filter_str = (
                f'{field}.interval.civil_start_time >= "{start}" '
                f'AND {field}.interval.civil_start_time < "{end}"'
            )
            points = client.list_points(data_type, filter_str)
            # Only our own points — including a foreign one makes Google Health
            # reject the entire request (DATA_POINT_NOT_OWNED_BY_CLIENT), which
            # is why "delete all of today" removed nothing at all once a mirror
            # app had added entries to the same day.
            names = [p["name"] for p in points if p.get("name") and not _foreign_app(p)]
            foreign = [p for p in points if _foreign_app(p)]
            if names:
                client.batch_delete_data_points(data_type, names)
            # VERIFY. This used to report len(names) — what it TRIED to delete —
            # as the number removed, so the user was told "cleared N meals"
            # whether or not anything actually went away. Re-read the day and
            # count what really disappeared.
            remaining = client.list_points(data_type, filter_str)
            counts[data_type] = max(0, len(points) - len(remaining))
            still_foreign = [p for p in remaining if _foreign_app(p)]
            if remaining:
                leftover[data_type] = len(remaining)
                for p in still_foreign:
                    leftover_apps.add(_foreign_app(p))
                log.warning("%d %s points remain for %s after deleting %d of ours "
                            "(%d belong to other apps: %s)",
                            len(remaining), data_type, user_id, counts[data_type],
                            len(still_foreign), ", ".join(sorted(leftover_apps)) or "unknown")
            if foreign:
                log.info("skipped %d foreign %s points (not deletable by us)",
                         len(foreign), data_type)
    except HealthAPIError as e:
        # Don't clear local history if Google Health still holds the points —
        # the two stores must not diverge.
        log.error("failed to clear today's %s logs: %s", kind, e)
        return labels["delete_failed"]
    except Exception:
        log.exception("unexpected error clearing today's %s logs", kind)
        return labels["delete_failed"]

    # Clear matching local history rows for today (user-local midnight, UTC ts)
    cutoff = (now_local.replace(hour=0, minute=0, second=0, microsecond=0)
              .astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"))
    removed_local = 0
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT rowid, content FROM insights "
            "WHERE user_id = ? AND kind = 'food_log' AND ts >= ?",
            (user_id, cutoff),
        ).fetchall()
        for row in rows:
            try:
                row_kind = "drink" if json.loads(row["content"]).get("type") == "drink" else "food"
            except (json.JSONDecodeError, ValueError):
                row_kind = "food"
            if kind != "all" and row_kind != kind:
                continue
            conn.execute("DELETE FROM insights WHERE rowid = ?", (row["rowid"],))
            conn.execute("DELETE FROM log_messages WHERE insight_rowid = ?", (row["rowid"],))
            removed_local += 1

    total = counts["nutrition-log"] + counts["hydration-log"]
    if total == 0 and removed_local == 0 and not leftover:
        return labels["nothing_today"]
    parts = []
    if kind in ("food", "all"):
        parts.append(labels["deleted_meals"].format(n=counts["nutrition-log"]))
    if kind in ("drink", "all"):
        parts.append(labels["deleted_drinks"].format(n=counts["hydration-log"]))
    log.info("cleared today's logs for %s: %s (local rows: %d, leftover: %s)",
             user_id, counts, removed_local, leftover or "none")
    # Nothing removed but entries remain: lead with why, not with "cleared 0".
    status = ("🗑️ " + labels["deleted_today"] + " " + " / ".join(parts)
              if total or not leftover else "")
    if leftover:
        # Entries we could not remove are almost always written by another app
        # that mirrors data into Google Health — deleting them here is either
        # refused or instantly undone by that app's next sync, so say so plainly
        # instead of implying the day is clean.
        status += ("\n\n" if status else "") + labels["delete_leftover"].format(
            n=sum(leftover.values()),
            apps=", ".join(sorted(leftover_apps)) or "?",
        )
    return status


def delete_newest_log(user_id: str, kind: str) -> str | None:
    """Delete the newest stored log of the given kind ('food'|'drink').

    Falls back to raw newest-point deletion in Google Health when no local
    row exists (e.g. logs made before local history was kept)."""
    db.init_db()
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT rowid, content FROM insights "
            "WHERE user_id = ? AND kind = 'food_log' "
            "ORDER BY ts DESC, rowid DESC LIMIT 10",
            (user_id,),
        ).fetchall()
    for row in rows:
        try:
            content = json.loads(row["content"])
        except (json.JSONDecodeError, ValueError):
            continue
        if ("drink" if content.get("type") == "drink" else "food") == kind:
            return delete_log(user_id, row["rowid"])
    return delete_last_log(user_id, kind)


def adjust_last_log(user_id: str, params: dict | None,
                    insight_rowid: int | None = None) -> tuple[str, int | None]:
    """Rescale a food/drink log ("I actually had 4 of those", "กินไปแล้ว 4
    รอบ", "only drank half").

    Returns (status_line, adjusted_rowid_or_None) — the rowid lets the caller
    map the confirmation message, so quoting IT adjusts the same log again.

    params: {"kind": "food"|"drink" (optional), "times": N} where times is the
    TOTAL multiple of the originally logged amount. insight_rowid pins the
    exact log (resolved from a LINE quote-reply); otherwise the newest log
    whose type matches params["kind"] is used, falling back to the newest of
    any type. Deletes the original Google Health point(s), re-logs the scaled
    totals anchored at the ORIGINAL log time, and updates the stored insights
    row in place (so history and weekly reports don't double-count). Returns
    a localized status line.
    """
    db.init_db()
    labels = LABELS.get(_lang_code(_get_language(user_id)), LABELS["en"])

    times = _num((params or {}).get("times"))
    if not isinstance(params, dict) or times <= 0:
        return labels["not_synced"], None

    with db.connect() as conn:
        if insight_rowid is not None:
            rows = conn.execute(
                "SELECT rowid, ts, content FROM insights "
                "WHERE user_id = ? AND kind = 'food_log' AND rowid = ?",
                (user_id, insight_rowid),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT rowid, ts, content FROM insights "
                "WHERE user_id = ? AND kind = 'food_log' "
                "ORDER BY ts DESC, rowid DESC LIMIT 10",
                (user_id,),
            ).fetchall()
    parsed = []
    for r in rows:
        try:
            parsed.append((r, json.loads(r["content"])))
        except (json.JSONDecodeError, ValueError):
            continue
    if not parsed:
        return labels["no_recent_log"], None

    # Prefer the newest log matching the kind the model asked for ("drank 4x"
    # should never grab a meal), but fall back to the newest of any type.
    want = str(params.get("kind") or "").lower()
    row, original = parsed[0]
    if insight_rowid is None and want in ("food", "drink"):
        for r, a in parsed:
            if (a.get("type") or "food") == want:
                row, original = r, a
                break

    # The stored log's own type decides which Google Health data points to
    # touch — never the model's guess, or a wrong guess deletes wrong data.
    kind = "drink" if original.get("type") == "drink" else "food"

    scaled = dict(original)
    for field in ("calories_kcal", "protein_g", "total_carbohydrate_g",
                  "total_fat_g", "volume_ml", "container_count"):
        if original.get(field) is not None:
            scaled[field] = round(_num(original.get(field)) * times, 1)
    scaled["times"] = times

    # Re-log anchored at the ORIGINAL log time (insights.ts is UTC), so the
    # entry doesn't jump to "now" on the Google Health timeline.
    try:
        ts_utc = datetime.strptime(row["ts"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        local = ts_utc.astimezone(db.user_tz(db.get_user(user_id)))
        scaled["date"] = local.date().isoformat()
        scaled["time"] = local.strftime("%H:%M")
    except (ValueError, TypeError):
        pass

    # Remove the original Google Health point(s) first — refuse to re-log if
    # they can't be removed (double-counting is worse than a failed adjustment).
    if not _delete_log_points(user_id, original, kind):
        return labels["not_synced"], None

    new_points: list[str] = []
    if kind == "drink":
        synced, hydration_point = log_hydration_to_health(user_id, scaled)
        if hydration_point:
            new_points.append(hydration_point)
        if synced and _num(scaled.get("calories_kcal")) > 10:
            _, nutrition_point = log_food_to_health(user_id, {
                "food_name_en": scaled.get("drink_name_en")
                                or scaled.get("drink_name_local") or "drink",
                "calories_kcal": scaled.get("calories_kcal", 0),
                "protein_g": scaled.get("protein_g", 0),
                "total_carbohydrate_g": scaled.get("total_carbohydrate_g", 0),
                "total_fat_g": scaled.get("total_fat_g", 0),
                "meal_type": scaled.get("meal_type"),
                "time": scaled.get("time"),
                "date": scaled.get("date"),
            })
            if nutrition_point:
                new_points.append(nutrition_point)
    else:
        synced, food_point = log_food_to_health(user_id, scaled)
        if food_point:
            new_points.append(food_point)
        # A liquid food (smoothie, soup) carries a volume that was rescaled with
        # everything else; re-log it or the adjustment would silently drop the
        # fluid the original entry recorded.
        if synced:
            fluid_point = _log_fluid_for_food(user_id, scaled)
            if fluid_point:
                new_points.append(fluid_point)
    scaled["health_point_names"] = new_points

    with db.connect() as conn:
        conn.execute(
            "UPDATE insights SET content = ? WHERE rowid = ?",
            (json.dumps({**scaled, "synced_to_health": synced}), row["rowid"]),
        )
    return (labels["synced"] if synced else labels["not_synced"]), row["rowid"]


def delete_last_log(user_id: str, kind: str = "food") -> str | None:
    """Delete the most recent nutrition-log or hydration-log entry from Google Health.

    kind: 'food' -> nutrition-log, 'drink' -> hydration-log.
    Returns the display name of what was deleted, or None if nothing found/failed.
    """
    data_type = "hydration-log" if kind == "drink" else "nutrition-log"
    field = data_type.replace("-", "_")

    # The user's local date, not the server's — a mismatch shifts the search
    # window and can miss the entry that was just logged (or match one from a
    # neighbouring day).
    today = datetime.now(db.user_tz(db.get_user(user_id))).date()
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
    except Exception:
        log.exception("unexpected error listing %s for delete", data_type)
        return None

    # Skip points another app wrote: we are not allowed to delete them
    # (DATA_POINT_NOT_OWNED_BY_CLIENT), and picking one would fail the request
    # while leaving the user's actual newest entry in place.
    points = [p for p in points if not _foreign_app(p)]
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
        if client.data_points_still_exist([name]):
            # Accepted but not applied — typically a point another app owns.
            log.error("%s point %s survived deletion", data_type, name)
            return None
        log.info("deleted %s data point: %s (%s)", data_type, name, label)
        return label
    except HealthAPIError as e:
        log.error("failed to delete %s: %s", data_type, e)
        return None
    except Exception:
        log.exception("unexpected error deleting %s", data_type)
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
        "kicker_food": "🍽️ Nutrition log",
        "kicker_drink": "💧 Hydration log",
        "synced": "✅ Logged",
        "not_synced": "⚠️ Not saved",
        "low_conf": "(Estimate may be off — try a clearer photo for better accuracy)",
        "empty_drink": "🥤 This looks like an empty container, so I didn't log any hydration. Send a photo with a drink in it and I'll track it!",
        "empty_food": "🍽️ I couldn't estimate a real portion here, so nothing was logged. Try a clearer photo of the food.",
        "no_recent_log": "🤔 I couldn't find a recent log to adjust.",
        "delete_failed": "⚠️ Couldn't delete from Google Health — please try again.",
        "nothing_today": "🤷 No logs found for today.",
        "targets_failed": "⚠️ I could not save your new nutrition targets — please state them again (e.g. \"set target 1800 kcal, 150g protein\").",
        "deleted_today": "Cleared today's logs:",
        "delete_leftover": "⚠️ {n} entries could NOT be removed — they were written by another app ({apps}), which keeps re-adding them. Turn off its nutrition/hydration sync, then delete them in that app or in Health Connect.",
        "deleted_meals": "{n} meal(s)",
        "deleted_drinks": "{n} drink(s)",
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
        "kicker_food": "🍽️ บันทึกโภชนาการ",
        "kicker_drink": "💧 บันทึกการดื่มน้ำ",
        "synced": "✅ บันทึกแล้ว",
        "not_synced": "⚠️ บันทึกไม่สำเร็จ",
        "low_conf": "(ค่าประมาณอาจคลาดเคลื่อน ลองถ่ายชัด ๆ อีกครั้ง)",
        "empty_drink": "🥤 ดูเหมือนแก้ว/ขวดจะว่างเปล่า ผมเลยยังไม่ได้บันทึกนะครับ ถ้ามีน้ำอยู่ในภาพ ส่งมาใหม่ได้เลยครับ",
        "empty_food": "🍽️ ผมประเมินปริมาณอาหารไม่ได้ เลยยังไม่บันทึกครับ ลองถ่ายอาหารให้ชัดขึ้นอีกนิดนะครับ",
        "no_recent_log": "🤔 ผมไม่พบรายการที่เพิ่งบันทึกไว้ให้ปรับครับ",
        "delete_failed": "⚠️ ยังลบจาก Google Health ไม่สำเร็จครับ ลองใหม่อีกครั้งนะครับ",
        "nothing_today": "🤷 วันนี้ยังไม่มีรายการที่บันทึกไว้ครับ",
        "targets_failed": "⚠️ ยังบันทึกเป้าโภชนาการใหม่ไม่สำเร็จครับ รบกวนบอกตัวเลขอีกครั้ง (เช่น \"ตั้งเป้า 1800 kcal โปรตีน 150g\")",
        "deleted_today": "ลบรายการของวันนี้แล้ว:",
        "delete_leftover": "⚠️ ยังมี {n} รายการที่ลบไม่ได้ เพราะถูกเขียนโดยแอปอื่น ({apps}) ซึ่งจะเพิ่มกลับมาเรื่อย ๆ กรุณาปิดการซิงค์โภชนาการ/น้ำของแอปนั้น แล้วลบในแอปนั้นหรือใน Health Connect ครับ",
        "deleted_meals": "อาหาร {n} รายการ",
        "deleted_drinks": "เครื่องดื่ม {n} รายการ",
        "ai_busy": "⏳ ตอนนี้ระบบ AI มีผู้ใช้งานเยอะมาก ผมเลยยังวิเคราะห์รูปไม่ได้ครับ อีกสักครู่ลองส่งรูปมาใหม่นะครับ",
        "quota_exhausted": "⛔ คีย์ Gemini ของคุณใช้โควต้าฟรีของวันนี้หมดแล้ว ผมเลยวิเคราะห์รูปไม่ได้ชั่วคราวครับ โควต้าจะรีเซ็ตเที่ยงคืนเวลาแปซิฟิก (ราวบ่าย 2 เวลาไทย)",
    },
}


def handle_food_photo(user_id: str, image_bytes: bytes,
                      mime_type: str = "image/jpeg") -> tuple[str | FlexReply, int | None]:
    """Full flow: analyze image → log to Google Health → return a LINE reply.

    Handles both food (nutrition-log) and drinks (hydration-log).
    Reply language follows the user's stored preference.
    Returns (reply, insights_rowid_or_None) — reply is a FlexReply (a log
    confirmation card, with the analyzed photo as its hero image) when the
    analysis produced a real log, or a plain str for apology/error cases
    (unclear photo, empty portion, AI unavailable). The rowid lets the
    caller map the sent confirmation message for later quote-replies.
    """
    db.init_db()

    language = _get_language(user_id)
    labels = LABELS.get(_lang_code(language), LABELS["en"])

    try:
        analysis = analyze_food_image(user_id, image_bytes, mime_type, language=language)
    except gemini.GeminiQuotaExhausted:
        return labels["quota_exhausted"], None
    except gemini.GeminiUnavailable:
        return labels["ai_busy"], None
    if not analysis or analysis.get("type") not in ("food", "drink"):
        return labels["unclear"], None

    image_url = None
    try:
        from coach.images import save_temp_image, temp_image_url
        image_url = temp_image_url(save_temp_image(image_bytes, mime_type))
    except Exception:
        log.exception("failed to save temp image for flex hero — continuing without it")

    if analysis["type"] == "drink":
        return _handle_drink(user_id, analysis, labels, image_url=image_url)
    return _handle_food(user_id, analysis, labels, image_url=image_url)


def _handle_food(user_id: str, analysis: dict, labels: dict,
                 image_url: str | None = None) -> tuple[str | FlexReply, int | None]:
    cal = round(float(analysis.get("calories_kcal") or 0))

    # Don't log if there's no real portion (e.g. empty plate / not food)
    if cal <= 0:
        log.info("food calories is 0 — skipping nutrition log")
        return labels["empty_food"], None

    synced, point_name = log_food_to_health(user_id, analysis)
    fluid_point = _log_fluid_for_food(user_id, analysis)
    rowid = _store_food_log(
        user_id,
        {**analysis, "health_point_names": [n for n in (point_name, fluid_point) if n]},
        synced,
    )

    # Show the localized name in the reply, English as fallback
    name = analysis.get("food_name_local") or analysis.get("food_name_en") or "meal"
    protein = round(float(analysis.get("protein_g") or 0))
    carbs = round(float(analysis.get("total_carbohydrate_g") or 0))
    fat = round(float(analysis.get("total_fat_g") or 0))
    confidence = analysis.get("confidence", "medium")

    rows = [
        (labels["protein"], f"{protein} g"),
        (labels["carbs"], f"{carbs} g"),
        (labels["fat"], f"{fat} g"),
    ]
    bubble = build_log_bubble(
        name=name, kicker=labels["kicker_food"], accent_color=COLOR_FOOD,
        highlight=("🔥", f"{cal} kcal"), rows=rows,
        notes=analysis.get("notes"),
        synced=synced,
        sync_label=labels["synced"] if synced else labels["not_synced"],
        low_conf_label=labels["low_conf"] if confidence == "low" else None,
        image_url=image_url,
    )
    # Photo logs show the coaching tip on the progress card (next carousel
    # page), not this log card — carried via FlexReply for the caller to place.
    return FlexReply(f"🍽️ {name} — {cal} kcal", bubble,
                     coaching_note=analysis.get("coaching_suggestion")), rowid


def _handle_drink(user_id: str, analysis: dict, labels: dict,
                  image_url: str | None = None) -> tuple[str | FlexReply, int | None]:
    ml = round(float(analysis.get("volume_ml") or 0))

    # Don't log an empty container
    if ml <= 0:
        log.info("drink volume is 0 — skipping hydration log")
        return labels["empty_drink"], None

    synced_hydration, hydration_point = log_hydration_to_health(user_id, analysis)

    # If the drink has significant calories/protein (e.g. protein shake, juice,
    # smoothie), also log it as a nutrition entry.
    cal = round(float(analysis.get("calories_kcal") or 0))
    synced_nutrition, nutrition_point = False, None
    if cal > 10:
        # Build a food-like analysis dict for the nutrition log
        nutrition_analysis = {
            "food_name_en": analysis.get("drink_name_en") or analysis.get("drink_name_local") or "drink",
            "calories_kcal": analysis.get("calories_kcal", 0),
            "protein_g": analysis.get("protein_g", 0),
            "total_carbohydrate_g": analysis.get("total_carbohydrate_g", 0),
            "total_fat_g": analysis.get("total_fat_g", 0),
        }
        synced_nutrition, nutrition_point = log_food_to_health(user_id, nutrition_analysis)

    rowid = _store_food_log(
        user_id,
        {**analysis, "health_point_names": [n for n in (hydration_point, nutrition_point) if n]},
        synced_hydration,
    )

    name = analysis.get("drink_name_local") or analysis.get("drink_name_en") or "drink"
    protein = round(float(analysis.get("protein_g") or 0))
    carbs = round(float(analysis.get("total_carbohydrate_g") or 0))
    fat = round(float(analysis.get("total_fat_g") or 0))
    confidence = analysis.get("confidence", "medium")

    count = int(float(analysis.get("container_count") or 1))
    rows = []
    if count > 1:
        rows.append((labels["containers"], str(count)))
    if cal > 0:
        rows.append((labels["energy"], f"{cal} kcal"))
    if protein > 0:
        rows.append((labels["protein"], f"{protein} g"))
    if carbs > 0:
        rows.append((labels["carbs"], f"{carbs} g"))
    if fat > 0:
        rows.append((labels["fat"], f"{fat} g"))

    sync_label = labels["synced"] if (synced_hydration or synced_nutrition) else labels["not_synced"]

    bubble = build_log_bubble(
        name=name, kicker=labels["kicker_drink"], accent_color=COLOR_DRINK,
        highlight=("🥤", f"{ml} ml"), rows=rows,
        notes=analysis.get("notes"),
        synced=synced_hydration or synced_nutrition,
        sync_label=sync_label,
        low_conf_label=labels["low_conf"] if confidence == "low" else None,
        image_url=image_url,
    )
    return FlexReply(f"💧 {name}", bubble,
                     coaching_note=analysis.get("coaching_suggestion")), rowid


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    DEFAULT_USER_ID = "U1068a1b9c15b44e7ff1439bdefdeb5dc"

    if len(sys.argv) < 2:
        print("Usage: python -m coach.food <image_path>")
        sys.exit(1)
    with open(sys.argv[1], "rb") as f:
        img = f.read()
    reply, _ = handle_food_photo(DEFAULT_USER_ID, img)
    if isinstance(reply, FlexReply):
        print(reply.alt_text)
        print(json.dumps(reply.bubble, indent=2, ensure_ascii=False))
    else:
        print(reply)
