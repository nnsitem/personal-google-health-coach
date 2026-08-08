"""Daily summary orchestrator: sync → generate → deliver via LINE.

Run manually:  python -m coach.daily
Also invoked by the scheduler at 10:00am local time.
"""

import logging
from datetime import datetime

from coach import db
from coach.ai import build_daily_snapshot, generate_daily_narrative
from coach.flex import (build_daily_report_bubble, delta_chip, report_labels,
                        REPORT_LABELS, COLOR_DAILY)
from coach.sync import run_sync
from coach.line import send_messages, flex_message, LineError

log = logging.getLogger(__name__)


def _readiness_pill(readiness: dict | None, labels: dict) -> str | None:
    """Short header pill from the trends.readiness verdict + score."""
    if not readiness:
        return None
    verdict = (readiness.get("verdict") or "").lower()
    score = readiness.get("score")
    if not verdict:
        return None
    if "well recovered" in verdict:
        label = labels["rd_well"]
    elif "under-recovered" in verdict:
        label = labels["rd_under"]
    elif "fatigue" in verdict or "illness" in verdict:
        label = labels["rd_fatigue"]
    else:
        label = labels["rd_normal"]
    return f"{label} · {score}" if score is not None else label


def _fmt_date(iso: str, labels: dict) -> str:
    """'2026-08-08' → localized 'SAT 8 AUG' / 'ส. 8 ส.ค.'."""
    try:
        d = datetime.fromisoformat(iso)
    except (ValueError, TypeError):
        return ""
    wd = labels["weekdays"][d.weekday()]
    mon = labels["months"][d.month - 1]
    text = f"{wd} {d.day} {mon}"
    # English abbreviations read better uppercased; Thai has no case.
    return text.upper() if labels is REPORT_LABELS["en"] else text


def _latest_and_avg(trends: dict, metric: str) -> tuple[float | None, float | None]:
    """Today's value (falling back to yesterday) and the 7-day average."""
    m = trends.get(metric) or {}
    latest = m.get("today") if m.get("today") is not None else m.get("yesterday")
    return latest, m.get("week_avg")


def _daily_view_model(snapshot: dict, labels: dict) -> dict:
    """Turn the daily snapshot into rows the Flex builder can render directly.
    All numbers are deterministic here — Gemini only writes the narrative."""
    trends = snapshot.get("trends") or {}

    recovery_rows: list[tuple[str, str, dict | None]] = []
    rhr, rhr_avg = _latest_and_avg(trends, "resting_hr")
    if rhr is not None:
        recovery_rows.append((labels["resting_hr"], f"{int(round(rhr))} bpm",
                              delta_chip(rhr, rhr_avg, higher_is_better=False)))
    hrv, hrv_avg = _latest_and_avg(trends, "hrv_ms")
    if hrv is not None:
        recovery_rows.append((labels["hrv"], f"{int(round(hrv))} ms",
                              delta_chip(hrv, hrv_avg, higher_is_better=True)))
    spo2, spo2_avg = _latest_and_avg(trends, "spo2_pct")
    if spo2 is not None:
        recovery_rows.append((labels["spo2"], f"{round(spo2, 1)}%", None))

    activity_rows: list[tuple[str, str, dict | None]] = []
    steps, steps_avg = _latest_and_avg(trends, "steps")
    if steps is not None:
        activity_rows.append((labels["steps"], f"{int(round(steps)):,}",
                              delta_chip(steps, steps_avg, higher_is_better=True)))
    azm, azm_avg = _latest_and_avg(trends, "active_zone_min")
    if azm is not None:
        activity_rows.append((labels["azm"], f"{int(round(azm))}",
                              delta_chip(azm, azm_avg, higher_is_better=True)))
    cal, _ = _latest_and_avg(trends, "calories_kcal")
    if cal is not None:
        activity_rows.append((labels["calories"], f"{int(round(cal)):,} kcal", None))

    sleep_label = None
    sleep_stage_min = None
    sessions = snapshot.get("sleep") or []
    if sessions:
        s0 = sessions[0]  # most recent (DESC order)
        sleep_stage_min = {
            "DEEP": s0.get("deep_min", 0), "LIGHT": s0.get("light_min", 0),
            "REM": s0.get("rem_min", 0), "AWAKE": s0.get("awake_min", 0),
        }
        hrs = s0.get("duration_hours")
        sleep_label = f"{labels['sleep']} · {hrs}h {labels['asleep']}" if hrs else labels["sleep"]

    return {
        "date_label": _fmt_date(snapshot.get("today", ""), labels),
        "readiness_pill": _readiness_pill(trends.get("readiness"), labels),
        "recovery_rows": recovery_rows,
        "sleep_label": sleep_label,
        "sleep_stage_min": sleep_stage_min,
        "activity_rows": activity_rows,
    }


def run_daily_summary(user_id: str) -> str:
    """Full daily flow: refresh data, generate summary, send via LINE.

    Returns the generated narrative text.
    """
    db.init_db()

    # 1. Sync latest data so the snapshot is fresh
    log.info("refreshing health data before daily summary...")
    try:
        run_sync(user_id)
    except Exception:
        log.exception("sync failed before daily summary — proceeding with stale data")

    # 2. Build the snapshot once; derive deterministic stat rows + the narrative
    snapshot = build_daily_snapshot(user_id)
    labels = report_labels(db.get_user_language(user_id))
    vm = _daily_view_model(snapshot, labels)

    log.info("generating daily narrative with Gemini...")
    narrative = generate_daily_narrative(user_id, snapshot)
    log.info("daily narrative generated (%d chars)", len(narrative))

    # 3. Deliver via LINE as a data-driven Flex report card
    try:
        bubble = build_daily_report_bubble(
            color=COLOR_DAILY,
            date_label=vm["date_label"],
            readiness_pill=vm["readiness_pill"],
            recovery_rows=vm["recovery_rows"],
            sleep_label=vm["sleep_label"],
            sleep_stage_min=vm["sleep_stage_min"],
            activity_rows=vm["activity_rows"],
            narrative=narrative,
            labels=labels,
        )
        send_messages([flex_message("🌅 Your daily health brief is ready", bubble)], to=user_id)
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

    return narrative


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    DEFAULT_USER_ID = "U1068a1b9c15b44e7ff1439bdefdeb5dc"
    print(run_daily_summary(DEFAULT_USER_ID))
