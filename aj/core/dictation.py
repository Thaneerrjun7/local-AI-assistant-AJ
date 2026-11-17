"""Dictation: the user says it, and it's typed exactly where his cursor is, in any app.

The user, 2026-10-22: "I'll say to the AI and it'll type it on exactly where I want. It could be
my Google Chrome messages. It could be WhatsApp messages. It could be anywhere... I want it to be
accurate." And: "I do not want this AI to type the message and send it for me. I'll do that
myself." So:
- Gemini writes the words down (gemini.transcribe), because Parakeet, fine for commands, gets
  names and punctuation wrong in a message. Parakeet stands in only when Gemini can't (no key,
  offline, an error, or an answer that can't be a transcript of that much audio), and then he's
  told to check it before he sends it.
- It types into the window that was in front when he asked, never Computer's own: a FrontWatch
  remembers the last other window, for when he typed the request into the assistant's box.
- It types only when UI Automation says the focus is in a text box (uia.focused_editable() is
  True). Keys typed anywhere else can fire an app's shortcuts (Gmail acts on single letters). If
  it can't, the words are kept, and "type that" types them once he has clicked into the box.
- Never Enter, never the clipboard (core/keyboard.py). A newline is Shift+Enter only in a box
  known to take line breaks (WhatsApp's message box, a document, an edit box UI Automation calls
  multi-line), and a space everywhere else: Shift+Enter submits a single-line Chrome input or the
  Run box (the review of 2026-10-22). He presses Send. The spoken line says where it went and that
  sending is his ("Typed it in WhatsApp. Press send when you're ready."); nothing here says "sent".
- The keys follow the box, not just the window: before each piece, the focused element must be
  the one checked at the start (its UI Automation runtime id), or typing stops. A click into the
  address bar of the same window used to get the rest of the message (the review of 2026-10-22).
- It can be stopped: a cancel event is checked before the window is brought forward, before each
  piece, between Gemini's tries and before a dictation is armed. It is set when the dispatcher
  gives up on the tool (its timeout), and when the user says stop or cancel, or talks over the
  assistant (Dictation.abort). Until 2026-10-22 an abandoned tool could still type afterwards.
- "Make it nicer" rewrites the words as refined, polite English (same meaning, nothing added).
  Right after typing, it replaces what was typed only when that is provably safe: same window and
  box, the box still ends with exactly those characters, the caret is at its very end with nothing
  selected (checked before every single Backspace, so none can reach his own text), one Backspace
  per character. Otherwise it says so and offers to type the polished version instead ("type that").
- It and AJ (Speak) never take dictation at the same time (core/mic_share.py): while AJ (Speak)
  listens, "write this down" says so; while this one waits for his words, AJ (Speak) does.

Gemini models compared for the transcript, live on 2026-10-22: seven test sentences said by the
project's Piper voice (rendered silently, 21.6 s in all), with Appa, Amma and Suhinish, numbers
("7:30", "10:15", "45 ringgit"), questions, a spoken "new line", "full stop" and "question mark",
and Manglish ("Okay lah, see you at the mamak later"):
- gemini-3.5-transcribe: 6 of 7 answered (one per-minute quota 429), median 1.8 s, max 2.1 s;
  word error 13% on those six, two exact ("Hi Appa, I'll be home by 7:30 tonight.", and "...full
  stop can you pick me up question mark" came back as "...in the morning. Can you pick me up?").
  Appa and Amma right; Suhinish not ("Yinish", "Suenishi"); the Manglish line was its worst.
- gemini-3.8-flash: overloaded that day (HTTP 503 "high demand", or no answer within 30 s);
  1 of 9 tries answered, after 41 s (correctly).
- Parakeet (the offline ears) on the same six: word error 23%, "Emma" for Amma, "7.30", "1015",
  and "full stop" written out as words.
So the transcription model is the default (config.py switches it). It refuses a system
instruction and answers in parts[].audioTranscription (core/gemini.py handles both). It drops a
spoken "new line" without putting a line break in (checked twice, with and without the rules), so
line breaks by voice work only when the words reach said_punctuation() (Parakeet's transcripts).
For "make it nicer", gemini-3.1-flash-lite rewrote a test line in 4.9 s; gemini-3.8-flash gave no
answer within 20 s that day.

Nothing here imports the brain, the pipeline or Qt: the stand-alone dictation window uses the same
functions (transcribe, polish, choose_target, type_into, replace_typed, FrontWatch). The Dictation
class at the bottom is the assistant's own state on top of them (a dictation it is waiting for,
the words kept for "type that", what it typed last).
"""
import ctypes
import io
import logging
import os
import re
import threading
import time
import unicodedata
import wave
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from ctypes import wintypes
from dataclasses import dataclass, field, replace

import numpy as np

from aj.config import DictationSettings
from aj.core import gemini, mic_share, uia
from aj.core.audio import MIC_RATE

log = logging.getLogger(__name__)

# ---- what he hears ----------------------------------------------------------------------------
# Plain and short: the words are on his screen, so nothing is read back.
GO_AHEAD = "Go ahead."
ASK_MESSAGE = "What would you like to say?"  # after a WhatsApp chat opens (the user, 2026-10-22)
TYPED = "Typed it."
TYPED_POLISHED = "Typed the polished version."
TYPED_UNPOLISHED = "Couldn't polish it, so I typed it as you said it."
REPLACED = "Replaced it with the polished version."
PRESS_SEND = "Press send when you're ready."
OFFLINE = "Gemini wasn't available, so I used the offline ears. Check it before you send."
CANCELLED = "Okay. Nothing typed."
NOTHING_HEARD = "Nothing heard, so dictation is off."
NOTHING_TO_TYPE = "I didn't catch any words to type."
CAPPED = "I stopped at one minute. The rest wasn't typed."
CLICK_FIRST = "Click into the box where you want it, then say type that."
CLICK_FIRST_ARM = "Click into the box where you want it, then say write this down."
NO_WINDOW = "I don't know where to type. Click into the box where you want it, then ask again."
REFUSED = "Windows stopped me typing there. Check the box."
# The window, or the box in it, changed mid-way (since 2026-10-22 the box is followed too).
MOVED_AWAY = "The cursor left that box, so I stopped. Part of it is typed."
STOPPED = "Stopped. Nothing typed."
STOPPED_PART = "Stopped, so only part of it is typed. Check the box."
KEYS_HELD = "A key was still held down, so I didn't type. Let go of it and say type that."
SPEAK_BUSY = "AJ (Speak) is listening right now, so I won't take dictation too. Finish that first."
NOT_AT_END = ("Your cursor isn't at the end of what I typed, so I left it as it is. "
              "Say type that and I'll type the polished version where your cursor is.")
