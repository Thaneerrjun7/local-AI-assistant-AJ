"""AJ's engine: load the model, ears and voice, hold the conversation, shut down cleanly.

The window runs it on a background thread (Qt owns the main thread); the console test modes
in __main__ use the same pieces directly. Loading and shutdown are the same code either way,
so what the --wav latency runs measure is what the window runs.

Nothing here imports Qt. The window hears from the engine through an `Events` object whose
methods are called on the engine's threads (the window's version turns each into a Qt signal).

Sleep mode (2026-10-22, core/wake.py): the engine owns the Gate that decides awake or asleep;
the window learns of it through the status (Status.ASLEEP...), so there is one source of truth.
"""
import threading
import time
import wave
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Protocol

import logging
import sys
from datetime import datetime

from aj import memory, persona, tools
from aj.lines import Lines
from aj.config import CLAP_PATH, DATA_DIR, Settings
from aj.core import audio, placement, power_source
from aj.core.audio import Mic, Player
from aj.core.brain import Brain
from aj.core.chatterbox_voice import ChatterboxVoice
from aj.core.chunker import clean_for_speech
from aj.core.clap import ClapDetector, load_template
from aj.core.dictation import Dictation
from aj.core.escalate import Escalator
from aj.core.laptop_state import Monitor
from aj.core.llm import LlamaServer
from aj.core.pipeline import Mouth, Session, Status, TurnTimes, WavMic
from aj.core.speech import Ears, PiperVoice, Voice
from aj.core.thinker import Thinker
from aj.core.wake import Gate, Mode, WakeWords
from aj.tools.dispatch import Dispatcher

log = logging.getLogger(__name__)


LEARN_WHEN_IDLE_SECS = 4.0
VOICE_CACHE = DATA_DIR / "voice_cache"  # pre-rendered lines, so later starts skip rendering them


