"""Delivery: no aside twice, humanity rationed, spoken reactions and sighs where a person would
make them."""
import random
import unittest

from ultron.core.delivery import BETWEEN, HUMANITY_EVERY, MAX_REACTIONS, OPENERS, SIGH_EVERY, SIGHS, Delivery


class Never(random.Random):
    def random(self) -> float:
        return 0.99  # no optional reactions: only the rules under test


class Always(random.Random):
    def random(self) -> float:
        return 0.0


def turn(d: Delivery, user: str, *sentences: str, explain: bool = False) -> list[str]:
    d.start_turn(user, explain=explain)
    return [s for s in (d.shape(x) for x in sentences) if s is not None]


LONG = "This sentence is long enough that a person might react to their own thought before it."


class RepeatTests(unittest.TestCase):
    def test_a_repeated_aside_is_dropped_but_the_answer_never_is(self):
        d = Delivery(Never())
        turn(d, "tell me a joke", "Mondays exist.", "Your printer is plotting against you again.")
        said = turn(d, "another one", "Mondays exist.", "Your printer is plotting against you again, as usual.")
        self.assertEqual(said, ["Mondays exist."])  # the answer stays even when it's the same

    def test_retelling_facts_is_not_a_repeat(self):
        d = Delivery(Never())
        turn(d, "weather", "It's 15 degrees.", "Clear skies all day with no rain expected.")
        d.start_turn("weather again")
        d.strict = False
        self.assertEqual([d.shape("It's 15 degrees."), d.shape("Clear skies all day with no rain expected.")],
                         ["It's 15 degrees.", "Clear skies all day with no rain expected."])

    def test_the_offered_line_is_allowed(self):
        d = Delivery(Never())
        turn(d, "x", "Fine.", "That's touchingly optimistic, honestly, completely.")
        d.start_turn("people are good")
        d.allow("That's touchingly optimistic.")
        self.assertIsNotNone(d.shape("Well."))
        self.assertIsNotNone(d.shape("That's touchingly optimistic, honestly, completely."))


class InventedFactsTests(unittest.TestCase):
    """Sentences the live model actually said (2026-10-06) that nobody had told it."""

    def test_made_up_details_are_dropped_whole_sentence_and_all(self):
        d = Delivery(Never())
        said = turn(d, "hey ultron, how are you doing?", "Still running, still monitoring your schedule,",
                    "still not impressed by your morning coffee choices.", "What do you need?")
        self.assertEqual(said, ["What do you need?"])
        self.assertEqual(d.invented, 1)
        for line in ("The laptop's battery is at 78 percent.", "Your grades are below average.",
                     "Battery at 63 percent, temperature stable.", "No new tasks assigned."):
            self.assertEqual(turn(d, "hi", "Hello.", line), ["Hello."], line)

    def test_what_he_was_told_is_fine(self):
        d = Delivery(Never())
        self.assertEqual(turn(d, "I have a meeting at 3 and an exam tomorrow", "Your meeting is at 3.",
                              "Your exam won't revise itself."),
                         ["Your meeting is at 3.", "Your exam won't revise itself."])
        d.start_turn("how's my battery")
        d.ground("Battery at 78%, charging.")  # a tool's result
        self.assertEqual(d.shape("Your battery is at 78 percent."), "Your battery is at 78 percent.")

    def test_claiming_to_have_done_something_needs_a_tool_to_have_run(self):
        d = Delivery(Never())
        for line in ("Set a reminder for 3 PM.", "Today I processed tasks.", "Done, I've muted it."):
            self.assertEqual(turn(d, "I have a meeting at 3", "Okay.", line), ["Okay."], line)
        self.assertEqual(turn(d, "I have a meeting at 3", "Want me to set a reminder for 3?"),
                         ["Want me to set a reminder for 3?"])  # an offer, not a claim
        d.start_turn("remind me at 3")
        d.acted = True  # the reminder tool ran
        self.assertEqual(d.shape("Set a reminder for 3 PM."), "Set a reminder for 3 PM.")

    def test_no_pretend_work_on_the_bosss_things(self):
        d = Delivery(Never())
        said = turn(d, "how are you doing?", "Still running, still analyzing your files,",
                    "still waiting for your next command.", "What do you need?")
        self.assertEqual(said, ["What do you need?"])
        said = turn(d, "what have you been up to?", "I've been idle, monitoring, and waiting.", "What now?")
        self.assertEqual(said, ["What now?"])

    def test_no_countdowns_nobody_asked_for(self):
        d = Delivery(Never())
        said = turn(d, "I have a meeting at 3", "The meeting is at 3:00 PM.",
                    "You've got two hours and thirty-four minutes before it starts.")
        self.assertEqual(said, ["The meeting is at 3:00 PM."])  # the live model got the sum wrong
        said = turn(d, "how long until my meeting at 3?", "You've got five hours and thirty minutes.")
        self.assertEqual(len(said), 1)  # asked for: that's the answer

    def test_knowledge_questions_and_weather_pass(self):
        d = Delivery(Never())
        said = turn(d, "why does coffee keep me awake", "Caffeine blocks adenosine, the chemical that makes "
                    "you feel tired.", "Did you have coffee this morning?",
                    "The temperature is 6 degrees right now.")
        self.assertEqual(len(said), 3)


