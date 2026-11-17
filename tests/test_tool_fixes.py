"""The round of 2026-11-18 after the user asked for Chrome's tabs, two commands at once, and a real
test of every tool (scripts/check_tools.py, scripts/check_tool_picks.py). All faked: no window,
key, network or model is touched."""
import json
import re
import unittest
from unittest import mock

from aj import tools
from aj.config import Settings
from aj.core import brain as brain_mod
from aj.core import dictation as dictating
from aj.core.delivery import action_claims
from aj.core.pipeline import TurnTimes, voice_control
from aj.lines import Lines
from aj.tools import calc, intents, system, web
from aj.tools.dispatch import ToolResult

tools.load_all()


def tabs(*titles, selected=1, window=7):
    return [[web.Tab(t, window, i + 1, i + 1 == selected) for i, t in enumerate(titles)]]


STRIP = tabs("What's New", "Inbox (3) - me@gmail.com - Gmail", "Lofi - YouTube",
             "Docs", "Example Domain", selected=4)


class TabTitleTests(unittest.TestCase):
    def test_chromes_memory_note_is_dropped(self):
        self.assertEqual(web.tab_title("Lofi - YouTube - Memory usage - 113 MB"), "Lofi - YouTube")
        self.assertEqual(web.tab_title("Docs - 58.2 MB freed up"), "Docs")
        self.assertEqual(web.tab_title("Song - YouTube - Audio playing"), "Song - YouTube")

    def test_found_by_site_number_or_last(self):
        windows = STRIP
        self.assertEqual(web._find_tab(windows, "youtube").position, 3)
        self.assertEqual(web._find_tab(windows, "gmail").position, 2)
        self.assertEqual(web._find_tab(windows, "my inbox").position, 2)
        self.assertEqual(web._find_tab(windows, number="fifth").position, 5)
        self.assertEqual(web._find_tab(windows, number="2nd").position, 2)
        self.assertEqual(web._find_tab(windows, number="last").position, 5)
        self.assertIsNone(web._find_tab(windows, number="9"))
        self.assertIsNone(web._find_tab(windows, "netflix"))


class OpenWebsiteTabTests(unittest.TestCase):
    def setUp(self):
        for target, kw in [("chrome_tabs", {"return_value": STRIP}), ("_show", {"return_value": True}),
                           ("_keys", {}), ("_wait_count", {"return_value": True}), ("_count_in", {"return_value": 5}),
                           ("_open_in_browser", {}), ("window_showing", {"return_value": None})]:
            patcher = mock.patch.object(web, target, **kw)
            self.addCleanup(patcher.stop)
            setattr(self, target, patcher.start())

    def test_open_goes_to_the_tab_already_open(self):
        self.assertIn("Switched to tab 3", web.open_website("youtube"))
        self._open_in_browser.assert_not_called()

    def test_open_with_no_such_tab_opens_one(self):
        self.assertEqual(web.open_website("netflix"), "Opened netflix.com in a new tab.")

    def test_switch_by_number(self):
        self.assertIn("Switched to tab 5", web.open_website(tab="switch", number="fifth"))

    def test_switch_to_a_tab_that_isnt_there_says_so(self):
        with self.assertRaisesRegex(web.ToolError, "no tab with netflix"):
            web.open_website("netflix", tab="switch")

    def test_new_close_and_close_and_new_use_chromes_keys(self):
        self.assertEqual(web.open_website(tab="new"), "New tab open.")
        self.assertEqual(web.open_website("gmail", tab="close"), "Closed Inbox (3) - me@gmail.com - Gmail.")
        self.assertEqual(web.open_website(tab="close_and_new"), "Closed Docs and opened a new tab.")
        self.assertEqual([c.args[1] for c in self._keys.call_args_list], ["ctrl+t", "ctrl+w", "ctrl+w", "ctrl+t"])

    def test_a_close_that_didnt_happen_isnt_claimed(self):
        self._wait_count.return_value = False
        with self.assertRaisesRegex(web.ToolError, "still there"):
            web.open_website(tab="close")

    def test_list(self):
        said = web.open_website(tab="list")
        self.assertTrue(said.startswith("Chrome has 5 tabs open: 1: What's New; 2: Inbox"), said)
        self.assertIn("Showing tab 4.", said)

    def test_no_chrome(self):
        self.chrome_tabs.return_value = []
        with self.assertRaisesRegex(web.ToolError, "Chrome isn't open"):
            web.open_website(tab="list")


