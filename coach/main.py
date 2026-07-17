"""FastAPI app: LINE webhook + in-process scheduler.

- POST /webhook   LINE Messaging API webhook (inbound messages → chat agent → reply)
- GET  /healthz   liveness check
- POST /chat      local testing endpoint
- GET  /auth/google         (Task 5 — placeholder)
- GET  /auth/google/callback (Task 5 — placeholder)

V2: multi-user. Any LINE user can message the bot; a user record is created on
first contact. The single-user LINE_USER_ID check is removed.
"""

import base64
import hashlib
import hmac
import json
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import FastAPI, Request, Response, BackgroundTasks

from coach import db
from coach.config import LINE_CHANNEL_SECRET, TZ
from coach.config import DAILY_SUMMARY_HOUR, DAILY_SUMMARY_MINUTE
from coach.chat import handle_message
from coach.line import send_text as push_text
from coach.line import reply_text, LineError

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

scheduler = BackgroundScheduler(timezone=str(TZ))


# --- Scheduled jobs (multi-user: iterate all configured users) --------------

def _safe_sync_all() -> None:
    """Hourly sync for all active users with a Google token."""
    from coach.sync import run_sync
    from coach import notify
    for user in db.list_active_users():
        uid = user["line_user_id"]
        try:
            run_sync(uid)
            notify.record_success(uid, "google_auth", "sync")
        except Exception as e:
            log.exception("sync failed for user %s", uid)
            # Auth breakage gets its own "reconnect" message; anything else
            # counts toward a generic sync-trouble notification.
            kind = "google_auth" if notify.is_auth_error(e) else "sync"
            notify.record_failure(uid, kind, str(e))


def _safe_daily_summary_all() -> None:
    """Hourly dispatcher: send the daily brief to each configured user whose
    LOCAL clock is at DAILY_SUMMARY_HOUR, at most once per local day.

    Runs every hour (at DAILY_SUMMARY_MINUTE) instead of once at a fixed
    server-TZ time, so users.timezone is honored per user.
    """
    from coach.daily import run_daily_summary
    from coach import notify
    for user in db.list_active_users():
        uid = user["line_user_id"]
        if not user.get("gemini_api_key"):
            continue
        tz = db.user_tz(user)
        if datetime.now(tz).hour != DAILY_SUMMARY_HOUR:
            continue
        if db.insight_sent_today(uid, "daily_summary", tz):
            continue  # already generated today (e.g. misfire catch-up ran late)
        try:
            run_daily_summary(uid)
            notify.record_success(uid, "daily_summary")
        except Exception as e:
            log.exception("daily summary failed for user %s", uid)
            # Runs once per local day, so threshold 2 = two missed mornings.
            notify.record_failure(uid, "daily_summary", str(e), threshold=2)


def _safe_nudge_check_all() -> None:
    """Nudge check for all configured users."""
    from coach.nudges import run_nudge_check
    for user in db.list_active_users():
        uid = user["line_user_id"]
        if not user.get("gemini_api_key"):
            continue
        try:
            run_nudge_check(uid)
        except Exception:
            log.exception("nudge check failed for user %s", uid)


def _safe_weekly_report_all() -> None:
    """Hourly dispatcher: send the weekly report to each configured user whose
    LOCAL time is Sunday 9:00–9:59, at most once per local day."""
    from coach.weekly import run_weekly_report
    for user in db.list_active_users():
        uid = user["line_user_id"]
        if not user.get("gemini_api_key"):
            continue
        tz = db.user_tz(user)
        now_local = datetime.now(tz)
        if now_local.weekday() != 6 or now_local.hour != 9:
            continue
        if db.insight_sent_today(uid, "weekly_report", tz):
            continue
        try:
            run_weekly_report(uid)
        except Exception:
            log.exception("weekly report failed for user %s", uid)


