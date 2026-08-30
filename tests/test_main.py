"""Voice message routing (coach.main._process_audio_message), added 2026-08-28
alongside DESIGN-V3.md #2. Deliberately does NOT re-test the chat pipeline
itself (that's tests/test_chat_directives.py's job) — only that a voice
message is gated, downloaded, transcribed, and handed off exactly like a
typed message, with the reply token consumed by the echo rather than reused.

Stays offline like every other test: LINE and Gemini calls are stubbed.
"""

import unittest
from unittest import mock

from coach import gemini, main
from tests import support


class VoiceMessageRouting(unittest.TestCase):
    def setUp(self):
        self.user_id = support.new_user(gemini_api_key="fake-key", google_token_json="{}")
        self.sent = []
        patcher = mock.patch.object(main, "_send", side_effect=self._record_send)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _record_send(self, user_id, text, reply_token=None):
        self.sent.append(text)
        return ["fake-line-message-id"]

    def test_unconfigured_user_is_asked_to_set_up_first(self):
        # No gemini_api_key/google_token_json — must not touch LINE or Gemini.
        bare_user = support.new_user()
        with mock.patch("coach.line.get_message_content") as download:
            main._process_audio_message(bare_user, "msg1", None)
        download.assert_not_called()
        self.assertTrue(self.sent)

    def test_download_failure_tells_the_user_not_silence(self):
        with mock.patch("coach.line.get_message_content", side_effect=RuntimeError("boom")):
            main._process_audio_message(self.user_id, "msg1", None)
        self.assertTrue(any("download" in t.lower() for t in self.sent))

    def test_no_speech_tells_the_user_instead_of_guessing(self):
        with mock.patch("coach.line.get_message_content", return_value=b"fake-audio"), \
             mock.patch("coach.chat.transcribe_voice_message", return_value=None):
            main._process_audio_message(self.user_id, "msg1", None)
        self.assertTrue(any("couldn't make out" in t for t in self.sent))

    def test_quota_exhausted_gives_the_specific_message(self):
        with mock.patch("coach.line.get_message_content", return_value=b"fake-audio"), \
             mock.patch("coach.chat.transcribe_voice_message",
                        side_effect=gemini.GeminiQuotaExhausted("x")):
            main._process_audio_message(self.user_id, "msg1", None)
        self.assertTrue(any("quota" in t.lower() for t in self.sent))

    def test_generic_unavailable_gives_a_try_again_message(self):
        with mock.patch("coach.line.get_message_content", return_value=b"fake-audio"), \
             mock.patch("coach.chat.transcribe_voice_message",
                        side_effect=gemini.GeminiUnavailable("x")):
            main._process_audio_message(self.user_id, "msg1", None)
        self.assertTrue(any("trouble connecting" in t.lower() for t in self.sent))

    def test_successful_transcript_is_echoed_then_routed_like_typed_text(self):
        with mock.patch("coach.line.get_message_content", return_value=b"fake-audio"), \
             mock.patch("coach.chat.transcribe_voice_message", return_value="log a banana"), \
             mock.patch.object(main, "_process_text_message") as spy:
            main._process_audio_message(self.user_id, "msg1", "the-reply-token")

        self.assertTrue(any("log a banana" in t for t in self.sent))
        spy.assert_called_once()
        args, kwargs = spy.call_args
        self.assertEqual(args[0], self.user_id)
        self.assertEqual(args[1], "log a banana")
        # The reply token is single-use and was already spent on the echo —
        # reusing it for the real answer would silently fail LINE's API.
        self.assertIsNone(kwargs.get("reply_token"))
        self.assertEqual(kwargs.get("inbound_message_id"), "msg1")


if __name__ == "__main__":
    unittest.main()
