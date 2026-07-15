"""Rule-triggered nudge engine.

Runs hourly. Each rule checks recent health data and returns a nudge
condition (short context string) or None. When a condition fires, Gemini
generates the actual message — rules decide *when* to speak, AI decides
*what* to say.

Constraints:
- Max 3 nudges per day
- Quiet hours: 22:00–07:00 local time (no messages)
- A nudge type won't re-fire within 6 hours of the same type

Run manually:  python -m coach.nudges
"""

import json
import logging
from datetime import datetime, timedelta, timezone

from google import genai

from coach import db
from coach.config import GEMINI_API_KEY, GEMINI_MODEL, GEMINI_FALLBACK_MODELS, TZ
from coach.line import send_text, LineError

log = logging.getLogger(__name__)

MAX_NUDGES_PER_DAY = 3
QUIET_HOUR_START = 22  # 10 PM
QUIET_HOUR_END = 7     # 7 AM
NUDGE_COOLDOWN_HOURS = 6  # same nudge type won't fire within this window

NUDGE_SYSTEM_PROMPT = """\
You are a personal health coach sending a brief nudge via LINE messaging.
You're given a specific condition that triggered this message.

Guidelines:
- Be encouraging, not nagging. One short paragraph only.
- Respond in the user's preferred language (default English if unknown).
- LINE does NOT support markdown. Use emoji for emphasis (1-2 max) and「」to highlight numbers.
- Keep it under 300 characters — it should feel like a quick tap on the shoulder.
- Be specific about the data that triggered this nudge.
- End with a simple actionable suggestion.
- Always complete your sentences.
"""


# ---------------------------------------------------------------------------
# Rules — each returns a dict {"type": str, "condition": str} or None
# ---------------------------------------------------------------------------

def _rule_low_steps(now: datetime) -> dict | None:
    """Fire if it's afternoon (14:00+) and today's steps are below 3000."""
    if now.hour < 14:
        return None

    today = now.date().isoformat()
    with db.connect() as conn:
        row = conn.execute(
            "SELECT value_json FROM metrics WHERE day = ? AND data_type = 'steps'",
            (today,),
        ).fetchone()

    if not row:
        return None

    value = json.loads(row["value_json"])
    steps = value.get("steps", {}).get("countSum")
    if steps is None:
        return None

    steps = int(steps)
    if steps < 3000:
        return {
            "type": "low_steps",
            "condition": f"It's {now.strftime('%H:%M')} and you've only logged {steps:,} steps today. "
                         f"You usually average over 6,000.",
        }
    return None


def _rule_step_streak(now: datetime) -> dict | None:
    """Fire a positive nudge if user has hit 6000+ steps for 5+ consecutive days."""
    if now.hour < 18:
        return None

    today = now.date()
    streak = 0
    for i in range(1, 8):  # check last 7 days (not today)
        day = (today - timedelta(days=i)).isoformat()
        with db.connect() as conn:
            row = conn.execute(
                "SELECT value_json FROM metrics WHERE day = ? AND data_type = 'steps'",
                (day,),
            ).fetchone()
        if not row:
            break
        value = json.loads(row["value_json"])
        steps = int(value.get("steps", {}).get("countSum", 0))
        if steps >= 6000:
            streak += 1
        else:
            break

    if streak >= 5:
        return {
            "type": "step_streak",
            "condition": f"You've hit 6,000+ steps for {streak} days in a row! Celebrate the streak.",
        }
    return None


def _rule_high_resting_hr(now: datetime) -> dict | None:
    """Fire if today's resting HR is 5+ bpm above the 7-day average."""
    today = now.date()
    with db.connect() as conn:
        rows = conn.execute(
            """
            SELECT day, value_json FROM metrics
            WHERE data_type = 'daily-resting-heart-rate'
            ORDER BY day DESC LIMIT 7
            """,
        ).fetchall()

    if len(rows) < 3:
        return None

    bpms = []
    today_bpm = None
    for row in rows:
        value = json.loads(row["value_json"])
        bpm = value.get("dailyRestingHeartRate", {}).get("beatsPerMinute")
        if bpm:
            bpm = int(bpm)
            bpms.append(bpm)
            if row["day"] == today.isoformat():
                today_bpm = bpm

    if today_bpm is None or len(bpms) < 3:
        return None

    avg = sum(bpms) / len(bpms)
    if today_bpm >= avg + 5:
        return {
            "type": "high_resting_hr",
            "condition": f"Your resting heart rate today is {today_bpm} bpm, which is {today_bpm - avg:.0f} bpm "
                         f"above your recent average of {avg:.0f}. This could mean you need extra recovery.",
        }
    return None


def _rule_bedtime_reminder(now: datetime) -> dict | None:
    """Fire at 21:00-21:30 if user's average bedtime is around 22:00."""
    if not (21 <= now.hour <= 21 and now.minute < 30):
        return None

    # Check recent sleep start times
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT start FROM sleep_sessions ORDER BY start DESC LIMIT 5"
        ).fetchall()

    if len(rows) < 2:
        return None

    # Calculate average bedtime hour (in local time)
    bed_hours = []
    for row in rows:
        try:
            start = datetime.fromisoformat(row["start"].replace("Z", "+00:00"))
            local_start = start.astimezone(TZ)
            bed_hours.append(local_start.hour + local_start.minute / 60)
        except (ValueError, TypeError):
            continue

    if not bed_hours:
        return None

    avg_bed = sum(bed_hours) / len(bed_hours)
    # Only nudge if they typically sleep between 21:30 and 23:30
    if 21.5 <= avg_bed <= 23.5:
        return {
            "type": "bedtime_reminder",
            "condition": f"Based on your sleep data, you usually fall asleep around "
                         f"{int(avg_bed)}:{int((avg_bed % 1) * 60):02d}. "
                         f"Time to start winding down for quality sleep.",
        }
    return None


