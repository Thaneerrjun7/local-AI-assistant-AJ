"""Dictation tools: what the user says is typed exactly where his cursor is, in any app. The work
is in core/dictation.py; these are the calls the fast path and the model make.

The user, 2026-10-22: "I want another tool where i tell computer write this down for me on
whatsapp and then whatever i say gets written down in text so i dont have to type it." Two ways in:
- "write this down", "type this for me", "take dictation" (no words yet): start_dictation, a short
  "Go ahead.", and his NEXT words are typed (the pipeline keeps them as audio for Gemini).
- "type I'll be late" in one go: type_text. Claimed by the fast path, Gemini hears the turn's own
  audio and the command words come off its transcript (Brain lends it the audio).
Plus "type that" (the words kept when the focus wasn't in a text box, or a message the assistant
wrote out in its reply) and "make it nicer" right after typing (polish_typed). Those two are for
the fast path only: they're left out of the model's tool list (brain.LLM_TOOLS) to keep it short.

Never claimed: "write a message to mum saying ..." (that's composing a message in the reply, as
before) and "type of ..." questions ("what type of laptop is this").

"Open AJ speak", "open the speak window" (open_speak): the AJ (Speak) window, the small
speech-to-text window (aj/speak), started with the project's pythonw. It runs one at a time, so a
second call only shows it. "AJ, speak" and "speak window, listen" start it listening at once
(--listen). The user, 2026-10-22: AJ (Speak) by voice.

Each tool hands core the dispatcher's cancel event (registry.cancel_event), so a call the
dispatcher gave up on stops before it types or arms anything late; and type_text's words are
private: the audit log keeps only their length (the review of 2026-10-22).
"""
import re
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

from aj.config import APP_DIR
from aj.core import dictation as core
from aj.tools.registry import Level, ToolError, cancel_event, tool

desk: core.Dictation | None = None  # the engine's (engine.py sets it); one of our own otherwise


def _desk() -> core.Dictation:
    global desk
    if desk is None:
        desk = core.Dictation()
    return desk


def _run(work: Callable[[], str]) -> str:
    try:
        return work()
    except core.NotDone as exc:
        raise ToolError(str(exc)) from None


@tool(level=Level.SAFE, timeout=10.0, cloud=True)
def start_dictation(polish: bool = False, app: str | None = None) -> str:
    """Take dictation: the user's next words are typed exactly where their cursor is, in any app. It never sends.

    polish: true when they ask for it to sound nicer, more polite or refined
    app: the app to type into, only if they named one ("whatsapp")
    """
    return _run(lambda: _desk().arm(polish, app or "", cancel=cancel_event()))


@tool(level=Level.SAFE, timeout=120.0, cloud=True, private=("text",))
def type_text(text: str, polish: bool = False, app: str | None = None) -> str:
    """Type the user's words exactly where their cursor is, in any app (a chat, a document). Never presses Enter or Send.

    text: the exact words to type
    polish: true when they ask for it to sound nicer, more polite or refined
    app: the app to type into, only if they named one ("whatsapp")
    """
    return _run(lambda: _desk().type_words(text, polish, app or "", cancel=cancel_event()))


@tool(level=Level.SAFE, timeout=120.0, cloud=True)
def type_that(polish: bool = False, app: str | None = None) -> str:
    """Type the words kept from the last dictation, or the message written out in the last reply.

    polish: true when they ask for it to sound nicer, more polite or refined
    app: the app to type into, only if they named one
    """
    return _run(lambda: _desk().type_ready(polish, app or "", cancel=cancel_event()))


@tool(level=Level.SAFE, timeout=120.0, cloud=True)
def polish_typed() -> str:
    """Rewrite what was just typed to sound more refined and polite, in its place when that is safe."""
    return _run(lambda: _desk().polish_last(cancel=cancel_event()))


# ---- AJ (Speak) ---------------------------------------------------------------------------------

_DETACHED = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP: it outlives nothing of ours
_popen = subprocess.Popen  # module-level so tests can replace it and never start a window


def _pythonw() -> Path | None:
    """The project's pythonw.exe (no console window), else the one beside this Python."""
    for candidate in (APP_DIR / ".venv" / "Scripts" / "pythonw.exe", Path(sys.executable).with_name("pythonw.exe")):
        if candidate.exists():
            return candidate
    return None


@tool(level=Level.SAFE, timeout=10.0, cloud=False)
def open_speak(listen: bool = False) -> str:
    """Open AJ (Speak), the small speech-to-text window that types what the user says where their cursor is. It never sends.

    listen: true to start listening at once ("AJ speak", "speak window, listen")
    """
    exe = _pythonw()
    if exe is None:
        raise ToolError("I can't find the Python that runs AJ (Speak).")
    try:
        _popen([str(exe), "-m", "aj.speak", *(["--listen"] if listen else [])], cwd=str(APP_DIR),
               creationflags=_DETACHED, close_fds=True, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
               stderr=subprocess.DEVNULL)
    except OSError as exc:
        raise ToolError(f"AJ (Speak) didn't start: {exc}") from None
    if listen:
        return "Opening AJ (Speak), listening. Say what to type."
    return "Opening AJ (Speak). Click its mic when you're ready."


