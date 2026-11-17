"""Brain routing: instant commands, hard questions, conversation with tool calls. All faked."""
import random
import sys
import time
import types
import unittest
from unittest import mock

from aj.config import Settings
from aj.core import brain as brain_mod
from aj.core import dictation as dictating
from aj.core.escalate import HardAnswer
from aj.core.pipeline import TurnTimes
from aj.lines import POOLS, SIGNATURES, Lines
from aj.tools.dispatch import ToolResult


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
    """The model's words, as stream_events yields them."""
    return lambda *a, **k: iter([("text", i) for i in items])


def rounds(*each):
    """stream_events for a turn with tool calls: each request gets the next round's events."""
    left = iter(each)
    return lambda *a, **k: iter(next(left))


class BrainTests(unittest.TestCase):
    def make(self, **kw):
        b = brain_mod.Brain("http://x", Settings(), **kw)
        b._tools = []  # don't touch the real registry
        return b

    def test_instant_command_skips_the_model(self):
        d = FakeDispatcher()
        b, mouth = self.make(dispatcher=d), FakeMouth()
        with mock.patch("aj.tools.intents.match", return_value=("set_volume", {"level": 30})), \
                mock.patch.object(brain_mod, "stream_events") as model:
            said = b.reply("volume 30", mouth, TurnTimes(0.0))
        model.assert_not_called()
        self.assertEqual(d.calls, [("set_volume", {"level": 30})])
        self.assertEqual(said, "Volume at 30 percent.")  # spoken form of the tool's "30%"

    def test_search_results_are_summarised_not_read_out(self):
        d = FakeDispatcher(result="1. Some page - a long snippet (site.com)")
        b, mouth = self.make(dispatcher=d), FakeMouth()
        with mock.patch("aj.tools.intents.match", return_value=("web_search", {"query": "x"})), \
                mock.patch.object(brain_mod, "stream_events", pieces("Top hit says yes. ")):
            said = b.reply("search for x", mouth, TurnTimes(0.0))
        self.assertEqual(said, "Top hit says yes.")

    def test_hard_question_holds_then_relays(self):
        e = FakeEscalator()
        b, mouth = self.make(escalator=e), FakeMouth()
        with mock.patch.object(brain_mod, "stream_events", pieces("Twenty-five eighty-four each. ")):
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
        d = FakeDispatcher(result="Volume at 20 percent.")
        b, mouth = self.make(dispatcher=d), FakeMouth()
        with mock.patch("aj.tools.intents.match", return_value=None), \
                mock.patch.object(brain_mod, "stream_events", side_effect=rounds(
                    [("tools", [{"id": "", "name": "set_volume", "arguments": '{"level": 20}'}])],
                    [("text", "Quieter. Fine. ")])) as model:
            said = b.reply("it's too loud", mouth, TurnTimes(0.0))
        self.assertEqual(d.calls, [("set_volume", '{"level": 20}')])
        self.assertEqual(said, "Quieter. Fine.")
        follow_up = model.call_args_list[1]
        self.assertEqual(follow_up.args[1][-1]["role"], "tool")  # the result went back to the model
        self.assertEqual(follow_up.kwargs["tools"], model.call_args_list[0].kwargs["tools"])  # same cached prefix

    def test_tool_calls_can_follow_a_tool_result(self):
        d = FakeDispatcher(result="1. resume.pdf (Documents)")
        b, mouth = self.make(dispatcher=d), FakeMouth()
        with mock.patch("aj.tools.intents.match", return_value=None), \
                mock.patch.object(brain_mod, "stream_events", side_effect=rounds(
                    [("tools", [{"id": "a", "name": "find_files", "arguments": '{"query": "resume"}'}])],
                    [("tools", [{"id": "b", "name": "open_file", "arguments": '{"path_or_index": "1"}'}])],
                    [("text", "There it is.")])):
            said = b.reply("open my resume", mouth, TurnTimes(0.0))
        self.assertEqual([name for name, _ in d.calls], ["find_files", "open_file"])
        self.assertEqual(said, "There it is.")

    def test_relays_offer_the_tools_but_ask_for_words(self):
        b = self.make(dispatcher=FakeDispatcher())
        b._tools = [{"type": "function", "function": {"name": "x", "description": "x", "parameters": {}}}]
        with mock.patch.object(brain_mod, "stream_events", side_effect=pieces("Fine.")) as model:
            b._relay("content", "question", None, TurnTimes(0.0))
        self.assertEqual(model.call_args.kwargs["tools"], b._tools)  # the cached prefix stays valid
        self.assertEqual(model.call_args.kwargs["tool_choice"], "none")

    def test_plain_chat_streams_and_records_history(self):
        b, mouth = self.make(), FakeMouth()
        with mock.patch.object(brain_mod, "stream_events", events(("text", "Still here. "), ("text", "What now?"))):
            said = b.reply("hey computer", mouth, TurnTimes(0.0))
        self.assertEqual(said, "Still here. What now?")
        self.assertEqual(b.history[-1], {"role": "assistant", "content": said})
        self.assertTrue(b.history[-2]["content"].startswith("hey computer"))

    def test_a_weather_question_is_answered_not_read_out(self):
        report = ToolResult("Toronto today: 15°C now, high 15°C, low 7°C, clear, no rain expected.", "ok")
        d = FakeDispatcher(result=report)
        b, mouth = self.make(dispatcher=d), FakeMouth()
        with mock.patch("aj.tools.intents.match", return_value=("get_weather", {"day": "today"})), \
                mock.patch.object(brain_mod, "stream_events",
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
        with mock.patch.object(brain_mod, "stream_events", events(("text", "Ok."))):
            for said, args in asks:
                with mock.patch("aj.tools.intents.match", return_value=args and ("get_weather", args)):
                    b.reply(said, mouth, TurnTimes(0.0))
        self.assertEqual([args.get("location") for _, args in d.calls], ["toronto", "toronto", None])

    def test_a_failed_weather_lookup_is_said_plainly(self):
        d = FakeDispatcher(result=ToolResult("The weather service didn't answer in time.", "timeout"))
        b, mouth = self.make(dispatcher=d), FakeMouth()
        with mock.patch("aj.tools.intents.match", return_value=("get_weather", {})), \
                mock.patch.object(brain_mod, "stream_events") as model:
            said = b.reply("what's the weather", mouth, TurnTimes(0.0))
        model.assert_not_called()
        self.assertEqual(said, "That took too long, so I gave up on it.")  # not the dispatcher's words for the model

    def test_a_slow_tool_gets_a_waiting_line(self):
        d = FakeDispatcher(result=ToolResult("Toronto now: 9°C, clear.", "ok"), delay=0.3)
        b, mouth = self.make(dispatcher=d), FakeMouth()
        with mock.patch.object(brain_mod, "WAIT_LINE_AFTER_SECS", 0.05), \
                mock.patch("aj.tools.intents.match", return_value=("get_weather", {"day": "now"})), \
                mock.patch.object(brain_mod, "stream_events", pieces("Nine degrees and clear. ")):
            b.reply("what's the weather", mouth, TurnTimes(0.0))
        self.assertIn(mouth.said[0], POOLS["wait_weather"])
        self.assertEqual(mouth.said[1], "Nine degrees and clear.")

    def test_some_requests_are_answered_with_a_signature_line(self):
        b, mouth = self.make(), FakeMouth()
        with mock.patch.object(brain_mod, "stream_events") as model:
            said = b.reply("Computer, are you there?", mouth, TurnTimes(0.0))
        model.assert_not_called()
        self.assertEqual(said, next(s.text for s in SIGNATURES if s.key == "dream"))

    def test_a_fitting_line_is_offered_to_the_model(self):
        b, mouth = self.make(lines=Lines(path=None, rng=Lucky())), FakeMouth()
        with mock.patch.object(brain_mod, "stream_events",
                               side_effect=events(("text", next(s.text for s in SIGNATURES if s.key == "naive")))) \
                as model:
            b.reply("I think people are basically good", mouth, TurnTimes(0.0))
        self.assertIn("One of your signature lines fits this moment", model.call_args.args[1][-1]["content"])
        self.assertNotIn("signature lines", b.history[-2]["content"])  # the offer isn't kept in history
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
        with mock.patch("aj.tools.intents.match", return_value=None), \
                mock.patch.object(brain_mod, "stream_events", side_effect=rounds(
                    [("tools", [{"id": "", "name": "battery_status", "arguments": "{}"}])],
                    [("text", "Your battery is at 78 percent. ")])):
            said = b.reply("am I running low", mouth, TurnTimes(0.0))
        self.assertEqual(said, "Your battery is at 78 percent.")

    def test_marks_are_spoken_but_not_kept(self):
        b, mouth = self.make(), FakeMouth()
        with mock.patch.object(brain_mod, "stream_events", events(("text", "*sighs* Fine. "), ("text", "Ask away."))):
            said = b.reply("I have a question", mouth, TurnTimes(0.0))
        self.assertIn(mouth.said[0], ["Hmm. Fine.", "Ugh. Fine."])  # a sigh is said as a word
        self.assertEqual(said, f"{mouth.said[0]} Ask away.")


class HonestyTests(unittest.TestCase):
    """Section 2 of docs/FIX_PLAN.md: what he says must be what he did."""

    def make(self, **kw):
        b = brain_mod.Brain("http://x", Settings(), **kw)
        b._tools = []
        return b

    def test_told_hes_wrong_he_takes_the_reading_again(self):
        d = FakeDispatcher(result=ToolResult("Battery at 80%, plugged in.", "ok"))
        b, mouth = self.make(dispatcher=d), FakeMouth()
        with mock.patch("aj.tools.intents.match", return_value=("battery_status", {})):
            b.reply("what's my battery", mouth, TurnTimes(0.0))
        with mock.patch("aj.tools.intents.match", return_value=None),                 mock.patch.object(brain_mod, "stream_events", side_effect=pieces("It's 80 percent. I was right.")) as model:
            said = b.reply("That's not true.", mouth, TurnTimes(0.0))
        self.assertEqual([name for name, _ in d.calls], ["battery_status", "battery_status"])
        self.assertIn("returned just now: Battery at 80%", model.call_args.args[1][-1]["content"])
        self.assertEqual(said, "It's 80 percent. I was right.")

    def test_a_claim_without_a_tool_gets_one_more_chance_to_call_it(self):
        d = FakeDispatcher(result=ToolResult("Opening Settings.", "ok"))
        b, mouth = self.make(dispatcher=d), FakeMouth()
        with mock.patch("aj.tools.intents.match", return_value=None),                 mock.patch.object(brain_mod, "stream_events", side_effect=rounds(
                    [("text", "Settings are now open.")],
                    [("tools", [{"id": "a", "name": "open_app", "arguments": '{"name": "settings"}'}])],
                    [("text", "There.")])) as model:
            said = b.reply("pull up the settings thing", mouth, TurnTimes(0.0))
        self.assertIn("no tool ran", model.call_args_list[1].args[1][-1]["content"])
        self.assertEqual(d.calls, [("open_app", '{"name": "settings"}')])
        # The false claim was never said, and an app tool's own result is the answer (2026-10-23):
        # no third request for the model to word it.
        self.assertEqual(said, "Opening Settings.")
        self.assertEqual(model.call_count, 2)

    def test_an_app_tools_result_is_said_as_it_is(self):
        # Live (2026-10-23) switch_to_app worked for "open Chrome" and he said only "What do you need?".
        for result, expected in [(ToolResult("Switching to Google Chrome.", "ok"), "Switching to Google Chrome."),
                                 (ToolResult("I can't find an app called Photoshop.", "error"),
                                  "I can't find an app called Photoshop.")]:
            d = FakeDispatcher(result=result)
            b, mouth = self.make(dispatcher=d), FakeMouth()
            with mock.patch("aj.tools.intents.match", return_value=None), \
                    mock.patch.object(brain_mod, "stream_events", side_effect=rounds(
                        [("tools", [{"id": "a", "name": "open_app", "arguments": '{"name": "chrome"}'}])],
                        [("text", "What do you need?")])) as model:
                said = b.reply("jarvis bring chrome up for me", mouth, TurnTimes(0.0))
            self.assertEqual(said, expected)
            self.assertEqual(model.call_count, 1)

    def test_saying_an_app_cant_be_opened_without_trying_gets_it_tried(self):
        # Live (2026-10-23): "I can't open Claude." with Claude installed, and no tool called.
        d = FakeDispatcher(result=ToolResult("Opening Claude.", "ok"))
        b, mouth = self.make(dispatcher=d), FakeMouth()
        with mock.patch("aj.tools.intents.match", return_value=None), \
                mock.patch.object(brain_mod, "stream_events", side_effect=rounds(
                    [("text", "I can't open Claude. Only Chrome is available.")],
                    [("tools", [{"id": "a", "name": "open_app", "arguments": '{"name": "Claude"}'}])])) as model:
            said = b.reply("open the claude thing", mouth, TurnTimes(0.0))
        self.assertIn("never called open_app", model.call_args_list[1].args[1][-1]["content"])
        self.assertEqual(d.calls, [("open_app", '{"name": "Claude"}')])
        self.assertEqual(said, "Opening Claude.")  # the refusal was never said
        self.assertEqual(mouth.said, ["Opening Claude."])

    def test_a_checked_launch_is_in_what_he_did_lately(self):
        b = self.make()
        b.record("Quick Share is open: checked a few seconds after launching it.")
        with mock.patch.object(brain_mod, "stream_events", side_effect=pieces("Yes.")) as model:
            b.reply("did the app open?", None, TurnTimes(0.0))
        sent = model.call_args.args[1][-1]["content"]
        self.assertIn("Quick Share is open: checked", sent)
        self.assertNotIn("Since your last reply", sent)  # nothing to announce

    def test_if_he_still_cant_he_says_it_isnt_done(self):
        b, mouth = self.make(dispatcher=FakeDispatcher()), FakeMouth()
        with mock.patch("aj.tools.intents.match", return_value=None),                 mock.patch.object(brain_mod, "stream_events", side_effect=rounds(
                    [("text", "Message sent.")], [("text", "Message sent to Appa.")])):
            said = b.reply("text appa hi", mouth, TurnTimes(0.0))
        self.assertIn(said, POOLS["not_done"])

    def test_a_no_in_the_window_is_said_in_words_not_read_out(self):
        d = FakeDispatcher(result=ToolResult("Not done: the user said no to close_app. Don't retry.", "declined"))
        b, mouth = self.make(dispatcher=d), FakeMouth()
        with mock.patch("aj.tools.intents.match", return_value=("close_app", {"name": "Spotify"})):
            said = b.reply("close spotify", mouth, TurnTimes(0.0))
        self.assertIn(said, POOLS["declined"])

    def test_and_tomorrow_after_the_weather_fetches_tomorrows_forecast(self):
        d = FakeDispatcher(result=ToolResult("Here today: 27°C, storms.", "ok"))
        b, mouth = self.make(dispatcher=d), FakeMouth()
        with mock.patch("aj.tools.intents.match", return_value=None),                 mock.patch.object(brain_mod, "stream_events", side_effect=lambda *a, **k: iter([("text", "Storms.")])):
            b._weather_turn = None
            with mock.patch("aj.tools.intents.match", return_value=("get_weather", {"day": "today"})):
                b.reply("what's the weather", mouth, TurnTimes(0.0))
            b.reply("And tomorrow?", mouth, TurnTimes(0.0))
            b.reply("tomorrow", mouth, TurnTimes(0.0))  # still within two turns
        self.assertEqual([args.get("day") for _, args in d.calls], ["today", "tomorrow", "tomorrow"])

    def test_and_tomorrow_out_of_the_blue_is_left_to_the_model(self):
        d = FakeDispatcher()
        b = self.make(dispatcher=d)
        self.assertIsNone(b._weather_follow_up("And tomorrow?"))

    def test_an_echoed_speaker_label_is_not_spoken(self):
        # Either of his names: AJ since 2026-10-22, still called Computer too.
        for label in ("AJ", "Computer"):
            b, mouth = self.make(), FakeMouth()
            with mock.patch.object(brain_mod, "stream_events", side_effect=pieces(f"{label}: Ready. ", "Go on.")):
                said = b.reply("who are you", mouth, TurnTimes(0.0))
            self.assertEqual(said, "Ready. Go on.", label)

    def test_what_happened_in_between_is_in_the_next_message(self):
        b = self.make()
        b.note("WhatsApp didn't open: its window never appeared.")
        with mock.patch.object(brain_mod, "stream_events", side_effect=pieces("Right.")) as model:
            b.reply("did it open?", None, TurnTimes(0.0))
        self.assertIn("[Since your last reply: WhatsApp didn't open", model.call_args.args[1][-1]["content"])
        with mock.patch.object(brain_mod, "stream_events", side_effect=pieces("Fine.")) as model:
            b.reply("ok", None, TurnTimes(0.0))
        self.assertNotIn("Since your last reply", model.call_args.args[1][-1]["content"])  # told once

class FakeDesk:
    """The parts of core.dictation.Dictation the brain uses."""

    def __init__(self, armed=None):
        self.armed, self.turn_audio, self.last_heard = armed, ("audio",), ""
        self.lent, self.armed_with, self.replies = [], [], []

    def new_turn(self):
        pass

    def lend(self, pieces, said, command, request=None):
        self.lent.append((pieces, said, command, request))

    def arm(self, polish=False, app="", check=True):
        self.armed_with.append((app, check))
        self.armed = dictating.Request(7, polish, app)
        return dictating.GO_AHEAD

    def disarm(self):
        request, self.armed = self.armed, None
        return request

    def quick_listen(self, pieces):
        return ""

    def saw_reply(self, reply):
        self.replies.append(reply)


class FakeMemory:
    def __init__(self):
        self.turns = []

    def context_for(self, text):
        return ""

    def after_turn(self, text, reply):
        self.turns.append(text)


class DictationTests(unittest.TestCase):
    """Typing and WhatsApp chats in the brain (2026-10-22)."""

    def make(self, desk=None, **kw):
        b = brain_mod.Brain("http://x", Settings(), dictation=desk or FakeDesk(), **kw)
        b._tools = []
        return b

    def test_the_new_tools_are_offered_and_waited_for(self):
        for name in ("whatsapp_open_chat", "start_dictation", "type_text", "set_humor", "personality_setting"):
            self.assertIn(name, brain_mod.LLM_TOOLS)
        self.assertLessEqual({"whatsapp_open_chat", "type_text"}, brain_mod._SLOW)

    def test_type_in_one_go_hands_over_this_turns_audio(self):
        desk = FakeDesk()
        d = FakeDispatcher(result=ToolResult("Typed it.", "ok"))
        b, mouth = self.make(desk, dispatcher=d), FakeMouth()
        with mock.patch("aj.tools.intents.match", return_value=("type_text", {"text": "ill be late"})):
            said = b.reply("Type I'll be late.", mouth, TurnTimes(0.0))
        self.assertEqual(desk.lent, [(("audio",), "Type I'll be late.", True, None)])
        self.assertEqual(said, "Typed it.")

    def test_a_long_typing_result_is_said_as_it_is(self):
        line = (f"{dictating.OFFLINE} Typed the polished version in WhatsApp. {dictating.PRESS_SEND} "
                f"{dictating.CAPPED}")
        self.assertGreater(len(line), brain_mod._SPEAK_DIRECTLY_MAX)
        d = FakeDispatcher(result=ToolResult(line, "ok"))
        b, mouth = self.make(dispatcher=d), FakeMouth()
        with mock.patch("aj.tools.intents.match", return_value=("type_text", {"text": "x"})), \
                mock.patch.object(brain_mod, "stream_events") as model:
            said = b.reply("type x", mouth, TurnTimes(0.0))
        model.assert_not_called()
        self.assertEqual(said, line)

    def test_an_opened_chat_asks_what_to_say_and_listens(self):
        desk = FakeDesk()
        d = FakeDispatcher(result=ToolResult("Appa's chat is open.", "ok"))
        b, mouth = self.make(desk, dispatcher=d), FakeMouth()
        with mock.patch("aj.tools.intents.match", return_value=("whatsapp_open_chat", {"name": "Appa"})):
            said = b.reply("go to Appa", mouth, TurnTimes(0.0))
        self.assertEqual(said, "Appa's chat is open. What would you like to say?")
        self.assertEqual(desk.armed_with, [("whatsapp", False)])

    def test_no_chat_no_question(self):
        """"No chat called Bob" is a failure since 2026-10-22 (ToolError): said as it is, no question."""
        desk = FakeDesk()
        d = FakeDispatcher(result=ToolResult("No chat called Bob in WhatsApp.", "error"))
        b, mouth = self.make(desk, dispatcher=d), FakeMouth()
        with mock.patch("aj.tools.intents.match", return_value=("whatsapp_open_chat", {"name": "Bob"})):
            said = b.reply("go to Bob", mouth, TurnTimes(0.0))
        self.assertEqual(said, "No chat called Bob in WhatsApp.")
        self.assertEqual(desk.armed_with, [])

    def test_a_chat_the_model_opened_asks_too(self):
        desk = FakeDesk()
        d = FakeDispatcher(result=ToolResult("Suhinish's chat is open.", "ok"))
        b, mouth = self.make(desk, dispatcher=d), FakeMouth()
        with mock.patch("aj.tools.intents.match", return_value=None), \
                mock.patch.object(brain_mod, "stream_events", side_effect=rounds(
                    [("tools", [{"id": "a", "name": "whatsapp_open_chat", "arguments": '{"name": "Suhinish"}'}])],
                    [("text", "Suhinish's chat is open.")])) as model:
            said = b.reply("I want to message Suhinish", mouth, TurnTimes(0.0))
        self.assertIn(brain_mod._CHAT_OPEN_HOW, model.call_args_list[1].args[1][-1]["content"])
        self.assertEqual(said, "Suhinish's chat is open. What would you like to say?")
        self.assertEqual(desk.armed_with, [("whatsapp", False)])

    def test_waiting_for_dictation_the_next_words_are_typed(self):
        desk, memory = FakeDesk(armed=dictating.Request(7, True, "")), FakeMemory()
        d = FakeDispatcher(result=ToolResult("Typed the polished version.", "ok"))
        b, mouth = self.make(desk, dispatcher=d, memory=memory), FakeMouth()
        with mock.patch.object(brain_mod, "stream_events") as model:
            said = b.reply("i will be late", mouth, TurnTimes(0.0))
        model.assert_not_called()
        self.assertEqual(d.calls, [("type_text", {"text": "i will be late", "polish": True})])
        self.assertEqual(desk.lent[0][1:3], ("i will be late", False))
        self.assertEqual(said, "Typed the polished version.")
        self.assertIsNone(desk.armed)
        self.assertEqual(memory.turns, [])  # his message to someone else is not about him
        self.assertIn("not said to you", b.history[-2]["content"])

    def test_cancelled_or_capped(self):
        d = FakeDispatcher(result=ToolResult("Typed it.", "ok"))
        b, mouth = self.make(FakeDesk(armed=dictating.Request(7)), dispatcher=d), FakeMouth()
        self.assertEqual(b.reply("Never mind.", mouth, TurnTimes(0.0)), dictating.CANCELLED)
        self.assertEqual(d.calls, [])
        b.dictation.armed = dictating.Request(7)
        heard, said = b.dictate(None, mouth, TurnTimes(0.0), typed="a long one", capped=True)
        self.assertEqual(said, f"Typed it. {dictating.CAPPED}")

    def test_his_drafts_are_remembered_for_type_that(self):
        desk = FakeDesk()
        b = self.make(desk)
        with mock.patch.object(brain_mod, "stream_events", side_effect=pieces('Here: "Hi Amma, I\'m late." ')):
            b.reply("write a message to mum saying I'm late", None, TurnTimes(0.0))
        self.assertEqual(desk.replies, ['Here: "Hi Amma, I\'m late."'])

    def test_the_prompt_is_rebuilt_when_the_humor_dial_turns(self):
        dial = types.SimpleNamespace(current_humor=lambda: 60)
        built = []

        def system_prompt(tools, name, humor=None, alias="Computer"):
            built.append(humor)
            return f"prompt at {humor}"

        b = self.make()
        with mock.patch.dict(sys.modules, {"aj.tools.personality": dial}), \
                mock.patch.object(brain_mod.persona, "system_prompt", system_prompt):
            first, again = b._system_prompt(), b._system_prompt()
            dial.current_humor = lambda: 45
            turned = b._system_prompt()
        self.assertEqual((first, again, turned), ("prompt at 60", "prompt at 60", "prompt at 45"))
        self.assertEqual(built, [60, 45])  # cached in between


if __name__ == "__main__":
    unittest.main()
