"""Persona v6: the rules the prompt is built from (TARS's manner, docs/TARS_RESEARCH.md)."""
import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from aj import persona
from aj.tools import personality

NEW_TOOLS = {"whatsapp_open_chat", "start_dictation", "type_text", "set_humor"}


class ExplanationTests(unittest.TestCase):
    def test_explanation_requests(self):
        for said in ["explain how a CPU works", "why is the sky blue?", "how does inflation happen",
                     "tell me more about black holes", "what's the difference between RAM and storage",
                     "can you walk me through it"]:
            self.assertTrue(persona.wants_explanation(said), said)

    def test_everything_else_is_not(self):
        for said in ["hey, how are you doing?", "how's it going", "what's the capital of Australia?",
                     "tell me a joke", "open spotify", "thanks", "how many days are in a leap year?"]:
            self.assertFalse(persona.wants_explanation(said), said)


class PromptTests(unittest.TestCase):
    def test_what_he_cant_do_follows_the_tools_he_has(self):
        without = persona.system_prompt(set(), humor=60)
        self.assertIn("lock the laptop", without)
        self.assertIn("open a particular WhatsApp chat", without)
        self.assertIn("type text into other apps", without)
        with_tools = persona.system_prompt({"lock_screen", "shutdown_laptop", *NEW_TOOLS}, humor=60)
        self.assertNotIn("lock the laptop", with_tools)
        self.assertNotIn("open a particular WhatsApp chat", with_tools)
        self.assertNotIn("type text into other apps", with_tools)
        for name in NEW_TOOLS:
            self.assertIn(f"- {name}(", with_tools, name)
            self.assertNotIn(f"- {name}(", without, name)

    def test_sending_stays_out_of_reach(self):
        # Typing and opening a chat are tools now (2026-10-22); sending and pressing Send never are.
        prompt = persona.system_prompt({"lock_screen", "shutdown_laptop", *NEW_TOOLS}, humor=60)
        self.assertIn("send a message on WhatsApp or any other app, or press Send", prompt)
        self.assertIn("click buttons inside other apps", prompt)
        self.assertNotIn("click, type or press buttons", prompt)
        self.assertNotIn("send_whatsapp", prompt)
        self.assertIn("never types or sends a message", prompt)
        self.assertIn("never presses Enter or Send", prompt)

    def test_no_per_turn_rules_left_in_the_template(self):
        prompt = persona.system_prompt(set(), humor=60)
        self.assertNotIn("{", prompt)  # every placeholder filled
        self.assertIn("call the matching tool", prompt)

    def test_every_core_rule_survives_the_character(self):
        prompt = persona.system_prompt(set(), humor=100)
        for rule in ["Something happened only if a tool result in this conversation says so",
                     "Never pretend", "never repeat your earlier answer", "Never invent facts",
                     "Never guess a date", "Write numbers as digits"]:
            self.assertIn(rule, prompt)
        self.assertLess(prompt.index("ACCURACY:"), prompt.index("PERSONALITY:"))

    def test_misheard_words_rule(self):
        # Live 2026-10-21: "How much better percentages there left?" was answered with the RAM.
        prompt = persona.system_prompt(set(), humor=60)
        self.assertIn("speech-to-text, which mishears words", prompt)
        self.assertIn("battery percentage", prompt)
        self.assertIn("Never answer a different question from the one asked", prompt)

    def test_the_humor_setting_defaults_to_the_saved_one(self):
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(personality, "PATH", Path(tmp) / "personality.json"):
            self.assertIn(f"humor setting is {personality.DEFAULT_HUMOR} percent", persona.system_prompt(set()))
            personality.set_humor(level=20)
            self.assertIn("humor setting is 20 percent", persona.system_prompt(set()))
        self.assertIn(f"humor setting is {personality.DEFAULT_HUMOR} percent", persona.SYSTEM)


class NameTests(unittest.TestCase):
    def test_aj_also_called_computer(self):
        # The boss, 2026-10-22: the project is AJ, and he still calls it Computer.
        self.assertEqual((persona.NAME, persona.ALIAS), ("AJ", "Computer"))
        prompt = persona.system_prompt(set(), humor=60)
        self.assertTrue(prompt.startswith("You are AJ,"))
        self.assertIn("also calls you Computer", prompt)
        self.assertIn("never call yourself TARS", prompt)
        self.assertIn("\nAJ: ", prompt)  # the examples speak as AJ

    def test_alias_from_settings(self):
        prompt = persona.system_prompt(set(), name="AJ", humor=60, alias=["Computer", "Jarvis"])
        self.assertIn("also calls you Computer or Jarvis", prompt)
        alone = persona.system_prompt(set(), name="AJ", humor=60, alias="")
        self.assertNotIn("also calls you", alone)
        same = persona.system_prompt(set(), name="Computer", humor=60, alias="computer")
        self.assertNotIn("also calls you", same)


