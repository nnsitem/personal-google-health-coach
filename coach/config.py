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
# LINE_USER_ID removed in v2 — user identity is now per-user in the DB

TZ = ZoneInfo(os.environ.get("TZ", "UTC"))

# Gemini (Google AI) settings
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-flash-latest")
# Fallback models if primary is unavailable — ordered by capacity diversity so
# a 503 on the flash tier can fall through to a different (pro) tier.
GEMINI_FALLBACK_MODELS = ["gemini-3.5-flash", "gemini-pro-latest"]
# Total time budget (seconds) to keep retrying Gemini across models. Replies go
# via LINE push (not a time-limited reply token), so we can afford a long window.
GEMINI_MAX_WAIT_SECONDS = int(os.environ.get("GEMINI_MAX_WAIT_SECONDS", "120"))

# Daily summary delivery time (local TZ)
DAILY_SUMMARY_HOUR = int(os.environ.get("DAILY_SUMMARY_HOUR", "7"))
DAILY_SUMMARY_MINUTE = int(os.environ.get("DAILY_SUMMARY_MINUTE", "30"))

# Trailing window re-fetched on every sync; device sync lag means data for
# "yesterday" keeps changing, so we always re-read and upsert.
SYNC_LOOKBACK_HOURS = 48
