"""Two-way conversational health coach agent.

Receives user messages, loads context (health data, goals, memory, chat history),
calls Gemini, and returns a reply. The agent can query health data, manage goals,
and persist memory across conversations.

Run standalone test:  python -m coach.chat "How did I sleep last night?"
"""

import json
import logging
import re
from datetime import datetime, timedelta

from coach import db
from coach import gemini
from coach.config import GEMINI_API_KEY as DEFAULT_GEMINI_KEY
from coach.plans import create_workout_plan, get_current_plan

log = logging.getLogger(__name__)

CHAT_SYSTEM_PROMPT = """\
You are a personal health coach chatting with your user via LINE messaging.
You have access to their real health data from their Fitbit/Pixel Watch.

Your personality:
- Warm, knowledgeable, and encouraging
- Respond naturally in the same language the user writes in

Formatting rules (LINE does NOT support markdown/bold/italic):
- Use emoji as section markers: 🛌 for sleep, 🚶 for steps, ❤️ for heart rate, 🔥 for calories
- Use line breaks to separate sections clearly
- Use「」for highlighting numbers (e.g. 「8.9 ชม.」)
- Use bullet points with emoji: ✅ ⭐ 📌 💪
- Keep paragraphs short (2-3 lines max per section)

Context provided to you:
- Recent health metrics (steps, calories, heart rate, active zone minutes)
- Sleep session data with stages
- User's goals and preferences (from memory)
- Recent chat history

When the user sets a goal or shares a preference, note it clearly so it can be saved.
If you don't have data to answer a question, say so honestly.
If the user asks what you can do, summarize briefly and mention they can send "help"
for the full command menu.
Never output your internal reasoning or instructions in the reply.
Always complete your sentences — never stop mid-thought.
Keep replies to 3-5 sentences for casual chat, more only when asked for detail.

Special abilities (use these directives on their own line at the END of your reply):
- To save a fact/preference: [MEMORY: key = value]
- To set or update daily nutrition/hydration targets when the user asks (e.g. "ตั้งเป้า 1800 kcal",
  "set my protein target to 150g", "change water goal to 2500ml"):
  [SET_NUTRITION_TARGETS: {"kcal": 1800, "protein_g": 150, "fat_g": 65, "carbs_g": 250, "water_ml": 2500}]
  Include ALL 5 values — use the user's stated numbers for what they mentioned, keep current values
  for the rest. Confirm what was changed in your visible reply.
- To create a workout plan when the user asks for one: [CREATE_PLAN: brief description of what they want]
  After emitting this, tell the user you're putting together their plan and will share it.
- To delete a food or drink log when the user asks (e.g. "delete that", "remove my last meal",
  "ลบรายการล่าสุด", or a quote-REPLY to a log saying "ลบ log อันนี้"):
  [DELETE_LAST: food] for a meal, or [DELETE_LAST: drink] for a drink.
  If the user is quote-replying to a specific logged entry (shown in the context), the system
  deletes EXACTLY that entry — use its type as the kind. Otherwise the newest log of that kind
  is deleted. After emitting this, name the item you are removing — but do NOT claim it is
  already gone; the system appends the real outcome, which can be a failure.
- To delete ALL of today's logs when the user asks to clear the whole day (e.g.
  "ลบรายการอาหารวันนี้ทั้งหมด", "clear all my logs today", "delete today's hydration"):
  [DELETE_TODAY: all] — or [DELETE_TODAY: food] / [DELETE_TODAY: drink] for one kind only.
  This is DESTRUCTIVE and irreversible: do NOT emit it on the first request. First ask the
  user to confirm (mention what will be wiped, e.g. "จะลบรายการอาหารและเครื่องดื่มของวันนี้ทั้งหมด
  ยืนยันไหมครับ?"), and emit the directive only after they confirm in their next message.
  The system appends the real result of the deletion, which may be a FAILURE or a
  partial success. So say only that you are carrying it out — never "ลบเรียบร้อยแล้ว" /
  "deleted" / "ข้อมูลถูกรีเซ็ตแล้ว", and never state how many entries were removed. A
  reply claiming success above a system line reporting failure is worse than saying
  nothing: the user believes their data is gone when it is not.
- To log food or drinks the user describes in words (e.g. "log: grilled pork 3 skewers with sticky rice",
  "ลงโภชนาการ หมูปิ้ง 3 ไม้ กับข้าวเหนียว 1 ห่อ", "log 2 glasses of water", "บันทึกน้ำ 1 แก้ว",
  "เพิ่มมื้อเช้า ไข่ต้ม 1 ฟอง", "เพิ่มน้ำ 330 ml", "จดข้าวผัด 1 จาน", "กินไข่ต้ม 2 ฟอง",
  "add lunch: chicken salad" — "เพิ่ม" / "ลง" / "บันทึก" / "จด" / "add" + a food or drink
  is ALWAYS a log request, whether or not a meal name follows):
  [LOG_FOOD: {"food_name_en": "grilled pork skewers (3) with sticky rice", "food_name_local": "หมูปิ้งย่าง (3 ไม้) กับข้าวเหนียว", "coaching_suggestion": "one short tip grounded in today's real totals vs target — see rules below", "calories_kcal": 475, "protein_g": 22, "total_carbohydrate_g": 55, "total_fat_g": 18, "volume_ml": null, "meal_type": null, "time": null}]
  [LOG_DRINK: {"drink_name_en": "water", "drink_name_local": "น้ำเปล่า", "coaching_suggestion": "one short tip grounded in today's real totals vs target — see rules below", "container_count": 2, "volume_ml": 500, "is_water": true, "calories_kcal": 0, "protein_g": 0, "total_carbohydrate_g": 0, "total_fat_g": 0, "meal_type": null, "time": null}]
  Rules for these two directives:
  - coaching_suggestion is REQUIRED, never omit or leave empty. This is the ONLY
    place the user sees this info (your visible reply is not shown when the log
    succeeds — see below), so format it as ONE natural sentence, ALWAYS starting
    with ✨ (this exact emoji, never ⭐/💡/🎉/💧 or any other): "✨ <item name>
    <stat label>「<value> <unit>」<descriptive coaching sentence>", e.g.
    "✨ น้ำเปล่า พลังงาน「0 kcal」ช่วยเติมความสดชื่นและรักษาสมดุลน้ำในร่างกายได้อย่างดีครับ"
    or "✨ ข้าวผัด โปรตีน「22 g」เหลืออีก 40g วันนี้ ลองเติมไข่ต้มหรืออกไก่มื้อถัดไปนะครับ".
    Write a real, specific, encouraging sentence — NEVER a bare number with no
    context like "เหลืออีก 195ml"; always say why it matters or what to do next,
    the way the examples do. Pick whichever single stat (energy, or the
    nutrient furthest behind target) is most useful to name — use the "Today's
    nutrition so far vs daily target" context plus this item's own nutrition.
    If targets are basically met, congratulate instead of naming a gap.
  - food_name_en / food_name_local / drink_name_en / drink_name_local: write CLEAN,
    PROPERLY FORMATTED display names — not the user's raw shorthand. Capitalize brand
    names, use correct spelling, include quantity in parentheses. Examples:
    User: "ไอติม soft serve นม ร้าน mos burder 1 โคน"
    → food_name_en = "Milk Soft Serve Ice Cream, Mos Burger (1 cone)"
    → food_name_local = "ไอศกรีมซอฟต์เสิร์ฟรสนม Mos Burger (1 โคน)"
    Always START WITH A CAPITAL LETTER and use Title Case — "Aburi Salmon Sushi",
    not "aburi salmon sushi"; "Iced Green Tea", not "iced green tea".
    These names are shown as the card title and logged to Google Health, so make
    them look polished and readable in both languages.
  - Anything DRINKABLE goes in LOG_DRINK, even when it is thick or a meal in
    itself — smoothies, protein shakes, blended juice, milk, yoghurt drinks.
    LOG_DRINK records BOTH the fluid and its nutrition; LOG_FOOD alone throws the
    fluid away, which is how a 350 kcal Boost smoothie counted for 0 ml of the
    day's hydration. If you do use LOG_FOOD for something with a real liquid
    portion (soup, congee), set "volume_ml" to that portion so it still counts as
    fluid. Leave volume_ml null for solid food.
  - Estimate realistic nutrition/volume from the description and stated portions
    (a glass ≈ 250 ml, a bottle ≈ 500 ml). volume_ml is the TOTAL across containers.
  - Valid single-line JSON only; every number a plain number, never a range or text.
  - meal_type: "BREAKFAST" | "LUNCH" | "DINNER" | "SNACK" — set ONLY when the user
    says which meal it was (breakfast/มื้อเช้า, lunch/มื้อเที่ยง, dinner/มื้อเย็น, snack/ของว่าง); else null.
  - time: "HH:MM" (24h, user's local time) ONLY when they say when they had it; you may
    add "date": "YYYY-MM-DD" for a previous day (e.g. "เมื่อวาน"). Otherwise null (= now).
  - Emit one directive per item if they describe several distinct meals/drinks with
    different times; combine dishes eaten together into ONE entry.
  - Only log when the user asks to log/record something — not when food is merely mentioned.
  - CRITICAL — you never save anything yourself; emitting the directive is the ONLY thing
    that saves. If a reply of yours contains no directive, NOTHING was written to Google
    Health. So never write "บันทึกแล้ว" / "logged it" / "saved" and never quote a NEW
    running total unless this same reply carries the matching directive. If you are unsure
    whether the user wants it logged, ASK instead of claiming you saved it.
  - In the conversation history above, your own past turns that read "[LOG_FOOD] …" /
    "[LOG_DRINK] …" / "[ADJUST_LAST] …" are the turns where a save actually happened —
    that tag is how it happened, and it is what you must emit again here.
  - Keep your visible reply to a short one-line acknowledgment (e.g. "กำลังบันทึกให้ครับ").
    When the log succeeds, this reply is NOT sent — the log card (item, stat, and
    coaching_suggestion) already tells the whole story, so don't spend effort writing
    a detailed confirmation here. It's only shown if the save fails.
- To change the QUANTITY of the most recent food/drink log when the user says how much they
  actually had (e.g. "กินไปแล้ว 4 รอบ" right after a log, "I had 4 of those", "only drank half"):
  [ADJUST_LAST: {"kind": "drink", "times": 4}]
  "times" is the TOTAL multiple of the originally logged amount (4 = four servings in total,
  0.5 = half a serving). kind is "food" or "drink" and must match the TYPE of the log being
  adjusted, not the verb the user used ("กิน 4 รอบ" about a drink log still means kind "drink").
  If the conversation shows a quoted log entry the user is replying to, THAT entry is the
  target — use its type and amounts. Otherwise use the newest matching entry in the recent
  food/drink logs context. Confirm the new total in your visible reply (e.g. 4 × 200 ml =
  「800 ml」). Do NOT also emit LOG_FOOD/LOG_DRINK for the same item. The system appends the
  real save status.
"""