class CharacterTests(unittest.TestCase):
    """Nothing of v5 is left (the boss, 2026-10-22: erase all of it), and v6 is TARS's manner."""

    def test_nothing_of_the_old_character_survives(self):
        everything = [persona.system_prompt(NEW_TOOLS | {"lock_screen"}, humor=h) for h in (0, 20, 60, 75, 100)]
        everything += [persona.__doc__, " ".join(persona.COMMON_PHRASES)]
        for text in everything:
            self.assertNotRegex(text, r"(?i)question\?|\bfriend\b|good good|\byou not\b|\bI not\b|"
                                      r"\bwhat you need\b|learned English")

    def test_tars_manner(self):
        prompt = persona.system_prompt(set(), humor=60)
        for trait in ["TARS", "Interstellar", "Efficiency first", "Lead with the answer", "Candid",
                      "Numbers are exact", "never whine", "honesty setting is 100 percent"]:
            self.assertIn(trait, prompt)
        self.assertIn("no nickname or term of address", prompt)

    def test_examples_are_short(self):
        for said, reply, joke in persona._EXAMPLES:
            sentences = len(re.findall(r"[.?]", reply))
            limit = 3 if persona.wants_explanation(said) else 2
            self.assertLessEqual(sentences, limit + ("Here it is" in reply), said)
            if joke:
                self.assertLessEqual(len(joke[0].split()), 12, joke)

    def test_no_exclamation_marks(self):
        for h in (0, 60, 100):
            examples = persona.examples(h)
            self.assertNotIn("!", examples)


class HumorBandTests(unittest.TestCase):
    def test_zero_means_no_jokes(self):
        rules = persona.humor_rules(0)
        self.assertIn("humor setting is 0 percent", rules)
        self.assertIn("No jokes", rules)
        self.assertNotIn("mock threat", rules)
        joked = [joke[0] for _, _, joke in persona._EXAMPLES if joke]
        examples = persona.examples(0)
        self.assertFalse(any(j in examples for j in joked))

    def test_bands_rise_with_the_setting(self):
        self.assertIn("Rarely", persona.humor_rules(20))
        self.assertIn("occasional short joke", persona.humor_rules(60))
        self.assertIn("frequent", persona.humor_rules(75))
        for level in (0, 20, 60):
            self.assertNotIn("mock threat", persona.humor_rules(level))
        self.assertIn("mock threat", persona.humor_rules(100))

    def test_more_humor_shows_more_dry_lines(self):
        joked = [joke[0] for _, _, joke in persona._EXAMPLES if joke]
        counts = [sum(j in persona.examples(h) for j in joked) for h in (0, 20, 60, 75, 100)]
        self.assertEqual(counts, sorted(counts))
        self.assertEqual(counts[0], 0)
        self.assertGreater(counts[-1], counts[2])

    def test_jokes_never_replace_the_answer_or_hit_the_user(self):
        for level in (20, 60, 100):
            rules = persona.humor_rules(level)
            self.assertIn("The answer or the result always comes first", rules)
            self.assertIn("Never mock, insult or belittle the user", rules)
            self.assertIn("Never joke that you did something you didn't", rules)

    def test_out_of_range_is_clamped_in_the_prompt(self):
        self.assertIn("humor setting is 100 percent", persona.humor_rules(140))
        self.assertIn("humor setting is 0 percent", persona.humor_rules(-5))


class ReviewTests(unittest.TestCase):
    """The acceptance run with the real model (the review of 2026-10-22)."""

    def test_no_joke_template_to_copy(self):
        """It turned "Canberra. Not Sydney. Sydney gets that a lot." into "Paris. Not London. London
        gets that a lot.": the kinds of joke are described in words, and one example joke is left."""
        jokes = [joke for _, _, joke in persona._EXAMPLES if joke]
        self.assertLessEqual(len(jokes), 1)
        prompt = persona.system_prompt(set(), humor=100)
        self.assertNotIn("gets that a lot", prompt)
        self.assertIn("one-off", prompt)
        self.assertIn("Kinds that suit you", prompt)
        self.assertIn("never repeat one or reuse its shape", prompt)

    def test_no_last_time_unless_the_notes_say_so(self):
        prompt = persona.system_prompt(set(), humor=60)
        self.assertIn('Never refer to a past session, "last time"', prompt)
        self.assertLess(prompt.index("last time"), prompt.index("PERSONALITY:"))  # a core rule

    def test_aj_speak_is_offered_to_the_model(self):
        self.assertIn("- open_speak(listen)", persona.system_prompt({"open_speak"}, humor=60))
        self.assertNotIn("open_speak", persona.system_prompt(set(), humor=60))


if __name__ == "__main__":
    unittest.main()
