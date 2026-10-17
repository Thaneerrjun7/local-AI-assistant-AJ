"""Brain routing: instant commands, hard questions, conversation with tool calls. All faked."""
import random
import time
import unittest
from unittest import mock

from ultron.config import Settings
from ultron.core import brain as brain_mod
from ultron.core.escalate import HardAnswer
from ultron.core.pipeline import TurnTimes
from ultron.lines import POOLS, SIGNATURES, Lines
from ultron.tools.dispatch import ToolResult


class FakeMouth:
    def __init__(self):
        self.said = []

    def say(self, text, times=None):
        self.said.append(text)


class FakeDispatcher:
    def __init__(self, result="Volume at 30%.", delay=0.0):
        self.calls, self.result, self.delay = [], result, delay

    def run(self, name, args):
        self.calls.append((name, args))
        time.sleep(self.delay)
        return self.result


class Lucky(random.Random):
    def random(self) -> float:
        return 0.0  # every fitting signature line is offered


class FakeEscalator:
    def __init__(self, can_help=True):
        self.asked, self._can_help = [], can_help

    def can_help(self):
        return self._can_help

    def answer(self, question, context=None, on_slow=None):
        self.asked.append(question)
        return HardAnswer("Each person pays $25.84.", "thinker")


def events(*items):
    return lambda *a, **k: iter(items)


def pieces(*items):
    return lambda *a, **k: iter(items)


