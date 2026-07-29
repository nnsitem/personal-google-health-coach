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


def _pick(d: dict, *keys: str):
    """Return the first present field among candidate names, or None."""
    for k in keys:
        if k in d and d[k] is not None:
            return d[k]
    return None


def _hrv(v: dict):
    # Confirmed live from Mac mini sync logs 2026-07-30: real keys are
    # ['date', 'averageHeartRateVariabilityMilliseconds', 'entropy',
    # 'noRemSleepRootMeanSquareOfSuccessiveDifferencesMilliseconds',
    # 'deepSleepRootMeanSquareOfSuccessiveDifferencesMilliseconds'] — none of
    # the originally-guessed names (rmssdMillis/hrvMillis/millis) existed.
    nested = v.get("dailyHeartRateVariability", {})
    x = _pick(nested, "averageHeartRateVariabilityMilliseconds")
    if x is not None:
        return float(x)
    if nested:
        log.info("unrecognized daily-heart-rate-variability shape: %s", list(nested.keys()))
    return None


def _spo2(v: dict):
    # Confirmed live from Mac mini sync logs 2026-07-30: real keys are
    # ['date', 'averagePercentage', 'lowerBoundPercentage',
    # 'upperBoundPercentage', 'standardDeviationPercentage'] — none of the
    # originally-guessed names existed.
    nested = v.get("dailyOxygenSaturation", {})
    x = _pick(nested, "averagePercentage")
    if x is not None:
        return float(x)
    if nested:
        log.info("unrecognized daily-oxygen-saturation shape: %s", list(nested.keys()))
    return None


def _resp_rate(v: dict):
    nested = v.get("dailyRespiratoryRate", {})
    x = _pick(nested, "breathsPerMinute", "respirationsPerMinute", "rate")
    if x is not None:
        return float(x)
    if nested:
        log.info("unrecognized daily-respiratory-rate shape: %s", list(nested.keys()))
    return None


_EXTRACTORS = {
    "steps": _steps,
    "total-calories": _calories,
    "daily-resting-heart-rate": _resting_hr,
    "active-zone-minutes": _azm,
    "daily-heart-rate-variability": _hrv,
    "daily-oxygen-saturation": _spo2,
    "daily-respiratory-rate": _resp_rate,
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


def _asleep_minutes(stages: list[dict]) -> float:
    """Minutes actually asleep (DEEP/LIGHT/REM). AWAKE time is EXCLUDED —
    the Google Health app's headline number is time asleep, not time in bed,
    and the coach must quote the same figure the user sees in the app."""
    total = 0.0
    for s in stages:
        try:
            st = datetime.fromisoformat(s["startTime"].replace("Z", "+00:00"))
            en = datetime.fromisoformat(s["endTime"].replace("Z", "+00:00"))
            if s.get("type") in ("DEEP", "LIGHT", "REM"):
                total += (en - st).total_seconds() / 60
        except (ValueError, KeyError, TypeError):
            continue
    return total


def _sleep_series(user_id: str, days: int) -> dict[str, float]:
    """Return {wake_date: asleep_hours of the MAIN sleep period ending that date}.

    - Sessions separated by ≤3h are merged into one period, so an interrupted
      night isn't reported as only its final segment.
    - The LONGEST period of the date wins: an afternoon nap ends on the same
      local date as the night and previously OVERWROTE it (the coach then
      reported the nap's 3.5h while the app showed the night's 6h).
    """
    today = datetime.now(TZ).date()
    cutoff = (today - timedelta(days=days)).isoformat()
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT start, end, stages_json FROM sleep_sessions WHERE user_id = ? AND start >= ? ORDER BY start",
            (user_id, cutoff),
        ).fetchall()

    sessions = []
    for row in rows:
        try:
            st = datetime.fromisoformat(row["start"].replace("Z", "+00:00"))
            en = datetime.fromisoformat(row["end"].replace("Z", "+00:00"))
        except ValueError:
            continue
        stages = json.loads(row["stages_json"]) if row["stages_json"] else []
        asleep = _asleep_minutes(stages)
        if asleep > 0:
            sessions.append((st, en, asleep))

    # Merge near-adjacent sessions (gap ≤ 3h) into one sleep period
    periods: list[tuple] = []
    for st, en, asleep in sessions:
        if periods and (st - periods[-1][1]).total_seconds() <= 3 * 3600:
            pst, pen, pasleep = periods[-1]
            periods[-1] = (pst, max(pen, en), pasleep + asleep)
        else:
            periods.append((st, en, asleep))

    out: dict[str, float] = {}
    for _, en, asleep in periods:
        d = en.astimezone(TZ).date().isoformat()
        hours = round(asleep / 60, 1)
        if hours > out.get(d, 0.0):
            out[d] = hours
    return out


def _pct_dev(value: float | None, baseline: float | None) -> float | None:
    if value is None or not baseline:
        return None
    return (value - baseline) / baseline * 100


