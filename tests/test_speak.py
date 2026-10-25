"""AJ (Speak) (aj/speak): the floating speech-to-text window and what is behind it. Qt runs on its
offscreen platform and everything else is a fake: a fake desktop (it can type and press Backspace,
and has no way to press Enter or use a clipboard), a fake microphone, a fake voice detector and a
fake Gemini. Nothing is typed anywhere, no hotkey is registered, no window appears, and no sound is
played.

Run: .venv\\Scripts\\python.exe -m unittest tests.test_speak
"""
import ctypes
import inspect
import json
import logging
import os
import queue
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

os.environ["QT_QPA_PLATFORM"] = "offscreen"  # before any QApplication exists: never show a real window

import numpy as np

from aj.config import DictationSettings, Settings
from aj.core import dictation, gemini, keyboard
from aj.core.audio import MIC_BLOCK, MIC_RATE
from aj.core.uia import Node
from aj.speak import hotkey, instance, listen, store
from aj.speak import speaker as sp
from aj.speak.speaker import Speaker

try:
    from PyQt6.QtCore import QEvent, QPointF, Qt
    from PyQt6.QtGui import QMouseEvent
    from PyQt6.QtWidgets import QApplication
except ImportError:  # pragma: no cover - the window is optional
    QApplication = None

if QApplication is not None:
    from aj.speak import icon
    from aj.speak import window as win
    from aj.speak.window import SpeakWindow

APP, OURS, CHROME, WHATSAPP = 100, 1, 77, 200
BLOCK_SECS = MIC_BLOCK / MIC_RATE
SAID = "Hi Amma, I'll be home by 7:30."
POLISHED = "Hello Amma, I shall be home by 7:30, please."


def wait_until(condition, timeout: float = 3.0) -> bool:
    deadline = time.perf_counter() + timeout
    while time.perf_counter() < deadline:
        if condition():
            return True
        time.sleep(0.005)
    return condition()


def join_jobs() -> None:
    """Wait for every dictation thread to end (a cancelled one finishes by itself)."""
    for thread in threading.enumerate():
        if thread.name == "speak-job":
            thread.join(3)


def block(amplitude: float = 0.0) -> np.ndarray:
    return np.full(MIC_BLOCK, amplitude, np.float32)


# ---- fakes ------------------------------------------------------------------------------------------

class FakeDesktop:
    """The desktop as dictation.Desktop sees it. It can type and press Backspace and nothing else."""

    def __init__(self, front=APP, editable=True):
        self.front_hwnd = front
        self.answers = list(editable) if isinstance(editable, list) else [editable]  # one per editable() call
        self.typed: list[str] = []
        self.backspaces = 0
        self.focused: list[int] = []
        self.on_type = None
        self.type_gate: threading.Event | None = None
        self.exes = {APP: "notepad.exe", WHATSAPP: "whatsapp.root.exe", OURS: "python.exe", CHROME: "chrome.exe"}

    def front(self):
        return self.front_hwnd

    def focus(self, hwnd):
        self.focused.append(hwnd)
        self.front_hwnd = hwnd
        return True

    def exe(self, hwnd):
        return self.exes.get(hwnd, "")

    def title(self, hwnd):
        return "AJ (Speak)" if hwnd == OURS else ""

    def ours(self, hwnd):
        return hwnd == OURS

    def alive(self, hwnd):
        return bool(hwnd)

    def windows(self):
        return []

    def editable(self):
        return self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]

    def focused_box(self):
        """The focused element as type_into() reads it (2026-10-22): its answer is editable()'s."""
        editable = self.editable()
        if editable is None:
            return None
        return Node(control_type="Edit", has_value=bool(editable), read_only=not editable, focusable=True,
                    runtime_id=(5, 1))

    def value(self):
        return ""

    def caret_at_end(self):
        return True

    def type(self, text, multiline=False):
        if self.type_gate is not None:
            self.type_gate.wait(5)
        self.typed.append(text)
        if self.on_type:
            self.on_type()

    def backspace(self):
        self.backspaces += 1

    @property
    def text(self) -> str:
        return "".join(self.typed)


class FakeWatch:
    def __init__(self, last=0):
        self.last, self.started, self.stopped = last, False, False

    def last_other(self):
        return self.last

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True


class FakeMic:
    """core.audio.Mic: a queue of blocks, and a record of being opened and closed."""

    def __init__(self, blocks=0, amplitude=0.0, fail=False):
        self.blocks: queue.Queue = queue.Queue()
        for _ in range(blocks):
            self.blocks.put(block(amplitude))
        self.fail, self.opened, self.closed = fail, 0, 0

    def open(self):
        if self.fail:
            raise OSError("no microphone")
        self.opened += 1

    def close(self):
        self.closed += 1


PIECE = np.full(MIC_RATE * 6, 0.1, np.float32)  # six seconds of 'speech': up to 50 words are believable


class FakeVad:
    """Silero VAD as listen.record() sees it, told which blocks have the boss talking in them."""
    min_silence = 0.3

    def __init__(self, talking):
        self.script = list(talking)
        self.fed = 0
        self.hearing = False
        self.resets = 0
        self.flushes = 0

    def reset(self):
        self.resets += 1

    def feed(self, _block):
        on = self.script[self.fed] if self.fed < len(self.script) else False
        self.fed += 1
        ended = self.hearing and not on
        self.hearing = on
        return [PIECE.copy()] if ended else []

    @property
    def hearing_speech(self):
        return self.hearing

    def flush(self):
        self.flushes += 1
        out = [PIECE.copy()] if self.hearing else []
        self.hearing = False
        return out


class FakeEars:
    def __init__(self, text):
        self.text = text

    def transcribe(self, piece, rate=MIC_RATE):
        return self.text


class Rig:
    """A Speaker wired to fakes. Gemini is patched, so the real core (transcribe, polish, type_into) runs."""

    def __init__(self, case: unittest.TestCase, *, front=APP, editable=True, talk=15, quiet=60, last_other=0,
                 settings=None, mic_fails=False, vad_fails=False, ears_fails=False, gemini_down=False,
                 polish_down=False, said=SAID, polished=POLISHED, ears_text="hi amma i'll be home by seven thirty",
                 live_mic=False):
        self.desktop = FakeDesktop(front, editable)
        self.watch = FakeWatch(last_other)
        self.mic = FakeMic(0 if live_mic else talk + quiet, fail=mic_fails)
        self.vad = FakeVad([True] * talk)
        self.said, self.polished, self.ears_text = said, polished, ears_text
        self.gemini_down, self.polish_down, self.ears_fails, self.vad_fails = gemini_down, polish_down, ears_fails, vad_fails
        self.gemini_gate = threading.Event()
        self.gemini_gate.set()  # clear it to hold Gemini mid-answer
        self.polish_gate = threading.Event()
        self.polish_gate.set()  # ... or mid-polish
        self.gemini_calls: list[dict] = []
        self.polish_calls: list[str] = []
        self.mics_made = self.ears_made = 0
        self.events: list[tuple[str, str]] = []
        self.levels: list[float] = []
        self.speaker = Speaker(settings or Settings(), desktop=self.desktop, watch=self.watch,
                               mic_factory=self._mic, vad_factory=self._vad, ears_factory=self._ears)
        self.speaker.on_state = lambda state, text: self.events.append((state, text))
        self.speaker.on_level = self.levels.append
        case.enterContext(mock.patch.object(gemini, "transcribe", self._transcribe))
        case.enterContext(mock.patch.object(gemini, "generate", self._generate))
        case.addCleanup(self.gemini_gate.set)
        case.addCleanup(self.polish_gate.set)
        case.addCleanup(join_jobs)

    def _mic(self):
        self.mics_made += 1
        return self.mic

    def _vad(self, voice):
        if self.vad_fails:
            raise FileNotFoundError("silero_vad.onnx")
        return self.vad

    def _ears(self, voice):
        self.ears_made += 1
        if self.ears_fails:
            raise OSError("the model files are missing")
        return FakeEars(self.ears_text)

    def _transcribe(self, audio, hint="", model="", timeout=0):
        self.gemini_calls.append({"hint": hint, "bytes": len(audio), "model": model})
        self.gemini_gate.wait(5)
        if self.gemini_down:
            raise gemini.GeminiUnavailable("no key")
        return self.said

    def _generate(self, text, system="", model="", temperature=0, max_tokens=0, timeout=0):
        self.polish_calls.append(text)
        self.polish_gate.wait(5)
        if self.polish_down:
            raise gemini.GeminiUnavailable("no key")
        return self.polished

    def rearm(self, talk=15, quiet=60, amplitude=0.0) -> None:
        """Another dictation's worth of sound (the Speaker keeps its voice detector between dictations)."""
        self.mic = FakeMic(talk + quiet, amplitude)
        self.vad.script, self.vad.fed, self.vad.hearing = [True] * talk, 0, False

    @property
    def states(self) -> list[str]:
        return [state for state, _ in self.events]

    @property
    def final(self) -> tuple[str, str]:
        return self.events[-1]

    def finished(self) -> bool:
        return bool(self.events) and self.events[-1][0] in sp.RESULTS

    def go(self) -> tuple[str, str]:
        """Click the mic and wait for the result."""
        self.speaker.toggle()
        self.assert_done()
        return self.final

    def assert_done(self) -> None:
        if not wait_until(self.finished):
            raise AssertionError(f"never finished: {self.events}")
        join_jobs()


