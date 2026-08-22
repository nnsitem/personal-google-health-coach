"""Central configuration. Everything comes from env vars + the data/ folder."""

import os
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

load_dotenv()

# All persistent state (SQLite DB, Google tokens) lives here — bind-mounted in Docker.
DATA_DIR = Path(os.environ.get("COACH_DATA_DIR", "data")).resolve()
DATA_DIR.mkdir(parents=True, exist_ok=True)

DB_PATH = DATA_DIR / "coach.db"
GOOGLE_CLIENT_SECRET_FILE = DATA_DIR / "google_client_secret.json"
GOOGLE_CLIENT_SECRET_WEB_FILE = DATA_DIR / "google_client_secret_web.json"  # Web app client for multi-user OAuth
GOOGLE_TOKEN_FILE = DATA_DIR / "google_token.json"

GOOGLE_HEALTH_BASE = "https://health.googleapis.com/v4"
GOOGLE_HEALTH_SCOPES = [
    "https://www.googleapis.com/auth/googlehealth.activity_and_fitness.readonly",
    "https://www.googleapis.com/auth/googlehealth.sleep.readonly",
    "https://www.googleapis.com/auth/googlehealth.health_metrics_and_measurements.readonly",
    "https://www.googleapis.com/auth/googlehealth.nutrition.readonly",
    "https://www.googleapis.com/auth/googlehealth.nutrition.writeonly",
]

# LINE Messaging API
LINE_CHANNEL_SECRET = os.environ.get("LINE_CHANNEL_SECRET", "")
LINE_CHANNEL_ACCESS_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "")
# Per-user LINE_USER_ID check was removed in v2 — user identity is now per-user
# in the DB. The env var is kept as the owner's own id, repurposed as the
# recipient for infra-level alerts (e.g. coach.watchdog's auto-restart
# notifications) that aren't tied to any particular user.
ADMIN_LINE_USER_ID = os.environ.get("LINE_USER_ID", "")

TZ = ZoneInfo(os.environ.get("TZ", "UTC"))

# Gemini (Google AI) settings
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-flash-latest")
# Fallback models if primary is unavailable — each must be a genuinely different
# capacity tier or a flash 503 spike takes out the whole chain. Note that
# gemini-flash-latest is an alias for gemini-3.5-flash (since 2026-05-19), so
# listing 3.5-flash as a fallback adds nothing; flash-lite and pro run on
# separate capacity (verified responsive during a flash 503 outage).
# Ordered STRONGEST FIRST. It used to fall back to flash-lite before pro, so the
# first thing tried after a flash outage was the weakest model available — the
# wrong direction when the answer is a nutrition estimate the user will act on.
GEMINI_FALLBACK_MODELS = ["gemini-pro-latest", "gemini-flash-lite-latest"]

# Used for calls where a wrong number is worse than a slow answer: estimating a
# meal's nutrition from a photo, and the chat turn that writes the log entry.
GEMINI_ACCURACY_MODEL = os.environ.get("GEMINI_ACCURACY_MODEL", "gemini-pro-latest")

# Thinking effort. Measured 2026-08-23 on a nutrition question with a known
# answer (~525 kcal): LOW -> 514, MEDIUM -> 524, HIGH -> 525, costing 531 / 929 /
# 2103 thinking tokens. MINIMAL is now rejected outright (400 INVALID_ARGUMENT),
# and MEDIUM/HIGH currently answer 503 on the flash tiers, so LOW is the highest
# level that is universally available. Raise it here if that changes — and raise
# the output caps with it, since thinking tokens share that budget.
GEMINI_THINKING_LEVEL = os.environ.get("GEMINI_THINKING_LEVEL", "LOW")
# Total time budget (seconds) to keep retrying Gemini across models. Replies go
# via LINE push (not a time-limited reply token), so we can afford a long window.
GEMINI_MAX_WAIT_SECONDS = int(os.environ.get("GEMINI_MAX_WAIT_SECONDS", "120"))
# Hard ceiling on a SINGLE Gemini HTTP request. The SDK applies none by
# default, and on 2026-08-22 one generateContent call hung for 30 minutes
# before finally answering 502 — the user's "เพิ่มน้ำ 250ml" was logged and
# answered half an hour later, long after the LINE reply token had expired.
# GEMINI_MAX_WAIT_SECONDS only decides whether to start another round; it
# cannot interrupt a request already in flight, so this is what bounds it.
# 120s, not 30s: accuracy is preferred over speed here, and cutting off a slow
# but correct answer only to retry on a weaker model is the wrong trade. Still a
# hard bound — the incident this exists for was a single request hanging 30m32s.
GEMINI_REQUEST_TIMEOUT_SECONDS = int(os.environ.get("GEMINI_REQUEST_TIMEOUT_SECONDS", "120"))

# Daily summary delivery time (local TZ)
DAILY_SUMMARY_HOUR = int(os.environ.get("DAILY_SUMMARY_HOUR", "10"))
DAILY_SUMMARY_MINUTE = int(os.environ.get("DAILY_SUMMARY_MINUTE", "0"))

# Trailing window re-fetched on every sync; device sync lag means data for
# "yesterday" keeps changing, so we always re-read and upsert.
SYNC_LOOKBACK_HOURS = 48
