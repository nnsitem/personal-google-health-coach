"""AI coach engine — builds health snapshots and generates coaching messages via Gemini.

The snapshot is a compact JSON summary of recent health data. Gemini receives it
along with a coaching system prompt and returns a WhatsApp-formatted message.
"""

import json
import logging
from datetime import date, datetime, timedelta

from coach import db
from coach import gemini
from coach.config import GEMINI_API_KEY as DEFAULT_GEMINI_KEY, TZ

log = logging.getLogger(__name__)

SYSTEM_PROMPT = """\
You are a personal health coach delivering a daily morning briefing via LINE messaging.
Your user wears a Fitbit and you have access to their recent health data.

Guidelines:
- Be warm, concise, and encouraging. No medical advice.
- Respond in the user's preferred language (check coach_memory for language preference, default to English).

Formatting rules (LINE does NOT support markdown/bold/italic):
- Start with a greeting emoji line (e.g. 🌅 Good morning!)
- Use emoji as section headers on their own line:
  🛌 Sleep
  🚶 Activity
  ❤️ Recovery
  🎯 Today's Focus
- Use「」to highlight key numbers (e.g. 「7,904 steps」)
- One blank line between sections
- Use ✅ for positive achievements, 📌 for suggestions
- Keep total message under 800 characters
- End with a short motivational line or question

Reference actual numbers from the data. Don't invent stats.
If data is missing for a metric, skip it gracefully.
If "todays_workout" is present in the snapshot, mention it in the 🎯 Today's Focus section.
Use the "trends" data (week_avg, month_avg, and week-over-week trend) to give
context — e.g. "your steps are up 12% vs last week" or "resting HR is steady
against your monthly average". This shows you understand the user's patterns.
"""


def build_daily_snapshot(user_id: str) -> dict:
    """Query SQLite for the last 7 days of health data and return a compact snapshot."""
    tz = db.user_tz(db.get_user(user_id))
    today = datetime.now(tz).date()
    days_back = 7

    snapshot = {
        "today": today.isoformat(),
        "timezone": str(tz),
        "metrics": {},
        "sleep": [],
    }

    # Fetch daily metrics
    with db.connect() as conn:
        rows = conn.execute(
            """
            SELECT day, data_type, value_json
            FROM metrics
            WHERE user_id = ? AND day >= ?
            ORDER BY day DESC
            """,
            (user_id, (today - timedelta(days=days_back)).isoformat()),
        ).fetchall()

    for row in rows:
        day = row["day"]
        data_type = row["data_type"]
        value = json.loads(row["value_json"])

        if day not in snapshot["metrics"]:
            snapshot["metrics"][day] = {}

        # Extract the meaningful values from the API response
        extracted = _extract_metric_value(data_type, value)
        if extracted:
            snapshot["metrics"][day][data_type] = extracted

    # Fetch sleep sessions
    with db.connect() as conn:
        sleep_rows = conn.execute(
            """
            SELECT start, end, stages_json, efficiency, score
            FROM sleep_sessions
            WHERE user_id = ? AND start >= ?
            ORDER BY start DESC
            """,
            (user_id, (today - timedelta(days=days_back)).isoformat()),
        ).fetchall()

    for row in sleep_rows:
        stages = json.loads(row["stages_json"]) if row["stages_json"] else []
        duration_info = _summarize_sleep_stages(stages)
        snapshot["sleep"].append({
            "start": row["start"],
            "end": row["end"],
            "duration_hours": duration_info["total_hours"],
            "deep_min": duration_info["deep_min"],
            "light_min": duration_info["light_min"],
            "rem_min": duration_info["rem_min"],
            "awake_min": duration_info["awake_min"],
            "efficiency": row["efficiency"],
            "score": row["score"],
        })

    # Load coach memory for personalization
    with db.connect() as conn:
        memory_rows = conn.execute(
            "SELECT name, content FROM coach_memory WHERE user_id = ? ORDER BY updated_at DESC LIMIT 10",
            (user_id,),
        ).fetchall()

    if memory_rows:
        snapshot["coach_memory"] = {row["name"]: row["content"] for row in memory_rows}

    # Load active goals
    with db.connect() as conn:
        goal_rows = conn.execute(
            "SELECT key, value_json FROM goals WHERE user_id = ?",
            (user_id,),
        ).fetchall()

    if goal_rows:
        snapshot["goals"] = {row["key"]: json.loads(row["value_json"]) for row in goal_rows}

    # Include today's scheduled workout from the active plan, if any
    try:
        from coach.plans import get_today_workout
        today_workout = get_today_workout(user_id)
        if today_workout:
            snapshot["todays_workout"] = today_workout
    except Exception:
        pass

    # Include multi-window trends (week/month averages + week-over-week) so the
    # brief can reference patterns, not just yesterday's numbers.
    try:
        from coach.stats import build_trends
        snapshot["trends"] = build_trends(user_id)
    except Exception:
        pass

    return snapshot