# ---- where it sits and what it remembers -----------------------------------------------------------

class StoreTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.path = self.dir / "speak_window.json"

    def test_a_missing_file_means_the_defaults(self):
        self.assertEqual(store.load(self.path), {})

    def test_a_corrupt_or_odd_file_means_the_defaults(self):
        for text in ("{not json", "[1, 2]", "42", "", "��"):
            self.path.write_text(text, encoding="utf-8")
            self.assertEqual(store.load(self.path), {}, text)

    def test_what_was_saved_comes_back(self):
        self.assertTrue(store.save({"x": 120, "y": -40, "polite": True}, self.path))
        self.assertEqual(store.load(self.path), {"x": 120, "y": -40, "polite": True})

    def test_junk_values_are_dropped_one_by_one(self):
        self.path.write_text(json.dumps({"x": "left", "y": 5, "polite": "yes", "other": 1}), encoding="utf-8")
        self.assertEqual(store.load(self.path), {})  # a position needs both numbers
        self.path.write_text(json.dumps({"x": True, "y": 5, "polite": False}), encoding="utf-8")
        self.assertEqual(store.load(self.path), {"polite": False})
        self.path.write_text(json.dumps({"x": 10 ** 9, "y": 5}), encoding="utf-8")
        self.assertEqual(store.load(self.path), {})

    def test_a_file_that_cannot_be_written_fails_soft(self):
        blocker = self.dir / "a file"
        blocker.write_text("x")
        with self.assertLogs("aj.speak.store", "ERROR"):
            self.assertFalse(store.save({"x": 1, "y": 2}, blocker / "speak_window.json"))  # its parent is a file

    def test_a_value_that_cannot_be_written_fails_soft(self):
        with self.assertLogs("aj.speak.store", "ERROR"):
            self.assertFalse(store.save({"x": object()}, self.path))
        self.assertFalse(self.path.exists())

    def test_the_default_file_is_in_the_data_folder(self):
        from aj.config import DATA_DIR
        self.assertEqual(store.PATH.parent, DATA_DIR)

    SCREEN = (0, 0, 1920, 1040)

    def test_a_saved_position_on_a_screen_is_used(self):
        self.assertEqual(store.place({"x": 300, "y": 200}, 320, 76, [self.SCREEN]), (300, 200))

    def test_a_saved_position_off_every_screen_goes_to_the_corner(self):
        self.assertEqual(store.place({"x": 5000, "y": 200}, 320, 76, [self.SCREEN]), (1920 - 320 - 24, 1040 - 76 - 24))
        self.assertEqual(store.place({"x": -400, "y": -300}, 320, 76, [self.SCREEN]), (1576, 940))

    def test_a_window_with_only_a_sliver_showing_is_brought_back(self):
        self.assertEqual(store.place({"x": 1910, "y": 500}, 320, 76, [self.SCREEN]), (1576, 940))
        self.assertEqual(store.place({"x": 1700, "y": 500}, 320, 76, [self.SCREEN]), (1700, 500))  # enough showing

    def test_a_second_screen_counts(self):
        second = (1920, 0, 1280, 1024)
        self.assertEqual(store.place({"x": 2500, "y": 100}, 320, 76, [self.SCREEN, second]), (2500, 100))
        self.assertEqual(store.place({"x": 2500, "y": 100}, 320, 76, [self.SCREEN]), (1576, 940))  # it was unplugged

    def test_nothing_saved_starts_in_the_corner_and_no_screens_cannot_crash(self):
        self.assertEqual(store.place({}, 320, 76, [self.SCREEN]), (1576, 940))
        self.assertEqual(store.place({}, 320, 76, []), (0, 0))


# ---- one at a time ---------------------------------------------------------------------------------

class InstanceTests(unittest.TestCase):
    def setUp(self):
        self.name = f"AJ.Speak.Test.{os.getpid()}.{time.monotonic_ns()}"  # never meets a real AJ (Speak)
        self.first = instance.Instance(self.name)
        self.addCleanup(self.first.release)

    def test_the_first_claims_it_and_the_second_does_not(self):
        second = instance.Instance(self.name)
        self.addCleanup(second.release)
        self.assertTrue(self.first.claim())
        self.assertFalse(second.claim())

    def test_names_are_separate_from_the_assistants_and_from_each_other(self):
        other = instance.Instance(self.name + ".other")
        self.addCleanup(other.release)
        self.assertTrue(self.first.claim())
        self.assertTrue(other.claim())
        from aj.core import switch
        self.assertNotEqual(instance.Instance()._mutex_name, switch._MUTEX_NAME)
        self.assertNotEqual(instance.Instance()._show_name, switch._SHOW_NAME)

    def test_a_second_launch_asks_the_first_to_show_itself(self):
        shown = threading.Event()
        count = []
        self.assertTrue(self.first.claim())
        self.first.watch_for_show(lambda: (count.append(1), shown.set()))
        second = instance.Instance(self.name)
        self.addCleanup(second.release)
        self.assertFalse(second.claim())
        self.assertTrue(second.request_show())
        self.assertTrue(shown.wait(3))
        shown.clear()
        self.assertTrue(second.request_show())  # and again: each request is one call
        self.assertTrue(shown.wait(3))
        self.assertEqual(len(count), 2)

    def test_asking_before_the_first_is_listening_says_so(self):
        self.assertFalse(instance.Instance(self.name).request_show())

    def test_once_released_the_name_is_free_again(self):
        self.assertTrue(self.first.claim())
        self.first.release()
        again = instance.Instance(self.name)
        self.addCleanup(again.release)
        self.assertTrue(again.claim())


# ---- the hotkey -----------------------------------------------------------------------------------

class FakeKeys:
    """hotkey.Win32Keys without Windows: some combinations are 'taken', and presses are scripted."""

    def __init__(self, taken=(), explode=False):
        self.taken, self.explode = set(taken), explode
        self.tried: list[str] = []
        self.registered: list[str] = []
        self.unregistered: list[int] = []
        self.events: queue.Queue = queue.Queue()

    def register(self, key_id, chord):
        if self.explode:
            raise OSError("no message queue")
        self.tried.append(chord.label)
        if chord.label in self.taken:
            return False
        self.registered.append(chord.label)
        return True

    def unregister(self, key_id):
        self.unregistered.append(key_id)

    def wait(self):
        return self.events.get(timeout=3)

    def wake(self):
        self.events.put(None)

    def press(self):
        self.events.put(hotkey._ID)


class HotkeyTests(unittest.TestCase):
    def make(self, taken=(), **kw):
        self.pressed = threading.Semaphore(0)
        self.count = 0

        def on_press():
            self.count += 1
            self.pressed.release()
        self.keys = FakeKeys(taken, kw.pop("explode", False))
        self.hot = hotkey.Hotkey(on_press, keys=self.keys, **kw)
        self.addCleanup(self.hot.stop)
        return self.hot.start()

    def test_ctrl_alt_s_is_the_first_choice(self):
        chord = self.make()
        self.assertEqual(chord.label, "Ctrl+Alt+S")
        self.assertEqual(self.keys.tried, ["Ctrl+Alt+S"])
        self.assertEqual(chord.mods, hotkey.MOD_CONTROL | hotkey.MOD_ALT)
        self.assertEqual(chord.vk, ord("S"))
        self.assertEqual(hotkey.describe(chord), "Ctrl+Alt+S")

    def test_a_press_runs_the_action_each_time(self):
        self.make()
        self.keys.press()
        self.keys.press()
        self.assertTrue(self.pressed.acquire(timeout=3))
        self.assertTrue(self.pressed.acquire(timeout=3))
        self.assertEqual(self.count, 2)

    def test_when_s_is_taken_it_falls_back_to_ctrl_alt_d_and_says_so(self):
        chord = self.make(taken={"Ctrl+Alt+S"})
        self.assertEqual(chord.label, "Ctrl+Alt+D")
        self.assertEqual(self.keys.tried, ["Ctrl+Alt+S", "Ctrl+Alt+D"])
        self.assertEqual(hotkey.describe(chord), "Ctrl+Alt+D (Ctrl+Alt+S is taken by another program)")

    def test_when_both_are_taken_there_is_no_hotkey_and_the_window_still_works(self):
        chord = self.make(taken={"Ctrl+Alt+S", "Ctrl+Alt+D"})
        self.assertIsNone(chord)
        self.assertEqual(hotkey.describe(chord), "no hotkey (Ctrl+Alt+S and Ctrl+Alt+D are both taken)")
        self.assertEqual(self.keys.registered, [])

    def test_a_combination_in_use_by_the_assistant_is_never_tried(self):
        chord = self.make(avoid=["ctrl+alt+s"])
        self.assertEqual(chord.label, "Ctrl+Alt+D")
        self.assertEqual(self.keys.tried, ["Ctrl+Alt+D"])

    def test_the_assistants_own_hotkey_is_not_one_of_the_choices(self):
        labels = {c.label.lower() for c in hotkey.CHORDS}
        self.assertNotIn(Settings().hotkey, labels)

    def test_an_action_that_fails_does_not_end_the_hotkey(self):
        calls = []

        def on_press():
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError("boom")
        keys = FakeKeys()
        hot = hotkey.Hotkey(on_press, keys=keys)
        self.addCleanup(hot.stop)
        hot.start()
        with self.assertLogs("aj.speak.hotkey", "ERROR"):
            keys.press()
            keys.press()
            self.assertTrue(wait_until(lambda: len(calls) == 2))

    def test_stopping_lets_go_of_the_hotkey(self):
        self.make()
        self.hot.stop()
        self.assertEqual(self.keys.unregistered, [hotkey._ID])

    def test_a_failure_to_set_it_up_is_not_a_crash(self):
        with self.assertLogs("aj.speak.hotkey", "ERROR"):
            self.assertIsNone(self.make(explode=True))


