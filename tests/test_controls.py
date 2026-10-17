"""The window's controls, below the window: the mic button really opens and closes the mic
stream, typed text is answered like speech, turning off stops everything, and each status
reads as the user expects. All with fakes, so no model, mic or GPU is touched."""
import contextlib
import io
import queue
import threading
import time
import unittest
from unittest import mock

from ultron import persona
from ultron.config import Settings
from ultron.core import brain as brain_mod
from ultron.core import pipeline
from ultron.core.brain import Brain
from ultron.core.pipeline import Session, Status, TurnTimes, pick_status
from ultron.engine import Engine
from ultron.ui import status as looks


def wait_until(condition, timeout: float = 3.0) -> bool:
    deadline = time.perf_counter() + timeout
    while time.perf_counter() < deadline:
        if condition():
            return True
        time.sleep(0.01)
    return condition()


class FakeMic:
    """Records open/close like the real Mic: closed means no stream at all."""

    def __init__(self):
        self.blocks: queue.Queue = queue.Queue()
        self.is_open = False
        self.calls: list[str] = []

    def open(self) -> None:
        self.is_open = True
        self.calls.append("open")

    def close(self) -> None:
        self.is_open = False
        self.calls.append("close")


class FakeEars:
    hearing_speech = False

    def reset(self) -> None:
        pass

    def feed(self, block) -> list:
        return []


class FakeBrain:
    base = "http://127.0.0.1:1"

    def __init__(self, reply: str = "Noted."):
        self.cancel = threading.Event()
        self.asked: list[str] = []
        self._reply = reply

    def reply(self, text: str, mouth, times: TurnTimes) -> str:
        self.asked.append(text)
        time.sleep(0.2)  # a model takes a while; the loop reports Thinking meanwhile
        return self._reply


class FakeMouth:
    def __init__(self):
        self.silenced = 0

    def silence(self) -> None:
        self.silenced += 1


class Recorder:
    """Collects what the engine reports (the window's Events), thread-safely enough for tests."""

    def __init__(self):
        self.statuses: list[Status] = []
        self.heard: list[str] = []
        self.turns: list[tuple[str, str]] = []

    def on_status(self, status: Status) -> None:
        self.statuses.append(status)

    def on_heard(self, text: str) -> None:
        self.heard.append(text)

    def on_turn(self, text: str, reply: str, times: TurnTimes) -> None:
        self.turns.append((text, reply))


def start_session(listening: bool = True):
    mic, brain, events, stop = FakeMic(), FakeBrain(), Recorder(), threading.Event()
    session = Session(Settings(), FakeEars(), brain, None, mic, stop, on_turn=events.on_turn,
                      on_status=events.on_status, on_heard=events.on_heard, listening=listening)
    thread = threading.Thread(target=session.run, daemon=True)
    thread.start()
    return session, mic, brain, events, stop, thread


class MicSwitchTests(unittest.TestCase):
    def test_mic_off_closes_the_stream_and_on_reopens_it(self):
        session, mic, _, events, stop, thread = start_session()
        self.assertTrue(wait_until(lambda: mic.is_open))
        self.assertTrue(wait_until(lambda: events.statuses[-1:] == [Status.LISTENING]))

        session.set_listening(False)
        self.assertTrue(wait_until(lambda: not mic.is_open))
        self.assertTrue(wait_until(lambda: events.statuses[-1:] == [Status.MIC_OFF]))

        session.set_listening(True)
        self.assertTrue(wait_until(lambda: mic.is_open))
        self.assertTrue(wait_until(lambda: events.statuses[-1:] == [Status.LISTENING]))

        stop.set()
        thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(mic.calls, ["open", "close", "open", "close"])

    def test_starting_with_the_mic_off_never_opens_it(self):
        _, mic, _, events, stop, thread = start_session(listening=False)
        self.assertTrue(wait_until(lambda: events.statuses[-1:] == [Status.MIC_OFF]))
        stop.set()
        thread.join(2)
        self.assertNotIn("open", mic.calls)

    def test_status_is_reported_only_when_it_changes(self):
        _, _, _, events, stop, thread = start_session()
        self.assertTrue(wait_until(lambda: events.statuses == [Status.LISTENING]))
        time.sleep(0.3)  # many loop passes, no change: nothing more is sent
        self.assertEqual(events.statuses, [Status.LISTENING])
        stop.set()
        thread.join(2)


class TypedTextTests(unittest.TestCase):
    def test_typed_text_is_answered_with_the_mic_off(self):
        session, mic, brain, events, stop, thread = start_session(listening=False)
        session.ask("what time is it")
        self.assertTrue(wait_until(lambda: events.turns == [("what time is it", "Noted.")]))
        self.assertEqual(brain.asked, ["what time is it"])
        self.assertEqual(events.heard, ["what time is it"])
        self.assertTrue(wait_until(lambda: events.statuses[-1:] == [Status.MIC_OFF]))
        self.assertIn(Status.THINKING, events.statuses)
        self.assertNotIn("open", mic.calls)  # typing never opens the mic
        stop.set()
        thread.join(2)

    def test_typed_off_command_turns_ultron_off(self):
        session, _, brain, _, stop, thread = start_session()
        session.ask("Ultron, turn off")
        thread.join(3)
        self.assertTrue(stop.is_set())
        self.assertFalse(thread.is_alive())
        self.assertEqual(brain.asked, [])  # never sent to the model


