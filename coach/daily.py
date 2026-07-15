"""Daily summary orchestrator: sync → generate → deliver via LINE.

Run manually:  python -m coach.daily
Also invoked by the scheduler at 7:30am local time.
"""

import logging

from coach import db
from coach.ai import generate_daily_summary
from coach.sync import run_sync
from coach.line import send_text, LineError

log = logging.getLogger(__name__)


def run_daily_summary() -> str:
    """Full daily flow: refresh data, generate summary, send via LINE.

    Returns the generated message text.
    """
    db.init_db()

    # 1. Sync latest data so the snapshot is fresh
    log.info("refreshing health data before daily summary...")
    try:
        run_sync()
    except Exception:
        log.exception("sync failed before daily summary — proceeding with stale data")

    # 2. Generate coaching message via Gemini
    log.info("generating daily summary with Gemini...")
    message = generate_daily_summary()
    log.info("daily summary generated (%d chars)", len(message))

    # 3. Deliver via LINE
    try:
        send_text(message)
        log.info("daily summary sent via LINE")

        # Mark as delivered
        with db.connect() as conn:
            conn.execute(
                """
                UPDATE insights SET delivered = 1
                WHERE rowid = (
                    SELECT rowid FROM insights
                    WHERE kind = 'daily_summary' AND delivered = 0
                    ORDER BY ts DESC LIMIT 1
                )
                """,
            )
    except LineError as e:
        log.error("LINE delivery failed: %s", e)
        log.info("message was saved to insights table for retry")

    return message


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    print(run_daily_summary())
