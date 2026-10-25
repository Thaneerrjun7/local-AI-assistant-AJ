"""Sleep mode wired in (2026-10-22): the conversation loop asks core/wake.py's Gate about every
utterance; asleep only its name gets through, on battery the mic closes; the engine sleeps and
wakes on request, can let go of the model while asleep, and answers the wake listener's
--wake-command first. Fakes throughout: no model, mic, GPU or speakers."""
import contextlib
import io
import queue
import tempfile
import threading
import time
import unittest
import wave
from pathlib import Path
from unittest import mock

import numpy as np

from aj import toggle
from aj.__main__ import parse_args
from aj.config import Settings, load_settings
from aj.core import pipeline
from aj.core.audio import MIC_BLOCK, MIC_RATE
from aj.core.pipeline import Session, Status, TurnTimes, is_off_command
from aj.core.wake import Gate, Mode, WakeWords
from aj.engine import Engine


def wait_until(condition, timeout: float = 3.0) -> bool:
    deadline = time.perf_counter() + timeout
    while time.perf_counter() < deadline:
        if condition():
            return True
        time.sleep(0.01)
    return condition()


class FakeMic:
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


class ScriptedEars:
    """Each block put on the mic is one whole utterance; its first sample says which line it is."""
    hearing_speech = False

    def __init__(self):
        self.lines: list[str] = [""]

    def utterance(self, text: str) -> np.ndarray:
        self.lines.append(text)
        return np.full(MIC_BLOCK, len(self.lines) - 1, dtype=np.float32)

    def reset(self) -> None:
        pass

    def feed(self, block) -> list:
        return [block] if block[0] >= 1 else []

    def transcribe(self, samples, rate: int = MIC_RATE) -> str:
        return self.lines[int(samples[0])]


class FakeLines:
    def pick(self, pool: str) -> str:
        return f"<{pool}>"


class FakeBrain:
    base = "http://127.0.0.1:1"

    def __init__(self):
        self.cancel = threading.Event()
        self.asked: list[str] = []
        self.lines = FakeLines()

    def reply(self, text: str, mouth, times: TurnTimes) -> str:
        self.asked.append(text)
        return "Done."


class FakeDictation:
    def __init__(self):
        self.armed = None

    def hear(self, pieces) -> None:
        pass

    def disarm(self):
        request, self.armed = self.armed, None
        return request


class Recorder:
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


