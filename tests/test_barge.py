"""Interrupting AJ through the speakers (core/barge.py) and what happens to the cut-off reply."""
import threading
import unittest
from unittest import mock

import numpy as np

from aj.config import Settings
from aj.core import barge
from aj.core.audio import MIC_BLOCK, Player
from aj.core.barge import BargeIn, is_echo
from aj.core.brain import Brain
from aj.core.pipeline import Session, TurnTimes

SAID = "The weather today is clear, with a high of thirty one degrees and no rain expected."


class EchoTests(unittest.TestCase):
    def test_his_own_words_coming_back_are_echo(self):
        self.assertTrue(is_echo("today is clear with a high of thirty one", SAID))
        self.assertTrue(is_echo("", SAID))  # a murmur: nothing to act on
        self.assertTrue(is_echo("uh", SAID))

    def test_different_words_are_the_boss(self):
        self.assertFalse(is_echo("actually tell me about tomorrow instead", SAID))
        self.assertFalse(is_echo("open spotify", SAID))

    def test_stop_words_always_stop_him(self):
        for said in ("stop", "Wait.", "hold on a second", "Computer, stop", "shut up", "okay stop"):
            self.assertFalse(is_echo(said, f"{said} {SAID}"), said)


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


class BargeInTests(unittest.TestCase):
    def make(self, heard):
        self.clock = Clock()
        self.transcribe = mock.Mock(return_value=heard)
        return BargeIn(self.transcribe, lambda: SAID, clock=self.clock)

    def feed(self, b, secs, hearing=True):
        """Feed `secs` of 32 ms blocks; returns the first True, if any."""
        hit = False
        for _ in range(int(secs / 0.032)):
            self.clock.now += 0.032
            hit = b.feed(np.zeros(MIC_BLOCK, np.float32), hearing) or hit
        return hit

    def test_the_boss_is_noticed_after_a_short_listen(self):
        b = self.make("tell me about tomorrow instead")
        self.assertFalse(self.feed(b, barge.FIRST_CHECK_SECS - 0.1))
        self.transcribe.assert_not_called()  # too soon to tell
        self.assertTrue(self.feed(b, 0.2))

    def test_echo_is_ignored_and_checked_again_now_and_then(self):
        b = self.make("clear with a high of thirty one degrees")
        self.assertFalse(self.feed(b, 3.0))
        self.assertLessEqual(self.transcribe.call_count, 1 + 3.0 / barge.RECHECK_SECS)
        self.assertGreaterEqual(self.transcribe.call_count, 2)

    def test_silence_costs_nothing(self):
        b = self.make("anything")
        self.assertFalse(self.feed(b, 3.0, hearing=False))
        self.transcribe.assert_not_called()

    def test_it_keeps_the_start_of_what_was_said(self):
        b = self.make("x")
        self.feed(b, 5.0, hearing=False)
        self.assertEqual(len(b.recent_audio()), int(barge.WINDOW_SECS * 16000 / MIC_BLOCK) * MIC_BLOCK)


class PlayerWordsTests(unittest.TestCase):
    def test_only_what_started_playing_counts_as_heard(self):
        p = Player(24000)
        p.new_turn()
        p.play(np.ones(100, np.float32), "First sentence.")
        p.play(np.ones(100, np.float32), "Second sentence.")
        p.play(np.ones(100, np.float32), "Never reached.")
        out = np.zeros((150, 1), np.float32)
        p._on_output(out, 150, None, None)  # all of the first, half of the second
        p.stop()
        self.assertEqual(p.heard(), "First sentence. Second sentence.")
        self.assertIn("Second sentence.", p.said_lately())
        p.new_turn()
        self.assertEqual(p.heard(), "")


class CutOffTests(unittest.TestCase):
    def test_history_keeps_only_what_the_boss_heard(self):
        b = Brain("http://x", Settings())
        b.history = [{"role": "user", "content": "what's the weather"},
                     {"role": "assistant", "content": SAID + " Bring an umbrella anyway."}]
        b.cut_off("The weather today is clear,")
        self.assertEqual(b.history[-1]["content"],
                         "The weather today is clear, [cut off: the user started talking here]")
        b.cut_off("")
        self.assertIn("before you said anything", b.history[-1]["content"])


