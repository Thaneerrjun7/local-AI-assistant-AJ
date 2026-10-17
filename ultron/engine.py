"""Ultron's engine: load the model, ears and voice, hold the conversation, shut down cleanly.

The window runs it on a background thread (Qt owns the main thread); the console test modes
in __main__ use the same pieces directly. Loading and shutdown are the same code either way,
so what the --wav latency runs measure is what the window runs.

Nothing here imports Qt. The window hears from the engine through an `Events` object whose
methods are called on the engine's threads (the window's version turns each into a Qt signal).
"""
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Protocol

import logging
import sys
from datetime import datetime

from ultron import memory, persona, tools
from ultron.lines import Lines
from ultron.config import DATA_DIR, Settings
from ultron.core.audio import Mic, Player
from ultron.core.brain import Brain
from ultron.core.chatterbox_voice import ChatterboxVoice
from ultron.core.chunker import clean_for_speech
from ultron.core.escalate import Escalator
from ultron.core.llm import LlamaServer
from ultron.core.pipeline import Mouth, Session, Status, TurnTimes
from ultron.core.speech import Ears, Voice
from ultron.core.thinker import Thinker
from ultron.tools.dispatch import Dispatcher

log = logging.getLogger(__name__)


LEARN_WHEN_IDLE_SECS = 4.0
VOICE_CACHE = DATA_DIR / "voice_cache"  # pre-rendered lines, so later starts skip rendering them


class MemoryHook:
    """What the brain needs from memory: what Ultron knows for this turn, and afterwards the
    turn logged and any new facts learned in the background. Memory trouble never breaks a turn.

    Learning runs the model too (slot 1), so it waits until Ultron has been idle for a few
    seconds: run straight after a reply, it competed with the next reply for the GPU and slowed
    it (measured live). A new turn starting cancels the wait; the turns are learned together later."""

    def __init__(self, base: str):
        self.base = base
        self.session_id = datetime.now().strftime("%Y%m%d-%H%M%S")
        self._pending: list[dict] = []
        self._timer: threading.Timer | None = None
        self._lock = threading.Lock()

    def context_for(self, user_text: str) -> str:
        self._hold_learning()  # a turn is starting: not now
        try:
            return memory.context_for(user_text)
        except Exception:
            log.exception("memory recall failed")
            return ""

    def after_turn(self, user_text: str, reply: str) -> None:
        try:
            memory.log_turn(self.session_id, "user", user_text)
            memory.log_turn(self.session_id, "assistant", reply)
        except Exception:
            log.exception("memory log failed")
        with self._lock:
            self._pending += [{"role": "user", "content": user_text}, {"role": "assistant", "content": reply}]
            self._timer = threading.Timer(LEARN_WHEN_IDLE_SECS, self._learn)
            self._timer.daemon = True
            self._timer.start()

    def _hold_learning(self) -> None:
        with self._lock:
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None

    def _learn(self) -> None:
        with self._lock:
            turns, self._pending, self._timer = self._pending, [], None
        if turns:
            try:
                memory.learn_in_background(self.base, turns)
            except Exception:
                log.exception("memory learning failed")


class Events(Protocol):
    """What the engine reports while it runs. Called on engine threads, never the UI thread."""

    def on_status(self, status: Status) -> None: ...

    def on_heard(self, text: str) -> None: ...

    def on_turn(self, text: str, reply: str, times: TurnTimes) -> None: ...


def load_voice(settings: Settings, first: list[str] = ()) -> Voice:
    """Load the voice and render what he says first (the greeting). Chatterbox, in the user's
    own voice, unless it can't start: then Kokoro, so he always has a voice."""
    voice = None
    if settings.voice.engine == "chatterbox":
        try:
            voice = ChatterboxVoice(settings.voice, cache_dir=VOICE_CACHE)
        except Exception as e:
            print(f"The Chatterbox voice didn't start ({e}); using Kokoro instead.", flush=True)
    if voice is None:
        voice = Voice(settings.voice, cache_dir=VOICE_CACHE)
    voice.prerender(list(first))
    return voice


def print_turn(text: str, reply: str, times: TurnTimes) -> None:
    print(f"you    > {text}\nultron > {reply}\n         {times.report()}", flush=True)