class SessionTests(unittest.TestCase):
    def start(self, on_ac=lambda: True, idle_mins: float = 0.0, dictation=None):
        settings = Settings()
        settings.sleep.power_check_secs = 0.05
        settings.voice.unfinished_wait_secs = 0.3
        self.mic, self.ears, self.brain, self.events = FakeMic(), ScriptedEars(), FakeBrain(), Recorder()
        self.stop = threading.Event()
        self.ac_calls = 0

        def ac():
            self.ac_calls += 1
            return on_ac()

        self.gate = Gate(WakeWords(settings.wake_names), idle_mins=idle_mins)
        self.session = Session(settings, self.ears, self.brain, None, self.mic, self.stop,
                               on_turn=self.events.on_turn, on_status=self.events.on_status,
                               on_heard=self.events.on_heard, gate=self.gate, on_ac=ac, dictation=dictation)
        self.thread = threading.Thread(target=self.session.run, daemon=True)
        self.thread.start()
        self.addCleanup(self.finish)
        self.assertTrue(wait_until(lambda: self.mic.is_open))

    def finish(self):
        self.stop.set()
        self.thread.join(3)

    def say(self, text: str) -> None:
        self.mic.blocks.put(self.ears.utterance(text))

    def status(self) -> Status | None:
        return self.events.statuses[-1] if self.events.statuses else None

    def test_awake_the_name_is_taken_off_and_shown(self):
        self.start()
        self.say("Hey AJ, open WhatsApp.")
        self.assertTrue(wait_until(lambda: self.brain.asked == ["open WhatsApp."]))
        self.assertEqual(self.events.heard, ["Hey AJ, open WhatsApp."])
        self.assertTrue(wait_until(lambda: self.events.turns == [("Hey AJ, open WhatsApp.", "Done.")]))

    def test_go_to_sleep_then_only_the_name_gets_through(self):
        self.start()
        self.say("Go back to sleep.")
        self.assertTrue(wait_until(lambda: self.status() == Status.ASLEEP))
        self.assertEqual(self.events.turns, [("Go back to sleep.", "<sleep>")])
        self.say("What time is it?")
        self.say("The computer is slow today.")
        time.sleep(0.3)
        self.assertEqual(self.brain.asked, [])  # never answered...
        self.assertEqual(self.events.heard, ["Go back to sleep."])  # ...nor shown
        self.say("Hey AJ, what time is it?")
        self.assertTrue(wait_until(lambda: self.brain.asked == ["what time is it?"]))
        self.assertTrue(wait_until(lambda: self.status() == Status.LISTENING))
        self.assertEqual(self.gate.mode, Mode.AWAKE)

    def test_the_name_alone_wakes_it_with_a_short_line(self):
        self.start()
        self.session.sleep()
        self.assertTrue(wait_until(lambda: self.status() == Status.ASLEEP))
        self.say("Computer.")
        self.assertTrue(wait_until(lambda: ("Computer.", "<wake>") in self.events.turns))
        self.assertEqual(self.brain.asked, [])
        self.assertFalse(self.gate.asleep)

    def test_after_the_name_alone_it_waits_a_breath_for_the_command(self):
        self.start()
        self.say("Hey AJ.")
        time.sleep(0.1)
        self.say("open WhatsApp")
        self.assertTrue(wait_until(lambda: self.brain.asked == ["open WhatsApp"]))
        self.assertEqual(self.events.turns, [("Hey AJ. open WhatsApp", "Done.")])

    def test_turn_off_after_any_spelling_of_the_name(self):
        self.start()
        self.session.sleep()
        self.assertTrue(wait_until(lambda: self.status() == Status.ASLEEP))
        self.say("OJ turn off.")  # measured: Parakeet's spelling of "AJ, turn off."
        self.thread.join(3)
        self.assertTrue(self.stop.is_set())
        self.assertEqual(self.brain.asked, [])

    def test_typing_wakes_it(self):
        self.start()
        self.session.sleep()
        self.assertTrue(wait_until(lambda: self.gate.asleep))
        self.session.ask("what time is it")
        self.assertTrue(wait_until(lambda: self.brain.asked == ["what time is it"]))
        self.assertFalse(self.gate.asleep)

    def test_tray_sleep_and_wake(self):
        self.start()
        self.session.sleep()
        self.assertTrue(wait_until(lambda: self.status() == Status.ASLEEP))
        self.session.wake()
        self.assertTrue(wait_until(lambda: self.status() == Status.LISTENING))
        self.assertEqual(self.events.turns, [])  # no lines for the tray: the window says it

    def test_on_battery_asleep_the_mic_closes_and_plugging_in_opens_it(self):
        plugged = [False]
        self.start(on_ac=lambda: plugged[0])
        self.session.sleep()
        self.assertTrue(wait_until(lambda: not self.mic.is_open))
        self.assertTrue(wait_until(lambda: self.status() == Status.ASLEEP_ON_BATTERY))
        plugged[0] = True
        self.assertTrue(wait_until(lambda: self.mic.is_open))
        self.assertTrue(wait_until(lambda: self.status() == Status.ASLEEP))
        plugged[0] = False
        self.assertTrue(wait_until(lambda: not self.mic.is_open))  # unplugged again: closed within seconds

    def test_awake_on_battery_works_as_before_and_never_looks_at_the_cable(self):
        self.start(on_ac=lambda: False)
        self.say("What time is it?")
        self.assertTrue(wait_until(lambda: self.brain.asked == ["What time is it?"]))
        self.assertTrue(self.mic.is_open)
        self.assertEqual(self.ac_calls, 0)

    def test_unknown_power_counts_as_battery(self):
        self.start(on_ac=lambda: None)
        self.session.sleep()
        self.assertTrue(wait_until(lambda: not self.mic.is_open))

    def test_the_tray_still_wakes_it_on_battery(self):
        self.start(on_ac=lambda: False)
        self.session.sleep()
        self.assertTrue(wait_until(lambda: not self.mic.is_open))
        self.session.wake()
        self.assertTrue(wait_until(lambda: self.mic.is_open))
        self.assertTrue(wait_until(lambda: self.status() == Status.LISTENING))

    def test_asleep_with_the_mic_button_off(self):
        self.start()
        self.session.set_listening(False)
        self.session.sleep()
        self.assertTrue(wait_until(lambda: self.status() == Status.ASLEEP_MIC_OFF))
        self.assertEqual(self.ac_calls, 0)  # nothing to listen with: the cable doesn't matter

    def test_falls_asleep_after_the_idle_minutes(self):
        self.start(idle_mins=0.2 / 60)
        self.assertTrue(wait_until(lambda: self.status() == Status.ASLEEP))

    def test_falling_asleep_drops_a_waiting_dictation(self):
        dictation = FakeDictation()
        dictation.armed = object()
        self.start(dictation=dictation)
        self.session.sleep()
        self.assertTrue(wait_until(lambda: dictation.armed is None))
        self.assertTrue(wait_until(lambda: self.status() == Status.ASLEEP))

    def test_asleep_the_rooms_talk_doesnt_keep_the_gpu_awake(self):
        self.start()
        self.session.sleep()
        self.assertTrue(wait_until(lambda: self.gate.asleep))
        with mock.patch.object(pipeline, "wake_gpu") as ping:
            self.ears.hearing_speech = True
            self.say("so anyway")
            time.sleep(0.3)
            self.ears.hearing_speech = False
        ping.assert_not_called()


