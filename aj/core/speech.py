"""Speech in and out via sherpa-onnx: Silero VAD (CPU), Parakeet STT, and the voice: Kokoro (GPU or
CPU) or Piper (CPU)."""
import hashlib
import os
import re
import threading
import time
from collections import OrderedDict
from dataclasses import replace
from pathlib import Path

import numpy as np

from aj.core.gpu_libs import add_cuda_dll_dirs

add_cuda_dll_dirs()  # before sherpa_onnx loads onnxruntime's CUDA provider
import sherpa_onnx as so  # noqa: E402

from aj.config import VoiceSettings  # noqa: E402
from aj.core.audio import MIC_BLOCK, MIC_RATE  # noqa: E402
from aj.core.chunker import for_voice, speech_parts  # noqa: E402
from aj.core.voice_fx import CLEAR, FxSettings, VoiceFx  # noqa: E402

# Words people trail off on mid-thought: if the transcript ends on one, wait a bit longer.
# Parakeet punctuates, so a closing "?" means done; strong words ("and", "the", "um") mean
# unfinished even if it added a period; softer ones ("like", "then") only with no punctuation.
_STRONG = re.compile(r"\b(and|but|so|or|the|a|an|to|of|my|for|with|because|um+|uh+)[\s,.…-]*$", re.IGNORECASE)
_SOFT = re.compile(r"\b(like|then|is|are|was|if|when)[\s,…-]*$", re.IGNORECASE)


# Voices the user has rejected. Never use these, whatever settings.toml says.
BANNED_VOICES = {26: "bm_george (rejected by the user, 2026-10-04)"}

# British voices in kokoro-multi-lang v1.0 (bf_* 20-23, bm_* 24-27) speak with British
# pronunciation: its own dictionary and espeak's British English for words not in it. In this
# espeak data British English is plain "en"; "en-gb" isn't a voice and gives silence (measured).
BRITISH_VOICES = range(20, 28)


def accent(speaker_id: int) -> tuple[str, str]:
    """(pronunciation dictionary, espeak language) for a voice."""
    return ("lexicon-gb-en.txt", "en") if speaker_id in BRITISH_VOICES else ("lexicon-us-en.txt", "en-us")

_CACHE_MAX_WORDS = 6   # only short phrases repeat often enough to be worth keeping
_CACHE_ENTRIES = 300   # ~1 s of 24 kHz float32 audio each: roughly 30 MB at most

_QUIET = 0.01           # below this the effects' reverb tail is inaudible
SLOW = 0.88             # [slow]: the half-sung strings line
PAUSE_SECS = {"short": 0.38, "long": 0.6}  # "..." and [pause]
CHUCKLE = "Heh heh."
SIGH_WORDS = ("Hmm.", "Ugh.")  # a [sigh] in a fixed line is said, not synthesized
SENTENCE_GAP = 0.35      # seconds between sentences
MID_SENTENCE_GAP = 0.12  # after a chunk cut mid-sentence: a beat, not a full stop
_ENDS_SENTENCE = re.compile(r"[.!?…][\"')\]]*$")


def looks_unfinished(text: str) -> bool:
    text = text.strip()
    if text.endswith("?"):
        return False
    return bool(_STRONG.search(text) or _SOFT.search(text))


class Ears:
    """Voice activity detection plus transcription."""

    def __init__(self, s: VoiceSettings):
        vad_cfg = so.VadModelConfig(
            silero_vad=so.SileroVadModelConfig(model=s.vad_model, threshold=0.5,
                                               min_silence_duration=s.end_silence_secs,
                                               min_speech_duration=0.2, window_size=MIC_BLOCK,
                                               max_speech_duration=30),
            sample_rate=MIC_RATE, num_threads=1)
        self.vad = so.VoiceActivityDetector(vad_cfg, buffer_size_in_seconds=60)
        d = Path(s.stt_dir)
        self.recognizer = so.OfflineRecognizer.from_transducer(
            encoder=str(d / "encoder.int8.onnx"), decoder=str(d / "decoder.int8.onnx"),
            joiner=str(d / "joiner.int8.onnx"), tokens=str(d / "tokens.txt"),
            num_threads=s.stt_threads, model_type="nemo_transducer", provider=s.stt_provider)
        # Dictation (2026-10-22) can transcribe on a tool thread while barge-in transcribes on the
        # loop thread; one decode at a time on the shared recognizer.
        self._decoding = threading.Lock()

    def feed(self, block: np.ndarray) -> list[np.ndarray]:
        """Feed one mic block; returns any utterances that just ended."""
        self.vad.accept_waveform(block)
        out = []
        while not self.vad.empty():
            out.append(np.asarray(self.vad.front.samples, dtype=np.float32))
            self.vad.pop()
        return out

    @property
    def hearing_speech(self) -> bool:
        return self.vad.is_speech_detected()

    def reset(self) -> None:
        self.vad.reset()

    def transcribe(self, samples: np.ndarray, rate: int = MIC_RATE) -> str:
        with self._decoding:
            stream = self.recognizer.create_stream()
            stream.accept_waveform(rate, samples)
            self.recognizer.decode_stream(stream)
            return stream.result.text.strip()