def _readiness(series: dict[str, dict[str, float]], sleep_map: dict[str, float]) -> dict | None:
    """Combine resting-HR, HRV, sleep (+ SpO2/respiratory-rate anomaly checks)
    into a recovery verdict, comparing today/yesterday against a trailing
    30-day baseline (excluding the last 2 days so an off night doesn't skew
    its own baseline).

    Degrades gracefully: HRV/SpO2/respiratory-rate rows may not exist yet
    (unverified data-type strings, see sync.py LIST_TYPES) — the score is
    computed from whichever signals are actually present.
    """
    today = datetime.now(TZ).date()
    yesterday = today - timedelta(days=1)
    t_iso, y_iso = today.isoformat(), yesterday.isoformat()

    def latest_and_baseline(day_map: dict[str, float]) -> tuple[float | None, float | None]:
        latest = day_map.get(t_iso)
        if latest is None:
            latest = day_map.get(y_iso)
        return latest, _window_avg(day_map, 30, 2)

    signals: dict = {}
    contributions: list[float] = []
    anomalies: list[str] = []

    rhr_latest, rhr_base = latest_and_baseline(series.get("daily-resting-heart-rate", {}))
    if rhr_latest is not None and rhr_base:
        dev = _pct_dev(rhr_latest, rhr_base)
        signals["resting_hr"] = {"latest": rhr_latest, "baseline": round(rhr_base, 1), "pct_vs_baseline": round(dev, 1)}
        contributions.append(-dev)  # lower resting HR than baseline = better recovery

    hrv_latest, hrv_base = latest_and_baseline(series.get("daily-heart-rate-variability", {}))
    if hrv_latest is not None and hrv_base:
        dev = _pct_dev(hrv_latest, hrv_base)
        signals["hrv"] = {"latest": hrv_latest, "baseline": round(hrv_base, 1), "pct_vs_baseline": round(dev, 1)}
        contributions.append(dev * 1.5)  # HRV is the strongest recovery signal, weighted higher

    sleep_latest, sleep_base = latest_and_baseline(sleep_map)
    if sleep_latest is not None and sleep_base:
        dev = _pct_dev(sleep_latest, sleep_base)
        signals["sleep_hours"] = {"latest": sleep_latest, "baseline": round(sleep_base, 1), "pct_vs_baseline": round(dev, 1)}
        contributions.append(dev * 1.2)

    spo2_latest, spo2_base = latest_and_baseline(series.get("daily-oxygen-saturation", {}))
    if spo2_latest is not None and spo2_base:
        signals["spo2"] = {"latest": spo2_latest, "baseline": round(spo2_base, 1)}
        if spo2_latest - spo2_base <= -2:
            anomalies.append("spo2_drop")

    resp_latest, resp_base = latest_and_baseline(series.get("daily-respiratory-rate", {}))
    if resp_latest is not None and resp_base:
        dev = _pct_dev(resp_latest, resp_base)
        signals["respiratory_rate"] = {"latest": resp_latest, "baseline": round(resp_base, 1), "pct_vs_baseline": round(dev, 1)}
        if dev is not None and dev >= 12:
            anomalies.append("resp_rate_elevated")

    if not contributions and not anomalies:
        return None

    score = round(max(-100.0, min(100.0, sum(contributions) / len(contributions)))) if contributions else None

    if anomalies:
        verdict = "possible fatigue/illness signal — consider an easier day"
    elif score is None:
        verdict = None
    elif score >= 15:
        verdict = "well recovered"
    elif score <= -15:
        verdict = "under-recovered — consider easing up today"
    else:
        verdict = "normal recovery"

    return {"score": score, "verdict": verdict, "signals": signals, "anomalies": anomalies}


def _exercise_series(user_id: str, days: int) -> tuple[dict[str, float], dict[str, int]]:
    """Return ({day: total_minutes}, {day: session_count}) from exercise_sessions,
    bucketed by the session's start date."""
    today = datetime.now(TZ).date()
    cutoff = (today - timedelta(days=days)).isoformat()
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT start, end FROM exercise_sessions WHERE user_id = ? AND start >= ? ORDER BY start",
            (user_id, cutoff),
        ).fetchall()

    minutes: dict[str, float] = {}
    counts: dict[str, int] = {}
    for row in rows:
        try:
            st = datetime.fromisoformat(row["start"].replace("Z", "+00:00"))
            en = datetime.fromisoformat(row["end"].replace("Z", "+00:00"))
        except ValueError:
            continue
        d = st.astimezone(TZ).date().isoformat()
        minutes[d] = minutes.get(d, 0.0) + (en - st).total_seconds() / 60
        counts[d] = counts.get(d, 0) + 1
    return minutes, counts


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
        "daily-heart-rate-variability": "hrv_ms",
        "daily-oxygen-saturation": "spo2_pct",
        "daily-respiratory-rate": "resp_rate_bpm",
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

    out["readiness"] = _readiness(series, sleep_map)

    # Exercise sessions (actual workouts vs plan adherence)
    exercise_min_map, exercise_count_map = _exercise_series(user_id, 35)
    this_week_min = _window_avg(exercise_min_map, 7, 0)
    last_week_min = _window_avg(exercise_min_map, 14, 7)
    out["exercise_minutes"] = {
        "week_avg": this_week_min,
        "month_avg": _window_avg(exercise_min_map, 30, 0),
        "trend": _trend(this_week_min, last_week_min),
        "sessions_this_week": sum(
            c for d, c in exercise_count_map.items()
            if 0 <= (today - datetime.fromisoformat(d).date()).days < 7
        ),
    }

    return out


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    db.init_db()

    DEFAULT_USER_ID = "U1068a1b9c15b44e7ff1439bdefdeb5dc"
    print(json.dumps(build_trends(DEFAULT_USER_ID), indent=2))
