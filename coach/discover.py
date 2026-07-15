"""Smoke-test the Google Health API connection.

Run:  python -m coach.discover
Tries to fetch yesterday's step count to verify auth + API access.
Then attempts a few common data types to see what your account exposes.
"""

import json

from coach.health_api import HealthClient, HealthAPIError


# Known data types from the Google Health API docs (kebab-case).
# Not all accounts have all of these — depends on your devices.
KNOWN_DATA_TYPES = [
    "steps",
    "distance",
    "active-zone-minutes",
    "total-calories",
    "heart-rate",
    "resting-heart-rate-personal-range",
    "heart-rate-variability-personal-range",
    "sleep",
    "exercise",
    "weight",
    "body-fat",
    "floors",
    "sedentary-period",
    "active-minutes",
    "active-energy-burned",
]


def main() -> None:
    # Owner-run CLI smoke test — the file-based v1 token is intentional here.
    client = HealthClient(allow_default_credentials=True)

    print("Testing connection with 'steps' dailyRollUp (yesterday)...\n")
    try:
        result = client.test_connection("steps")
        print(json.dumps(result, indent=2))
        print("\n✅ Connection works!\n")
    except HealthAPIError as e:
        print(f"❌ Failed: {e}\n")
        return

    print("Probing known data types (dailyRollUp for yesterday)...\n")
    from datetime import date, timedelta
    today = date.today()
    yesterday = today - timedelta(days=1)

    available = []
    for dt in KNOWN_DATA_TYPES:
        try:
            points = client.daily_rollup(dt, yesterday.isoformat(), today.isoformat())
            status = f"✅ {len(points)} points"
            available.append(dt)
        except HealthAPIError as e:
            if e.status == 400:
                status = "⚠️  not supported for dailyRollUp (may work with list)"
            elif e.status == 404:
                status = "❌ not available"
            else:
                status = f"❌ error {e.status}"
        print(f"  {dt:45s} {status}")

    print(f"\n{len(available)} data types returned data via dailyRollUp.")
    if available:
        print(f"Available: {', '.join(available)}")


if __name__ == "__main__":
    main()