class SessionCutInTests(unittest.TestCase):
    def make(self):
        ears, brain, mouth = mock.Mock(), mock.Mock(), mock.Mock()
        brain.cancel = threading.Event()
        mouth.player.heard.return_value = "The weather today is clear,"
        source = mock.Mock()
        source.blocks.empty.return_value = True
        s = Session(Settings(), ears, brain, mouth, source, threading.Event(), interrupt=True)
        return s, ears, brain, mouth

    def test_cut_in_stops_him_and_keeps_listening(self):
        s, ears, brain, mouth = self.make()
        s._barge._ring.append(np.ones(MIC_BLOCK, np.float32))
        s._cut_in()
        self.assertTrue(brain.cancel.is_set())
        mouth.silence.assert_called_once()
        ears.reset.assert_called_once()
        self.assertEqual(len(s._cut_start), MIC_BLOCK)  # the boss's first words are kept

    def test_a_cut_off_turn_keeps_the_mic_state_and_trims_the_reply(self):
        s, ears, brain, mouth = self.make()
        brain.reply.return_value = SAID
        s._cut = True
        s._answer("what's the weather", TurnTimes(0.0))
        brain.cut_off.assert_called_once_with("The weather today is clear,")
        ears.reset.assert_not_called()  # the boss is still talking

    def test_a_normal_turn_has_the_loop_reset_the_ears(self):
        s, ears, brain, mouth = self.make()
        brain.reply.return_value = SAID
        s._answer("what's the weather", TurnTimes(0.0))
        brain.cut_off.assert_not_called()
        ears.reset.assert_not_called()  # not from the answer's thread: that crashed AJ (2026-10-19)
        self.assertTrue(s._settle)
        s._settle_ears()  # what the loop thread does next
        ears.reset.assert_called_once()
        self.assertFalse(s._settle)

    def test_a_vad_error_costs_the_utterance_not_aj(self):
        s, ears, brain, mouth = self.make()
        ears.feed.side_effect = ValueError("vector too long")
        self.assertEqual(s._hear(np.zeros(MIC_BLOCK, np.float32)), [])
        ears.reset.assert_called_once()

    def test_off_without_speakers_check(self):
        ears, brain, mouth = mock.Mock(), mock.Mock(), mock.Mock()
        s = Session(Settings(), ears, brain, mouth, mock.Mock(), threading.Event(), interrupt=False)
        self.assertIsNone(s._barge)
        s = Session(Settings(), ears, brain, mouth, mock.Mock(), threading.Event(), barge_in=True, interrupt=True)
        self.assertIsNone(s._barge)  # headphones: any speech stops him, as before


class SpokenAnswerTests(unittest.TestCase):
    """A yes/no box can be answered out loud (live, 2026-10-19, it opened behind other windows)."""

    def test_yes_and_no(self):
        from aj.core.pipeline import yes_or_no
        for said, want in [("Yes.", True), ("yeah, do it", True), ("go ahead", True), ("No.", False),
                           ("no wait", False), ("cancel that", False), ("not now", False), ("what?", None)]:
            self.assertEqual(yes_or_no(said), want, said)

    def test_the_next_spoken_answer_settles_the_question(self):
        s, ears, brain, mouth = SessionCutInTests.make(self)
        got = []
        s.listen_for_answer(got.append)
        ears.transcribe.side_effect = ["um, what was that", "yes"]
        s._answer_by_voice([np.zeros(10, np.float32), np.zeros(10, np.float32)])
        self.assertEqual(got, [True])
        self.assertIsNone(s._answer_to)

    def test_a_late_click_on_an_old_box_is_ignored(self):
        from aj.ui.bridge import Bridge
        b = Bridge()
        b._asking = "Shut the laptop down?"
        b.answer_confirm(True, "Delete notes.txt?")
        self.assertFalse(b._answered.is_set())
        b.answer_confirm(True, "Shut the laptop down?")
        self.assertTrue(b._answer)

if __name__ == "__main__":
    unittest.main()