def fit_tail(audio: np.ndarray, rate: int, secs: float) -> np.ndarray:
    """Set the trailing silence to exactly `secs`, trimming or padding, so every pause is
    chosen rather than whatever the voice (or the effects' reverb) happened to leave."""
    loud = np.flatnonzero(np.abs(audio) > _QUIET)
    end = (int(loud[-1]) + 1 if len(loud) else 0) + int(rate * secs)
    if end <= len(audio):
        return audio[:end]
    return np.concatenate([audio, np.zeros(end - len(audio), dtype=np.float32)])


def silence(rate: int, secs: float) -> np.ndarray:
    return np.zeros(int(rate * secs), dtype=np.float32)


class Voice:
    """Kokoro text-to-speech (plain Onyx by default; the effects chain is optional), real pauses
    and a chuckle.

    Lines rendered ahead of time are kept on disk too (`cache_dir`), keyed by the voice, speed,
    effects and words, so later starts load them in milliseconds. Rendering all ~40 rotating
    lines takes ~20 s of GPU time, and doing that at every start slowed the first replies
    (measured 2026-10-06)."""

    def __init__(self, s: VoiceSettings, cache_dir: Path | None = None):
        if s.speaker_id in BANNED_VOICES:
            raise ValueError(f"voice {s.speaker_id} is banned: {BANNED_VOICES[s.speaker_id]}")
        d = Path(s.kokoro_dir)
        model = next(p for p in (d / "model.onnx", d / "model.int8.onnx") if p.exists())
        words, lang = accent(s.speaker_id)
        lexicon = ",".join(str(p) for p in (d / words, d / "lexicon-zh.txt") if p.exists())
        self.tts = so.OfflineTts(so.OfflineTtsConfig(
            model=so.OfflineTtsModelConfig(
                kokoro=so.OfflineTtsKokoroModelConfig(
                    model=str(model), voices=str(d / "voices.bin"), tokens=str(d / "tokens.txt"),
                    lexicon=lexicon, data_dir=str(d / "espeak-ng-data"), dict_dir=str(d / "dict"),
                    lang=lang),
                num_threads=s.tts_threads, provider=s.tts_provider),
            max_num_sentences=1))
        self._setup(s, model.name, cache_dir)

    def _setup(self, s: VoiceSettings, model_name: str, cache_dir: Path | None) -> None:
        """Everything but the engine itself, which the subclasses load their own way."""
        self.settings = s
        self.sid, self.speed = getattr(self, "sid", s.speaker_id), getattr(self, "speed", s.speed)
        preset = CLEAR if s.fx_preset == "clear" else FxSettings()
        self.fx = VoiceFx(replace(preset, enabled=s.effects))
        self.rate = self.tts.sample_rate
        self.cache_dir = Path(cache_dir) if cache_dir is not None else None
        self._fingerprint = f"{model_name}|{self.fx.settings}"
        self._cache: OrderedDict[str, np.ndarray] = OrderedDict()
        self._kept: dict[str, np.ndarray] = {}  # pre-rendered lines: never evicted
        self._lock = threading.Lock()  # one render at a time (the mouth and background pre-rendering)
        self._last_live = 0.0  # when the mouth last asked for a render
        self._sighs = 0

    def _disk(self, key: str) -> Path | None:
        if self.cache_dir is None:
            return None
        return self.cache_dir / (hashlib.sha1(f"{self._fingerprint}|{key}".encode()).hexdigest() + ".npy")

    def _words(self, text: str, speed: float, keep: bool = False) -> np.ndarray:
        """Rendered words. Short phrases are cached: AJ repeats himself a lot ("Noted.",
        "Opening it."), and a cached phrase plays with no render wait. A render costs ~0.45 s
        for a short line and ~1 s for twenty words on the GPU; a cache hit costs nothing.
        `keep` pins it for good, on disk as well."""
        if self.bans_apply and self.sid in BANNED_VOICES:
            raise ValueError(f"voice {self.sid} is banned: {BANNED_VOICES[self.sid]}")
        words = text.lower().split()
        key = f"{self.sid}|{speed:.3f}|{' '.join(words)}"
        cached = self._kept.get(key)
        if cached is None:
            cached = self._cache.get(key)
            if cached is not None:
                self._cache.move_to_end(key)
        if cached is not None:
            if keep:
                self._kept[key] = cached
            return cached
        disk = self._disk(key) if keep else None
        if disk is not None and disk.exists():
            try:
                self._kept[key] = out = np.load(disk)
                return out
            except (OSError, ValueError):
                pass  # unreadable: render it again below
        with self._lock:
            out = self._render(text, speed)
        if keep:
            self._kept[key] = out
            if disk is not None:
                self._save(disk, out)
        elif len(words) <= _CACHE_MAX_WORDS:
            self._cache[key] = out
            if len(self._cache) > _CACHE_ENTRIES:
                self._cache.popitem(last=False)
        return out

    def _render(self, text: str, speed: float) -> np.ndarray:
        """The voice engine itself: Kokoro here; ChatterboxVoice replaces just this (and say)."""
        audio = self.tts.generate(text, sid=self.sid, speed=speed)
        return self.fx.apply(np.asarray(audio.samples, dtype=np.float32), audio.sample_rate)

    bans_apply = True  # BANNED_VOICES are Kokoro's speaker numbers

    @property
    def uses_gpu(self) -> bool:
        return self.settings.tts_provider == "cuda"

    def close(self) -> None:
        """Nothing to release for Kokoro: it lives in AJ's own process."""

    @staticmethod
    def _save(path: Path, audio: np.ndarray) -> None:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_name(path.stem + ".tmp.npy")
            np.save(tmp, audio)
            os.replace(tmp, path)
        except OSError:
            pass  # only a cache: it renders again next time

    def _sound(self, name: str, speed: float) -> np.ndarray:
        """A chuckle or a sigh, said in his own voice ("Heh heh.", "Hmm."). Synthesized breaths
        and sighs were tried first and sounded fake (user, 2026-10-06)."""
        if name == "chuckle":
            return fit_tail(self._words(CHUCKLE, speed, keep=True), self.rate, 0.12)
        word = SIGH_WORDS[self._sighs % len(SIGH_WORDS)]
        self._sighs += 1
        return fit_tail(self._words(word, speed, keep=True), self.rate, 0.2)

    def say(self, text: str, keep: bool = False) -> np.ndarray:
        """Rendered audio for one line, marks and all: a "..." that really pauses, a chuckle.
        A finished sentence ends with the pause before the next one; a chunk cut mid-sentence
        (at a comma, before an "and") gets only a short beat."""
        if not keep:
            self._last_live = time.monotonic()
        parts = speech_parts(for_voice(text))
        speed, out = self.speed, []
        for i, (kind, value) in enumerate(parts):
            if kind == "slow":
                speed = self.speed * SLOW
            elif kind == "pause":
                out.append(silence(self.rate, PAUSE_SECS[value]))
            elif kind == "sound":
                out.append(self._sound(value, speed))
            else:
                audio = self._words(value, speed, keep)
                if i < len(parts) - 1:  # mid-line: the next pause or sound sets the gap
                    gap = 0.05 if parts[i + 1][0] == "pause" else 0.15
                else:
                    gap = SENTENCE_GAP if _ENDS_SENTENCE.search(value) else MID_SENTENCE_GAP
                out.append(fit_tail(audio, self.rate, gap))
        if not keep:
            self._last_live = time.monotonic()
        return np.concatenate(out) if out else silence(self.rate, 0.05)

    def prerender(self, phrases: list[str]) -> None:
        for phrase in phrases:
            self.say(phrase, keep=True)

    def prerender_when_idle(self, phrases: list[str], busy=lambda: False, idle_secs: float = 2.0) -> None:
        """Render (or load from disk) in the background, one line at a time, only while AJ
        hasn't spoken for a couple of seconds and isn't answering, so it never delays a reply."""
        def run() -> None:
            for phrase in phrases:
                while busy() or time.monotonic() - self._last_live < idle_secs:
                    time.sleep(0.25)
                try:
                    self.say(phrase, keep=True)
                except Exception:  # the voice is shutting down (AJ switched off): stop quietly
                    return

        threading.Thread(target=run, daemon=True, name="prerender").start()


