"""The humor dial (tools/personality.py): set by voice like TARS's in Interstellar, kept across
restarts, honest about its range, and read into the personality prompt in bands."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from aj import persona
from aj.tools import apps, dispatch, intents, personality, system
from aj.tools.registry import ToolError


class DialCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.folder = Path(tmp.name)
        patcher = mock.patch.object(personality, "PATH", self.folder / "personality.json")
        patcher.start()
        self.addCleanup(patcher.stop)


class SetHumorTests(DialCase):
    def test_default_is_coopers_final_setting(self):
        self.assertEqual(personality.current_humor(), 60)
        self.assertEqual(personality.DEFAULT_HUMOR, 60)

    def test_set_and_confirm_in_a_few_words(self):
        self.assertEqual(personality.set_humor(level=75), "Humor at 75 percent.")
        self.assertEqual(personality.current_humor(), 75)
        self.assertEqual(personality.set_humor(level=75), "Humor is already at 75 percent.")
        self.assertEqual(personality.set_humor(level=0), "Humor at 0 percent. Jokes off.")
        self.assertEqual(personality.set_humor(level=100), "Humor at 100 percent. You asked for it.")

    def test_steps_of_15(self):
        self.assertEqual(personality.set_humor(change="up"), "Humor at 75 percent.")  # the mission setting
        personality.set_humor(change="down")
        self.assertEqual(personality.set_humor(change="down"), "Humor at 45 percent.")
        personality.set_humor(level=95)
        self.assertEqual(personality.set_humor(change="up"), "Humor at 100 percent. You asked for it.")
        self.assertEqual(personality.set_humor(change="up"), "Humor is already at 100 percent. That's the top.")
        personality.set_humor(level=10)
        self.assertEqual(personality.set_humor(change="down"), "Humor at 0 percent. Jokes off.")
        self.assertEqual(personality.set_humor(change="down"), "Humor is already at 0 percent. That's the bottom.")

    def test_out_of_range_is_refused_honestly(self):
        personality.set_humor(level=40)
        for bad in (150, -10, 101):
            with self.assertRaises(ToolError) as caught:
                personality.set_humor(level=bad)
            self.assertEqual(str(caught.exception), "Humor goes from 0 to 100 percent. It stays at 40 percent.")
        self.assertEqual(personality.current_humor(), 40)
        with self.assertRaises(ToolError):
            personality.set_humor()

    def test_survives_a_restart(self):
        personality.set_humor(level=35)
        saved = json.loads(personality.PATH.read_text(encoding="utf-8"))
        self.assertEqual(saved, {"humor": 35})
        self.assertEqual(personality.current_humor(), 35)  # read from disk each time

    def test_keeps_other_settings_in_the_file(self):
        personality.PATH.write_text(json.dumps({"humor": 20, "later": "kept"}), encoding="utf-8")
        personality.set_humor(level=50)
        self.assertEqual(json.loads(personality.PATH.read_text(encoding="utf-8")), {"humor": 50, "later": "kept"})

    def test_a_bad_file_falls_back_to_the_default(self):
        for junk in ['{"humor": 400}', '{"humor": "lots"}', '[1, 2]', '{"humor": true}']:
            personality.PATH.write_text(junk, encoding="utf-8")
            self.assertEqual(personality.current_humor(), 60, junk)
        personality.PATH.write_text("not json", encoding="utf-8")
        self.assertEqual(personality.current_humor(), 60)  # moved aside to .bad, never deleted

    def test_a_failed_save_says_so(self):
        personality.set_humor(level=30)
        with mock.patch.object(personality, "write_json", side_effect=OSError("disk full")):
            with self.assertRaises(ToolError) as caught:
                personality.set_humor(level=80)
        self.assertIn("stays at 30 percent", str(caught.exception))

    def test_through_the_dispatcher(self):
        d = dispatch.Dispatcher(confirm=lambda q: False, audit_path=self.folder / "audit.jsonl")
        result = d.run("set_humor", {"level": "40%"})
        self.assertEqual((result, result.outcome), ("Humor at 40 percent.", "ok"))  # SAFE: no yes needed
        result = d.run("set_humor", {"level": 400})
        self.assertEqual(result.outcome, "error")
        self.assertEqual(d.run("personality_setting", {}), "Humor at 40 percent.")


class SettingQuestionTests(DialCase):
    def test_humor_and_honesty(self):
        personality.set_humor(level=45)
        self.assertEqual(personality.personality_setting(), "Humor at 45 percent.")
        honesty = personality.personality_setting(setting="honesty")
        self.assertIn("100 percent", honesty)
        self.assertNotIn("90", honesty)  # TARS's own setting, which CORE doesn't allow here


class IntentTests(DialCase):
    def setUp(self):
        super().setUp()
        saved = intents._rules
        self.addCleanup(setattr, intents, "_rules", saved)
        # tools.MODULES order: personality before apps and system
        self.accepted = intents.collect([personality, apps, system])

    def test_every_rule_compiles(self):
        self.assertEqual(self.accepted, len(personality.INTENTS) + len(apps.INTENTS) + len(system.INTENTS))

    def test_setting_it(self):
        for said, level in [("Set humor to sixty.", 60), ("Set your humour to 75 percent.", 75),
                            ("set humor to 60%", 60), ("Humor 40 percent", 40), ("humour at thirty five", 35),
                            ("Set your humor setting to seventy-five per cent", 75),
                            ("Turn your humor down to 40.", 40), ("AJ, set humor to 90", 90),
                            ("Hey Computer, change your humor level to a hundred.", 100), ("max humor", 100),
                            ("set humor to zero", 0), ("humor to 150", 150)]:
            self.assertEqual(intents.match(said), ("set_humor", {"level": level}), said)

    def test_nudging_it(self):
        for said, change in [("Turn your humor down.", "down"), ("turn down your humour a bit", "down"),
                             ("Less jokes.", "down"), ("fewer jokes please", "down"), ("lower your humor", "down"),
                             ("be less funny", "down"), ("Turn your humor up.", "up"), ("Be funnier.", "up"),
                             ("more jokes", "up"), ("crank up the humor a notch", "up"), ("raise your humour", "up")]:
            self.assertEqual(intents.match(said), ("set_humor", {"change": change}), said)

    def test_off_and_on(self):
        for said in ["No jokes.", "Stop joking around.", "Turn off your humor.", "humor off", "no more jokes please"]:
            self.assertEqual(intents.match(said), ("set_humor", {"level": 0}), said)
        self.assertEqual(intents.match("turn your humor back on"), ("set_humor", {"level": 60}))

    def test_asking(self):
        for said in ["What's your humor setting?", "what is your humour level", "AJ, what's your humor at?",
                     "how funny are you set to", "humor setting"]:
            self.assertEqual(intents.match(said), ("personality_setting", {"setting": "humor"}), said)
        for said in ["What's your honesty setting?", "What is your honesty parameter?", "how honest are you",
                     "Set your honesty to 90 percent.", "lower your honesty", "turn your honesty down"]:
            self.assertEqual(intents.match(said), ("personality_setting", {"setting": "honesty"}), said)

    def test_leaves_other_commands_alone(self):
        self.assertEqual(intents.match("turn the volume down"), ("set_volume", {"change": "down"}))
        self.assertEqual(intents.match("set the brightness to sixty"), ("set_brightness", {"level": 60}))
        # his names are wake fillers, never part of the command (2026-10-22: AJ, also Computer)
        for said in ["AJ, turn the volume down.", "A.J. turn the volume down", "turn the volume down, AJ",
                     "Hey Computer, turn the volume down"]:
            self.assertEqual(intents.match(said), ("set_volume", {"change": "down"}), said)
        for said in ["tell me a joke", "that's funny", "you have a good sense of humor", "what is humor",
                     "is honesty the best policy", "why are you so funny", "what is honesty", "humor"]:
            hit = intents.match(said)
            self.assertNotIn(hit and hit[0], ("set_humor", "personality_setting"), said)

    def test_numbers(self):
        for words, n in [("60", 60), ("sixty", 60), ("seventy five", 75), ("zero", 0), ("fifteen", 15),
                         ("a hundred", 100), ("one hundred", 100), ("max", 100), ("half", 50)]:
            self.assertEqual(personality._number(words), n, words)


class PromptBandTests(DialCase):
    def test_the_prompt_follows_the_dial(self):
        personality.set_humor(level=0)
        self.assertIn("humor setting is 0 percent. No jokes", persona.system_prompt(set()))
        personality.set_humor(level=75)
        prompt = persona.system_prompt(set())
        self.assertIn("humor setting is 75 percent", prompt)
        self.assertIn("mock threat", prompt)

    def test_an_explicit_level_wins(self):
        personality.set_humor(level=0)
        self.assertIn("humor setting is 20 percent", persona.system_prompt(set(), humor=20))

    def test_the_humor_change_is_the_only_part_that_changes(self):
        low, high = persona.system_prompt(set(), humor=20), persona.system_prompt(set(), humor=80)
        self.assertEqual(low.split("PERSONALITY:")[0], high.split("PERSONALITY:")[0])  # the core is the same


if __name__ == "__main__":
    unittest.main()
