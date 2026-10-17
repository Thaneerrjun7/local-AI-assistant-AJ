"""Reminders and timers: spoken times, the store, cancelling, voice commands and the Task
Scheduler job. A temporary store and a fake schtasks, so nothing reaches the real scheduler."""
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

from ultron.tools import intents, reminders
from ultron.tools.registry import ToolError

NOW = datetime(2026, 10, 6, 12, 0)  # a Tuesday, noon


class ParseWhenTests(unittest.TestCase):
    def due(self, text):
        return reminders.parse_when(text, NOW).due

    def test_clock_times(self):
        self.assertEqual(self.due("at 6pm"), datetime(2026, 10, 6, 18, 0))
        self.assertEqual(self.due("at 18:30"), datetime(2026, 10, 6, 18, 30))
        self.assertEqual(self.due("at 9am"), datetime(2026, 10, 7, 9, 0))  # passed today, so tomorrow

    def test_relative_and_named_days(self):
        self.assertEqual(self.due("in 20 minutes"), datetime(2026, 10, 6, 12, 20))
        self.assertEqual(self.due("in 2 hours"), datetime(2026, 10, 6, 14, 0))
        self.assertEqual(self.due("tomorrow at 9am"), datetime(2026, 10, 7, 9, 0))
        self.assertEqual(self.due("on friday at 3pm"), datetime(2026, 10, 9, 15, 0))

    def test_repeating(self):
        plan = reminders.parse_when("every weekday at 7am", NOW)
        self.assertEqual(tuple(plan.days), reminders.WEEKDAYS)
        self.assertEqual(plan.due, datetime(2026, 10, 7, 7, 0))  # 7am today has gone

    def test_refuses_past_incomplete_and_nonsense(self):
        with self.assertRaises(ToolError):
            reminders.parse_when("today at 9am", NOW)  # already passed
        with self.assertRaises(ToolError):
            reminders.parse_when("tomorrow", NOW)  # no time
        with self.assertRaises(reminders.NotATime):
            reminders.parse_when("banana", NOW)


class StoreAndToolTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.calls: list[list[str]] = []
        self.xml: list[str] = []

        def fake_schtasks(args):
            self.calls.append(args)
            if args[0] == "/Create":
                self.xml.append(Path(args[args.index("/XML") + 1]).read_text(encoding="utf-16"))
            return 0, ""

        for target, value in (("STORE_PATH", Path(tmp.name) / "reminders.json"), ("schtasks", fake_schtasks),
                              ("_now", lambda: NOW)):
            patcher = mock.patch.object(reminders, target, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_create_list_cancel(self):
        said = reminders.create_reminder("call mom", "at 6pm")
        self.assertIn("call mom", said)
        self.assertEqual(self.calls[-1][0], "/Create")
        self.assertIn("<TimeTrigger>", self.xml[-1])
        self.assertIn("2026-10-06T18:00:00", self.xml[-1])
        self.assertIn("call mom", reminders.list_reminders())
        reminders.cancel_reminder("call mom")
        self.assertEqual(self.calls[-1][0], "/Delete")
        self.assertEqual(reminders.list_reminders(), "No reminders or timers are set.")

    def test_repeating_job_is_a_weekly_calendar_trigger(self):
        reminders.create_reminder("take medicine", "every weekday at 7am")
        self.assertIn("<CalendarTrigger>", self.xml[-1])
        self.assertIn("<Monday />", self.xml[-1])
        self.assertNotIn("<Saturday />", self.xml[-1])

    def test_timer(self):
        self.assertIn("10", reminders.start_timer(minutes=10))
        self.assertIn("timer", reminders.list_reminders())
        with self.assertRaises(ToolError):
            reminders.start_timer()

    def test_task_text_cannot_break_the_command(self):
        reminders.create_reminder('quote " and %PATH% & stuff', "in 5 minutes")
        self.assertNotIn("%PATH%", self.xml[-1].split("<Arguments>")[1].split("</Arguments>")[0])


class IntentTests(unittest.TestCase):
    def setUp(self):
        saved = intents._rules
        self.addCleanup(setattr, intents, "_rules", saved)
        intents.collect([reminders])

    def test_voice_commands(self):
        for said, tool in [("remind me to call mom at 6pm", "create_reminder"),
                           ("remind me in 20 minutes to check the oven", "create_reminder"),
                           ("set a timer for 10 minutes", "start_timer"),
                           ("what are my reminders", "list_reminders"),
                           ("cancel the dentist reminder", "cancel_reminder")]:
            with self.subTest(said=said):
                hit = intents.match(said)
                self.assertIsNotNone(hit)
                self.assertEqual(hit[0], tool)


if __name__ == "__main__":
    unittest.main()
