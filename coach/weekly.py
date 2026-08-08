"""Weekly report: comprehensive health summary delivered every Sunday.

Run manually:  python -m coach.weekly
Also invoked by the scheduler on Sundays at 9:00am local time.
"""

import json
import logging
from datetime import datetime, timedelta

from coach import db
from coach import gemini
from coach.config import GEMINI_API_KEY as DEFAULT_GEMINI_KEY, TZ
from coach.flex import (build_weekly_report_bubble, trend_chip, report_labels,
                        REPORT_LABELS, COLOR_WEEKLY)
from coach.line import send_messages, flex_message, LineError
from coach.stats import build_trends
from coach.sync import run_sync

log = logging.getLogger(__name__)

WEEKLY_SYSTEM_PROMPT = """\
You are a personal health coach writing the "Key Insight" narrative for a
weekly report delivered via LINE. The user's weekly numbers — a 7-day steps
chart and their step / sleep / resting-HR / active-minute averages with
week-over-week trends — are ALREADY shown to them as visual cards above your
text. So do NOT restate totals or list metrics back.

Write 3-5 short sentences that:
1. Open with one line of genuine encouragement grounded in the week.
2. Name the single most useful PATTERN you see (e.g. "your best step days
   line up with nights you slept 7h+", or a consistency/recovery observation)
   — interpretation the raw numbers alone don't give.
3. End with ONE concrete focus for next week.

Rules:
- Respond in the user's preferred language (check coach_memory, default English).
- LINE has no markdown. Plain prose sentences only — no section headers, no
  emoji headers, no bullet lists, no 「」callouts.
- You MAY cite at most one or two specific numbers if they sharpen the insight,
  but never enumerate the weekly stats. Keep it under 500 characters.
- No medical advice. Always finish your sentences.
"""


def build_weekly_snapshot(user_id: str) -> dict:
    """Build a comprehensive 7-day snapshot for the weekly report."""
    tz = db.user_tz(db.get_user(user_id))
    today = datetime.now(tz).date()
    week_start = today - timedelta(days=7)

    snapshot = {
        "report_date": today.isoformat(),
        "week_range": f"{week_start.isoformat()} to {(today - timedelta(days=1)).isoformat()}",
        "timezone": str(tz),
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
        start_local = datetime.fromisoformat(row["start"].replace("Z", "+00:00")).astimezone(tz)
        end_local = datetime.fromisoformat(row["end"].replace("Z", "+00:00")).astimezone(tz)

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
    user = db.get_user(user_id)
    api_key = (user.get("gemini_api_key") if user else None) or DEFAULT_GEMINI_KEY
    if not api_key:
        raise RuntimeError("No Gemini API key configured")

    if snapshot is None:
        snapshot = build_weekly_snapshot(user_id)

    language = db.get_user_language(user_id)
    user_message = (
        "Here is my complete health data for the past week (the totals, "
        "averages and a steps chart are already shown to me as cards — write "
        "only the coaching insight):\n\n"
        f"```json\n{json.dumps(snapshot, separators=(',', ':'))}\n```\n\n"
        f"Write my weekly 'Key Insight' narrative in {language}."
    )

    text = gemini.generate(
        api_key, contents=user_message, system_instruction=WEEKLY_SYSTEM_PROMPT,
        max_output_tokens=1536, min_chars=40,
    )

    with db.connect() as conn:
        conn.execute(
            "INSERT INTO insights (user_id, ts, kind, content, delivered) VALUES (?, datetime('now'), 'weekly_report', ?, 0)",
            (user_id, text),
        )
    return text


def _fmt_range(week_range: str, labels: dict) -> str:
    """'2026-07-28 to 2026-08-03' → localized '28 Jul – 3 Aug' / '28 ก.ค. – 3 ส.ค.'."""
    def one(iso: str) -> str:
        d = datetime.fromisoformat(iso)
        return f"{d.day} {labels['months'][d.month - 1]}"
    try:
        a, b = week_range.split(" to ")
        return f"{one(a)} – {one(b)}"
    except (ValueError, AttributeError):
        return week_range or ""


def _weekly_view_model(user_id: str, snapshot: dict, labels: dict) -> dict:
    """Deterministic weekly stats: a 7-day steps series + averages with
    week-over-week trend chips. Gemini contributes only the narrative."""
    trends = build_trends(user_id)
    tz = db.user_tz(db.get_user(user_id))
    today = datetime.now(tz).date()
    daily = snapshot.get("daily_metrics", {})

    steps_series: list[tuple[str, float]] = []
    for i in range(7, 0, -1):
        d = today - timedelta(days=i)
        steps = (daily.get(d.isoformat()) or {}).get("steps", 0) or 0
        # Compact axis tick: English first-letter, Thai short weekday (no dot).
        tick = labels["weekdays"][d.weekday()].rstrip(".")
        steps_series.append((tick[0] if labels is REPORT_LABELS["en"] else tick, steps))

    def avg_row(label, metric, fmt, higher_is_better):
        m = trends.get(metric) or {}
        wa = m.get("week_avg")
        if wa is None:
            return None
        return (label, fmt(wa), trend_chip(m.get("trend"), higher_is_better))

    average_rows = [r for r in (
        avg_row(labels["steps_per_day"], "steps", lambda v: f"{int(round(v)):,}", True),
        avg_row(labels["sleep_per_night"], "sleep_hours", lambda v: f"{round(v, 1)}h", True),
        avg_row(labels["resting_hr"], "resting_hr", lambda v: f"{int(round(v))} bpm", False),
        avg_row(labels["azm"], "active_zone_min", lambda v: f"{int(round(v))}", True),
    ) if r]

    return {
        "range_label": _fmt_range(snapshot.get("week_range", ""), labels),
        "steps_series": steps_series,
        "average_rows": average_rows,
    }


def run_weekly_report(user_id: str) -> str:
    """Full weekly flow: generate report and send via LINE."""
    db.init_db()

    # Sync latest data so the snapshot is fresh
    log.info("refreshing health data before weekly report...")
    try:
        run_sync(user_id)
    except Exception:
        log.exception("sync failed before weekly report — proceeding with stale data")

    # Build the snapshot once; derive deterministic stats + the narrative
    snapshot = build_weekly_snapshot(user_id)
    labels = report_labels(db.get_user_language(user_id))
    vm = _weekly_view_model(user_id, snapshot, labels)

    log.info("generating weekly narrative...")
    message = generate_weekly_report(user_id, snapshot)
    log.info("weekly narrative generated (%d chars)", len(message))

    try:
        bubble = build_weekly_report_bubble(
            color=COLOR_WEEKLY,
            range_label=vm["range_label"],
            steps_series=vm["steps_series"],
            average_rows=vm["average_rows"],
            narrative=message,
            labels=labels,
        )
        send_messages([flex_message("📊 Your weekly health report is ready", bubble)], to=user_id)
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
