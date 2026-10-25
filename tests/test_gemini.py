"""Gemini fallback: message conversion and failing honestly. No network calls."""
import io
import json
import unittest
import urllib.error
from unittest import mock

from aj.core import gemini


class GeminiTests(unittest.TestCase):
    def test_messages_become_gemini_contents(self):
        system, contents = gemini._to_gemini([
            {"role": "system", "content": "be AJ"},
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "What."},
            {"role": "user", "content": "explain gravity"},
        ])
        self.assertEqual(system, "be AJ")
        self.assertEqual([c["role"] for c in contents], ["user", "model", "user"])
        self.assertEqual(contents[-1]["parts"][0]["text"], "explain gravity")

    def test_no_key_raises_unavailable(self):
        with mock.patch.object(gemini, "read_secret", return_value=None):
            with self.assertRaises(gemini.GeminiUnavailable):
                list(gemini.stream_reply([{"role": "user", "content": "hi"}]))

    def test_thinking_off_where_a_model_has_the_switch(self):
        # Live 2026-10-22: a zero budget works on 3.x Flash; the transcription model refuses any setting.
        self.assertEqual(gemini.thinking_off(gemini.MODEL), {"thinkingConfig": {"thinkingBudget": 0}})
        self.assertEqual(gemini.thinking_off("gemini-3.5-transcribe"), {})
        self.assertEqual(gemini.thinking_off("gemini-pro-latest"), {})

    def test_generate_sends_one_plain_request(self):
        sent = []

        def urlopen(req, timeout=None):
            sent.append(req)
            return io.BytesIO(json.dumps({"candidates": [{"content": {"parts": [{"text": " Polite. "}]},
                                                          "finishReason": "STOP"}]}).encode())

        with mock.patch.object(gemini, "read_secret", return_value="KEY"), \
                mock.patch.object(gemini.urllib.request, "urlopen", side_effect=urlopen):
            self.assertEqual(gemini.generate("rude", system="be polite"), "Polite.")
        body = json.loads(sent[0].data)
        self.assertTrue(sent[0].full_url.endswith(f"/models/{gemini.MODEL}:generateContent"))
        self.assertNotIn("KEY", sent[0].full_url)
        self.assertEqual(body["systemInstruction"]["parts"][0]["text"], "be polite")

    def test_a_429_says_which_quota_ran_out(self):
        body = json.dumps({"error": {"code": 429, "message": "You exceeded your current quota.", "details": [
            {"violations": [{"quotaId": "GenerateRequestsPerMinutePerProjectPerModel-FreeTier"}]}]}}).encode()
        error = urllib.error.HTTPError("https://x", 429, "Too Many Requests", {}, io.BytesIO(body))
        with mock.patch.object(gemini, "read_secret", return_value="KEY"), \
                mock.patch.object(gemini.urllib.request, "urlopen", side_effect=error):
            with self.assertRaises(gemini.GeminiUnavailable) as caught:
                gemini.generate("hi")
        self.assertTrue(caught.exception.busy)
        self.assertIn("PerMinute", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
