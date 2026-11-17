"""The live laptop status (core/laptop_state.py), the laptop_status tool, and how the brain uses
them. Every reading is faked: nothing here looks at the real laptop."""
import time
import unittest
from unittest import mock

from aj.config import Settings
from aj.core import brain as brain_mod
from aj.core import laptop_state as ls
from aj.core.laptop_state import Monitor, OpenApp, Snapshot
from aj.core.pipeline import TurnTimes
from aj.tools import apps, intents, status
from aj.tools.apps import App, Window

SNAP = Snapshot(taken=1.0, battery=80, plugged=True,
                apps=(OpenApp("Google Chrome", 2, front=True, titles=("YouTube", "Inbox")),
                      OpenApp("WhatsApp", minimised=True), OpenApp("Settings")),
                volume=40, muted=False, brightness=60, online=True, cpu=9, ram=71,
                disks=(("C:", 120.4, 475.0),))


class SummaryTests(unittest.TestCase):
    def test_one_line_with_everything(self):
        self.assertEqual(
            SNAP.summary(),
            'battery 80%, plugged in; in front: Google Chrome (2 windows: "YouTube", "Inbox"); '
            "also running: WhatsApp (minimised), Settings; volume 40%; brightness 60%; online; CPU 9%, RAM 71% used; "
            "free space C: 120 GB of 475 GB")

    def test_what_isnt_known_is_left_out(self):
        self.assertEqual(Snapshot(battery=12, plugged=False, muted=True, volume=30).summary(),
                         "battery 12%, on battery power; no apps running; sound muted")

    def test_browser_titles_lose_the_browser_name_and_stay_short(self):
        self.assertEqual(ls._short("Inbox - Google Chrome", "Google Chrome"), "Inbox")
        self.assertEqual(len(ls._short("x" * 200, "Google Chrome")), ls.TITLE_CHARS)

    def test_cpu_load_is_the_change_between_two_readings(self):
        self.assertEqual(ls.cpu_percent((100, 1000), (190, 1100)), 10)  # 90 of 100 ticks idle
        self.assertIsNone(ls.cpu_percent(None, (1, 2)))


class MonitorTests(unittest.TestCase):
    def fake_readings(self):
        return [mock.patch.object(ls, "_power", return_value={"battery": 80, "plugged": True, "saver": False}),
                mock.patch.object(ls, "open_apps", return_value=(OpenApp("Spotify", front=True),)),
                mock.patch.object(ls, "_sound", return_value={"volume": 30, "muted": False}),
                mock.patch.object(ls, "online", return_value=True),
                mock.patch.object(ls, "ram_percent", return_value=50),
                mock.patch.object(ls, "disks", return_value=(("C:", 10.0, 100.0),)),
                mock.patch.object(ls, "_brightness", return_value={"brightness": 70})]

    def test_refresh_reads_everything_and_a_failure_costs_one_reading(self):
        patches = self.fake_readings()
        for p in patches:
            p.start()
        self.addCleanup(mock.patch.stopall)
        with mock.patch.object(ls, "_sound", side_effect=OSError("no speakers")):
            m = Monitor()
            m.refresh(slow=True)
        snap = m.snapshot()
        self.assertEqual((snap.battery, snap.plugged, snap.brightness, snap.ram), (80, True, 70, 50))
        self.assertEqual(snap.apps[0].name, "Spotify")
        self.assertIsNone(snap.volume)  # only the reading that failed is missing

    def test_a_brightness_change_goes_in_at_once(self):
        m = Monitor()
        m.after_tool("set_brightness", "Brightness at 35%.")
        self.assertEqual(m.snapshot().brightness, 35)

    def test_the_thread_stops(self):
        patches = self.fake_readings()
        for p in patches:
            p.start()
        self.addCleanup(mock.patch.stopall)
        m = Monitor(quick_every=0.01)
        m.start()
        time.sleep(0.05)
        m.stop()
        m._thread.join(1)
        self.assertFalse(m._thread.is_alive())