# ---------------------------------------------------------------------------
# Context builders
# ---------------------------------------------------------------------------

def _get_recent_metrics(user_id: str, days: int = 7) -> dict:
    """Get the last N days of health metrics (the USER's days, not the server's)."""
    cutoff = (datetime.now(db.user_tz(db.get_user(user_id))).date()
              - timedelta(days=days)).isoformat()
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT day, data_type, value_json FROM metrics WHERE user_id = ? AND day >= ? ORDER BY day DESC",
            (user_id, cutoff),
        ).fetchall()

    metrics = {}
    for row in rows:
        day = row["day"]
        if day not in metrics:
            metrics[day] = {}
        value = json.loads(row["value_json"])

        data_type = row["data_type"]
        if data_type == "steps":
            metrics[day]["steps"] = int(value.get("steps", {}).get("countSum", 0))
        elif data_type == "total-calories":
            metrics[day]["calories"] = round(value.get("totalCalories", {}).get("kcalSum", 0))
        elif data_type == "daily-resting-heart-rate":
            metrics[day]["resting_hr"] = int(value.get("dailyRestingHeartRate", {}).get("beatsPerMinute", 0))
        elif data_type == "active-zone-minutes":
            azm = value.get("activeZoneMinutes", {})
            metrics[day]["active_zone_min"] = (
                int(azm.get("sumInFatBurnHeartZone", 0))
                + int(azm.get("sumInCardioHeartZone", 0))
                + int(azm.get("sumInPeakHeartZone", 0))
            )
    return metrics