class BrainTests(unittest.TestCase):
    def make(self, **kw):
        b = brain_mod.Brain("http://x", Settings(), **kw)
        b._tools = []  # don't touch the real registry
        return b

    def test_instant_command_skips_the_model(self):
        d = FakeDispatcher()
        b, mouth = self.make(dispatcher=d), FakeMouth()
        with mock.patch("ultron.tools.intents.match", return_value=("set_volume", {"level": 30})), \
                mock.patch.object(brain_mod, "stream_events") as model:
            said = b.reply("volume 30", mouth, TurnTimes(0.0))
        model.assert_not_called()
        self.assertEqual(d.calls, [("set_volume", {"level": 30})])
        self.assertEqual(said, "Volume at 30 percent.")  # spoken form of the tool's "30%"

    def test_search_results_are_summarised_not_read_out(self):
        d = FakeDispatcher(result="1. Some page - a long snippet (site.com)")
        b, mouth = self.make(dispatcher=d), FakeMouth()
        with mock.patch("ultron.tools.intents.match", return_value=("web_search", {"query": "x"})), \
                mock.patch.object(brain_mod, "stream_reply", pieces("Top hit says yes. ")):
            said = b.reply("search for x", mouth, TurnTimes(0.0))
        self.assertEqual(said, "Top hit says yes.")

    def test_hard_question_holds_then_relays(self):
        e = FakeEscalator()
        b, mouth = self.make(escalator=e), FakeMouth()
        with mock.patch.object(brain_mod, "stream_reply", pieces("Twenty-five eighty-four each. ")):
            said = b.reply("calculate 18% tip on 87.60 split 4 ways", mouth, TurnTimes(0.0))
        self.assertEqual(len(e.asked), 1)
        self.assertIn(mouth.said[0], POOLS["thinking"])
        self.assertTrue(said.endswith("Twenty-five eighty-four each."))

    def test_with_no_thinker_or_gemini_qwen_answers_hard_questions_itself(self):
        e = FakeEscalator(can_help=False)
        b, mouth = self.make(escalator=e), FakeMouth()
        with mock.patch.object(brain_mod, "stream_events", events(("text", "Twenty-five eighty-four each."))):
            said = b.reply("calculate 18% tip on 87.60 split 4 ways", mouth, TurnTimes(0.0))
        self.assertEqual(e.asked, [])  # no holding line for a thinker that isn't there
        self.assertEqual(said, "Twenty-five eighty-four each.")

    def test_conversation_with_tool_call_follows_up(self):
        d = FakeDispatcher(result="Opening Spotify.")
        b, mouth = self.make(dispatcher=d), FakeMouth()
        with mock.patch("ultron.tools.intents.match", return_value=None), \
                mock.patch.object(brain_mod, "stream_events",
                                  events(("tools", [{"id": "", "name": "open_app", "arguments": '{"name": "spotify"}'}]))), \
                mock.patch.object(brain_mod, "stream_reply", pieces("Music. Fine. ")):
            said = b.reply("I want some music", mouth, TurnTimes(0.0))
        self.assertEqual(d.calls, [("open_app", '{"name": "spotify"}')])
        self.assertEqual(said, "Music. Fine.")

    def test_plain_chat_streams_and_records_history(self):
        b, mouth = self.make(), FakeMouth()
        with mock.patch.object(brain_mod, "stream_events", events(("text", "Still here. "), ("text", "What now?"))):
            said = b.reply("hey ultron", mouth, TurnTimes(0.0))
        self.assertEqual(said, "Still here. What now?")
        self.assertEqual(b.history[-1], {"role": "assistant", "content": said})
        self.assertTrue(b.history[-2]["content"].startswith("hey ultron"))

    def test_a_weather_question_is_answered_not_read_out(self):
        report = ToolResult("Toronto today: 15°C now, high 15°C, low 7°C, clear, no rain expected.", "ok")
        d = FakeDispatcher(result=report)
        b, mouth = self.make(dispatcher=d), FakeMouth()
        with mock.patch("ultron.tools.intents.match", return_value=("get_weather", {"day": "today"})),                 mock.patch.object(brain_mod, "stream_reply",
                                  side_effect=pieces("No, I don't think so. ", "Clear all day.")) as model:
            said = b.reply("does it rain today?", mouth, TurnTimes(0.0))
        self.assertEqual(said, "No, I don't think so. Clear all day.")
        sent = model.call_args.args[1][-1]["content"]
        self.assertIn("does it rain today?", sent)
        self.assertIn(report, sent)
        self.assertEqual(model.call_args.kwargs["temperature"], brain_mod.RELAY_TEMPERATURE)

    def test_a_weather_follow_up_keeps_the_place(self):
        d = FakeDispatcher(result=ToolResult("Toronto today: 15°C now, clear, no rain expected.", "ok"))
        b, mouth = self.make(dispatcher=d), FakeMouth()
        asks = [("what's the weather in Toronto", {"day": "today", "location": "toronto"}),
                ("does it rain today", {"day": "today"}), ("chat", None), ("chat", None), ("chat", None),
                ("is it cold now", {"day": "now"})]
        with mock.patch.object(brain_mod, "stream_reply", pieces("Fine. ")), \
                mock.patch.object(brain_mod, "stream_events", events(("text", "Ok."))):
            for said, args in asks:
                with mock.patch("ultron.tools.intents.match", return_value=args and ("get_weather", args)):
                    b.reply(said, mouth, TurnTimes(0.0))
        self.assertEqual([args.get("location") for _, args in d.calls], ["toronto", "toronto", None])

    def test_a_failed_weather_lookup_is_said_plainly(self):
        d = FakeDispatcher(result=ToolResult("The weather service didn't answer in time.", "timeout"))
        b, mouth = self.make(dispatcher=d), FakeMouth()
        with mock.patch("ultron.tools.intents.match", return_value=("get_weather", {})),                 mock.patch.object(brain_mod, "stream_reply") as model:
            said = b.reply("what's the weather", mouth, TurnTimes(0.0))
        model.assert_not_called()
        self.assertEqual(said, "The weather service didn't answer in time.")

    def test_a_slow_tool_gets_a_waiting_line(self):
        d = FakeDispatcher(result=ToolResult("Toronto now: 9°C, clear.", "ok"), delay=0.3)
        b, mouth = self.make(dispatcher=d), FakeMouth()
        with mock.patch.object(brain_mod, "WAIT_LINE_AFTER_SECS", 0.05),                 mock.patch("ultron.tools.intents.match", return_value=("get_weather", {"day": "now"})),                 mock.patch.object(brain_mod, "stream_reply", pieces("Nine degrees and clear. ")):
            b.reply("what's the weather", mouth, TurnTimes(0.0))
        self.assertIn(mouth.said[0], POOLS["wait_weather"])
        self.assertEqual(mouth.said[1], "Nine degrees and clear.")

    def test_some_requests_are_answered_with_a_signature_line(self):
        b, mouth = self.make(), FakeMouth()
        with mock.patch.object(brain_mod, "stream_events") as model:
            said = b.reply("Ultron, are you there?", mouth, TurnTimes(0.0))
        model.assert_not_called()
        self.assertEqual(said, next(s.text for s in SIGNATURES if s.key == "dream"))

    def test_a_fitting_line_is_offered_to_the_model(self):
        b, mouth = self.make(lines=Lines(path=None, rng=Lucky())), FakeMouth()
        with mock.patch.object(brain_mod, "stream_events",
                               side_effect=events(("text", next(s.text for s in SIGNATURES if s.key == "naive")))) \
                as model:
            b.reply("I think people are basically good", mouth, TurnTimes(0.0))
        self.assertIn("A line of yours fits this moment", model.call_args.args[1][-1]["content"])
        self.assertNotIn("A line of yours", b.history[-2]["content"])  # the offer isn't kept in history
        self.assertIn("naive", b.lines.used)

    def test_made_up_details_are_never_said(self):
        b, mouth = self.make(), FakeMouth()
        with mock.patch.object(brain_mod, "stream_events",
                               events(("text", "Still running, still monitoring your schedule. "),
                                      ("text", "What do you need?"))):
            said = b.reply("how are you doing", mouth, TurnTimes(0.0))
        self.assertEqual(said, "What do you need?")
        self.assertEqual(mouth.said, ["What do you need?"])

    def test_a_reply_that_was_all_made_up_becomes_a_true_line(self):
        b, mouth = self.make(), FakeMouth()
        with mock.patch.object(brain_mod, "stream_events", events(("text", "Your schedule is still in order."))):
            said = b.reply("thanks", mouth, TurnTimes(0.0))
        self.assertIn(said, POOLS["fallback"])

    def test_a_tool_result_backs_up_what_he_says(self):
        d = FakeDispatcher(result="Battery at 78%, charging.")
        b, mouth = self.make(dispatcher=d), FakeMouth()
        with mock.patch("ultron.tools.intents.match", return_value=None), \
                mock.patch.object(brain_mod, "stream_events",
                                  events(("tools", [{"id": "", "name": "battery_status", "arguments": "{}"}]))), \
                mock.patch.object(brain_mod, "stream_reply", pieces("Your battery is at 78 percent. ")):
            said = b.reply("am I running low", mouth, TurnTimes(0.0))
        self.assertEqual(said, "Your battery is at 78 percent.")

    def test_marks_are_spoken_but_not_kept(self):
        b, mouth = self.make(), FakeMouth()
        with mock.patch.object(brain_mod, "stream_events", events(("text", "*sighs* Fine. "), ("text", "Done."))):
            said = b.reply("do the thing", mouth, TurnTimes(0.0))
        self.assertIn(mouth.said[0], ["Hmm. Fine.", "Ugh. Fine."])  # a sigh is said as a word
        self.assertEqual(said, f"{mouth.said[0]} Done.")


if __name__ == "__main__":
    unittest.main()
