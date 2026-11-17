"""The wake listener (aj/listener.py): when it listens, what starts AJ, and what is never kept.

Power, the microphone, the ears, the wake words and starting AJ are all faked: no microphone is
opened, no model is loaded and nothing is started. The named kernel objects are real but get
names of their own, so a listener the user has running is never touched."""
import io
import logging
import queue
import shutil
import tempfile
import threading
import time
import unittest
import uuid
import wave
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

import numpy as np

from aj import listener as L
from aj.config import APP_DIR, VoiceSettings
from aj.core.audio import MIC_BLOCK, MIC_RATE
from aj.core.wake import split_wake


class FakeMic:
    def __init__(self, fail: Exception | None = None):
        self.blocks: queue.Queue = queue.Queue()
        self.is_open = False
        self.opens = 0
        self.fail = fail

    def open(self):
        if self.fail is not None:
            raise self.fail
        if not self.is_open:
            self.opens += 1
        self.is_open = True

    def close(self):
        self.is_open = False
        while not self.blocks.empty():
            self.blocks.get_nowait()


class FakeEars:
    """The VAD is faked: the next feed() returns whatever the test queued in `ending`.
    transcribe() knows each utterance by its first sample (see ListenerTests.utterance)."""

    def __init__(self, said: dict):
        self.said = said
        self.ending: list[np.ndarray] = []
        self.transcribed: list[int] = []  # lengths, in samples
        self.resets = 0

    def feed(self, block):
        out, self.ending = self.ending, []
        return out

    def transcribe(self, samples):
        self.transcribed.append(len(samples))
        return self.said[round(float(samples[0]), 4)]

    def reset(self):
        self.resets += 1