def _get_recent_sleep(user_id: str, days: int = 7) -> list[dict]:
    """Get recent sleep sessions summarized, in the user's local time."""
    tz = db.user_tz(db.get_user(user_id))
    cutoff = (datetime.now(tz) - timedelta(days=days)).isoformat()
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT start, end, stages_json, efficiency, score FROM sleep_sessions WHERE user_id = ? AND start >= ? ORDER BY start DESC",
            (user_id, cutoff),
        ).fetchall()

    sessions = []
    for row in rows:
        stages = json.loads(row["stages_json"]) if row["stages_json"] else []
        totals = {"DEEP": 0, "LIGHT": 0, "REM": 0, "AWAKE": 0}
        for stage in stages:
            try:
                s = datetime.fromisoformat(stage["startTime"].replace("Z", "+00:00"))
                e = datetime.fromisoformat(stage["endTime"].replace("Z", "+00:00"))
                mins = (e - s).total_seconds() / 60
                if stage.get("type") in totals:
                    totals[stage["type"]] += mins
            except (ValueError, KeyError):
                continue

        in_bed_min = sum(totals.values())
        asleep_min = in_bed_min - totals["AWAKE"]
        start_local = datetime.fromisoformat(row["start"].replace("Z", "+00:00")).astimezone(tz)
        end_local = datetime.fromisoformat(row["end"].replace("Z", "+00:00")).astimezone(tz)

        sessions.append({
            "date": start_local.strftime("%Y-%m-%d"),
            "bedtime": start_local.strftime("%H:%M"),
            "wake": end_local.strftime("%H:%M"),
            # asleep_hours matches the Google Health app's headline number
            # (time asleep, awake time excluded)
            "asleep_hours": round(asleep_min / 60, 1),
            "in_bed_hours": round(in_bed_min / 60, 1),
            "deep_min": round(totals["DEEP"]),
            "rem_min": round(totals["REM"]),
            "light_min": round(totals["LIGHT"]),
            "awake_min": round(totals["AWAKE"]),
        })
    return sessions


def _get_recent_food_logs(user_id: str, hours: int = 48, limit: int = 8) -> list[dict]:
    """Recent food/drink logs (photo or chat), newest first, summarized for
    the chat context — so the coach knows what "the last log" was and can
    answer follow-ups like "กินไปแล้ว 4 รอบ" / "make that 4"."""
    from datetime import timezone as _timezone
    cutoff = (datetime.now(_timezone.utc) - timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M:%S")
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT ts, content FROM insights "
            "WHERE user_id = ? AND kind = 'food_log' AND ts >= ? "
            "ORDER BY ts DESC, rowid DESC LIMIT ?",
            (user_id, cutoff, limit),
        ).fetchall()

    logs = []
    for row in rows:
        try:
            a = json.loads(row["content"])
        except (json.JSONDecodeError, ValueError):
            continue
        entry = {
            "ts_utc": row["ts"],
            "type": a.get("type") or ("drink" if a.get("volume_ml") else "food"),
            "name": (a.get("food_name_local") or a.get("food_name_en")
                     or a.get("drink_name_local") or a.get("drink_name_en") or "?"),
        }
        if a.get("volume_ml"):
            entry["ml"] = round(float(a["volume_ml"]))
        if a.get("calories_kcal"):
            entry["kcal"] = round(float(a["calories_kcal"]))
        if a.get("times"):
            entry["times"] = a["times"]
        logs.append(entry)
    return logs


def _get_goals(user_id: str) -> dict:
    """Load all user goals."""
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT key, value_json FROM goals WHERE user_id = ?",
            (user_id,),
        ).fetchall()
    return {row["key"]: json.loads(row["value_json"]) for row in rows}


def _get_coach_memory(user_id: str) -> dict:
    """Load coach memory (preferences, facts about the user)."""
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT name, content FROM coach_memory WHERE user_id = ? ORDER BY updated_at DESC LIMIT 20",
            (user_id,),
        ).fetchall()
    return {row["name"]: row["content"] for row in rows}


def _get_chat_history(user_id: str, limit: int = 20) -> list[dict]:
    """Load recent chat messages."""
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT ts, role, text FROM chat_messages WHERE user_id = ? ORDER BY ts DESC LIMIT ?",
            (user_id, limit),
        ).fetchall()
    # Return in chronological order
    return [{"role": row["role"], "text": row["text"]} for row in reversed(rows)]


def _save_chat_message(user_id: str, role: str, text: str) -> None:
    """Store a chat message."""
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO chat_messages (user_id, ts, role, text) VALUES (?, datetime('now'), ?, ?)",
            (user_id, role, text),
        )


def save_goal(user_id: str, key: str, value) -> None:
    """Save or update a user goal."""
    with db.connect() as conn:
        conn.execute(
            """
            INSERT INTO goals (user_id, key, value_json, updated_at)
            VALUES (?, ?, ?, datetime('now'))
            ON CONFLICT(user_id, key) DO UPDATE SET value_json = excluded.value_json, updated_at = datetime('now')
            """,
            (user_id, key, json.dumps(value)),
        )


def save_memory(user_id: str, name: str, content: str) -> None:
    """Save or update a coach memory entry."""
    with db.connect() as conn:
        conn.execute(
            """
            INSERT INTO coach_memory (user_id, name, content, updated_at)
            VALUES (?, ?, ?, datetime('now'))
            ON CONFLICT(user_id, name) DO UPDATE SET content = excluded.content, updated_at = datetime('now')
            """,
            (user_id, name, content),
        )


# ---------------------------------------------------------------------------
# Agent
# ---------------------------------------------------------------------------

def _ensure_fresh_data(user_id: str) -> None:
    """Run a sync if the last successful sync was more than 10 minutes ago.

    This ensures the chat always has reasonably current data without syncing
    on every single message when messages come in rapid succession.

    The sync is nine Google Health calls made INLINE, before the user gets any
    answer: ~15s on a good day, and 96s during the health.googleapis.com
    timeouts of 2026-08-22. Callers skip it when the message can't need device
    data (see _needs_device_data).
    """
    from datetime import datetime, timedelta, timezone
    from coach.sync import run_sync

    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=10)).strftime("%Y-%m-%d %H:%M:%S")
    with db.connect() as conn:
        row = conn.execute(
            "SELECT ts FROM sync_log WHERE user_id = ? AND ok = 1 ORDER BY ts DESC LIMIT 1",
            (user_id,),
        ).fetchone()

    if row and row["ts"] > cutoff:
        return  # last sync was recent enough

    try:
        run_sync(user_id)
    except Exception:
        log.warning("sync before chat failed — proceeding with cached data", exc_info=True)


