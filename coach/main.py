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
import os
import threading
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import FastAPI, Request, Response, BackgroundTasks
from fastapi.responses import FileResponse
from linebot.v3.messaging import TextMessage

from coach import db
from coach.config import LINE_CHANNEL_SECRET, TZ
from coach.config import DAILY_SUMMARY_HOUR, DAILY_SUMMARY_MINUTE
from coach.chat import handle_message
from coach.flex import FlexReply
from coach.line import send_text as push_text
from coach.line import reply_text, send_messages, reply_messages, flex_message, LineError

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

scheduler = BackgroundScheduler(timezone=str(TZ))


# --- Photo batching -----------------------------------------------------
#
# LINE delivers each photo (and each text message) as its own webhook event,
# handled by its own background thread (FastAPI's BackgroundTasks runs sync
# handlers via a thread pool, not on the asyncio loop — plain `threading`
# primitives are what's actually safe to share across these calls). Sending
# several photos together, or a photo followed by a quick caption, should
# read as ONE meal/log rather than N separate ones — so an image message
# doesn't analyze immediately: it joins a short-lived per-user batch that
# resets a debounce timer on every new arrival (image or text) and only
# finalizes (runs the actual Gemini analysis + log) once nothing new has
# come in for _PHOTO_BATCH_WINDOW_SECONDS. A single photo with no follow-up
# still goes through this path — it "batches" as a batch of one — so
# existing single-photo behavior is unchanged except for that added wait.
_PHOTO_BATCH_WINDOW_SECONDS = float(os.environ.get("PHOTO_BATCH_WINDOW_SECONDS") or "3.5")
_MAX_BATCH_IMAGES = 6

_pending_photo_batches: dict[str, dict] = {}
_pending_photo_lock = threading.Lock()


def _touch_photo_batch(user_id: str, *, create: bool = False,
                       image: tuple[bytes, str] | None = None,
                       text: str | None = None,
                       message_id: str | None = None,
                       reply_token: str | None = None) -> bool:
    """Add an image and/or text to the user's open photo batch and (re)start
    its debounce timer. With create=False, does nothing and returns False
    when no batch is currently open (the caller should fall through to its
    normal handling — this is how a plain text message not following a
    photo stays untouched). Returns True whenever the item was attached to
    a batch (new or existing).

    Each touch bumps the batch's "epoch" and the scheduled timer carries the
    epoch it was started for; _finalize_photo_batch only proceeds if that
    epoch is still current. threading.Timer.cancel() cannot be trusted alone
    here — it only prevents a timer that hasn't started running yet, so a
    timer whose interval elapses at the same instant a new touch arrives can
    still fire (see cancel()'s documented "no effect once run() has started"
    behavior). The epoch check, taken under the same lock as every touch, is
    what actually guarantees a stale timer's finalize is a no-op rather than
    an early/duplicate finalize.
    """
    with _pending_photo_lock:
        batch = _pending_photo_batches.get(user_id)
        if batch is None:
            if not create:
                return False
            batch = {"images": [], "texts": [], "message_ids": [], "reply_token": None,
                     "timer": None, "epoch": 0}
            _pending_photo_batches[user_id] = batch

        if image is not None:
            if len(batch["images"]) >= _MAX_BATCH_IMAGES:
                log.info("photo batch for %s hit the %d-image cap — extra photo ignored",
                         user_id, _MAX_BATCH_IMAGES)
                message_id = None  # this photo contributed nothing — don't map it to the eventual log
            else:
                batch["images"].append(image)
        if text:
            batch["texts"].append(text)
        if message_id:
            batch["message_ids"].append(message_id)
        if reply_token:
            batch["reply_token"] = reply_token  # keep the most recent — replies fall back to push anyway

        if batch["timer"]:
            batch["timer"].cancel()  # best-effort; the epoch check below is what's actually relied on
        batch["epoch"] += 1
        epoch = batch["epoch"]
        delay = 0.3 if len(batch["images"]) >= _MAX_BATCH_IMAGES else _PHOTO_BATCH_WINDOW_SECONDS
        timer = threading.Timer(delay, _finalize_photo_batch, args=(user_id, epoch))
        timer.daemon = True
        batch["timer"] = timer
        timer.start()
        return True


