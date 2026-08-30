"""coach.chat.transcribe_voice_message — DESIGN-V3.md #2 (voice messages).

Stays offline: the Gemini call is stubbed. Part.from_bytes/mime_type plumbing
uses the real google-genai SDK (no network call happens building a Part).
"""

import unittest
from unittest import mock

from coach import chat


class VoiceTranscription(unittest.TestCase):
    def test_returns_the_transcript_on_success(self):
        with mock.patch("coach.chat.gemini.generate", return_value="log a banana"):
            result = chat.transcribe_voice_message("fake-key", b"fake-audio")
        self.assertEqual(result, "log a banana")

    def test_strips_surrounding_whitespace(self):
        with mock.patch("coach.chat.gemini.generate", return_value="  hello there  \n"):
            result = chat.transcribe_voice_message("fake-key", b"fake-audio")
        self.assertEqual(result, "hello there")

    def test_no_speech_sentinel_becomes_none(self):
        # A silent/unintelligible clip must read as "nothing to transcribe",
        # not as if the model literally said the word "NO_SPEECH".
        with mock.patch("coach.chat.gemini.generate", return_value="[NO_SPEECH]"):
            result = chat.transcribe_voice_message("fake-key", b"fake-audio")
        self.assertIsNone(result)

    def test_blank_reply_becomes_none(self):
        with mock.patch("coach.chat.gemini.generate", return_value="   "):
            result = chat.transcribe_voice_message("fake-key", b"fake-audio")
        self.assertIsNone(result)

    def test_generic_failure_returns_none_rather_than_raising(self):
        with mock.patch("coach.chat.gemini.generate", side_effect=RuntimeError("boom")):
            result = chat.transcribe_voice_message("fake-key", b"fake-audio")
        self.assertIsNone(result)

    def test_capacity_outage_propagates_instead_of_being_swallowed(self):
        # Distinct from a generic failure: coach.main tells the user "quota
        # exhausted" / "try again" for this, instead of a misleading "I
        # couldn't make out any speech".
        with mock.patch("coach.chat.gemini.generate",
                        side_effect=chat.gemini.GeminiUnavailable("down")):
            with self.assertRaises(chat.gemini.GeminiUnavailable):
                chat.transcribe_voice_message("fake-key", b"fake-audio")

    def test_default_mime_type_is_the_one_line_actually_sends(self):
        # LINE only ever sends user voice messages as M4A. Some tooling
        # misidentifies that container as video/mp4 — pin the audio mime type
        # explicitly rather than relying on sniffing.
        captured = {}

        def fake_generate(api_key, contents, **kwargs):
            captured["mime_type"] = contents[1].inline_data.mime_type
            return "ok"

        with mock.patch("coach.chat.gemini.generate", side_effect=fake_generate):
            chat.transcribe_voice_message("fake-key", b"fake-audio")
        self.assertEqual(captured["mime_type"], "audio/mp4")


if __name__ == "__main__":
    unittest.main()
