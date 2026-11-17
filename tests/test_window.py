"""The window itself, offscreen: Close (or the title-bar X) turns AJ off and the window
waits for the engine before it really closes; the mic button and the text box reach the
engine; engine reports cross from another thread into the window. Uses a fake engine and
Qt's offscreen platform, so nothing appears on screen and no model loads.

The window is Qt Quick (qml/Main.qml); a click or a key is driven through the same QML
functions the buttons and the text box call, so these test the wiring from QML to Python too."""
import os
import threading
import time
import unittest

os.environ["QT_QPA_PLATFORM"] = "offscreen"  # before any QApplication exists: never show a real window

try:
    from PyQt6.QtCore import QObject
    from PyQt6.QtWidgets import QApplication
except ImportError:  # pragma: no cover - the window is optional for the console test modes
    QApplication = None

if QApplication is not None:
    from aj.core.pipeline import Status, TurnTimes
    from aj.ui import native
    from aj.ui.app import connect
    from aj.ui.bridge import Bridge
    from aj.ui.conversation import MAX_MESSAGES, Conversation
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
        self.app.processEvents()

    def tearDown(self):
        self.window._engine_done = True
        self.window.close()
        self.window.deleteLater()
        self.app.processEvents()

    def item(self, name: str) -> QObject:
        found = self.window.root.findChild(QObject, name)
        self.assertIsNotNone(found, name)
        return found

    def press(self, what: str, *args) -> None:
        native.call(self.window.root, what, *args)
        self.app.processEvents()

    def test_says_everything_is_in_character(self):
        disclaimer = self.item("disclaimer")
        self.assertTrue(disclaimer.property("visible"))
        self.assertIn("in character", disclaimer.property("text"))

    def test_controls_are_off_while_loading(self):
        self.assertEqual(self.window.status, Status.LOADING)
        self.assertFalse(self.item("micButton").property("enabled"))
        self.assertFalse(self.item("entry").property("enabled"))
        self.assertTrue(self.item("closeButton").property("enabled"))

    def test_typing_box_takes_focus_once_loaded_and_close_never_does(self):
        self.window.set_status(Status.LISTENING)
        self.app.processEvents()
        self.assertTrue(self.item("entry").property("focus"))
        self.assertFalse(self.item("closeButton").property("activeFocusOnTab"))

    def test_mic_defaults_on_and_a_click_turns_it_off(self):
        self.window.set_status(Status.LISTENING)
        self.assertTrue(self.window.root.property("micOn"))
        self.press("pressMic")
        self.assertEqual(self.engine.calls, [("set_listening", False)])
        self.assertIn("Mic off", self.window.root.property("micCaption"))
        self.press("pressMic")
        self.assertEqual(self.engine.calls[-1], ("set_listening", True))

    def test_the_mic_button_does_nothing_while_loading(self):
        self.press("pressMic")
        self.assertEqual(self.engine.calls, [])

    def test_close_turns_aj_off_and_waits_for_the_engine(self):
        self.window.set_status(Status.LISTENING)
        self.press("pressClose")
        self.assertEqual(self.engine.calls, [("turn_off",)])
        self.assertEqual(self.closing, [False])
        self.assertTrue(self.window.isVisible())  # still up, saying "Turning off..."
        self.assertEqual(self.window.status, Status.TURNING_OFF)
        self.assertFalse(self.item("micButton").property("enabled"))
        self.assertFalse(self.item("closeButton").property("enabled"))

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
        self.assertIn("llama-server exited", self.window.conversation.plain_text())
        self.assertFalse(self.item("micButton").property("enabled"))
        self.assertEqual(self.window.root.property("mode"), "error")  # the orb goes red
        self.assertTrue(self.window.root.property("statusTone"))
        self.window.close()
        self.assertFalse(self.window.isVisible())

    def test_enter_sends_typed_text(self):
        self.window.set_status(Status.LISTENING)
        self.press("type", "  what time is it  ")
        self.press("submit")
        self.press("type", "   ")
        self.press("submit")
        self.assertEqual(self.engine.calls, [("ask", "what time is it")])
        self.assertEqual(self.window.root.property("entryText"), "   ")  # blank input is left alone, not sent

    def test_a_mic_that_wont_open_brings_the_window_back(self):
        """Asleep in the tray, nothing else would tell him the mic is dead (the review of 2026-10-22)."""
        self.window.set_status(Status.LISTENING)
        self.window.set_status(Status.ASLEEP)
        self.assertFalse(self.window.isVisible())
        self.window.set_status(Status.MIC_FAILED)
        self.assertTrue(self.window.isVisible())
        self.assertIn("Microphone", self.window.root.property("statusText"))
        self.assertEqual(self.window.root.property("mode"), "mic failed")  # the orb sends out orange

    def test_the_orb_shows_each_status(self):
        for status in (Status.LISTENING, Status.THINKING, Status.SPEAKING, Status.DICTATING, Status.MIC_OFF):
            self.window.set_status(status)
            self.assertEqual(self.window.root.property("mode"), status.value)

    def test_transcript_shows_text_literally(self):
        self.window.add_heard("<b>open</b> notepad")
        self.window.add_turn("Opening it.", "sound 610 ms")
        text = self.window.conversation.plain_text()
        self.assertIn("you: <b>open</b> notepad", text)
        self.assertIn("aj: Opening it.", text)
        self.assertIn("sound 610 ms", text)
        self.app.processEvents()
        self.assertEqual(self.item("messages").property("count"), 2)

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
        while "Still here." not in self.window.conversation.plain_text() and time.perf_counter() < deadline:
            self.app.processEvents()
        self.assertEqual(self.window.status, Status.THINKING)
        self.assertIn("sound 500 ms", self.window.conversation.plain_text())


