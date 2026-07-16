"""Adaptive workout plan management.

Plans are stored in the goals table and tracked daily.
The chat agent can create plans, and the daily summary references them.

Run manually:  python -m coach.plans
"""

import json
import logging
from datetime import datetime, timedelta

from coach import db
from coach import gemini
from coach.config import GEMINI_API_KEY as DEFAULT_GEMINI_KEY, TZ

log = logging.getLogger(__name__)

PLAN_SYSTEM_PROMPT = """\
You are a personal fitness coach creating a workout plan.
Based on the user's health data, goals, and preferences, create a practical multi-week plan.

Output the plan as a JSON object with this structure:
{
  "name": "plan name",
  "duration_weeks": number,
  "goal": "what this plan targets",
  "schedule": [
    {"day": "Monday", "workout": "description", "duration_min": number},
    ...
  ],
  "notes": "any important notes or progression tips"
}

Guidelines:
- Make it realistic based on their current activity level
- Include rest days
- Progress gradually (don't overload week 1)
- Consider their resting heart rate for intensity suggestions
- Keep descriptions concise but actionable
"""


def create_workout_plan(user_id: str, user_request: str, context: dict) -> dict:
    """Generate a workout plan based on user request and health context.

    Returns the plan dict and saves it to the goals table.
    """
    user = db.get_user(user_id)
    api_key = (user.get("gemini_api_key") if user else None) or DEFAULT_GEMINI_KEY
    if not api_key:
        raise RuntimeError("No Gemini API key configured")

    language = db.get_user_language(user_id)
    prompt = (
        f"User request: {user_request}\n\n"
        f"Health context:\n```json\n{json.dumps(context, indent=2)}\n```\n\n"
        "Create a workout plan as a JSON object. "
        f"Keep the JSON keys in English, but write all human-readable text "
        f"values (exercise names, descriptions, notes) in {language}."
    )

    text = gemini.generate(
        api_key, contents=prompt, system_instruction=PLAN_SYSTEM_PROMPT,
        max_output_tokens=2048,
    )
    plan = _extract_json(text)
    if plan:
        save_plan(user_id, plan)
        return plan
    raise RuntimeError("Failed to parse workout plan from model response")


def _extract_json(text: str) -> dict | None:
    """Extract JSON object from model response (may contain markdown code blocks).

    Robust against missing closing fences — falls back to brace matching.
    """
    if not text:
        return None

    # Strip markdown code fences if present
    if "```json" in text:
        start = text.find("```json") + 7
        end = text.find("```", start)
        if end > start:
            text = text[start:end].strip()
    elif "```" in text:
        start = text.find("```") + 3
        end = text.find("```", start)
        if end > start:
            text = text[start:end].strip()

    try:
        return json.loads(text)
    except (json.JSONDecodeError, ValueError):
        # Try to find JSON object by brace matching
        brace_start = text.find("{")
        brace_end = text.rfind("}") + 1
        if brace_start >= 0 and brace_end > brace_start:
            try:
                return json.loads(text[brace_start:brace_end])
            except (json.JSONDecodeError, ValueError):
                pass
    return None


def save_plan(user_id: str, plan: dict) -> None:
    """Save a workout plan to the goals table."""
    plan["created"] = datetime.now(TZ).isoformat()
    plan["week"] = 1
    plan["completed_days"] = []

    with db.connect() as conn:
        conn.execute(
            """
            INSERT INTO goals (user_id, key, value_json, updated_at)
            VALUES (?, 'workout_plan', ?, datetime('now'))
            ON CONFLICT(user_id, key) DO UPDATE SET value_json = excluded.value_json, updated_at = datetime('now')
            """,
            (user_id, json.dumps(plan)),
        )
    log.info("saved workout plan: %s (%d weeks)", plan.get("name", "unnamed"), plan.get("duration_weeks", 0))


def get_current_plan(user_id: str) -> dict | None:
    """Load the current workout plan."""
    with db.connect() as conn:
        row = conn.execute(
            "SELECT value_json FROM goals WHERE user_id = ? AND key = 'workout_plan'",
            (user_id,),
        ).fetchone()
    if row:
        return json.loads(row["value_json"])
    return None


def get_today_workout(user_id: str) -> str | None:
    """Get today's scheduled workout from the active plan."""
    plan = get_current_plan(user_id)
    if not plan:
        return None

    schedule = plan.get("schedule", [])
    if not schedule:
        return None

    today_name = datetime.now(TZ).strftime("%A")
    for entry in schedule:
        if entry.get("day", "").lower() == today_name.lower():
            workout = entry.get("workout", "")
            duration = entry.get("duration_min", "")
            if workout:
                return f"{workout} ({duration} min)" if duration else workout
    return None


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    db.init_db()

    DEFAULT_USER_ID = "U1068a1b9c15b44e7ff1439bdefdeb5dc"

    plan = get_current_plan(DEFAULT_USER_ID)
    if plan:
        print(f"Current plan: {plan.get('name')}")
        print(f"Week: {plan.get('week')}")
        print(f"Schedule:")
        for entry in plan.get("schedule", []):
            print(f"  {entry.get('day')}: {entry.get('workout')} ({entry.get('duration_min')} min)")
    else:
        print("No active plan.")

    today = get_today_workout(DEFAULT_USER_ID)
    if today:
        print(f"\nToday's workout: {today}")
