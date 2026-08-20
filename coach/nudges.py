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

from coach import db
from coach import gemini
from coach.config import GEMINI_API_KEY as DEFAULT_GEMINI_KEY, TZ
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

LOW_STEPS_THRESHOLD = 3000
# Below this, 3,000 steps isn't a shortfall for this person, so the nudge would
# be nagging them about a normal day.
LOW_STEPS_MIN_BASELINE = 4000


def _avg_steps(user_id: str, days: int = 30) -> float | None:
    """The user's own mean daily steps over the trailing `days`, or None."""
    with db.connect() as conn:
        row = conn.execute(
            """
            SELECT AVG(CAST(json_extract(value_json, '$.steps.countSum') AS REAL)) AS avg_steps
            FROM metrics
            WHERE user_id = ? AND data_type = 'steps' AND day >= date('now', ?)
              AND json_extract(value_json, '$.steps.countSum') IS NOT NULL
            """,
            (user_id, f"-{days} day"),
        ).fetchone()
    return row["avg_steps"] if row and row["avg_steps"] else None


def _rule_low_steps(user_id: str, now: datetime) -> dict | None:
    """Fire if it's afternoon (14:00+) and today's steps are below 3000."""
    if now.hour < 14:
        return None

    today = now.date().isoformat()
    with db.connect() as conn:
        row = conn.execute(
            "SELECT value_json FROM metrics WHERE user_id = ? AND day = ? AND data_type = 'steps' "
            "ORDER BY updated_at DESC",
            (user_id, today),
        ).fetchone()

    if not row:
        return None

    value = json.loads(row["value_json"])
    steps = value.get("steps", {}).get("countSum")
    if steps is None:
        return None

    steps = int(steps)
    if steps >= LOW_STEPS_THRESHOLD:
        return None

    # Quote the user's REAL baseline. This used to assert a hard-coded "you
    # usually average over 6,000" — wrong for everyone whose average isn't
    # that, and the coach stating a made-up number about the user's own
    # history undermines every other number it reports.
    baseline = _avg_steps(user_id)
    if baseline is None:
        comparison = "You have no step history yet to compare against."
    elif baseline < LOW_STEPS_MIN_BASELINE:
        return None  # 3,000 is normal for them — nothing to nudge about
    else:
        comparison = f"You average {round(baseline):,} steps a day over the past month."

    return {
        "type": "low_steps",
        "condition": f"It's {now.strftime('%H:%M')} and you've only logged {steps:,} steps today. "
                     f"{comparison}",
    }