class LevelControls(FakeControls):
    """An engine with voice levels (aj.engine.Engine has them; the window uses them if there)."""

    def __init__(self):
        super().__init__()
        self.voice = self.mic = 0.0

    def voice_level(self) -> float:
        return self.voice

    def mic_level(self) -> float:
        return self.mic


@unittest.skipIf(QApplication is None, "PyQt6 is not installed")
class OrbWiringTests(unittest.TestCase):
    """Phase 2: the orb moves with the voices and shows Working while a tool runs."""

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        from PyQt6.QtTest import QTest
        self.wait = QTest.qWait
        self.engine = LevelControls()
        self.window = AJWindow(self.engine)
        self.window.show()
        self.app.processEvents()

    def tearDown(self):
        self.window._engine_done = True
        self.window.close()
        self.window.deleteLater()
        self.app.processEvents()

    def test_working_shows_while_a_tool_runs_during_thinking(self):
        self.window.set_status(Status.THINKING)
        self.window.set_working("get_weather")
        self.assertEqual(self.window.root.property("mode"), "working")
        self.assertEqual(self.window.root.property("statusText"), "Working · weather")
        self.window.set_working("")
        self.assertEqual(self.window.root.property("mode"), "thinking")
        self.assertEqual(self.window.root.property("statusText"), "Thinking")

    def test_a_tool_while_he_speaks_keeps_the_speaking_look(self):
        self.window.set_status(Status.SPEAKING)
        self.window.set_working("open_app")
        self.assertEqual(self.window.root.property("mode"), "speaking")  # it moves with his voice

    def test_the_orb_follows_the_voice_only_while_it_moves_with_one(self):
        self.window.set_status(Status.LISTENING)
        self.engine.voice = 0.9
        self.wait(120)
        self.assertEqual(self.window.root.property("voiceLevel"), 0.0)  # not read while listening
        self.window.set_status(Status.SPEAKING)
        self.wait(150)
        self.assertGreater(self.window.root.property("voiceLevel"), 0.5)
        self.window.set_status(Status.LISTENING)
        self.assertEqual(self.window.root.property("voiceLevel"), 0.0)  # and still again at once

    def test_your_voice_while_he_hears_you(self):
        self.window.set_status(Status.HEARING)
        self.engine.mic = 0.6
        self.wait(150)
        self.assertGreater(self.window.root.property("micLevel"), 0.4)

    def test_tool_reports_cross_threads_through_the_bridge(self):
        bridge = Bridge()
        connect(bridge, self.window)
        self.window.set_status(Status.THINKING)
        thread = threading.Thread(target=bridge.on_working, args=("calculate",))
        thread.start()
        thread.join()
        deadline = time.perf_counter() + 2
        while self.window.root.property("mode") != "working" and time.perf_counter() < deadline:
            self.app.processEvents()
        self.assertEqual(self.window.root.property("statusText"), "Working · calculating")


@unittest.skipIf(QApplication is None, "PyQt6 is not installed")
class ConversationTests(unittest.TestCase):
    def test_old_messages_fall_off_past_the_cap(self):
        conversation = Conversation("AJ")
        for i in range(MAX_MESSAGES + 25):
            conversation.add_you(f"message {i}")
        self.assertEqual(conversation.rowCount(), MAX_MESSAGES)
        self.assertTrue(conversation.plain_text().startswith("you: message 25"))

    def test_replies_are_labelled_with_the_assistants_name(self):
        conversation = Conversation("Computer")
        conversation.add_aj("Here.", "sound 500 ms")
        conversation.add_error("it broke")
        self.assertEqual(conversation.plain_text(), "computer: Here.\nsound 500 ms\nerror: it broke")


if __name__ == "__main__":
    unittest.main()
