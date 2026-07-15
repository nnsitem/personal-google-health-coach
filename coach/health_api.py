"""Thin client for the Google Health API (v4).

The API launched in 2026. Endpoint shapes below follow the published docs at
https://developers.google.com/health/reference/rest/v4/

Data type names are kebab-case in URL paths (e.g., active-zone-minutes).
`dailyRollUp` and `reconcile` are POST methods with a JSON request body.
`list` is a GET with query parameters including a filter string.
"""

import time
from datetime import date

import requests

from coach.auth import get_credentials
from coach.config import GOOGLE_HEALTH_BASE


class HealthAPIError(RuntimeError):
    def __init__(self, status: int, body: str, url: str):
        super().__init__(f"Google Health API {status} for {url}: {body[:500]}")
        self.status = status
        self.body = body


def _civil_date(d: date | str) -> dict:
    """Convert a date or YYYY-MM-DD string to a CivilDateTime object (date only).

    The REST API expects: {"date": {"year": ..., "month": ..., "day": ...}}
    """
    if isinstance(d, str):
        parts = d.split("-")
        return {"date": {"year": int(parts[0]), "month": int(parts[1]), "day": int(parts[2])}}
    return {"date": {"year": d.year, "month": d.month, "day": d.day}}


class HealthClient:
    def __init__(self):
        self._creds = get_credentials()
        self._session = requests.Session()

    def _request(self, method: str, path: str, params: dict | None = None, json_body: dict | None = None) -> dict:
        url = f"{GOOGLE_HEALTH_BASE}/{path.lstrip('/')}"
        for attempt in range(4):
            resp = self._session.request(
                method,
                url,
                params=params or {},
                json=json_body,
                headers={"Authorization": f"Bearer {self._creds.token}"},
                timeout=30,
            )
            if resp.status_code in (429, 500, 502, 503):
                time.sleep(2 ** attempt)
                continue
            if resp.status_code != 200:
                raise HealthAPIError(resp.status_code, resp.text, url)
            return resp.json()
        raise HealthAPIError(resp.status_code, resp.text, url)

    def _get(self, path: str, params: dict | None = None) -> dict:
        return self._request("GET", path, params=params)

    def _post(self, path: str, json_body: dict) -> dict:
        return self._request("POST", path, json_body=json_body)

    # ---- paginated helpers ------------------------------------------------

    def _paginate_get(self, path: str, params: dict, items_key: str) -> list[dict]:
        items: list[dict] = []
        page_token = None
        while True:
            page_params = dict(params)
            if page_token:
                page_params["pageToken"] = page_token
            body = self._get(path, page_params)
            items.extend(body.get(items_key, []))
            page_token = body.get("nextPageToken")
            if not page_token:
                return items

    def _paginate_post(self, path: str, json_body: dict, items_key: str) -> list[dict]:
        items: list[dict] = []
        page_token = None
        while True:
            body_with_token = dict(json_body)
            if page_token:
                body_with_token["pageToken"] = page_token
            resp = self._post(path, body_with_token)
            items.extend(resp.get(items_key, []))
            page_token = resp.get("nextPageToken")
            if not page_token:
                return items

    # ---- reads -----------------------------------------------------------

    def daily_rollup(self, data_type: str, start_date: str, end_date: str) -> list[dict]:
        """Civil-day aggregates for a data type.

        data_type: kebab-case name (e.g., 'steps', 'active-zone-minutes')
        start_date, end_date: YYYY-MM-DD strings. Range is [start, end).
        """
        return self._paginate_post(
            f"users/me/dataTypes/{data_type}/dataPoints:dailyRollUp",
            {
                "range": {
                    "start": _civil_date(start_date),
                    "end": _civil_date(end_date),
                },
            },
            "rollupDataPoints",
        )

    def list_points(self, data_type: str, filter_str: str) -> list[dict]:
        """Raw data points with a filter string.

        data_type: kebab-case name (e.g., 'sleep', 'steps')
        filter_str: AIP-160 filter (see API docs for format per data type)
        """
        return self._paginate_get(
            f"users/me/dataTypes/{data_type}/dataPoints",
            {"filter": filter_str, "pageSize": 1000},
            "dataPoints",
        )

    # ---- writes ----------------------------------------------------------

    def create_data_point(self, data_type: str, data_point: dict) -> dict:
        """Create a single data point (write). Requires a *.writeonly scope.

        data_type: kebab-case name (e.g., 'nutrition-log')
        data_point: a DataPoint dict with the typed payload
        """
        return self._post(
            f"users/me/dataTypes/{data_type}/dataPoints",
            data_point,
        )

    def batch_delete_data_points(self, data_type: str, names: list[str]) -> dict:
        """Delete data points by their resource names. Requires a *.writeonly scope.

        names: full resource names, e.g.
          'users/me/dataTypes/nutrition-log/dataPoints/{id}'
        """
        return self._post(
            f"users/me/dataTypes/{data_type}/dataPoints:batchDelete",
            {"names": names},
        )

    def reconcile(self, data_type: str, start_iso: str, end_iso: str) -> list[dict]:
        """Merged-across-devices stream (matches what the Google Health app shows).

        Uses POST with a time range in the request body.
        """
        return self._paginate_post(
            f"users/me/dataTypes/{data_type}/dataPoints:reconcile",
            {
                "interval": {
                    "startTime": start_iso,
                    "endTime": end_iso,
                },
                "pageSize": 10000,
            },
            "dataPoints",
        )

    # ---- discovery (smoke test) ------------------------------------------

    def test_connection(self, data_type: str = "steps") -> dict:
        """Quick connectivity check: fetch today's daily rollup for a common type."""
        from datetime import date as _date, timedelta
        today = _date.today()
        yesterday = today - timedelta(days=1)
        return self._post(
            f"users/me/dataTypes/{data_type}/dataPoints:dailyRollUp",
            {
                "range": {
                    "start": _civil_date(yesterday),
                    "end": _civil_date(today),
                },
            },
        )