NOTHING_READY = "Nothing to type yet. Say write this down first."
NOTHING_TYPED = "I haven't typed anything to polish."
NO_POLISH = "I couldn't polish it just now. It's still as you said it."
ALREADY_FINE = "It already reads well, so I left it."
CANT_REPLACE = "I can't safely replace it, the box has changed. Say type that and I'll type the polished version after it."
CHECK_BOX = "Something changed while I was replacing it, so I stopped. Check the box."

KEEP_SECS = 600.0      # words kept for "type that", and what was typed for "make it nicer"
JOIN_GAP_SECS = 0.3    # silence put back between the VAD's pieces of one dictation
QUICK_SECS = 3.0       # a dictation this short is checked for "cancel" locally first
_CHUNK = 64            # characters typed between checks that his window and box still have the focus
_WORDS_PER_SEC = 7.0   # faster than anyone talks: a longer "transcript" is Gemini making things up
BUSY_RETRY_SECS = 1.5  # an overloaded or rate-limited Gemini gets one more try after this

POLISH_RULES = """Rewrite the user's message so it reads as refined, polite, natural English, the way a thoughtful person would write it.
- Keep exactly the same meaning, and the same language. If it mixes in Malay or Tamil words, keep them.
- It is the user's own message to someone else: rewrite it, never answer it or reply to it.
- You may soften the tone (please, thank you, sorry), but add no new facts, names, times, places or promises.
- Don't add a greeting or a sign-off unless it already has one.
- Keep names exactly as written (for example Appa, Amma, Suhinish).
- Keep it about as long as it is, and keep its line breaks.
- Reply with the rewritten message only: no quotation marks, no notes, no choices."""


# ---- audio ----------------------------------------------------------------------------------------

def wav_bytes(samples: np.ndarray, rate: int = MIC_RATE) -> bytes:
    """Float audio (-1..1) as a 16-bit mono WAV file, in memory: what gemini.transcribe sends."""
    pcm = (np.clip(np.asarray(samples, dtype=np.float32), -1.0, 1.0) * 32767).astype("<i2")
    out = io.BytesIO()
    with wave.open(out, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm.tobytes())
    return out.getvalue()


def _pieces(samples) -> list[np.ndarray]:
    if samples is None:
        return []
    if isinstance(samples, np.ndarray):
        samples = [samples]
    return [np.asarray(p, dtype=np.float32) for p in samples if p is not None and len(p)]


def joined(pieces: Sequence[np.ndarray], rate: int = MIC_RATE) -> np.ndarray:
    """The VAD's pieces of one dictation as one recording, with a short pause between them, so
    the words don't run together (the VAD keeps the speech and drops the pauses)."""
    pieces = _pieces(pieces)
    if not pieces:
        return np.zeros(0, np.float32)
    gap = np.zeros(int(rate * JOIN_GAP_SECS), np.float32)
    out = [pieces[0]]
    for piece in pieces[1:]:
        out += [gap, piece]
    return np.concatenate(out)


# ---- transcribing -------------------------------------------------------------------------------

@dataclass(frozen=True)
class Heard:
    text: str
    engine: str  # "gemini", "offline" (Parakeet), "given" (typed or passed in) or "" (nothing)


_LABEL = re.compile(r"^\s*(?:here(?:'s| is) the )?(?:transcript(?:ion)?|text)\s*:\s*", re.IGNORECASE)
_WRAPPED = re.compile(r'^\s*["“](.*)["”]\s*$', re.DOTALL)
_NO_SPEECH = re.compile(r"^\s*[\[(][^\])]*[\])]\s*$")  # "[silence]", "(no speech)"


def _clean_heard(text: str) -> str:
    text = _LABEL.sub("", text.replace("\r\n", "\n").strip())
    wrapped = _WRAPPED.match(text)
    if wrapped and '"' not in wrapped.group(1):
        text = wrapped.group(1)
    return "" if _NO_SPEECH.match(text) else text.strip()


# Spoken punctuation left in a transcript as words. Parakeet always writes it out ("Thursday. New
# line. Please...", "in the morning full stop can you pick me up question mark"); the transcription
# model turns "full stop" and "question mark" into marks itself, but drops "new line" without a
# line break (live, 2026-10-22, with and without the rules in the request).
_SAID_MARKS = [
    (re.compile(r"[ \t]*,?[ \t]*\bnew paragraph\b[.,!?]?[ \t]*", re.IGNORECASE), "\n\n"),
    (re.compile(r"[ \t]*,?[ \t]*(?<!\ba )(?<!\bthe )(?<!\bthis )(?<!\bthat )\bnew line\b(?! of\b| for\b)[.,!?]?[ \t]*",
                re.IGNORECASE), "\n"),
    (re.compile(r"[ \t]*,?[ \t]*\bfull stop\b[.,]?[ \t]*", re.IGNORECASE), ". "),
    (re.compile(r"[ \t]*,?[ \t]*\bquestion mark\b[.,?]?[ \t]*", re.IGNORECASE), "? "),
    (re.compile(r"[ \t]*,?[ \t]*\bexclamation (?:mark|point)\b[.,!]?[ \t]*", re.IGNORECASE), "! "),
    (re.compile(r"[ \t]*\bcomma\b[.,]?[ \t]*", re.IGNORECASE), ", "),
]
_MARKED = "\x00"


def said_punctuation(text: str) -> str:
    """ "...Thursday. New line. Please bring it full stop" -> "...Thursday.\\nPlease bring it." """
    for pattern, mark in _SAID_MARKS:
        text = pattern.sub(mark + (_MARKED if mark != ", " else ""), text)
    text = re.sub(f"{_MARKED}([a-z])", lambda m: m.group(1).upper(), text)  # a new sentence starts
    text = re.sub(r"[ \t]+\n", "\n", text.replace(_MARKED, ""))
    return re.sub(r"\n[ \t]+", "\n", text).strip()