def _extract_metric_value(data_type: str, value: dict) -> dict | None:
    """Pull out the meaningful number(s) from a raw API response point."""
    if data_type == "steps":
        steps_data = value.get("steps", {})
        count = steps_data.get("countSum")
        if count:
            return {"count": int(count)}
    elif data_type == "total-calories":
        cal_data = value.get("totalCalories", {})
        kcal = cal_data.get("kcalSum")
        if kcal:
            return {"kcal": round(kcal)}
    elif data_type == "active-zone-minutes":
        azm_data = value.get("activeZoneMinutes", {})
        return {
            "fat_burn": int(azm_data.get("sumInFatBurnHeartZone", 0)),
            "cardio": int(azm_data.get("sumInCardioHeartZone", 0)),
            "peak": int(azm_data.get("sumInPeakHeartZone", 0)),
            "total": (
                int(azm_data.get("sumInFatBurnHeartZone", 0))
                + int(azm_data.get("sumInCardioHeartZone", 0))
                + int(azm_data.get("sumInPeakHeartZone", 0))
            ),
        }
    elif data_type == "daily-resting-heart-rate":
        rhr_data = value.get("dailyRestingHeartRate", {})
        bpm = rhr_data.get("beatsPerMinute")
        if bpm:
            return {"bpm": int(bpm)}
    return None


def _summarize_sleep_stages(stages: list[dict]) -> dict:
    """Calculate total duration and per-stage minutes from sleep stage data."""
    totals = {"DEEP": 0, "LIGHT": 0, "REM": 0, "AWAKE": 0}

    for stage in stages:
        start_str = stage.get("startTime", "")
        end_str = stage.get("endTime", "")
        stage_type = stage.get("type", "")
        if not (start_str and end_str and stage_type):
            continue
        try:
            start = datetime.fromisoformat(start_str.replace("Z", "+00:00"))
            end = datetime.fromisoformat(end_str.replace("Z", "+00:00"))
            minutes = (end - start).total_seconds() / 60
            if stage_type in totals:
                totals[stage_type] += minutes
        except (ValueError, TypeError):
            continue

    total_min = sum(totals.values())
    return {
        "total_hours": round(total_min / 60, 1),
        "deep_min": round(totals["DEEP"]),
        "light_min": round(totals["LIGHT"]),
        "rem_min": round(totals["REM"]),
        "awake_min": round(totals["AWAKE"]),
    }


def generate_daily_summary(user_id: str, snapshot: dict | None = None) -> str:
    """Generate a daily coaching message using Gemini.

    Returns the message text ready for LINE delivery.
    Tries the primary model, then fallbacks if unavailable.
    """
    user = db.get_user(user_id)
    api_key = (user.get("gemini_api_key") if user else None) or DEFAULT_GEMINI_KEY
    if not api_key:
        raise RuntimeError("No Gemini API key configured")

    if snapshot is None:
        snapshot = build_daily_snapshot(user_id)

    user_message = (
        "Here is my health data snapshot for today's briefing:\n\n"
        f"```json\n{json.dumps(snapshot, separators=(',', ':'))}\n```\n\n"
        "Generate my complete daily morning health briefing. "
        "Include sleep recap, activity recap, and a motivational closing. "
        "Keep it under 900 characters total."
    )

    message_text = gemini.generate(
        api_key, contents=user_message, system_instruction=SYSTEM_PROMPT,
        max_output_tokens=2048, min_chars=50,
    )

    # Store the generated insight
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO insights (user_id, ts, kind, content, delivered) VALUES (?, datetime('now'), 'daily_summary', ?, 0)",
            (user_id, message_text),
        )
    return message_text


if __name__ == "__main__":
    """Quick test: build snapshot and print it, then generate summary if API key is set."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    db.init_db()

    DEFAULT_USER_ID = "U1068a1b9c15b44e7ff1439bdefdeb5dc"

    snapshot = build_daily_snapshot(DEFAULT_USER_ID)
    print("=== SNAPSHOT ===")
    print(json.dumps(snapshot, indent=2))
    print()

    if DEFAULT_GEMINI_KEY:
        print("=== DAILY SUMMARY ===")
        summary = generate_daily_summary(DEFAULT_USER_ID, snapshot)
        print(summary)
    else:
        print("(GEMINI_API_KEY not set — skipping generation)")
