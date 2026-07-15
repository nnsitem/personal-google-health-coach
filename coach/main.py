"""FastAPI app: LINE webhook + in-process scheduler.

- POST /webhook   LINE Messaging API webhook (inbound messages → chat agent → reply)
- GET  /healthz   liveness check
- POST /chat      local testing endpoint

The hourly sync runs via APScheduler inside this process, so there is no
host-level cron — the container is fully self-contained and OS-portable.
"""

import hashlib
import hmac
import json
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import FastAPI, Request, Response, BackgroundTasks

from coach import db
from coach.config import LINE_CHANNEL_SECRET, LINE_USER_ID, TZ
from coach.config import DAILY_SUMMARY_HOUR, DAILY_SUMMARY_MINUTE
from coach.sync import run_sync
from coach.daily import run_daily_summary
from coach.nudges import run_nudge_check
from coach.chat import handle_message
from coach.line import reply_text
from coach.weekly import run_weekly_report

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

scheduler = BackgroundScheduler(timezone=str(TZ))


def _safe_sync() -> None:
    try:
        run_sync()
    except Exception:
        log.exception("hourly sync failed")


def _safe_daily_summary() -> None:
    try:
        run_daily_summary()
    except Exception:
        log.exception("daily summary failed")


def _safe_nudge_check() -> None:
    try:
        run_nudge_check()
    except Exception:
        log.exception("nudge check failed")


def _safe_weekly_report() -> None:
    try:
        run_weekly_report()
    except Exception:
        log.exception("weekly report failed")


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    scheduler.add_job(_safe_sync, "cron", minute=5, id="hourly_sync")
    scheduler.add_job(
        _safe_daily_summary, "cron",
        hour=DAILY_SUMMARY_HOUR, minute=DAILY_SUMMARY_MINUTE,
        id="daily_summary",
    )
    scheduler.add_job(_safe_nudge_check, "cron", minute=35, id="hourly_nudge")
    scheduler.add_job(
        _safe_weekly_report, "cron",
        day_of_week="sun", hour=9, minute=0,
        id="weekly_report",
    )
    scheduler.start()
    log.info(
        "scheduler started (sync at :05, nudges at :35, daily at %02d:%02d, weekly Sun 9:00)",
        DAILY_SUMMARY_HOUR, DAILY_SUMMARY_MINUTE,
    )
    yield
    scheduler.shutdown(wait=False)


app = FastAPI(lifespan=lifespan)


@app.get("/healthz")
def healthz():
    return {"ok": True, "ts": datetime.now(timezone.utc).isoformat()}


# --- LINE Webhook ---

def _valid_line_signature(body: bytes, signature: str) -> bool:
    """Validate LINE webhook signature using channel secret."""
    if not LINE_CHANNEL_SECRET:
        log.warning("LINE_CHANNEL_SECRET not set — skipping signature check")
        return True
    hash_value = hmac.new(
        LINE_CHANNEL_SECRET.encode(), body, hashlib.sha256
    ).digest()
    import base64
    expected = base64.b64encode(hash_value).decode()
    return hmac.compare_digest(expected, signature)


def _detect_image_mime(data: bytes) -> str:
    """Sniff the image mime type from magic bytes (LINE images vary)."""
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    return "image/jpeg"  # sensible default


def _process_text_message(user_id: str, text: str) -> None:
    from coach.line import send_text as push_text
    log.info("LINE message from %s: %s", user_id, text)
    try:
        reply = handle_message(text)
        push_text(reply, to=user_id)
        log.info("replied via LINE push: %s", reply[:80])
    except Exception:
        log.exception("failed to handle LINE message")


def _process_image_message(user_id: str, message_id: str) -> None:
    from coach.line import send_text as push_text, get_image_content
    from coach.food import handle_food_photo
    log.info("LINE image from %s (id=%s) — analyzing", user_id, message_id)
    try:
        image_bytes = get_image_content(message_id)
        mime = _detect_image_mime(image_bytes)
        reply = handle_food_photo(image_bytes, mime_type=mime)
        push_text(reply, to=user_id)
        log.info("photo processed: %s", reply[:80])
    except Exception:
        log.exception("failed to handle photo")
        try:
            push_text("Sorry, I couldn't analyze that photo. Please try again. 🙏", to=user_id)
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

        # Only respond to your own messages (or all if LINE_USER_ID not set)
        if LINE_USER_ID and user_id != LINE_USER_ID:
            log.warning("dropping message from unknown user %s", user_id)
            continue

        # Process in the background so we return 200 to LINE immediately
        # (Gemini calls take several seconds; slow webhooks get retried/disabled).
        if msg_type == "text":
            background_tasks.add_task(_process_text_message, user_id, msg["text"])
        elif msg_type == "image":
            background_tasks.add_task(_process_image_message, user_id, msg.get("id", ""))

    return {"ok": True}


# --- Local testing endpoint ---

@app.post("/chat")
async def chat_endpoint(request: Request):
    """Direct chat endpoint for local testing. Send JSON: {"message": "..."}"""
    body = await request.json()
    text = body.get("message", "")
    if not text:
        return {"error": "missing 'message' field"}
    reply = handle_message(text)
    return {"reply": reply}
