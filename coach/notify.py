"""User-facing failure notifications (DESIGN-V2 §12 items 1 & 4).

Scheduled jobs fail silently from the user's point of view — they just stop
getting data or summaries. This module turns repeated failures into a single
LINE message per failure streak:

- 'google_auth'   — token refresh / 401/403: "reconnect with: login"
- 'sync'          — hourly sync failing for another reason
- 'daily_summary' — the daily brief failing (bad Gemini key, etc.)

A streak notifies ONCE when it crosses its threshold; the flag resets when the
job next succeeds (db.clear_failures), so a later breakage notifies again.
"""

import logging

from coach import db
from coach.line import send_text

log = logging.getLogger(__name__)

# Consecutive failures before the user is told. Sync-related kinds run hourly
# (~3h of breakage); the daily summary runs once a day, so 2 = two missed days.
DEFAULT_THRESHOLD = 3

_MESSAGES = {
    "google_auth": (
        "⚠️ Google Health disconnected\n\n"
        "I haven't been able to access your Google Health data — your "
        "authorization has likely expired or been revoked.\n\n"
        "Please reconnect — send: login"
    ),
    "sync": (
        "⚠️ Health data sync trouble\n\n"
        "Your Google Health sync has been failing, so my picture of your data "
        "may be stale. I'll keep retrying automatically.\n\n"
        "If this keeps up, try reconnecting — send: login"
    ),
    "daily_summary": (
        "⚠️ Daily summary problem\n\n"
        "I couldn't put together your daily summary the last couple of days.\n\n"
        "If your Gemini key changed or hit its quota, set a new one — send: set key"
    ),
}


def is_auth_error(exc: BaseException) -> bool:
    """Whether an exception means the user's Google authorization is broken
    (refresh failed / token rejected) rather than a transient API problem."""
    from coach.health_api import HealthAPIError
    try:
        from google.auth.exceptions import RefreshError
    except ImportError:  # pragma: no cover
        RefreshError = ()
    if isinstance(exc, RefreshError):
        return True
    if isinstance(exc, HealthAPIError) and exc.status in (401, 403):
        return True
    return False


def record_failure(user_id: str, kind: str, detail: str = "",
                   threshold: int = DEFAULT_THRESHOLD) -> None:
    """Count a failure; notify the user once when the streak hits threshold.

    Never raises — this runs inside scheduler error handlers, and a broken
    notification must not mask the original failure.
    """
    try:
        count, notified = db.bump_failure(user_id, kind, detail)
        if count < threshold or notified:
            return
        send_text(_MESSAGES.get(kind, _MESSAGES["sync"]), to=user_id)
        # Marked only after a successful send, so a failed push (e.g. monthly
        # quota exhausted) retries on the next failure instead of going silent.
        db.mark_failure_notified(user_id, kind)
        log.info("notified user %s about %s failures (streak=%d)", user_id, kind, count)
    except Exception:
        log.exception("failed to record/notify %s failure for user %s", kind, user_id)


def record_success(user_id: str, *kinds: str) -> None:
    """Reset failure streaks after a successful run. Never raises."""
    try:
        for kind in kinds:
            db.clear_failures(user_id, kind)
    except Exception:
        log.exception("failed to clear failure state for user %s", user_id)