def _needs_device_data(text: str, is_quote_reply: bool = False) -> bool:
    """Whether answering this message could require fresh watch data.

    "เพิ่มน้ำ 250ml" needs nothing from the watch — steps, sleep and exercise
    have no bearing on writing down a glass of water — yet it paid for a full
    sync before the reply. Logging, adjusting and deleting are all decided from
    the message plus our own history, so they skip it; anything else (questions
    about sleep, steps, recovery, plans, or plain conversation) still syncs.
    """
    if _QUESTION_RE.search(text):
        return True    # "วันนี้เดินไปกี่ก้าว" is exactly what fresh data is for
    if is_quote_reply:
        return False   # an adjustment or deletion of an entry we already hold
    return not (_RECORD_VERB_RE.search(text)
                or _ADJUST_INTENT_RE.search(text)
                or _DELETE_INTENT_RE.search(text))

def _build_context_message(user_id: str) -> str:
    """Build a context block with current + historical health data for the agent."""
    from coach.stats import build_trends

    tz = db.user_tz(db.get_user(user_id))
    now = datetime.now(tz)
    goals = _get_goals(user_id)
    memory = _get_coach_memory(user_id)

    # The model reasons about "today" from this line; the server's clock is not
    # the user's once anyone signs up outside the server's timezone.
    parts = [f"Current time: {now.strftime('%Y-%m-%d %H:%M')} ({tz})"]

    # Multi-window summary: today, yesterday, weekly & monthly averages, trends.
    # This lets the coach reason about patterns, not just today's snapshot.
    try:
        trends = build_trends(user_id)
        parts.append(
            "Health data (today / yesterday / week_avg / month_avg / trend): "
            f"{json.dumps(trends, separators=(',', ':'))}"
        )
    except Exception:
        log.exception("failed to build trends; falling back to recent metrics")
        metrics = _get_recent_metrics(user_id, 7)
        if metrics:
            parts.append(f"Recent metrics (last 7 days): {json.dumps(metrics, separators=(',', ':'))}")

    # Recent raw sleep detail (last 3 nights) for stage-level questions
    sleep = _get_recent_sleep(user_id, 3)
    if sleep:
        parts.append(f"Recent sleep detail: {json.dumps(sleep, separators=(',', ':'))}")

    # Recent food/drink logs so follow-ups about "the last log" have context
    food_logs = _get_recent_food_logs(user_id)
    if food_logs:
        parts.append(
            "Recent food/drink logs (newest first, ts in UTC): "
            f"{json.dumps(food_logs, separators=(',', ':'), ensure_ascii=False)}"
        )

    # Today's nutrition/hydration totals vs target — needed to ground the
    # LOG_FOOD/LOG_DRINK coaching_suggestion in real numbers, not guesses.
    try:
        from coach.food import get_daily_progress
        progress = get_daily_progress(user_id)
        parts.append(
            "Today's nutrition so far vs daily target (kcal/protein_g/carbs_g/fat_g/water_ml): "
            f"current={json.dumps(progress['current'], separators=(',', ':'))} "
            f"target={json.dumps(progress['targets'], separators=(',', ':'))}"
        )
    except Exception:
        log.exception("failed to load daily nutrition progress for chat context")

    if goals:
        parts.append(f"User goals: {json.dumps(goals, separators=(',', ':'))}")
    if memory:
        parts.append(f"Coach memory: {json.dumps(memory, separators=(',', ':'))}")

    # Include active workout plan if one exists
    plan = get_current_plan(user_id)
    if plan:
        parts.append(f"Active workout plan: {json.dumps(plan, separators=(',', ':'))}")

    return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# Missed-directive safety net
#
# The conversational model is not reliable about appending a [LOG_FOOD] /
# [LOG_DRINK] directive: observed live, "เพิ่มมื้อเที่ยง ไข่ต้ม 1 ฟอง" came
# back as a fluent "บันทึกไข่ต้มเรียบร้อยแล้ว … รวม 1,605 kcal" with no
# directive at all, so nothing reached Google Health while the user was told
# it had. The same sentence logged fine 100 minutes later — it is a
# prompt-adherence coin flip, not a parsing bug.
#
# So logging does not stay a side effect of the chat turn: when the message
# plainly asks to record something and the chat reply carried no log
# directive, one small single-purpose call re-reads the message and returns
# just the payload. It costs a call only on the turns the primary path missed.
# ---------------------------------------------------------------------------

# Verbs that explicitly say "record this" — these win over the revision
# wording below, because "บันทึกน้ำครึ่งแก้ว" is a new entry even though it
# contains ครึ่ง (half).
_RECORD_VERB_RE = re.compile(
    r"เพิ่ม|บันทึก|จด|ลงมื้อ|ลงอาหาร|ลงน้ำ|ลงโภชนา|"
    r"\blog\b|\badd\b|\brecord\b",
    re.IGNORECASE,
)
# Merely eating/drinking. Enough on its own ("กินไข่ต้ม 2 ฟอง"), but yields to
# revision wording since "I had 4 of those" is about an existing entry.
_CONSUME_VERB_RE = re.compile(
    r"กิน|ทาน|ดื่ม|\bate\b|\bdrank\b|\bhad\b|\bdrink\b|\beat\b",
    re.IGNORECASE,
)

# A question about food ("วันนี้กินไปกี่แคล", "how many calories did I have?")
# trips the verbs above without asking for anything to be logged.
_QUESTION_RE = re.compile(
    r"[?？]|ไหม|มั้ย|หรือเปล่า|กี่|เท่าไห?ร่|อะไร|ยังไง|เป็นไง|"
    r"\bhow\b|\bwhat\b|\bwhy\b|\bwhen\b|\bshould\b|\bdid i\b",
    re.IGNORECASE,
)


