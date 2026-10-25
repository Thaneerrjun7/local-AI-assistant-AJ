"""The window itself, offscreen: Close (or the title-bar X) turns AJ off and the window
waits for the engine before it really closes; the mic button and the text box reach the
engine; engine reports cross from another thread into the window. Uses a fake engine and
Qt's offscreen platform, so nothing appears on screen and no model loads."""
import os
import threading
import time
import unittest

os.environ["QT_QPA_PLATFORM"] = "offscreen"  # before any QApplication exists: never show a real window

try:
    from PyQt6.QtCore import Qt
    from PyQt6.QtWidgets import QApplication
except ImportError:  # pragma: no cover - the window is optional for the console test modes
    QApplication = None

if QApplication is not None:
    from aj.core.pipeline import Status, TurnTimes
    from aj.ui.app import connect
    from aj.ui.bridge import Bridge
    from aj.ui.window import AJWindow


class FakeControls:
    def __init__(self):
        self.calls: list[tuple] = []

    def set_listening(self, on: bool) -> None:
        self.calls.append(("set_listening", on))

    def ask(self, text: str) -> None:
        self.calls.append(("ask", text))

    def turn_off(self) -> None:
        self.calls.append(("turn_off",))


@unittest.skipIf(QApplication is None, "PyQt6 is not installed")
class WindowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.engine = FakeControls()
        self.window = AJWindow(self.engine)
        self.closing: list[bool] = []
        self.window.closing.connect(self.closing.append)
        self.window.show()

    def tearDown(self):
        self.window._engine_done = True
        self.window.close()
        self.window.deleteLater()
        self.app.processEvents()

    def test_says_everything_is_in_character(self):
        self.assertTrue(self.window.disclaimer.isVisible())
        self.assertIn("in character", self.window.disclaimer.text())

    def test_controls_are_off_while_loading(self):
        self.assertEqual(self.window.status, Status.LOADING)
        self.assertFalse(self.window.mic.isEnabled())
        self.assertFalse(self.window.entry.isEnabled())
        self.assertTrue(self.window.close_button.isEnabled())

    def test_typing_box_takes_focus_once_loaded_and_close_never_does(self):
        self.window.set_status(Status.LISTENING)
        self.assertIs(self.window.focusWidget(), self.window.entry)
        self.assertEqual(self.window.close_button.focusPolicy(), Qt.FocusPolicy.NoFocus)

    def test_mic_defaults_on_and_a_click_turns_it_off(self):
        self.window.set_status(Status.LISTENING)
        self.assertTrue(self.window.mic.isChecked())
        self.window.mic.click()
        self.assertEqual(self.engine.calls, [("set_listening", False)])
        self.assertIn("Mic off", self.window.mic_caption.text())
        self.window.mic.click()
        self.assertEqual(self.engine.calls[-1], ("set_listening", True))

    def test_close_turns_aj_off_and_waits_for_the_engine(self):
        self.window.set_status(Status.LISTENING)
        self.window.close_button.click()
        self.assertEqual(self.engine.calls, [("turn_off",)])
        self.assertEqual(self.closing, [False])
        self.assertTrue(self.window.isVisible())  # still up, saying "Turning off..."
        self.assertEqual(self.window.status, Status.TURNING_OFF)
        self.assertFalse(self.window.mic.isEnabled())
        self.assertFalse(self.window.close_button.isEnabled())

        self.window.close()  # the title-bar X again: no second turn_off
        self.assertEqual(self.engine.calls, [("turn_off",)])

        self.window.engine_finished()
        self.assertFalse(self.window.isVisible())

    def test_closing_while_loading_says_so(self):
        self.window.close()
        self.assertEqual(self.closing, [True])  # app.py exits at once: nothing to say goodbye with
        self.assertEqual(self.engine.calls, [("turn_off",)])

    def test_spoken_turn_off_closes_the_window_once_the_engine_ends(self):
        self.window.set_status(Status.LISTENING)
        self.window.set_status(Status.TURNING_OFF)  # "Computer, turn off": the engine stops itself
        self.assertEqual(self.closing, [False])
        self.window.set_status(Status.LISTENING)  # a late report can't revive the controls
        self.assertEqual(self.window.status, Status.TURNING_OFF)
        self.window.engine_finished()
        self.assertFalse(self.window.isVisible())

    def test_an_engine_error_stays_on_screen_until_closed(self):
        self.window.set_status(Status.LISTENING)
        self.window.show_error("RuntimeError: llama-server exited")
        self.window.engine_finished()
        self.assertTrue(self.window.isVisible())
        self.assertIn("llama-server exited", self.window.transcript.toPlainText())
        self.assertFalse(self.window.mic.isEnabled())
        self.window.close()
        self.assertFalse(self.window.isVisible())

    def test_enter_sends_typed_text(self):
        self.window.set_status(Status.LISTENING)
        self.window.entry.setText("  what time is it  ")
        self.window.entry.returnPressed.emit()
        self.window.entry.setText("   ")
        self.window.entry.returnPressed.emit()
        self.assertEqual(self.engine.calls, [("ask", "what time is it")])
        self.assertEqual(self.window.entry.text(), "   ")  # blank input is left alone, not sent

    def test_a_mic_that_wont_open_brings_the_window_back(self):
        """Asleep in the tray, nothing else would tell him the mic is dead (the review of 2026-10-22)."""
        self.window.set_status(Status.LISTENING)
        self.window.set_status(Status.ASLEEP)
        self.assertFalse(self.window.isVisible())
        self.window.set_status(Status.MIC_FAILED)
        self.assertTrue(self.window.isVisible())
        self.assertIn("Microphone", self.window.status_label.text())

    def test_transcript_shows_text_literally(self):
        self.window.add_heard("<b>open</b> notepad")
        self.window.add_turn("Opening it.", "sound 610 ms")
        text = self.window.transcript.toPlainText()
        self.assertIn("you: <b>open</b> notepad", text)
        self.assertIn("aj: Opening it.", text)
        self.assertIn("sound 610 ms", text)

    def test_engine_reports_cross_threads_through_the_bridge(self):
        bridge = Bridge()
        connect(bridge, self.window)

        def engine_thread() -> None:
            bridge.on_status(Status.THINKING)
            bridge.on_heard("hello")
            bridge.on_turn("hello", "Still here.", TurnTimes(stopped_talking=1.0, first_sound=1.5))

        thread = threading.Thread(target=engine_thread)
        thread.start()
        thread.join()
        deadline = time.perf_counter() + 2
        while "Still here." not in self.window.transcript.toPlainText() and time.perf_counter() < deadline:
            self.app.processEvents()
        self.assertEqual(self.window.status, Status.THINKING)
        self.assertIn("sound 500 ms", self.window.transcript.toPlainText())


if __name__ == "__main__":
    unittest.main()
