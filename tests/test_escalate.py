"""Thinker-first escalation with a fake thinker and a fake Gemini. No models, no network."""
import time
import unittest
from unittest import mock

from ultron.core import escalate, gemini


class FakeThinker:
    available = True

    def __init__(self, answer="It's 6:15 pm.", delay=0.0, fail=False):
        self.answer, self.delay, self.fail = answer, delay, fail

    def solve(self, question, context=None):
        time.sleep(self.delay)
        if self.fail:
            raise RuntimeError("boom")
        return self.answer


def fake_gemini(text="Gemini says hi."):
    return mock.patch.object(gemini, "stream_reply", side_effect=lambda *a, **k: iter([text]))


class EscalateTests(unittest.TestCase):
    def setUp(self):
        self._budget = escalate.THINKER_BUDGET_SECS
        escalate.THINKER_BUDGET_SECS = 0.2

    def tearDown(self):
        escalate.THINKER_BUDGET_SECS = self._budget

    def test_thinker_answers_in_time(self):
        e = escalate.Escalator(FakeThinker(), gemini_ready=lambda: True)
        self.assertEqual(e.answer("when does the train arrive").source, "thinker")

    def test_slow_thinker_hands_over_to_gemini(self):
        slow = []
        with fake_gemini():
            e = escalate.Escalator(FakeThinker(delay=1.0), gemini_ready=lambda: True)
            got = e.answer("solve this puzzle", on_slow=lambda: slow.append(True))
        self.assertEqual((got.source, got.text, slow), ("gemini", "Gemini says hi.", [True]))

    def test_no_gemini_waits_for_thinker(self):
        e = escalate.Escalator(FakeThinker(delay=0.4), gemini_ready=lambda: False)
        self.assertEqual(e.answer("solve this puzzle").source, "thinker")

    def test_failed_thinker_escalates(self):
        with fake_gemini():
            got = escalate.Escalator(FakeThinker(fail=True), gemini_ready=lambda: True).answer("puzzle")
        self.assertEqual(got.source, "gemini")

    def test_live_questions_go_to_gemini_with_search(self):
        with fake_gemini() as stream:
            got = escalate.Escalator(FakeThinker(), gemini_ready=lambda: True).answer("who won the match today")
        self.assertEqual(got.source, "gemini")
        self.assertTrue(stream.call_args.kwargs.get("web"))

    def test_nothing_available_is_honest(self):
        got = escalate.Escalator(FakeThinker(fail=True), gemini_ready=lambda: False).answer("puzzle")
        self.assertEqual(got.source, "none")
        self.assertIn("Do not guess", escalate.relay_prompt("puzzle", got))

    def test_router(self):
        for hard in ["calculate 18% of 87.60", "how long until my battery hits 10 percent",
                     "plan my study schedule", "what's 17 * 23", "latest news on the election"]:
            self.assertTrue(escalate.looks_hard(hard), hard)
        for easy in ["open spotify", "tell me a joke", "how are you", "what's the capital of Japan"]:
            self.assertFalse(escalate.looks_hard(easy), easy)


if __name__ == "__main__":
    unittest.main()