def _finalize_photo_batch(user_id: str, epoch: int) -> None:
    """Runs on the debounce timer's own thread once a photo batch has gone
    quiet: analyze every photo (+ any caption text) as one meal and reply.

    `epoch` pins this call to the specific touch that scheduled it — see
    _touch_photo_batch's docstring for why that (not Timer.cancel()) is what
    prevents a stale timer from finalizing a batch early or twice.
    """
    from coach.food import join_captions

    with _pending_photo_lock:
        batch = _pending_photo_batches.get(user_id)
        if batch is None or batch.get("epoch") != epoch:
            return  # superseded by a later touch — that touch's own timer will finalize
        batch = _pending_photo_batches.pop(user_id)
    if not batch["images"]:
        return

    from coach.food import handle_food_photos, get_daily_progress
    from coach.flex import build_daily_progress_bubble

    reply_token = batch["reply_token"]
    log.info("finalizing photo batch for %s: %d photo(s), %d caption text(s)",
             user_id, len(batch["images"]), len(batch["texts"]))

    # Defaults to the FULL caption text so it's still forwarded to chat below
    # even if handle_food_photos() raises before it can judge relevance
    # itself — a crash in the photo path must not also swallow the user's
    # typed message.
    leftover_caption = join_captions(batch["texts"])
    try:
        reply, log_rowid, leftover_caption = handle_food_photos(
            user_id, batch["images"], captions=batch["texts"] or None)
        messages_to_send = [reply]
        # Combine log + progress into a carousel (swipe to see progress) —
        # same presentation as the pre-batching single-photo flow.
        try:
            if isinstance(reply, FlexReply):
                progress = get_daily_progress(user_id)
                progress_bubble = build_daily_progress_bubble(
                    current=progress["current"], targets=progress["targets"],
                    lang=progress["lang"], coaching_note=reply.coaching_note,
                )
                if progress_bubble:
                    carousel = {"type": "carousel", "contents": [reply.bubble, progress_bubble]}
                    messages_to_send = [FlexReply(reply.alt_text, carousel)]
        except Exception:
            pass
        sent_ids = _send_multi(user_id, messages_to_send, reply_token)
        # Map the coach's confirmation AND every contributing message (all
        # photos + any caption text) — a quote-reply to any of them should
        # target this one log.
        _map_sent_log(user_id, sent_ids + batch["message_ids"],
                      [log_rowid] if log_rowid is not None else [])
    except Exception:
        log.exception("failed to handle photo batch for %s", user_id)
        try:
            _send(user_id, "Sorry, I couldn't analyze that photo. Please try again. 🙏", reply_token)
        except Exception:
            pass

    # A caption the model judged unrelated to the food (or one sent while
    # analysis crashed/failed before it could judge, or before analysis ran
    # at all) must not vanish — forward it through the normal chat pipeline
    # as a follow-up, independent of whether the photo analysis succeeded.
    if leftover_caption:
        try:
            chat_reply, chat_rowids, extra_flex = handle_message(user_id, leftover_caption)
            more_ids = _send_multi(user_id, [chat_reply, *extra_flex])
            _map_sent_log(user_id, more_ids, chat_rowids)
        except Exception:
            log.exception("failed to forward leftover caption text to chat for %s", user_id)


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
    from coach import notify
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
            notify.record_success(uid, "weekly_report")
        except Exception as e:
            log.exception("weekly report failed for user %s", uid)
            # Runs once per local week, so threshold 2 = two missed Sundays.
            notify.record_failure(uid, "weekly_report", str(e), threshold=2)


def _safe_backfill_all() -> None:
    """One-time backfill for users with sparse history."""
    from coach.sync import backfill_if_sparse
    for user in db.list_active_users():
        uid = user["line_user_id"]
        try:
            backfill_if_sparse(uid, min_days=14, backfill_days=90)
        except Exception:
            log.exception("backfill failed for user %s", uid)


def _safe_image_cleanup() -> None:
    """Prune temp meal/drink photos (served for Flex hero images) once stale."""
    from coach.images import cleanup_old_images
    try:
        cleanup_old_images()
    except Exception:
        log.exception("temp image cleanup failed")


def _safe_db_prune() -> None:
    """Drop rows past their retention window (sync_log, old nudges, chat tail)."""
    try:
        db.prune_old_rows()
    except Exception:
        log.exception("db prune failed")


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
    scheduler.add_job(_safe_image_cleanup, "cron", minute=50, id="image_cleanup",
                      misfire_grace_time=3000, coalesce=True)
    # Daily at 04:20 server time — off-peak, and no user job runs then.
    scheduler.add_job(_safe_db_prune, "cron", hour=4, minute=20, id="db_prune",
                      misfire_grace_time=3600, coalesce=True)
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