def _safe_backfill_all() -> None:
    """One-time backfill for users with sparse history."""
    from coach.sync import backfill_if_sparse
    for user in db.list_active_users():
        uid = user["line_user_id"]
        try:
            backfill_if_sparse(uid, min_days=14, backfill_days=90)
        except Exception:
            log.exception("backfill failed for user %s", uid)


# --- Lifespan ---------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    # misfire_grace_time lets a job that missed its slot (container down,
    # previous run overran) still fire once it can; coalesce collapses a
    # backlog of missed runs into one. The daily/weekly senders are hourly
    # DISPATCHERS that check each user's local time, so users.timezone is
    # honored and a caught-up misfire can't double-send (insights dedup).
    scheduler.add_job(_safe_backfill_all, "date", id="startup_backfill",
                      misfire_grace_time=3600)
    scheduler.add_job(_safe_sync_all, "cron", minute=5, id="hourly_sync",
                      misfire_grace_time=1800, coalesce=True)
    scheduler.add_job(
        _safe_daily_summary_all, "cron",
        minute=DAILY_SUMMARY_MINUTE,
        id="daily_summary", misfire_grace_time=3000, coalesce=True,
    )
    scheduler.add_job(_safe_nudge_check_all, "cron", minute=35, id="hourly_nudge",
                      misfire_grace_time=1200, coalesce=True)
    scheduler.add_job(
        _safe_weekly_report_all, "cron",
        minute=0,
        id="weekly_report", misfire_grace_time=3000, coalesce=True,
    )
    scheduler.start()
    log.info(
        "scheduler started (sync at :05, nudges at :35, daily dispatch at :%02d "
        "for local %02d:00, weekly dispatch hourly for local Sun 09:00)",
        DAILY_SUMMARY_MINUTE, DAILY_SUMMARY_HOUR,
    )
    yield
    scheduler.shutdown(wait=False)


app = FastAPI(lifespan=lifespan)


# --- Health check -----------------------------------------------------------

@app.get("/healthz")
def healthz():
    return {"ok": True, "ts": datetime.now(timezone.utc).isoformat()}


# --- LINE Webhook -----------------------------------------------------------

def _valid_line_signature(body: bytes, signature: str) -> bool:
    """Validate LINE webhook signature using channel secret."""
    if not LINE_CHANNEL_SECRET:
        log.warning("LINE_CHANNEL_SECRET not set — skipping signature check")
        return True
    hash_value = hmac.new(
        LINE_CHANNEL_SECRET.encode(), body, hashlib.sha256
    ).digest()
    expected = base64.b64encode(hash_value).decode()
    return hmac.compare_digest(expected, signature)


def _oauth_redirect_uri() -> str:
    """Canonical OAuth callback URL. Must exactly match the URI registered in
    Google Cloud Console. Built from PUBLIC_HOST (not request.url_for, which
    behind the Cloudflare tunnel yields the internal http://coach:8080 host).
    """
    import os
    host = os.environ.get("PUBLIC_HOST", "coach.signagegold.co")
    return f"https://{host}/auth/google/callback"


def _detect_image_mime(data: bytes) -> str:
    """Sniff the image mime type from magic bytes."""
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    return "image/jpeg"


def _send(user_id: str, text: str, reply_token: str | None = None) -> list[str]:
    """Send via the event's free reply token when possible, else push.

    Push messages count against the per-BOT monthly quota (500 on the free
    plan, shared across all users); replies to webhook events are free and
    unlimited. Reply tokens are single-use and short-lived, so slow paths
    (long Gemini retries) may miss the window — the push fallback covers that.

    Returns the LINE message ids of what was sent (for quote-reply tracking).
    """
    if reply_token:
        try:
            return reply_text(reply_token, text).get("message_ids", [])
        except LineError as e:
            log.info("reply token unusable (%s) — falling back to push", e)
    return push_text(text, to=user_id).get("message_ids", [])


