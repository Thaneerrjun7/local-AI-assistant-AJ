"""Sleep mode and the wake words (core/wake.py): what wakes the assistant, what puts it to sleep,
and what ordinary talk in the room must never do. The transcripts marked "measured" are what
Parakeet actually wrote in scripts/check_wake_words.py (2026-10-22)."""
import unittest

from aj.core.wake import DEFAULT, Gate, Heard, Mode, WakeWords, is_sleep_command, split_wake


class WakeWordTests(unittest.TestCase):
    def assertWakes(self, said: str, command: str = "") -> None:
        self.assertEqual(split_wake(said), (True, command), said)

    def assertStaysAsleep(self, said: str) -> None:
        self.assertEqual(split_wake(said), (False, said.strip()), said)

    def test_each_name_alone_wakes_it(self):
        for said in ("Hey AJ.", "AJ.", "AJ", "Hey computer.", "OK computer.", "Okay, computer.", "Computer.",
                     "Hi AJ!", "Hello computer", "Uh, hey AJ?"):
            self.assertWakes(said)

    def test_the_command_in_the_same_breath_is_kept(self):
        self.assertWakes("Hey AJ, open WhatsApp.", "open WhatsApp.")
        self.assertWakes("AJ open WhatsApp", "open WhatsApp")
        self.assertWakes("Computer, what time is it?", "what time is it?")
        self.assertWakes("Okay, AJ, what time is it?", "what time is it?")
        self.assertWakes("Hey computer how are you", "how are you")
        self.assertWakes("AJ can you hear me", "can you hear me")
        self.assertWakes("Computer, is it raining?", "is it raining?")

    def test_dictation_after_the_name_goes_through_as_said(self):
        """"Hey AJ, type" wakes it, and "type" reaches the dictation intents as the command."""
        self.assertWakes("Hey AJ, type.", "type.")
        self.assertWakes("Hey computer, write this down.", "write this down.")
        self.assertWakes("AJ, speak.", "speak.")

    def test_measured_spellings_of_aj(self):
        self.assertWakes("A AJ.")                                  # "hey" written "a"
        self.assertWakes("A A J open WhatsApp.", "open WhatsApp.")
        self.assertWakes("Hey, Jay.")
        self.assertWakes("Hey J.")
        self.assertWakes("Hey hey Jay.")
        self.assertWakes("Hey yeah Jay.")
        self.assertWakes("A Jay.")
        self.assertWakes("Hey A Jay.")
        self.assertWakes("Aj.")
        self.assertWakes("A.J., open WhatsApp", "open WhatsApp")
        self.assertWakes("A. J. open Spotify", "open Spotify")
        self.assertWakes("A OJ, open WhatsApp.", "open WhatsApp.")
        self.assertWakes("OJ turn off.", "turn off.")
        self.assertWakes("AAJ, Volume 30", "Volume 30")
        self.assertWakes("A A G type.", "type.")
        self.assertWakes("I J Sleep.", "Sleep.")
        self.assertWakes("Edge go to sleep.", "go to sleep.")
        self.assertWakes("Edge turn off.", "turn off.")
        self.assertWakes("A computer.")
        self.assertWakes("K computer")
        self.assertWakes("Computer open, what's that?", "open, what's that?")

    def test_a_name_that_doesnt_start_the_utterance_never_wakes_it(self):
        for said in ("The computer is slow today.", "I'm 18 years of age.", "Open WhatsApp, computer.",
                     "I think AJ is late.", "Can you fix my computer?", "Yeah, the computer froze again."):
            self.assertStaysAsleep(said)

    def test_the_name_followed_by_ordinary_talk_doesnt_wake_it(self):
        for said in ("Computer is slow today.", "Computer science is hard.", "Computer's acting up again.",
                     "AJ is coming over later.", "AJ's car is outside.", "Computers are getting cheaper.",
                     "Computer can do it faster.",            # measured: Parakeet dropped the "A"
                     "computer, the phone and a tablet.",     # measured, likewise
                     "A computer can do it faster.", "A computer, a phone and a tablet.",
                     "Hey, a computer would be nice.", "Okay, computer stuff later.",
                     "A jay flew past the window.", "OJ is my favourite drink.", "A J shaped curve.",
                     "Edge is slow.", "Edge.", "Edge, is it open?", "Hey Edge."):  # "Edge" only before a command
            self.assertStaysAsleep(said)

    def test_names_that_sound_close_are_not_aj(self):
        for said in ("Ajay said he would come later.", "Ajay, come here.", "Aja said hi.", "Age is just a number.",
                     "Ajax won the match.", "Jay, can you pass the salt?", "J Cole is my favourite rapper.",
                     "As I speak.", "I just sleep.", "Aye.", "A.", "Hey.", "Hey, how are you?", "Okay, let's go.",
                     "Hey Siri, set a timer.", "OK Google, what's the weather?", "Peter open WhatsApp.",
                     "Compute the total for me.", "Commuter trains are late again.", ""):
            self.assertStaysAsleep(said)

    def test_a_friend_named_jay_is_the_accepted_trade_off(self):
        """Parakeet writes "Hey AJ" as "Hey Jay" about one time in five, so this wakes it too."""
        self.assertWakes("Hey Jay, how are you?", "how are you?")

    def test_other_names_from_the_settings(self):
        words = WakeWords(["Jarvis", "AJ"])
        self.assertEqual(split_wake("Jarvis, open Spotify", words), (True, "open Spotify"))
        self.assertEqual(split_wake("Hey AJ", words), (True, ""))
        self.assertEqual(split_wake("Computer, open Spotify", words), (False, "Computer, open Spotify"))
        self.assertEqual(split_wake("Jarvis is a film character", words)[0], False)

    def test_curly_apostrophes_and_odd_spacing(self):
        self.assertWakes("  Hey AJ,   what’s the time?  ", "what’s the time?")
        self.assertStaysAsleep("Computer’s slow.")


