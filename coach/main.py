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

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

scheduler = BackgroundScheduler(timezone=str(TZ))


# --- Scheduled jobs (multi-user: iterate all configured users) --------------

def _safe_sync_all() -> None:
    """Hourly sync for all active users with a Google token."""
    from coach.sync import run_sync
    for user in db.list_active_users():
        uid = user["line_user_id"]
        try:
            run_sync(uid)
        except Exception:
            log.exception("sync failed for user %s", uid)


def _safe_daily_summary_all() -> None:
    """Daily summary for all users with both Google token + Gemini key."""
    from coach.daily import run_daily_summary
    for user in db.list_active_users():
        uid = user["line_user_id"]
        if not user.get("gemini_api_key"):
            continue
        try:
            run_daily_summary(uid)
        except Exception:
            log.exception("daily summary failed for user %s", uid)


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
    """Weekly report for all configured users."""
    from coach.weekly import run_weekly_report
    for user in db.list_active_users():
        uid = user["line_user_id"]
        if not user.get("gemini_api_key"):
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
    scheduler.add_job(_safe_backfill_all, "date", id="startup_backfill")
    scheduler.add_job(_safe_sync_all, "cron", minute=5, id="hourly_sync")
    scheduler.add_job(
        _safe_daily_summary_all, "cron",
        hour=DAILY_SUMMARY_HOUR, minute=DAILY_SUMMARY_MINUTE,
        id="daily_summary",
    )
    scheduler.add_job(_safe_nudge_check_all, "cron", minute=35, id="hourly_nudge")
    scheduler.add_job(
        _safe_weekly_report_all, "cron",
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


def _send_welcome(user_id: str) -> None:
    """Send a welcome message to brand-new users explaining setup."""
    push_text(
        "👋 Welcome to your AI Health Coach!\n\n"
        "I help you track your health, analyze food photos, and give personalized coaching — all via this chat.\n\n"
        "To get started, you need two things:\n\n"
        "1️⃣ Connect Google Health\n"
        "   → Send: login\n\n"
        "2️⃣ Set your Gemini AI key\n"
        "   → Send: set key\n"
        "   (Get a free key at aistudio.google.com/apikey)\n\n"
        "Once both are set, just chat with me or send a food photo! 🍽️💪",
        to=user_id,
    )
    log.info("sent welcome to new user %s", user_id)


def _process_text_message(user_id: str, text: str) -> None:
    """Handle a text message in the background."""
    log.info("LINE message from %s: %s", user_id, text)

    # Check for onboarding commands before passing to chat agent
    lower = text.strip().lower()

    # Login command: send the Google OAuth URL
    if lower in ("login", "login google", "connect google", "เชื่อมต่อ google", "action=login_google"):
        try:
            from coach.oauth import _sign_state
            import os
            host = os.environ.get("PUBLIC_HOST", "coach.signagegold.co")
            state = _sign_state(user_id)
            login_url = f"https://{host}/auth/google?state={state}"
            push_text(
                f"🔗 Open this link to connect your Google Health account:\n\n{login_url}\n\n"
                "Sign in with the Google account linked to your Fitbit/Pixel Watch.",
                to=user_id,
            )
            log.info("sent login URL to %s", user_id)
        except Exception:
            log.exception("failed to generate login URL")
            push_text("Sorry, I couldn't generate a login link. Please try again.", to=user_id)
        return

    # Set Gemini key command: enter "awaiting key" mode
    if lower in ("set key", "set gemini key", "ตั้งค่า key", "เปลี่ยน key", "action=set_gemini_key"):
        db.update_user(user_id, onboarding_state="awaiting_gemini_key")
        push_text(
            "🔑 Please paste your Gemini API key.\n\n"
            "Get one free from: https://aistudio.google.com/apikey\n\n"
            "Just send the key as your next message (starts with 'AI...' or 'AQ...').",
            to=user_id,
        )
        log.info("user %s entering Gemini key setup mode", user_id)
        return

    # Check if user is in "awaiting key" mode — validate and store the key
    user = db.get_user(user_id)
    if user and user.get("onboarding_state") == "awaiting_gemini_key":
        _handle_gemini_key_input(user_id, text.strip())
        return

    # Require full setup before using the coach
    if not _ensure_configured(user_id, user):
        return

    # Pass to the chat agent
    try:
        reply = handle_message(user_id, text)
        push_text(reply, to=user_id)
        log.info("replied via LINE push: %s", reply[:80])
    except Exception:
        log.exception("failed to handle LINE message")


def _ensure_configured(user_id: str, user: dict | None) -> bool:
    """Check the user has both a Gemini key and Google token. If not, send a
    reminder and return False. This gates every path that would otherwise hit
    the user's Gemini key or Google token (preventing fallback to the owner's).
    """
    if not user or not user.get("gemini_api_key"):
        push_text(
            "🔑 You haven't set up your AI key yet.\n"
            "Send: set key\n\n"
            "Get a free one at: https://aistudio.google.com/apikey",
            to=user_id,
        )
        return False

    if not user.get("google_token_json"):
        push_text(
            "🔗 You haven't connected Google Health yet.\n"
            "Send: login\n\n"
            "This connects your Fitbit/Pixel Watch data.",
            to=user_id,
        )
        return False

    return True


def _handle_gemini_key_input(user_id: str, key: str) -> None:
    """Validate a Gemini API key and store it if valid."""
    # Basic format check
    if len(key) < 20 or " " in key or "\n" in key:
        push_text(
            "❌ That doesn't look like a valid API key. "
            "Please paste the full key (no spaces or line breaks).\n\n"
            "Or send 'cancel' to exit setup.",
            to=user_id,
        )
        return

    if key.lower() == "cancel":
        db.update_user(user_id, onboarding_state="")
        push_text("Key setup cancelled.", to=user_id)
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
        push_text(
            "❌ That key didn't work. Please check and try again.\n\n"
            "Error: " + str(e)[:100] + "\n\n"
            "Or send 'cancel' to exit setup.",
            to=user_id,
        )
        return

    # Key is valid — store it and exit onboarding mode
    db.update_user(user_id, gemini_api_key=key, onboarding_state="")
    push_text(
        "✅ Gemini API key saved and verified!\n\n"
        "Your AI health coach is now fully configured. "
        "Send me a message or a food photo to get started 💪",
        to=user_id,
    )
    log.info("stored valid Gemini key for user %s", user_id)


def _process_image_message(user_id: str, message_id: str) -> None:
    """Handle an image message in the background."""
    from coach.line import get_image_content
    from coach.food import handle_food_photo

    # Require full setup before touching the user's Gemini key / Google token
    if not _ensure_configured(user_id, db.get_user(user_id)):
        return

    log.info("LINE image from %s (id=%s) — analyzing", user_id, message_id)
    try:
        image_bytes = get_image_content(message_id)
        mime = _detect_image_mime(image_bytes)
        reply = handle_food_photo(user_id, image_bytes, mime_type=mime)
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

        if not user_id:
            continue

        # V2: auto-create user on first contact. If new, send welcome + skip to onboarding.
        is_new = db.get_user(user_id) is None
        db.get_or_create_user(user_id)

        if is_new:
            background_tasks.add_task(_send_welcome, user_id)
            continue

        # Process in the background so we return 200 to LINE immediately
        if msg_type == "text":
            background_tasks.add_task(_process_text_message, user_id, msg["text"])
        elif msg_type == "image":
            background_tasks.add_task(_process_image_message, user_id, msg.get("id", ""))

    return {"ok": True}


# --- Local testing endpoint -------------------------------------------------

@app.post("/chat")
async def chat_endpoint(request: Request):
    """Direct chat endpoint for local testing. Send JSON: {"message": "...", "user_id": "..."}"""
    body = await request.json()
    text = body.get("message", "")
    user_id = body.get("user_id", "U1068a1b9c15b44e7ff1439bdefdeb5dc")
    if not text:
        return {"error": "missing 'message' field"}
    reply = handle_message(user_id, text)
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