def plausible(text: str, secs: float) -> bool:
    """Could anyone have said this many words in this much audio?"""
    return len(text.split()) <= _WORDS_PER_SEC * secs + 8


def _stopped(cancel) -> bool:
    """Has this job been told to stop? `cancel`: anything with is_set() (a threading.Event), or None."""
    return cancel is not None and cancel.is_set()


def transcribe(samples, rate: int = MIC_RATE, hint: str = "", local: Callable[[np.ndarray], str] | None = None,
               settings: DictationSettings | None = None, cancel=None) -> Heard:
    """What was said: Gemini's transcript, or the local one (`local`, e.g. Ears.transcribe, run on
    each VAD piece) when Gemini is unavailable, gives nothing, or gives more words than the audio
    could hold. `samples`: one recording, or the VAD's pieces of one. `cancel`: checked before each
    try; once it is set nothing more is tried and nothing is returned (Heard("", ""))."""
    pieces = _pieces(samples)
    total = sum(len(p) for p in pieces)
    if not total:
        return Heard("", "")
    s = settings or DictationSettings()
    audio = wav_bytes(joined(pieces, rate), rate)
    models = [s.gemini_model, s.gemini_model, *([s.backup_model] if s.backup_model else [])]
    for i, model in enumerate(models):
        if _stopped(cancel):
            return Heard("", "")
        try:
            text = _clean_heard(gemini.transcribe(audio, hint, model=model, timeout=s.timeout_secs))
        except gemini.GeminiUnavailable as exc:
            log.warning("Gemini (%s) couldn't transcribe: %s", model, exc)
            if exc.busy and i + 1 < len(models):  # 503 "high demand" or a per-minute 429: once more
                time.sleep(BUSY_RETRY_SECS)
                continue
            break
        if text and plausible(text, total / rate):
            return Heard(said_punctuation(text), "gemini")
        log.warning("Gemini's transcript was %s; using the offline ears", "empty" if not text else "implausible")
        break
    if local is None or _stopped(cancel):
        return Heard("", "")
    text = said_punctuation(" ".join(t for t in (str(local(p) or "").strip() for p in pieces) if t))
    return Heard(text, "offline" if text else "")


# ---- polishing ----------------------------------------------------------------------------------

_PREAMBLE = re.compile(r"^\s*(?:sure|okay|ok|here(?:'s| is)[^\n:]*):\s*\n", re.IGNORECASE)


def _clean_polished(text: str) -> str:
    text = _PREAMBLE.sub("", (text or "").replace("\r\n", "\n").strip())
    wrapped = _WRAPPED.match(text)
    return (wrapped.group(1) if wrapped and '"' not in wrapped.group(1) else text).strip()


def _polish_locally(text: str, llm) -> str:
    """The local model's rewrite, when Gemini can't: slot 1, so the conversation's cached prompt
    in slot 0 stays put."""
    if llm is None:
        return ""
    from aj.core.llm import stream_events  # here: the stand-alone window may have no local model
    base, llm_settings = llm
    messages = [{"role": "system", "content": POLISH_RULES}, {"role": "user", "content": text}]
    try:
        return "".join(value for kind, value in stream_events(base, messages, llm_settings, slot=1, temperature=0.3,
                                                              max_tokens=600) if kind == "text")
    except Exception as exc:  # noqa: BLE001 - no local model, or it failed: no polish, never a crash
        log.warning("the local model couldn't polish it: %s", exc)
        return ""


def polish(text: str, settings: DictationSettings | None = None, llm=None, cancel=None) -> str | None:
    """The words as refined, polite, natural English (same meaning, same language, no added
    facts), by Gemini, else the local model (`llm`: (server base URL, LlmSettings)). None when
    neither could, the answer doesn't look like a rewrite of it, or `cancel` was set."""
    text = text.strip()
    if not text or _stopped(cancel):
        return None
    s = settings or DictationSettings()
    try:
        out = gemini.generate(text, system=POLISH_RULES, model=s.polish_model, temperature=0.3,
                              timeout=s.timeout_secs)
    except gemini.GeminiUnavailable as exc:
        if _stopped(cancel):
            return None
        log.warning("Gemini couldn't polish it (%s); trying the local model", exc)
        out = _polish_locally(text, llm)
    if _stopped(cancel):
        return None
    out = _clean_polished(out)
    if not out or len(out) > 3 * len(text) + 80:
        return None
    return out


# ---- the words of a command ----------------------------------------------------------------------

_ADJ = (r"nicer|nice|polite|politer|refined|better|professional|formal|friendlier|friendly|smoother|cleaner|"
        r"proper|kinder|warmer|softer|gentler|classier|respectful|courteous")
POLISH_ASK = (rf"(?:and )?(?:(?:make|making) (?:it|that|this|them)|so (?:that )?it) (?:sounds? |looks? |reads? )?"
              rf"(?:(?:a bit|a little|more|much|really|very) )*(?:{_ADJ})(?: and (?:(?:a bit|more) )?(?:{_ADJ}))*"
              r"|(?:and )?(?:politely|nicely)|(?:and )?(?:polish|clean|tidy) it(?: up)?")
_LEAD = re.compile(r"^[\s,.!:;-]*(?:(?:hey|hi|ok(?:ay)?|so|and|um+|uh+|right|alright|please|just|now|aj|a\.? ?j\.?|"
                   r"computer|can you|could you|would you|will you|go ahead and|i (?:want|need) you to)[\s,.!:;-]+)*",
                   re.IGNORECASE)
_VERB = re.compile(r"type\b(?:\s+(?:out|in|up)\b)?(?:[\s,:;-]+(?:this|the following|these words)\s*[:,-])?"
                   r"(?:[\s,:;-]+(?:(?:a|the)\s+)?(?:message|text|reply)\s+(?:saying|that says))?"
                   r"(?:[\s,:;-]+saying)?[\s,:;-]*", re.IGNORECASE)
