"""One conversation turn with every stage overlapping:
your speech -> transcript -> model token stream -> sentence chunks -> voice -> speakers.

AJ starts speaking the first chunk while the model is still writing the rest.
Each turn is timed from the moment you stopped talking, so the latency report shows
where every millisecond went.

Dictation (2026-10-22): after "write this down" (or a WhatsApp chat opening), the user's next
words are the message, not a turn. They're collected as audio, never transcribed here, until he
has been quiet for DictationSettings.silence_secs (1.5 s: people pause mid-sentence while
dictating; a normal turn ends after 0.3 s), then handed to Brain.dictate. The VAD itself still
cuts at 0.3 s (core/speech.py), and its pieces are joined, so nothing about the ears changes.

Sleep mode (2026-10-22): asleep, everything heard goes past core/wake.py's Gate first and only
an utterance that starts with its name ("Hey AJ, open WhatsApp") gets through, the name taken
off. The rest is dropped unseen: never shown, answered or remembered, and no dictation is taken.
Listening while asleep is for mains power only (the user's rule): unplugged, the mic closes.

The review of 2026-10-22:
- A dictation's words never reach the log: on_turn (which engine.print_turn writes to aj.log) is
  given "(dictation, N characters)" for them. The window still shows them as he said them.
- Talking over the assistant stops any typing in flight (Dictation.abort), as well as the voice.
- While AJ (Speak) takes a dictation (core/mic_share.py), every utterance that ends is dropped:
  his words are for it, not commands. Not shown, answered or logged, and no barge-in either.
- The mic reopening can fail (a headset plugged in while asleep, a device gone): it is tried a few
  times, then the window comes up saying the microphone isn't working, and it is tried again
  every few seconds. Until then an error there ended Session.run silently, leaving a deaf
  assistant that still held its single-instance lock.
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

from aj.config import Settings
from aj.core.audio import MIC_BLOCK, MIC_RATE, Player
from aj.core.barge import BargeIn
from aj.core import mic_share
from aj.core.chunker import clean_for_speech
from aj.core.llm import wake_gpu
from aj.core.speech import Ears, Voice, looks_unfinished
from aj.core.wake import Gate, Heard, Mode, split_wake

if TYPE_CHECKING:
    from aj.core.brain import Brain


# The whole utterance must be the command, so "turn off the music" is not mistaken for it.
# "AJ" since 2026-10-22, as Parakeet writes it ("AJ", "A.J.", "A J"), or "Computer".
# "All right, turn off." (live, 2026-10-23) went to the model, which took it for the laptop.
_OFF = re.compile(r"^\W*((?:all\W*right|ok(?:ay)?|right|so|then)\W+)?(hey\W+)?((?:computer|aj|a\W?j)\W+)?(please\W+)?"
                  r"((turn|switch|power)\W+(yourself\W+)?off|shut\W*(yourself\W+)?down|go\W+dark|good\W*bye|"
                  r"de\W*activate(\W+yourself)?)"
                  r"(\W+(computer|aj|now|please))*\W*$", re.IGNORECASE)


_YES = re.compile(r"^\W*(?:(?:uh|um|ok|okay|yeah)\W+)?(?:yes|yeah|yep|yup|sure|do it|go ahead|confirm(?:ed)?|"
                  r"affirmative|please do|ok(?:ay)?|correct)\b", re.IGNORECASE)
_NO = re.compile(r"^\W*(?:(?:uh|um|oh)\W+)?(?:no|nope|nah|don'?t|do not|cancel|stop|never ?mind|negative|wait|"
                 r"not (?:now|yet|today))\b",
                 re.IGNORECASE)


def yes_or_no(text: str) -> bool | None:
    """A spoken answer to a yes/no question, or None when it's neither."""
    if _NO.search(text):
        return False
    return True if _YES.search(text) else None