# ---- listening --------------------------------------------------------------------------------------

class ListenTests(unittest.TestCase):
    def record(self, script, blocks=None, settings=None, finish=None, cancelled=lambda: False, levels=None, **kw):
        mic = FakeMic(len(script) + 40 if blocks is None else blocks, amplitude=0.05)
        vad = FakeVad(script)
        rec = listen.record(mic, vad, settings or DictationSettings(), finish or threading.Event(), cancelled,
                            on_level=None if levels is None else levels.append, **kw)
        return rec, vad

    def test_it_stops_after_the_quiet_the_settings_ask_for(self):
        rec, vad = self.record([True] * 10 + [False] * 80)
        self.assertEqual(rec.reason, listen.SILENCE)
        self.assertEqual(len(rec.pieces), 1)
        # 1.5 s of quiet in all, 0.3 s of which the detector had already waited before it said so
        self.assertAlmostEqual(rec.secs, 10 * BLOCK_SECS + 1.5 - 0.3 + BLOCK_SECS, delta=0.1)
        self.assertEqual(vad.fed, round(rec.secs / BLOCK_SECS))
        self.assertEqual(vad.resets, 1)

    def test_the_quiet_is_the_settings_silence_secs(self):
        rec, _ = self.record([True] * 10 + [False] * 120, settings=DictationSettings(silence_secs=3.0))
        self.assertEqual(rec.reason, listen.SILENCE)
        self.assertAlmostEqual(rec.secs, 10 * BLOCK_SECS + 3.0 - 0.3 + BLOCK_SECS, delta=0.1)

    def test_a_pause_in_the_middle_of_a_sentence_does_not_end_it(self):
        rec, _ = self.record([True] * 10 + [False] * 25 + [True] * 10 + [False] * 80)  # 0.8 s pause
        self.assertEqual(rec.reason, listen.SILENCE)
        self.assertEqual(len(rec.pieces), 2)

    def test_nothing_said_ends_after_start_secs_without_pieces(self):
        rec, _ = self.record([False] * 100, settings=DictationSettings(start_secs=0.5))
        self.assertEqual(rec.reason, listen.NOTHING)
        self.assertEqual(rec.pieces, [])
        self.assertAlmostEqual(rec.secs, 0.5, delta=0.05)

    def test_a_second_click_finishes_it_and_keeps_the_speech_still_open(self):
        finish = threading.Event()
        finish.set()
        mic = FakeMic(0)
        vad = FakeVad([True] * 5)
        vad.hearing = True  # mid-word when he clicked
        rec = listen.record(mic, vad, DictationSettings(), finish, lambda: False)
        self.assertEqual(rec.reason, listen.FINISHED)
        self.assertEqual(len(rec.pieces), 1)  # flushed: the last words are not lost
        self.assertEqual(vad.flushes, 1)

    def test_the_cap_ends_it_and_keeps_what_was_said(self):
        rec, vad = self.record([True] * 200, settings=DictationSettings(max_secs=0.64), blocks=200)
        self.assertEqual(rec.reason, listen.CAPPED)
        self.assertAlmostEqual(rec.secs, 0.64, delta=0.04)
        self.assertEqual(len(rec.pieces), 1)

    def test_cancelling_ends_it_and_keeps_nothing(self):
        rec, vad = self.record([True] * 50, cancelled=lambda: True)
        self.assertEqual(rec.reason, listen.CANCELLED)
        self.assertEqual(rec.pieces, [])
        self.assertEqual(vad.flushes, 0)

    def test_a_microphone_that_sends_nothing_has_stalled(self):
        started = time.monotonic()
        rec, _ = self.record([], blocks=0, stall_secs=0.25)
        self.assertEqual(rec.reason, listen.STALLED)
        self.assertEqual(rec.pieces, [])
        self.assertLess(time.monotonic() - started, 2.0)

    def test_the_level_bar_gets_values_between_0_and_1_and_ends_at_0(self):
        levels: list[float] = []
        self.record([True] * 10 + [False] * 80, levels=levels)
        self.assertGreater(len(levels), 10)
        self.assertTrue(all(0.0 <= v <= 1.0 for v in levels))
        self.assertGreater(max(levels), 0.2)  # 0.05 is a clear voice
        self.assertEqual(levels[-1], 0.0)

    def test_level_of_a_block(self):
        self.assertEqual(listen.level_of(block(0.0)), 0.0)
        self.assertLess(listen.level_of(block(0.002)), listen.level_of(block(0.05)))
        self.assertEqual(listen.level_of(block(1.0)), 1.0)
        self.assertEqual(listen.level_of(np.zeros(0, np.float32)), 0.0)

    @unittest.skipUnless(Path(Settings().voice.vad_model).exists(), "the Silero model isn't here")
    def test_the_real_detector_loads_hears_silence_as_nothing_and_flushes(self):
        vad = listen.Endpointer(Settings().voice)
        for _ in range(60):
            self.assertEqual(vad.feed(block(0.0)), [])
        self.assertFalse(vad.hearing_speech)
        self.assertEqual(vad.flush(), [])
        vad.reset()


# ---- what the window says about a result ----------------------------------------------------------

class VerdictTests(unittest.TestCase):
    def outcome(self, ok=True, reason="typed", line="Typed it."):
        return dictation.Outcome(ok, reason, line, "x", APP)

    def test_a_plain_success_is_just_typed(self):
        self.assertEqual(sp.verdict(self.outcome()), (sp.TYPED, "Typed"))

    def test_offline_ears_say_to_check_it(self):
        state, text = sp.verdict(self.outcome(), engine="offline")
        self.assertEqual(state, sp.CHECK)
        self.assertEqual(text, "Typed. Gemini unavailable, used offline ears: check it.")

    def test_a_polish_that_failed_says_so(self):
        self.assertEqual(sp.verdict(self.outcome(), how="unpolished"), (sp.CHECK, "Typed. Couldn't polish it."))

    def test_the_notes_add_up(self):
        state, text = sp.verdict(self.outcome(), engine="offline", how="unpolished", capped_at=60.0)
        self.assertEqual(state, sp.CHECK)
        self.assertIn("offline ears", text)
        self.assertIn("polish", text)
        self.assertIn("60 seconds", text)

    def test_nothing_in_a_success_says_sent(self):
        for engine in ("gemini", "offline"):
            for how in ("plain", "polished", "unpolished"):
                self.assertNotIn("sent", sp.verdict(self.outcome(), engine, how)[1].lower())

    def test_not_in_a_text_box_asks_him_to_click_into_one(self):
        for reason in ("no_window", "no_focus", "not_editable", "unknown_field"):
            self.assertEqual(sp.verdict(self.outcome(False, reason, dictation.CLICK_FIRST)),
                             (sp.ERROR, "Click into a text box first."), reason)

    def test_the_cores_own_line_is_shown_for_every_other_failure(self):
        for reason, line in (("moved", dictation.MOVED_AWAY), ("refused", dictation.REFUSED),
                             ("empty", dictation.NOTHING_TO_TYPE)):
            self.assertEqual(sp.verdict(self.outcome(False, reason, line)), (sp.ERROR, line), reason)

    def test_no_line_tells_him_to_say_type_that(self):
        self.assertNotIn("type that", sp.CLICK_FIRST)  # that is the assistant's voice, not this window


# ---- one dictation, start to finish ------------------------------------------------------------------

