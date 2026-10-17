"""Ultron's voice as Chatterbox Turbo, copying the user's own recording (2026-10-06): the user
found every Kokoro voice flat, and Chatterbox acts (real sighs and chuckles) in a cloned voice.

The model runs in a separate process (ultron/voice_server.py) in its own environment, since it
needs PyTorch. This class starts it, ties it to Ultron so it dies with him (the user's rule:
off means everything heavy is off, and nothing starts by itself), and sends it one line at a
time. Everything else (caching, the disk cache, rendering while idle) is the Kokoro Voice's.

Measured on this laptop with Qwen loaded (2026-10-06): 5.8 of 6.1 GB of graphics memory,
~1.8 GB of RAM, ~15 s to load, and about 1.4x faster than real time (a short line ~1.9 s).
There's no room left for the thinker model; see engine.py.
"""
import hashlib
import json
import re
import subprocess
import threading
import time
from collections import OrderedDict
from pathlib import Path

import numpy as np

from ultron.config import APP_DIR, LOG_DIR, VoiceSettings
from ultron.core.chunker import for_voice
from ultron.core.llm import NO_WINDOW
from ultron.core.proc import bind_to_ultron
from ultron.core.speech import MID_SENTENCE_GAP, SENTENCE_GAP, _ENDS_SENTENCE, Voice, fit_tail, silence

SERVER = APP_DIR / "ultron" / "voice_server.py"
_TAG_AT_END = re.compile(r"\s*\[[a-z ]+\]\s*$")


def to_chatterbox(text: str) -> str:
    """Ultron's text as Chatterbox should read it. [sigh] and [chuckle] are Chatterbox's own
    tags, so they stay and it performs them; [pause] becomes "..."; [slow] has no meaning for
    it. Accents and shouting are handled as for any voice (chunker.for_voice)."""
    text = re.sub(r"\[pause\]", "...", for_voice(text), flags=re.IGNORECASE)
    text = re.sub(r"\[slow\]", "", text, flags=re.IGNORECASE)
    return " ".join(text.split())


class ChatterboxVoice(Voice):
    real_sighs = True  # Delivery leaves [sigh] for it to perform, instead of saying "Hmm."

    def __init__(self, s: VoiceSettings, cache_dir: Path | None = None):
        clip = Path(s.voice_clip) if s.voice_clip and Path(s.voice_clip).exists() else None
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        self._log = open(LOG_DIR / "voice-server.log", "w", encoding="utf-8")
        config = {"hf_home": s.hf_home, "clip": str(clip) if clip else None}
        self.proc = subprocess.Popen([s.chatterbox_python, str(SERVER), json.dumps(config)],
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self._log,
                                     creationflags=NO_WINDOW)
        bind_to_ultron(self.proc)  # it ends when Ultron does, however he ends
        try:
            ready = self._receive()
        except Exception:
            self.close()
            raise
        self.settings, self.rate = s, int(ready["rate"])
        self.cache_dir = Path(cache_dir) if cache_dir is not None else None
        voice = hashlib.sha1(clip.read_bytes()).hexdigest()[:16] if clip else "built-in"
        self._fingerprint = f"chatterbox-turbo|{voice}"  # a new recording means new audio, not stale copies
        self._cache: OrderedDict[str, np.ndarray] = OrderedDict()
        self._kept: dict[str, np.ndarray] = {}
        self._lock = threading.Lock()
        self._last_live = 0.0
        self._sighs = 0

    def _receive(self) -> dict:
        line = self.proc.stdout.readline()
        if not line:
            raise RuntimeError(f"the Chatterbox voice stopped (see {LOG_DIR / 'voice-server.log'})")
        reply = json.loads(line)
        if "error" in reply:
            raise RuntimeError(f"Chatterbox: {reply['error']}")
        return reply

    def _render(self, text: str, speed: float) -> np.ndarray:
        """One line from the voice process (the caller holds the lock: one request at a time).
        `speed` isn't used: Chatterbox has no speed setting."""
        self.proc.stdin.write((json.dumps({"text": text}) + "\n").encode())
        self.proc.stdin.flush()
        size = int(self._receive()["samples"]) * 4
        data = bytearray()
        while len(data) < size:
            piece = self.proc.stdout.read(size - len(data))
            if not piece:
                raise RuntimeError("the Chatterbox voice stopped mid-line")
            data += piece
        return np.frombuffer(bytes(data), dtype="<f4").astype(np.float32)

    def say(self, text: str, keep: bool = False) -> np.ndarray:
        """One line, sent whole: Chatterbox paces a "..." itself and performs the tags, so the
        line isn't split up the way Kokoro's is. It ends with the pause before the next one."""
        if not keep:
            self._last_live = time.monotonic()
        words = to_chatterbox(text)
        if not words:
            return silence(self.rate, 0.05)
        audio = self._words(words, self.settings.speed, keep)
        gap = SENTENCE_GAP if _ENDS_SENTENCE.search(_TAG_AT_END.sub("", words)) else MID_SENTENCE_GAP
        if not keep:
            self._last_live = time.monotonic()
        return fit_tail(audio, self.rate, gap)

    def close(self) -> None:
        """Stop the voice process: closing its input ends it; if it hangs, it's killed."""
        if self.proc.poll() is None:
            try:
                self.proc.stdin.close()
                self.proc.wait(timeout=5)
            except (OSError, subprocess.TimeoutExpired):
                self.proc.kill()
        self.proc.stdout.close()
        self._log.close()