class HumanityTests(unittest.TestCase):
    def test_one_humanity_remark_in_a_while(self):
        d = Delivery(Never())
        first = turn(d, "how was your day", "Long.", "Humanity surviving this long remains a minor miracle.")
        self.assertEqual(len(first), 2)
        for _ in range(HUMANITY_EVERY - 1):
            said = turn(d, "and now?", "Fine.", "Your species keeps inventing new ways to waste time.")
            self.assertEqual(said, ["Fine."])
        said = turn(d, "and now?", "Fine.", "Your species keeps inventing new ways to waste time.")
        self.assertEqual(len(said), 2)

    def test_allowed_when_the_talk_is_about_people(self):
        d = Delivery(Never())
        turn(d, "x", "Ok.", "Humanity is a rounding error.")
        said = turn(d, "why do humans fight wars?", "Resources, mostly.", "Humans also love a flag.")
        self.assertEqual(len(said), 2)


class SighTests(unittest.TestCase):
    def test_a_sigh_is_said_as_a_word_and_rationed(self):
        d = Delivery(Never())
        first = turn(d, "a", "[sigh] Fine.")[0]
        self.assertIn(first, [f"{s} Fine." for s in SIGHS])
        self.assertEqual(turn(d, "b", "[sigh] Again."), ["Again."])  # too soon after the last one
        for _ in range(SIGH_EVERY - 2):
            turn(d, "c", "Ok.")
        again = turn(d, "d", "[sigh] Fine.")[0]
        self.assertIn(again, [f"{s} Fine." for s in SIGHS])
        self.assertNotEqual(again, first)  # not the same sigh twice running

    def test_a_voice_that_can_sigh_keeps_the_sigh(self):
        d = Delivery(Never(), real_sighs=True)
        self.assertEqual(turn(d, "a", "[sigh] Fine."), ["[sigh] Fine."])
        self.assertEqual(turn(d, "b", "[sigh] Again."), ["Again."])  # still rationed

    def test_no_sigh_on_top_of_his_own_reaction(self):
        d = Delivery(Never())
        self.assertEqual(turn(d, "a", "[sigh] Hm. Fine."), ["Hm. Fine."])

    def test_asking_the_same_thing_twice_gets_a_sigh(self):
        d = Delivery(Never())
        d.start_turn("what is the meaning of life")
        self.assertEqual(d.opener(), "")
        d.start_turn("what is the meaning of life")
        self.assertEqual(d.opener(), "[sigh] ")


class ReactionTests(unittest.TestCase):
    def test_reactions_before_explanations_and_between_long_sentences(self):
        d = Delivery(Always())
        said = turn(d, "explain it", "A vaccine trains you.", "Short one.", LONG, explain=True)
        self.assertIn(said[0].removesuffix(" A vaccine trains you."), OPENERS)
        self.assertEqual(said[1], "Short one.")
        self.assertIn(said[2].removesuffix(f" {LONG}"), BETWEEN)

    def test_never_in_the_middle_of_a_sentence(self):
        d = Delivery(Always())
        said = turn(d, "explain", "The sky looks blue because sunlight passes through the air",
                    "and blue light scatters far more than any of the other colours in it.", explain=True)
        self.assertEqual(said[1], "and blue light scatters far more than any of the other colours in it.")

    def test_short_replies_and_his_own_reactions_are_left_alone(self):
        d = Delivery(Always())
        self.assertEqual(turn(d, "hi", "Hello there."), ["Hello there."])
        self.assertEqual(turn(d, "explain", "Well, it's simple.", explain=True), ["Well, it's simple."])

    def test_never_more_than_a_couple_or_the_same_twice_running(self):
        d = Delivery(Always())
        said = turn(d, "explain", "First.", "Two.", LONG, "Three.", "Four.", LONG, "Five.", "Six.", LONG,
                    explain=True)
        reactions = [r for s in said for r in set(OPENERS + BETWEEN) if s.startswith(r + " ")]
        self.assertEqual(len(reactions), MAX_REACTIONS)
        self.assertNotEqual(reactions[0], reactions[1])


if __name__ == "__main__":
    unittest.main()
