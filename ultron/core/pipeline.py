"""One conversation turn with every stage overlapping:
your speech -> transcript -> model token stream -> sentence chunks -> voice -> speakers.

Ultron starts speaking the first chunk while the model is still writing the rest.
Each turn is timed from the moment you stopped talking, so the latency report shows
where every millisecond went.
"""
import queue
import re
import threading
import time
import wave
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING
from pathlib import Path

import numpy as np

from ultron.config import Settings
from ultron.core.audio import MIC_BLOCK, MIC_RATE, Player
from ultron.core.llm import wake_gpu
from ultron.core.speech import Ears, Voice, looks_unfinished

if TYPE_CHECKING:
    from ultron.core.brain import Brain


# The whole utterance must be the command, so "turn off the music" is not mistaken for it.
_OFF = re.compile(r"^\W*(hey\W+)?(ultron\W+)?(please\W+)?"
                  r"((turn|switch|power)\W+(yourself\W+)?off|shut\W*(yourself\W+)?down|go\W+dark|good\W*bye)"
                  r"(\W+(ultron|now|please))*\W*$", re.IGNORECASE)


def is_off_command(text: str) -> bool:
    """'Ultron, turn off' turns Ultron off. It never means the laptop: Ultron has no power control."""
    return bool(_OFF.search(text))


class Status(StrEnum):
    """What Ultron is doing right now. The window shows it; the console modes ignore it."""
    LOADING = "loading"
    LISTENING = "listening"
    HEARING = "hearing"
    THINKING = "thinking"
    SPEAKING = "speaking"
    MIC_OFF = "mic off"
    TURNING_OFF = "turning off"


def pick_status(listening: bool, answering: bool, hearing: bool, speaking: bool) -> Status:
    """Hearing you wins over everything (with headphones you can talk over him); a typed
    question still gets Thinking/Speaking with the mic off."""
    if listening and hearing:
        return Status.HEARING
    if answering:
        return Status.SPEAKING if speaking else Status.THINKING
    return Status.LISTENING if listening else Status.MIC_OFF


@dataclass
class TurnTimes:
    stopped_talking: float
    heard: float = 0.0
    asked: float = 0.0
    first_token: float = 0.0
    first_chunk: float = 0.0
    first_audio: float = 0.0
    first_sound: float = 0.0

    def report(self) -> str:
        def ms(t: float) -> str:
            return f"{(t - self.stopped_talking) * 1000:.0f}" if t else "-"
        return (f"heard {ms(self.heard)} | asked {ms(self.asked)} | first token {ms(self.first_token)}"
                f" | first chunk {ms(self.first_chunk)}"
                f" | voice ready {ms(self.first_audio)} | SOUND {ms(self.first_sound)} ms after you stopped")

    def summary(self) -> str:
        """The window's one-line version: time to first sound first, as that's what you feel.
        Stages that didn't happen (nothing heard for typed text, no sound when muted) are left out."""
        stages = (("sound", self.first_sound), ("heard", self.heard), ("first token", self.first_token),
                  ("voice", self.first_audio))
        parts = [f"{name} {(t - self.stopped_talking) * 1000:.0f}" for name, t in stages if t]
        return " · ".join(parts) + " ms" if parts else ""


class Mouth:
    """Background voice worker: text chunks in, audio out, strictly in order."""

    def __init__(self, voice: Voice, player: Player):
        self.voice, self.player = voice, player
        self._queue: queue.Queue = queue.Queue()
        self._generation = 0
        threading.Thread(target=self._run, daemon=True, name="mouth").start()

    def say(self, text: str, times: TurnTimes | None = None) -> None:
        self._queue.put((self._generation, text, times))

    def silence(self) -> None:
        """Drop everything queued and stop the speakers (barge-in)."""
        self._generation += 1
        while True:
            try:
                self._queue.get_nowait()
                self._queue.task_done()
            except queue.Empty:
                break
        self.player.stop()

    def wait_done(self) -> None:
        self._queue.join()
        self.player.idle.wait()

    def _run(self) -> None:
        while True:
            generation, text, times = self._queue.get()
            try:
                if generation != self._generation:
                    continue
                audio = self.voice.say(text)
                if generation != self._generation:
                    continue
                if times is not None and not times.first_audio:
                    times.first_audio = time.perf_counter()
                self.player.play(audio)
            finally:
                self._queue.task_done()


