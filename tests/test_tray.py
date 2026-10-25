"""The tray icon (ui/tray.py), offscreen: its menu, its tooltip and its icon follow the state, and
clicks come out as requests. Qt's offscreen platform has no notification area, so nothing appears
on screen; the menu and the icon object work all the same."""
import os
import unittest

os.environ["QT_QPA_PLATFORM"] = "offscreen"  # before any QApplication exists: never show a real window

try:
    from PyQt6.QtWidgets import QApplication, QSystemTrayIcon
except ImportError:  # pragma: no cover - the window is optional for the console test modes
    QApplication = None

if QApplication is not None:
    from PyQt6.QtCore import Qt

    from aj.core.pipeline import Status
    from aj.ui import tray
    from aj.ui.app import connect
    from aj.ui.bridge import Bridge
    from aj.ui.tray import Tray, state_for, tooltip, tray_icon, wake_hint
    from aj.ui.window import AJWindow


@unittest.skipIf(QApplication is None, "PyQt6 is not installed")
class TrayTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.tray = Tray(["AJ", "Computer"])
        self.got: list[str] = []
        for name in ("wake_requested", "sleep_requested", "off_requested", "show_requested"):
            getattr(self.tray, name).connect(lambda name=name: self.got.append(name))

    def tearDown(self):
        self.tray.deleteLater()
        self.app.processEvents()

    def menu(self) -> dict[str, bool]:
        return {a.text(): a.isEnabled() for a in self.tray.menu.actions() if a.text()}

    def test_the_menu_has_wake_sleep_and_turn_off(self):
        self.assertEqual(list(self.menu()), ["Wake up", "Sleep", "Turn off"])
        self.assertIs(self.tray.icon.contextMenu(), self.tray.menu)

    def test_while_loading_only_turn_off_works(self):
        self.assertEqual(self.tray.state, tray.LOADING)
        self.assertEqual(self.menu(), {"Wake up": False, "Sleep": False, "Turn off": True})
        self.assertIn("starting", self.tray.tooltip())

    def test_awake(self):
        self.tray.set_state(tray.AWAKE)
        self.assertEqual(self.menu(), {"Wake up": False, "Sleep": True, "Turn off": True})
        self.assertEqual(self.tray.tooltip(), "AJ: awake")

    def test_asleep_says_how_to_wake_it(self):
        self.tray.set_state(tray.ASLEEP)
        self.assertEqual(self.menu(), {"Wake up": True, "Sleep": False, "Turn off": True})
        self.assertEqual(self.tray.tooltip(), 'AJ: asleep. Say "Hey AJ" or "Computer" to wake it.')

    def test_asleep_on_battery_says_it_isnt_listening(self):
        self.tray.set_state(tray.ASLEEP_ON_BATTERY)
        self.assertEqual(self.menu(), {"Wake up": True, "Sleep": False, "Turn off": True})
        self.assertIn("not listening on battery", self.tray.tooltip())

    def test_turning_off_leaves_nothing_to_click(self):
        self.tray.set_state(tray.TURNING_OFF)
        self.assertEqual(self.menu(), {"Wake up": False, "Sleep": False, "Turn off": False})

    def test_menu_clicks_become_requests(self):
        self.tray.set_state(tray.AWAKE)
        self.tray.sleep_action.trigger()
        self.tray.set_state(tray.ASLEEP)
        self.tray.wake_action.trigger()
        self.tray.off_action.trigger()
        self.assertEqual(self.got, ["sleep_requested", "wake_requested", "off_requested"])

    def test_a_click_on_the_icon_wakes_it_when_asleep_and_shows_the_window_when_awake(self):
        click, menu = QSystemTrayIcon.ActivationReason.Trigger, QSystemTrayIcon.ActivationReason.Context
        self.tray.set_state(tray.ASLEEP)
        self.tray.icon.activated.emit(click)
        self.tray.set_state(tray.ASLEEP_ON_BATTERY)
        self.tray.icon.activated.emit(click)
        self.tray.set_state(tray.AWAKE)
        self.tray.icon.activated.emit(click)
        self.tray.icon.activated.emit(menu)  # a right click only opens the menu
        self.tray.set_state(tray.TURNING_OFF)
        self.tray.icon.activated.emit(click)
        self.assertEqual(self.got, ["wake_requested", "wake_requested", "show_requested"])

    def test_each_state_has_its_own_icon(self):
        images = {state: tray_icon(state).pixmap(32, 32).toImage() for state in tray.STATES}
        for a in tray.STATES:
            self.assertFalse(images[a].isNull(), a)
            for b in tray.STATES:
                if a < b:
                    self.assertNotEqual(images[a], images[b], (a, b))
        self.tray.set_state(tray.ASLEEP)
        self.assertEqual(self.tray.icon.icon().pixmap(32, 32).toImage(), images[tray.ASLEEP])

    def test_tooltips_fit_windows_limit(self):
        names = ["A rather long assistant name", "Computer", "Jarvis"]
        for state in tray.STATES:
            self.assertLessEqual(len(tooltip(state, names)), 127, state)

    def test_wake_hint_wording(self):
        self.assertEqual(wake_hint(["AJ"]), 'Say "Hey AJ"')
        self.assertEqual(wake_hint(["AJ", "Computer", "Jarvis"]), 'Say "Hey AJ", "Computer" or "Jarvis"')

    def test_an_unknown_state_is_a_bug(self):
        with self.assertRaises(ValueError):
            self.tray.set_state("dreaming")

    def test_status_to_tray_state(self):
        self.assertEqual(state_for(Status.LOADING), tray.LOADING)
        self.assertEqual(state_for(Status.ASLEEP), tray.ASLEEP)
        self.assertEqual(state_for(Status.ASLEEP_ON_BATTERY), tray.ASLEEP_ON_BATTERY)
        self.assertEqual(state_for(Status.ASLEEP_MIC_OFF), tray.ASLEEP_MIC_OFF)
        self.assertEqual(state_for(Status.TURNING_OFF), tray.TURNING_OFF)
        for status in (Status.LISTENING, Status.HEARING, Status.THINKING, Status.SPEAKING, Status.MIC_OFF,
                       Status.DICTATING):
            self.assertEqual(state_for(status), tray.AWAKE, status)


