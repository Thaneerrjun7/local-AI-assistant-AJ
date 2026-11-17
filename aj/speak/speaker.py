"""AJ (Speak)'s logic, with no Qt: one dictation from a click on the mic to "Typed".

The user, 2026-10-22: speech to text "anywhere where I can type anything", "I want it to be
accurate", and "I do not want this AI to type the message and send it for me. I'll do that myself."
So a dictation is, in order:
1. Where: the window in front right now (dictation.choose_target). AJ (Speak) never takes the
   focus (window.py), so that is the app he was typing in, never this window. If its focus is not
   in a text box the dictation does not start at all ("Click into a text box first"), so he isn't
   made to talk for a minute to a window that can't take it.
2. Listen (listen.py): the microphone is open only for this, until he pauses, clicks again, hits the
   cap, or cancels.
3. Write it down: Gemini (dictation.transcribe). Parakeet, the offline ears, is loaded only when
   Gemini can't, and then he is told to check it; it is let go again as soon as Gemini works.
4. If Polite is on, polish it (dictation.polish): refined, polite English, the same meaning.
5. Type it (dictation.type_into): real key presses where his cursor is. Never Enter, never the
   clipboard, never sent.

Everything slow (the microphone, Gemini, typing) is on a thread of its own, one per dictation, so
the window stays responsive. Results go out through two callbacks, on_state and on_level, which are
called from that thread (on_state with the lock held), so they may only feed a Qt signal or record
the call, and never call back into the Speaker. Every state change is made under that one lock and
only by the dictation that is still current, so a dictation he cancelled can never overwrite the
one he started next, and can never type.

While a dictation is under way (listening, writing it down, typing) the named event
Local\\AJ.Speak.Listening is raised (core/mic_share.py), so the assistant, if it is on, drops what
it hears meanwhile instead of taking his words for a command. And while the assistant waits for
words of its own ("write this down"), a dictation here doesn't start: he is told it's busy (the
review of 2026-10-22).

Cancelling (the x) is instant even while Gemini is thinking: the dictation is dropped on the spot
and its thread finishes by itself, typing nothing. The one moment it can't be cancelled is while the
keys are going in. Nothing here logs what he said.
"""
import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass, field

from aj.config import Settings
from aj.core import audio, dictation, mic_share
from aj.core.audio import MIC_RATE
from aj.speak import listen

log = logging.getLogger(__name__)

# ---- the states the window shows ---------------------------------------------------------------
READY = "ready"
LISTENING = "listening"
TRANSCRIBING = "transcribing"
POLISHING = "polishing"
TYPING = "typing"
TYPED = "typed"          # typed, nothing to check
CHECK = "check"          # typed, but he should look at it (offline ears, not polished, cut at the cap)
ERROR = "error"          # nothing was typed (or not all of it): the text says why
CANCELLED = "cancelled"
STARTING = "starting"    # a dictation's phase only: the mic is opening, the text box is being checked
RESULTS = (TYPED, CHECK, ERROR, CANCELLED)

# ---- what the window says ---------------------------------------------------------------------
TYPED_TEXT = "Typed"
CLICK_FIRST = "Click into a text box first."
AJ_BUSY = "AJ is taking dictation right now. Finish that first."
CANCELLED_TEXT = "Cancelled. Nothing typed."
NOTHING_HEARD = "Didn't hear anything. Click the mic and try again."
NO_WORDS = "Couldn't make out any words. Try again."
NO_EARS = "Gemini is unavailable and the offline ears wouldn't load. Try again later."
NO_MIC = "Couldn't open the microphone. Check it is plugged in and allowed."
MIC_STOPPED = "The microphone stopped sending sound. Try again."
NO_VAD = "Couldn't load the listening model. Details are in the log."
SOMETHING_WRONG = "Something went wrong. Details are in the log."
OFFLINE_NOTE = "Gemini unavailable, used offline ears: check it."
NOT_POLISHED_NOTE = "Couldn't polish it."
# type_into() says "then say type that", which is for the assistant; here he clicks and speaks again.
_CLICK_FIRST_REASONS = {"no_window", "no_focus", "not_editable", "unknown_field"}


def _load_ears(voice):
    """Parakeet (0.7 GB), only when it is asked for."""
    from aj.core.speech import Ears  # here: it imports the speech libraries and the model is large
    return Ears(voice)


def verdict(outcome: dictation.Outcome, engine: str = "gemini", how: str = "plain",
            capped_at: float = 0.0) -> tuple[str, str]:
    """(state, text) for what type_into() did. The text for a failure is the core's own honest line,
    except where it talks to the assistant's voice; a success is just "Typed", with a note when he
    should look at it before he sends it."""
    if not outcome.ok:
        return ERROR, CLICK_FIRST if outcome.reason in _CLICK_FIRST_REASONS else outcome.line
    notes = []
    if engine == "offline":
        notes.append(OFFLINE_NOTE)
    if how == "unpolished":
        notes.append(NOT_POLISHED_NOTE)
    if capped_at:
        notes.append(f"Stopped listening at {capped_at:g} seconds.")
    return (CHECK, f"{TYPED_TEXT}. {' '.join(notes)}") if notes else (TYPED, TYPED_TEXT)