def _send_welcome(user_id: str, reply_token: str | None = None) -> None:
    """Send a welcome message to brand-new users explaining setup."""
    _send(
        user_id,
        "👋 Welcome to your AI Health Coach!\n\n"
        "I help you track your health, analyze food photos, and give personalized coaching — all via this chat.\n\n"
        "To get started, you need two things:\n\n"
        "1️⃣ Connect Google Health\n"
        "   → Send: login\n\n"
        "2️⃣ Set your Gemini AI key\n"
        "   → Send: set key\n"
        "   (Get a free key at aistudio.google.com/apikey)\n\n"
        "Once both are set, just chat with me or send a food photo! 🍽️💪\n\n"
        "📖 Send: help — to see everything I can do",
        reply_token,
    )
    log.info("sent welcome to new user %s", user_id)


def _map_sent_log(user_id: str, message_ids: list[str], rowids: list[int]) -> None:
    """Associate LINE messages with the log(s) they concern, so a later
    quote-reply can target that exact log. Covers BOTH directions: the coach's
    confirmation message ids AND the user's own inbound message (photo/text)
    that created the log — users quote either one. Best-effort."""
    if not message_ids or not rowids:
        return
    try:
        # One confirmation message may cover several logs; a quote-reply to it
        # most plausibly means the newest one.
        for mid in message_ids:
            db.map_log_message(mid, user_id, rowids[-1])
    except Exception:
        log.exception("failed to map log message for %s", user_id)


HELP_TEXTS = {
    "en": (
        "📖 What I can do\n\n"
        "🔧 Setup\n"
        "• login — connect Google Health\n"
        "• set key — set your Gemini AI key\n\n"
        "💬 Just chat\n"
        "Ask about your sleep, steps, heart rate, calories or trends — "
        "e.g. \"How did I sleep last night?\"\n\n"
        "🍽️ Log food & drinks\n"
        "• Send a photo of your meal or drink — I'll analyze and log it\n"
        "• Or type: \"log: grilled chicken with rice\"\n"
        "• Say when it was: \"log breakfast: ...\", \"log 2 glasses of water at 9:00\"\n\n"
        "✏️ Fix a log\n"
        "• Reply (quote) a log message: \"I had 4 of those\", \"only half\"\n"
        "• \"delete my last meal\" / \"delete my last drink\", or quote-reply \"delete this\"\n"
        "• \"delete all today's logs\" — clears the whole day (I'll ask you to confirm)\n\n"
        "🏋️ Coaching\n"
        "• \"Create a workout plan for ...\"\n"
        "• Daily summary every morning, weekly report on Sunday\n"
        "• Tell me your goals or preferred language and I'll remember\n\n"
        "📖 help — show this menu"
    ),
    "th": (
        "📖 สิ่งที่ผมช่วยได้\n\n"
        "🔧 ตั้งค่า\n"
        "• login — เชื่อมต่อ Google Health\n"
        "• set key — ตั้งค่า Gemini AI key\n\n"
        "💬 คุยได้เลย\n"
        "ถามเรื่องการนอน ก้าวเดิน หัวใจ แคลอรี หรือแนวโน้มย้อนหลัง — "
        "เช่น \"เมื่อคืนนอนเป็นยังไงบ้าง\"\n\n"
        "🍽️ บันทึกอาหาร/เครื่องดื่ม\n"
        "• ส่งรูปอาหารหรือเครื่องดื่ม เดี๋ยวผมวิเคราะห์และบันทึกให้\n"
        "• หรือพิมพ์: \"ลงโภชนาการ ข้าวมันไก่ 1 จาน\"\n"
        "• ระบุมื้อ/เวลาได้: \"ลงมื้อเช้า ...\", \"บันทึกน้ำ 2 แก้ว ตอน 9 โมง\"\n\n"
        "✏️ แก้ไขรายการ\n"
        "• Reply (quote) ไปที่ข้อความบันทึก: \"กินไป 4 รอบ\", \"กินแค่ครึ่งเดียว\"\n"
        "• \"ลบรายการล่าสุด\" หรือ quote แล้วพิมพ์ \"ลบอันนี้\"\n"
        "• \"ลบรายการวันนี้ทั้งหมด\" — ล้างทั้งวัน (ผมจะถามยืนยันก่อน)\n\n"
        "🏋️ โค้ชชิ่ง\n"
        "• \"สร้างแผนออกกำลังกายให้หน่อย ...\"\n"
        "• สรุปประจำวันทุกเช้า และรายงานประจำสัปดาห์วันอาทิตย์\n"
        "• บอกเป้าหมายหรือภาษาที่อยากให้ใช้ได้เลย เดี๋ยวผมจำไว้\n\n"
        "📖 help — แสดงเมนูนี้"
    ),
}


