"""Lock, sleep, restart, shut down, sign out, cancel. Every Windows call is faked: running these
tests never locks, sleeps or shuts down the laptop."""
import subprocess
import unittest
from unittest import mock

from aj.tools import dispatch, power, registry
from aj.tools.registry import Level, ToolError


def finished(code):
    return subprocess.CompletedProcess([], code, "", "")


class PowerTests(unittest.TestCase):
    def setUp(self):
        self.run = mock.patch.object(power.subprocess, "run", return_value=finished(0)).start()
        self.timer = mock.patch.object(power.threading, "Timer").start()
        self.addCleanup(mock.patch.stopall)
        power._pending = None

    def test_lock_runs_at_once_and_the_rest_ask_first(self):
        self.assertEqual(registry.get("lock_screen").level, Level.SAFE)
        self.assertEqual(registry.get("cancel_shutdown").level, Level.SAFE)
        for name in ("sleep_laptop", "restart_laptop", "shutdown_laptop", "sign_out"):
            self.assertEqual(registry.get(name).level, Level.CONFIRM, name)

    def test_lock(self):
        user32 = mock.Mock()
        user32.LockWorkStation.return_value = 1
        with mock.patch.object(power.ctypes, "WinDLL", return_value=user32):
            self.assertEqual(power.lock_screen(), "Locked.")
        user32.LockWorkStation.return_value = 0
        with mock.patch.object(power.ctypes, "WinDLL", return_value=user32), self.assertRaises(ToolError):
            power.lock_screen()

    def test_shutdown_and_restart_count_down_through_windows(self):
        self.assertIn("30 seconds", power.shutdown_laptop())
        args = self.run.call_args.args[0]
        self.assertTrue(args[0].lower().endswith("shutdown.exe"))
        self.assertEqual(args[1:4], ["/s", "/t", "30"])
        self.assertNotIn("shell", self.run.call_args.kwargs)
        power.restart_laptop()
        self.assertEqual(self.run.call_args.args[0][1:4], ["/r", "/t", "30"])

    def test_one_already_counting_down_is_said_plainly(self):
        self.run.return_value = finished(power._ALREADY_PENDING)
        with self.assertRaisesRegex(ToolError, "already counting down"):
            power.shutdown_laptop()

    def test_sleep_and_sign_out_wait_a_moment_and_can_be_cancelled(self):
        power.sleep_laptop()
        self.timer.assert_called_once_with(power.SOON_SECS, power._suspend)
        self.timer.return_value.start.assert_called_once()
        self.timer.return_value.is_alive.return_value = True
        self.assertEqual(power.cancel_shutdown(), "Cancelled. The laptop stays on.")
        self.timer.return_value.cancel.assert_called_once()
        self.run.assert_not_called()  # nothing went to Windows

    def test_cancel_with_nothing_pending(self):
        self.run.return_value = finished(power._NOTHING_PENDING)
        with self.assertRaisesRegex(ToolError, "no shutdown"):
            power.cancel_shutdown()
        self.assertEqual(self.run.call_args.args[0][1:], ["/a"])

    def test_the_yes_no_question_is_worded_for_people(self):
        self.assertEqual(dispatch.confirm_question(registry.get("shutdown_laptop"), {}),
                         "Shut the laptop down in 30 seconds? Anything unsaved could be lost.")

    def test_a_no_means_nothing_happens(self):
        d = dispatch.Dispatcher(confirm=lambda q: False, audit_path=mock.MagicMock())
        with mock.patch.object(dispatch.Dispatcher, "_audit"):
            result = d.run("shutdown_laptop", {})
        self.assertEqual(result.outcome, "declined")
        self.run.assert_not_called()


class SleepIntentTests(unittest.TestCase):
    """The laptop's sleep and the assistant's sleep mode (core/wake.py, 2026-10-22) don't overlap:
    naming the laptop (or "the computer") is the laptop, with a yes; the assistant's own name
    said to it is sleep mode, which the conversation loop takes before any tool sees it."""

    def setUp(self):
        from aj.tools import intents
        intents.collect([power])
        self.addCleanup(intents.collect)
        self.match = intents.match

    def test_naming_the_laptop_puts_the_laptop_to_sleep(self):
        for said in ("Put the laptop to sleep.", "Sleep the laptop", "Put the computer to sleep",
                     "Sleep the computer", "Put my PC to sleep", "AJ, put the laptop to sleep",
                     "Put the laptop in sleep mode", "Laptop, go to sleep", "Send the machine to sleep"):
            self.assertEqual(self.match(said), ("sleep_laptop", {}), said)

    def test_its_own_name_is_not_the_laptop(self):
        for said in ("Sleep, computer.", "Computer, go to sleep.", "Go to sleep.", "Sleep mode.", "AJ sleep"):
            self.assertIsNone(self.match(said), said)


if __name__ == "__main__":
    unittest.main()
