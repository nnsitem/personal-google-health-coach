"""Historical aggregation and trends.

Turns raw daily metrics + sleep sessions into compact, comparative summaries
the coach can reason over: today vs yesterday, this week's averages, this
month's averages, and week-over-week trends.

This is what lets the coach "learn from behavior" rather than just report a
single day's numbers.
"""

import json
import logging
from datetime import datetime, timedelta

from coach import db
from coach.config import TZ

log = logging.getLogger(__name__)


# ---- value extraction ------------------------------------------------------

def _steps(v: dict):
    x = v.get("steps", {}).get("countSum")
    return int(x) if x is not None else None


def _calories(v: dict):
    x = v.get("totalCalories", {}).get("kcalSum")
    return round(float(x)) if x is not None else None


def _resting_hr(v: dict):
    x = v.get("dailyRestingHeartRate", {}).get("beatsPerMinute")
    return int(x) if x is not None else None


def _azm(v: dict):
    a = v.get("activeZoneMinutes", {})
    if not a:
        return None
    return (
        int(a.get("sumInFatBurnHeartZone", 0))
        + int(a.get("sumInCardioHeartZone", 0))
        + int(a.get("sumInPeakHeartZone", 0))
    )


_EXTRACTORS = {
    "steps": _steps,
    "total-calories": _calories,
    "daily-resting-heart-rate": _resting_hr,
    "active-zone-minutes": _azm,
}


def _load_daily_series(user_id: str, days: int) -> dict[str, dict[str, float]]:
    """Return {data_type: {day: value}} for the last `days` days."""
    today = datetime.now(TZ).date()
    cutoff = (today - timedelta(days=days)).isoformat()

    series: dict[str, dict[str, float]] = {dt: {} for dt in _EXTRACTORS}
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT day, data_type, value_json FROM metrics WHERE user_id = ? AND day >= ? ORDER BY day",
            (user_id, cutoff),
        ).fetchall()

    for row in rows:
        dt = row["data_type"]
        extractor = _EXTRACTORS.get(dt)
        if not extractor:
            continue
        try:
            val = extractor(json.loads(row["value_json"]))
        except (ValueError, KeyError, TypeError):
            val = None
        if val is not None:
            series[dt][row["day"]] = val
    return series


def _avg(values: list[float]) -> float | None:
    vals = [v for v in values if v is not None]
    return round(sum(vals) / len(vals), 1) if vals else None


def _window_avg(day_map: dict[str, float], start_days_ago: int, end_days_ago: int) -> float | None:
    """Average of values whose day is in [today-start, today-end)."""
    today = datetime.now(TZ).date()
    picked = []
    for d, v in day_map.items():
        try:
            dd = datetime.fromisoformat(d).date()
        except ValueError:
            continue
        age = (today - dd).days
        if end_days_ago <= age < start_days_ago:
            picked.append(v)
    return _avg(picked)


def _trend(this_week: float | None, last_week: float | None) -> str | None:
    """Human-readable week-over-week trend."""
    if this_week is None or last_week is None or last_week == 0:
        return None
    pct = (this_week - last_week) / last_week * 100
    if abs(pct) < 3:
        return "steady"
    return f"{'up' if pct > 0 else 'down'} {abs(round(pct))}% vs last week"


def _sleep_series(user_id: str, days: int) -> dict[str, float]:
    """Return {date: total_sleep_hours} for the last `days` days."""
    today = datetime.now(TZ).date()
    cutoff = (today - timedelta(days=days)).isoformat()
    out: dict[str, float] = {}
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT start, end, stages_json FROM sleep_sessions WHERE user_id = ? AND start >= ? ORDER BY start",
            (user_id, cutoff),
        ).fetchall()
    for row in rows:
        stages = json.loads(row["stages_json"]) if row["stages_json"] else []
        total_min = 0.0
        for s in stages:
            try:
                st = datetime.fromisoformat(s["startTime"].replace("Z", "+00:00"))
                en = datetime.fromisoformat(s["endTime"].replace("Z", "+00:00"))
                if s.get("type") in ("DEEP", "LIGHT", "REM", "AWAKE"):
                    total_min += (en - st).total_seconds() / 60
            except (ValueError, KeyError):
                continue
        if total_min > 0:
            # Attribute the sleep to its wake-up (end) local date
            try:
                end_local = datetime.fromisoformat(row["end"].replace("Z", "+00:00")).astimezone(TZ)
                out[end_local.date().isoformat()] = round(total_min / 60, 1)
            except (ValueError, KeyError):
                continue
    return out


def build_trends(user_id: str) -> dict:
    """Build a compact multi-window summary with today, yesterday, weekly and
    monthly averages, and week-over-week trends for each metric.
    """
    today = datetime.now(TZ).date()
    yesterday = today - timedelta(days=1)
    t_iso, y_iso = today.isoformat(), yesterday.isoformat()

    series = _load_daily_series(user_id, 35)  # enough for month + prior-week comparison
    out: dict = {"as_of": datetime.now(TZ).strftime("%Y-%m-%d %H:%M")}

    labels = {
        "steps": "steps",
        "total-calories": "calories_kcal",
        "daily-resting-heart-rate": "resting_hr",
        "active-zone-minutes": "active_zone_min",
    }

    for dt, key in labels.items():
        day_map = series.get(dt, {})
        this_week = _window_avg(day_map, 7, 0)
        last_week = _window_avg(day_map, 14, 7)
        out[key] = {
            "today": day_map.get(t_iso),
            "yesterday": day_map.get(y_iso),
            "week_avg": this_week,
            "month_avg": _window_avg(day_map, 30, 0),
            "trend": _trend(this_week, last_week),
        }

    # Sleep
    sleep_map = _sleep_series(user_id, 35)
    this_week_sleep = _window_avg(sleep_map, 7, 0)
    last_week_sleep = _window_avg(sleep_map, 14, 7)
    out["sleep_hours"] = {
        "last_night": sleep_map.get(t_iso) or sleep_map.get(y_iso),
        "week_avg": this_week_sleep,
        "month_avg": _window_avg(sleep_map, 30, 0),
        "trend": _trend(this_week_sleep, last_week_sleep),
    }

    return out


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    db.init_db()

    DEFAULT_USER_ID = "U1068a1b9c15b44e7ff1439bdefdeb5dc"
    print(json.dumps(build_trends(DEFAULT_USER_ID), indent=2))