class StatusTests(unittest.TestCase):
    def test_asleep_wins_unless_answering(self):
        self.assertEqual(pipeline.pick_status(True, False, True, False, asleep=True), Status.ASLEEP)
        self.assertEqual(pipeline.pick_status(False, False, False, False, asleep=True, unplugged=True),
                         Status.ASLEEP_ON_BATTERY)
        self.assertEqual(pipeline.pick_status(False, False, False, False, asleep=True), Status.ASLEEP_MIC_OFF)
        self.assertEqual(pipeline.pick_status(True, True, False, True, asleep=True), Status.SPEAKING)

    def test_off_command_with_measured_spellings(self):
        for said in ("AJ, turn off.", "OJ turn off.", "Hey A.J., turn off", "Computer, shut down"):
            self.assertTrue(is_off_command(said), said)
        self.assertFalse(is_off_command("AJ, turn off the music"))


class FakeServer:
    base = "http://127.0.0.1:1"

    def __init__(self):
        self.calls: list[str] = []

    def launch(self) -> None:
        self.calls.append("launch")

    def wait_ready(self, timeout: float = 120) -> float:
        self.calls.append("ready")
        return 0.0

    def stop(self) -> None:
        self.calls.append("stop")


def write_wav(path: Path, samples: np.ndarray) -> None:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(MIC_RATE)
        w.writeframes((samples * 32767).astype(np.int16).tobytes())


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.out = contextlib.redirect_stdout(io.StringIO())
        self.out.__enter__()
        self.addCleanup(self.out.__exit__, None, None, None)

    def test_it_answers_to_both_names(self):
        engine = Engine(Settings())
        self.assertEqual(engine.gate.words.names, ["AJ", "Computer"])

    def test_sleep_and_wake_before_the_conversation_starts(self):
        engine = Engine(Settings())
        engine.sleep()
        self.assertTrue(engine.gate.asleep)
        engine.wake()
        self.assertFalse(engine.gate.asleep)

    def test_the_model_stays_loaded_asleep_by_default(self):
        server = FakeServer()
        engine = Engine(Settings(), server=server)
        engine.gate.sleep()
        engine.gate.wake()
        time.sleep(0.1)
        self.assertEqual(server.calls, [])

    def test_free_gpu_lets_go_of_the_model_asleep_and_reloads_it_on_waking(self):
        settings = Settings()
        settings.sleep.free_gpu = True
        server = FakeServer()
        engine = Engine(settings, server=server)
        engine.gate.sleep()
        self.assertTrue(wait_until(lambda: server.calls == ["stop"]))
        engine.gate.wake()
        engine.model_ready()  # an answer waits here until the model is back
        self.assertEqual(server.calls, ["stop", "launch", "ready"])
        self.assertIsNotNone(engine.reload_secs)
        engine.model_ready()  # loaded: nothing more to do
        self.assertEqual(server.calls, ["stop", "launch", "ready"])

    def test_the_wake_command_is_read_answered_and_deleted(self):
        folder = Path(tempfile.mkdtemp())
        path = folder / "aj-wake-1.wav"
        write_wav(path, np.full(MIC_RATE // 2, 0.1, dtype=np.float32))
        engine = Engine(Settings(), wake_command=path)
        engine.ears = mock.Mock()
        engine.ears.transcribe.return_value = "Hey AJ, open WhatsApp."
        text, audio = engine.read_wake_command()
        self.assertEqual(text, "open WhatsApp.")
        self.assertEqual(len(audio[0]), MIC_RATE // 2)
        self.assertFalse(path.exists())
        self.assertIsNone(engine.read_wake_command())  # once only

    def test_a_damaged_wake_command_costs_only_the_command(self):
        path = Path(tempfile.mkdtemp()) / "aj-wake-2.wav"
        path.write_bytes(b"not a wav")
        engine = Engine(Settings(), wake_command=path)
        engine.ears = mock.Mock()
        self.assertIsNone(engine.read_wake_command())
        self.assertFalse(path.exists())

    def test_run_answers_the_wake_command_first_instead_of_greeting(self):
        path = Path(tempfile.mkdtemp()) / "aj-wake-3.wav"
        write_wav(path, np.full(MIC_RATE // 2, 0.1, dtype=np.float32))
        engine = Engine(Settings(), speak=False, wake_command=path)
        mic, events, brain = FakeMic(), Recorder(), FakeBrain()

        def fake_load() -> float:
            engine.ears, engine.brain = ScriptedEars(), brain
            engine.ears.transcribe = lambda samples, rate=MIC_RATE: "Computer, what time is it?"
            return 0.0

        engine.load = fake_load
        with mock.patch("aj.engine.Mic", return_value=mic):
            thread = threading.Thread(target=engine.run, args=(events,), daemon=True)
            thread.start()
            self.assertTrue(wait_until(lambda: brain.asked == ["what time is it?"]))
            engine.turn_off()
            thread.join(3)
        self.assertFalse(path.exists())


class LaunchTests(unittest.TestCase):
    def test_wake_command_is_not_a_console_mode(self):
        args, console = parse_args(["--wake-command", "C:/temp/aj-wake-1.wav"])
        self.assertFalse(console)
        self.assertEqual(args.wake_command, Path("C:/temp/aj-wake-1.wav"))
        self.assertTrue(parse_args(["--text"])[1])

    def test_toggle_start_passes_extra_arguments(self):
        with mock.patch.object(toggle.subprocess, "Popen") as popen:
            toggle.start(["--wake-command", "x.wav"], env={"A": "1"})
        args = popen.call_args.args[0]
        self.assertEqual(args[1:], ["-m", "aj", "--wake-command", "x.wav"])
        self.assertEqual(popen.call_args.kwargs["env"], {"A": "1"})
        self.assertTrue(popen.call_args.kwargs["creationflags"] & 0x20)  # normal priority, not the listener's

    def test_settings(self):
        settings = Settings()
        self.assertEqual(settings.wake_names, ["AJ", "Computer"])
        self.assertFalse(settings.sleep.free_gpu)
        self.assertEqual(settings.sleep.idle_mins, 0)
        path = Path(tempfile.mkdtemp()) / "settings.toml"
        path.write_text('aliases = ["Computer", "Jarvis"]\n[sleep]\nfree_gpu = true\nidle_mins = 15\n')
        loaded = load_settings(path)
        self.assertEqual(loaded.wake_names, ["AJ", "Computer", "Jarvis"])
        self.assertTrue(loaded.sleep.free_gpu)
        self.assertEqual(loaded.sleep.idle_mins, 15)


if __name__ == "__main__":
    unittest.main()