class Engine:
    """One Ultron: model server, ears, voice and the conversation.

    set_listening(), ask() and turn_off() are safe from any thread, before or after loading
    finishes: the mic setting is remembered and applied when the conversation starts."""

    def __init__(self, settings: Settings, *, hear: bool = True, speak: bool = True, barge_in: bool = False):
        self.settings, self.barge_in = settings, barge_in
        self.stop = threading.Event()
        self.server = LlamaServer(settings.llm)
        self.ears: Ears | None = None
        self.brain: Brain | None = None
        self.player: Player | None = None
        self.mouth: Mouth | None = None
        self.session: Session | None = None
        self.thinker: Thinker | None = None
        self.voice: Voice | None = None
        self.events: Events | None = None
        self._hear, self._speak = hear, speak
        self._listening = True  # the mic is on when Ultron finishes loading
        self.lines = Lines()
        self.greeting = self.lines.pick("greeting") if speak else ""  # only one he'll say counts as said

    def load(self) -> float:
        """Load the model, ears and voice in parallel, then warm the model up. Returns seconds."""
        t0 = time.perf_counter()
        with ThreadPoolExecutor(3) as pool:
            server_job = pool.submit(self.server.start)
            ears_job = pool.submit(Ears, self.settings.voice) if self._hear else None
            voice_job = pool.submit(load_voice, self.settings, [clean_for_speech(self.greeting)]) \
                if self._speak else None
            tools_job = pool.submit(tools.load_all)
            server_job.result()
            self.ears = ears_job.result() if ears_job else None
            self.voice = voice = voice_job.result() if voice_job else None
            print(f"Tools loaded: {', '.join(tools_job.result())}", flush=True)
        threading.Thread(target=memory.backup, daemon=True, name="memory-backup").start()
        # Chatterbox and Qwen together fill the 6 GB graphics card (5.8 GB measured), so with that
        # voice there's no thinker: hard questions go to Gemini if there's a key, else Qwen answers.
        chatterbox = isinstance(voice, ChatterboxVoice)
        self.thinker = None if chatterbox else Thinker(self.settings.llm)
        self.brain = Brain(self.server.base, self.settings, dispatcher=Dispatcher(confirm=self.confirm),
                           escalator=Escalator(self.thinker), memory=MemoryHook(self.server.base), lines=self.lines,
                           real_sighs=chatterbox)
        self.brain.warm_up()
        if voice is not None:
            # His common phrases ("Opening WhatsApp.", holding lines, goodbyes...) render in the
            # background while he's idle, so neither loading nor a reply waits on them; after
            # the first run they load from disk in milliseconds.
            apps = sys.modules.get("ultron.tools.apps")
            voice.prerender_when_idle([*persona.COMMON_PHRASES, *getattr(apps, "PRERENDER", []),
                                       *self.lines.prerender_list()], busy=self._answering)
            self.player = Player(voice.rate)
            self.player.open()
            self.mouth = Mouth(voice, self.player)
        return time.perf_counter() - t0

    def _answering(self) -> bool:
        return self.session is not None and not self.session.quiet.is_set()

    def greet(self) -> None:
        if self.mouth is not None:
            self.mouth.say(clean_for_speech(self.greeting))
            self.mouth.wait_done()  # don't listen over his own greeting

    def new_session(self, source=None, on_turn=None, on_status=None, on_heard=None) -> Session:
        session = Session(self.settings, self.ears, self.brain, self.mouth, source, self.stop,
                          barge_in=self.barge_in, on_turn=on_turn, on_status=on_status,
                          on_heard=on_heard, listening=self._listening)
        self.session = session
        session.set_listening(self._listening)  # in case the mic button moved while it was built
        return session

    def run(self, events: Events) -> None:
        """The window's engine thread, start to finish: load, greet, talk until switched off,
        then shut down. Errors propagate (after shutdown) for the caller to report."""
        def on_turn(text: str, reply: str, times: TurnTimes) -> None:
            print_turn(text, reply, times)  # the log keeps every turn's latency
            events.on_turn(text, reply, times)

        self.events = events
        events.on_status(Status.LOADING)
        try:
            print(f"Ultron is on ({self.load():.1f} s to load).", flush=True)
            # Built before the greeting, because the window's controls come alive with it:
            # anything typed or a mic click during the greeting waits in the session.
            session = self.new_session(Mic(), on_turn=on_turn, on_status=events.on_status,
                                       on_heard=events.on_heard)
            if self.mouth is not None and not self.stop.is_set():
                events.on_status(Status.SPEAKING)
                self.greet()
            if not self.stop.is_set():
                session.run()
        finally:
            if self.stop.is_set():
                events.on_status(Status.TURNING_OFF)
            self.shutdown()

    def confirm(self, question: str) -> bool:
        """A CONFIRM tool (delete, move, close an app, forget) needs the user's yes. The window
        shows a Yes/No box; without a window there's nobody to click, so the answer is no."""
        ask = getattr(self.events, "ask_confirm", None)
        if ask is None:
            print(f"(needs a yes in the window, so not done) {question}", flush=True)
            return False
        if self.mouth is not None:
            self.mouth.say("I need a yes in the window for that.")
        return ask(question)

    def set_listening(self, on: bool) -> None:
        self._listening = on
        if self.session is not None:
            self.session.set_listening(on)

    def ask(self, text: str) -> None:
        """Typed text, answered like speech. Ignored while loading (the text box is off then)."""
        if self.session is not None:
            self.session.ask(text)

    def turn_off(self) -> None:
        """Switch off from any thread: stop listening and cut any reply short now; the engine
        thread then says goodbye and frees everything."""
        self.stop.set()
        if self.brain is not None:
            self.brain.cancel.set()
        if self.mouth is not None:
            self.mouth.silence()

    def shutdown(self) -> None:
        """Say goodbye if switched off, then release the speakers and stop the model server."""
        try:
            if self.mouth is not None and self.stop.is_set():  # switched off: goodbye before going dark
                self.mouth.say(clean_for_speech(self.lines.pick("goodbye")))
                self.mouth.wait_done()
            self.lines.save()
        finally:
            if self.player is not None:
                self.player.close()
            if self.voice is not None:
                self.voice.close()  # the Chatterbox process ends here (and with Ultron, regardless)
            if self.thinker is not None:
                self.thinker.stop()
            self.server.stop()
            print("Ultron is off.", flush=True)