def is_off_command(text: str) -> bool:
    """'Computer, turn off' turns AJ off. The laptop is only ever turned off by name ("shut down
    the laptop"), through tools/power.py and a yes in the window. The name may be any spelling
    Parakeet uses for it ("OJ turn off.", measured 2026-10-22: core/wake.py)."""
    return bool(_OFF.search(text) or _OFF.search(split_wake(text)[1]))


class Status(StrEnum):
    """What AJ is doing right now. The window shows it; the console modes ignore it."""
    LOADING = "loading"
    LISTENING = "listening"
    HEARING = "hearing"
    THINKING = "thinking"
    SPEAKING = "speaking"
    MIC_OFF = "mic off"
    TURNING_OFF = "turning off"
    DICTATING = "dictating"  # waiting for, or hearing, words to type
    MIC_FAILED = "mic failed"  # the mic wouldn't open: shown, with the window up, until it does (2026-10-22)
    # Sleep mode (2026-10-22): in the tray, answering only its name. On battery the mic is closed
    # (the user: unplugged "this feature should not be available at all"); with the mic button
    # off nothing said can wake it either. The tray and the shortcut always can.
    ASLEEP = "asleep"
    ASLEEP_ON_BATTERY = "asleep on battery"
    ASLEEP_MIC_OFF = "asleep, mic off"


ASLEEP_STATUSES = frozenset({Status.ASLEEP, Status.ASLEEP_ON_BATTERY, Status.ASLEEP_MIC_OFF})


def pick_status(listening: bool, answering: bool, hearing: bool, speaking: bool, dictating: bool = False,
                asleep: bool = False, unplugged: bool = False, mic_failed: bool = False) -> Status:
    """Hearing you wins over everything (with headphones you can talk over him); a typed
    question still gets Thinking/Speaking with the mic off. Waiting for dictation shows as such,
    even while he talks, so he knows his words will be typed, not answered. Asleep, talk in the
    room isn't "hearing you": it's only listened to for the name. `unplugged`: asleep with the
    mic closed because the laptop is on battery. `mic_failed`: the mic is wanted but wouldn't
    open; that shows whenever nothing is being answered, asleep or awake."""
    if mic_failed and not answering:
        return Status.MIC_FAILED
    if asleep and not answering:
        if unplugged:
            return Status.ASLEEP_ON_BATTERY
        return Status.ASLEEP if listening else Status.ASLEEP_MIC_OFF
    if listening and dictating and not answering:
        return Status.DICTATING
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


_MODE_LINES = {"wake": "Yes, sir?", "sleep": "Very good, sir. Call when you need me.", "clap": "Hello, sir."}


def mode_line(lines, pool: str) -> str:
    """What it says when its name alone wakes it ("wake") or when it goes to sleep ("sleep"):
    from the persona's rotation (lines.POOLS), or a plain line without one."""
    try:
        return lines.pick(pool)
    except (AttributeError, KeyError):
        return _MODE_LINES[pool]


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
                self.player.play(audio, text)
            finally:
                self._queue.task_done()


class WavMic:
    """Feeds WAV files into the pipeline in real time, as if you said them into the mic.
    Each file starts only once AJ has finished answering the previous one."""

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


# Said over AJ, the start of the user's words is kept and put in front of the rest; this
# long after the interruption, it no longer belongs to anything ("stop" and then silence).
CUT_IN_KEEP_SECS = 4.0
# The quiet put between the pieces of a turn when they are heard again as one (_hear_whole).
WHOLE_GAP_SECS = 0.15
# The mic reopening (2026-10-22): tries in a row, the wait between them, and how long after a
# failed round it is tried again.
MIC_TRIES = 3
MIC_TRY_WAIT_SECS = 0.5
MIC_RETRY_SECS = 5.0


def dictation_label(chars: int) -> str:
    """What the log shows for a dictation instead of its words (the review of 2026-10-22)."""
    return f"(dictation, {chars} characters)" if chars else "(dictation)"