# Phrasing that revises an entry that already exists ("กินไปแล้ว 4 รอบ", "only
# half", "make that 750ml"). These share verbs with the log intents above, so
# without this the extractor would answer a missed ADJUST_LAST by creating a
# second, phantom entry instead of rescaling the first.
# Deleting an entry needs nothing from the watch either.
_DELETE_INTENT_RE = re.compile(r"ลบ|เคลียร์|\bdelete\b|\bremove\b|\bclear\b", re.IGNORECASE)

# แก้(?!ว) because แก้ว ("glass") merely contains แก้ ("to change") — without
# the guard, "บันทึกน้ำ 1 แก้ว" reads as a revision and never gets logged.
_ADJUST_INTENT_RE = re.compile(
    r"รอบ|เท่า|ครึ่ง|แก้(?!ว)|เปลี่ยน|ปรับ|อันนี้|"
    r"\bactually\b|\bmake (?:that|it)\b|\binstead\b|\bhalf\b|\bof those\b|\bchange\b",
    re.IGNORECASE,
)


def _looks_like_log_request(text: str, is_quote_reply: bool = False) -> bool:
    """Cheap gate for the extractor below: does this message ask for a NEW
    entry to be recorded?

    False for questions, for revisions of an existing entry, and for
    quote-replies (which by definition point at one) — the extractor creates
    entries, so a wrong yes here would duplicate rather than rescale.
    """
    if is_quote_reply or _QUESTION_RE.search(text):
        return False
    if _RECORD_VERB_RE.search(text):
        return True
    return bool(_CONSUME_VERB_RE.search(text)) and not _ADJUST_INTENT_RE.search(text)


_LOG_EXTRACTOR_PROMPT = """\
You extract food/drink logging requests. You do NOT chat, coach, or explain.

Given one user message, output ONLY a JSON object, no markdown, no prose:
{"items": [ ... ]}

Each item is one meal or one drink the user is asking to record:
food:  {"kind": "food", "food_name_en": "...", "food_name_local": "...",
        "coaching_suggestion": "...", "calories_kcal": 0, "protein_g": 0,
        "total_carbohydrate_g": 0, "total_fat_g": 0, "meal_type": null, "time": null}
drink: {"kind": "drink", "drink_name_en": "...", "drink_name_local": "...",
        "coaching_suggestion": "...", "container_count": 1, "volume_ml": 0,
        "is_water": true, "calories_kcal": 0, "protein_g": 0,
        "total_carbohydrate_g": 0, "total_fat_g": 0, "meal_type": null, "time": null}

Return {"items": []} — and nothing else — when the message is NOT asking to
record an item: a question about past intake, a request to delete or change an
existing entry, a comment about food, or ordinary conversation.

Rules:
- One item per distinct meal/drink; dishes eaten together are ONE item.
- Estimate realistic nutrition from the description and stated portions
  (a glass ≈ 250 ml, a bottle ≈ 500 ml). volume_ml is the TOTAL across containers.
- Every number a plain number, never a range or text.
- *_en names in English; *_local names and coaching_suggestion in the user's language.
  Names are display titles: capitalize brands, correct the spelling, put the
  quantity in parentheses — e.g. "ไข่ต้ม (1 ฟอง)" / "Boiled Egg (1 egg)".
- meal_type is "BREAKFAST"|"LUNCH"|"DINNER"|"SNACK" ONLY when the user names the
  meal (มื้อเช้า/มื้อเที่ยง/มื้อเย็น/ของว่าง); otherwise null.
- time is "HH:MM" (24h local) ONLY when the user states a time; you may add
  "date": "YYYY-MM-DD" for an earlier day. Otherwise null.
- coaching_suggestion is REQUIRED: ONE natural sentence starting with ✨ (this
  exact emoji), naming the item and one useful stat, e.g.
  "✨ ไข่ต้ม โปรตีน「6 g」ช่วยเติมโปรตีนให้เข้าใกล้เป้าหมายวันนี้ครับ".
  Ground it in the supplied today's-totals-vs-target numbers. Never a bare number.
"""


def _extract_log_fallback(user_id: str, user_text: str, api_key: str) -> list[tuple[str, dict]]:
    """Re-read the message with a single-purpose extractor and return
    [(kind, payload)] in the same shape _process_directives produces.

    Returns [] when the message isn't a log request or the call fails — the
    caller then behaves exactly as it did before this net existed.
    """
    language = db.get_user_language(user_id)
    tz = db.user_tz(db.get_user(user_id))
    prompt_parts = [f"User's language: {language}",
                    f"Current local time: {datetime.now(tz).strftime('%Y-%m-%d %H:%M')}"]
    try:
        from coach.food import get_daily_progress
        progress = get_daily_progress(user_id)
        prompt_parts.append(
            "Today's totals so far vs daily target: "
            f"current={json.dumps(progress['current'], separators=(',', ':'))} "
            f"target={json.dumps(progress['targets'], separators=(',', ':'))}"
        )
    except Exception:
        log.warning("extractor: could not load daily progress", exc_info=True)
    prompt_parts.append(f"User message:\n{user_text}")

    try:
        text = gemini.generate(
            api_key, contents="\n\n".join(prompt_parts),
            system_instruction=_LOG_EXTRACTOR_PROMPT,
            max_output_tokens=4096, min_chars=2, max_wait=120, prefer_accuracy=True,
        )
    except Exception:
        log.warning("extractor call failed — leaving the turn unlogged", exc_info=True)
        return []

    from coach.food import _extract_json
    data = _extract_json(text)
    if not isinstance(data, dict):
        log.warning("extractor returned unparseable output: %s", str(text)[:200])
        return []

    out: list[tuple[str, dict]] = []
    for item in data.get("items") or []:
        if not isinstance(item, dict):
            continue
        kind = str(item.get("kind") or "").lower()
        if kind not in ("food", "drink"):
            # Infer from the payload when the model omits "kind".
            kind = "drink" if (item.get("volume_ml") or item.get("drink_name_en")) else "food"
        out.append((kind, {k: v for k, v in item.items() if k != "kind"}))
    return out