@app.get("/images/{token}")
async def serve_temp_image(token: str):
    """Serves a temp meal/drink photo so it can sit as a Flex hero image —
    LINE's servers need a public URL, and inbound photo bytes are otherwise
    only ever in-memory (see coach/images.py). 404s for anything that isn't
    exactly a token this process minted."""
    from coach.images import resolve_temp_image
    path = resolve_temp_image(token)
    if not path:
        return Response(status_code=404)
    return FileResponse(path)


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


def _to_line_message(payload):
    """Convert a str or FlexReply into a line-bot-sdk Message object."""
    if isinstance(payload, FlexReply):
        return flex_message(payload.alt_text, payload.bubble)
    return TextMessage(text=str(payload)[:5000])


def _send_multi(user_id: str, payloads: list, reply_token: str | None = None) -> list[str]:
    """Like _send, but for a list of str/FlexReply payloads sent as one LINE
    message batch (a text reply plus any Flex log-confirmation cards)."""
    messages = [_to_line_message(p) for p in payloads if p]
    if not messages:
        return []
    if reply_token:
        try:
            return reply_messages(reply_token, messages).get("message_ids", [])
        except LineError as e:
            log.info("reply token unusable (%s) — falling back to push", e)
    return send_messages(messages, to=user_id).get("message_ids", [])


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
        "🎯 Nutrition targets\n"
        "• \"Set target 1800 kcal, 150g protein, 2500ml water\"\n"
        "• \"Change my calorie goal to 2200\"\n"
        "• Current defaults: 2000 kcal, 120g protein, 65g fat, 250g carbs, 2000ml water\n\n"
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
        "🎯 ตั้งเป้าโภชนาการ\n"
        "• \"ตั้งเป้า 1800 kcal, 150g โปรตีน, 2500ml น้ำ\"\n"
        "• \"เปลี่ยนเป้าแคลอรี่เป็น 2200\"\n"
        "• ค่าเริ่มต้น: 2000 kcal, 120g โปรตีน, 65g ไขมัน, 250g คาร์บ, 2000ml น้ำ\n\n"
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

    # A caption sent right after a photo (or while more photos are still
    # arriving) joins that open batch instead of going to chat here —
    # handle_food_photos() decides whether it's actually relevant to the
    # food and, if not, _finalize_photo_batch() forwards it back to chat
    # itself so it's never silently dropped. A quote-reply is always an
    # explicit targeted action (e.g. adjusting a specific past log), so it
    # skips batching and goes straight to chat as before.
    if not quoted_message_id and _touch_photo_batch(
        user_id, create=False, text=text, message_id=inbound_message_id, reply_token=reply_token,
    ):
        log.info("attached trailing text from %s to its open photo batch", user_id)
        return

    # Pass to the chat agent
    try:
        reply, log_rowids, extra_flex = handle_message(user_id, text, quoted_message_id=quoted_message_id)
        sent_ids = _send_multi(user_id, [reply, *extra_flex], reply_token)
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
    """Handle an image message in the background: download it and add it to
    the user's short-lived photo batch (see _touch_photo_batch) rather than
    analyzing immediately, so photos sent together — or a caption text sent
    right after — are logged as one meal instead of separately."""
    from coach.line import get_image_content

    # Require full setup before touching the user's Gemini key / Google token
    if not _ensure_configured(user_id, db.get_user(user_id), reply_token):
        return

    try:
        image_bytes = get_image_content(message_id)
        mime = _detect_image_mime(image_bytes)
    except Exception:
        log.exception("failed to download photo %s", message_id)
        try:
            _send(user_id, "Sorry, I couldn't download that photo. Please try again. 🙏", reply_token)
        except Exception:
            pass
        return

    log.info("LINE image from %s (id=%s) — added to photo batch", user_id, message_id)
    _touch_photo_batch(user_id, create=True, image=(image_bytes, mime),
                       message_id=message_id, reply_token=reply_token)


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

        # Exactly-once per LINE message id. The isRedelivery check above only
        # catches retries LINE flags as such; an unflagged duplicate delivery
        # used to be processed again, logging the same meal twice (observed
        # 2026-08-19: one request, two logs 3s apart, two Google Health points).
        if not db.claim_message(msg.get("id", ""), user_id):
            log.info("skipping already-processed message %s", msg.get("id"))
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
    reply, _, extra_flex = handle_message(user_id, text)
    return {"reply": reply, "flex": [f.bubble for f in extra_flex]}


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
