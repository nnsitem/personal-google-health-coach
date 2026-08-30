"""Gemini client bounds.

On 2026-08-22 a single generateContent call hung for 30 minutes 32 seconds
before answering 502. The user's "เพิ่มน้ำ 250ml" was logged and answered half
an hour later, by which time the LINE reply token had expired — which is what
"no reply" and "much slower than before" actually were.
"""

import logging
import time
import unittest

from coach import gemini
from coach.config import (GEMINI_ACCURACY_MODEL, GEMINI_MODEL,
                          GEMINI_REQUEST_TIMEOUT_SECONDS, GEMINI_THINKING_LEVEL)


class HttpOptions(unittest.TestCase):
    def test_request_timeout_is_set(self):
        # The SDK applies no timeout of its own, so without this a stalled
        # connection blocks the reply indefinitely.
        opts = gemini._http_options()
        self.assertIsNotNone(opts)
        self.assertEqual(opts.timeout, GEMINI_REQUEST_TIMEOUT_SECONDS * 1000)

    def test_sdk_internal_retries_are_disabled(self):
        # The SDK retries 5x by default against the SAME model; generate()'s own
        # rotation is better informed (different tier, quota parking, server
        # retryDelay), so letting the SDK spend the budget only delays failover.
        self.assertEqual(gemini._http_options().retry_options.attempts, 1)


class TransientClassification(unittest.TestCase):
    """A stall must be treated like any other transient fault — cooled down —
    rather than retried straight into another stall."""

    def test_timeout_and_gateway_errors_are_transient(self):
        for message in ("504 Deadline Exceeded", "DEADLINE_EXCEEDED",
                        "httpx.ReadTimeout", "request timed out",
                        "502 Bad Gateway", "503 UNAVAILABLE"):
            self.assertTrue([m for m in gemini._TRANSIENT_MARKERS if m in message], message)

    def test_permanent_errors_are_not_transient(self):
        for message in ("404 NOT_FOUND", "PERMISSION_DENIED"):
            self.assertFalse([m for m in gemini._TRANSIENT_MARKERS if m in message], message)
            self.assertTrue([m for m in gemini._SKIP_MARKERS if m in message], message)


class HangingModel(unittest.TestCase):
    def test_gives_up_instead_of_waiting_indefinitely(self):
        class Hanging:
            class models:
                @staticmethod
                def generate_content(**kwargs):
                    time.sleep(1)
                    raise RuntimeError("504 Deadline Exceeded")

        real = gemini.genai.Client
        gemini.genai.Client = lambda **kwargs: Hanging()
        self.addCleanup(setattr, gemini.genai, "Client", real)

        started = time.time()
        with self.assertRaises(gemini.GeminiUnavailable):
            gemini.generate("fake-key", contents="hi", max_wait=4)
        self.assertLess(time.time() - started, 20)


class AccuracyFirst(unittest.TestCase):
    """The user's stated preference: a slower answer beats a wrong number."""

    def setUp(self):
        gemini._cooldown_until.clear()
        self.addCleanup(gemini._cooldown_until.clear)
        self.tried = []

        class Spy:
            class models:
                @staticmethod
                def generate_content(model, **kwargs):
                    self.tried.append(model)
                    raise RuntimeError("404 NOT_FOUND")

        real = gemini.genai.Client
        gemini.genai.Client = lambda **kwargs: Spy()
        self.addCleanup(setattr, gemini.genai, "Client", real)

    def _order(self, **kwargs):
        try:
            gemini.generate("k", contents="x", max_wait=1, **kwargs)
        except gemini.GeminiUnavailable:
            pass
        return list(self.tried)

    def test_configured_primary_is_tried_first(self):
        self.assertEqual(self._order()[0], GEMINI_MODEL)

    def test_weakest_tier_is_the_last_resort(self):
        # The chain used to reach flash-lite BEFORE pro, so the first substitute
        # after an outage was the weakest model in the account.
        order = self._order()
        self.assertIn("lite", order[-1])
        self.assertFalse([m for m in order[:-1] if "lite" in m], order)

    def test_the_unstable_alias_is_not_in_the_chain(self):
        # gemini-flash-latest was the only model to fail the 2026-08-23
        # stability probe (3/5), and every incident that day involved it.
        self.assertNotIn("gemini-flash-latest", self._order())

    def test_prefer_accuracy_leads_with_the_strong_model(self):
        order = self._order(prefer_accuracy=True)
        self.assertEqual(order[0], GEMINI_ACCURACY_MODEL)
        # ...but still falls through, so an accuracy call is never left unanswered.
        self.assertIn(GEMINI_MODEL, order)