def handle_message(user_id: str, user_text: str,
                   quoted_message_id: str | None = None) -> tuple[str, list[int], list]:
    """Process an inbound user message and generate a coach reply.

    quoted_message_id: LINE id of the message the user quote-replied to, if
    any — when it maps to a log confirmation we sent, that exact log becomes
    the target for adjustments (instead of guessing "the last log").

    Stores both the user message and the reply in chat_messages.
    Returns (reply_text, created_log_rowids, extra_flex_replies) — the rowids
    let the caller map the outgoing confirmation message for future
    quote-replies; extra_flex_replies are FlexReply log-confirmation cards
    (from food/drink logged in chat text) to send as their own messages
    alongside reply_text.
    """
    db.init_db()

    # Resolve a quote-reply to the specific log it points at
    quoted_log = None
    if quoted_message_id:
        try:
            quoted_log = db.get_log_for_message(user_id, quoted_message_id)
            log.info("quoted message %s -> log rowid %s", quoted_message_id,
                     quoted_log["rowid"] if quoted_log else None)
        except Exception:
            log.exception("failed to resolve quoted message %s", quoted_message_id)

    # Refresh health data first, but only when the answer could depend on it.
    # A log/adjust/delete request is decided from the message plus our own
    # history, so it no longer waits on nine Google Health calls to be told
    # about steps it never mentions.
    if _needs_device_data(user_text, is_quote_reply=bool(quoted_message_id)):
        _ensure_fresh_data(user_id)
    else:
        log.info("skipping pre-chat sync — this message needs no device data")

    # Store user message
    _save_chat_message(user_id, "user", user_text)

    # Build context
    context = _build_context_message(user_id)
    history = _get_chat_history(user_id, 10)  # fewer messages = faster

    # Build conversation for Gemini
    # Format: system context + chat history + current message
    conversation_parts = [f"[HEALTH DATA CONTEXT]\n{context}\n\n[CONVERSATION]"]
    for msg in history[:-1]:  # exclude the message we just stored (it's the current one)
        prefix = "User" if msg["role"] == "user" else "Coach"
        # Turns stored before the log-marker fix (below) are blank whenever a
        # log succeeded, because the Flex card carried the whole reply. Rendered
        # verbatim they read as the coach answering a log request with silence,
        # so name what actually happened instead.
        text = msg["text"] or (
            "[LOG_FOOD / LOG_DRINK] (logged — confirmation card sent)"
            if msg["role"] != "user" else ""
        )
        conversation_parts.append(f"{prefix}: {text}")
    if quoted_log:
        try:  # stored with escaped unicode; re-dump so Thai names are readable
            quoted_json = json.dumps(json.loads(quoted_log["content"]),
                                     separators=(",", ":"), ensure_ascii=False)
        except (json.JSONDecodeError, ValueError):
            quoted_json = quoted_log["content"]
        conversation_parts.append(
            "(The user's next message is a quote-REPLY to this specific logged "
            "entry — it is the target of any adjustment, NOT the most recent "
            f"log: {quoted_json})"
        )
    elif quoted_message_id:
        conversation_parts.append(
            "(The user's next message is a quote-REPLY to an earlier message "
            "that is NOT a tracked log entry (possibly logged before tracking "
            "existed). If they are asking to adjust or delete a log, do NOT "
            "guess which one — ask them to confirm which item they mean, "
            "unless the recent-logs context makes it unambiguous.)"
        )
    conversation_parts.append(f"User: {user_text}")
    conversation_parts.append("\nRespond as the coach in 3-5 sentences maximum. Complete your thought fully — do not leave sentences unfinished. If the user mentions a goal or preference you should remember, end your response with a line like [MEMORY: key = value] and I'll save it.")

    full_prompt = "\n".join(conversation_parts)

    # Call Gemini
    user = db.get_user(user_id)
    api_key = (user.get("gemini_api_key") if user else None) or DEFAULT_GEMINI_KEY
    if not api_key:
        reply = "I'm not configured yet — GEMINI_API_KEY is missing."
        _save_chat_message(user_id, "coach", reply)
        return reply, [], []

    try:
        # Shorter budget than scheduled jobs — a person is waiting in chat.
        reply = gemini.generate(
            api_key, contents=full_prompt, system_instruction=CHAT_SYSTEM_PROMPT,
            max_output_tokens=4096, min_chars=10, max_wait=180, prefer_accuracy=True,
        )
    except gemini.GeminiQuotaExhausted:
        log.warning("Gemini daily quota exhausted for user %s", user_id)
        reply = ("⛔ Your Gemini AI key has used up its free daily quota. "
                 "I'll be able to reply again after it resets at midnight US Pacific "
                 "time (~2pm Thailand time).")
    except Exception:
        log.exception("Gemini call failed")
        reply = "Sorry, I'm having trouble connecting right now. Try again in a moment! 🙏"

    # Extract and process directives (memory + plan creation + delete + logs)
    reply, plan_request, delete_kind, chat_logs, delete_today, directive_failures = _process_directives(user_id, reply)

    # Safety net: the model regularly answers a log request with a fluent
    # "saved it!" and no directive, which used to mean the entry never reached
    # Google Health while the user was told it had. Re-read the message with
    # the single-purpose extractor instead of losing the log. Skipped when a
    # deletion/adjustment was requested — those aren't new entries.
    if (not chat_logs and not delete_kind and not delete_today
            and _looks_like_log_request(user_text, is_quote_reply=bool(quoted_message_id))):
        chat_logs = _extract_log_fallback(user_id, user_text, api_key)
        log.info("no log directive in reply for a log-shaped message — extractor produced %d entry(ies)",
                 len(chat_logs))

    # If the coach requested a plan, create it and append a formatted summary
    if plan_request:
        try:
            context_dict = {
                "metrics": _get_recent_metrics(user_id, 7),
                "sleep": _get_recent_sleep(user_id, 7),
                "goals": _get_goals(user_id),
            }
            plan = create_workout_plan(user_id, plan_request, context_dict)
            reply = reply + "\n\n" + _format_plan(plan)
            log.info("created workout plan: %s", plan.get("name", "unnamed"))
        except Exception:
            log.exception("failed to create workout plan")
            reply = reply + "\n\n(ขออภัย ยังสร้างแผนไม่สำเร็จ ลองใหม่อีกครั้งนะครับ)"

    # If the coach logged food/drinks described in chat, write them to Google
    # Health and append the REAL save status (the model is told not to claim
    # success itself).
    created_rowids: list[int] = []
    extra_flex: list = []
    # What actually got written, for the chat-history turn (see below).
    history_marks: list[str] = []
    for kind, analysis in chat_logs:
        try:
            from coach.food import log_chat_entry, adjust_last_log
            if kind == "adjust":
                status, adj_rowid = adjust_last_log(
                    user_id, analysis,
                    insight_rowid=quoted_log["rowid"] if quoted_log else None,
                )
                # Map the adjustment confirmation too, so quoting IT
                # re-targets the same log.
                if adj_rowid is not None:
                    created_rowids.append(adj_rowid)
                    history_marks.append(
                        f"[ADJUST_LAST] ×{(analysis or {}).get('times')}")
            else:
                status, rowid = log_chat_entry(user_id, kind, analysis)
                if rowid is not None:
                    created_rowids.append(rowid)
                    tag = "LOG_DRINK" if kind == "drink" else "LOG_FOOD"
                    from coach.flex import FlexReply as _FlexReply
                    label = (status.alt_text if isinstance(status, _FlexReply)
                             else (analysis or {}).get("food_name_local")
                             or (analysis or {}).get("drink_name_local") or "")
                    history_marks.append(f"[{tag}] {label}".strip())
        except Exception:
            # log_food_to_health/log_hydration_to_health already turn any
            # write failure (HealthAPIError or otherwise) into a clean
            # (False, None) — reaching here means something else broke (e.g.
            # a local DB write). Must still be a clear, localized message:
            # a bare "⚠️" reads as decorative and gives no indication that
            # nothing was actually saved.
            log.exception("failed to process chat %s directive", kind)
            from coach.food import LABELS, _lang_code
            status = LABELS.get(_lang_code(db.get_user_language(user_id)), LABELS["en"])["not_synced"]
        from coach.flex import FlexReply
        if isinstance(status, FlexReply):
            # Try to combine log card + daily progress into a single carousel
            try:
                from coach.food import get_daily_progress
                from coach.flex import build_daily_progress_bubble
                progress = get_daily_progress(user_id)
                progress_bubble = build_daily_progress_bubble(
                    current=progress["current"],
                    targets=progress["targets"],
                    lang=progress["lang"],
                )
                if progress_bubble:
                    # Wrap both bubbles in a carousel container
                    carousel = {
                        "type": "carousel",
                        "contents": [status.bubble, progress_bubble],
                    }
                    extra_flex.append(FlexReply(status.alt_text, carousel))
                else:
                    extra_flex.append(status)
            except Exception:
                extra_flex.append(status)  # fallback: just the log card
        elif status:
            reply = reply + "\n\n" + status

    # A successful log's card already shows the item, its stat, and a coaching
    # tip — the AI's own acknowledgment text on top of that is just noise, so
    # drop it (unless the reply also covers something else, e.g. a plan).
    if extra_flex and not plan_request:
        reply = ""

    # If the coach requested a deletion: a quote-reply deletes exactly the
    # quoted log; otherwise the newest log of that kind.
    if delete_kind:
        try:
            from coach.food import delete_log, delete_newest_log
            if quoted_log:
                deleted = delete_log(user_id, quoted_log["rowid"])
            else:
                deleted = delete_newest_log(user_id, delete_kind)
            if deleted:
                reply = reply + f"\n\n🗑️ ({deleted})"
            else:
                reply = reply + "\n\n(ไม่พบรายการล่าสุดให้ลบ หรือยังลบไม่สำเร็จ)"
        except Exception:
            log.exception("failed to delete log")

    # If the coach requested clearing today's logs (already user-confirmed
    # per the prompt), sweep the whole local day
    if delete_today:
        try:
            from coach.food import delete_today_logs
            reply = reply + "\n\n" + delete_today_logs(user_id, delete_today)
        except Exception:
            log.exception("failed to delete today's logs")
            reply = reply + "\n\n⚠️"

    # A directive the system could not carry out must not be left under a
    # confident reply that says it worked.
    if directive_failures:
        from coach.food import LABELS, _lang_code
        labels = LABELS.get(_lang_code(db.get_user_language(user_id)), LABELS["en"])
        for failed in directive_failures:
            if failed == "nutrition_targets":
                reply = (reply + "\n\n" + labels["targets_failed"]).strip()

    reply = reply.strip()

    # Store coach reply. What lands here is fed back as few-shot context on
    # every later turn, so it must record what the coach ACTUALLY did: a
    # successful log stored an EMPTY turn (the Flex card carried the whole
    # reply) while a MISSED log stored the model's fluent "บันทึกแล้ว" prose.
    # Read back together, those taught the model that prose confirmations are
    # how logging works — and it then stopped emitting the directive at all
    # (observed live 2026-08-20: two "ไข่ต้ม 1 ฟอง" requests confirmed in
    # prose, nothing written). Naming the executed directive keeps the
    # history's implicit example the correct one.
    history_reply = "\n".join(p for p in (reply, " ".join(history_marks)) if p)
    _save_chat_message(user_id, "coach", history_reply)

    return reply, created_rowids, extra_flex


