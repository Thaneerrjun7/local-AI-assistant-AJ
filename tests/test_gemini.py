"""Gemini fallback: message conversion and failing honestly. No network calls."""
import unittest
from unittest import mock

from ultron.core import gemini


class GeminiTests(unittest.TestCase):
    def test_messages_become_gemini_contents(self):
        system, contents = gemini._to_gemini([
            {"role": "system", "content": "be Ultron"},
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "What."},
            {"role": "user", "content": "explain gravity"},
        ])
        self.assertEqual(system, "be Ultron")
        self.assertEqual([c["role"] for c in contents], ["user", "model", "user"])
        self.assertEqual(contents[-1]["parts"][0]["text"], "explain gravity")

    def test_no_key_raises_unavailable(self):
        with mock.patch.object(gemini, "read_secret", return_value=None):
            with self.assertRaises(gemini.GeminiUnavailable):
                list(gemini.stream_reply([{"role": "user", "content": "hi"}]))


if __name__ == "__main__":
    unittest.main()