# ---- the fast path ------------------------------------------------------------------------------
# Patterns see normalized text (tools/intents.py): lowercase, no punctuation, no apostrophes, and
# "hey", "AJ", "computer", "please", "can you" and a trailing "for me" already taken off.

_APPS = {"whatsapp": "whatsapp", "whats app": "whatsapp", "telegram": "telegram", "discord": "discord",
         "messenger": "messenger", "slack": "slack", "teams": "teams", "notepad": "notepad", "word": "word",
         "google docs": "google docs", "docs": "google docs", "gmail": "gmail", "chrome": "chrome",
         "the browser": "chrome"}
_APP = rf"(?: (?:on|in|into|to) (?:the |my )?(?P<app>{'|'.join(sorted(_APPS, key=len, reverse=True))})(?: chat| app| box)?)?"
_POLISH = rf"(?P<polish> (?:{core.POLISH_ASK}))?"
_ADJ = core._ADJ
_THEM = r"(?:it|that|this|the message|the text|what i (?:wrote|typed|said)|what you typed)"
_NOT_WORDS = {"this", "that", "it", "something", "a message", "for me", "out a message"}
_COMPOSING = re.compile(r"(?:a |an |the )?(?:message|text|reply|email|note|letter) (?:to|for) ")


def _app(m: re.Match) -> str | None:
    return _APPS.get(m["app"]) if m["app"] else None


def _dictate(m: re.Match) -> dict:
    return {"polish": bool(m["polish"]), "app": _app(m)}


def _ready(m: re.Match) -> dict | None:
    """'Type that': only when there's something kept to type; otherwise it's a new dictation."""
    return _dictate(m) if _desk().has_ready() else None


def _typed(m: re.Match) -> dict | None:
    """'Make it nicer': only right after typing; otherwise it's about his own reply (the model's)."""
    return {} if _desk().has_typed() else None


def _words(m: re.Match) -> dict | None:
    words = m["words"].strip()
    if words in _NOT_WORDS or words == "of" or words.startswith("of ") or _COMPOSING.match(words):
        return None  # "type of laptop", "type a message to Appa": not words to type
    return {"text": core.strip_command(f"type {words}"), "polish": bool(m["polish"]), "app": _app(m)}


_SPEAK = r"(?:the )?(?:aj |a j )?speak(?: window| app| box)?"
_LISTEN = r"(?P<listen>,? (?:and )?(?:start )?listen(?:ing)?(?: now)?)?"


def _speak(m: re.Match) -> dict:
    return {"listen": bool(m["listen"])}


INTENTS: list[tuple[str, str, Callable[[re.Match], dict | None]]] = [
    # AJ (Speak) by voice (the user, 2026-10-22). "AJ, speak" reaches here as "speak" (the name
    # comes off first), and means: open it and listen.
    (r"speak", "open_speak", lambda m: {"listen": True}),
    (rf"(?:open|launch|start|show|bring up|pull up|run)(?: up)? {_SPEAK}{_LISTEN}", "open_speak", _speak),
    (rf"(?:the )?(?:aj |a j )?speak window{_LISTEN}", "open_speak", _speak),
    (rf"(?:type|write|put|enter|add|insert|paste) (?:that|it|the polished (?:one|version)|the message|the draft|"
     rf"what you (?:just )?(?:wrote|said))(?: (?:in|out|down|in there|there|in the box|into the box|after it|at the end))?"
     rf"{_APP}{_POLISH}", "type_that", _ready),
    (rf"(?:make|making) {_THEM} (?:sound |look |read )?(?:(?:a bit|a little|more|much|really|very) )*(?:{_ADJ})"
     rf"(?: and (?:more )?(?:{_ADJ}))*"
     rf"|(?:polish|clean up|tidy up|refine|improve|reword|rephrase|rewrite) {_THEM}(?: up)?(?: (?:a bit|a little))?",
     "polish_typed", _typed),
    (rf"(?:write|type|take|jot|get)(?: (?:this|that|it|something|these words|my words|a message|a note|what i say))?"
     rf" down(?: what i say)?(?: for me)?{_APP}{_POLISH}", "start_dictation", _dictate),
    (rf"(?:write|type)(?: down)? (?:what i say|what im (?:about to )?say(?:ing)?|after me|as i (?:speak|talk|say it))"
     rf"{_APP}{_POLISH}", "start_dictation", _dictate),
    (rf"type(?: (?:this|that|it|something|a message|out a message|for me))?{_APP}{_POLISH}", "start_dictation",
     _dictate),
    (rf"(?:(?:start|begin|take|do|go into|turn on|switch to|enter) )?(?:a )?dictat(?:e|ion)(?: mode)?(?: something)?"
     rf"{_APP}{_POLISH}", "start_dictation", _dictate),
    (rf"(?:i (?:want|need|would like|wanna) to|id like to|let me|can i) (?:dictate|type|write)(?: something| a message)?"
     rf"(?: down)?{_APP}{_POLISH}", "start_dictation", _dictate),
    (rf"type(?: out| in| up)? (?P<words>.+?){_APP}{_POLISH}", "type_text", _words),
]