class SleepCommandTests(unittest.TestCase):
    def test_ways_to_say_go_to_sleep(self):
        for said in ("Go to sleep.", "Go back to sleep.", "Sleep mode.", "Activate sleep mode.", "Go to sleep mode.",
                     "Sleep now.", "AJ sleep", "AJ, go to sleep.", "Computer, go back to sleep.",
                     "Hey AJ, go back to sleep mode.", "Enter sleep mode.", "Switch to sleep mode.",
                     "You can go back to sleep now.", "Go to sleep, computer.", "Sleep, AJ.", "sleep computer",
                     "Put yourself to sleep.", "Back to sleep.", "Take a nap.", "Good night.", "Standby mode.",
                     "Please go to sleep.", "Can you go to sleep?", "Okay, go to sleep now.", "Turn on sleep mode.",
                     "Go to sleep mod.",      # measured: "Go to sleep mode." came back "mod" once
                     "And the sleep mode."):  # measured, for "Enter sleep mode."
            self.assertTrue(is_sleep_command(said), said)

    def test_the_laptop_going_to_sleep_is_power_pys(self):
        """Those need a yes in the window (tools/power.py sleep_laptop), so they must not land here."""
        for said in ("Put the laptop to sleep.", "Sleep the laptop.", "Put the computer to sleep.",
                     "Sleep the computer.", "Put my PC to sleep.", "AJ, put the laptop to sleep.",
                     "Put the laptop in sleep mode.", "Laptop, go to sleep.", "Can you put my computer to sleep?"):
            self.assertFalse(is_sleep_command(said), said)

    def test_talk_about_sleep_is_not_a_command(self):
        for said in ("I need to sleep early tonight.", "Did you get enough sleep?", "How do I go to sleep faster?",
                     "Don't go to sleep.", "I'm going to sleep.", "Remind me to sleep at eleven.",
                     "Go to sleep, it's late.", "What is sleep mode?", "Disable sleep mode.", "Turn off sleep mode.",
                     "Is the laptop in sleep mode?", "Sleep well.", ""):
            self.assertFalse(is_sleep_command(said), said)


