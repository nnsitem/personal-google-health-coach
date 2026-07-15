"""Hourly sync: pull the trailing 48h from Google Health into SQLite.

Run manually:  python -m coach.sync
Also invoked hourly by the scheduler in coach.main.

Idempotent: everything is upserted, so re-running (or re-reading a window
that already exists) is safe. Late-arriving data from device sync lag is
picked up because the window always covers the last 48 hours.
"""

import logging
from datetime import date, timedelta, datetime, timezone

from coach import db
from coach.config import SYNC_LOOKBACK_HOURS, TZ
from coach.health_api import HealthAPIError, HealthClient

log = logging.getLogger(__name__)

# Data types to sync via dailyRollUp (kebab-case as the API requires).
# Adjust after running `python -m coach.discover` to see what your account has.
DAILY_ROLLUP_TYPES = [
    "steps",
    "total-calories",
    "active-zone-minutes",
]

# Data types that only support list/reconcile (not dailyRollUp)
LIST_TYPES = [
    "daily-resting-heart-rate",
]


def sync_daily_rollups(client: HealthClient, start_date: str, end_date: str) -> None:
    for data_type in DAILY_ROLLUP_TYPES:
        try:
            points = client.daily_rollup(data_type, start_date, end_date)
        except HealthAPIError as e:
            log.warning("dailyRollUp failed for %s: %s", data_type, e)
            db.log_sync(data_type, ok=False, detail=str(e))
            continue
        for point in points:
            # DailyRollupDataPoint has civilStartTime and a value union field.
            civil_start = point.get("civilStartTime", {})
            date_obj = civil_start.get("date", {})
            day = ""
            if date_obj:
                y = date_obj.get("year", 0)
                m = date_obj.get("month", 0)
                d = date_obj.get("day", 0)
                if y and m and d:
                    day = f"{y:04d}-{m:02d}-{d:02d}"
            if not day:
                log.warning("no date on %s point, skipping: %s", data_type, point)
                continue
            db.upsert_metric(day, None, data_type, point, source="dailyRollUp")
        db.log_sync(data_type, ok=True, detail=f"{len(points)} points")
        log.info("synced %s: %d daily points", data_type, len(points))


def sync_sleep(client: HealthClient, start_date: str, end_date: str) -> None:
    """Sync sleep sessions using the list endpoint with a filter."""
    # Sleep uses civil_end_time filter per API docs
    filter_str = (
        f'sleep.interval.civil_end_time >= "{start_date}" '
        f'AND sleep.interval.civil_end_time < "{end_date}"'
    )
    try:
        sessions = client.list_points("sleep", filter_str)
    except HealthAPIError as e:
        log.warning("sleep list failed: %s", e)
        db.log_sync("sleep", ok=False, detail=str(e))
        return

    for session in sessions:
        sleep_data = session.get("sleep", session)
        interval = sleep_data.get("interval", {})
        start = interval.get("startTime") or ""
        end = interval.get("endTime") or ""
        if not (start and end):
            log.warning("sleep session missing start/end, skipping: %s", session)
            continue
        db.upsert_sleep_session(
            str(start), str(end),
            stages=sleep_data.get("stages"),
            efficiency=sleep_data.get("sleepEfficiency"),
            score=sleep_data.get("overallScore"),
        )
    db.log_sync("sleep", ok=True, detail=f"{len(sessions)} sessions")
    log.info("synced sleep: %d sessions", len(sessions))


def sync_list_types(client: HealthClient, start_date: str, end_date: str) -> None:
    """Sync data types that only support list (not dailyRollUp)."""
    for data_type in LIST_TYPES:
        # Use civil date filter with the data type name in snake_case for the filter field
        filter_field = data_type.replace("-", "_")
        filter_str = (
            f'{filter_field}.date >= "{start_date}" '
            f'AND {filter_field}.date < "{end_date}"'
        )
        try:
            points = client.list_points(data_type, filter_str)
        except HealthAPIError as e:
            log.warning("list failed for %s: %s", data_type, e)
            db.log_sync(data_type, ok=False, detail=str(e))
            continue
        for point in points:
            # Try to extract date from the point's nested data
            # For daily types like daily-resting-heart-rate, the date is nested
            # inside the camelCase version of the type name
            camel_key = data_type.replace("-", " ").title().replace(" ", "")
            camel_key = camel_key[0].lower() + camel_key[1:]  # lowerCamelCase
            type_data = point.get(camel_key, {})
            date_info = type_data.get("date", {})
            day = ""
            if isinstance(date_info, dict) and date_info:
                y = date_info.get("year", 0)
                m = date_info.get("month", 0)
                d = date_info.get("day", 0)
                if y and m and d:
                    day = f"{y:04d}-{m:02d}-{d:02d}"
            if not day:
                log.warning("no date on %s point, storing with start_date: %s", data_type, point)
                day = start_date
            db.upsert_metric(day, None, data_type, point, source="list")
        db.log_sync(data_type, ok=True, detail=f"{len(points)} points")
        log.info("synced %s (list): %d points", data_type, len(points))


def run_sync() -> None:
    db.init_db()
    client = HealthClient()

    today_local = datetime.now(TZ).date()
    # Cover trailing days for dailyRollUp (civil dates)
    lookback_days = SYNC_LOOKBACK_HOURS // 24 + 1
    start_date = (today_local - timedelta(days=lookback_days)).isoformat()
    # end_date is exclusive — use today (not tomorrow) to avoid future-date errors
    end_date = today_local.isoformat()

    sync_daily_rollups(client, start_date, end_date)
    sync_list_types(client, start_date, end_date)
    sync_sleep(client, start_date, end_date)
    log.info("sync complete (%s .. %s)", start_date, end_date)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    run_sync()
