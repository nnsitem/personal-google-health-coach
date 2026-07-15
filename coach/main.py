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
from fastapi import FastAPI, Request, Response

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


@app.post("/webhook")
async def line_webhook(request: Request):
    body = await request.body()
    signature = request.headers.get("X-Line-Signature", "")

    if not _valid_line_signature(body, signature):
        return Response(status_code=403)

    payload = json.loads(body)
    events = payload.get("events", [])

    from coach.line import send_text as push_text

    for event in events:
        if event.get("type") != "message":
            continue

        msg = event.get("message", {})
        msg_type = msg.get("type")
        user_id = event.get("source", {}).get("userId", "")

        # Only respond to your own messages (or all if LINE_USER_ID not set)
        if LINE_USER_ID and user_id != LINE_USER_ID:
            log.warning("dropping message from unknown user %s", user_id)
            continue

        if msg_type == "text":
            text = msg["text"]
            log.info("LINE message from %s: %s", user_id, text)
            try:
                reply = handle_message(text)
                push_text(reply, to=user_id)
                log.info("replied via LINE push: %s", reply[:80])
            except Exception:
                log.exception("failed to handle LINE message")

        elif msg_type == "image":
            message_id = msg.get("id", "")
            log.info("LINE image from %s (id=%s) — analyzing food", user_id, message_id)
            try:
                from coach.line import get_image_content
                from coach.food import handle_food_photo
                image_bytes = get_image_content(message_id)
                reply = handle_food_photo(image_bytes)
                push_text(reply, to=user_id)
                log.info("food photo processed: %s", reply[:80])
            except Exception:
                log.exception("failed to handle food photo")
                push_text("ขออภัยครับ วิเคราะห์รูปอาหารไม่สำเร็จ ลองใหม่อีกครั้งนะครับ 🙏", to=user_id)

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
