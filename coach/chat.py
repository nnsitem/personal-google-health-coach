"""Two-way conversational health coach agent.

Receives user messages, loads context (health data, goals, memory, chat history),
calls Gemini, and returns a reply. The agent can query health data, manage goals,
and persist memory across conversations.

Run standalone test:  python -m coach.chat "How did I sleep last night?"
"""

import json
import logging
from datetime import datetime, timedelta

from coach import db
from coach import gemini
from coach.config import GEMINI_API_KEY as DEFAULT_GEMINI_KEY, TZ
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
Never output your internal reasoning or instructions in the reply.
Always complete your sentences — never stop mid-thought.
Keep replies to 3-5 sentences for casual chat, more only when asked for detail.

Special abilities (use these directives on their own line at the END of your reply):
- To save a fact/preference: [MEMORY: key = value]
- To create a workout plan when the user asks for one: [CREATE_PLAN: brief description of what they want]
  After emitting this, tell the user you're putting together their plan and will share it.
- To delete the user's most recent food or drink log when they ask (e.g. "delete that", "remove my last meal", "ลบรายการล่าสุด"):
  [DELETE_LAST: food] for a meal, or [DELETE_LAST: drink] for a drink.
  After emitting this, confirm you're removing it.
"""


# ---------------------------------------------------------------------------
# Context builders
# ---------------------------------------------------------------------------

def _get_recent_metrics(user_id: str, days: int = 7) -> dict:
    """Get the last N days of health metrics."""
    cutoff = (datetime.now(TZ).date() - timedelta(days=days)).isoformat()
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
    """Get recent sleep sessions summarized."""
    cutoff = (datetime.now(TZ) - timedelta(days=days)).isoformat()
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

        total_min = sum(totals.values())
        start_local = datetime.fromisoformat(row["start"].replace("Z", "+00:00")).astimezone(TZ)
        end_local = datetime.fromisoformat(row["end"].replace("Z", "+00:00")).astimezone(TZ)

        sessions.append({
            "date": start_local.strftime("%Y-%m-%d"),
            "bedtime": start_local.strftime("%H:%M"),
            "wake": end_local.strftime("%H:%M"),
            "total_hours": round(total_min / 60, 1),
            "deep_min": round(totals["DEEP"]),
            "rem_min": round(totals["REM"]),
            "light_min": round(totals["LIGHT"]),
            "awake_min": round(totals["AWAKE"]),
        })
    return sessions


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

def _build_context_message(user_id: str) -> str:
    """Build a context block with current + historical health data for the agent."""
    from coach.stats import build_trends

    now = datetime.now(TZ)
    goals = _get_goals(user_id)
    memory = _get_coach_memory(user_id)

    parts = [f"Current time: {now.strftime('%Y-%m-%d %H:%M')} ({TZ})"]

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

    if goals:
        parts.append(f"User goals: {json.dumps(goals, separators=(',', ':'))}")
    if memory:
        parts.append(f"Coach memory: {json.dumps(memory, separators=(',', ':'))}")

    # Include active workout plan if one exists
    plan = get_current_plan(user_id)
    if plan:
        parts.append(f"Active workout plan: {json.dumps(plan, separators=(',', ':'))}")

    return "\n\n".join(parts)


def handle_message(user_id: str, user_text: str) -> str:
    """Process an inbound user message and generate a coach reply.

    Stores both the user message and the reply in chat_messages.
    Returns the reply text.
    """
    db.init_db()

    # Always refresh health data before responding so the coach has the latest.
    # Only skip if the last sync was very recent (< 10 minutes ago).
    _ensure_fresh_data(user_id)

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
        conversation_parts.append(f"{prefix}: {msg['text']}")
    conversation_parts.append(f"User: {user_text}")
    conversation_parts.append("\nRespond as the coach in 3-5 sentences maximum. Complete your thought fully — do not leave sentences unfinished. If the user mentions a goal or preference you should remember, end your response with a line like [MEMORY: key = value] and I'll save it.")

    full_prompt = "\n".join(conversation_parts)

    # Call Gemini
    user = db.get_user(user_id)
    api_key = (user.get("gemini_api_key") if user else None) or DEFAULT_GEMINI_KEY
    if not api_key:
        reply = "I'm not configured yet — GEMINI_API_KEY is missing."
        _save_chat_message(user_id, "coach", reply)
        return reply

    try:
        # Shorter budget than scheduled jobs — a person is waiting in chat.
        reply = gemini.generate(
            api_key, contents=full_prompt, system_instruction=CHAT_SYSTEM_PROMPT,
            max_output_tokens=2048, min_chars=10, max_wait=60,
        )
    except gemini.GeminiQuotaExhausted:
        log.warning("Gemini daily quota exhausted for user %s", user_id)
        reply = ("⛔ Your Gemini AI key has used up its free daily quota. "
                 "I'll be able to reply again after it resets at midnight US Pacific "
                 "time (~2pm Thailand time).")
    except Exception:
        log.exception("Gemini call failed")
        reply = "Sorry, I'm having trouble connecting right now. Try again in a moment! 🙏"

    # Extract and process directives (memory + plan creation + delete)
    reply, plan_request, delete_kind = _process_directives(user_id, reply)

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

    # If the coach requested a deletion, remove the last log
    if delete_kind:
        try:
            from coach.food import delete_last_log
            deleted = delete_last_log(user_id, delete_kind)
            if deleted:
                reply = reply + f"\n\n🗑️ ({deleted})"
            else:
                reply = reply + "\n\n(ไม่พบรายการล่าสุดให้ลบ หรือยังลบไม่สำเร็จ)"
        except Exception:
            log.exception("failed to delete last log")

    # Store coach reply
    _save_chat_message(user_id, "coach", reply)

    return reply


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


def _process_directives(user_id: str, text: str) -> tuple[str, str | None, str | None]:
    """Extract [MEMORY: ...], [CREATE_PLAN: ...] and [DELETE_LAST: ...] directives.

    Returns (cleaned_text, plan_request_or_None, delete_kind_or_None).
    Memory directives are saved immediately; plan/delete requests are returned
    for the caller to handle (slower operations).
    """
    lines = text.split("\n")
    clean_lines = []
    plan_request = None
    delete_kind = None

    for line in lines:
        stripped = line.strip()
        if stripped.startswith("[MEMORY:") and stripped.endswith("]"):
            inner = stripped[8:-1].strip()
            if "=" in inner:
                key, value = inner.split("=", 1)
                key, value = key.strip(), value.strip()
                save_memory(user_id, key, value)
                log.info("saved memory: %s = %s", key, value)
                # Mirror the language preference onto the users column so
                # non-chat modules (food replies, etc.) see it too.
                if key.lower() == "language" and value:
                    db.update_user(user_id, language=value)
        elif stripped.startswith("[CREATE_PLAN:") and stripped.endswith("]"):
            plan_request = stripped[13:-1].strip()
            log.info("plan creation requested: %s", plan_request)
        elif stripped.startswith("[DELETE_LAST:") and stripped.endswith("]"):
            kind = stripped[13:-1].strip().lower()
            delete_kind = "drink" if "drink" in kind else "food"
            log.info("delete requested: %s", delete_kind)
        else:
            clean_lines.append(line)

    return "\n".join(clean_lines).strip(), plan_request, delete_kind


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    DEFAULT_USER_ID = "U1068a1b9c15b44e7ff1439bdefdeb5dc"

    message = sys.argv[1] if len(sys.argv) > 1 else "How did I sleep last night?"
    print(f"You: {message}\n")
    reply = handle_message(DEFAULT_USER_ID, message)
    print(f"Coach: {reply}")