def _rule_step_streak(user_id: str, now: datetime) -> dict | None:
    """Fire a positive nudge if user has hit 6000+ steps for 5+ consecutive days."""
    if now.hour < 18:
        return None

    today = now.date()
    streak = 0
    for i in range(1, 8):  # check last 7 days (not today)
        day = (today - timedelta(days=i)).isoformat()
        with db.connect() as conn:
            row = conn.execute(
                "SELECT value_json FROM metrics WHERE user_id = ? AND day = ? AND data_type = 'steps' "
                "ORDER BY updated_at DESC",
                (user_id, day),
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


def _rule_high_resting_hr(user_id: str, now: datetime) -> dict | None:
    """Fire if today's resting HR is 5+ bpm above the 7-day average."""
    today = now.date()
    with db.connect() as conn:
        rows = conn.execute(
            """
            SELECT day, value_json FROM metrics
            WHERE user_id = ? AND data_type = 'daily-resting-heart-rate'
            ORDER BY day DESC LIMIT 7
            """,
            (user_id,),
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


def _night_hour(local_dt: datetime) -> float:
    """Bedtime on a continuous evening scale: 22:30 → 22.5, 01:30 → 25.5.

    Falling asleep after midnight still belongs to the previous evening, so
    hours before noon are shifted past 24. Without this a plain average of raw
    clock hours puts a 23:30-and-00:30 sleeper at 12:00 (midday).
    """
    h = local_dt.hour + local_dt.minute / 60
    return h + 24 if h < 12 else h


def _fmt_night_hour(value: float) -> str:
    """Night-scale hour back to a clock label: 25.5 → '01:30'."""
    hours = int(value) % 24
    return f"{hours:02d}:{int(round((value % 1) * 60)) % 60:02d}"


def _rule_bedtime_reminder(user_id: str, now: datetime) -> dict | None:
    """Fire during the 21:00 hour if the user typically falls asleep 21:00–03:00.

    The window is the whole hour, not 21:00–21:30: the nudge check is scheduled
    at :35 past the hour (coach.main), so a `minute < 30` condition could never
    be true and this rule never fired once in production. Hour 21 is the last
    slot available anyway — quiet hours begin at 22:00 — and NUDGE_COOLDOWN_HOURS
    already prevents a repeat, so matching on the hour alone is sufficient.
    """
    if now.hour != 21:
        return None

    # Check recent sleep start times
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT start FROM sleep_sessions WHERE user_id = ? ORDER BY start DESC LIMIT 5",
            (user_id,),
        ).fetchall()

    if len(rows) < 2:
        return None

    # Calculate average bedtime hour (in the user's local time — `now` carries
    # the user's tz from run_nudge_check)
    bed_hours = []
    for row in rows:
        try:
            start = datetime.fromisoformat(row["start"].replace("Z", "+00:00"))
            local_start = start.astimezone(now.tzinfo)
            bed_hours.append(_night_hour(local_start))
        except (ValueError, TypeError):
            continue

    if not bed_hours:
        return None

    avg_bed = sum(bed_hours) / len(bed_hours)
    # Only nudge people whose typical bedtime is 21:00–03:00 on the night scale.
    # The window used to be 21.5–23.5 against the RAW local hour, so anyone
    # falling asleep after midnight averaged ~1.6 and was silently excluded —
    # i.e. the rule skipped exactly the late sleepers a wind-down reminder is
    # for. (This user averages 01:38; they could never have received it.)
    if not (21.0 <= avg_bed <= 27.0):
        return None

    return {
        "type": "bedtime_reminder",
        "condition": f"Based on your sleep data, you usually fall asleep around "
                     f"{_fmt_night_hour(avg_bed)}. It's {now.strftime('%H:%M')} now. "
                     f"Time to start winding down for quality sleep.",
    }


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


def _nudges_sent_today(user_id: str, now: datetime) -> int:
    """Count nudges already sent today (local day, compared in UTC)."""
    # Local midnight, converted to a UTC string to match how ts is stored
    local_midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    today_start = _utc_str(local_midnight)
    with db.connect() as conn:
        row = conn.execute(
            "SELECT COUNT(*) as cnt FROM insights WHERE user_id = ? AND kind = 'nudge' AND ts >= ?",
            (user_id, today_start),
        ).fetchone()
    return row["cnt"] if row else 0


def _recently_sent(user_id: str, nudge_type: str, now: datetime) -> bool:
    """Check if this nudge type was sent within the cooldown window."""
    cutoff = _utc_str(now - timedelta(hours=NUDGE_COOLDOWN_HOURS))
    with db.connect() as conn:
        row = conn.execute(
            "SELECT COUNT(*) as cnt FROM insights WHERE user_id = ? AND kind = 'nudge' AND content LIKE ? AND ts >= ?",
            (user_id, f'%"type": "{nudge_type}"%', cutoff),
        ).fetchone()
    return (row["cnt"] if row else 0) > 0


# ---------------------------------------------------------------------------
# Nudge generation and delivery
# ---------------------------------------------------------------------------

def _generate_nudge_message(user_id: str, condition: str) -> str:
    """Use Gemini to turn a condition into a friendly nudge message."""
    user = db.get_user(user_id)
    api_key = (user.get("gemini_api_key") if user else None) or DEFAULT_GEMINI_KEY
    if not api_key:
        return f"💡 {condition}"

    language = db.get_user_language(user_id)
    user_message = (
        f"Nudge condition: {condition}\n\n"
        f"Write the entire nudge message in {language}. "
        f"Generate a brief, friendly nudge message."
    )

    try:
        return gemini.generate(
            api_key, contents=user_message, system_instruction=NUDGE_SYSTEM_PROMPT,
            max_output_tokens=1024, min_chars=20,
        )
    except Exception:
        log.warning("nudge generation failed, using raw condition")
        return f"💡 {condition}"


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def run_nudge_check(user_id: str) -> str | None:
    """Evaluate all rules and send a nudge if one fires.

    Returns the sent message text, or None if no nudge was sent.
    """
    db.init_db()
    # All rule hour checks, quiet hours, and day boundaries use the USER's
    # local clock, not the server's.
    now = datetime.now(db.user_tz(db.get_user(user_id)))

    # Guard: quiet hours
    if _is_quiet_hours(now):
        log.info("quiet hours — skipping nudge check")
        return None

    # Guard: daily limit
    sent_today = _nudges_sent_today(user_id, now)
    if sent_today >= MAX_NUDGES_PER_DAY:
        log.info("daily nudge limit reached (%d/%d) — skipping", sent_today, MAX_NUDGES_PER_DAY)
        return None

    # Evaluate rules
    for rule_fn in RULES:
        try:
            result = rule_fn(user_id, now)
        except Exception:
            log.exception("rule %s failed", rule_fn.__name__)
            continue

        if result is None:
            continue

        nudge_type = result["type"]
        condition = result["condition"]

        # Check cooldown
        if _recently_sent(user_id, nudge_type, now):
            log.info("nudge '%s' on cooldown — skipping", nudge_type)
            continue

        # Generate and send
        log.info("nudge triggered: %s", nudge_type)
        message = _generate_nudge_message(user_id, condition)

        # Store
        with db.connect() as conn:
            conn.execute(
                "INSERT INTO insights (user_id, ts, kind, content, delivered) VALUES (?, datetime('now'), 'nudge', ?, 0)",
                (user_id, json.dumps({"type": nudge_type, "condition": condition, "message": message})),
            )

        # Deliver
        try:
            send_text(message, to=user_id)
            log.info("nudge sent via LINE: %s", message[:80])
            with db.connect() as conn:
                conn.execute(
                    """
                    UPDATE insights SET delivered = 1
                    WHERE rowid = (
                        SELECT rowid FROM insights
                        WHERE user_id = ? AND kind = 'nudge' AND delivered = 0
                        ORDER BY ts DESC LIMIT 1
                    )
                    """,
                    (user_id,),
                )
        except LineError as e:
            log.error("nudge delivery failed: %s", e)

        return message

    log.info("no nudge conditions triggered")
    return None


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    DEFAULT_USER_ID = "U1068a1b9c15b44e7ff1439bdefdeb5dc"
    result = run_nudge_check(DEFAULT_USER_ID)
    if result:
        print(f"Nudge sent: {result}")
    else:
        print("No nudge triggered.")