class PiperVoice(Voice):
    """A Piper (VITS) voice through sherpa-onnx, on the CPU, so no graphics memory at all.

    Tried at the boss's request (2026-10-19) after asking for a voice with almost no drain. Measured:
    ready in under 2 s (Kokoro's GPU start took 4 s), about 0.1 s for a short line and 0.3-0.5 s
    for a long one, 25 times faster than it speaks. Everything but the engine is Voice's: real
    pauses, cached phrases, a chuckle."""

    bans_apply = False

    def __init__(self, s: VoiceSettings, cache_dir: Path | None = None):
        d = Path(s.piper_dir) / s.piper_voice
        model = next(d.glob("*.onnx"), None)
        if model is None:
            raise FileNotFoundError(f"no Piper voice in {d}")
        self.tts = so.OfflineTts(so.OfflineTtsConfig(
            model=so.OfflineTtsModelConfig(
                vits=so.OfflineTtsVitsModelConfig(model=str(model), tokens=str(d / "tokens.txt"),
                                                  data_dir=str(d / "espeak-ng-data"), noise_scale=s.piper_noise,
                                                  noise_scale_w=s.piper_noise_w),
                num_threads=s.piper_threads, provider="cpu"),
            max_num_sentences=1))
        self.sid, self.speed = s.piper_speaker, s.piper_speed
        self._setup(s, model.name, cache_dir)

    @property
    def uses_gpu(self) -> bool:
        return False