# All rules to evaluate
RULES = [
    _rule_low_steps,
    _rule_step_streak,
    _rule_high_resting_hr,
    _rule_bedtime_reminder,
]


# ---------------------------------------------------------------------------
# Rate limiting and quiet hours
# ---------------------------------------------------------------------------

def _is_quiet_hours(now: datetime) -> bool:
    """Check if current time is within quiet hours."""
    return now.hour >= QUIET_HOUR_START or now.hour < QUIET_HOUR_END


def _utc_str(dt: datetime) -> str:
    """Format a datetime as a UTC string matching SQLite's datetime('now')."""
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _nudges_sent_today(now: datetime) -> int:
    """Count nudges already sent today (local day, compared in UTC)."""
    # Local midnight, converted to a UTC string to match how ts is stored
    local_midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    today_start = _utc_str(local_midnight)
    with db.connect() as conn:
        row = conn.execute(
            "SELECT COUNT(*) as cnt FROM insights WHERE kind = 'nudge' AND ts >= ?",
            (today_start,),
        ).fetchone()
    return row["cnt"] if row else 0


def _recently_sent(nudge_type: str, now: datetime) -> bool:
    """Check if this nudge type was sent within the cooldown window."""
    cutoff = _utc_str(now - timedelta(hours=NUDGE_COOLDOWN_HOURS))
    with db.connect() as conn:
        row = conn.execute(
            "SELECT COUNT(*) as cnt FROM insights WHERE kind = 'nudge' AND content LIKE ? AND ts >= ?",
            (f'%"type": "{nudge_type}"%', cutoff),
        ).fetchone()
    return (row["cnt"] if row else 0) > 0


# ---------------------------------------------------------------------------
# Nudge generation and delivery
# ---------------------------------------------------------------------------

def _generate_nudge_message(condition: str) -> str:
    """Use Gemini to turn a condition into a friendly nudge message."""
    import time as _time

    client = genai.Client(api_key=GEMINI_API_KEY)
    user_message = f"Nudge condition: {condition}\n\nGenerate a brief, friendly nudge message."

    models_to_try = [GEMINI_MODEL] + GEMINI_FALLBACK_MODELS
    for model in models_to_try:
        for attempt in range(3):
            try:
                response = client.models.generate_content(
                    model=model,
                    contents=user_message,
                    config=genai.types.GenerateContentConfig(
                        system_instruction=NUDGE_SYSTEM_PROMPT,
                        max_output_tokens=1024,
                        thinking_config=genai.types.ThinkingConfig(
                            thinking_budget=0,
                        ),
                    ),
                )
                text = response.text
                if text and len(text) > 20:
                    return text
            except Exception as e:
                if "503" in str(e) or "UNAVAILABLE" in str(e):
                    _time.sleep(2 ** attempt)
                    continue
                elif "404" in str(e) or "NOT_FOUND" in str(e):
                    break
                else:
                    raise

    # Fallback: just send the condition as-is
    return f"💡 {condition}"


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def run_nudge_check() -> str | None:
    """Evaluate all rules and send a nudge if one fires.

    Returns the sent message text, or None if no nudge was sent.
    """
    db.init_db()
    now = datetime.now(TZ)

    # Guard: quiet hours
    if _is_quiet_hours(now):
        log.info("quiet hours — skipping nudge check")
        return None

    # Guard: daily limit
    sent_today = _nudges_sent_today(now)
    if sent_today >= MAX_NUDGES_PER_DAY:
        log.info("daily nudge limit reached (%d/%d) — skipping", sent_today, MAX_NUDGES_PER_DAY)
        return None

    # Evaluate rules
    for rule_fn in RULES:
        try:
            result = rule_fn(now)
        except Exception:
            log.exception("rule %s failed", rule_fn.__name__)
            continue

        if result is None:
            continue

        nudge_type = result["type"]
        condition = result["condition"]

        # Check cooldown
        if _recently_sent(nudge_type, now):
            log.info("nudge '%s' on cooldown — skipping", nudge_type)
            continue

        # Generate and send
        log.info("nudge triggered: %s", nudge_type)
        message = _generate_nudge_message(condition)

        # Store
        with db.connect() as conn:
            conn.execute(
                "INSERT INTO insights (ts, kind, content, delivered) VALUES (datetime('now'), 'nudge', ?, 0)",
                (json.dumps({"type": nudge_type, "condition": condition, "message": message}),),
            )

        # Deliver
        try:
            send_text(message)
            log.info("nudge sent via LINE: %s", message[:80])
            with db.connect() as conn:
                conn.execute(
                    """
                    UPDATE insights SET delivered = 1
                    WHERE rowid = (
                        SELECT rowid FROM insights
                        WHERE kind = 'nudge' AND delivered = 0
                        ORDER BY ts DESC LIMIT 1
                    )
                    """,
                )
        except LineError as e:
            log.error("nudge delivery failed: %s", e)

        return message

    log.info("no nudge conditions triggered")
    return None


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    result = run_nudge_check()
    if result:
        print(f"Nudge sent: {result}")
    else:
        print("No nudge triggered.")
