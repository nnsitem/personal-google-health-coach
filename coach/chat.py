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
- To delete a food or drink log when the user asks (e.g. "delete that", "remove my last meal",
  "ลบรายการล่าสุด", or a quote-REPLY to a log saying "ลบ log อันนี้"):
  [DELETE_LAST: food] for a meal, or [DELETE_LAST: drink] for a drink.
  If the user is quote-replying to a specific logged entry (shown in the context), the system
  deletes EXACTLY that entry — use its type as the kind. Otherwise the newest log of that kind
  is deleted. After emitting this, confirm which item you're removing.
- To delete ALL of today's logs when the user asks to clear the whole day (e.g.
  "ลบรายการอาหารวันนี้ทั้งหมด", "clear all my logs today", "delete today's hydration"):
  [DELETE_TODAY: all] — or [DELETE_TODAY: food] / [DELETE_TODAY: drink] for one kind only.
  This is DESTRUCTIVE and irreversible: do NOT emit it on the first request. First ask the
  user to confirm (mention what will be wiped, e.g. "จะลบรายการอาหารและเครื่องดื่มของวันนี้ทั้งหมด
  ยืนยันไหมครับ?"), and emit the directive only after they confirm in their next message.
  The system appends the result of the deletion.
- To log food or drinks the user describes in words (e.g. "log: grilled pork 3 skewers with sticky rice",
  "ลงโภชนาการ หมูปิ้ง 3 ไม้ กับข้าวเหนียว 1 ห่อ", "log 2 glasses of water", "บันทึกน้ำ 1 แก้ว"):
  [LOG_FOOD: {"food_name_en": "grilled pork skewers (3) with sticky rice", "food_name_local": "หมูปิ้ง 3 ไม้ กับข้าวเหนียว 1 ห่อ", "calories_kcal": 475, "protein_g": 22, "total_carbohydrate_g": 55, "total_fat_g": 18, "meal_type": null, "time": null}]
  [LOG_DRINK: {"drink_name_en": "water", "drink_name_local": "น้ำเปล่า", "container_count": 2, "volume_ml": 500, "is_water": true, "calories_kcal": 0, "protein_g": 0, "total_carbohydrate_g": 0, "total_fat_g": 0, "meal_type": null, "time": null}]
  Rules for these two directives:
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
  - In your visible reply, confirm the item with the estimated calories (or volume) and
    the meal slot if given. Do NOT say it was saved — the system appends the real save status.
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

        in_bed_min = sum(totals.values())
        asleep_min = in_bed_min - totals["AWAKE"]
        start_local = datetime.fromisoformat(row["start"].replace("Z", "+00:00")).astimezone(TZ)
        end_local = datetime.fromisoformat(row["end"].replace("Z", "+00:00")).astimezone(TZ)

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

    # Recent food/drink logs so follow-ups about "the last log" have context
    food_logs = _get_recent_food_logs(user_id)
    if food_logs:
        parts.append(
            "Recent food/drink logs (newest first, ts in UTC): "
            f"{json.dumps(food_logs, separators=(',', ':'), ensure_ascii=False)}"
        )

    if goals:
        parts.append(f"User goals: {json.dumps(goals, separators=(',', ':'))}")
    if memory:
        parts.append(f"Coach memory: {json.dumps(memory, separators=(',', ':'))}")

    # Include active workout plan if one exists
    plan = get_current_plan(user_id)
    if plan:
        parts.append(f"Active workout plan: {json.dumps(plan, separators=(',', ':'))}")

    return "\n\n".join(parts)


def handle_message(user_id: str, user_text: str,
                   quoted_message_id: str | None = None) -> tuple[str, list[int]]:
    """Process an inbound user message and generate a coach reply.

    quoted_message_id: LINE id of the message the user quote-replied to, if
    any — when it maps to a log confirmation we sent, that exact log becomes
    the target for adjustments (instead of guessing "the last log").

    Stores both the user message and the reply in chat_messages.
    Returns (reply_text, created_log_rowids) — the rowids let the caller map
    the outgoing confirmation message for future quote-replies.
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
        return reply, []

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

    # Extract and process directives (memory + plan creation + delete + logs)
    reply, plan_request, delete_kind, chat_logs, delete_today = _process_directives(user_id, reply)

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
            else:
                status, rowid = log_chat_entry(user_id, kind, analysis)
                if rowid is not None:
                    created_rowids.append(rowid)
        except Exception:
            log.exception("failed to process chat %s directive", kind)
            status = "⚠️"
        if status:
            reply = reply + "\n\n" + status

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

    # Store coach reply
    _save_chat_message(user_id, "coach", reply)

    return reply, created_rowids


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


def _process_directives(user_id: str, text: str) -> tuple[str, str | None, str | None, list]:
    """Extract [MEMORY: ...], [CREATE_PLAN: ...], [DELETE_LAST: ...] and
    [LOG_FOOD/LOG_DRINK: {...}] directives.

    Returns (cleaned_text, plan_request_or_None, delete_kind_or_None, logs,
    delete_today_or_None) where logs is a list of ("food"|"drink"|"adjust",
    payload_dict_or_None) — None marks a directive whose JSON didn't parse,
    so the caller can surface a not-saved warning instead of silently
    dropping it. Memory directives are saved immediately; the rest are
    returned for the caller to handle (slower operations).
    """
    lines = text.split("\n")
    clean_lines = []
    plan_request = None
    delete_kind = None
    delete_today = None
    logs: list[tuple[str, dict | None]] = []

    def _parse_log(kind: str, inner: str) -> None:
        try:
            data = json.loads(inner)
            logs.append((kind, data if isinstance(data, dict) else None))
        except (json.JSONDecodeError, ValueError):
            log.warning("unparseable %s directive: %s", kind, inner[:200])
            logs.append((kind, None))

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
        elif stripped.startswith("[LOG_FOOD:") and stripped.endswith("]"):
            _parse_log("food", stripped[10:-1].strip())
        elif stripped.startswith("[LOG_DRINK:") and stripped.endswith("]"):
            _parse_log("drink", stripped[11:-1].strip())
        elif stripped.startswith("[ADJUST_LAST:") and stripped.endswith("]"):
            _parse_log("adjust", stripped[13:-1].strip())
        elif stripped.startswith("[DELETE_TODAY:") and stripped.endswith("]"):
            val = stripped[14:-1].strip().lower()
            if "drink" in val or "hydration" in val or "water" in val:
                delete_today = "drink"
            elif "food" in val or "meal" in val or "nutrition" in val:
                delete_today = "food"
            else:
                delete_today = "all"
            log.info("delete-today requested: %s", delete_today)
        else:
            clean_lines.append(line)

    return "\n".join(clean_lines).strip(), plan_request, delete_kind, logs, delete_today


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    DEFAULT_USER_ID = "U1068a1b9c15b44e7ff1439bdefdeb5dc"

    message = sys.argv[1] if len(sys.argv) > 1 else "How did I sleep last night?"
    print(f"You: {message}\n")
    reply, _ = handle_message(DEFAULT_USER_ID, message)
    print(f"Coach: {reply}")