class IntentTests(unittest.TestCase):
    def test_the_users_phrasings(self):
        for said, args in [
            ("Go to the tab that has YouTube already open.", {"tab": "switch", "url_or_site": "youtube"}),
            ("go to the tab that has Gmail open", {"tab": "switch", "url_or_site": "gmail"}),
            ("go to the fifth tab in the row above", {"tab": "switch", "number": "fifth"}),
            ("Open a new tab.", {"tab": "new"}),
            ("Delete this tab and open a new one.", {"tab": "close_and_new"}),
            ("close the Gmail tab", {"tab": "close", "url_or_site": "gmail"}),
            ("what tabs do I have open", {"tab": "list"}),
            ("go to tab five", {"tab": "switch", "number": "5"}),
            ("Can you go to YouTube on Chrome?", {"url_or_site": "youtube"}),
            ("pull up my gmail", {"url_or_site": "gmail"}),
        ]:
            self.assertEqual(intents.match(said), ("open_website", args), said)

    def test_not_tabs(self):
        for said in ["go to sleep", "close the window", "open chrome"]:
            self.assertNotEqual((intents.match(said) or ("",))[0], "open_website", said)

    def test_two_commands_at_once(self):
        self.assertEqual(intents.match_all("lower the brightness by 50% and increase the volume to 100%"),
                         [("set_brightness", {"change": "down", "by": 50}), ("set_volume", {"level": 100})])
        self.assertEqual(intents.match_all("volume 30 then brightness 70"),
                         [("set_volume", {"level": 30}), ("set_brightness", {"level": 70})])
        self.assertIsNone(intents.match_all("play rock and roll on youtube"))  # one command
        self.assertIsNone(intents.match_all("open chrome and tell me a joke"))  # the model's job

    def test_humor_in_plain_words(self):
        for said in ["tone down the jokes a bit", "dial back the humor", "be more serious"]:
            self.assertEqual(intents.match(said), ("set_humor", {"change": "down"}), said)

    def test_no_mute_yourself(self):
        self.assertEqual(voice_control("No, mute yourself."), "hush")
        self.assertEqual(voice_control("Stop, turn off the mic."), "mic off")


class SmallFixTests(unittest.TestCase):
    def test_percent_of(self):
        self.assertEqual(calc.calculate("18% of 87.60"), "It's 15.768.")
        self.assertEqual(calc.calculate("17 times 23"), "It's 391.")
        self.assertEqual(calc.calculate("10 % 3"), "It's 1.")  # still modulo without "of"

    def test_by_an_amount(self):
        self.assertEqual(system._validated(None, "down", "Brightness", 50), (None, -50))
        self.assertEqual(system._validated(None, "up", "Volume"), (None, 10))

    def test_youtube_in_its_newer_layout(self):
        page = '{"lockupViewModel":{"contentId":"abcdefghijk","metadata":{"title":{"content":"A lofi mix"}}}}'
        self.assertEqual(web.first_video(page), ("abcdefghijk", "A lofi mix"))

    def test_a_humor_level_needs_a_tool(self):
        self.assertEqual(action_claims("Humor at 45 percent, sir."), [{"set_humor", "personality_setting"}])


class LandedTests(unittest.TestCase):
    """Keys sent aren't keys received: Notepad still restoring its tabs kept 5 of 13 (2026-11-18)."""

    class Box:
        def __init__(self, keeps: int | None):
            self.keeps, self.text = keeps, ""

        def __call__(self):
            return None if self.keeps is None else self.text

    def test_words_that_didnt_arrive_are_said(self):
        box = self.Box(5)
        box.text = "AJ to"
        self.assertFalse(dictating._landed(box, "AJ tool check", wait=0.05))

    def test_arrived_with_other_spacing_counts(self):
        box = self.Box(99)
        box.text = "Hello\nthere, AJ tool  check"
        self.assertTrue(dictating._landed(box, "AJ tool check"))

    def test_a_box_that_cant_be_read_is_trusted(self):
        self.assertTrue(dictating._landed(self.Box(None), "anything"))


class FakeRunner:
    def __init__(self):
        self.calls = []

    def run(self, name, args):
        self.calls.append((name, args))
        return ToolResult(f"{name.replace('_', ' ')} done.", "ok")