class FakeEngine:
    def __init__(self):
        self.calls: list[tuple] = []

    def set_listening(self, on: bool) -> None:
        self.calls.append(("set_listening", on))

    def ask(self, text: str) -> None:
        self.calls.append(("ask", text))

    def turn_off(self) -> None:
        self.calls.append(("turn_off",))

    def sleep(self) -> None:
        self.calls.append(("sleep",))

    def wake(self) -> None:
        self.calls.append(("wake",))


@unittest.skipIf(QApplication is None, "PyQt6 is not installed")
class SleepingWindowTests(unittest.TestCase):
    """The window and the tray together, as ui/app.py connects them."""

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.engine = FakeEngine()
        self.window = AJWindow(self.engine)
        self.bridge, self.tray = Bridge(), Tray(["AJ", "Computer"])
        connect(self.bridge, self.window, self.tray)
        self.window.show()
        self.status(Status.LISTENING)

    def tearDown(self):
        self.window._engine_done = True
        self.window.close()
        self.window.deleteLater()
        self.tray.deleteLater()
        self.app.processEvents()

    def status(self, status: Status) -> None:
        self.bridge.on_status(status)
        self.app.processEvents()

    def test_asleep_the_window_goes_to_the_tray_and_comes_back_on_waking(self):
        self.status(Status.ASLEEP)
        self.assertFalse(self.window.isVisible())
        self.assertEqual(self.tray.state, tray.ASLEEP)
        self.status(Status.SPEAKING)  # "Hey AJ": the wake line
        self.assertTrue(self.window.isVisible())
        self.assertEqual(self.tray.state, tray.AWAKE)

    def test_woken_by_voice_it_comes_back_without_taking_the_keyboard(self):
        self.status(Status.ASLEEP)
        shown = []
        original = self.window.show

        def show():
            shown.append(self.window.testAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating))
            original()

        self.window.show = show
        self.status(Status.THINKING)
        self.assertEqual(shown, [True])
        self.assertFalse(self.window.testAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating))

    def test_battery_and_mic_off_are_asleep_too(self):
        self.status(Status.ASLEEP_ON_BATTERY)
        self.assertFalse(self.window.isVisible())
        self.assertIn("battery", self.tray.tooltip())
        self.status(Status.ASLEEP)  # plugged in again: still asleep, still hidden
        self.assertFalse(self.window.isVisible())

    def test_tray_menu_reaches_the_engine(self):
        self.tray.sleep_action.trigger()
        self.assertEqual(self.engine.calls, [("sleep",)])
        self.status(Status.ASLEEP)
        self.tray.wake_action.trigger()
        self.assertEqual(self.engine.calls, [("sleep",), ("wake",)])
        self.assertTrue(self.window.isVisible())  # asked for from the tray: shown at once

    def test_tray_turn_off_works_with_the_window_hidden(self):
        self.status(Status.ASLEEP)
        self.tray.off_action.trigger()
        self.assertEqual(self.engine.calls, [("turn_off",)])
        self.assertEqual(self.tray.state, tray.TURNING_OFF)
        closed = []
        self.window.closed.connect(lambda: closed.append(True))
        self.window.engine_finished()
        self.assertEqual(closed, [True])  # app.py quits on this

    def test_the_shortcut_wakes_it_when_asleep(self):
        self.status(Status.ASLEEP)
        self.bridge.show_requested.emit()
        self.app.processEvents()
        self.assertEqual(self.engine.calls, [("wake",)])
        self.assertTrue(self.window.isVisible())

    def test_the_shortcut_while_awake_just_shows_it(self):
        self.bridge.show_requested.emit()
        self.app.processEvents()
        self.assertEqual(self.engine.calls, [])

    def test_no_sleep_once_turning_off(self):
        self.window.close()
        self.window.go_to_sleep()
        self.assertEqual(self.engine.calls, [("turn_off",)])


if __name__ == "__main__":
    unittest.main()