class MemoryHook:
    """What the brain needs from memory: what AJ knows for this turn, and afterwards the
    turn logged and any new facts learned in the background. Memory trouble never breaks a turn.

    Learning runs the model too (slot 1), so it waits until AJ has been idle for a few
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

    def stop(self) -> None:
        """AJ is switching off: drop the pending learning pass (the turns stay logged)."""
        self._hold_learning()

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
    """What the engine reports while it runs. Called on engine threads, never the UI thread.
    An Events may also have on_working(tool): a tool's name when one starts, "" when it ends
    (the window's Working look); the engine calls it when it's there."""

    def on_status(self, status: Status) -> None: ...

    def on_heard(self, text: str) -> None: ...

    def on_turn(self, text: str, reply: str, times: TurnTimes) -> None: ...


def load_voice(settings: Settings, first: list[str] = ()) -> Voice | None:
    """Load the voice and render what he says first (the greeting). None if it won't start:
    he then answers in writing only, rather than not starting at all. (Kokoro used to be the
    fallback; the user had it removed on 2026-10-21, keeping only the Piper voice.)"""
    engine = settings.voice.engine
    kinds = {"piper": PiperVoice, "chatterbox": ChatterboxVoice, "kokoro": Voice}
    try:
        voice = kinds.get(engine, PiperVoice)(settings.voice, cache_dir=VOICE_CACHE)
    except Exception as e:
        print(f"The {engine} voice didn't start ({e}); answering in writing only.", flush=True)
        return None
    voice.prerender(list(first))
    return voice


def print_turn(text: str, reply: str, times: TurnTimes) -> None:
    """One turn into the log (aj.log under pythonw). A dictation's words never get here: the
    pipeline passes "(dictation, N characters)" for them (the review of 2026-10-22)."""
    print(f"you    > {text}\naj     > {reply}\n         {times.report()}", flush=True)


class Engine:
    """One AJ: model server, ears, voice and the conversation.

    set_listening(), ask(), sleep(), wake() and turn_off() are safe from any thread, before or
    after loading finishes: the mic setting is remembered and applied when the conversation starts."""

    def __init__(self, settings: Settings, *, hear: bool = True, speak: bool = True, barge_in: bool = False,
                 server: LlamaServer | None = None, wake_command: Path | None = None):
        """`server`: one already launched (aj/__main__.py starts it before importing this).
        `wake_command`: the listener started it with what the user said after its name ("Hey AJ,
        open WhatsApp"), a 16 kHz mono WAV: answered first, instead of the greeting."""
        self.settings, self.barge_in = settings, barge_in
        self.stop = threading.Event()
        self.server = server or LlamaServer(placement.start_settings(settings))
        # [placement] mode = "auto": moves the model between the RTX 4050, the 780M and the CPU
        # while he runs (core/placement.py). Started at the end of load().
        self.placer: placement.Placer | None = None
        self.ears: Ears | None = None
        self.brain: Brain | None = None
        self.player: Player | None = None
        self.mouth: Mouth | None = None
        self.session: Session | None = None
        self.thinker: Thinker | None = None
        self.voice: Voice | None = None
        self.state = Monitor()  # what the laptop is doing, refreshed every few seconds while he's on
        # "Write this down" (core/dictation.py): shared by the brain, the pipeline and the dictation
        # tools. Its ears (for when Gemini can't) and the local model (to polish offline) come in load().
        self.dictation = Dictation(settings.dictation)
        self.events: Events | None = None
        self._hear, self._speak = hear, speak
        self._listening = True  # the mic is on when AJ finishes loading
        self.lines = Lines()
        self.greeting = self.lines.pick("greeting") if speak else ""  # only one he'll say counts as said
        self.wake_command = wake_command
        # Sleep mode: awake at start. Asleep it may let go of the model (Settings.sleep.free_gpu);
        # model_ready() brings it back before anything needs it.
        self.gate = Gate(WakeWords(settings.wake_names), idle_mins=settings.sleep.idle_mins,
                         on_change=self._mode_changed)
        self._model_lock = threading.Lock()
        self._model_freed = False
        self.reload_secs: float | None = None  # how long the last reload after sleep took

    def load(self) -> float:
        """Load everything at once: the model (and its warm-up), the ears, the voice and the
        speakers. Returns seconds.

        The warm-up starts as soon as the model is up and the tools are loaded, while the ears
        and the voice are still loading: done after them, it added 2.5 s to every start
        (measured 2026-10-21)."""
        t0 = time.perf_counter()
        self.server.launch()  # usually done already, before the imports
        with ThreadPoolExecutor(5) as pool:
            tools_job = pool.submit(tools.load_all)
            brain_job = pool.submit(self._brain_and_warm_up, tools_job)
            ears_job = pool.submit(Ears, self.settings.voice) if self._hear else None
            voice_job = pool.submit(self._voice_and_speakers) if self._speak else None
            pool.submit(audio.preload)
            brain_job.result()
            self.ears = ears_job.result() if ears_job else None
            self.voice = voice = voice_job.result() if voice_job else None
            print(f"Tools loaded: {', '.join(tools_job.result())}", flush=True)
        self.dictation.ears = self.ears
        self.dictation.start()  # remembers the last window in front that isn't his
        threading.Thread(target=memory.backup, daemon=True, name="memory-backup").start()
        self.brain.delivery.real_sighs = isinstance(voice, ChatterboxVoice)  # it can sigh for real
        if voice is not None:
            # Common phrases ("Opening WhatsApp.", holding lines, goodbyes...) render in the
            # background while idle, so neither loading nor a reply waits on them; after the
            # first run they load from disk in milliseconds.
            apps = sys.modules.get("aj.tools.apps")
            voice.prerender_when_idle([*persona.COMMON_PHRASES, *getattr(apps, "PRERENDER", []),
                                       *self.lines.prerender_list()], busy=self._answering)
            self.mouth = Mouth(voice, self.player)
        self._start_placement()
        return time.perf_counter() - t0

    def _start_placement(self) -> None:
        """[placement] mode = "auto": the watcher that moves the model between units (core/placement.py)."""
        if self.settings.placement.mode != "auto":
            return
        self.placer = placement.Placer(
            self.settings, self.server, open_apps=lambda: [a.name for a in self.state.snapshot().apps],
            idle=lambda: not self._answering(), warm_up=self.brain.warm_up, adopt=self._adopt, stop=self.stop)
        self.state.changed(ai_unit=self.placer.unit.label)
        print(f"(AJ's model is on {self.placer.unit.label}.)", flush=True)
        self.placer.start()

    def _adopt(self, server: LlamaServer, unit: "placement.Unit", why: str) -> None:
        """The model moved (on the placement thread, between turns): everything that talks to it
        talks to the new server from now on; the Placer stops the old one after this."""
        self.server = server
        if self.brain is not None:
            self.brain.base = server.base
            if isinstance(self.brain.memory, MemoryHook):
                self.brain.memory.base = server.base
        self.dictation.llm = (server.base, server.settings)
        if self.thinker is not None:
            self.thinker.place(server.settings.backend, server.settings.device)
        self.state.changed(ai_unit=f"{unit.label} ({why})")
        if self.settings.placement.announce and self.mouth is not None and not self.stop.is_set():
            self.mouth.say(f"Moving to {unit.label.removeprefix('the ')}: {why}.")

    def _wakes_gpu(self) -> bool:
        """Only the RTX 4050's clocks need waking while you talk (core/pipeline.py)."""
        return placement.unit_of(self.server.settings) == placement.RTX

    def _brain_and_warm_up(self, tools_job) -> None:
        """The brain, built as soon as the model server answers; its warm-up goes on in the
        background, through the greeting."""
        self.server.wait_ready()
        tools_job.result()
        # Chatterbox on the GPU and Qwen together fill the 6 GB card (5.8 GB measured), so then
        # there's no thinker: hard questions go to Gemini if there's a key, else Qwen answers.
        v = self.settings.voice
        gpu_full = self._speak and v.engine == "chatterbox" and v.chatterbox_device != "cpu"
        self.thinker = None if gpu_full else Thinker(self.server.settings)  # where the main model is
        self.brain = Brain(self.server.base, self.settings, dispatcher=Dispatcher(confirm=self.confirm),
                           escalator=Escalator(self.thinker), memory=MemoryHook(self.server.base), lines=self.lines,
                           state=self.state, dictation=self.dictation)
        self.brain.on_tool = self._tool_running
        status = sys.modules.get("aj.tools.status")
        if status is not None:
            status.monitor = self.state
        self.dictation.llm = (self.server.base, self.server.settings)
        dictation_tools = sys.modules.get("aj.tools.dictation")
        if dictation_tools is not None:
            dictation_tools.desk = self.dictation
        self.state.start()
        apps = sys.modules.get("aj.tools.apps")
        if apps is not None:
            apps.on_verify = self._launch_checked
        self.brain.warm_up_in_background()

    def _launch_checked(self, name: str, ok: bool) -> None:
        """An app launch was checked (tools/apps.py, a few seconds after "Opening WhatsApp.").
        Live (2026-10-18) WhatsApp never appeared and he never said so; now he does, and he
        knows it next turn. A launch that worked is kept too: asked "did it open?" (live,
        2026-10-23, after Quick Share) he had only "Opening Quick Share." to go on."""
        if ok:
            if self.brain is not None:
                self.brain.record(f"{name} is open: checked a few seconds after launching it.")
            return
        if self.brain is not None:
            self.brain.note(f"{name} didn't open: its window never appeared.")
        if self.mouth is not None and not self.stop.is_set():
            self.mouth.say(f"{name} didn't open.")

    def _voice_and_speakers(self) -> Voice:
        """The voice, the greeting rendered, and the speakers opened: all while the model loads.
        Opened last, after the model's warm-up, the speakers cost 0.9 s of startup (measured)."""
        voice = load_voice(self.settings, [clean_for_speech(self.greeting)])
        if voice is not None:
            self.player = Player(voice.rate)
            self.player.open()
        return voice

    def _tool_running(self, tool: str) -> None:
        """The brain started (a name) or finished ("") a tool: told to the window, if it listens."""
        on_working = getattr(self.events, "on_working", None)
        if on_working is not None:
            on_working(tool)

    def voice_level(self) -> float:
        """How loud AJ's voice is as it's heard right now, 0..1: the speakers play each block
        output_latency after it's sent, so the orb moves with what you hear, not ahead of it."""
        player = self.player
        if player is None:
            return 0.0
        return player.level.at(time.perf_counter() - player.output_latency)

    def mic_level(self) -> float:
        """How loud you are at the mic right now, 0..1 (0 with the mic closed)."""
        level = getattr(getattr(self.session, "source", None), "level", None)
        return level.at(time.perf_counter()) if level is not None else 0.0

    def _answering(self) -> bool:
        return self.session is not None and not self.session.quiet.is_set()

    def greet(self) -> None:
        if self.mouth is not None:
            self.mouth.say(clean_for_speech(self.greeting))
            self.mouth.wait_done()  # don't listen over his own greeting

    def _clap_detector(self) -> ClapDetector | None:
        """Two claps wake him from sleep ([sleep] clap), matched to the user's own recorded clap
        when there is one (scripts/record_clap.py)."""
        if not self.settings.sleep.clap:
            return None
        template = load_template(CLAP_PATH)
        print(f"(Two claps wake him from sleep: {'your recorded clap' if template else 'any clap'}.)", flush=True)
        return ClapDetector(template, strictness=self.settings.sleep.clap_strictness)

    def new_session(self, source=None, on_turn=None, on_status=None, on_heard=None) -> Session:
        session = Session(self.settings, self.ears, self.brain, self.mouth, source, self.stop,
                          barge_in=self.barge_in, on_turn=on_turn, on_status=on_status,
                          on_heard=on_heard, listening=self._listening,
                          interrupt=self.settings.voice.interrupt, dictation=self.dictation,
                          gate=self.gate, on_ac=power_source.on_ac_power, ready=self.model_ready,
                          wakes_gpu=self._wakes_gpu, clap=self._clap_detector())
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
            print(f"AJ is on ({self.load():.1f} s to load).", flush=True)
            # Built before the greeting, because the window's controls come alive with it:
            # anything typed or a mic click during the greeting waits in the session.
            session = self.new_session(Mic(), on_turn=on_turn, on_status=events.on_status,
                                       on_heard=events.on_heard)
            first = self.read_wake_command()
            if first is not None:  # "Hey AJ, open WhatsApp" started it: that's answered, no greeting
                session.ask(*first)
            elif self.mouth is not None and not self.stop.is_set():
                events.on_status(Status.SPEAKING)
                self.greet()
            if not self.stop.is_set():
                session.run()
        finally:
            if self.stop.is_set():
                events.on_status(Status.TURNING_OFF)
            self.shutdown()

    def confirm(self, question: str) -> bool:
        """A CONFIRM tool (delete, move, close an app, shut down) needs the user's yes: a click on
        the window's Yes, or said out loud. Live (2026-10-19) "turn off the laptop" came back
        "no": the box had opened behind other windows (Windows often won't let a background app
        come to the front) and timed out. So he now asks out loud and takes a spoken answer too.
        Without a window or a mic there's nobody to answer, so it's no."""
        ask = getattr(self.events, "ask_confirm", None)
        if ask is None:
            print(f"(needs a yes in the window, so not done) {question}", flush=True)
            return False
        session, answer = self.session, getattr(self.events, "answer_confirm", None)
        if self.mouth is not None:
            self.mouth.say(clean_for_speech(f"{question} Say yes or no."))
        if session is not None and answer is not None:
            session.listen_for_answer(lambda yes: answer(yes, question))
        try:
            return ask(question)
        finally:
            if session is not None:
                session.listen_for_answer(None)

    def set_listening(self, on: bool) -> None:
        self._listening = on
        if self.session is not None:
            self.session.set_listening(on)

    def ask(self, text: str) -> None:
        """Typed text, answered like speech. Ignored while loading (the text box is off then)."""
        if self.session is not None:
            self.session.ask(text)

    # ----- sleep mode -----

    def sleep(self) -> None:
        """Into sleep mode (the tray's Sleep), from any thread."""
        if self.session is not None:
            self.session.sleep()
        else:
            self.gate.sleep()

    def wake(self) -> None:
        """Out of sleep mode (the tray, the shortcut), from any thread."""
        if self.session is not None:
            self.session.wake()
        else:
            self.gate.wake()

    def _mode_changed(self, mode: Mode) -> None:
        """The Gate changed mode (on the conversation's thread). With Settings.sleep.free_gpu the
        model goes while asleep and starts coming back the moment it wakes, while the wake line
        plays; an answer that needs it waits in model_ready()."""
        print(f"({'Asleep' if mode == Mode.ASLEEP else 'Awake'}.)", flush=True)
        if not self.settings.sleep.free_gpu or self.stop.is_set():
            return
        work = self._free_model if mode == Mode.ASLEEP else self.model_ready
        threading.Thread(target=work, daemon=True, name=f"model-{mode}").start()

    def _free_model(self) -> None:
        with self._model_lock:
            if self._model_freed or not self.gate.asleep or self.stop.is_set():
                return
            if self.thinker is not None:
                self.thinker.stop()  # it loads on demand again
            self.server.stop()
            self._model_freed = True
            print("(Asleep: the model is unloaded; the graphics card is free.)", flush=True)

    def model_ready(self) -> None:
        """The model, loaded again if sleep mode let go of it, or started again on the right unit
        if its server died (a GPU that went away under it: core/placement.py). Called before any answer."""
        if self.placer is not None:
            self.placer.recover()
        with self._model_lock:
            if not self._model_freed or self.stop.is_set():
                return
            t0 = time.perf_counter()
            self.server.launch()
            self.server.wait_ready()
            self._model_freed = False
            if self.brain is not None:
                self.brain.warm_up_in_background()  # a request that comes first waits for it
            self.reload_secs = time.perf_counter() - t0
            print(f"(Awake: the model is back, {self.reload_secs:.1f} s.)", flush=True)

    def read_wake_command(self) -> tuple[str, list] | None:
        """What the user said after its name when the listener started it (aj/listener.py):
        (the command, its audio), or None. The recording is deleted once read, whatever happens."""
        path, self.wake_command = self.wake_command, None
        if path is None:
            return None
        try:
            samples = WavMic._load(Path(path))
        except (OSError, EOFError, ValueError, wave.Error) as exc:  # a bad recording costs only the command
            print(f"(couldn't read the wake command {path}: {exc})", flush=True)
            return None
        finally:
            Path(path).unlink(missing_ok=True)
        if self.ears is None or not len(samples):
            return None
        heard = self.ears.transcribe(samples)
        woke, rest = self.gate.words.split(heard)
        command = rest if woke else heard  # the listener heard its name already: keep all the words
        # Its length only: "Hey AJ, type I'll be late" would put a message in the log (2026-10-22).
        print(f"(started by its name, with a command of {len(command.split())} words)", flush=True)
        return (command, [samples]) if command.strip() else None

    def turn_off(self) -> None:
        """Switch off from any thread: stop listening and cut any reply short now; the engine
        thread then says goodbye and frees everything."""
        self.stop.set()
        if self.brain is not None:
            self.brain.cancel.set()
        self.dictation.abort()  # nothing more is typed once he has turned it off (2026-10-22)
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
                self.voice.close()  # the Chatterbox process ends here (and with AJ, regardless)
            self.state.stop()
            self.dictation.stop()
            if isinstance(getattr(self.brain, "memory", None), MemoryHook):
                self.brain.memory.stop()  # a learning pass due now would hit a server going away
            if self.thinker is not None:
                self.thinker.stop()
            self.server.stop()
            print("AJ is off.", flush=True)
