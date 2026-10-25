"""Microphone input and speaker output through sounddevice.

The mic is opened only while AJ is allowed to listen and closed otherwise, which
releases the device (battery mode, games, voice chat). The player keeps one output
stream open while AJ is awake, so speaking never waits for a device to open.
"""
import queue
import threading
import time

import numpy as np

# sounddevice is imported when first needed, not with this module: its import starts PortAudio,
# which lists every audio device and took 0.85 s of the 1.4 s import (measured 2026-10-21).
# preload() does it on a background thread while the model loads.
_sd = None
_sd_lock = threading.Lock()


def sounddevice():
    global _sd
    with _sd_lock:
        if _sd is None:
            import sounddevice as module
            _sd = module
    return _sd


def preload() -> None:
    sounddevice()

MIC_RATE = 16000
MIC_BLOCK = 512  # 32 ms: Silero VAD's window size


class Mic:
    def __init__(self):
        self.blocks: queue.Queue[np.ndarray] = queue.Queue()
        self._stream = None

    @property
    def is_open(self) -> bool:
        return self._stream is not None

    def _on_audio(self, indata, frames, time_info, status) -> None:
        self.blocks.put(indata[:, 0].copy())

    def open(self) -> None:
        if self._stream is None:
            self._stream = sounddevice().InputStream(samplerate=MIC_RATE, channels=1, dtype="float32",
                                          blocksize=MIC_BLOCK, callback=self._on_audio)
            self._stream.start()

    def close(self) -> None:
        if self._stream is not None:
            self._stream.stop()
            self._stream.close()
            self._stream = None
        while not self.blocks.empty():
            self.blocks.get_nowait()


class Player:
    """Plays queued buffers back to back. stop() silences it at once (barge-in).

    Each buffer can carry the words it says, so it's known what the boss has actually heard
    this turn (an interrupted reply goes into the conversation only that far) and what was
    said in the last few seconds (to tell AJ's echo from the boss, core/barge.py)."""

    def __init__(self, rate: int = 24000):
        self.rate = rate
        self._queue: queue.Queue[tuple[np.ndarray, str]] = queue.Queue()
        self._started: list[tuple[float, str]] = []  # (when it began playing, its words), this turn
        self._current: np.ndarray | None = None
        self._pos = 0
        self._lock = threading.Lock()
        self._stream = None
        self.idle = threading.Event()
        self.idle.set()
        self.first_sound_at: float | None = None  # perf_counter when this turn's audio began

    def open(self) -> None:
        if self._stream is None:
            self._stream = sounddevice().OutputStream(samplerate=self.rate, channels=1, dtype="float32",
                                           callback=self._on_output, latency="low")
            self._stream.start()

    def close(self) -> None:
        self.stop()
        if self._stream is not None:
            self._stream.stop()
            self._stream.close()
            self._stream = None

    @property
    def output_latency(self) -> float:
        return self._stream.latency if self._stream is not None else 0.0

    def new_turn(self) -> None:
        self.first_sound_at = None
        with self._lock:
            self._started = []

    def play(self, samples: np.ndarray, words: str = "") -> None:
        self.idle.clear()
        self._queue.put((samples.astype(np.float32, copy=False), words))

    def heard(self) -> str:
        """The words of every buffer that began playing this turn."""
        with self._lock:
            return " ".join(w for _, w in self._started if w)

    def said_lately(self, secs: float = 6.0) -> str:
        """The words of the buffers that began in the last `secs`, and the one playing now."""
        cutoff = time.perf_counter() - secs
        with self._lock:
            recent = [w for t, w in self._started if t >= cutoff] or [w for _, w in self._started[-1:]]
        return " ".join(w for w in recent if w)

    def stop(self) -> None:
        with self._lock:
            while not self._queue.empty():
                self._queue.get_nowait()
            self._current, self._pos = None, 0
        self.idle.set()

    def _on_output(self, outdata, frames, time_info, status) -> None:
        out = np.zeros(frames, dtype=np.float32)
        filled = 0
        with self._lock:
            while filled < frames:
                if self._current is None or self._pos >= len(self._current):
                    try:
                        self._current, words = self._queue.get_nowait()
                        self._pos = 0
                        self._started.append((time.perf_counter(), words))
                    except queue.Empty:
                        self._current = None
                        break
                n = min(frames - filled, len(self._current) - self._pos)
                out[filled:filled + n] = self._current[self._pos:self._pos + n]
                filled += n
                self._pos += n
            if filled and self.first_sound_at is None:
                self.first_sound_at = time.perf_counter()
            if self._current is None and self._queue.empty():
                self.idle.set()
        outdata[:, 0] = out
