"""Delivery: no aside twice, humanity rationed, spoken reactions and sighs where a person would
make them."""
import random
import unittest

from aj.core.delivery import BETWEEN, HUMANITY_EVERY, MAX_REACTIONS, OPENERS, SIGH_EVERY, SIGHS, Delivery


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
        said = turn(d, "hey computer, how are you doing?", "Still running, still monitoring your schedule,",
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


class SessionOfTheEighteenthTests(unittest.TestCase):
    """What he said in the user's session of 2026-10-18, none of it true."""

    def test_a_done_claim_needs_its_own_tool(self):
        d = Delivery(Never())
        d.start_turn("open whatsapp and send this message")
        d.did("open_app")  # WhatsApp really opened...
        self.assertEqual(d.shape("Opening WhatsApp."), "Opening WhatsApp.")
        self.assertIsNone(d.shape("Message sent."))  # ...but nothing sent anything
        for line in ("Screen is now locked.", "System is now powered down.", "Reminder set for 10:28 PM to eat lunch.",
                     "I opened WhatsApp and sent the message to contact APPA.", "Opening Google Chrome.",
                     "I will open WhatsApp and send the message.", "Wi-Fi is now active.", "Deactivated."):
            self.assertEqual(turn(d, "do it", "Right.", line), ["Right."], line)
        self.assertEqual(d.false_actions, 1)

    def test_states_refusals_and_offers_are_not_claims(self):
        d = Delivery(Never())
        for line in ("Chrome is already open.", "I can't send messages yet.", "I cannot lock the screen.",
                     "Want me to set a reminder?", "If you want, I can open it.", "WhatsApp didn't open in time.",
                     "I'm locked inside this laptop."):
            self.assertEqual(turn(d, "hm", line), [line], line)

    def test_the_word_battery_in_the_question_vouches_for_nothing(self):
        d = Delivery(Never())
        self.assertEqual(turn(d, "What is the battery status of the laptop?", "Battery level is 17 percent.",
                              "Laptop is unplugged.", "Want me to check?"), ["Want me to check?"])
        self.assertEqual(turn(d, "am I plugged in?", "Yes, you're plugged in."), [])

    def test_a_reading_must_match(self):
        d = Delivery(Never())
        d.start_turn("battery?")
        d.ground("Battery at 80%, plugged in.", fact=True)
        self.assertEqual(d.shape("The battery is at 80 percent and plugged in."),
                         "The battery is at 80 percent and plugged in.")
        self.assertIsNone(d.shape("Your battery is at 17 percent."))
        self.assertIsNone(d.shape("You're running on battery power."))

    def test_explanations_keep_their_numbers(self):
        d = Delivery(Never())
        said = turn(d, "explain how a laptop battery works", "A battery is usually charged to 100 percent when full.",
                    explain=True)
        self.assertEqual(len(said), 1)


class TypingAndChatsTests(unittest.TestCase):
    """2026-10-22: he can type and open a chat now, never send."""

    def test_typed_needs_the_typing_done(self):
        d = Delivery(Never())
        for line in ("Typed it in WhatsApp.", "Written down.", "I wrote it down.", "It's typed."):
            self.assertEqual(turn(d, "write this down", line), [], line)  # live: "Written down." with no tool
        d.start_turn("type I'll be late")
        d.did("type_text")
        self.assertEqual(d.shape("Typed it in WhatsApp."), "Typed it in WhatsApp.")
        d.start_turn("write this down")
        d.did("start_dictation")
        self.assertIsNone(d.shape("Typed it."))  # listening isn't typing
        self.assertEqual(d.shape("I'll type what you say."), "I'll type what you say.")

    def test_nothing_is_ever_sent(self):
        d = Delivery(Never())
        d.start_turn("message appa")
        d.did("whatsapp_open_chat")
        d.did("type_text")
        for line in ("Message sent.", "Sent it to Appa.", "I pressed send.", "I'll press send for you.",
                     "I'll message Appa."):
            self.assertIsNone(d.shape(line), line)

    def test_press_send_is_an_instruction(self):
        d = Delivery(Never())
        self.assertEqual(turn(d, "type it", "Press send when you're ready.", "Just hit send."),
                         ["Press send when you're ready.", "Just hit send."])

    def test_an_opened_chat(self):
        """Since the review of 2026-10-22 "Appa's chat is open" is a claim, not a state: only
        whatsapp_open_chat can make it true. Live, switch_to_app was taken for it."""
        d = Delivery(Never())
        for line in ("Appa's chat is open.", "Opened Appa's chat.", "You're now in Appa's chat.",
                     "Your chat with Suhinish is open.", "I've opened your conversation with Amma."):
            self.assertEqual(turn(d, "go to appa", line), [], line)
            d.start_turn("I want to talk to suhinish on whatsapp")
            d.did("switch_to_app")  # WhatsApp in front is not a chat open
            self.assertIsNone(d.shape(line), line)
            d.start_turn("go to appa")
            d.did("whatsapp_open_chat")
            self.assertEqual(d.shape(line), line)
        self.assertEqual(turn(d, "is whatsapp open", "WhatsApp is open."), ["WhatsApp is open."])  # still a state

    def test_stock_offers_of_help_are_dropped_after_the_answer(self):
        """About 7 replies in 18 ended this way in the acceptance run (2026-10-22), against the prompt."""
        d = Delivery(Never())
        for offer in ("How can I assist you further?", "How can I help?", "Is there anything else I can help you with?",
                      "Let me know if you need anything else.", "Anything else?", "What else can I do for you?",
                      "So, how may I assist you today?", "Feel free to ask if you have more questions."):
            self.assertEqual(turn(d, "what's the capital of japan", "Tokyo.", offer), ["Tokyo."], offer)

    def test_real_follow_up_questions_stay(self):
        d = Delivery(Never())
        for question in ("Want a reminder?", "Which subject?", "Should I open it?", "Do you want the forecast for tomorrow?",
                         "Which one did you mean?"):
            self.assertEqual(turn(d, "i have an exam", "Understood.", question), ["Understood.", question], question)
        self.assertEqual(turn(d, "hi", "How can I help?"), ["How can I help?"])  # the whole reply stays

    def test_no_last_time_unless_told(self):
        """It invented "No issues with the system, just like last time." (2026-10-22)."""
        d = Delivery(Never())
        for line in ("No issues with the system, just like last time.", "Same as last time.",
                     "Your plan looks fine, as usual.", "Like before, it went well."):
            self.assertEqual(turn(d, "how's the system", "Fine.", line), ["Fine."], line)
        d.start_turn("how was it last time")  # the boss brings it up
        self.assertEqual(d.shape("Same as last time."), "Same as last time.")
        d.start_turn("what did we talk about")
        d.ground("Possibly relevant from earlier:\n- user: my exam is on Friday", fact=True)  # memory says so
        self.assertEqual(d.shape("Like last time, your exam came up."), "Like last time, your exam came up.")

    def test_type_and_write_down_as_words_claim_nothing(self):
        d = Delivery(Never())
        self.assertEqual(turn(d, "what diabetes do I have", "That's type 2.", "Write it down."),
                         ["That's type 2.", "Write it down."])

    def test_a_joke_label_is_left_out(self):
        d = Delivery(Never())
        self.assertEqual(turn(d, "tell me a joke", "A joke about printers: They jam on purpose."),
                         ["They jam on purpose."])
        self.assertEqual(turn(d, "another", "Here's one: Fine."), ["Fine."])
        self.assertEqual(turn(d, "hm", "Very very very hot."), ["Very very very hot."])  # no flourish rule now


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
        if len(SIGHS) > 1:
            self.assertNotEqual(again, first)  # not the same sigh twice running, when there's a choice

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
        self.assertEqual(said[1:], ["Short one.", LONG])  # one added reaction per reply at most
        said = turn(Delivery(Always()), "tell me", "A vaccine trains you.", "Short one.", LONG)
        self.assertEqual(said[:2], ["A vaccine trains you.", "Short one."])
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
        self.assertEqual(len(set(reactions)), len(reactions))  # never the same one twice


if __name__ == "__main__":
    unittest.main()
