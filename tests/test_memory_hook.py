"""Memory learning waits until Ultron is idle, so it never competes with a reply for the GPU."""
import time
import unittest
from unittest import mock

from ultron import engine


class MemoryHookTests(unittest.TestCase):
    def setUp(self):
        self._idle = engine.LEARN_WHEN_IDLE_SECS
        engine.LEARN_WHEN_IDLE_SECS = 0.15
        patcher = mock.patch.multiple(engine.memory, log_turn=mock.DEFAULT, learn_in_background=mock.DEFAULT,
                                      context_for=mock.DEFAULT)
        self.mocks = patcher.start()
        self.mocks["context_for"].return_value = ""
        self.addCleanup(patcher.stop)

    def tearDown(self):
        engine.LEARN_WHEN_IDLE_SECS = self._idle

    def test_learns_after_idle(self):
        hook = engine.MemoryHook("http://x")
        hook.after_turn("my name is Alex", "Noted.")
        self.mocks["learn_in_background"].assert_not_called()  # not straight away
        time.sleep(0.4)
        self.mocks["learn_in_background"].assert_called_once()
        self.assertEqual(len(self.mocks["learn_in_background"].call_args.args[1]), 2)

    def test_a_new_turn_holds_learning_and_batches_the_turns(self):
        hook = engine.MemoryHook("http://x")
        hook.after_turn("I live in Kuala Lumpur", "Noted.")
        hook.context_for("what's the weather")  # the next turn starts before the idle wait ends
        time.sleep(0.3)
        self.mocks["learn_in_background"].assert_not_called()
        hook.after_turn("what's the weather", "Hot.")
        time.sleep(0.4)
        self.mocks["learn_in_background"].assert_called_once()
        self.assertEqual(len(self.mocks["learn_in_background"].call_args.args[1]), 4)  # both turns


if __name__ == "__main__":
    unittest.main()