class Session:
    """Listens, decides when you've finished, and answers.

    While AJ speaks, the mic keeps listening (`interrupt`, the default since 2026-10-18):
    core/barge.py tells his own voice coming back through the speakers from the user talking
    over him. When it's the user, he stops at once, the user's words are heard out from their
    start, and only what the user actually heard of the cut-off reply stays in the conversation.
    With headphones, `barge_in=True` stops him at any speech at all, as before.

    The window drives it from another thread: set_listening() turns the mic on or off, ask()
    answers typed text, and sleep() and wake() switch sleep mode. They only leave a note; the
    loop thread does the work, so the mic stream, the VAD and the Gate are never touched from
    two threads at once."""

    def __init__(self, settings: Settings, ears: Ears, brain: "Brain", mouth: Mouth | None, source,
                 stop: threading.Event, barge_in: bool = False, on_turn=None,
                 on_status: Callable[[Status], None] | None = None,
                 on_heard: Callable[[str], None] | None = None, listening: bool = True,
                 interrupt: bool = False, dictation=None, gate: Gate | None = None,
                 on_ac: Callable[[], bool | None] | None = None, ready: Callable[[], None] | None = None,
                 speak_listening: Callable[[], bool] = mic_share.speak_listening,
                 wakes_gpu: Callable[[], bool] | None = None, clap=None):
        """`dictation`: the engine's core.dictation.Dictation. While it's armed, speech is taken
        down for typing (Brain.dictate) instead of answered.
        `gate`: sleep mode (core/wake.py); None, always awake (the console modes and older tests).
        `on_ac`: core/power_source.on_ac_power, asked every few seconds while asleep.
        `ready`: called on the answer's thread before the model is needed (the engine reloads it
        there if sleep mode let go of it).
        `speak_listening`: is AJ (Speak) taking a dictation? Then what is heard isn't for us.
        `wakes_gpu`: is the model on the RTX 4050, whose clocks the pings while you talk are for?
        None: always (as before core/placement.py); on the 780M they would only cost power.
        `clap`: a core.clap.ClapDetector. Asleep, two claps wake him like his name does, and he
        says hello (the "clap" lines); awake it hears nothing."""
        self.clap = clap
        self._clap_fed = False  # the detector has audio from this sleep (reset when he wakes)
        self.s = settings.voice
        self.d = settings.dictation
        self.dictation = dictation
        self.gate, self.on_ac, self.ready = gate, on_ac, ready
        self.wakes_gpu = wakes_gpu
        self._speak_listening = speak_listening
        self._power_every = settings.sleep.power_check_secs
        self._power_at = 0.0     # when the power cable was last looked at (0: look now)
        self._plugged_in = True
        self._mode_note: Mode | None = None  # sleep() or wake() from another thread, for the loop
        self._taken: list[np.ndarray] = []  # the dictation so far, as the VAD cut it
        self._taking_since = 0.0  # when listening for dictation began (0: not yet)
        self._last_voice = 0.0    # when he was last heard while dictating
        self._overflow = False    # past the length cap: what he still says is let go until a pause
        self.ears, self.brain, self.mouth, self.source = ears, brain, mouth, source
        self.stop, self.barge_in, self.on_turn = stop, barge_in, on_turn
        self.on_status, self.on_heard = on_status, on_heard
        self.quiet = threading.Event()  # set while AJ is not answering
        self.quiet.set()
        self._worker: threading.Thread | None = None
        self._listening = listening  # what the mic button says; the loop makes it so
        self._typed: queue.Queue[str] = queue.Queue()
        self._nudge = threading.Event()  # wakes the loop early while the mic is closed
        self._status: Status | None = None
        self._turn_started = 0.0
        self._barge = (BargeIn(ears.transcribe, mouth.player.said_lately)
                       if interrupt and not barge_in and ears is not None and mouth is not None else None)
        self._cut = False  # the user talked over this reply
        self._cut_at = 0.0
        self._cut_start: np.ndarray | None = None  # the user's first words, said over AJ
        self._settle = False  # an answer just ended: reset the ears before listening on
        self._answer_to: Callable[[bool], None] | None = None  # a yes/no question waiting for a spoken answer
        self._mic_failed = False  # the mic is wanted but wouldn't open
        self._mic_retry_at = 0.0  # when to try it again

    def set_listening(self, on: bool) -> None:
        """Mic on or off, from any thread. Off closes the mic stream itself, not just ignores it.
        A click on the mic button after the mic failed tries it again at once."""
        self._listening = on
        self._mic_retry_at = 0.0
        self._nudge.set()

    def ask(self, text: str, audio: list[np.ndarray] | None = None) -> None:
        """Answer typed text as if it had been said. Waits its turn if AJ is mid-answer.
        Typing wakes it from sleep mode: only a shown window can be typed into. `audio`: the
        speech behind the text, when there was some (the listener's --wake-command recording)."""
        self._typed.put((text, audio))
        self._nudge.set()

    def sleep(self) -> None:
        """Into sleep mode, from any thread (the tray's Sleep)."""
        self._mode_note = Mode.ASLEEP
        self._nudge.set()

    def wake(self) -> None:
        """Out of sleep mode, from any thread (the tray, the shortcut). No line is said: the
        window coming back says it."""
        self._mode_note = Mode.AWAKE
        self._nudge.set()

    @property
    def asleep(self) -> bool:
        return self.gate is not None and self.gate.asleep

    def _answer(self, text: str, times: TurnTimes, audio: list[np.ndarray] | None = None,
                said: str | None = None) -> None:
        """`said`: the utterance as heard, name and all ("Hey AJ, open WhatsApp"), for the window;
        `text` is what's answered ("open WhatsApp")."""
        self.quiet.clear()
        shown = said or text
        if self.on_heard:
            self.on_heard(shown)
        if is_off_command(text):
            if self.on_turn:
                self.on_turn(shown, "(turning off)", times)
            self.stop.set()
            return
        if self.mouth is not None:
            self.mouth.player.new_turn()
        if self.dictation is not None:
            self.dictation.hear(audio)  # "type <words>": Gemini hears this turn's own audio
        if self.ready is not None:
            self.ready()
        reply = self.brain.reply(text, self.mouth, times)
        typed = getattr(self.brain, "typed_turn", None)  # a dictation: its words stay out of the log
        self._end_answer(shown if typed is None else dictation_label(typed), reply, times)

    def _say_mode_line(self, pool: str, said: str, times: TurnTimes) -> None:
        """Its name alone ("Hey AJ.": "wake") or "go to sleep" ("sleep"): a short line and no
        model. Not part of the conversation, so the brain's history is left alone."""
        self.quiet.clear()
        if self.on_heard:
            self.on_heard(said)
        line = mode_line(getattr(self.brain, "lines", None), pool)
        if self.mouth is not None:
            self.mouth.player.new_turn()
            self.mouth.say(clean_for_speech(line), times)
            self.mouth.wait_done()
            times.first_sound = self.mouth.player.first_sound_at or 0.0
        if self.on_turn:
            self.on_turn(said, line, times)
        if not self._cut:
            self._settle = True
        self.quiet.set()

    def _answer_dictation(self, pieces: list[np.ndarray] | None, times: TurnTimes, capped: bool) -> None:
        """His dictation (or None: he said nothing in time), typed where he asked."""
        self.quiet.clear()
        if self.mouth is not None:
            self.mouth.player.new_turn()
        if self.ready is not None:
            self.ready()  # polishing offline needs the local model
        heard, reply = self.brain.dictate(pieces, self.mouth, times, capped=capped)
        if self.on_heard and heard:
            self.on_heard(heard)  # the window shows his words; the log (on_turn) only their length
        self._end_answer(dictation_label(len(heard)), reply, times)

    def _end_answer(self, text: str, reply: str, times: TurnTimes) -> None:
        if self.mouth is not None:
            self.mouth.wait_done()
            times.first_sound = self.mouth.player.first_sound_at or 0.0
        if self._cut:
            heard = self.mouth.player.heard() if self.mouth is not None else ""
            self.brain.cut_off(heard)  # what the user didn't hear was never said
            reply = f"{heard} ... (cut off)".strip()
        if self.on_turn:
            self.on_turn(text, reply, times)
        if not self._cut:  # cut off, the user is still talking: keep listening to him
            self._settle = True  # the loop thread resets the ears: only it may touch them
        self.quiet.set()

    # ----- dictation -----

    def _dictating(self) -> bool:
        """Is a dictation waiting for his words? One that ended elsewhere (he typed it instead)
        leaves nothing behind."""
        armed = self.dictation is not None and self.dictation.armed is not None
        if not armed and (self._taken or self._taking_since):
            self._taken, self._taking_since = [], 0.0
        return armed

    def _drop_dictation(self) -> None:
        """Mic off: no dictation either (off means stop listening now)."""
        if self.dictation is not None:
            self.dictation.disarm()
        self._taken, self._taking_since, self._overflow = [], 0.0, False

    def _take_down(self, utterance: np.ndarray) -> None:
        self._taken.append(utterance)
        self._last_voice = time.perf_counter()

    def _dictation_step(self) -> None:
        """Between mic blocks while a dictation is waiting: start it once he has finished (a long
        pause, or the length cap), or give up if he never starts."""
        now = time.perf_counter()
        if not self._taking_since:
            self._taking_since = self._last_voice = now
        hearing = self.ears.hearing_speech
        if hearing:
            self._last_voice = now
        taken = sum(len(p) for p in self._taken) / MIC_RATE
        if self._taken and taken >= self.d.max_secs:
            self._overflow = hearing  # still talking: the rest is let go, never answered
            self._finish_dictation(capped=True)
        elif self._taken and not hearing and now - self._last_voice >= self.d.silence_secs:
            self._finish_dictation(capped=False)
        elif not self._taken and not hearing and now - self._taking_since >= self.d.start_secs:
            self._finish_dictation(capped=False)  # nothing said: Brain.dictate says dictation is off

    def _finish_dictation(self, capped: bool) -> None:
        if self.gate is not None:
            self.gate.touch()  # a dictation is activity: the idle timer starts again
        pieces, self._taken, self._taking_since = self._taken or None, [], 0.0
        stopped = time.perf_counter() - (0.0 if capped else self.d.silence_secs)
        self._start_turn(self._answer_dictation, (pieces, TurnTimes(stopped_talking=stopped), capped))

    def _let_go(self, utterances: list[np.ndarray]) -> bool:
        """After a dictation hit its length cap mid-sentence, the rest of what he says is dropped
        until he pauses, so it's never taken for a command. True while that lasts."""
        if not self._overflow:
            return False
        now = time.perf_counter()
        if self.ears.hearing_speech or utterances:
            self._last_voice = now
        elif now - self._last_voice >= self.d.silence_secs:
            self._overflow = False
        return True

    def listen_for_answer(self, answer: Callable[[bool], None] | None) -> None:
        """A yes/no question is open (Engine.confirm): the next spoken yes or no answers it.
        None: the question is settled. From any thread."""
        self._answer_to = answer

    def _settle_ears(self) -> None:
        """After an answer: forget what the mic heard meanwhile (his own voice). On the loop
        thread only. Done from the answer's thread while the loop fed the same VAD (since the
        mic listens during answers, 2026-10-18), the two collided and AJ crashed with
        "vector too long" (live, 2026-10-19)."""
        self._settle = False
        self.ears.reset()
        while not self.source.blocks.empty():  # drop any echo of AJ's own voice
            self.source.blocks.get_nowait()
        if self._barge is not None:
            self._barge.reset()

    def _hear_whole(self, pieces: list[np.ndarray]) -> str:
        """A turn the voice detector cut in pieces at his pauses, heard again as one: Parakeet
        gets words right from the whole sentence that it gets wrong from a half (2026-10-23, the
        user: understood only when he spoke carefully). One more transcription, only for such turns."""
        gap = np.zeros(int(MIC_RATE * WHOLE_GAP_SECS), dtype=np.float32)
        joined = np.concatenate([part for piece in pieces for part in (piece, gap)][:-1])
        try:
            return self.ears.transcribe(joined)
        except Exception as exc:  # noqa: BLE001 - the pieces' own words still stand
            print(f"(hearing the whole turn again failed: {exc})", flush=True)
            return ""

    def _hear(self, block: np.ndarray) -> list[np.ndarray]:
        """One mic block into the VAD. A VAD error costs what was being heard, never AJ."""
        try:
            return self.ears.feed(block)
        except (ValueError, RuntimeError) as exc:
            print(f"(listening hiccup, starting the ears afresh: {exc})", flush=True)
            self.ears.reset()
            return []

    def _answer_by_voice(self, utterances: list[np.ndarray]) -> None:
        answer = self._answer_to
        for utterance in utterances:
            said = yes_or_no(self.ears.transcribe(utterance))
            if said is not None and answer is not None:
                self._answer_to = None
                answer(said)
                return

    def _cut_in(self) -> None:
        """The user started talking over AJ: stop him now, and hear the user out from the
        start of what they said (the VAD restarts on the user alone, so the echo before it is
        left out)."""
        self._cut, self._cut_at = True, time.perf_counter()
        self._cut_start = self._barge.recent_audio()
        self._barge.reset()
        self.ears.reset()
        self.brain.cancel.set()
        abort = getattr(self.dictation, "abort", None)
        if abort is not None:
            abort()  # typing still in flight stops too (the review of 2026-10-22)
        if self.mouth is not None:
            self.mouth.silence()

    def _keep_gpu_awake_while_you_talk(self) -> None:
        """A single ping isn't enough: the GPU starts clocking down again right after it.
        So ping every 0.4 s while you're talking (one at a time), and never when you're not."""
        if not self.ears.hearing_speech or time.perf_counter() - self._last_wake < 0.4:
            return
        if self.wakes_gpu is not None and not self.wakes_gpu():
            return
        if self._waker is not None and self._waker.is_alive():
            return
        self._last_wake = time.perf_counter()
        self._waker = threading.Thread(target=wake_gpu, args=(self.brain.base,), daemon=True)
        self._waker.start()

    def _switch_mic(self, on: bool) -> bool:
        """Open or close the mic stream (off releases the device). Returns whether it is open now.
        Mid-answer the VAD is left alone: the answer resets it when it finishes.

        Opening can fail (PortAudio's error when the device was plugged in or out while the stream
        was closed, say). Until 2026-10-22 that error ended Session.run, silently, while the
        process kept running and holding its single-instance lock: a deaf assistant nothing could
        restart. Now it is tried MIC_TRIES times, and if it still won't open, the status says so
        (Status.MIC_FAILED brings the window up) and it is tried again every MIC_RETRY_SECS."""
        if not on:
            self.source.close()
            self._mic_failed = False
        else:
            for attempt in range(MIC_TRIES):
                try:
                    self.source.open()
                    break
                except Exception as exc:  # noqa: BLE001 - PortAudio's errors are plain Exceptions
                    try:
                        self.source.close()  # half-open: let go of what there is
                    except Exception:  # noqa: BLE001 - nothing more to let go of
                        pass
                    if attempt + 1 == MIC_TRIES or self.stop.wait(MIC_TRY_WAIT_SECS):
                        print(f"(the microphone wouldn't open: {exc}; trying again in {MIC_RETRY_SECS:g} s)",
                              flush=True)
                        self._mic_failed = True
                        self._mic_retry_at = time.perf_counter() + MIC_RETRY_SECS
                        return False
            self._mic_failed = False
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
        """Report the status only when it changes, so an idle AJ sends nothing."""
        answering = not self.quiet.is_set()
        sound_at = self.mouth.player.first_sound_at if self.mouth is not None else None
        speaking = answering and sound_at is not None and sound_at >= self._turn_started
        # Mid-answer without barge-in the mic is ignored, so it can't be "hearing" you then.
        hearing = mic_on and self.ears.hearing_speech and (self.barge_in or not answering)
        dictating = self.dictation is not None and self.dictation.armed is not None
        unplugged = self.asleep and self._listening and not mic_on and not self._mic_failed
        status = pick_status(mic_on, answering, hearing, speaking, dictating, self.asleep, unplugged,
                             mic_failed=self._mic_failed and not mic_on)
        if status != self._status:
            self._status = status
            if self.on_status:
                self.on_status(status)

    def _start_answer(self, text: str, times: TurnTimes, audio: list[np.ndarray] | None = None,
                      said: str | None = None) -> None:
        self._start_turn(self._answer, (text, times, audio, said))

    def _route(self, text: str, times: TurnTimes, audio: list[np.ndarray] | None = None,
               typed: bool = False) -> bool:
        """What was said (or typed), past sleep mode first. False: dropped (asleep, not its name)."""
        if self.gate is None or (typed and self._dictating()):  # typed while dictating: the message
            self._start_answer(text, times, audio)
            return True
        heard = self.gate.route(text, typed=typed)
        if heard.action == Heard.IGNORE:
            return False
        if heard.action == Heard.ANSWER:
            self._start_answer(heard.text, times, audio, said=text)
        else:
            self._start_turn(self._say_mode_line, ("wake" if heard.action == Heard.WAKE else "sleep", text, times))
        return True

    def _mic_wanted(self) -> bool:
        """The mic button's setting, except asleep on battery: then no listening at all. The power
        cable is looked at every few seconds while asleep (a cheap call), never while awake."""
        if not self._listening or not self.asleep:
            return self._listening
        now = time.perf_counter()
        if not self._power_at or now - self._power_at >= self._power_every:
            self._power_at = now
            self._plugged_in = Gate.hears_wake_words(self.on_ac() if self.on_ac is not None else True)
        return self._plugged_in

    def _apply_mode_note(self) -> None:
        note, self._mode_note = self._mode_note, None
        if note is None or self.gate is None:
            return
        if note == Mode.ASLEEP:
            self.gate.sleep()
        else:
            self.gate.wake()

    def _clapped(self, block: np.ndarray | None, busy: bool) -> bool:
        """Asleep and not answering: the block goes to the clap detector, and two claps wake him.
        Only asleep (the user, 2026-11-18): awake, a clap is just a clap."""
        if self.clap is None or self.gate is None:
            return False
        if not self.asleep or busy:
            if self._clap_fed:
                self.clap.reset()  # a fresh start next time he sleeps
                self._clap_fed = False
            return False
        if block is None:
            return False
        self._clap_fed = True
        if not self.clap.feed(block):
            return False
        self.gate.wake()
        self.clap.reset()
        self._clap_fed = False
        self._start_turn(self._say_mode_line, ("clap", "(two claps)", TurnTimes(stopped_talking=time.perf_counter())))
        return True

    def _start_turn(self, work: Callable, args: tuple) -> None:
        if self._worker is not None:
            self._worker.join()
        self._cut = False
        if self._barge is not None:
            self._barge.reset()
        self.quiet.clear()  # the answer clears it too; doing it here flips the status at once
        self._turn_started = time.perf_counter()
        self._worker = threading.Thread(target=work, args=args, daemon=True)
        self._worker.start()

    def _finish_answer(self) -> None:
        """Wait for the last answer. If AJ was switched off mid-answer, keep cutting it
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
        heard: list[np.ndarray] = []  # the speech behind `pending`, for "type <words>" (Gemini hears it)
        self._waker, self._last_wake = None, 0.0
        mic_on, was_asleep = False, self.asleep
        try:
            while not self.stop.is_set():
                if getattr(self.source, "done", None) is not None and self.source.done.is_set():
                    return
                self._apply_mode_note()
                if self.asleep != was_asleep:
                    was_asleep = self.asleep
                    if was_asleep:  # just fell asleep: nothing half-heard or armed carries over
                        pending, heard = "", []
                        self._drop_dictation()
                        self._power_at = 0.0  # look at the power cable now
                wanted = self._mic_wanted()
                if not wanted:
                    self._mic_failed = False  # not wanted any more: nothing to report
                waiting = wanted and self._mic_failed and time.perf_counter() < self._mic_retry_at
                if mic_on != wanted and not waiting:
                    mic_on = self._switch_mic(wanted)
                    pending, heard = "", []  # mic off means stop listening now, half-heard sentences included
                    if not mic_on:
                        self._drop_dictation()
                busy = not self.quiet.is_set()
                if self._settle and not busy:
                    self._settle_ears()
                if not busy and not pending and not self._typed.empty():
                    typed, audio = self._typed.get_nowait()
                    self._route(typed, TurnTimes(stopped_talking=time.perf_counter()), audio, typed=True)
                    self._update_status(mic_on)  # Thinking now, not after the next mic block
                    continue
                if self.gate is not None and not busy and not pending and self.gate.idle_due() \
                        and not self._dictating():
                    self.gate.sleep()  # Settings.sleep.idle_mins with nothing answered
                    continue
                block = self._next_block(mic_on, busy)
                if self._clapped(block, busy):  # asleep, two claps: awake, hello, and listening again
                    pending, heard = "", []
                    self._update_status(mic_on)
                    continue
                self._update_status(mic_on)
                confirming = busy and self._answer_to is not None
                if busy and not self.barge_in and self._barge is None and not confirming:
                    continue
                dictating = mic_on and not self.asleep and self._dictating()
                if block is not None:
                    if busy and self.barge_in and self.ears.hearing_speech:
                        self.brain.cancel.set()
                        self.mouth.silence()
                    utterances = self._hear(block)
                    if confirming:
                        if not self._speak_listening():
                            self._answer_by_voice(utterances)
                        continue
                    if self._let_go(utterances):
                        continue
                    if busy and self._barge is not None and not self._cut:
                        if self._barge.feed(block, self.ears.hearing_speech) and not self._speak_listening():
                            self._cut_in()
                        utterances = []  # his own voice coming back: nothing to answer
                    elif not dictating and not self.asleep:  # dictation needs no model, and asleep
                        self._keep_gpu_awake_while_you_talk()  # the room's talk shouldn't keep the GPU up
                    if utterances and self._speak_listening():
                        utterances = []  # AJ (Speak) has his words: never shown, answered or logged here
                        self._cut_start = None
                    for utterance in utterances:
                        if self._cut_start is not None:
                            if time.perf_counter() - self._cut_at < CUT_IN_KEEP_SECS:
                                utterance = np.concatenate([self._cut_start, utterance])
                            self._cut_start = None
                        if dictating:  # words to type: kept as audio, for Gemini to write down
                            self._take_down(utterance)
                            continue
                        stopped = time.perf_counter() - self.s.end_silence_secs
                        text = self.ears.transcribe(utterance)
                        if not text:
                            continue
                        pending = f"{pending} {text}".strip()
                        heard.append(utterance)
                        times = TurnTimes(stopped_talking=stopped, heard=time.perf_counter())
                        # "Hey AJ ... open WhatsApp": after the name alone, a breath for the command.
                        more = looks_unfinished(pending) or (self.gate is not None and self.gate.only_name(pending))
                        deadline = time.perf_counter() + (self.s.unfinished_wait_secs if more else 0.0)
                if dictating and not busy:
                    self._dictation_step()
                if pending and not self.ears.hearing_speech and time.perf_counter() >= deadline:
                    (text, pending), (audio, heard) = (pending, ""), (heard, [])
                    if len(audio) > 1:
                        text = self._hear_whole(audio) or text
                    self._route(text, times, audio)
        finally:
            if self._worker is not None:
                self._finish_answer()
            self.source.close()
