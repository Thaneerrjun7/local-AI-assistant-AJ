"""Interrupting AJ through the laptop's own speakers.

The boss's ask (2026-10-18): when he talks over AJ, AJ should stop, hear him out, and
answer with the earlier conversation, what he'd already said and the new words all in mind.
With speakers, though, the mic hears AJ's own voice too, so "someone is talking" proves
nothing (until now speech was simply ignored while he talked, unless --headphones was on).

So while he talks, speech the mic hears is written down (the last ~1.2 s, about 0.1 s of work
for Parakeet) and compared with what he has just been saying. Mostly the same words: his own
voice coming back, ignored. Different words: the boss. "Stop", "wait", "hold on" stop him at
once. It's checked again every so often while the speech goes on, since the boss may start
talking in the middle of the echo.
"""
import re
import time
from collections import deque
from collections.abc import Callable

import numpy as np

from aj.core.audio import MIC_BLOCK, MIC_RATE

WINDOW_SECS = 1.2        # how much of the latest audio is written down for each check
FIRST_CHECK_SECS = 0.45  # speech heard for this long before the first check
RECHECK_SECS = 0.8       # then again this often, while it lasts
ECHO_SHARE = 0.5         # this share of the words heard being his own: it's his echo

STOP = re.compile(r"^\W*(?:(?:computer|aj|a\W?j|hey|okay|ok|no|please)\W+)*(?:stop|wait|hold on|hang on|shut up|be quiet|"
                  r"quiet|enough|pause|one sec(?:ond)?)\b", re.IGNORECASE)
_WORD = re.compile(r"[a-z][a-z']+")


def _stems(text: str) -> set[str]:
    out = set()
    for word in _WORD.findall(text.lower()):
        word = word.replace("'", "")
        for suffix in ("ing", "ed", "es", "s"):
            if word.endswith(suffix) and len(word) - len(suffix) >= 3:
                word = word[:-len(suffix)]
                break
        if len(word) >= 3:
            out.add(word)
    return out


def is_echo(heard: str, spoken: str) -> bool:
    """Is what the mic heard just AJ's own words coming back?"""
    if STOP.match(heard):
        return False
    words = _stems(heard)
    if not words:
        return True  # nothing anyone could act on: a breath, a murmur, half a word of echo
    return len(words & _stems(spoken)) / len(words) >= ECHO_SHARE


class BargeIn:
    """Fed every mic block while AJ talks; says when the boss has started talking over him."""

    def __init__(self, transcribe: Callable[[np.ndarray], str], spoken: Callable[[], str],
                 clock: Callable[[], float] = time.monotonic):
        self.transcribe, self.spoken, self.clock = transcribe, spoken, clock
        self._ring: deque[np.ndarray] = deque(maxlen=max(1, int(WINDOW_SECS * MIC_RATE / MIC_BLOCK)))
        self._since: float | None = None   # when the speech now being heard began
        self._checked = 0.0

    def reset(self) -> None:
        self._ring.clear()
        self._since = None

    def recent_audio(self) -> np.ndarray:
        """The last WINDOW_SECS of mic audio: the start of what the boss said over AJ."""
        return np.concatenate(list(self._ring)) if self._ring else np.zeros(0, dtype=np.float32)

    def feed(self, block: np.ndarray, hearing_speech: bool) -> bool:
        """True once the speech being heard is the boss, not AJ's echo."""
        self._ring.append(block)
        now = self.clock()
        if not hearing_speech:
            self._since = None
            return False
        if self._since is None:
            self._since, self._checked = now, 0.0
        if now - self._since < FIRST_CHECK_SECS:
            return False
        if self._checked and now - self._checked < RECHECK_SECS:
            return False
        self._checked = now
        heard = self.transcribe(self.recent_audio())
        return not is_echo(heard, self.spoken())