class MicShare:
    """core/mic_share.py as the Speaker uses it (tests pass a fake)."""

    def aj_dictating(self) -> bool:
        return mic_share.aj_dictating()

    def listening(self, on: bool) -> None:
        try:
            mic_share.speak_flag.set(on)
        except OSError as exc:  # no event: the assistant may hear him too, but the dictation works
            log.error("couldn't tell the assistant a dictation is %s: %s", "on" if on else "off", exc)


@dataclass(eq=False)
class _Job:
    """One dictation. `hwnd` is where it types, fixed when he started."""
    hwnd: int
    finish: threading.Event = field(default_factory=threading.Event)  # stop listening, now
    phase: str = STARTING
    cancelled: bool = False


class Speaker:
    def __init__(self, settings: Settings | None = None, *, desktop=None, watch=None, mic_factory=None,
                 vad_factory=None, ears_factory=None, transcribe=None, polish=None, type_into=None,
                 share=None):
        """The keyword arguments are for the tests, which give it a fake desktop, mic, voice detector
        and Gemini; left out, they are the real ones."""
        self.settings = settings or Settings()
        self.s = self.settings.dictation
        self.desktop = desktop or dictation.Desktop()
        self.watch = watch if watch is not None else dictation.FrontWatch(self.desktop)
        self._mic_factory = mic_factory or audio.Mic
        self._vad_factory = vad_factory or listen.Endpointer
        self._ears_factory = ears_factory or _load_ears
        self._transcribe = transcribe or dictation.transcribe
        self._polish = polish or dictation.polish
        self._type_into = type_into or dictation.type_into
        self._share = share or MicShare()  # who has his words (core/mic_share.py)
        self.on_state: Callable[[str, str], None] = lambda state, text: None
        self.on_level: Callable[[float], None] = lambda level: None
        self.polite = False       # the window's Polite toggle
        self.state = READY
        self._lock = threading.Lock()
        self._job: _Job | None = None
        self._vad = None
        self._vad_lock = threading.Lock()
        self._ears = None
        self._ears_failed = False
        self._ears_lock = threading.Lock()

    # -- life --------------------------------------------------------------------------------------

    def start(self) -> None:
        """Watch which window is in front, and get the voice detector and the audio library ready in
        the background, so the first click doesn't wait for them."""
        self.watch.start()
        threading.Thread(target=self._warm, daemon=True, name="speak-warm").start()

    def _warm(self) -> None:
        try:
            if self._mic_factory is audio.Mic:
                audio.preload()
            self._voice_detector()
        except Exception as exc:  # noqa: BLE001 - the first dictation reports it properly
            log.error("couldn't get ready in the background: %s", exc)

    def close(self) -> None:
        self.cancel()
        self.watch.stop()

    # -- what the window calls --------------------------------------------------------------------

    @property
    def busy(self) -> bool:
        return self._job is not None

    def toggle(self) -> None:
        """The mic button and the hotkey: start a dictation, or while listening, finish it now.
        A press does nothing in the moment the microphone is opening (a double-click would
        otherwise end it before it began), or while the words are being written down and typed."""
        with self._lock:
            job = self._job
            if job is None:
                self._begin()
            elif job.phase == LISTENING:
                job.finish.set()

    def listen(self) -> bool:
        """Start a dictation now, unless one is already under way (then nothing changes, so a
        second "AJ, speak" never ends the one he is in the middle of). True if one started."""
        with self._lock:
            if self._job is not None:
                return False
            self._begin()
            return self._job is not None

    def cancel(self) -> bool:
        """The x: drop the dictation and type nothing. False when there is none, or the keys are
        already going in."""
        with self._lock:
            job = self._job
            if job is None or job.phase == TYPING:
                return False
            job.cancelled = True
            self._job = None
            self._share.listening(False)  # his next words are the assistant's again
            self.state = CANCELLED
            self.on_state(CANCELLED, CANCELLED_TEXT)
        job.finish.set()
        return True

    # -- one dictation -------------------------------------------------------------------------------

    def _begin(self) -> None:
        """Called with the lock held, on the caller's thread: choose where it types, then go."""
        if self._share.aj_dictating():  # one dictation at a time
            self.state = ERROR
            self.on_state(ERROR, AJ_BUSY)
            return
        hwnd = dictation.choose_target("", self.desktop, self.watch)
        if not hwnd:
            self.state = ERROR
            self.on_state(ERROR, CLICK_FIRST)
            return
        job = _Job(hwnd)
        self._job = job
        self._share.listening(True)
        threading.Thread(target=self._run, args=(job,), daemon=True, name="speak-job").start()

    def _go(self, job: _Job, state: str, text: str = "") -> bool:
        """Move this dictation on to `state`. False if it was cancelled meanwhile: stop quietly."""
        with self._lock:
            if self._job is not job:
                return False
            job.phase = self.state = state
            self.on_state(state, text)
            return True

    def _end(self, job: _Job, state: str, text: str) -> None:
        with self._lock:
            if self._job is not job:
                return  # cancelled: the window already says so
            self._job = None
            self.state = state
            self.on_state(state, text)

    def _level(self, job: _Job, level: float) -> None:
        if self._job is job:
            self.on_level(level)

    def _run(self, job: _Job) -> None:
        try:
            end = self._session(job)
        except Exception:  # noqa: BLE001 - whatever it was, he sees a line and the log has the details
            log.exception("a dictation failed")
            end = (ERROR, SOMETHING_WRONG)
        finally:
            with self._lock:
                if self._job is None or self._job is job:  # not while a newer dictation listens
                    self._share.listening(False)
        if end is not None:
            self._end(job, *end)

    def _check_box(self, hwnd: int) -> bool:
        """Is the cursor in a text box in that window? The same test type_into() makes at the end,
        made first so he isn't made to talk to a window that would refuse it."""
        return bool(self.desktop.alive(hwnd) and self.desktop.focus(hwnd) and self.desktop.editable() is True)

    def _session(self, job: _Job) -> tuple[str, str] | None:
        """The whole dictation. Returns the final (state, text), or None when it was cancelled."""
        self._ears_failed = False  # a model that wouldn't load last time gets another go
        try:
            mic = self._mic_factory()
            mic.open()  # first, so nothing he says while the box is checked is lost: it queues up
        except Exception as exc:  # noqa: BLE001 - no device, no permission, in use
            log.error("couldn't open the microphone: %s", exc)
            return ERROR, NO_MIC
        try:
            if not self._check_box(job.hwnd):
                return ERROR, CLICK_FIRST
            try:
                detector = self._voice_detector()
            except Exception as exc:  # noqa: BLE001 - the model file is missing or won't load
                log.error("couldn't load the voice detector: %s", exc)
                return ERROR, NO_VAD
            if not self._go(job, LISTENING):
                return None
            recording = listen.record(mic, detector, self.s, job.finish, lambda: job.cancelled,
                                      on_level=lambda level: self._level(job, level))
        finally:
            mic.close()  # the microphone is open only while listening
        if job.cancelled:
            return None
        if not recording.pieces:
            return ERROR, MIC_STOPPED if recording.reason == listen.STALLED else NOTHING_HEARD
        if not self._go(job, TRANSCRIBING):
            return None
        heard = self._hear(job, recording.pieces)
        if job.cancelled:
            return None
        if heard.engine == "gemini":
            self._let_go_of_the_ears()
        text = heard.text.strip()
        if not text:
            return ERROR, NO_EARS if self._ears_failed else NO_WORDS
        words, how = text, "plain"
        if self.polite:
            if not self._go(job, POLISHING):
                return None
            better = self._polish(text, self.s)
            if job.cancelled:
                return None
            if better:
                words, how = better, "polished"
            else:
                how = "unpolished"
        if not self._go(job, TYPING):  # from here it can't be cancelled
            return None
        outcome = self._type_into(job.hwnd, words, how, heard.engine == "offline", self.desktop)
        capped_at = self.s.max_secs if recording.reason == listen.CAPPED else 0.0
        return verdict(outcome, heard.engine, how, capped_at)

    def _hear(self, job: _Job, pieces) -> dictation.Heard:
        where = "a WhatsApp chat" if dictation.is_whatsapp(self.desktop, job.hwnd) else "a text box"
        return self._transcribe(pieces, MIC_RATE, f"It is a message being typed into {where}.",
                                local=self._offline, settings=self.s)

    # -- the ears ----------------------------------------------------------------------------------

    def _voice_detector(self):
        with self._vad_lock:
            if self._vad is None:
                self._vad = self._vad_factory(self.settings.voice)
            return self._vad

    def _offline(self, piece) -> str:
        """Parakeet's transcript of one piece, loading it the first time Gemini can't do it."""
        with self._ears_lock:
            if self._ears is None:
                if self._ears_failed:
                    return ""
                try:
                    self._ears = self._ears_factory(self.settings.voice)
                except Exception:  # noqa: BLE001 - missing files, no memory
                    log.exception("couldn't load the offline ears")
                    self._ears_failed = True
                    return ""
            ears = self._ears
        return str(ears.transcribe(piece) or "")

    def _let_go_of_the_ears(self) -> None:
        """Gemini is working again: give Parakeet's memory back."""
        with self._ears_lock:
            self._ears = None