class Unlucky:
    def random(self):
        return 0.99

    def choice(self, seq):
        return seq[0]


class BrainTests(unittest.TestCase):
    def make(self, runner):
        b = brain_mod.Brain("http://x", Settings(), dispatcher=runner, lines=Lines(path=None, rng=Unlucky()))
        b._tools = [{"type": "function", "function": {"name": n, "parameters": {}}}
                    for n in ("open_website", "set_volume", "set_brightness", "list_apps", "open_app")]
        return b

    def test_two_commands_both_done_and_said(self):
        runner = FakeRunner()
        b = self.make(runner)
        said = b.reply("lower the brightness by 50% and increase the volume to 100%", None, TurnTimes(0.0))
        self.assertEqual([c[0] for c in runner.calls], ["set_brightness", "set_volume"])
        self.assertEqual(said, "set brightness done. set volume done.")

    def test_the_anything_else_line_never_reaches_the_model(self):
        runner = FakeRunner()
        b = self.make(runner)
        b.lines.after_tool = lambda tool: "Sir, will there be anything else?"
        said = b.reply("open youtube", None, TurnTimes(0.0))
        self.assertIn("anything else", said)
        self.assertNotIn("anything else", b.history[-1]["content"])
        with mock.patch.object(brain_mod, "stream_events",
                               return_value=iter([("text", "Fine, sir. Will there be anything else, sir?")])), \
                mock.patch("aj.tools.intents.match", return_value=None):
            said = b.reply("how are you", None, TurnTimes(0.0))
        self.assertEqual(said, "Fine, sir.")

    def test_a_claim_without_a_tool_gets_its_tool_under_the_grammar(self):
        """ "Yeah, do that." -> "Opening YouTube." with no tool (live, 2026-11-18)."""
        runner = FakeRunner()
        b = self.make(runner)
        replies = iter([[("text", "Opening YouTube now, sir.")], [("text", "YouTube is open, sir.")]])
        with mock.patch.object(brain_mod, "stream_events", side_effect=lambda *a, **k: iter(next(replies))), \
                mock.patch("aj.tools.intents.match", return_value=None), \
                mock.patch("aj.memory.slot.complete",
                           return_value=json.dumps({"tool": "open_website", "arguments": {"url_or_site": "youtube"}})), \
                mock.patch("builtins.print"):
            said = b.reply("Yeah, do that.", None, TurnTimes(0.0))
        self.assertEqual(runner.calls, [("open_website", '{"url_or_site": "youtube"}')])
        self.assertEqual(said, "YouTube is open, sir.")

    def test_none_under_the_grammar_keeps_the_old_nudge(self):
        runner = FakeRunner()
        b = self.make(runner)
        replies = iter([[("text", "I've turned the heater on, sir.")], [("text", "I can't control a heater, sir.")]])
        with mock.patch.object(brain_mod, "stream_events", side_effect=lambda *a, **k: iter(next(replies))), \
                mock.patch("aj.tools.intents.match", return_value=None), \
                mock.patch("aj.memory.slot.complete", return_value=json.dumps({"tool": "none", "arguments": {}})):
            said = b.reply("turn on the heater", None, TurnTimes(0.0))
        self.assertEqual(runner.calls, [])
        self.assertIn("can't", said)

    def test_the_app_list_doesnt_end_an_open_request(self):
        """ "open the app I use for spreadsheets": list_apps first, then open_app (2026-11-18)."""
        runner = FakeRunner()
        b = self.make(runner)
        rounds = iter([[("tools", [{"id": "1", "name": "list_apps", "arguments": "{}"}])],
                       [("tools", [{"id": "2", "name": "open_app", "arguments": '{"name": "Excel"}'}])]])
        with mock.patch.object(brain_mod, "stream_events", side_effect=lambda *a, **k: iter(next(rounds))), \
                mock.patch("aj.tools.intents.match", return_value=None), \
                mock.patch("aj.tools.intents.match_all", return_value=None):
            said = b.reply("open the app I use for spreadsheets", None, TurnTimes(0.0))
        self.assertEqual([c[0] for c in runner.calls], ["list_apps", "open_app"])
        self.assertEqual(said, "open app done.")


if __name__ == "__main__":
    unittest.main()