def _help_text(user_id: str) -> str:
    lang = (db.get_user_language(user_id) or "").strip().lower()
    is_th = lang.startswith("th") or "thai" in lang or "ไทย" in lang
    return HELP_TEXTS["th" if is_th else "en"]


def _process_text_message(user_id: str, text: str, reply_token: str | None = None,
                          quoted_message_id: str | None = None,
                          inbound_message_id: str | None = None) -> None:
    """Handle a text message in the background."""
    log.info("LINE message from %s: %s (quoted=%s)", user_id, text, quoted_message_id)

    # Check for onboarding commands before passing to chat agent
    lower = text.strip().lower()

    # Help command: static menu of everything the coach can do (free, no AI
    # call, works even before setup is complete)
    if lower in ("help", "ช่วยเหลือ", "วิธีใช้", "เมนู", "commands", "command", "?"):
        _send(user_id, _help_text(user_id), reply_token)
        log.info("sent help menu to %s", user_id)
        return

    # Login command: send the Google OAuth URL
    if lower in ("login", "login google", "connect google", "เชื่อมต่อ google", "action=login_google"):
        try:
            from coach.oauth import _sign_state
            import os
            host = os.environ.get("PUBLIC_HOST", "coach.signagegold.co")
            state = _sign_state(user_id)
            login_url = f"https://{host}/auth/google?state={state}"
            _send(
                user_id,
                f"🔗 Open this link to connect your Google Health account:\n\n{login_url}\n\n"
                "Sign in with the Google account linked to your Fitbit/Pixel Watch.",
                reply_token,
            )
            log.info("sent login URL to %s", user_id)
        except Exception:
            log.exception("failed to generate login URL")
            _send(user_id, "Sorry, I couldn't generate a login link. Please try again.", reply_token)
        return

    # Set Gemini key command: enter "awaiting key" mode
    if lower in ("set key", "set gemini key", "ตั้งค่า key", "เปลี่ยน key", "action=set_gemini_key"):
        db.update_user(user_id, onboarding_state="awaiting_gemini_key")
        _send(
            user_id,
            "🔑 Please paste your Gemini API key.\n\n"
            "Get one free from: https://aistudio.google.com/apikey\n\n"
            "Just send the key as your next message (starts with 'AI...' or 'AQ...').",
            reply_token,
        )
        log.info("user %s entering Gemini key setup mode", user_id)
        return

    # Check if user is in "awaiting key" mode — validate and store the key
    user = db.get_user(user_id)
    if user and user.get("onboarding_state") == "awaiting_gemini_key":
        _handle_gemini_key_input(user_id, text.strip(), reply_token)
        return

    # Require full setup before using the coach
    if not _ensure_configured(user_id, user, reply_token):
        return

    # Pass to the chat agent
    try:
        reply, log_rowids = handle_message(user_id, text, quoted_message_id=quoted_message_id)
        sent_ids = _send(user_id, reply, reply_token)
        if inbound_message_id:
            sent_ids = sent_ids + [inbound_message_id]
        _map_sent_log(user_id, sent_ids, log_rowids)
        log.info("replied via LINE: %s", reply[:80])
    except Exception:
        log.exception("failed to handle LINE message")