# "Type that I'll be late": "that" introduces the words. "Type that sounds good" types all of it.
_THAT = re.compile(r"that\s+(?=(?:i|i'm|im|i'll|ill|i've|ive|i'd|we|we're|we'll|you|you're|you'll|he|she|they|"
                   r"they're|they'll|it's|it'll|my|our|your)\b)", re.IGNORECASE)
_POLISH_TAIL = re.compile(rf"[\s,;:-]*(?:please\s+)?(?:{POLISH_ASK.replace(' ', r'\s+')})\s*([.!?]*)\s*$", re.IGNORECASE)


def strip_command(text: str, app: str = "") -> str:
    """The words to type, from a transcript of the whole command: "Computer, type I'll be late,
    make it polite." -> "I'll be late." (and " on WhatsApp" at the end, when that's the app)."""
    text = text.strip()
    lead = _LEAD.match(text)
    start = lead.end() if lead else 0
    verb = _VERB.match(text, start)
    if verb is None:
        return text
    capital = next((c.isupper() for c in text if c.isalpha()), False)  # it began a sentence
    rest = text[verb.end():]
    rest = rest[_THAT.match(rest).end():] if _THAT.match(rest) else rest
    rest = _POLISH_TAIL.sub(lambda m: m.group(1)[:1], rest)
    if app:
        where = re.escape(app.strip()).replace(r"\ ", r"\s+")
        rest = re.sub(rf"[\s,]+(?:on|in|into)\s+(?:the\s+)?{where}(?:\s+chat)?\s*([.!?]*)\s*$",
                      lambda m: m.group(1)[:1], rest, flags=re.IGNORECASE)
    rest = rest.strip()
    if capital and rest[:1].islower():
        rest = rest[0].upper() + rest[1:]
    return rest


_CANCEL = re.compile(r"^\W*(?:(?:oh|uh+|um+|okay|ok|no|actually|wait|sorry)\W+)*"
                     r"(?:cancel(?: that| it| dictation| the dictation| the message)?|never ?mind|forget (?:about )?it|"
                     r"stop(?: dictation| it| that)?|scratch that|abort|no|nope|nothing|nothing for now|not now|"
                     r"don'?t (?:type|write) (?:it|that|anything))"
                     r"(?:\W+(?:aj|computer|please|thanks|thank you|then))*\W*$", re.IGNORECASE)


def is_cancel(text: str) -> bool:
    """Is this whole answer a "never mind"? ("No", "Nothing", "Cancel": the user, 2026-10-22.)"""
    return bool(_CANCEL.match(text or ""))


_QUOTED = re.compile(r'["“]([^"“”]{6,}?)["”]')


def draft_in(reply: str) -> str:
    """A message the assistant wrote out in its reply ('Here: "Hi Amma, ..."'), for "type that"."""
    found = [q.strip() for q in _QUOTED.findall(reply or "") if len(q.split()) >= 2]
    return max(found, key=len) if found else ""


def as_typed(text: str) -> str:
    """The text exactly as keyboard.type_text puts it in: line breaks as "\\n", tabs as spaces, no
    other control characters, no spaces or line breaks at either end."""
    text = str(text).replace("\r\n", "\n").replace("\r", "\n").replace("\t", " ")
    return "".join(c for c in text if c == "\n" or (ord(c) >= 0x20 and not 0x7F <= ord(c) <= 0x9F)).strip()


def one_key_each(text: str) -> bool:
    """Does each character take exactly one Backspace? Not so for emoji outside the basic plane,
    joined emoji, or combining marks (one Backspace can take a whole cluster, or only part of it)."""
    for c in text:
        if c == "\n":
            continue
        if ord(c) > 0xFFFF or unicodedata.combining(c) or unicodedata.category(c) in ("Mn", "Me", "Cf", "Cs", "Co", "Cc"):
            return False
    return True


# ---- the desktop --------------------------------------------------------------------------------

_user32 = ctypes.WinDLL("user32", use_last_error=True)
_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
_user32.GetWindowThreadProcessId.restype = wintypes.DWORD
_user32.IsWindow.argtypes = [wintypes.HWND]
_user32.IsWindow.restype = wintypes.BOOL
_user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
_user32.GetWindowTextLengthW.restype = ctypes.c_int
_user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
_user32.GetWindowTextW.restype = ctypes.c_int


class Desktop:
    """The real desktop: core/keyboard.py and core/uia.py, and the window list. Tests pass a fake
    with the same methods, so nothing here is ever typed during a test."""

    def front(self) -> int:
        from aj.core import keyboard
        return keyboard.front_window()

    def focus(self, hwnd: int) -> bool:
        from aj.core import keyboard
        return keyboard.focus(hwnd)

    def exe(self, hwnd: int) -> str:
        from aj.core import keyboard
        return keyboard.window_exe(hwnd)

    def title(self, hwnd: int) -> str:
        buffer = ctypes.create_unicode_buffer(_user32.GetWindowTextLengthW(hwnd) + 1)
        _user32.GetWindowTextW(hwnd, buffer, len(buffer))
        return buffer.value

    def ours(self, hwnd: int) -> bool:
        """One of this process's own windows (the assistant's, or the dictation window's)."""
        pid = wintypes.DWORD()
        _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        return pid.value == os.getpid()

    def alive(self, hwnd: int) -> bool:
        return bool(hwnd) and bool(_user32.IsWindow(hwnd))

    def windows(self) -> list:
        """Open app windows, front-most first (tools/apps.py: hwnd, title, exes, aumids)."""
        from aj.tools import apps  # here: tools import core modules, not the other way round
        return apps.list_windows()

    def editable(self) -> bool | None:
        return uia.focused_editable()

    def focused_box(self) -> uia.Node | None:
        """The element with the keyboard focus (its runtime id, kind and name), or None."""
        return uia.focused_node()

    def value(self) -> str | None:
        return uia.focused_value()

    def caret_at_end(self, box: uia.Node | None = None) -> bool | None:
        """Is the focused box's caret at the very end of its text, with nothing selected? With
        `box`, False when the focus is in another element: one read of the focus covers both."""
        found = uia.focused_node()
        if box is not None and not same_field(found, box):
            return False
        return uia.caret_at_end(found)

    def speak_listening(self) -> bool:
        """Is AJ (Speak) taking a dictation (core/mic_share.py)?"""
        return mic_share.speak_listening()

    def dictating(self, on: bool) -> None:
        """Tell AJ (Speak) whether this process is waiting for words to type."""
        try:
            mic_share.dictating_flag.set(on)
        except OSError as exc:  # no event: AJ (Speak) just can't see it; the dictation still works
            log.warning("couldn't say that dictation is %s: %s", "on" if on else "off", exc)

    def type(self, text: str, multiline: bool = False) -> None:
        from aj.core import keyboard
        keyboard.type_text(text, multiline=multiline)

    def backspace(self) -> None:
        from aj.core import keyboard
        keyboard.press("backspace")


