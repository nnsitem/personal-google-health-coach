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


def log_chat_entry(user_id: str, kind: str, analysis: dict | None) -> tuple[str, int | None]:
    """Log a food/drink the user DESCRIBED in chat (no photo).

    `analysis` is the JSON the chat model emitted in a [LOG_FOOD]/[LOG_DRINK]
    directive — same shape as the vision output, plus optional meal_type /
    time / date fields. Returns (status_line, insights_rowid_or_None): the
    localized status is appended to the coach's reply so the visible
    confirmation reflects whether the Google Health write actually happened,
    and the rowid lets the caller map the sent message for quote-replies.
    """
    db.init_db()
    labels = LABELS.get(_lang_code(_get_language(user_id)), LABELS["en"])

    if not isinstance(analysis, dict):
        return labels["not_synced"], None

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
        if synced_hydration and synced_nutrition:
            return labels["synced_drink"] + " + " + labels["synced_food"], rowid
        return (labels["synced_drink"] if synced_hydration else labels["not_synced"]), rowid

    if round(analysis.get("calories_kcal") or 0) <= 0:
        return labels["empty_food"], None
    synced, point_name = log_food_to_health(user_id, analysis)
    rowid = _store_food_log(
        user_id,
        {**analysis, "type": "food", "source": "chat",
         "health_point_names": [n for n in (point_name,) if n]},
        synced,
    )
    return (labels["synced_food"] if synced else labels["not_synced"]), rowid


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
                return True
            except HealthAPIError as e:
                log.error("failed to delete stored points for adjustment: %s", e)
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
    try:
        client = client_for_user(user_id)
        for data_type in data_types:
            field = data_type.replace("-", "_")
            filter_str = (
                f'{field}.interval.civil_start_time >= "{start}" '
                f'AND {field}.interval.civil_start_time < "{end}"'
            )
            points = client.list_points(data_type, filter_str)
            names = [p["name"] for p in points if p.get("name")]
            if names:
                client.batch_delete_data_points(data_type, names)
            counts[data_type] = len(names)
    except HealthAPIError as e:
        # Don't clear local history if Google Health still holds the points —
        # the two stores must not diverge.
        log.error("failed to clear today's %s logs: %s", kind, e)
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
    if total == 0 and removed_local == 0:
        return labels["nothing_today"]
    parts = []
    if kind in ("food", "all"):
        parts.append(labels["deleted_meals"].format(n=counts["nutrition-log"]))
    if kind in ("drink", "all"):
        parts.append(labels["deleted_drinks"].format(n=counts["hydration-log"]))
    log.info("cleared today's logs for %s: %s (local rows: %d)", user_id, counts, removed_local)
    return "🗑️ " + labels["deleted_today"] + " " + " / ".join(parts)


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
        ok_label = labels["synced_drink"]
    else:
        synced, food_point = log_food_to_health(user_id, scaled)
        if food_point:
            new_points.append(food_point)
        ok_label = labels["synced_food"]
    scaled["health_point_names"] = new_points

    with db.connect() as conn:
        conn.execute(
            "UPDATE insights SET content = ? WHERE rowid = ?",
            (json.dumps({**scaled, "synced_to_health": synced}), row["rowid"]),
        )
    return (ok_label if synced else labels["not_synced"]), row["rowid"]


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
        "no_recent_log": "🤔 I couldn't find a recent log to adjust.",
        "delete_failed": "⚠️ Couldn't delete from Google Health — please try again.",
        "nothing_today": "🤷 No logs found for today.",
        "deleted_today": "Cleared today's logs:",
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
        "synced_food": "✅ บันทึกลง Google Health เรียบร้อยแล้ว",
        "synced_drink": "✅ บันทึกการดื่มน้ำลง Google Health แล้ว",
        "not_synced": "⚠️ วิเคราะห์สำเร็จ แต่ยังบันทึกลง Google Health ไม่ได้",
        "low_conf": "(ค่าประมาณอาจคลาดเคลื่อน ลองถ่ายชัด ๆ อีกครั้ง)",
        "empty_drink": "🥤 ดูเหมือนแก้ว/ขวดจะว่างเปล่า ผมเลยยังไม่ได้บันทึกนะครับ ถ้ามีน้ำอยู่ในภาพ ส่งมาใหม่ได้เลยครับ",
        "empty_food": "🍽️ ผมประเมินปริมาณอาหารไม่ได้ เลยยังไม่บันทึกครับ ลองถ่ายอาหารให้ชัดขึ้นอีกนิดนะครับ",
        "no_recent_log": "🤔 ผมไม่พบรายการที่เพิ่งบันทึกไว้ให้ปรับครับ",
        "delete_failed": "⚠️ ยังลบจาก Google Health ไม่สำเร็จครับ ลองใหม่อีกครั้งนะครับ",
        "nothing_today": "🤷 วันนี้ยังไม่มีรายการที่บันทึกไว้ครับ",
        "deleted_today": "ลบรายการของวันนี้แล้ว:",
        "deleted_meals": "อาหาร {n} รายการ",
        "deleted_drinks": "เครื่องดื่ม {n} รายการ",
        "ai_busy": "⏳ ตอนนี้ระบบ AI มีผู้ใช้งานเยอะมาก ผมเลยยังวิเคราะห์รูปไม่ได้ครับ อีกสักครู่ลองส่งรูปมาใหม่นะครับ",
        "quota_exhausted": "⛔ คีย์ Gemini ของคุณใช้โควต้าฟรีของวันนี้หมดแล้ว ผมเลยวิเคราะห์รูปไม่ได้ชั่วคราวครับ โควต้าจะรีเซ็ตเที่ยงคืนเวลาแปซิฟิก (ราวบ่าย 2 เวลาไทย)",
    },
}


def handle_food_photo(user_id: str, image_bytes: bytes,
                      mime_type: str = "image/jpeg") -> tuple[str, int | None]:
    """Full flow: analyze image → log to Google Health → return a LINE reply.

    Handles both food (nutrition-log) and drinks (hydration-log).
    Reply language follows the user's stored preference.
    Returns (reply_text, insights_rowid_or_None) — the rowid lets the caller
    map the sent confirmation message for later quote-replies.
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

    if analysis["type"] == "drink":
        return _handle_drink(user_id, analysis, labels)
    return _handle_food(user_id, analysis, labels)


def _handle_food(user_id: str, analysis: dict, labels: dict) -> tuple[str, int | None]:
    cal = round(float(analysis.get("calories_kcal") or 0))

    # Don't log if there's no real portion (e.g. empty plate / not food)
    if cal <= 0:
        log.info("food calories is 0 — skipping nutrition log")
        return labels["empty_food"], None

    synced, point_name = log_food_to_health(user_id, analysis)
    rowid = _store_food_log(
        user_id,
        {**analysis, "health_point_names": [n for n in (point_name,) if n]},
        synced,
    )

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

    return "\n".join(lines), rowid


def _handle_drink(user_id: str, analysis: dict, labels: dict) -> tuple[str, int | None]:
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

    return "\n".join(lines), rowid


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    DEFAULT_USER_ID = "U1068a1b9c15b44e7ff1439bdefdeb5dc"

    if len(sys.argv) < 2:
        print("Usage: python -m coach.food <image_path>")
        sys.exit(1)
    with open(sys.argv[1], "rb") as f:
        img = f.read()
    print(handle_food_photo(DEFAULT_USER_ID, img)[0])