def fake_split(text: str) -> tuple[bool, str]:
    """Stands in for core/wake.py's split_wake: only "Hey AJ" at the start wakes."""
    if text.lower().startswith("hey aj"):
        return True, text[6:].strip(" ,.")
    return False, text


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class ListenerTests(unittest.TestCase):
    def setUp(self):
        self.power: bool | None = True
        self.running = False
        self.mic = FakeMic()
        self.said: dict[float, str] = {}
        self.ears_made: list[FakeEars] = []
        self.started: list[Path | None] = []
        self.clock = Clock()
        self.tmp = Path(tempfile.mkdtemp(prefix="listener-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.listener = self.make()

    def make(self, split=fake_split, start=None):
        def ears(voice):
            made = FakeEars(self.said)
            self.ears_made.append(made)
            return made

        def record_start(path):
            self.started.append(path)
            return True

        return L.Listener(VoiceSettings(), plugged_in=lambda: self.power, aj_running=lambda: self.running,
                          mic=self.mic, ears=ears, split=split, start=start or record_start,
                          wake_dir=self.tmp, clock=self.clock)

    def utterance(self, text: str, secs: float = 1.5) -> np.ndarray:
        """Audio the fake ears will transcribe as `text`, told apart by its sample value."""
        value = round(0.01 + 0.001 * len(self.said), 4)
        self.said[value] = text
        return np.full(int(secs * MIC_RATE), value, dtype=np.float32)

    def say(self, *utterances: np.ndarray) -> None:
        """One mic block arrives and the (fake) VAD says these utterances just ended."""
        self.listener.ears.ending = list(utterances)
        self.mic.blocks.put(np.zeros(MIC_BLOCK, dtype=np.float32))
        self.listener.hear()

    def listening(self) -> None:
        self.listener.check()
        self.assertTrue(self.mic.is_open)
        self.assertIsNotNone(self.listener.ears)

    # -- when it listens --

    def test_plugged_in_and_aj_off_it_listens(self):
        self.listening()
        self.assertIsNone(self.listener.why_not)
        self.assertEqual(len(self.ears_made), 1)

    def test_on_battery_the_mic_closes_and_the_models_unload(self):
        self.listening()
        self.power = False
        self.listener.check()
        self.assertFalse(self.mic.is_open)  # no stream open at all
        self.assertIsNone(self.listener.ears)
        self.assertEqual(self.listener.why_not, "on battery")
        self.power = True
        self.listener.check()
        self.assertTrue(self.mic.is_open)
        self.assertEqual(len(self.ears_made), 2)  # loaded again only when plugged back in

    def test_started_on_battery_it_never_opens_the_mic_or_loads_anything(self):
        self.power = False
        for _ in range(3):
            self.listener.check()
        self.assertEqual(self.mic.opens, 0)
        self.assertEqual(self.ears_made, [])

    def test_unknown_power_counts_as_battery(self):
        self.power = None
        self.listener.check()
        self.assertFalse(self.mic.is_open)
        self.assertEqual(self.listener.why_not, "on battery")

    def test_while_aj_runs_it_lets_go_and_resumes_when_aj_exits(self):
        self.listening()
        self.running = True
        self.listener.check()
        self.assertFalse(self.mic.is_open)
        self.assertIsNone(self.listener.ears)  # AJ loads its own Parakeet: no second copy
        self.assertEqual(self.listener.why_not, "AJ is on")
        self.running = False
        self.listener.check()
        self.assertTrue(self.mic.is_open)

    # -- what starts AJ --

    def test_a_wake_word_starts_aj_once(self):
        self.listening()
        with self.assertLogs("aj.listener", "INFO") as logs:
            self.say(self.utterance("Hey AJ."), self.utterance("Hey AJ!"))
        self.assertEqual(self.started, [None])  # no command: AJ just starts
        self.assertFalse(self.mic.is_open)  # AJ takes the mic from here
        self.assertIsNone(self.listener.ears)
        self.assertEqual(self.listener.why_not, "starting AJ")
        self.assertIn("wake word heard: starting AJ", logs.output[0])
        self.clock.now += 5  # AJ still starting: the mic stays shut
        self.listener.check()
        self.assertFalse(self.mic.is_open)
        self.assertFalse(self.listener.wake(self.utterance("Hey AJ."), ""))  # and a stray wake is ignored
        self.running = True  # AJ is up
        self.clock.now += L.HOLD_SECS
        self.listener.check()
        self.assertEqual(self.listener.why_not, "AJ is on")
        self.assertEqual(self.started, [None])
        self.running = False  # AJ switched off again: back to listening
        self.listener.check()
        self.assertTrue(self.mic.is_open)

    def test_a_command_after_the_wake_word_goes_to_aj_as_a_wav(self):
        self.listening()
        spoken = self.utterance("Hey AJ, open WhatsApp.", secs=1.7)
        self.say(spoken)
        [path] = self.started
        self.assertIsNotNone(path)
        self.assertTrue(path.exists())
        self.assertEqual(path.parent, self.tmp)
        self.assertTrue(path.name.startswith(L.WAKE_PREFIX))
        with wave.open(str(path), "rb") as w:
            self.assertEqual((w.getnchannels(), w.getsampwidth(), w.getframerate()), (1, 2, 16000))
            self.assertEqual(w.getnframes(), len(spoken))  # the whole utterance, name included
            pcm = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)
        self.assertAlmostEqual(pcm[0] / 32767, spoken[0], places=3)

    def test_aj_gets_the_command_on_its_command_line(self):
        with mock.patch.object(L.toggle.subprocess, "Popen") as popen:
            self.assertTrue(L.start_aj(Path("C:/temp/aj-wake-1.wav")))
            args, kwargs = popen.call_args
            self.assertEqual(args[0][1:], ["-m", "aj", "--wake-command", str(Path("C:/temp/aj-wake-1.wav"))])
            self.assertTrue(args[0][0].lower().endswith(("pythonw.exe", "python.exe")))
            self.assertEqual(kwargs["cwd"], APP_DIR)
            flags = kwargs["creationflags"]
            self.assertTrue(flags & 0x00000008 and flags & 0x00000200)  # detached, as the shortcut starts AJ
            self.assertTrue(flags & 0x00000020)  # at normal priority, not the listener's below-normal
            L.start_aj(None)
            self.assertEqual(popen.call_args.args[0][1:], ["-m", "aj"])

    def test_aj_does_not_inherit_the_listeners_blas_setting(self):
        with mock.patch.object(L.toggle.subprocess, "Popen") as popen, mock.patch.object(L, "_SET_BLAS", True), \
                mock.patch.dict(L.os.environ, {"OPENBLAS_NUM_THREADS": "1"}):
            L.start_aj(None)
        self.assertNotIn("OPENBLAS_NUM_THREADS", popen.call_args.kwargs["env"])

    def test_ordinary_talk_does_nothing_and_is_never_logged_or_kept(self):
        self.listening()
        with self.assertNoLogs(level=logging.DEBUG):  # nothing at all, from any logger
            self.say(self.utterance("We should get lunch."), self.utterance("The computer is slow today."))
        self.assertEqual(self.started, [])
        self.assertTrue(self.mic.is_open)
        self.assertEqual(list(self.tmp.iterdir()), [])  # no recording written

    def test_a_long_utterance_is_checked_on_its_start_first(self):
        self.listening()
        ears = self.listener.ears
        self.say(self.utterance("We should move the meeting to Thursday afternoon then.", secs=8))
        self.assertEqual(ears.transcribed, [int(L.HEAD_SECS * MIC_RATE)])  # the first 3 s only
        self.assertEqual(self.started, [])
        self.say(self.utterance("Hey AJ, remind me to move the meeting to Thursday afternoon.", secs=8))
        self.assertEqual(ears.transcribed[1:], [int(L.HEAD_SECS * MIC_RATE), 8 * MIC_RATE])  # then in full
        self.assertEqual(len(self.started), 1)

    def test_with_aj_already_on_a_wake_word_starts_nothing(self):
        self.listening()
        self.running = True  # AJ came on between two checks
        self.say(self.utterance("Hey AJ, what time is it?"))
        self.assertEqual(self.started, [])

    def test_the_real_wake_words(self):
        self.listener = self.make(split=split_wake)
        self.listening()
        self.say(self.utterance("The computer is slow today."), self.utterance("Ajay said he'd call."))
        self.assertEqual(self.started, [])
        self.say(self.utterance("Hey AJ, open WhatsApp."))
        self.assertEqual(len(self.started), 1)
        self.assertTrue(self.started[0].exists())

    # -- when things go wrong --

    def test_if_aj_never_comes_up_it_says_so_and_listens_again(self):
        self.listening()
        self.say(self.utterance("Hey AJ."))
        self.clock.now += L.HOLD_SECS + 1
        with self.assertLogs("aj.listener", "ERROR") as logs:
            self.listener.check()
        self.assertIn("didn't come on", logs.output[0])
        self.assertTrue(self.mic.is_open)

    def test_a_failed_start_is_logged_and_its_recording_deleted(self):
        def broken(path):
            raise OSError("no pythonw")
        self.listener = self.make(start=broken)
        self.listening()
        with self.assertLogs("aj.listener", "ERROR") as logs:
            self.say(self.utterance("Hey AJ, open WhatsApp."))
        self.assertIn("couldn't start AJ", logs.output[0])
        self.assertEqual(list(self.tmp.iterdir()), [])
        self.clock.now += L.TRY_HOLD_SECS
        self.listener.check()
        self.assertTrue(self.mic.is_open)

    def test_a_mic_that_sends_nothing_is_reopened(self):
        self.listening()
        ears = self.listener.ears
        self.clock.now += L.STALL_SECS + 1
        with self.assertLogs("aj.listener", "ERROR"):
            self.listener.hear()  # waits a quarter second for a block that never comes
        self.assertFalse(self.mic.is_open)
        self.assertEqual(ears.resets, 1)
        self.listener.check()
        self.assertTrue(self.mic.is_open)
        self.assertIs(self.listener.ears, ears)  # the models stayed loaded

    def test_a_mic_that_wont_open_is_retried_later_and_logged_once(self):
        self.mic.fail = OSError("no microphone")
        with self.assertLogs("aj.listener", "ERROR") as logs:
            self.listener.check()
            self.listener.check()  # too soon to retry
            self.clock.now += L.RETRY_SECS
            self.listener.check()  # retried, failed the same way: not logged again
        self.assertEqual(len(logs.output), 1)
        self.assertEqual(self.listener.why_not, "can't listen")
        self.assertIsNone(self.listener.ears)  # nothing held while it can't listen
        self.mic.fail = None
        self.clock.now += L.RETRY_SECS
        self.listener.check()
        self.assertTrue(self.mic.is_open)

    def test_the_stop_signal_ends_the_loop_with_the_mic_closed(self):
        stop = threading.Event()
        self.listener.stop = stop
        threading.Timer(0.3, stop.set).start()
        began = time.monotonic()
        with self.assertLogs("aj.listener", "INFO") as logs:
            self.listener.run()
        self.assertLess(time.monotonic() - began, 2.0)
        self.assertFalse(self.mic.is_open)
        self.assertIsNone(self.listener.ears)
        self.assertEqual([line.split(":")[-1] for line in logs.output], ["wake listener on", "wake listener off"])

    def test_old_recordings_are_swept(self):
        old, new, other = (self.tmp / f"{L.WAKE_PREFIX}old.wav", self.tmp / f"{L.WAKE_PREFIX}new.wav",
                           self.tmp / "notes.wav")
        for path in (old, new, other):
            path.write_bytes(b"x")
        stale = time.time() - L.WAKE_FILE_MAX_SECS - 60
        L.os.utime(old, (stale, stale))
        L.os.utime(other, (stale, stale))
        L.sweep_wake_files(self.tmp)
        self.assertFalse(old.exists())
        self.assertTrue(new.exists())
        self.assertTrue(other.exists())  # only the listener's own recordings

    def test_missing_models_raise_instead_of_ending_the_process(self):
        with self.assertRaises(FileNotFoundError):
            L.load_ears(VoiceSettings(vad_model=str(self.tmp / "missing.onnx")))


class KernelObjectTests(unittest.TestCase):
    """The real named mutex and event, under names no real listener uses."""

    def setUp(self):
        tag = uuid.uuid4().hex
        self.mutex, self.event = f"Local\\AJ.ListenerTest.{tag}", f"Local\\AJ.ListenerTest.{tag}.Stop"

    def test_one_listener_at_a_time(self):
        self.assertFalse(L.is_listener_running(self.mutex))
        self.assertTrue(L.claim_instance(self.mutex))
        self.assertTrue(L.is_listener_running(self.mutex))
        self.assertFalse(L.claim_instance(self.mutex))

    def test_stop_reaches_the_running_listener(self):
        self.assertFalse(L.request_stop(0, self.event, self.mutex))  # nobody listening yet
        stop = threading.Event()
        L.watch_for_stop(stop, self.event)
        self.assertTrue(L.request_stop(0, self.event, self.mutex))
        self.assertTrue(stop.wait(2.0))

    def test_stop_from_the_command_line(self):
        with mock.patch.object(L, "request_stop", return_value=False), redirect_stdout(io.StringIO()) as out:
            self.assertEqual(L.main(["--stop"]), 0)
        self.assertIn("isn't running", out.getvalue())


class SpeakListeningTests(unittest.TestCase):
    """While AJ (Speak) takes a dictation, the words are for it: "AJ, open WhatsApp" said into it
    must not start the assistant (the review of 2026-10-22)."""
    utterance, say, listening = ListenerTests.utterance, ListenerTests.say, ListenerTests.listening

    def setUp(self):
        self.speak_busy = True
        ListenerTests.setUp(self)

    def make(self, split=fake_split, start=None):
        listener = ListenerTests.make(self, split, start)
        listener._speak_listening = lambda: self.speak_busy
        return listener

    def test_nothing_is_looked_at_while_speak_listens(self):
        self.listening()
        self.say(self.utterance("Hey AJ, open WhatsApp"))
        self.assertEqual(self.started, [])
        self.assertEqual(self.listener.ears.transcribed, [])
        self.speak_busy = False
        self.say(self.utterance("Hey AJ, open WhatsApp"))
        self.assertEqual(len(self.started), 1)


if __name__ == "__main__":
    unittest.main()
