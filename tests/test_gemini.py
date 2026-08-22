"""Gemini client bounds.

On 2026-08-22 a single generateContent call hung for 30 minutes 32 seconds
before answering 502. The user's "เพิ่มน้ำ 250ml" was logged and answered half
an hour later, by which time the LINE reply token had expired — which is what
"no reply" and "much slower than before" actually were.
"""

import time
import unittest

from coach import gemini
from coach.config import GEMINI_REQUEST_TIMEOUT_SECONDS


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


if __name__ == "__main__":
    unittest.main()