class WavMic:
    """Feeds WAV files into the pipeline in real time, as if you said them into the mic.
    Each file starts only once Ultron has finished answering the previous one."""

    def __init__(self, paths: list[Path], quiet: threading.Event, tail_secs: float = 1.5):
        self.paths, self.quiet, self.tail_secs = paths, quiet, tail_secs
        self.blocks: queue.Queue[np.ndarray] = queue.Queue()
        self.done = threading.Event()

    def open(self) -> None:
        threading.Thread(target=self._run, daemon=True, name="wavmic").start()

    def close(self) -> None:
        pass

    @staticmethod
    def _load(path: Path) -> np.ndarray:
        with wave.open(str(path), "rb") as w:
            pcm = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32) / 32768
            if w.getnchannels() > 1:
                pcm = pcm.reshape(-1, w.getnchannels()).mean(axis=1)
            rate = w.getframerate()
        if rate != MIC_RATE:
            pcm = np.interp(np.arange(0, len(pcm), rate / MIC_RATE), np.arange(len(pcm)), pcm).astype(np.float32)
        return pcm

    def _run(self) -> None:
        block_secs = MIC_BLOCK / MIC_RATE
        for path in self.paths:
            self.quiet.wait()
            time.sleep(0.5)
            audio = np.concatenate([self._load(path), np.zeros(int(MIC_RATE * self.tail_secs), np.float32)])
            start = time.perf_counter()
            for i in range(0, len(audio) - MIC_BLOCK + 1, MIC_BLOCK):
                self.blocks.put(audio[i:i + MIC_BLOCK])
                time.sleep(max(0.0, start + (i // MIC_BLOCK + 1) * block_secs - time.perf_counter()))
            time.sleep(0.5)
            self.quiet.wait()
        time.sleep(1.0)
        self.done.set()


class Session:
    """Listens, decides when you've finished, and answers. Half-duplex by default: while
    Ultron speaks through laptop speakers the mic is ignored, so he can't hear himself.
    With headphones, `barge_in=True` lets you interrupt him by talking.

    The window drives it from another thread: set_listening() turns the mic on or off and
    ask() answers typed text. Both only leave a note; the loop thread does the work, so the
    mic stream and the VAD are never touched from two threads at once."""

    def __init__(self, settings: Settings, ears: Ears, brain: "Brain", mouth: Mouth | None, source,
                 stop: threading.Event, barge_in: bool = False, on_turn=None,
                 on_status: Callable[[Status], None] | None = None,
                 on_heard: Callable[[str], None] | None = None, listening: bool = True):
        self.s = settings.voice
        self.ears, self.brain, self.mouth, self.source = ears, brain, mouth, source
        self.stop, self.barge_in, self.on_turn = stop, barge_in, on_turn
        self.on_status, self.on_heard = on_status, on_heard
        self.quiet = threading.Event()  # set while Ultron is not answering
        self.quiet.set()
        self._worker: threading.Thread | None = None
        self._listening = listening  # what the mic button says; the loop makes it so
        self._typed: queue.Queue[str] = queue.Queue()
        self._nudge = threading.Event()  # wakes the loop early while the mic is closed
        self._status: Status | None = None
        self._turn_started = 0.0

    def set_listening(self, on: bool) -> None:
        """Mic on or off, from any thread. Off closes the mic stream itself, not just ignores it."""
        self._listening = on
        self._nudge.set()

    def ask(self, text: str) -> None:
        """Answer typed text as if it had been said. Waits its turn if Ultron is mid-answer."""
        self._typed.put(text)
        self._nudge.set()

    def _answer(self, text: str, times: TurnTimes) -> None:
        self.quiet.clear()
        if self.on_heard:
            self.on_heard(text)
        if is_off_command(text):
            if self.on_turn:
                self.on_turn(text, "(turning off)", times)
            self.stop.set()
            return
        if self.mouth is not None:
            self.mouth.player.new_turn()
        reply = self.brain.reply(text, self.mouth, times)
        if self.mouth is not None:
            self.mouth.wait_done()
            times.first_sound = self.mouth.player.first_sound_at or 0.0
        if self.on_turn:
            self.on_turn(text, reply, times)
        self.ears.reset()
        while not self.source.blocks.empty():  # drop any echo of Ultron's own voice
            self.source.blocks.get_nowait()
        self.quiet.set()

    def _keep_gpu_awake_while_you_talk(self) -> None:
        """A single ping isn't enough: the GPU starts clocking down again right after it.
        So ping every 0.4 s while you're talking (one at a time), and never when you're not."""
        if not self.ears.hearing_speech or time.perf_counter() - self._last_wake < 0.4:
            return
        if self._waker is not None and self._waker.is_alive():
            return
        self._last_wake = time.perf_counter()
        self._waker = threading.Thread(target=wake_gpu, args=(self.brain.base,), daemon=True)
        self._waker.start()

    def _switch_mic(self, on: bool) -> bool:
        """Open or close the mic stream (off releases the device). Mid-answer the VAD is left
        alone: the answer resets it when it finishes."""
        if on:
            self.source.open()
        else:
            self.source.close()
        if self.quiet.is_set():
            self.ears.reset()
        return on

    def _next_block(self, mic_on: bool, busy: bool) -> np.ndarray | None:
        if mic_on:
            try:
                return self.source.blocks.get(timeout=0.05)
            except queue.Empty:
                return None
        # Mic closed: nothing to read, so sleep until nudged. Short naps only while answering,
        # so Thinking -> Speaking still shows up promptly.
        self._nudge.wait(0.05 if busy else 0.25)
        self._nudge.clear()
        return None

    def _update_status(self, mic_on: bool) -> None:
        """Report the status only when it changes, so an idle Ultron sends nothing."""
        answering = not self.quiet.is_set()
        sound_at = self.mouth.player.first_sound_at if self.mouth is not None else None
        speaking = answering and sound_at is not None and sound_at >= self._turn_started
        # Mid-answer without barge-in the mic is ignored, so it can't be "hearing" you then.
        hearing = mic_on and self.ears.hearing_speech and (self.barge_in or not answering)
        status = pick_status(mic_on, answering, hearing, speaking)
        if status != self._status:
            self._status = status
            if self.on_status:
                self.on_status(status)

    def _start_answer(self, text: str, times: TurnTimes) -> None:
        if self._worker is not None:
            self._worker.join()
        self.quiet.clear()  # _answer clears it too; doing it here flips the status at once
        self._turn_started = time.perf_counter()
        self._worker = threading.Thread(target=self._answer, args=(text, times), daemon=True)
        self._worker.start()

    def _finish_answer(self) -> None:
        """Wait for the last answer. If Ultron was switched off mid-answer, keep cutting it
        short until it stops (the cancel can land just before the reply starts and clears it)."""
        deadline = time.perf_counter() + 30
        while self._worker.is_alive() and time.perf_counter() < deadline:
            if self.stop.is_set():
                self.brain.cancel.set()
                if self.mouth is not None:
                    self.mouth.silence()
            self._worker.join(timeout=0.1)

    def run(self) -> None:
        pending, times, deadline = "", None, 0.0
        self._waker, self._last_wake = None, 0.0
        mic_on = False
        try:
            while not self.stop.is_set():
                if getattr(self.source, "done", None) is not None and self.source.done.is_set():
                    return
                if mic_on != self._listening:
                    mic_on = self._switch_mic(self._listening)
                    pending = ""  # mic off means stop listening now, half-heard sentences included
                busy = not self.quiet.is_set()
                if not busy and not pending and not self._typed.empty():
                    typed = self._typed.get_nowait()
                    self._start_answer(typed, TurnTimes(stopped_talking=time.perf_counter()))
                    self._update_status(mic_on)  # Thinking now, not after the next mic block
                    continue
                block = self._next_block(mic_on, busy)
                self._update_status(mic_on)
                if busy and not self.barge_in:
                    continue
                if block is not None:
                    if busy and self.barge_in and self.ears.hearing_speech:
                        self.brain.cancel.set()
                        self.mouth.silence()
                    utterances = self.ears.feed(block)
                    self._keep_gpu_awake_while_you_talk()
                    for utterance in utterances:
                        stopped = time.perf_counter() - self.s.end_silence_secs
                        text = self.ears.transcribe(utterance)
                        if not text:
                            continue
                        pending = f"{pending} {text}".strip()
                        times = TurnTimes(stopped_talking=stopped, heard=time.perf_counter())
                        deadline = time.perf_counter() + (self.s.unfinished_wait_secs
                                                          if looks_unfinished(pending) else 0.0)
                if pending and not self.ears.hearing_speech and time.perf_counter() >= deadline:
                    text, pending = pending, ""
                    self._start_answer(text, times)
        finally:
            if self._worker is not None:
                self._finish_answer()
            self.source.close()