class SpeakerTests(unittest.TestCase):
    def test_nothing_is_opened_until_he_clicks(self):
        rig = Rig(self)
        self.assertEqual((rig.mics_made, rig.mic.opened, rig.ears_made), (0, 0, 0))
        self.assertFalse(rig.speaker.busy)
        self.assertEqual(rig.speaker.state, sp.READY)

    def test_a_dictation_is_listened_for_written_down_and_typed(self):
        rig = Rig(self)
        self.assertEqual(rig.go(), (sp.TYPED, "Typed"))
        self.assertEqual(rig.states, [sp.LISTENING, sp.TRANSCRIBING, sp.TYPING, sp.TYPED])
        self.assertEqual(rig.desktop.text, SAID)
        self.assertEqual(rig.desktop.focused, [APP, APP])  # the check at the start, then typing
        self.assertEqual(len(rig.gemini_calls), 1)
        self.assertEqual(rig.gemini_calls[0]["model"], DictationSettings().gemini_model)
        self.assertIn("text box", rig.gemini_calls[0]["hint"])
        self.assertEqual(rig.polish_calls, [])
        self.assertFalse(rig.speaker.busy)

    def test_the_microphone_is_open_only_while_listening(self):
        rig = Rig(self)
        rig.go()
        self.assertEqual((rig.mics_made, rig.mic.opened, rig.mic.closed), (1, 1, 1))

    def test_the_microphone_is_closed_even_when_it_goes_wrong(self):
        rig = Rig(self, editable=False)
        self.assertEqual(rig.go()[0], sp.ERROR)
        self.assertEqual((rig.mic.opened, rig.mic.closed), (1, 1))

    def test_the_parakeet_is_not_loaded_when_gemini_works(self):
        rig = Rig(self)
        rig.go()
        self.assertEqual(rig.ears_made, 0)

    def test_the_level_bar_is_fed_while_it_listens(self):
        rig = Rig(self)
        rig.rearm(amplitude=0.05)
        rig.go()
        self.assertGreater(len(rig.levels), 5)
        self.assertEqual(rig.levels[-1], 0.0)

    # -- where it types ------------------------------------------------------------------------

    def test_it_types_into_the_window_that_was_in_front(self):
        rig = Rig(self, front=CHROME)
        rig.go()
        self.assertEqual(rig.desktop.focused, [CHROME, CHROME])
        self.assertEqual(rig.desktop.text, SAID)

    def test_it_never_types_into_its_own_window(self):
        rig = Rig(self, front=OURS, last_other=CHROME)  # its own window is in front: the one before it is meant
        rig.go()
        self.assertEqual(rig.desktop.focused, [CHROME, CHROME])
        self.assertNotIn(OURS, rig.desktop.focused)
        self.assertEqual(rig.final, (sp.TYPED, "Typed"))

    def test_with_only_its_own_window_there_is_nowhere_to_type_and_it_does_not_listen(self):
        rig = Rig(self, front=OURS, last_other=0)
        rig.speaker.toggle()
        self.assertEqual(rig.events, [(sp.ERROR, sp.CLICK_FIRST)])
        self.assertEqual((rig.mics_made, rig.desktop.typed), (0, []))
        self.assertFalse(rig.speaker.busy)

    def test_a_whatsapp_chat_is_told_to_gemini_as_one(self):
        rig = Rig(self, front=WHATSAPP)
        rig.go()
        self.assertIn("WhatsApp", rig.gemini_calls[0]["hint"])

    def test_the_place_is_fixed_when_he_starts_not_when_it_types(self):
        rig = Rig(self, front=APP)
        rig.gemini_gate.clear()
        rig.speaker.toggle()
        self.assertTrue(wait_until(lambda: rig.gemini_calls))
        rig.desktop.front_hwnd = CHROME  # he clicked elsewhere while Gemini was thinking
        rig.gemini_gate.set()
        rig.assert_done()
        self.assertEqual(rig.desktop.focused[-1], APP)
        self.assertEqual(rig.final, (sp.TYPED, "Typed"))

    def test_the_cursor_not_being_in_a_text_box_is_found_before_he_talks(self):
        rig = Rig(self, editable=False)
        self.assertEqual(rig.go(), (sp.ERROR, "Click into a text box first."))
        self.assertEqual(rig.gemini_calls, [])
        self.assertEqual(rig.desktop.typed, [])
        self.assertNotIn(sp.LISTENING, rig.states)

    def test_uia_not_knowing_counts_as_not_a_text_box(self):
        rig = Rig(self, editable=None)  # uia.focused_editable() is None when it can't tell
        self.assertEqual(rig.go(), (sp.ERROR, "Click into a text box first."))
        self.assertEqual(rig.desktop.typed, [])

    def test_the_box_going_away_while_he_talks_shows_the_honest_line(self):
        rig = Rig(self, editable=[True, False])  # fine at the start, gone when it types
        self.assertEqual(rig.go(), (sp.ERROR, "Click into a text box first."))
        self.assertEqual(rig.desktop.typed, [])

    def test_moving_to_another_window_while_it_types_shows_the_cores_line(self):
        rig = Rig(self, said="word " * 40)  # long enough to be typed in several pieces
        rig.desktop.on_type = lambda: setattr(rig.desktop, "front_hwnd", CHROME)
        self.assertEqual(rig.go(), (sp.ERROR, dictation.MOVED_AWAY))
        self.assertGreater(len(rig.desktop.typed), 0)
        self.assertLess(len(rig.desktop.text), len(("word " * 40).strip()))

    # -- Gemini, the offline ears and the polish ----------------------------------------------------

    def test_without_gemini_the_offline_ears_stand_in_and_he_is_told_to_check(self):
        rig = Rig(self, gemini_down=True)
        state, text = rig.go()
        self.assertEqual((state, text), (sp.CHECK, "Typed. Gemini unavailable, used offline ears: check it."))
        self.assertEqual(rig.ears_made, 1)
        self.assertEqual(rig.desktop.text, "hi amma i'll be home by seven thirty")  # as Parakeet wrote it

    def test_the_offline_ears_are_loaded_once_and_let_go_when_gemini_is_back(self):
        rig = Rig(self, gemini_down=True)
        rig.go()
        rig.events.clear()
        rig.rearm()
        rig.go()
        self.assertEqual(rig.ears_made, 1)  # kept while Gemini stays down
        self.assertIsNotNone(rig.speaker._ears)
        rig.events.clear()
        rig.gemini_down = False
        rig.rearm()
        rig.go()
        self.assertEqual(rig.final, (sp.TYPED, "Typed"))
        self.assertIsNone(rig.speaker._ears)  # 0.7 GB given back

    def test_no_gemini_and_no_offline_ears_says_so_and_types_nothing(self):
        rig = Rig(self, gemini_down=True, ears_fails=True)
        with self.assertLogs("aj.speak.speaker", "ERROR"):
            self.assertEqual(rig.go(), (sp.ERROR, sp.NO_EARS))
        self.assertEqual(rig.desktop.typed, [])

    def test_no_words_in_the_recording_types_nothing(self):
        rig = Rig(self, said="", gemini_down=False, ears_text="")
        self.assertEqual(rig.go(), (sp.ERROR, sp.NO_WORDS))
        self.assertEqual(rig.desktop.typed, [])

    def test_polite_off_never_asks_for_a_rewrite(self):
        rig = Rig(self)
        rig.speaker.polite = False
        rig.go()
        self.assertEqual(rig.polish_calls, [])
        self.assertEqual(rig.desktop.text, SAID)
        self.assertNotIn(sp.POLISHING, rig.states)

    def test_polite_on_types_the_polished_words(self):
        rig = Rig(self)
        rig.speaker.polite = True
        self.assertEqual(rig.go(), (sp.TYPED, "Typed"))
        self.assertEqual(rig.polish_calls, [SAID])
        self.assertEqual(rig.desktop.text, POLISHED)
        self.assertEqual(rig.states, [sp.LISTENING, sp.TRANSCRIBING, sp.POLISHING, sp.TYPING, sp.TYPED])

    def test_polite_that_fails_types_it_as_said_and_tells_him(self):
        rig = Rig(self, polish_down=True)
        rig.speaker.polite = True
        self.assertEqual(rig.go(), (sp.CHECK, "Typed. Couldn't polish it."))
        self.assertEqual(rig.desktop.text, SAID)

    def test_the_toggle_is_read_when_it_matters_not_when_he_clicked(self):
        rig = Rig(self)
        rig.gemini_gate.clear()
        rig.speaker.toggle()
        self.assertTrue(wait_until(lambda: rig.gemini_calls))
        rig.speaker.polite = True  # he switched Polite on while Gemini was writing it down
        rig.gemini_gate.set()
        rig.assert_done()
        self.assertEqual(rig.desktop.text, POLISHED)

    # -- ending it early --------------------------------------------------------------------------

    def test_a_second_click_ends_the_listening_and_types_what_was_said(self):
        rig = Rig(self, talk=15, quiet=0)  # he is talking and never stops: the sound just runs out
        rig.speaker.toggle()
        self.assertTrue(wait_until(lambda: rig.vad.fed == 15 and sp.LISTENING in rig.states))
        self.assertTrue(rig.speaker.busy)
        rig.speaker.toggle()  # the mic button again, or the hotkey again
        rig.assert_done()
        self.assertEqual(rig.final, (sp.TYPED, "Typed"))
        self.assertEqual(rig.desktop.text, SAID)
        self.assertEqual(rig.vad.flushes, 1)

    def test_a_second_click_while_writing_it_down_does_nothing(self):
        rig = Rig(self)
        rig.gemini_gate.clear()
        rig.speaker.toggle()
        self.assertTrue(wait_until(lambda: rig.gemini_calls))
        rig.speaker.toggle()
        rig.speaker.toggle()
        rig.gemini_gate.set()
        rig.assert_done()
        self.assertEqual((rig.mics_made, len(rig.gemini_calls)), (1, 1))  # no second dictation started
        self.assertEqual(rig.desktop.text, SAID)

    def test_the_cap_types_what_was_said_and_tells_him(self):
        rig = Rig(self, talk=500, quiet=0, settings=Settings(dictation=DictationSettings(max_secs=0.64)))
        rig.mic = FakeMic(500)  # he talks on and on
        state, text = rig.go()
        self.assertEqual(state, sp.CHECK)
        self.assertIn("Stopped listening at 0.64 seconds", text)
        self.assertEqual(rig.desktop.text, SAID)

    def test_nothing_said_costs_no_gemini_call(self):
        rig = Rig(self, talk=0, quiet=200, settings=Settings(dictation=DictationSettings(start_secs=0.5)))
        self.assertEqual(rig.go(), (sp.ERROR, sp.NOTHING_HEARD))
        self.assertEqual(rig.gemini_calls, [])
        self.assertEqual(rig.desktop.typed, [])

    def test_cancel_while_listening_types_nothing(self):
        rig = Rig(self, live_mic=True)  # a microphone that stays silent: it keeps listening
        rig.speaker.toggle()
        self.assertTrue(wait_until(lambda: sp.LISTENING in rig.states))
        self.assertTrue(rig.speaker.cancel())
        self.assertEqual(rig.final, (sp.CANCELLED, sp.CANCELLED_TEXT))
        self.assertFalse(rig.speaker.busy)
        join_jobs()
        self.assertEqual(rig.mic.closed, 1)
        self.assertEqual((rig.gemini_calls, rig.desktop.typed), ([], []))
        self.assertEqual(rig.states, [sp.LISTENING, sp.CANCELLED])

    def test_cancel_while_gemini_is_thinking_is_instant_and_types_nothing_later(self):
        rig = Rig(self)
        rig.gemini_gate.clear()
        rig.speaker.toggle()
        self.assertTrue(wait_until(lambda: rig.gemini_calls))
        self.assertTrue(rig.speaker.cancel())
        self.assertEqual(rig.final[0], sp.CANCELLED)  # without waiting for Gemini
        rig.gemini_gate.set()
        join_jobs()
        self.assertEqual(rig.desktop.typed, [])
        self.assertEqual(rig.states[-1], sp.CANCELLED)  # the late answer changed nothing

    def test_cancel_while_polishing_types_nothing(self):
        rig = Rig(self)
        rig.speaker.polite = True
        rig.polish_gate.clear()
        rig.speaker.toggle()
        self.assertTrue(wait_until(lambda: rig.polish_calls))
        self.assertTrue(rig.speaker.cancel())
        rig.polish_gate.set()
        join_jobs()
        self.assertEqual(rig.desktop.typed, [])
        self.assertEqual(rig.states[-1], sp.CANCELLED)

    def test_it_is_too_late_to_cancel_once_the_keys_are_going_in(self):
        rig = Rig(self)
        rig.desktop.type_gate = threading.Event()
        self.addCleanup(rig.desktop.type_gate.set)
        rig.speaker.toggle()
        self.assertTrue(wait_until(lambda: sp.TYPING in rig.states))
        self.assertFalse(rig.speaker.cancel())
        self.assertTrue(rig.speaker.busy)
        rig.desktop.type_gate.set()
        rig.assert_done()
        self.assertEqual(rig.final, (sp.TYPED, "Typed"))
        self.assertEqual(rig.desktop.text, SAID)

    def test_cancel_with_nothing_going_on_does_nothing(self):
        rig = Rig(self)
        self.assertFalse(rig.speaker.cancel())
        self.assertEqual(rig.events, [])

    def test_a_cancelled_dictation_cannot_touch_the_next_one(self):
        rig = Rig(self)
        rig.gemini_gate.clear()
        rig.speaker.toggle()
        self.assertTrue(wait_until(lambda: rig.gemini_calls))
        rig.speaker.cancel()
        rig.rearm()  # his next dictation
        rig.said = "Second one."
        rig.speaker.toggle()  # while the first one's Gemini call is still waiting
        self.assertTrue(wait_until(lambda: sp.LISTENING in rig.states[rig.states.index(sp.CANCELLED):]))
        rig.gemini_gate.set()
        rig.assert_done()
        self.assertEqual(rig.desktop.text, "Second one.")  # typed once, and it is the second
        self.assertEqual(rig.states.count(sp.TYPED), 1)

    # -- what goes wrong ---------------------------------------------------------------------------

    def test_a_microphone_that_will_not_open_is_reported_and_logged(self):
        rig = Rig(self, mic_fails=True)
        with self.assertLogs("aj.speak.speaker", "ERROR"):
            self.assertEqual(rig.go(), (sp.ERROR, sp.NO_MIC))
        self.assertEqual(rig.desktop.typed, [])

    def test_a_voice_detector_that_will_not_load_is_reported_and_logged(self):
        rig = Rig(self, vad_fails=True)
        with self.assertLogs("aj.speak.speaker", "ERROR"):
            self.assertEqual(rig.go(), (sp.ERROR, sp.NO_VAD))
        self.assertEqual(rig.mic.closed, 1)

    def test_a_surprise_becomes_a_line_and_a_log_entry_not_a_dead_window(self):
        rig = Rig(self)
        rig.speaker._type_into = mock.Mock(side_effect=RuntimeError("boom"))
        with self.assertLogs("aj.speak.speaker", "ERROR"):
            self.assertEqual(rig.go(), (sp.ERROR, sp.SOMETHING_WRONG))
        self.assertFalse(rig.speaker.busy)

    def test_after_an_error_the_next_click_starts_a_new_dictation(self):
        rig = Rig(self, editable=[False, True])
        self.assertEqual(rig.go()[0], sp.ERROR)
        rig.events.clear()
        rig.rearm()
        self.assertEqual(rig.go(), (sp.TYPED, "Typed"))

    # -- the promises ----------------------------------------------------------------------------

    def test_close_cancels_and_stops_the_front_window_watch(self):
        rig = Rig(self, live_mic=True)
        rig.speaker.start()
        self.assertTrue(rig.watch.started)
        rig.speaker.toggle()
        self.assertTrue(wait_until(lambda: sp.LISTENING in rig.states))
        rig.speaker.close()
        self.assertTrue(rig.watch.stopped)
        self.assertEqual(rig.states[-1], sp.CANCELLED)

    def test_what_he_said_is_never_logged(self):
        phrase = "zebra quartz 7731"
        records: list[logging.LogRecord] = []

        class Keep(logging.Handler):
            def emit(self, record):
                records.append(record)

        root = logging.getLogger()
        handler, old = Keep(logging.DEBUG), root.level
        root.addHandler(handler)
        root.setLevel(logging.DEBUG)
        self.addCleanup(root.removeHandler, handler)
        self.addCleanup(root.setLevel, old)
        rig = Rig(self, gemini_down=True, ears_text=phrase)  # the offline path logs the most
        rig.speaker.polite = True
        rig.polish_down = True
        rig.go()
        self.assertEqual(rig.desktop.text, phrase)
        self.assertTrue(records)  # it does log the problems
        for record in records:
            self.assertNotIn("zebra", record.getMessage() + str(record.exc_info))


