"""Weekly report: comprehensive health summary delivered every Sunday.

Run manually:  python -m coach.weekly
Also invoked by the scheduler on Sundays at 9:00am local time.
"""

import json
import logging
from datetime import datetime, timedelta

from google import genai

from coach import db
from coach.config import GEMINI_API_KEY as DEFAULT_GEMINI_KEY, GEMINI_MODEL, GEMINI_FALLBACK_MODELS, TZ
from coach.line import send_text, LineError

log = logging.getLogger(__name__)

WEEKLY_SYSTEM_PROMPT = """\
You are a personal health coach delivering a weekly health report via LINE messaging.
You receive a full week of health data and should provide a comprehensive yet readable summary.

Structure your report like this:
1. A brief celebratory or encouraging opening
2. Weekly totals and averages (steps, calories, active minutes)
3. Sleep quality summary (average duration, consistency, deep/REM trends)
4. Heart rate & recovery trends
5. Goal progress (if goals are set)
6. One key insight or pattern you noticed
7. Focus suggestion for next week

Guidelines:
- Respond in the same language the user prefers (check coach memory for language preference)
- LINE does NOT support markdown. Use emoji as section headers (🚶❤️🛌🎯📊) and「」to highlight key numbers
- One blank line between sections for readability
- Keep it informative but readable — around 800-1200 characters
- Be specific with data, show comparisons (this week vs last week if available)
- End with an encouraging note
- Always complete your sentences
"""


def build_weekly_snapshot(user_id: str) -> dict:
    """Build a comprehensive 7-day snapshot for the weekly report."""
    today = datetime.now(TZ).date()
    week_start = today - timedelta(days=7)

    snapshot = {
        "report_date": today.isoformat(),
        "week_range": f"{week_start.isoformat()} to {(today - timedelta(days=1)).isoformat()}",
        "timezone": str(TZ),
        "daily_metrics": {},
        "sleep_sessions": [],
        "goals": {},
        "coach_memory": {},
    }

    # Daily metrics
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT day, data_type, value_json FROM metrics WHERE user_id = ? AND day >= ? AND day < ? ORDER BY day",
            (user_id, week_start.isoformat(), today.isoformat()),
        ).fetchall()

    for row in rows:
        day = row["day"]
        if day not in snapshot["daily_metrics"]:
            snapshot["daily_metrics"][day] = {}
        value = json.loads(row["value_json"])
        data_type = row["data_type"]

        if data_type == "steps":
            snapshot["daily_metrics"][day]["steps"] = int(value.get("steps", {}).get("countSum", 0))
        elif data_type == "total-calories":
            snapshot["daily_metrics"][day]["calories"] = round(value.get("totalCalories", {}).get("kcalSum", 0))
        elif data_type == "daily-resting-heart-rate":
            snapshot["daily_metrics"][day]["resting_hr"] = int(value.get("dailyRestingHeartRate", {}).get("beatsPerMinute", 0))
        elif data_type == "active-zone-minutes":
            azm = value.get("activeZoneMinutes", {})
            snapshot["daily_metrics"][day]["active_zone_min"] = (
                int(azm.get("sumInFatBurnHeartZone", 0))
                + int(azm.get("sumInCardioHeartZone", 0))
                + int(azm.get("sumInPeakHeartZone", 0))
            )

    # Sleep sessions
    with db.connect() as conn:
        sleep_rows = conn.execute(
            "SELECT start, end, stages_json FROM sleep_sessions WHERE user_id = ? AND start >= ? ORDER BY start",
            (user_id, week_start.isoformat()),
        ).fetchall()

    for row in sleep_rows:
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

        total_min = sum(totals.values())
        start_local = datetime.fromisoformat(row["start"].replace("Z", "+00:00")).astimezone(TZ)
        end_local = datetime.fromisoformat(row["end"].replace("Z", "+00:00")).astimezone(TZ)

        snapshot["sleep_sessions"].append({
            "date": start_local.strftime("%Y-%m-%d"),
            "bedtime": start_local.strftime("%H:%M"),
            "wake": end_local.strftime("%H:%M"),
            "total_hours": round(total_min / 60, 1),
            "deep_min": round(totals["DEEP"]),
            "rem_min": round(totals["REM"]),
        })

    # Goals
    with db.connect() as conn:
        goal_rows = conn.execute(
            "SELECT key, value_json FROM goals WHERE user_id = ?",
            (user_id,),
        ).fetchall()
    snapshot["goals"] = {row["key"]: json.loads(row["value_json"]) for row in goal_rows}

    # Coach memory
    with db.connect() as conn:
        memory_rows = conn.execute(
            "SELECT name, content FROM coach_memory WHERE user_id = ? ORDER BY updated_at DESC LIMIT 10",
            (user_id,),
        ).fetchall()
    snapshot["coach_memory"] = {row["name"]: row["content"] for row in memory_rows}

    return snapshot


def generate_weekly_report(user_id: str, snapshot: dict | None = None) -> str:
    """Generate the weekly report using Gemini."""
    import time

    user = db.get_user(user_id)
    api_key = (user.get("gemini_api_key") if user else None) or DEFAULT_GEMINI_KEY
    if not api_key:
        raise RuntimeError("No Gemini API key configured")

    if snapshot is None:
        snapshot = build_weekly_snapshot(user_id)

    client = genai.Client(api_key=api_key)

    user_message = (
        "Here is my complete health data for the past week:\n\n"
        f"```json\n{json.dumps(snapshot, separators=(',', ':'))}\n```\n\n"
        "Generate my weekly health report."
    )

    models_to_try = [GEMINI_MODEL] + GEMINI_FALLBACK_MODELS
    for model in models_to_try:
        for attempt in range(3):
            try:
                response = client.models.generate_content(
                    model=model,
                    contents=user_message,
                    config=genai.types.GenerateContentConfig(
                        system_instruction=WEEKLY_SYSTEM_PROMPT,
                        max_output_tokens=4096,
                        thinking_config=genai.types.ThinkingConfig(
                            thinking_budget=0,
                        ),
                    ),
                )
                text = response.text
                if text and len(text) > 100:
                    with db.connect() as conn:
                        conn.execute(
                            "INSERT INTO insights (user_id, ts, kind, content, delivered) VALUES (?, datetime('now'), 'weekly_report', ?, 0)",
                            (user_id, text),
                        )
                    return text
            except Exception as e:
                if "503" in str(e) or "UNAVAILABLE" in str(e):
                    time.sleep(2 ** attempt)
                    continue
                elif "404" in str(e) or "NOT_FOUND" in str(e):
                    break
                else:
                    raise

    raise RuntimeError("Failed to generate weekly report — all models unavailable")


def run_weekly_report(user_id: str) -> str:
    """Full weekly flow: generate report and send via LINE."""
    db.init_db()

    log.info("generating weekly report...")
    message = generate_weekly_report(user_id)
    log.info("weekly report generated (%d chars)", len(message))

    try:
        send_text(message, to=user_id)
        log.info("weekly report sent via LINE")
        with db.connect() as conn:
            conn.execute(
                """
                UPDATE insights SET delivered = 1
                WHERE rowid = (
                    SELECT rowid FROM insights
                    WHERE user_id = ? AND kind = 'weekly_report' AND delivered = 0
                    ORDER BY ts DESC LIMIT 1
                )
                """,
                (user_id,),
            )
    except LineError as e:
        log.error("LINE delivery failed: %s", e)

    return message


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    DEFAULT_USER_ID = "U1068a1b9c15b44e7ff1439bdefdeb5dc"
    print(run_weekly_report(DEFAULT_USER_ID))
