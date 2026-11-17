"""Listening for AJ (Speak): the microphone, and Silero VAD to tell when the user has stopped.

Nothing here tries to understand words. The user, 2026-10-22: "It can take longer, but ... I want it
to be accurate", so Gemini writes the words down (core/dictation.transcribe) and this only decides
when to stop recording. Silero VAD (the voice-activity model the assistant listens with too, about
2 MB) cuts the speech into pieces at every 0.3 s of quiet and drops the quiet. People pause in the
middle of a sentence while dictating, so the recording ends only after DictationSettings.silence_secs
(1.5 s) of quiet, not at the first pause. The pieces go on as they are; dictation.joined() puts a
short gap between them. Parakeet is not here at all: it is 0.7 GB, and speaker.py loads it only if
Gemini fails.

Time is counted in audio (512 samples is 32 ms), not by the clock, so the microphone's first blocks,
which pile up while the window checks the text box, count as what they are, and a test can feed
audio faster than real time. The one clock used is the stall check: a microphone that sends nothing
for a few seconds has stopped.
"""
import queue
import time
from collections.abc import Callable
from dataclasses import dataclass
from threading import Event

import numpy as np

from aj.config import DictationSettings, VoiceSettings
from aj.core.audio import MIC_BLOCK, MIC_RATE

# Why a recording ended.
SILENCE = "silence"      # he stopped talking
FINISHED = "finished"    # he clicked the mic again, or pressed the hotkey again
CAPPED = "capped"        # DictationSettings.max_secs
NOTHING = "nothing"      # DictationSettings.start_secs passed and he had said nothing
CANCELLED = "cancelled"  # the small x
STALLED = "stalled"      # the microphone stopped sending sound

POLL_SECS = 0.1    # how often a click or a cancel is noticed while no sound is arriving
STALL_SECS = 3.0   # no sound from the microphone for this long: it has stopped


class Endpointer:
    """Silero VAD through sherpa-onnx, set up as core/speech.Ears sets it up, without Parakeet."""

    def __init__(self, voice: VoiceSettings):
        from aj.core.gpu_libs import add_cuda_dll_dirs
        add_cuda_dll_dirs()  # before sherpa_onnx loads onnxruntime, as speech.py does
        import sherpa_onnx as so
        self.min_silence = voice.end_silence_secs
        config = so.VadModelConfig(
            silero_vad=so.SileroVadModelConfig(model=voice.vad_model, threshold=0.5,
                                               min_silence_duration=voice.end_silence_secs,
                                               min_speech_duration=0.2, window_size=MIC_BLOCK,
                                               max_speech_duration=30),
            sample_rate=MIC_RATE, num_threads=1)
        self.vad = so.VoiceActivityDetector(config, buffer_size_in_seconds=60)

    def _pieces(self) -> list[np.ndarray]:
        out = []
        while not self.vad.empty():
            out.append(np.asarray(self.vad.front.samples, dtype=np.float32))
            self.vad.pop()
        return out

    def feed(self, block: np.ndarray) -> list[np.ndarray]:
        """One microphone block; the pieces of speech that just ended."""
        self.vad.accept_waveform(block)
        return self._pieces()

    @property
    def hearing_speech(self) -> bool:
        return self.vad.is_speech_detected()

    def flush(self) -> list[np.ndarray]:
        """Whatever speech is still open (he stopped it mid-word with a click), as a last piece."""
        self.vad.flush()
        return self._pieces()

    def reset(self) -> None:
        self.vad.reset()


@dataclass(frozen=True)
class Recording:
    pieces: list
    reason: str
    secs: float


def level_of(block: np.ndarray) -> float:
    """How loud a block is, 0 to 1, for the level bar: room noise a sliver, normal speech most of it."""
    rms = float(np.sqrt(np.mean(np.square(block)))) if len(block) else 0.0
    return min(1.0, (rms * 14.0) ** 0.7)


def record(mic, vad, s: DictationSettings, finish: Event, cancelled: Callable[[], bool],
           on_level: Callable[[float], None] | None = None, stall_secs: float = STALL_SECS) -> Recording:
    """Read the open microphone until he is done, and return the pieces of speech.

    Ends on: `finish` set (a second click), `cancelled()`, s.silence_secs of quiet after he spoke,
    s.start_secs without a word, s.max_secs in all, or a stalled microphone. `vad` is anything with
    feed(block) -> pieces, hearing_speech, flush(), reset() and min_silence (Endpointer)."""
    vad.reset()
    pieces: list[np.ndarray] = []
    heard_at = 0.0  # audio seconds elapsed
    quiet = 0.0     # seconds of quiet since the detector stopped hearing speech
    blocks = 0
    last_sound = time.monotonic()
    reason = ""
    while not reason:
        if cancelled():
            reason = CANCELLED
        elif finish.is_set():
            reason = FINISHED
        else:
            try:
                block = mic.blocks.get(timeout=POLL_SECS)
            except queue.Empty:
                if time.monotonic() - last_sound >= stall_secs:
                    reason = STALLED
                continue
            last_sound = time.monotonic()
            block_secs = len(block) / MIC_RATE
            heard_at += block_secs
            blocks += 1
            if on_level is not None and blocks % 2 == 0:  # about 15 updates a second is plenty
                on_level(level_of(block))
            pieces.extend(vad.feed(block))
            talking = vad.hearing_speech
            quiet = 0.0 if talking else quiet + block_secs
            # The detector only says speech has ended after min_silence of quiet, so that much of
            # the quiet had already passed when it said so. A noise too short to count as speech
            # leaves no piece, so it can't end the recording (a cough before he starts).
            if pieces and not talking and quiet + vad.min_silence >= s.silence_secs:
                reason = SILENCE
            elif not pieces and not talking and heard_at >= s.start_secs:
                reason = NOTHING
            elif heard_at >= s.max_secs:
                reason = CAPPED
    if on_level is not None:
        on_level(0.0)
    if reason != CANCELLED:
        pieces.extend(vad.flush())
    return Recording(pieces, reason, heard_at)