class PromiseTests(unittest.TestCase):
    """Never Enter, never the clipboard, never sent: through the real keyboard layer, with SendInput
    replaced by a recorder, so no key reaches the desktop."""

    def test_a_message_with_line_breaks_is_typed_with_no_bare_enter(self):
        sent: list[tuple] = []

        def fake_send(count, array, size):
            for i in range(count):
                ki = array[i].ki
                sent.append((ki.wVk, ki.dwFlags & 0x2 == 0, ki.dwFlags & 0x4 != 0, ki.wScan))
            return count

        class KeysDesktop(FakeDesktop):
            def type(self, text, multiline=False):
                keyboard.type_text(text, multiline=multiline)

            def focused_box(self):  # Notepad's text area: a document, so it takes line breaks
                return Node(control_type="Document", has_text=True, focusable=True, runtime_id=(5, 2))

        us = {" ": (0x20, False), ",": (0xBC, False), ".": (0xBE, False)}
        for patch in (mock.patch.object(keyboard, "_send_input", fake_send),
                      mock.patch.object(keyboard, "_key_for", lambda c, layout: us.get(c) or (
                          (ord(c.upper()), c.isupper()) if c.isascii() and c.isalpha() else None)),
                      mock.patch.object(keyboard, "_layout", lambda: 0),
                      mock.patch.object(keyboard, "_caps_lock_on", lambda: False),
                      mock.patch.object(keyboard, "_key_held", lambda vk: False),
                      mock.patch.object(keyboard, "screen_locked", lambda: False),
                      mock.patch.object(keyboard.time, "sleep", lambda s: None)):
            patch.start()
            self.addCleanup(patch.stop)
        rig = Rig(self, said="Hi Amma.\nSee you soon, Appa.")
        rig.desktop = KeysDesktop(APP, True)
        rig.speaker.desktop = rig.desktop
        self.assertEqual(rig.go(), (sp.TYPED, "Typed"))
        self.assertTrue(sent)
        shift_down = False
        for vk, down, unicode_event, scan in sent:
            if vk == 0x10:
                shift_down = down
            if vk == 0x0D:
                self.assertTrue(shift_down, "Enter was pressed without Shift")
            if unicode_event:
                self.assertNotIn(scan, (0x0A, 0x0D))
        self.assertIn(0x0D, [vk for vk, *_ in sent])  # the line break itself went in, as Shift+Enter

    def test_no_key_is_ever_pressed_by_the_window_itself(self):
        with mock.patch.object(keyboard, "press", side_effect=AssertionError("a key was pressed")) as press:
            rig = Rig(self)
            rig.speaker.polite = True
            rig.go()
            rig2 = Rig(self, editable=False)
            rig2.go()
        press.assert_not_called()
        self.assertEqual(rig.desktop.backspaces, 0)

    def test_the_code_has_no_enter_clipboard_or_send(self):
        """Read as code (docstrings and comments, which say these things in the negative, don't count)."""
        import ast
        import aj.speak as package
        root = Path(inspect.getsourcefile(package)).parent
        names = {"press", "type_text", "pyperclip", "keybd_event", "sendinput", "vk_return", "_vk_return"}
        for path in sorted(root.glob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            docs = {id(n.body[0].value) for n in ast.walk(tree)
                    if isinstance(n, (ast.Module, ast.ClassDef, ast.FunctionDef)) and n.body
                    and isinstance(n.body[0], ast.Expr) and isinstance(n.body[0].value, ast.Constant)}
            for node in ast.walk(tree):
                if isinstance(node, ast.Name):
                    word = node.id
                elif isinstance(node, ast.Attribute):
                    word = node.attr
                elif isinstance(node, ast.alias):
                    word = node.name.split(".")[-1]
                elif isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docs:
                    self.assertNotIn(node.value.strip().lower(), ("enter", "return"), f"{path.name}: a key name")
                    continue
                else:
                    continue
                self.assertNotIn(word.lower(), names, f"{path.name} uses {word}")
                self.assertNotIn("clipboard", word.lower(), f"{path.name} uses {word}")

    def test_only_the_dictation_core_types(self):
        rig = Rig(self)
        rig.go()
        self.assertEqual(rig.desktop.backspaces, 0)
        self.assertEqual(rig.desktop.text, SAID)


# ---- the window -------------------------------------------------------------------------------------

class FakeSpeaker:
    """What the window needs of a Speaker, recording the calls."""

    def __init__(self):
        self.calls: list[str] = []
        self.busy = False
        self.polite = False
        self.cancel_works = True
        self.on_state = self.on_level = None

    def toggle(self):
        self.calls.append("toggle")

    def listen(self):
        self.calls.append("listen")
        return True

    def cancel(self):
        self.calls.append("cancel")
        return self.cancel_works

    def close(self):
        self.calls.append("close")


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


SCREEN = (0, 0, 1920, 1040)


@unittest.skipIf(QApplication is None, "PyQt6 is not installed")
class WindowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.dir = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.path = self.dir / "speak_window.json"
        self.clock = Clock()
        self.speaker = FakeSpeaker()
        self.enterContext(mock.patch.object(win, "screen_rects", lambda: [SCREEN]))
        self.raised = self.enterContext(mock.patch.object(win.Win32Window, "raise_topmost"))

    def make(self, saved=None, speaker=None):
        w = SpeakWindow(speaker or self.speaker, saved, store_path=self.path, screens=[SCREEN], clock=self.clock)
        self.quits = []
        w.quit_requested.connect(lambda: self.quits.append(1))
        self.addCleanup(w.deleteLater)
        self.addCleanup(w.hide)
        return w

    def pump(self, until=lambda: True, timeout=3.0) -> bool:
        deadline = time.perf_counter() + timeout
        while time.perf_counter() < deadline:
            self.app.processEvents()
            if until():
                return True
            time.sleep(0.005)
        self.app.processEvents()
        return until()

    # -- it must not take the focus ----------------------------------------------------------------

    def test_it_is_a_non_activating_always_on_top_tool_window(self):
        w = self.make()
        flags = w.windowFlags()
        for flag in (Qt.WindowType.Tool, Qt.WindowType.WindowStaysOnTopHint, Qt.WindowType.WindowDoesNotAcceptFocus,
                     Qt.WindowType.FramelessWindowHint):
            self.assertEqual(flags & flag, flag, flag)
        self.assertTrue(w.testAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating))
        self.assertTrue(w.testAttribute(Qt.WidgetAttribute.WA_AlwaysShowToolTips))  # it is never the active window
        self.assertEqual(w.windowTitle(), "AJ (Speak)")
        self.assertEqual((w.width(), w.height()), (320, 76))

    def test_nothing_in_it_can_take_keyboard_focus(self):
        w = self.make()
        for part in (w.mic, w.close_button, w.polite_button):
            self.assertEqual(part.focusPolicy(), Qt.FocusPolicy.NoFocus, part)
        w.show()
        self.assertIsNone(self.app.focusWidget())

    def test_the_real_window_gets_noactivate_and_toolwindow_when_created_and_when_shown(self):
        with mock.patch.object(win, "make_no_activate") as make:
            w = self.make()
            make.assert_called_with(int(w.winId()))
            make.reset_mock()
            w.show()
            make.assert_called_with(int(w.winId()))

    def test_make_no_activate_sets_the_two_styles_and_clears_appwindow(self):
        class Styles:
            def __init__(self, style):
                self.style, self.set_to = style, None

            def ex_style(self, hwnd):
                return self.style

            def set_ex_style(self, hwnd, style):
                self.set_to = style

        api = Styles(win.WS_EX_APPWINDOW | 0x100)
        self.assertTrue(win.make_no_activate(5, api))
        self.assertEqual(api.set_to & win.WS_EX_NOACTIVATE, win.WS_EX_NOACTIVATE)
        self.assertEqual(api.set_to & win.WS_EX_TOOLWINDOW, win.WS_EX_TOOLWINDOW)
        self.assertEqual(api.set_to & win.WS_EX_APPWINDOW, 0)
        self.assertEqual(api.set_to & 0x100, 0x100)  # the styles it already had are kept
        done = Styles(win.WS_EX_NOACTIVATE | win.WS_EX_TOOLWINDOW)
        self.assertTrue(win.make_no_activate(5, done))
        self.assertIsNone(done.set_to)  # nothing to change

    def test_make_no_activate_is_soft_about_a_handle_that_is_not_a_window(self):
        class Nothing:
            def ex_style(self, hwnd):
                return 0

        class Broken:
            def ex_style(self, hwnd):
                raise OSError("bad handle")

        self.assertFalse(win.make_no_activate(5, Nothing()))
        with self.assertLogs("aj.speak.window", "ERROR"):
            self.assertFalse(win.make_no_activate(5, Broken()))

    def test_the_real_style_calls_work_on_a_real_window_handle_of_ours_without_showing_anything(self):
        """GetWindowLongPtrW/SetWindowLongPtrW with the 64-bit types, on a window this test makes and
        never shows (a message-only window), not on the desktop."""
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.CreateWindowExW.restype = ctypes.c_void_p
        user32.CreateWindowExW.argtypes = [ctypes.c_uint32, ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint32,
                                           ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_void_p,
                                           ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
        user32.DestroyWindow.argtypes = [ctypes.c_void_p]
        hwnd = user32.CreateWindowExW(0x80, "Static", "t", 0, 0, 0, 0, 0, ctypes.c_void_p(-3), None, None, None)  # HWND_MESSAGE
        if not hwnd:
            self.skipTest("could not make a message-only window")
        self.addCleanup(user32.DestroyWindow, hwnd)
        real = win.Win32Window()
        self.assertTrue(win.make_no_activate(hwnd, real))
        style = real.ex_style(hwnd)
        self.assertEqual(style & win.WS_EX_NOACTIVATE, win.WS_EX_NOACTIVATE)
        self.assertEqual(style & win.WS_EX_TOOLWINDOW, win.WS_EX_TOOLWINDOW)

    # -- what it shows -----------------------------------------------------------------------------

    def test_it_starts_ready_and_says_the_hotkey(self):
        w = self.make()
        w.set_hotkey("Ctrl+Alt+S", "Ctrl+Alt+S")
        self.assertEqual(w.shown, (sp.READY, ""))
        self.assertEqual(w.caption, "Ready")
        self.assertIn("Ctrl+Alt+S", w.label.text())
        self.assertIn("Ctrl+Alt+S", w.mic.toolTip())
        self.assertIn("Ctrl+Alt+S", w.toolTip())

    def test_the_tooltip_says_which_hotkey_is_live_when_the_first_was_taken(self):
        w = self.make()
        w.set_hotkey("Ctrl+Alt+D", hotkey.describe(hotkey.CHORDS[1]))
        self.assertIn("Ctrl+Alt+D (Ctrl+Alt+S is taken by another program)", w.mic.toolTip())
        self.assertIn("Ctrl+Alt+D", w.label.text())
        w.set_hotkey("", hotkey.describe(None))
        self.assertIn("no hotkey", w.toolTip())
        self.assertNotIn("or press", w.mic.toolTip())

    def test_every_state_has_its_words_and_look(self):
        w = self.make()
        for state, caption in ((sp.LISTENING, "Listening"), (sp.TRANSCRIBING, "Transcribing"),
                               (sp.POLISHING, "Polishing"), (sp.TYPING, "Typing")):
            w.show_state(state)
            self.assertEqual(w.caption, caption)
        w.show_state(sp.LISTENING)
        self.assertEqual(w.mic.mode, win.RecordButton.LISTENING)
        self.assertFalse(w.bar.isHidden())
        self.assertTrue(w.bar.active)
        w.show_state(sp.TRANSCRIBING)
        self.assertEqual(w.mic.mode, win.RecordButton.BUSY)
        self.assertTrue(w.bar.isHidden())
        w.show_state(sp.TYPED, "Typed")
        self.assertEqual((w.caption, w.mic.mode), ("Typed", win.RecordButton.READY))

    def test_a_result_shows_the_honest_line_in_full(self):
        w = self.make()
        w.show_state(sp.ERROR, dictation.MOVED_AWAY)
        self.assertEqual(w.caption, dictation.MOVED_AWAY)
        self.assertIn("left that box", w.label.text())
        self.assertEqual(w.label.toolTip(), dictation.MOVED_AWAY)
        w.show_state(sp.CHECK, "Typed. Gemini unavailable, used offline ears: check it.")
        self.assertIn("offline ears", w.caption)

    def test_a_result_goes_back_to_ready_when_nothing_is_going_on(self):
        w = self.make()
        w.show_state(sp.TYPED, "Typed")
        self.assertTrue(w._revert.isActive())
        w._back_to_ready()
        self.assertEqual(w.shown, (sp.READY, ""))
        w.show_state(sp.LISTENING)
        self.assertFalse(w._revert.isActive())

    def test_a_result_stays_while_a_new_dictation_is_running(self):
        w = self.make()
        w.show_state(sp.ERROR, "x")
        self.speaker.busy = True
        w._back_to_ready()
        self.assertEqual(w.shown[0], sp.ERROR)

    def test_errors_stay_longer_than_a_success(self):
        self.assertGreater(win.REVERT_MS[sp.ERROR], win.REVERT_MS[sp.TYPED])
        self.assertGreater(win.REVERT_MS[sp.CHECK], win.REVERT_MS[sp.TYPED])

    def test_the_level_bar_follows_the_levels_while_listening_only(self):
        w = self.make()
        w.bar.set_level(0.7)
        self.assertEqual(w.bar.level, 0.0)  # not listening: nothing to show
        w.show_state(sp.LISTENING)
        w.bar.set_level(0.7)
        self.assertEqual(w.bar.level, 0.7)
        w.bar.set_level(5)
        self.assertEqual(w.bar.level, 1.0)

    def test_the_window_paints_every_state_without_error(self):
        w = self.make()
        for state in (sp.READY, sp.LISTENING, sp.TRANSCRIBING, sp.TYPED, sp.CHECK, sp.ERROR, sp.CANCELLED):
            w.show_state(state, "text that is long enough to wrap onto a second and a third line of the label")
            self.assertFalse(w.grab().isNull(), state)

    # -- clicks --------------------------------------------------------------------------------------

    def test_the_mic_button_and_the_hotkey_both_toggle_the_speaker(self):
        w = self.make()
        w.mic.click()
        self.assertEqual(self.speaker.calls, ["toggle"])
        w.hotkey_pressed.emit()
        self.assertEqual(self.speaker.calls, ["toggle", "toggle"])

    def test_the_polite_switch_reaches_the_speaker_and_is_remembered(self):
        w = self.make()
        self.assertFalse(self.speaker.polite)
        w.polite_button.click()
        self.assertTrue(self.speaker.polite)
        self.assertEqual(store.load(self.path).get("polite"), True)
        w.polite_button.click()
        self.assertFalse(self.speaker.polite)
        self.assertEqual(store.load(self.path).get("polite"), False)

    def test_polite_comes_back_as_it_was_left(self):
        w = self.make({"polite": True})
        self.assertTrue(w.polite_button.isChecked())
        self.assertTrue(self.speaker.polite)

    def test_the_x_cancels_while_a_dictation_is_going(self):
        w = self.make()
        w.show()
        self.speaker.busy = True
        w.show_state(sp.LISTENING)
        w.close_button.click()
        self.assertEqual(self.speaker.calls, ["cancel"])
        self.assertEqual(self.quits, [])
        self.assertTrue(w.isVisible())
        self.assertIn("Cancel", w.close_button.toolTip())

    def test_the_x_too_late_to_cancel_does_not_close_the_window(self):
        w = self.make()
        w.show()
        self.speaker.busy, self.speaker.cancel_works = True, False  # the keys are going in
        w.show_state(sp.TYPING)
        w.close_button.click()
        self.assertEqual(self.quits, [])
        self.assertTrue(w.isVisible())

    def test_the_x_closes_the_window_when_nothing_is_going_on(self):
        w = self.make()
        w.show()
        self.assertIn("Close", w.close_button.toolTip())
        w.close_button.click()
        self.assertEqual(self.quits, [1])
        self.assertIn("close", self.speaker.calls)
        self.assertNotIn("cancel", self.speaker.calls)

    def test_the_x_just_after_a_dictation_ends_does_not_close_it_by_accident(self):
        w = self.make()
        w.show()
        self.speaker.busy = True
        w.show_state(sp.TRANSCRIBING)
        self.speaker.busy = False
        w.show_state(sp.TYPED, "Typed")  # it finished a split second before the click
        w.close_button.click()
        self.assertEqual(self.quits, [])
        self.assertTrue(w.isVisible())
        self.clock.now += win.QUIT_GUARD_SECS + 0.1
        w.close_button.click()
        self.assertEqual(self.quits, [1])

    def test_going_back_to_ready_does_not_restart_the_close_guard(self):
        w = self.make()
        w.show()
        w.show_state(sp.TYPED, "Typed")  # never busy in this window's eyes
        w._back_to_ready()
        w.close_button.click()
        self.assertEqual(self.quits, [1])

    def test_the_right_click_menu_shows_the_hotkey_and_quits(self):
        w = self.make()
        w.show()
        w.set_hotkey("Ctrl+Alt+S", "Ctrl+Alt+S")
        menu = w.menu()
        actions = [a for a in menu.actions()]
        self.assertEqual([a.text() for a in actions], ["Hotkey: Ctrl+Alt+S", "Quit AJ (Speak)"])
        self.assertFalse(actions[0].isEnabled())
        actions[1].trigger()
        self.assertEqual(self.quits, [1])

    # -- where it sits -------------------------------------------------------------------------------

    def test_it_starts_in_the_corner_the_first_time(self):
        w = self.make()
        self.assertEqual((w.x(), w.y()), (1920 - 320 - 24, 1040 - 76 - 24))

    def test_it_comes_back_where_he_left_it(self):
        w = self.make({"x": 200, "y": 150})
        self.assertEqual((w.x(), w.y()), (200, 150))

    def test_a_saved_position_that_is_off_screen_is_not_used(self):
        w = self.make({"x": 9000, "y": 150})
        self.assertEqual((w.x(), w.y()), (1576, 940))

    def drag(self, w, dx, dy):
        press = QMouseEvent(QEvent.Type.MouseButtonPress, QPointF(40, 30), QPointF(w.x() + 40, w.y() + 30),
                            Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
        move = QMouseEvent(QEvent.Type.MouseMove, QPointF(40 + dx, 30 + dy), QPointF(w.x() + 40 + dx, w.y() + 30 + dy),
                           Qt.MouseButton.NoButton, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
        release = QMouseEvent(QEvent.Type.MouseButtonRelease, QPointF(40 + dx, 30 + dy),
                              QPointF(w.x() + 40 + dx, w.y() + 30 + dy), Qt.MouseButton.LeftButton,
                              Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier)
        w.mousePressEvent(press)
        w.mouseMoveEvent(move)
        w.mouseReleaseEvent(release)

    def test_dragging_moves_it_and_remembers_where_it_was_dropped(self):
        w = self.make({"x": 200, "y": 150})
        self.drag(w, 100, 50)
        self.assertEqual((w.x(), w.y()), (300, 200))
        self.assertEqual(store.load(self.path), {"x": 300, "y": 200, "polite": False})

    def test_a_click_without_a_drag_saves_nothing(self):
        w = self.make({"x": 200, "y": 150})
        self.drag(w, 0, 0)
        self.assertFalse(self.path.exists())

    def test_a_position_that_cannot_be_saved_is_forgotten_quietly(self):
        blocker = self.dir / "a file"
        blocker.write_text("x")
        self.path = blocker / "speak_window.json"
        w = self.make({"x": 200, "y": 150})
        with self.assertLogs("aj.speak.store", "ERROR"):
            self.drag(w, 10, 10)  # no exception
        self.assertEqual((w.x(), w.y()), (210, 160))

    def test_closing_saves_the_position_and_stops_the_speaker(self):
        w = self.make({"x": 200, "y": 150})
        w.show()
        w.close()
        self.assertEqual(store.load(self.path)["x"], 200)
        self.assertIn("close", self.speaker.calls)
        self.assertEqual(self.quits, [1])

    def test_a_second_launch_brings_it_back_on_screen_on_top_with_a_flash(self):
        w = self.make({"x": 200, "y": 150})
        w.show()
        w.move(-5000, -5000)  # lost off to the side
        w.show_requested.emit()
        self.assertEqual((w.x(), w.y()), (1576, 940))
        self.raised.assert_called()
        self.assertTrue(w._flash)
        w._end_flash()
        self.assertFalse(w._flash)

    def test_a_second_launch_shows_a_hidden_window(self):
        w = self.make()
        self.assertFalse(w.isVisible())
        w.bring_forward()
        self.assertTrue(w.isVisible())

    # -- with the real Speaker ---------------------------------------------------------------------

    def test_a_click_on_the_mic_to_typed_across_threads(self):
        rig = Rig(self)
        w = self.make(speaker=rig.speaker)
        w.show()
        w.mic.click()
        self.assertTrue(self.pump(lambda: w.shown[0] == sp.TYPED))
        self.assertEqual(w.caption, "Typed")
        self.assertEqual(rig.desktop.text, SAID)
        self.assertEqual(rig.desktop.backspaces, 0)

    def test_the_x_cancels_a_real_dictation(self):
        rig = Rig(self, live_mic=True)
        w = self.make(speaker=rig.speaker)
        w.show()
        w.mic.click()
        self.assertTrue(self.pump(lambda: w.shown[0] == sp.LISTENING))
        w.close_button.click()
        self.assertTrue(self.pump(lambda: w.shown[0] == sp.CANCELLED))
        self.assertEqual(w.caption, sp.CANCELLED_TEXT)
        self.assertEqual(rig.desktop.typed, [])

    def test_the_polite_switch_makes_the_real_dictation_polished(self):
        rig = Rig(self)
        w = self.make(speaker=rig.speaker)
        w.polite_button.click()
        w.mic.click()
        self.assertTrue(self.pump(lambda: w.shown[0] == sp.TYPED))
        self.assertEqual(rig.desktop.text, POLISHED)

    def test_the_honest_error_from_the_core_reaches_the_window(self):
        rig = Rig(self, editable=False)
        w = self.make(speaker=rig.speaker)
        w.mic.click()
        self.assertTrue(self.pump(lambda: w.shown[0] == sp.ERROR))
        self.assertEqual(w.caption, "Click into a text box first.")

    # -- the icon ------------------------------------------------------------------------------------

    def test_the_icon_file_has_every_size_as_png_frames(self):
        data = icon.ico_bytes()
        self.assertEqual(data[:6], b"\x00\x00\x01\x00" + len(icon.SIZES).to_bytes(2, "little"))
        for i, size in enumerate(icon.SIZES):
            entry = data[6 + 16 * i:22 + 16 * i]
            self.assertEqual(entry[0], size % 256)
            offset = int.from_bytes(entry[12:16], "little")
            self.assertEqual(data[offset:offset + 4], b"\x89PNG")
        self.assertFalse(icon.speak_icon().isNull())

    def test_the_icon_in_the_project_is_the_one_the_code_makes(self):
        shipped = Path(inspect.getsourcefile(icon)).with_name("aj_speak.ico")
        self.assertTrue(shipped.exists(), "run: python -m aj.speak --make-icon aj\\speak\\aj_speak.ico")
        self.assertEqual(shipped.read_bytes()[:6], icon.ico_bytes()[:6])


class ByVoiceTests(unittest.TestCase):
    """AJ (Speak) by voice (the boss, 2026-10-22): "open AJ speak", "AJ, speak", "speak window,
    listen". The window is never really started here."""

    def setUp(self):
        from aj import tools
        from aj.tools import intents
        tools.load_all()
        self.intents = intents

    def test_what_is_claimed(self):
        for said, listen in (("open AJ speak", False), ("open speak", False), ("open the speak window", False),
                             ("Hey AJ, open the speak window", False), ("AJ, speak.", True), ("Computer, speak", True),
                             ("speak window, listen", True), ("open speak and listen", True)):
            with self.subTest(said=said):
                self.assertEqual(self.intents.match(said), ("open_speak", {"listen": listen}))
        for said in ("I want to speak to the manager", "what languages can you speak", "speak up"):
            with self.subTest(said=said):
                self.assertNotEqual((self.intents.match(said) or ("",))[0], "open_speak")

    def test_it_starts_the_projects_pythonw_once_per_call(self):
        from aj.tools import dictation as tools_dictation
        from aj.tools.registry import Level, get
        launched = []
        with mock.patch.object(tools_dictation, "_popen", lambda args, **kw: launched.append((args, kw))):
            self.assertIn("Opening AJ (Speak)", tools_dictation.open_speak())
            self.assertIn("listening", tools_dictation.open_speak(listen=True))
        (first, kw), (second, _) = launched
        self.assertTrue(first[0].lower().endswith("pythonw.exe"))
        self.assertEqual(first[1:], ["-m", "aj.speak"])
        self.assertEqual(second[1:], ["-m", "aj.speak", "--listen"])
        self.assertEqual(Path(kw["cwd"]).resolve(), Path(tools_dictation.APP_DIR).resolve())
        spec = get("open_speak")
        self.assertEqual((spec.level, spec.cloud), (Level.SAFE, False))

    def test_listen_reaches_a_running_one(self):
        name = f"AJ.Speak.Test.{os.getpid()}"
        first, second = instance.Instance(name), instance.Instance(name)
        self.addCleanup(first.release)
        shows, listens = threading.Event(), threading.Event()
        self.assertTrue(first.claim())
        first.watch_for_show(shows.set, listens.set)
        self.assertFalse(second.claim())
        self.assertTrue(second.request_show(listen=True))
        self.assertTrue(shows.wait(2) and listens.wait(2))
        listens.clear()
        second.request_show()  # a plain second launch only shows it
        self.assertFalse(listens.wait(0.3))

    def test_the_listen_flag(self):
        from aj.speak import app
        with mock.patch.object(app, "run", return_value=0) as run, mock.patch.object(app.migrate, "run", return_value=[]), \
                mock.patch.object(app, "setup_logging"), mock.patch.object(app.logging, "shutdown"),                 mock.patch.object(app.os, "_exit") as exit_:
            app.main(["--listen"])
        run.assert_called_once_with(listen=True)
        exit_.assert_called_once_with(0)

    def test_a_second_listen_never_ends_the_dictation_under_way(self):
        rig = Rig(self, said="Hi Amma.")
        rig.speaker._job = object()  # one is under way
        self.assertFalse(rig.speaker.listen())


class FakeShare:
    """core/mic_share.py for the Speaker: whether the assistant is dictating, and what it was told."""

    def __init__(self, aj_dictating=False):
        self.busy = aj_dictating
        self.said: list[bool] = []

    def aj_dictating(self):
        return self.busy

    def listening(self, on):
        self.said.append(on)


class MicShareTests(unittest.TestCase):
    """The review of 2026-10-22: AJ (Speak) raises its "listening" event for its whole dictation, so
    the assistant drops what it hears; and it doesn't start while the assistant takes dictation."""

    def test_the_event_is_up_for_the_whole_dictation(self):
        rig = Rig(self)
        rig.speaker._share = share = FakeShare()
        self.assertEqual(rig.go(), (sp.TYPED, "Typed"))
        self.assertEqual(share.said, [True, False])

    def test_a_cancel_lets_the_assistant_hear_again_at_once(self):
        rig = Rig(self)
        rig.speaker._share = share = FakeShare()
        rig.gemini_gate.clear()  # hold Gemini mid-answer
        rig.speaker.toggle()
        self.assertTrue(wait_until(lambda: rig.gemini_calls))
        rig.speaker.cancel()
        self.assertEqual(share.said[-1], False)
        rig.gemini_gate.set()
        join_jobs()
        self.assertEqual(share.said[-1], False)

    def test_busy_while_the_assistant_takes_dictation(self):
        rig = Rig(self)
        rig.speaker._share = share = FakeShare(aj_dictating=True)
        rig.speaker.toggle()
        self.assertEqual(rig.events[-1], (sp.ERROR, sp.AJ_BUSY))
        self.assertIsNone(rig.speaker._job)
        self.assertEqual((share.said, rig.mics_made), ([], 0))  # the mic never opened


if __name__ == "__main__":
    unittest.main()