class EngineTests(unittest.TestCase):
    def test_turn_off_sets_stop_and_cuts_the_reply_short(self):
        engine = Engine(Settings())
        engine.brain, engine.mouth = FakeBrain(), FakeMouth()
        engine.turn_off()
        self.assertTrue(engine.stop.is_set())
        self.assertTrue(engine.brain.cancel.is_set())
        self.assertEqual(engine.mouth.silenced, 1)

    def test_turn_off_before_anything_loaded(self):
        engine = Engine(Settings())
        engine.turn_off()
        self.assertTrue(engine.stop.is_set())

    def test_mic_setting_made_while_loading_applies_when_the_session_starts(self):
        engine = Engine(Settings())
        engine.ears, engine.brain = FakeEars(), FakeBrain()
        engine.set_listening(False)
        mic, events = FakeMic(), Recorder()
        session = engine.new_session(mic, on_status=events.on_status)
        thread = threading.Thread(target=session.run, daemon=True)
        thread.start()
        self.assertTrue(wait_until(lambda: events.statuses[-1:] == [Status.MIC_OFF]))
        engine.turn_off()
        thread.join(2)
        self.assertNotIn("open", mic.calls)

    def test_run_answers_typed_text_then_turns_off_cleanly(self):
        """The window's engine thread end to end, with loading faked out."""
        engine = Engine(Settings(), speak=False)
        mic, events = FakeMic(), Recorder()

        def fake_load() -> float:
            engine.ears, engine.brain = FakeEars(), FakeBrain("It's noon.")
            return 0.0

        engine.load = fake_load
        with mock.patch("ultron.engine.Mic", return_value=mic), contextlib.redirect_stdout(io.StringIO()):
            thread = threading.Thread(target=engine.run, args=(events,), daemon=True)
            thread.start()
            self.assertTrue(wait_until(lambda: Status.LISTENING in events.statuses))
            engine.ask("what time is it")
            self.assertTrue(wait_until(lambda: events.turns == [("what time is it", "It's noon.")]))
            engine.turn_off()
            thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertEqual(events.statuses[0], Status.LOADING)
        self.assertEqual(events.statuses[-1], Status.TURNING_OFF)
        self.assertFalse(mic.is_open)  # the mic stream is closed on the way out


class ReminderTests(unittest.TestCase):
    def _reply(self, brain: Brain, text: str) -> list[dict]:
        sent = []

        def fake_stream(base, messages, settings, tools=None, cancel=None):
            sent.append(messages)
            yield "text", "Fine."

        with mock.patch.object(brain_mod, "stream_events", fake_stream):
            brain.reply(text, None, TurnTimes(stopped_talking=time.perf_counter()))
        return sent[-1]

    def test_length_reminder_follows_the_question_and_the_clock_comes_first(self):
        brain = Brain("http://127.0.0.1:1", Settings())
        for text, reminder in (("why is the sky blue?", persona.EXPLAIN_REMINDER),
                               ("what's the capital of France?", persona.SHORT_REMINDER)):
            sent = self._reply(brain, text)[-1]["content"]
            self.assertTrue(sent.startswith("[Now: "), sent)  # the real date and time, every turn
            self.assertTrue(sent.endswith("\n" + text + reminder), sent)
            # History keeps the turn without the clock: a stale time in old turns would mislead.
            self.assertEqual(brain.history[-2], {"role": "user", "content": text + reminder})

    def test_next_turn_resends_the_stored_history_unchanged(self):
        brain = Brain("http://127.0.0.1:1", Settings())
        self._reply(brain, "explain how vaccines work")
        stored = brain.history[0]
        second = self._reply(brain, "thanks")
        self.assertEqual(second[1], stored)


class StatusTests(unittest.TestCase):
    def test_pick_status(self):
        cases = [  # (listening, answering, hearing, speaking) -> status
            ((True, False, False, False), Status.LISTENING),
            ((False, False, False, False), Status.MIC_OFF),
            ((True, False, True, False), Status.HEARING),
            ((True, True, False, False), Status.THINKING),
            ((True, True, False, True), Status.SPEAKING),
            ((False, True, False, False), Status.THINKING),  # typed with the mic off
            ((False, True, False, True), Status.SPEAKING),
            ((True, True, True, True), Status.HEARING),     # talking over him with headphones
            ((False, False, True, False), Status.MIC_OFF),  # a closed mic hears nothing
        ]
        for args, expected in cases:
            self.assertEqual(pick_status(*args), expected, args)

    def test_every_status_has_the_wording_the_user_sees(self):
        expected = {Status.LOADING: "Loading...", Status.LISTENING: "Listening",
                    Status.HEARING: "Hearing you", Status.THINKING: "Thinking",
                    Status.SPEAKING: "Speaking", Status.MIC_OFF: "Mic off"}
        for status in Status:
            label, colour = looks.look(status)
            self.assertTrue(colour.startswith("#"), status)
            if status in expected:
                self.assertEqual(label, expected[status])

    def test_controls_are_off_only_while_loading_or_turning_off(self):
        self.assertEqual(looks.NOT_READY, {Status.LOADING, Status.TURNING_OFF})

    def test_mic_caption(self):
        self.assertIn("Mic on", looks.mic_caption(True))
        self.assertIn("Mic off", looks.mic_caption(False))


class LatencySummaryTests(unittest.TestCase):
    def test_spoken_turn(self):
        t = TurnTimes(stopped_talking=10.0, heard=10.21, asked=10.215, first_token=10.265,
                      first_chunk=10.4, first_audio=10.52, first_sound=10.61)
        self.assertEqual(t.summary(), "sound 610 · heard 210 · first token 265 · voice 520 ms")

    def test_typed_turn_leaves_out_what_did_not_happen(self):
        t = TurnTimes(stopped_talking=10.0, first_token=10.3)
        self.assertEqual(t.summary(), "first token 300 ms")

    def test_nothing_happened(self):
        self.assertEqual(TurnTimes(stopped_talking=10.0).summary(), "")


if __name__ == "__main__":
    unittest.main()