def _ensure_configured(user_id: str, user: dict | None, reply_token: str | None = None) -> bool:
    """Check the user has both a Gemini key and Google token. If not, send a
    reminder and return False. This gates every path that would otherwise hit
    the user's Gemini key or Google token (preventing fallback to the owner's).
    """
    if not user or not user.get("gemini_api_key"):
        _send(
            user_id,
            "🔑 You haven't set up your AI key yet.\n"
            "Send: set key\n\n"
            "Get a free one at: https://aistudio.google.com/apikey",
            reply_token,
        )
        return False

    if not user.get("google_token_json"):
        _send(
            user_id,
            "🔗 You haven't connected Google Health yet.\n"
            "Send: login\n\n"
            "This connects your Fitbit/Pixel Watch data.",
            reply_token,
        )
        return False

    return True


def _handle_gemini_key_input(user_id: str, key: str, reply_token: str | None = None) -> None:
    """Validate a Gemini API key and store it if valid."""
    # Cancel must be checked BEFORE the format check — 'cancel' is shorter than
    # 20 chars, so the other order traps the user in key-setup mode forever.
    if key.lower() == "cancel":
        db.update_user(user_id, onboarding_state="")
        _send(user_id, "Key setup cancelled.", reply_token)
        return

    # Basic format check
    if len(key) < 20 or " " in key or "\n" in key:
        _send(
            user_id,
            "❌ That doesn't look like a valid API key. "
            "Please paste the full key (no spaces or line breaks).\n\n"
            "Or send 'cancel' to exit setup.",
            reply_token,
        )
        return

    # Validate by making a test call
    try:
        from google import genai
        client = genai.Client(api_key=key)
        # Quick validation: list models (lightweight, no generation cost)
        models = list(client.models.list())
        if not models:
            raise RuntimeError("No models returned")
    except Exception as e:
        log.warning("Gemini key validation failed for user %s: %s", user_id, e)
        _send(
            user_id,
            "❌ That key didn't work. Please check and try again.\n\n"
            "Error: " + str(e)[:100] + "\n\n"
            "Or send 'cancel' to exit setup.",
            reply_token,
        )
        return

    # Key is valid — store it and exit onboarding mode
    db.update_user(user_id, gemini_api_key=key, onboarding_state="")
    _send(
        user_id,
        "✅ Gemini API key saved and verified!\n\n"
        "Your AI health coach is now fully configured. "
        "Send me a message or a food photo to get started 💪",
        reply_token,
    )
    log.info("stored valid Gemini key for user %s", user_id)


def _process_image_message(user_id: str, message_id: str, reply_token: str | None = None) -> None:
    """Handle an image message in the background."""
    from coach.line import get_image_content
    from coach.food import handle_food_photo

    # Require full setup before touching the user's Gemini key / Google token
    if not _ensure_configured(user_id, db.get_user(user_id), reply_token):
        return

    log.info("LINE image from %s (id=%s) — analyzing", user_id, message_id)
    try:
        image_bytes = get_image_content(message_id)
        mime = _detect_image_mime(image_bytes)
        reply, log_rowid = handle_food_photo(user_id, image_bytes, mime_type=mime)
        sent_ids = _send(user_id, reply, reply_token)
        # Map the coach's confirmation AND the user's own photo message — a
        # quote-reply to either should target this log.
        _map_sent_log(user_id, sent_ids + [message_id],
                      [log_rowid] if log_rowid is not None else [])
        log.info("photo processed: %s", reply[:80])
    except Exception:
        log.exception("failed to handle photo")
        try:
            _send(user_id, "Sorry, I couldn't analyze that photo. Please try again. 🙏", reply_token)
        except Exception:
            pass


