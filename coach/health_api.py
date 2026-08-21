"""Thin client for the Google Health API (v4).

The API launched in 2026. Endpoint shapes below follow the published docs at
https://developers.google.com/health/reference/rest/v4/

Data type names are kebab-case in URL paths (e.g., active-zone-minutes).
`dailyRollUp` is a POST method with a JSON request body.
`list` is a GET with query parameters including a filter string.
"""

import logging
import time
from datetime import date

import requests

from coach.auth import get_credentials
from coach.config import GOOGLE_HEALTH_BASE

log = logging.getLogger(__name__)


# dataPoints:batchDelete accepts at most 10000 names per request (documented).
# Kept well under it so one rejected chunk costs little and the request stays small.
DELETE_CHUNK_SIZE = 500

# Hard stop for paginated reads. Any legitimate window fits well inside this;
# hitting it means the server is not converging, and an unbounded `while True`
# would otherwise spin forever inside a request handler.
MAX_PAGES = 60

# `list` allows pageSize up to 10000 (default 1440) — EXCEPT sleep and exercise,
# which are capped at 25. Asking for more is silently clamped today, but sending
# an out-of-spec value invites a 400 the day the API tightens.
LIST_PAGE_SIZE = 1000
LIST_PAGE_SIZE_BY_TYPE = {"sleep": 25, "exercise": 25}


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
    def __init__(self, token_json: str | None = None, allow_default_credentials: bool = False):
        """token_json: the user's own OAuth token (required for user-scoped calls).

        allow_default_credentials=True opts in to the v1 file-based token
        (data/google_token.json) — for owner-run CLI tools only. It must never
        be used on a per-user code path: falling back silently would read from
        or write to the OWNER's Google Health account on another user's behalf.
        """
        self.token_refreshed = False
        if token_json:
            import json as _json
            from google.oauth2.credentials import Credentials
            from coach.config import GOOGLE_HEALTH_SCOPES
            token_data = _json.loads(token_json)
            self._creds = Credentials.from_authorized_user_info(token_data, GOOGLE_HEALTH_SCOPES)
            if self._creds.expired and self._creds.refresh_token:
                from google.auth.transport.requests import Request
                self._creds.refresh(Request())
                self.token_refreshed = True
        elif allow_default_credentials:
            self._creds = get_credentials()
        else:
            raise HealthAPIError(401, "no Google token for this user (re-authorize with 'login')", "local")
        self._session = requests.Session()

    def token_json(self) -> str:
        """The current credentials as a JSON string (post-refresh if one happened)."""
        return self._creds.to_json()

    def _request(self, method: str, path: str, params: dict | None = None,
                 json_body: dict | None = None, retry_5xx: bool = True) -> dict:
        """retry_5xx=False for non-idempotent calls (writes): a 5xx may mean
        "applied, then failed to answer", and resending would duplicate it."""
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
            # 429 means the request was REJECTED, so resending it is always
            # safe. A 5xx is ambiguous — the server may have applied the change
            # before failing to answer — so it is only retried for calls whose
            # repetition can't create anything (see retry_5xx).
            retryable = resp.status_code == 429 or (retry_5xx and resp.status_code in (500, 502, 503))
            if retryable and attempt < 3:
                time.sleep(2 ** attempt)
                continue
            if resp.status_code != 200:
                raise HealthAPIError(resp.status_code, resp.text, url)
            return resp.json()
        raise HealthAPIError(resp.status_code, resp.text, url)

    def _get(self, path: str, params: dict | None = None) -> dict:
        return self._request("GET", path, params=params)

    def _post(self, path: str, json_body: dict, retry_5xx: bool = True) -> dict:
        return self._request("POST", path, json_body=json_body, retry_5xx=retry_5xx)

    # ---- paginated helpers ------------------------------------------------

    def _paginate_get(self, path: str, params: dict, items_key: str) -> list[dict]:
        items: list[dict] = []
        page_token = None
        for _ in range(MAX_PAGES):
            page_params = dict(params)
            if page_token:
                page_params["pageToken"] = page_token
            body = self._get(path, page_params)
            items.extend(body.get(items_key, []))
            next_token = body.get("nextPageToken")
            if not next_token or next_token == page_token:
                # A token identical to the one we just sent means the server is
                # not advancing; continuing would re-append the same page until
                # the process dies. Stop and report what we have.
                if next_token:
                    log.warning("pagination stalled on %s (repeating pageToken) — "
                                "returning %d items", path, len(items))
                return items
            page_token = next_token
        log.warning("pagination hit the %d-page cap on %s — returning %d items "
                    "(result is INCOMPLETE)", MAX_PAGES, path, len(items))
        return items

    def _paginate_post(self, path: str, json_body: dict, items_key: str) -> list[dict]:
        items: list[dict] = []
        page_token = None
        for _ in range(MAX_PAGES):
            body_with_token = dict(json_body)
            if page_token:
                body_with_token["pageToken"] = page_token
            resp = self._post(path, body_with_token)
            items.extend(resp.get(items_key, []))
            next_token = resp.get("nextPageToken")
            if not next_token or next_token == page_token:
                if next_token:
                    log.warning("pagination stalled on %s (repeating pageToken) — "
                                "returning %d items", path, len(items))
                return items
            page_token = next_token
        log.warning("pagination hit the %d-page cap on %s — returning %d items "
                    "(result is INCOMPLETE)", MAX_PAGES, path, len(items))
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
            {"filter": filter_str,
             "pageSize": LIST_PAGE_SIZE_BY_TYPE.get(data_type, LIST_PAGE_SIZE)},
            "dataPoints",
        )

    # ---- writes ----------------------------------------------------------

    def create_data_point(self, data_type: str, data_point: dict) -> dict:
        """Create a single data point (write). Requires a *.writeonly scope.

        data_type: kebab-case name (e.g., 'nutrition-log')
        data_point: a DataPoint dict with the typed payload

        dataPoints.create returns an Operation envelope, NOT the DataPoint
        itself — the created resource (with its identifiable "name", e.g.
        'users/me/dataTypes/nutrition-log/dataPoints/{id}') lives nested in
        operation['response'], wrapped with an '@type' key. Returning the raw
        Operation here previously made callers read the OPERATION's own
        "name" (an unrelated 'operations/...' resource) instead of the data
        point's — so a later delete/adjustment quietly matched nothing and
        the original entry was never actually removed.
        """
        # retry_5xx=False: this is a CREATE. A 500/502/503 can mean the point
        # was written and only the response was lost, so an automatic retry
        # silently duplicates the entry (and the daily totals it feeds).
        op = self._post(
            f"users/me/dataTypes/{data_type}/dataPoints",
            data_point,
            retry_5xx=False,
        )
        if isinstance(op, dict) and "response" in op:
            if op.get("done") is False:
                log.warning(
                    "dataPoints.create for %s returned a pending operation "
                    "(done=false) — its resource name may not be usable yet",
                    data_type,
                )
            return op.get("response") or {}
        # Defensive: tolerate a bare DataPoint if the API ever returns one directly
        return op if isinstance(op, dict) else {}

    def batch_delete_data_points(self, data_type: str, names: list[str]) -> dict:
        """Delete data points by their resource names. Requires a *.writeonly scope.

        names: full resource names, e.g.
          'users/me/dataTypes/nutrition-log/dataPoints/{id}'

        Sent in chunks: the API documents "a maximum of 10000 data points can be
        deleted in a single request", and one oversized call fails ENTIRELY —
        which is how "delete all of today's logs" broke once a mirror app had
        flooded the day with ~20k points (2026-08-21). Chunking well under the
        cap also keeps a single failure from losing the whole sweep.

        Returns {"requested": n, "deleted": n} — `deleted` counts the names in
        chunks the API accepted, so a partial failure still reports progress
        before raising.
        """
        if not names:
            return {"requested": 0, "deleted": 0}
        deleted = 0
        for i in range(0, len(names), DELETE_CHUNK_SIZE):
            chunk = names[i:i + DELETE_CHUNK_SIZE]
            try:
                self._post(
                    f"users/me/dataTypes/{data_type}/dataPoints:batchDelete",
                    {"names": chunk},
                )
            except HealthAPIError:
                log.error("batchDelete failed for %s after %d/%d points",
                          data_type, deleted, len(names))
                raise
            deleted += len(chunk)
        if len(names) > DELETE_CHUNK_SIZE:
            log.info("batchDelete removed %d %s points in %d chunks",
                     deleted, data_type, (len(names) + DELETE_CHUNK_SIZE - 1) // DELETE_CHUNK_SIZE)
        return {"requested": len(names), "deleted": deleted}

    def data_points_still_exist(self, names: list[str]) -> list[str]:
        """Which of `names` Google Health still holds. [] means all are gone.

        A 200 from batchDelete only means the request was accepted — points we
        are not allowed to remove (another app wrote them) stay put. Callers
        that are about to drop local history, or to re-log a rescaled entry,
        MUST confirm the originals are actually gone or the two stores diverge
        and totals double-count.
        """
        still: list[str] = []
        for name in names:
            path = name[name.index("users/"):] if "users/" in name else name
            try:
                self._get(path)
                still.append(name)
            except HealthAPIError as e:
                if e.status != 404:
                    # Can't prove it's gone — treat as still present so the
                    # caller stays on the safe side.
                    log.warning("could not verify deletion of %s (%s)", name, e.status)
                    still.append(name)
        return still

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


def client_for_user(user_id: str) -> HealthClient:
    """Build a HealthClient from the user's stored token.

    If constructing the client refreshed the access token, the refreshed token
    is persisted back to the users table — otherwise every subsequent call pays
    the refresh round-trip again, and a rotated refresh token would be lost.
    Raises HealthAPIError(401) when the user has no stored token.
    """
    from coach import db
    user = db.get_user(user_id)
    token_json = (user.get("google_token_json") if user else None) or None
    client = HealthClient(token_json=token_json)
    if client.token_refreshed:
        try:
            db.update_user(user_id, google_token_json=client.token_json())
            log.info("persisted refreshed Google token for user %s", user_id)
        except Exception:
            # Best-effort: the in-memory credentials still work for this call.
            log.exception("failed to persist refreshed token for user %s", user_id)
    return client