class AppNameTests(unittest.TestCase):
    def setUp(self):
        self.addCleanup(apps._install, [], False)
        apps._install([App("Google Chrome", "shortcut", "chrome.lnk", ("chrome.exe",), "Chrome"),
                       App("WhatsApp", "appid", "5319275A.WhatsAppDesktop_x!App", ("whatsapp.exe",),
                           "5319275A.WhatsAppDesktop_x!App")], True)

    def test_windows_are_named_as_the_start_menu_names_them(self):
        self.assertEqual(apps.name_of(Window(1, "t", frozenset({"chrome.exe"}), frozenset({"chrome"}))), "Google Chrome")
        self.assertEqual(apps.name_of(Window(2, "t", frozenset({"applicationframehost.exe", "whatsapp.exe"}),
                                             frozenset({"5319275a.whatsappdesktop_x!app"}))), "WhatsApp")
        self.assertEqual(apps.name_of(Window(3, "t", frozenset({"explorer.exe"}), frozenset())), "File Explorer")
        self.assertEqual(apps.name_of(Window(4, "t", frozenset({"some_game.exe"}), frozenset())), "Some Game")


class StatusToolTests(unittest.TestCase):
    def test_the_tool_reads_the_monitor(self):
        monitor = mock.Mock()
        monitor.snapshot.return_value = SNAP
        with mock.patch.object(status, "monitor", monitor):
            self.assertEqual(status.laptop_status(), f"Laptop now: {SNAP.summary()}.")
        monitor.snapshot.assert_called_with(fresh=True)

    def test_instant_commands(self):
        for said in ["What apps are open?", "what apps do I have open right now", "what's open",
                     "how's my laptop doing", "laptop status"]:
            self.assertEqual(intents.match(said), ("laptop_status", {}), said)


class FakeState:
    def __init__(self, snap=SNAP):
        self.snap, self.after = snap, []

    def snapshot(self, fresh=False):
        return self.snap

    def after_tool(self, tool, result):
        self.after.append(tool)


def pieces(*items):
    return lambda *a, **k: iter([("text", i) for i in items])


class BrainUsesItTests(unittest.TestCase):
    def make(self, state):
        b = brain_mod.Brain("http://x", Settings(), state=state)
        b._tools = []
        return b

    def test_every_turn_starts_with_the_laptop_as_it_is(self):
        b = self.make(FakeState())
        with mock.patch("aj.tools.intents.match", return_value=None), \
                mock.patch.object(brain_mod, "stream_events", side_effect=pieces("Yes, Chrome's open.")) as model:
            said = b.reply("is chrome open?", None, TurnTimes(0.0))
        self.assertIn(f"[Laptop now: {SNAP.summary()}]", model.call_args.args[1][-1]["content"])
        self.assertEqual(said, "Yes, Chrome's open.")
        self.assertNotIn("Laptop now", b.history[-2]["content"])  # history keeps the user's words only

    def test_the_real_reading_passes_and_a_made_up_one_doesnt(self):
        b = self.make(FakeState())
        with mock.patch("aj.tools.intents.match", return_value=None), \
                mock.patch.object(brain_mod, "stream_events",
                                  side_effect=pieces("Your battery is at 80 percent and plugged in. ",
                                                     "It was at 17 percent an hour ago.")):
            said = b.reply("how's my battery?", None, TurnTimes(0.0))
        self.assertEqual(said, "Your battery is at 80 percent and plugged in.")

    def test_what_he_did_lately_is_in_the_next_message(self):
        b = self.make(FakeState())
        b.dispatcher = mock.Mock()
        b.dispatcher.run.return_value = brain_mod_result("Opening WhatsApp.")
        with mock.patch("aj.tools.intents.match", return_value=("open_app", {"name": "WhatsApp"})):
            b.reply("open whatsapp", None, TurnTimes(0.0))
        with mock.patch("aj.tools.intents.match", return_value=None), \
                mock.patch.object(brain_mod, "stream_events", side_effect=pieces("It's open.")) as model:
            b.reply("did it open?", None, TurnTimes(0.0))
        self.assertIn("[What you did lately:", model.call_args.args[1][-1]["content"])
        self.assertIn('open_app {"name": "WhatsApp"}: Opening WhatsApp.', model.call_args.args[1][-1]["content"])


def brain_mod_result(text):
    from aj.tools.dispatch import ToolResult
    return ToolResult(text, "ok")


if __name__ == "__main__":
    unittest.main()