@app.post("/webhook")
async def line_webhook(request: Request, background_tasks: BackgroundTasks):
    body = await request.body()
    signature = request.headers.get("X-Line-Signature", "")

    if not _valid_line_signature(body, signature):
        return Response(status_code=403)

    payload = json.loads(body)
    events = payload.get("events", [])

    for event in events:
        if event.get("type") != "message":
            continue

        # Skip redelivered events to avoid duplicate processing
        if event.get("deliveryContext", {}).get("isRedelivery"):
            log.info("skipping redelivered event")
            continue

        msg = event.get("message", {})
        msg_type = msg.get("type")
        user_id = event.get("source", {}).get("userId", "")
        reply_token = event.get("replyToken") or None

        if not user_id:
            continue

        # V2: auto-create user on first contact and send the welcome. Their
        # first message is still processed below (so "login" as an opener
        # works) — but the welcome takes the reply token, the follow-up
        # response goes out as a push.
        is_new = db.get_user(user_id) is None
        db.get_or_create_user(user_id)

        if is_new:
            background_tasks.add_task(_send_welcome, user_id, reply_token)
            reply_token = None  # consumed by the welcome

        # Process in the background so we return 200 to LINE immediately
        if msg_type == "text":
            background_tasks.add_task(_process_text_message, user_id, msg["text"],
                                      reply_token, msg.get("quotedMessageId"),
                                      msg.get("id"))
        elif msg_type == "image":
            background_tasks.add_task(_process_image_message, user_id, msg.get("id", ""), reply_token)

    return {"ok": True}


# --- Local testing endpoint -------------------------------------------------

@app.post("/chat")
async def chat_endpoint(request: Request):
    """Direct chat endpoint for local testing. Send JSON: {"message": "...", "user_id": "..."}

    The Cloudflare tunnel forwards ALL paths, so this endpoint is reachable from
    the public internet — it must never be open. Disabled unless CHAT_TEST_TOKEN
    is set in the environment AND the request carries it in an X-Chat-Token
    header. Responds 404 (not 403) so the endpoint's existence isn't advertised.
    """
    import os
    expected = os.environ.get("CHAT_TEST_TOKEN", "")
    provided = request.headers.get("X-Chat-Token", "")
    if not expected or not hmac.compare_digest(provided.encode(), expected.encode()):
        return Response(status_code=404)

    body = await request.json()
    text = body.get("message", "")
    user_id = body.get("user_id", "U1068a1b9c15b44e7ff1439bdefdeb5dc")
    if not text:
        return {"error": "missing 'message' field"}
    reply, _ = handle_message(user_id, text)
    return {"reply": reply}


# --- Google OAuth web flow --------------------------------------------------

@app.get("/auth/google")
async def auth_google(request: Request, state: str = ""):
    """Start the Google OAuth flow. Called when user taps 'Login Google Health'.

    Query param `state` contains the signed LINE userId.
    Redirects the user's browser to Google's consent screen.
    """
    from coach.oauth import build_auth_url, _verify_state
    from fastapi.responses import RedirectResponse

    # Verify this is a legitimate request
    user_id = _verify_state(state)
    if not user_id:
        return Response(content="Invalid or missing state parameter.", status_code=400)

    # Canonical redirect URI (matches what's registered in Google)
    redirect_uri = _oauth_redirect_uri()

    try:
        auth_url = build_auth_url(user_id, redirect_uri)
        return RedirectResponse(auth_url)
    except Exception as e:
        log.exception("failed to build auth URL")
        return Response(content=f"Error: {e}", status_code=500)


@app.get("/auth/google/callback")
async def auth_google_callback(request: Request, code: str = "", state: str = "", error: str = ""):
    """Google OAuth callback. Exchanges the code for tokens and stores them."""
    from coach.oauth import exchange_code

    if error:
        return Response(content=f"Authorization denied: {error}", status_code=400)

    if not code or not state:
        return Response(content="Missing code or state parameter.", status_code=400)

    redirect_uri = _oauth_redirect_uri()
    user_id, err = exchange_code(code, state, redirect_uri)

    if err:
        log.error("OAuth callback error for %s: %s", user_id, err)
        return Response(content=f"❌ {err}", status_code=400)

    # Notify the user on LINE that their account is connected
    try:
        push_text("✅ Google Health connected! Your health data will sync shortly.", to=user_id)
    except Exception:
        pass

    return Response(
        content="✅ Google Health connected successfully! You can close this window and return to LINE.",
        media_type="text/plain",
    )
