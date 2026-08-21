"""HealthClient: retry policy, delete limits, pagination bounds, page sizes."""

import types
import unittest

from coach.health_api import (DELETE_CHUNK_SIZE, LIST_PAGE_SIZE,
                              LIST_PAGE_SIZE_BY_TYPE, MAX_PAGES, HealthAPIError,
                              HealthClient)


class _Resp:
    def __init__(self, code):
        self.status_code = code
        self.text = "error body"

    def json(self):
        return {"response": {"name": "users/x/dataTypes/nutrition-log/dataPoints/1"}}


class _Session:
    """Always answers with one status code, and counts the attempts."""

    def __init__(self, code):
        self.code = code
        self.calls = 0

    def request(self, *a, **k):
        self.calls += 1
        return _Resp(self.code)


def _client(code):
    c = HealthClient.__new__(HealthClient)
    c._creds = types.SimpleNamespace(token="tok")
    c._session = _Session(code)
    return c


class RetryPolicy(unittest.TestCase):
    """A write must not be resent on 5xx: the server may have applied it and
    only lost the response, so retrying duplicates the entry."""

    def test_create_not_retried_on_5xx(self):
        for code in (500, 502, 503):
            c = _client(code)
            with self.assertRaises(HealthAPIError):
                c.create_data_point("nutrition-log", {})
            self.assertEqual(c._session.calls, 1, f"{code} must not be retried")

    def test_create_retried_on_429(self):
        # 429 means the request was rejected outright — resending is safe.
        c = _client(429)
        with self.assertRaises(HealthAPIError):
            c.create_data_point("nutrition-log", {})
        self.assertEqual(c._session.calls, 4)

    def test_reads_still_retried_on_5xx(self):
        c = _client(503)
        with self.assertRaises(HealthAPIError):
            c.daily_rollup("steps", "2026-08-20", "2026-08-21")
        self.assertEqual(c._session.calls, 4)


class BatchDelete(unittest.TestCase):
    """dataPoints:batchDelete accepts at most 10000 names; an oversized call
    fails entirely, which is how 'delete all of today' broke."""

    def setUp(self):
        self.sent = []

        class C(HealthClient):
            def __init__(inner, fail_at=None):
                inner.fail_at = fail_at
                inner.calls = 0

            def _post(inner, path, json_body, retry_5xx=True):
                inner.calls += 1
                self.sent.append(len(json_body["names"]))
                if inner.fail_at and inner.calls == inner.fail_at:
                    raise HealthAPIError(400, "too many", path)
                return {}

        self.C = C

    def test_chunks_stay_under_the_documented_cap(self):
        res = self.C().batch_delete_data_points("nutrition-log", [f"n{i}" for i in range(19673)])
        self.assertLessEqual(max(self.sent), 10000)
        self.assertEqual(max(self.sent), DELETE_CHUNK_SIZE)
        self.assertEqual(res["deleted"], 19673)

    def test_empty_list_sends_nothing(self):
        res = self.C().batch_delete_data_points("nutrition-log", [])
        self.assertEqual(res, {"requested": 0, "deleted": 0})
        self.assertEqual(self.sent, [])

    def test_failed_chunk_raises(self):
        with self.assertRaises(HealthAPIError):
            self.C(fail_at=3).batch_delete_data_points("x", [f"n{i}" for i in range(2000)])


class ExistenceCheck(unittest.TestCase):
    """A 200 from batchDelete is not proof; callers verify by name."""

    def _client_with(self, statuses):
        c = HealthClient.__new__(HealthClient)
        calls = iter(statuses)

        def fake_get(path, params=None):
            status = next(calls)
            if status != 200:
                raise HealthAPIError(status, "", path)
            return {"name": path}

        c._get = fake_get
        return c

    def test_404_means_gone(self):
        c = self._client_with([404])
        self.assertEqual(c.data_points_still_exist(["users/x/dataTypes/t/dataPoints/1"]), [])

    def test_200_means_still_there(self):
        c = self._client_with([200])
        name = "users/x/dataTypes/t/dataPoints/1"
        self.assertEqual(c.data_points_still_exist([name]), [name])

    def test_unknown_error_counts_as_still_there(self):
        # Can't prove it's gone, so stay on the safe side.
        c = self._client_with([500])
        name = "users/x/dataTypes/t/dataPoints/1"
        self.assertEqual(c.data_points_still_exist([name]), [name])


class Pagination(unittest.TestCase):
    """`while True` on a server that never advances hangs the request handler."""

    def test_repeating_token_stops(self):
        c = HealthClient.__new__(HealthClient)
        calls = {"n": 0}

        def stuck(path, params=None):
            calls["n"] += 1
            return {"dataPoints": [{"name": calls["n"]}], "nextPageToken": "SAME"}

        c._get = stuck
        items = c._paginate_get("x", {}, "dataPoints")
        self.assertEqual(calls["n"], 2)
        self.assertEqual(len(items), 2)

    def test_endless_new_tokens_hit_the_page_cap(self):
        c = HealthClient.__new__(HealthClient)
        calls = {"n": 0}

        def endless(path, params=None):
            calls["n"] += 1
            return {"dataPoints": [{"name": calls["n"]}], "nextPageToken": f"tok{calls['n']}"}

        c._get = endless
        c._paginate_get("x", {}, "dataPoints")
        self.assertEqual(calls["n"], MAX_PAGES)


class PageSize(unittest.TestCase):
    """`list` allows 10000 per page except sleep and exercise, capped at 25."""

    def test_per_type_page_size(self):
        seen = {}
        c = HealthClient.__new__(HealthClient)
        c._paginate_get = lambda path, params, key: seen.setdefault(path, params["pageSize"]) or []
        for dtype in ("sleep", "exercise", "nutrition-log", "steps"):
            c.list_points(dtype, "f")
        self.assertEqual(seen["users/me/dataTypes/sleep/dataPoints"], 25)
        self.assertEqual(seen["users/me/dataTypes/exercise/dataPoints"], 25)
        self.assertEqual(seen["users/me/dataTypes/nutrition-log/dataPoints"], LIST_PAGE_SIZE)
        self.assertEqual(LIST_PAGE_SIZE_BY_TYPE, {"sleep": 25, "exercise": 25})


class DeadCode(unittest.TestCase):
    def test_reconcile_removed(self):
        self.assertFalse(hasattr(HealthClient, "reconcile"))


if __name__ == "__main__":
    unittest.main()
