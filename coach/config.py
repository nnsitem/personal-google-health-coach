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
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-pro-latest")
# Chosen for STABILITY, measured 2026-08-23 with 5 identical calls per model:
#
#   5/5  gemini-3.5-flash-lite      p50 1.02s  max  1.13s
#   5/5  gemini-flash-lite-latest   p50 1.14s  max  1.23s
#   5/5  gemini-3.7-flash           p50 2.65s  max  6.97s
#   5/5  gemini-pro-latest          p50 2.72s  max  3.74s
#   5/5  gemini-3.6-flash           p50 8.50s  max 14.97s
#   5/5  gemini-3.5-flash           p50 11.30s max 13.42s
#   3/5  gemini-flash-latest        ConnectError + 503        <- the only failure
#
# gemini-flash-latest had been the primary, and it is the one model that failed —
# matching every incident of 2026-08-22/23: a 502, a 503, and a request that hung
# 30m32s. An alias tracking the newest release rides the least settled capacity.
# gemini-pro-latest was perfect here, has the tightest spread of the capable
# models, accepts every thinking level, and is the most accurate — so it serves
# both this instruction and the accuracy-first one.
#
# Fallbacks are pinned GA versions rather than aliases: no silent tier change,
# and neither has an announced shutdown date. Slower than the aliases, which the
# owner has explicitly deprioritised. gemini-3.7-flash is left out despite 5/5
# here because it answered 503 on every thinking level an hour earlier.
#
# Re-measure occasionally: pinned models do not auto-upgrade.
GEMINI_FALLBACK_MODELS = ["gemini-3.5-flash", "gemini-3.5-flash-lite"]

# Used for calls where a wrong number is worse than a slow answer: estimating a
# meal's nutrition from a photo, and the chat turn that writes the log entry.
GEMINI_ACCURACY_MODEL = os.environ.get("GEMINI_ACCURACY_MODEL", "gemini-pro-latest")

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