def _format_plan(plan: dict) -> str:
    """Format a workout plan dict into a readable LINE message."""
    lines = [f"📋 {plan.get('name', 'Your Workout Plan')}"]

    if plan.get("goal"):
        lines.append(f"🎯 {plan['goal']}")
    if plan.get("duration_weeks"):
        lines.append(f"⏳ {plan['duration_weeks']} weeks")

    lines.append("")  # blank line

    for entry in plan.get("schedule", []):
        day = entry.get("day", "")
        workout = entry.get("workout", "")
        duration = entry.get("duration_min", "")
        if workout:
            dur_str = f"「{duration} min」" if duration else ""
            lines.append(f"📅 {day}: {workout} {dur_str}".rstrip())

    if plan.get("notes"):
        lines.append("")
        lines.append(f"💡 {plan['notes']}")

    return "\n".join(lines)


_DIRECTIVE_OPEN_RE = re.compile(
    r"\[(MEMORY|SET_NUTRITION_TARGETS|CREATE_PLAN|DELETE_LAST|DELETE_TODAY|LOG_FOOD|LOG_DRINK|ADJUST_LAST):\s*"
)


def _match_brace(text: str, start: int) -> int | None:
    """Index just past the `}` closing the object that opens at `start`.

    String-aware, so a brace or bracket inside a value doesn't confuse the
    depth count. Returns None when the object never closes.
    """
    depth = 0
    in_str = False
    escaped = False
    for i in range(start, len(text)):
        c = text[i]
        if in_str:
            if escaped:
                escaped = False
            elif c == "\\":
                escaped = True
            elif c == '"':
                in_str = False
            continue
        if c == '"':
            in_str = True
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return i + 1
    return None