class GateTests(unittest.TestCase):
    def setUp(self):
        self.now = 1000.0
        self.changes: list[Mode] = []
        self.gate = Gate(DEFAULT, on_change=self.changes.append, clock=lambda: self.now)

    def test_awake_it_answers_everything_with_the_name_taken_off(self):
        self.assertEqual(self.gate.route("What time is it?"), Heard(Heard.ANSWER, "What time is it?"))
        self.assertEqual(self.gate.route("AJ, open WhatsApp"), Heard(Heard.ANSWER, "open WhatsApp"))
        self.assertEqual(self.gate.route("Hey AJ."), Heard(Heard.WAKE))  # just the name: a short line
        self.assertEqual(self.changes, [])

    def test_asleep_it_ignores_everything_without_its_name(self):
        self.gate.sleep()
        for said in ("What time is it?", "The computer is slow.", "Go to sleep.", "Turn off.", "Ajay said hi."):
            self.assertEqual(self.gate.route(said), Heard(Heard.IGNORE), said)
        self.assertEqual(self.gate.mode, Mode.ASLEEP)

    def test_its_name_wakes_it_and_the_command_runs_in_the_same_breath(self):
        self.gate.sleep()
        self.assertEqual(self.gate.route("Hey AJ, open WhatsApp."), Heard(Heard.ANSWER, "open WhatsApp."))
        self.assertEqual(self.gate.mode, Mode.AWAKE)
        self.assertEqual(self.changes, [Mode.ASLEEP, Mode.AWAKE])

    def test_the_name_alone_wakes_it_to_listen(self):
        self.gate.sleep()
        self.assertEqual(self.gate.route("Computer."), Heard(Heard.WAKE))
        self.assertFalse(self.gate.asleep)

    def test_turn_off_after_the_name_is_passed_on_while_asleep(self):
        """The pipeline's is_off_command then turns it off: "AJ, turn off" works asleep too."""
        self.gate.sleep()
        self.assertEqual(self.gate.route("AJ, turn off."), Heard(Heard.ANSWER, "turn off."))

    def test_going_to_sleep_by_voice(self):
        self.assertEqual(self.gate.route("Go back to sleep."), Heard(Heard.SLEEP))
        self.assertTrue(self.gate.asleep)
        self.assertEqual(self.gate.route("AJ, go to sleep."), Heard(Heard.SLEEP))  # asleep already: stays so
        self.assertTrue(self.gate.asleep)
        self.assertEqual(self.changes, [Mode.ASLEEP])

    def test_the_laptop_sleep_command_is_answered_not_obeyed(self):
        self.assertEqual(self.gate.route("Put the laptop to sleep."), Heard(Heard.ANSWER, "Put the laptop to sleep."))
        self.assertFalse(self.gate.asleep)

    def test_wake_and_sleep_report_only_real_changes(self):
        self.assertFalse(self.gate.wake())
        self.assertTrue(self.gate.sleep())
        self.assertFalse(self.gate.sleep())
        self.assertTrue(self.gate.wake())
        self.assertEqual(self.changes, [Mode.ASLEEP, Mode.AWAKE])

    def test_idle_sleep_is_off_by_default(self):
        self.now += 10 ** 6
        self.assertFalse(self.gate.idle_due())

    def test_idle_sleep_after_the_set_minutes_without_an_answer(self):
        gate = Gate(DEFAULT, idle_mins=5, clock=lambda: self.now)
        self.now += 299
        self.assertFalse(gate.idle_due())
        gate.route("What time is it?")  # answered: the clock starts again
        self.now += 299
        self.assertFalse(gate.idle_due())
        self.now += 2
        self.assertTrue(gate.idle_due())
        gate.sleep()
        self.assertFalse(gate.idle_due())  # asleep already

    def test_ignored_talk_doesnt_keep_it_awake(self):
        gate = Gate(DEFAULT, idle_mins=1, asleep=True, clock=lambda: self.now)
        gate.route("Hey AJ")
        self.now += 61
        self.assertTrue(gate.idle_due())

    def test_listening_while_asleep_is_for_mains_power_only(self):
        self.assertTrue(Gate.hears_wake_words(True))
        self.assertFalse(Gate.hears_wake_words(False))
        self.assertFalse(Gate.hears_wake_words(None))  # Windows couldn't say: his rule holds


class WeakSpellingTests(unittest.TestCase):
    """The review of 2026-10-22: after "OJ", "Edge", "I J" and the like (other words too), a polite
    word or "what" is not a command: "OJ, please" at breakfast must not wake it."""

    def test_breakfast_stays_asleep(self):
        for said in ("OJ, please.", "OJ, thanks.", "OJ please", "OJ, good.", "OJ, what?", "Edge, please.",
                     "I J, thanks.", "A OJ, please.", "OJ, what a mess."):
            with self.subTest(said=said):
                self.assertFalse(split_wake(said)[0])

    def test_real_commands_still_wake_it(self):
        for said, rest in (("OJ turn off.", "turn off."), ("OJ, please open WhatsApp.", "please open WhatsApp."),
                           ("Edge go to sleep.", "go to sleep."), ("I J sleep.", "sleep."),
                           ("AJ, please.", "please."), ("AJ, what time is it?", "what time is it?"),
                           ("Hey Jay, thanks.", "thanks."), ("Computer, thanks.", "thanks.")):
            with self.subTest(said=said):
                self.assertEqual(split_wake(said), (True, rest))


if __name__ == "__main__":
    unittest.main()