_EVENT_SYSTEM_FOREGROUND = 0x0003
_WINEVENT_OUTOFCONTEXT, _WINEVENT_SKIPOWNPROCESS = 0x0000, 0x0002
_WM_QUIT = 0x0012
_WINEVENTPROC = ctypes.WINFUNCTYPE(None, wintypes.HANDLE, wintypes.DWORD, wintypes.HWND, wintypes.LONG,
                                   wintypes.LONG, wintypes.DWORD, wintypes.DWORD)
_user32.SetWinEventHook.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.HMODULE, _WINEVENTPROC,
                                    wintypes.DWORD, wintypes.DWORD, wintypes.DWORD]
_user32.SetWinEventHook.restype = wintypes.HANDLE
_user32.UnhookWinEvent.argtypes = [wintypes.HANDLE]
_user32.UnhookWinEvent.restype = wintypes.BOOL
_user32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT]
_user32.GetMessageW.restype = wintypes.BOOL
_user32.PostThreadMessageW.argtypes = [wintypes.DWORD, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
_user32.PostThreadMessageW.restype = wintypes.BOOL
_kernel32.GetCurrentThreadId.restype = wintypes.DWORD


class FrontWatch:
    """The last window in front that isn't one of ours. When the user types "type that" into the
    assistant's own box, its window is in front, and the one he means is the one before it.

    Windows tells a hook each time the front window changes (SetWinEventHook), so this costs
    nothing while nothing changes: no polling. Our own windows are left out by Windows itself
    (WINEVENT_SKIPOWNPROCESS). If the hook can't be set, it looks twice a second instead."""

    def __init__(self, desktop: Desktop | None = None):
        self.desktop = desktop or Desktop()
        self._last = 0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._thread_id = 0

    def start(self) -> None:
        if self._thread is None:
            self.seen(self.desktop.front())
            self._thread = threading.Thread(target=self._run, daemon=True, name="front-watch")
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread_id:
            _user32.PostThreadMessageW(self._thread_id, _WM_QUIT, 0, 0)

    def seen(self, hwnd: int | None) -> None:
        if hwnd and not self.desktop.ours(hwnd):
            self._last = int(hwnd)

    def last_other(self) -> int:
        """The last window in front that isn't ours, if it's still open; else 0."""
        hwnd = self._last
        return hwnd if hwnd and self.desktop.alive(hwnd) else 0

    def _run(self) -> None:
        self._thread_id = _kernel32.GetCurrentThreadId()
        proc = _WINEVENTPROC(lambda _hook, _event, hwnd, _obj, _child, _thread, _when: self.seen(hwnd))
        hook = _user32.SetWinEventHook(_EVENT_SYSTEM_FOREGROUND, _EVENT_SYSTEM_FOREGROUND, None, proc, 0, 0,
                                       _WINEVENT_OUTOFCONTEXT | _WINEVENT_SKIPOWNPROCESS)
        if not hook:
            log.warning("couldn't watch the front window (error %s); looking twice a second instead",
                        ctypes.get_last_error())
            while not self._stop.wait(0.5):
                self.seen(self.desktop.front())
            return
        try:
            msg = wintypes.MSG()
            while not self._stop.is_set() and _user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                pass  # the hook's calls arrive while this thread waits for messages
        finally:
            _user32.UnhookWinEvent(hook)


def _key(app: str) -> str:
    return re.sub(r"[^a-z0-9]", "", app.lower())


def is_app(desktop: Desktop, hwnd: int, app: str) -> bool:
    """Is this window that app? By its program ("whatsapp.root.exe") or its title ("WhatsApp Web"
    in Chrome, "Google Docs")."""
    key = _key(app)
    return bool(key) and (key in _key(desktop.exe(hwnd)) or key in _key(desktop.title(hwnd)))


def find_app_window(desktop: Desktop, app: str) -> int:
    """The front-most window of a named app (its program first, then a window titled with it)."""
    key = _key(app)
    if not key:
        return 0
    windows = desktop.windows()
    for w in windows:
        if any(key in _key(name) for name in (*w.exes, *w.aumids)):
            return w.hwnd
    for w in windows:
        if key in _key(w.title):
            return w.hwnd
    return 0


def choose_target(app: str = "", desktop: Desktop | None = None, watch: FrontWatch | None = None) -> int:
    """Where to type: the window in front, unless it's one of ours (then the last one before it);
    with `app`, that app's window (the front one if it's that app). 0 when there's nowhere."""
    desktop = desktop or Desktop()
    front = desktop.front()
    mine = not front or desktop.ours(front)
    if app:
        if not mine and is_app(desktop, front, app):
            return front
        return find_app_window(desktop, app)
    if not mine:
        return front
    return watch.last_other() if watch is not None else 0


def is_whatsapp(desktop: Desktop, hwnd: int) -> bool:
    return is_app(desktop, hwnd, "whatsapp")


# ---- typing -------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Outcome:
    ok: bool
    reason: str      # "typed", "replaced", or why not: "empty", "no_window", "no_focus", "not_editable", ...
    line: str        # what to say about it
    text: str = ""   # what was typed (or would have been)
    hwnd: int = 0
    whatsapp: bool = False
    at: float = field(default_factory=time.monotonic)


def done_line(whatsapp: bool, how: str = "plain") -> str:
    """ "Typed it in WhatsApp. Press send when you're ready." Never "sent": he sends it."""
    if not whatsapp:
        return {"polished": TYPED_POLISHED, "unpolished": TYPED_UNPOLISHED, "replaced": REPLACED}.get(how, TYPED)
    line = {"polished": "Typed the polished version in WhatsApp.", "unpolished": TYPED_UNPOLISHED,
            "replaced": REPLACED}.get(how, "Typed it in WhatsApp.")
    return f"{line} {PRESS_SEND}"


def _chunks(text: str) -> list[str]:
    """The text in pieces of about _CHUNK characters, cut only before a space or line break, so
    no piece ends in a line break (keyboard.type_text drops a trailing one)."""
    out, current = [], ""
    for word in re.findall(r"\s*\S+", text):
        if current and len(current) + len(word) > _CHUNK:
            out.append(current)
            current = ""
        current += word
    return out + [current] if current else out


_WHATSAPP_MESSAGE_BOX = re.compile(r"^\s*type a message\b", re.IGNORECASE)  # "Type a message to Appa"
_LINE_BREAKS = re.compile(r" *\n+ *")


def takes_line_breaks(found: uia.Node | None, whatsapp: bool = False) -> bool:
    """Is a newline a line break in this box, so Shift+Enter is safe? WhatsApp's message box, or
    what uia.multiline() knows to be multi-line. Anywhere else a newline is typed as a space."""
    if found is None:
        return False
    if whatsapp and _WHATSAPP_MESSAGE_BOX.match(found.name or ""):
        return True
    return uia.multiline(found)


def as_single_line(text: str) -> str:
    """The text as keyboard.type_text types it into a single-line box: each run of line breaks
    becomes one space."""
    return _LINE_BREAKS.sub(" ", text)


def same_field(a: uia.Node | None, b: uia.Node | None) -> bool:
    """Are these the same element? By UI Automation's runtime id when both have one (always, in
    practice); otherwise by what kind of element it is and which window it lives in."""
    if a is None or b is None:
        return False
    if a.runtime_id and b.runtime_id:
        return a.runtime_id == b.runtime_id
    return ((a.control_type, a.automation_id, a.class_name, a.native_hwnd)
            == (b.control_type, b.automation_id, b.class_name, b.native_hwnd))


def _not_a_box(found: uia.Node | None) -> str:
    """"" for a box typing can go into, else why not ("unknown_field": UI Automation can't tell)."""
    if found is None:
        return "unknown_field"
    return "" if uia.editable(found) else "not_editable"


def type_into(hwnd: int, text: str, how: str = "plain", offline: bool = False,
              desktop: Desktop | None = None, cancel=None) -> Outcome:
    """Type `text` into that window, where its cursor is, if the focus there is in a text box.
    `how`: "plain", "polished" or "unpolished" (polish was asked for and failed), for the line.
    `cancel`: once it is set, nothing more is typed. Never presses Enter, never touches the
    clipboard. Outcome.text is what went in (or would have): outside a box that takes line
    breaks, each run of them is a space."""
    desktop = desktop or Desktop()
    text = as_typed(text)
    if not text:
        return Outcome(False, "empty", NOTHING_TO_TYPE, text, hwnd)
    if _stopped(cancel):  # before the window is brought forward
        return Outcome(False, "cancelled", STOPPED, text, hwnd)
    if not hwnd or not desktop.alive(hwnd):
        return Outcome(False, "no_window", NO_WINDOW, text, hwnd)
    if not desktop.focus(hwnd):
        return Outcome(False, "no_focus", CLICK_FIRST, text, hwnd)
    box = desktop.focused_box()
    why_not = _not_a_box(box)
    if why_not:  # not a text box, or UI Automation can't tell: no stray keys
        return Outcome(False, why_not, CLICK_FIRST, text, hwnd)
    whatsapp = is_whatsapp(desktop, hwnd)
    multiline = takes_line_breaks(box, whatsapp)
    if not multiline:
        text = as_single_line(text)
    for i, piece in enumerate(_chunks(text)):
        if _stopped(cancel):
            if i:
                return Outcome(False, "stopped", STOPPED_PART, text, hwnd, whatsapp)
            return Outcome(False, "cancelled", STOPPED, text, hwnd, whatsapp)
        # The same window, and the same box in it (a click into the address bar is the same window).
        if i and (desktop.front() != hwnd or not same_field(desktop.focused_box(), box)):
            return Outcome(False, "moved", MOVED_AWAY, text, hwnd, whatsapp)
        try:
            desktop.type(piece, multiline)
        except (OSError, ValueError) as exc:
            log.warning("typing was refused: %s", exc)
            from aj.core.keyboard import ModifiersHeld  # here: the keyboard is only loaded to type
            if i:  # part of it is in: typing it all again would double it
                return Outcome(False, "stopped", STOPPED_PART, text, hwnd, whatsapp)
            if isinstance(exc, ModifiersHeld):
                return Outcome(False, "keys_held", KEYS_HELD, text, hwnd, whatsapp)
            return Outcome(False, "refused", REFUSED, text, hwnd, whatsapp)
    line = done_line(whatsapp, how)
    return Outcome(True, "typed", f"{OFFLINE} {line}" if offline else line, text, hwnd, whatsapp)


def _field_text(value: str) -> str:
    return value.replace("\r\n", "\n").replace("\r", "\n").replace(" ", " ")


def replace_typed(hwnd: int, old: str, new: str, desktop: Desktop | None = None, cancel=None) -> Outcome:
    """Swap what was just typed (`old`) for `new`, only when provably safe: that window's focused
    box still ends with exactly `old`, its caret is at the very end with nothing selected (a
    Backspace deletes what is before the caret, or a selection: the review of 2026-10-22), and each
    of `old`'s characters takes one Backspace. Every few Backspaces the window, the box and the
    caret are checked again. After deleting, the box must read exactly as before minus `old`, or
    nothing more is done."""
    desktop = desktop or Desktop()
    old = as_typed(old)
    if not old or not one_key_each(old):
        return Outcome(False, "complex_text", CANT_REPLACE, new, hwnd)
    if _stopped(cancel):
        return Outcome(False, "cancelled", STOPPED, new, hwnd)
    if not desktop.alive(hwnd) or not desktop.focus(hwnd):
        return Outcome(False, "moved", CANT_REPLACE, new, hwnd)
    box = desktop.focused_box()
    if _not_a_box(box):
        return Outcome(False, "moved", CANT_REPLACE, new, hwnd)
    value = desktop.value()
    if value is None:
        return Outcome(False, "unreadable", CANT_REPLACE, new, hwnd)
    before = _field_text(value)
    if not before.endswith(old):
        return Outcome(False, "changed", CANT_REPLACE, new, hwnd)
    if desktop.caret_at_end(box) is not True:  # False, or None (it can't be told): nothing is deleted
        return Outcome(False, "caret", NOT_AT_END, new, hwnd)
    for i in range(len(old)):
        # Before EVERY Backspace (the review of 2026-10-22: every 5 left room for 4 of his own
        # characters to go after a click back into the text): the window, the box and its caret.
        if i and (_stopped(cancel) or desktop.front() != hwnd or desktop.caret_at_end(box) is not True):
            return Outcome(False, "interrupted", CHECK_BOX, new, hwnd)
        try:
            desktop.backspace()
        except (OSError, ValueError) as exc:
            log.warning("Backspace was refused: %s", exc)
            return Outcome(False, "refused", CHECK_BOX, new, hwnd)
    after = desktop.value()
    if after is None or _field_text(after) != before[:len(before) - len(old)]:
        return Outcome(False, "mismatch", CHECK_BOX, new, hwnd)
    typed = type_into(hwnd, new, desktop=desktop, cancel=cancel)
    if not typed.ok:
        return typed
    return replace(typed, reason="replaced", line=done_line(typed.whatsapp, "replaced"))


# ---- the assistant's dictation -----------------------------------------------------------------

class NotDone(Exception):
    """Nothing (or not all of it) was typed; the message says why, for the user to hear."""


@dataclass(frozen=True)
class Request:
    hwnd: int            # where it goes: the window in front when he asked
    polish: bool = False
    app: str = ""


@dataclass(frozen=True)
class _Lent:
    """This turn's speech, for the next type_text call to transcribe accurately."""
    pieces: tuple
    said: str            # Parakeet's transcript (or the typed text)
    command: bool        # it's the whole command ("type ..."): the command words come off
    request: Request | None = None


class Dictation:
    """The assistant's dictation: a request it's waiting to hear, the words it kept for "type
    that", and what it typed last, for "make it nicer". The tools (tools/dictation.py), the brain
    and the pipeline share one; the engine makes it."""

    def __init__(self, settings: DictationSettings | None = None, desktop: Desktop | None = None,
                 ears=None, llm=None):
        """`ears`: anything with transcribe(samples) -> str (Parakeet), for when Gemini can't.
        `llm`: (server base URL, LlmSettings) of the local model, to polish when Gemini can't."""
        self.s = settings or DictationSettings()
        self.desktop = desktop or Desktop()
        self.ears, self.llm = ears, llm
        self.watch = FrontWatch(self.desktop)
        self.armed: Request | None = None  # start_dictation ran: the user's next words are the message
        self.turn_audio: tuple | None = None  # this turn's speech (the pipeline sets it; None if typed)
        self.last_heard = ""
        self._lent: _Lent | None = None
        self._ready: tuple[str, float] | None = None  # words "type that" types
        self._typed: Outcome | None = None            # what was typed last, and where
        self._live: set[threading.Event] = set()       # one per typing, polishing or arming in flight
        self._live_lock = threading.Lock()

    def start(self) -> None:
        self.watch.start()

    def stop(self) -> None:
        self.watch.stop()
        self.disarm()  # AJ (Speak) may take dictation again

    def target(self, app: str = "") -> int:
        return choose_target(app, self.desktop, self.watch)

    # -- stopping ------------------------------------------------------------------------------

    def abort(self) -> bool:
        """Stop every typing, polishing or arming in flight, before its next step. The brain
        calls it when the user says stop or cancel, the pipeline when he talks over the assistant,
        the engine when it turns off (the review of 2026-10-22). True if anything was in flight."""
        with self._live_lock:
            live = list(self._live)
        for event in live:
            event.set()
        return bool(live)

    @contextmanager
    def _job(self, cancel=None) -> Iterator["_AnyOf"]:
        """A stop signal for one job: set by abort(), or by `cancel` (the dispatcher's, at the
        tool's timeout). A new job starts unset, so an earlier abort() never blocks the next one."""
        mine = threading.Event()
        with self._live_lock:
            self._live.add(mine)
        try:
            yield _AnyOf(cancel, mine)
        finally:
            with self._live_lock:
                self._live.discard(mine)

    # -- the turn ----------------------------------------------------------------------------

    def hear(self, pieces) -> None:
        """The speech of the turn about to be answered (None for typed text)."""
        self.turn_audio = tuple(_pieces(pieces)) or None

    def new_turn(self) -> None:
        self._lent = None
        self.last_heard = ""  # the brain logs its length as this turn's dictation (2026-10-22)

    def lend(self, pieces, said: str, command: bool, request: Request | None = None) -> None:
        """The next type_text call transcribes this speech itself (Gemini), instead of taking the
        words it's given. `command`: the speech is the whole command, so "type" comes off."""
        self._lent = _Lent(tuple(_pieces(pieces)), said or "", command, request)

    def arm(self, polish: bool = False, app: str = "", check: bool = True, cancel=None) -> str:
        """Wait for the user's next words, to type them into the window in front now (or `app`'s).
        `check`: first make sure the focus there is in a text box, so a minute of dictation isn't
        wasted on a window that can't take it. `cancel`: set (the tool timed out, or he said stop)
        before it is armed, and it isn't."""
        with self._job(cancel) as stop:
            if self.desktop.speak_listening():  # one dictation at a time (core/mic_share.py)
                raise NotDone(SPEAK_BUSY)
            hwnd = self.target(app)
            if not hwnd:
                raise NotDone(f"{app_name(app)} isn't open." if app else NO_WINDOW)
            if stop.is_set():
                raise NotDone(STOPPED)
            if check and (not self.desktop.focus(hwnd) or self.desktop.editable() is not True):
                raise NotDone(CLICK_FIRST_ARM)
            if stop.is_set():  # bringing the window forward can take a while: checked again
                raise NotDone(STOPPED)
            self.armed = Request(hwnd, polish, app)
            self.desktop.dictating(True)  # AJ (Speak) stays out of it until the words are in
            self._lent = None
            return GO_AHEAD

    def disarm(self) -> Request | None:
        request, self.armed = self.armed, None
        if request is not None:
            self.desktop.dictating(False)
        return request

    def quick_listen(self, pieces) -> str:
        """A short dictation, transcribed locally, only to catch "cancel" without a trip to Gemini."""
        pieces = _pieces(pieces)
        if self.ears is None or not pieces or sum(len(p) for p in pieces) > QUICK_SECS * MIC_RATE:
            return ""
        return " ".join(self.ears.transcribe(p).strip() for p in pieces).strip()

    # -- typing --------------------------------------------------------------------------------

    def _local(self, piece: np.ndarray) -> str:
        return self.ears.transcribe(piece) if self.ears is not None else ""

    def _words(self, text: str, lent: _Lent | None, app: str, hwnd: int, cancel=None) -> Heard:
        if lent is None:
            return Heard(text.strip(), "given")  # the model's words, or typed ones: as they are
        if not lent.pieces:
            words = lent.said or text
            return Heard(strip_command(words, app) if lent.command else words.strip(), "given")
        where = "a WhatsApp chat" if is_whatsapp(self.desktop, hwnd) else "a text box"
        hint = f"It is a message being typed into {where}."
        if lent.command:
            hint += " It starts with a spoken command to type; write that down too."
        heard = transcribe(lent.pieces, MIC_RATE, hint, local=None if lent.command else self._local, settings=self.s,
                           cancel=cancel)
        if _stopped(cancel):
            raise NotDone(STOPPED)
        if heard.engine != "gemini" and lent.command:
            heard = Heard(said_punctuation(lent.said or text), "offline")  # Parakeet's, of this turn
        return replace(heard, text=strip_command(heard.text, app)) if lent.command else heard

    def _type(self, hwnd: int, words: str, want_polish: bool, offline: bool, cancel=None) -> str:
        how = "plain"
        if want_polish:
            better = polish(words, self.s, self.llm, cancel=cancel)
            words, how = (better, "polished") if better else (words, "unpolished")
        outcome = type_into(hwnd, words, how, offline, self.desktop, cancel=cancel)
        if not outcome.ok:
            if outcome.reason not in ("moved", "stopped"):  # partly typed: typing it all again would double it
                self._ready = (as_typed(words), time.monotonic())
            raise NotDone(outcome.line)
        self._typed, self._ready = outcome, None
        return outcome.line

    def type_words(self, text: str, polish: bool = False, app: str = "", cancel=None) -> str:
        """type_text: the words (this turn's speech, transcribed by Gemini, when it was lent),
        polished if asked, typed where he asked. Returns what to say; raises NotDone. `cancel`:
        the dispatcher's, set at the tool's timeout."""
        lent, self._lent = self._lent, None
        request = lent.request if lent is not None and lent.request is not None else None
        hwnd = request.hwnd if request is not None else self.target(app)  # chosen before Gemini's wait
        if not hwnd:
            raise NotDone(f"{app_name(app)} isn't open." if app else NO_WINDOW)
        with self._job(cancel) as stop:
            heard = self._words(text, lent, app or (request.app if request else ""), hwnd, stop)
            self.last_heard = heard.text
            if not heard.text.strip():
                raise NotDone(NOTHING_TO_TYPE)
            return self._type(hwnd, heard.text, polish or bool(request and request.polish), heard.engine == "offline",
                              stop)

    def has_ready(self) -> bool:
        return self._ready is not None and time.monotonic() - self._ready[1] < KEEP_SECS

    def type_ready(self, polish: bool = False, app: str = "", cancel=None) -> str:
        """type_that: the words kept from a dictation that couldn't be typed, or the message the
        assistant wrote out in its last reply."""
        if not self.has_ready():
            raise NotDone(NOTHING_READY)
        hwnd = self.target(app)
        if not hwnd:
            raise NotDone(f"{app_name(app)} isn't open." if app else NO_WINDOW)
        with self._job(cancel) as stop:
            return self._type(hwnd, self._ready[0], polish, offline=False, cancel=stop)

    def saw_reply(self, reply: str) -> None:
        """The assistant's reply: a message it wrote out ('Here: "Hi Amma..."') can be typed with
        "type that"."""
        draft = draft_in(reply)
        if draft:
            self._ready = (draft, time.monotonic())

    def has_typed(self) -> bool:
        return self._typed is not None and time.monotonic() - self._typed.at < KEEP_SECS

    def polish_last(self, cancel=None) -> str:
        """polish_typed: the words just typed, made nicer, in their place if that's safe."""
        if not self.has_typed():
            raise NotDone(NOTHING_TYPED)
        last = self._typed
        hwnd = self.target()
        with self._job(cancel) as stop:
            better = polish(last.text, self.s, self.llm, cancel=stop)
            if stop.is_set():
                raise NotDone(STOPPED)
            if not better:
                raise NotDone(NO_POLISH)
            better = as_typed(better)
            if better == last.text:
                return ALREADY_FINE
            if hwnd != last.hwnd:
                self._ready = (better, time.monotonic())
                raise NotDone(CANT_REPLACE)
            outcome = replace_typed(hwnd, last.text, better, self.desktop, cancel=stop)
        if not outcome.ok:
            self._ready = (better, time.monotonic())
            raise NotDone(outcome.line)
        self._typed = outcome
        return outcome.line


class _AnyOf:
    """Set when any of its events is: the dispatcher's (the tool timed out) or the job's own
    (Dictation.abort). Quacks like a threading.Event for is_set()."""

    def __init__(self, *events):
        self.events = [e for e in events if e is not None]

    def is_set(self) -> bool:
        return any(e.is_set() for e in self.events)


def app_name(app: str) -> str:
    """How to say an app's name: "WhatsApp", not "whatsapp"."""
    known = {"whatsapp": "WhatsApp", "googledocs": "Google Docs", "gmail": "Gmail", "chrome": "Chrome"}
    return known.get(_key(app), app.strip().title())