def _scan_directives(text: str):
    """Yield (tag, payload, start, end) for each directive found in `text`.

    A JSON payload is delimited by brace matching rather than by the next
    "]" — a `]` inside a string value (a coaching_suggestion, a food name)
    used to truncate the payload into unparseable JSON, and a missing closing
    `]` made the regex skip the directive entirely, silently dropping the log
    while leaving the raw tag in the user's reply.
    """
    pos = 0
    while True:
        m = _DIRECTIVE_OPEN_RE.search(text, pos)
        if not m:
            return
        body_start = m.end()
        if body_start < len(text) and text[body_start] == "{":
            brace_end = _match_brace(text, body_start)
            if brace_end is None:  # unterminated JSON — take the rest of the line
                nl = text.find("\n", body_start)
                brace_end = len(text) if nl < 0 else nl
            payload = text[body_start:brace_end]
            end = brace_end
            # Swallow the closing "]" when the model remembered to write one.
            rest = text[brace_end:]
            skip = len(rest) - len(rest.lstrip(" \t"))
            if rest[skip:skip + 1] == "]":
                end = brace_end + skip + 1
        else:
            close = text.find("]", body_start)
            if close < 0:
                payload, end = text[body_start:], len(text)
            else:
                payload, end = text[body_start:close], close + 1
        yield m.group(1), payload.strip(), m.start(), end
        pos = end


def _process_directives(user_id: str, text: str) -> tuple[str, str | None, str | None, list, str | None, list]:
    """Extract [MEMORY: ...], [CREATE_PLAN: ...], [DELETE_LAST: ...] and
    [LOG_FOOD/LOG_DRINK: {...}] directives.

    Matches directives anywhere in the text (not just on an isolated line),
    since the model doesn't always put the tag alone on its own line — it may
    trail extra acknowledgment text right after the closing "]" on the same
    line, which a strict per-line match would miss entirely.

    Returns (cleaned_text, plan_request_or_None, delete_kind_or_None, logs,
    delete_today_or_None) where logs is a list of ("food"|"drink"|"adjust",
    payload_dict_or_None) — None marks a directive whose JSON didn't parse,
    so the caller can surface a not-saved warning instead of silently
    dropping it. Memory directives are saved immediately; the rest are
    returned for the caller to handle (slower operations).
    """
    plan_request = None
    delete_kind = None
    delete_today = None
    logs: list[tuple[str, dict | None]] = []
    # Directives that were emitted but could not be carried out; the caller
    # turns these into a visible correction so a confident reply is never left
    # standing over work that silently failed.
    failures: list[str] = []

    def _parse_log(kind: str, inner: str) -> None:
        try:
            data = json.loads(inner)
            if kind in ("food", "drink") and isinstance(data, dict) and not data.get("coaching_suggestion"):
                log.warning("%s directive missing coaching_suggestion", kind)
            logs.append((kind, data if isinstance(data, dict) else None))
        except (json.JSONDecodeError, ValueError):
            log.warning("unparseable %s directive: %s", kind, inner[:200])
            logs.append((kind, None))

    def _handle(tag: str, inner: str) -> None:
        nonlocal plan_request, delete_kind, delete_today
        if tag == "MEMORY":
            if "=" in inner:
                key, value = inner.split("=", 1)
                key, value = key.strip(), value.strip()
                save_memory(user_id, key, value)
                log.info("saved memory: %s = %s", key, value)
                # Mirror the language preference onto the users column so
                # non-chat modules (food replies, etc.) see it too.
                if key.lower() == "language" and value:
                    db.update_user(user_id, language=value)
        elif tag == "SET_NUTRITION_TARGETS":
            # A parse failure used to be logged and dropped, leaving the model's
            # "your targets are updated" reply standing over nothing saved.
            # Surface it so the caller can tell the user the truth.
            try:
                targets = json.loads(inner)
                if not isinstance(targets, dict) or not targets:
                    raise ValueError("empty or non-object payload")
                save_goal(user_id, "daily_nutrition_targets", targets)
                log.info("saved nutrition targets: %s", targets)
            except (json.JSONDecodeError, ValueError) as e:
                log.warning("failed to parse nutrition targets (%s): %s", e, inner[:200])
                failures.append("nutrition_targets")
        elif tag == "CREATE_PLAN":
            plan_request = inner
            log.info("plan creation requested: %s", plan_request)
        elif tag == "DELETE_LAST":
            kind = inner.lower()
            delete_kind = "drink" if "drink" in kind else "food"
            log.info("delete requested: %s", delete_kind)
        elif tag == "LOG_FOOD":
            _parse_log("food", inner)
        elif tag == "LOG_DRINK":
            _parse_log("drink", inner)
        elif tag == "ADJUST_LAST":
            _parse_log("adjust", inner)
        elif tag == "DELETE_TODAY":
            val = inner.lower()
            if "drink" in val or "hydration" in val or "water" in val:
                delete_today = "drink"
            elif "food" in val or "meal" in val or "nutrition" in val:
                delete_today = "food"
            else:
                delete_today = "all"
            log.info("delete-today requested: %s", delete_today)

    kept: list[str] = []
    cursor = 0
    for tag, inner, start, end in _scan_directives(text):
        kept.append(text[cursor:start])
        cursor = end
        _handle(tag, inner)
    kept.append(text[cursor:])
    return "".join(kept).strip(), plan_request, delete_kind, logs, delete_today, failures


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    DEFAULT_USER_ID = "U1068a1b9c15b44e7ff1439bdefdeb5dc"

    message = sys.argv[1] if len(sys.argv) > 1 else "How did I sleep last night?"
    print(f"You: {message}\n")
    reply, _, extra_flex = handle_message(DEFAULT_USER_ID, message)
    print(f"Coach: {reply}")
    for f in extra_flex:
        print(f"[flex] {f.alt_text}")