class ThinkingLadder(unittest.TestCase):
    def test_minimal_is_gone(self):
        # The API answers 400 INVALID_ARGUMENT for MINIMAL as of 2026-08-23, so
        # keeping it first burned one wasted call per model per process.
        self.assertNotIn("MINIMAL", gemini._THINKING_LADDER)

    def test_starts_at_the_configured_level_then_degrades(self):
        self.assertEqual(gemini._THINKING_LADDER[0], GEMINI_THINKING_LEVEL)
        self.assertIsNone(gemini._THINKING_LADDER[-1])


class FakeUsage:
    def __init__(self, cached=None, prompt=100):
        self.cached_content_token_count = cached
        self.prompt_token_count = prompt


class FakeResponse:
    """Minimal stand-in for a genai GenerateContentResponse."""
    def __init__(self, text="ok", usage_metadata=None, candidates=None):
        self.text = text
        self.usage_metadata = usage_metadata
        self.candidates = candidates if candidates is not None else []


class CacheUsageLogging(unittest.TestCase):
    """coach.gemini._log_cache_usage — added 2026-08-28 to tell whether Gemini's
    implicit prompt caching is actually firing, before spending effort on
    prompt restructuring or explicit (paid) caching. Must be a pure diagnostic:
    it can never affect the returned text and can never raise, since it runs
    on every successful call in production."""

    def setUp(self):
        # tests/__init__.py disables logging below CRITICAL suite-wide (on
        # purpose, so a passing run stays quiet) — assertLogs needs it back on
        # to see anything, so lift it for just this class and restore after.
        logging.disable(logging.NOTSET)
        self.addCleanup(logging.disable, logging.CRITICAL)

    def test_logs_hit_when_tokens_were_cached(self):
        response = FakeResponse(usage_metadata=FakeUsage(cached=80, prompt=100))
        with self.assertLogs(gemini.log, level="DEBUG") as cm:
            gemini._log_cache_usage("gemini-pro-latest", response)
        self.assertTrue(any("cache HIT" in line for line in cm.output))
        self.assertTrue(any("80" in line and "100" in line for line in cm.output))

    def test_logs_miss_when_nothing_was_cached(self):
        response = FakeResponse(usage_metadata=FakeUsage(cached=None, prompt=50))
        with self.assertLogs(gemini.log, level="DEBUG") as cm:
            gemini._log_cache_usage("gemini-pro-latest", response)
        self.assertTrue(any("cache MISS" in line for line in cm.output))

    def test_logs_miss_when_cached_count_is_zero(self):
        # 0 is falsy like None — both mean "no cache credit", not an error.
        response = FakeResponse(usage_metadata=FakeUsage(cached=0, prompt=50))
        with self.assertLogs(gemini.log, level="DEBUG") as cm:
            gemini._log_cache_usage("gemini-pro-latest", response)
        self.assertTrue(any("cache MISS" in line for line in cm.output))

    def test_no_crash_when_usage_metadata_is_missing(self):
        # Response objects from an older/mocked SDK build may not carry it at all.
        response = FakeResponse(usage_metadata=None)
        gemini._log_cache_usage("gemini-pro-latest", response)  # must not raise

    def test_no_crash_on_a_malformed_response_object(self):
        # Diagnostics must never break generation — even a response shape the
        # SDK never actually produces must be swallowed, not propagated.
        class Explodes:
            @property
            def usage_metadata(self):
                raise RuntimeError("boom")

        gemini._log_cache_usage("gemini-pro-latest", Explodes())  # must not raise


class CacheLoggingDoesNotAffectGeneration(unittest.TestCase):
    """End-to-end: adding the cache-usage log line must not change generate()'s
    behavior or output, whether or not the fake SDK response carries usage
    metadata at all."""

    def setUp(self):
        gemini._cooldown_until.clear()
        self.addCleanup(gemini._cooldown_until.clear)

    def _patch_client(self, response):
        class Fake:
            class models:
                @staticmethod
                def generate_content(**kwargs):
                    return response

        real = gemini.genai.Client
        gemini.genai.Client = lambda **kwargs: Fake()
        self.addCleanup(setattr, gemini.genai, "Client", real)

    def test_reply_unaffected_when_cache_hit(self):
        self._patch_client(FakeResponse(
            text="hello from the coach",
            usage_metadata=FakeUsage(cached=80, prompt=100),
        ))
        self.assertEqual(gemini.generate("k", contents="hi", max_wait=1),
                          "hello from the coach")

    def test_reply_unaffected_when_no_usage_metadata_at_all(self):
        self._patch_client(FakeResponse(text="hello", usage_metadata=None))
        self.assertEqual(gemini.generate("k", contents="hi", max_wait=1), "hello")


if __name__ == "__main__":
    unittest.main()
