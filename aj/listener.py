"""The wake listener: say "Hey AJ" or "Computer" while AJ is switched off, and AJ starts.

The user, 2026-10-22: "when it is plugged in, even if it's turned off, can I have the mic turned
on at all times listening to the call words of 'Hey AJ' or 'AJ' or 'computer' or 'Hey
computer'? If I say those words, it has to activate the AJ program." And: "When unplugged, it
should not be activated or online. This feature should not be available at all."

So this small process runs in the background (scripts/install_listener.ps1 puts
`pythonw -m aj.listener` in Startup: no console window) and listens only while all of
these hold, looked at every few seconds:
- The laptop is plugged in (core/power_source.py). On battery, or when Windows can't say, the
  microphone is closed completely (no stream open, so Windows shows no mic in use) and the
  speech models are unloaded: the listener is then a ~60 MB process that does nothing.
- AJ isn't running. AJ listens for itself (and wakes itself from sleep mode, core/wake.py), so
  while it's on the listener lets go of the mic and the models, and picks them up again when
  AJ exits. "Running" is the instance mutex the on/off shortcut looks at (core/switch.py).
- It hasn't just started AJ: AJ takes a moment to claim that mutex.

It hears with AJ's own ears (core/speech.py: Silero VAD, then Parakeet on the CPU) and asks
core/wake.py whether each utterance STARTS with a wake word. When one does, it starts AJ the way
the shortcut does (toggle.py); if words followed the name ("Hey AJ, open WhatsApp") the
utterance's audio goes to a temporary 16 kHz mono WAV and AJ gets `--wake-command <that file>`,
so it answers the command once it's up. The WAV holds the whole utterance, name included.

Privacy: ordinary talk in the room is transcribed only to look for the name, then forgotten. It
is never kept or logged. The log (listener.log in config.LOG_DIR) gets the listener starting and
stopping, wake events and errors, never words.

What it costs, measured 2026-10-22 on this laptop (Ryzen 7 7840HS, 16 threads):
- Listening, plugged in: 0.74-0.77 GB of RAM (working set), nearly all of it Parakeet's
  encoder. CPU: 1.6% of one core in silence through this code path (0.1% of the whole CPU);
  4.4% of one core (0.28%) for a live minute with the room mic, room sound included.
- Each utterance: 100-155 ms (190-375 ms of CPU) for 1-2 s of speech. A 7.4 s stretch of
  ordinary talk: 198 ms instead of 464 ms in full, thanks to the head check below.
- On battery or while AJ runs: no stream, models unloaded, 30-70 MB and no CPU to speak of.

Off: `python -m aj.listener --stop`. Out of Startup: `scripts\\install_listener.ps1 -Remove`.
"""
import os

# numpy's OpenBLAS reserves ~480 MB of memory for its threads as soon as numpy is imported, and the
# listener never multiplies a matrix: with one thread the reservation is ~16 MB (measured
# 2026-10-22). It has to be set before anything imports numpy, and AJ doesn't inherit it.
_SET_BLAS = "OPENBLAS_NUM_THREADS" not in os.environ
os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")

import argparse  # noqa: E402
import ctypes  # noqa: E402
import logging  # noqa: E402
import queue  # noqa: E402
import sys  # noqa: E402
import tempfile  # noqa: E402
import threading  # noqa: E402
import time  # noqa: E402
import wave  # noqa: E402
from collections.abc import Callable  # noqa: E402
from ctypes import wintypes  # noqa: E402
from dataclasses import replace  # noqa: E402
from pathlib import Path  # noqa: E402

import numpy as np  # noqa: E402

from aj.config import LOG_DIR, VoiceSettings, load_settings  # noqa: E402
from aj import toggle  # noqa: E402  (AJ starts the way the shortcut starts it, at normal priority)
from aj.core import migrate, switch  # noqa: E402
from aj.core.audio import MIC_RATE, Mic  # noqa: E402
from aj.core.power_source import on_ac_power  # noqa: E402
from aj.core import mic_share  # noqa: E402
from aj.core.wake import split_wake  # noqa: E402

log = logging.getLogger("aj.listener")  # a fixed name: run with -m, __name__ is "__main__"

CHECK_SECS = 3.0   # how often power and AJ are looked at (each look is a kernel call: microseconds)
# Parakeet's threads here. On 1-2 s utterances 2 threads answered as fast as AJ's 4 (106-136 ms
# against 91-131 ms) for half the CPU (219-344 ms against 391-828 ms), measured 2026-10-22.
STT_THREADS = 2
# A wake word has to START the utterance, so a long one is first transcribed from its first
# 3 s alone, and only in full if those start with the name: talk in the room costs a fraction.
HEAD_SECS = 3.0
HOLD_SECS = 20.0   # after starting AJ the mic stays shut this long while AJ comes up (it claims its mutex in ~1 s)
TRY_HOLD_SECS = 2.0  # the same with --no-start, where nothing comes up
STALL_SECS = 5.0   # an open mic that sends nothing this long is reopened (after sleep, a headset pulled out)
RETRY_SECS = 30.0  # after the mic or the models failed to open
WAKE_PREFIX = "aj-wake-"   # the command recordings handed to AJ, in %TEMP%
WAKE_FILE_MAX_SECS = 600   # older ones are deleted: AJ reads its recording within seconds of starting

# ---- one listener at a time, and the off signal (named kernel objects, as core/switch.py) ----

_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
_kernel32.CreateMutexW.restype = wintypes.HANDLE
_kernel32.OpenMutexW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
_kernel32.OpenMutexW.restype = wintypes.HANDLE
_kernel32.CreateEventW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR]
_kernel32.CreateEventW.restype = wintypes.HANDLE
_kernel32.OpenEventW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
_kernel32.OpenEventW.restype = wintypes.HANDLE
_kernel32.SetEvent.argtypes = [wintypes.HANDLE]
_kernel32.ResetEvent.argtypes = [wintypes.HANDLE]
_kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
_kernel32.WaitForSingleObject.restype = wintypes.DWORD
_kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
_kernel32.GetCurrentProcess.restype = wintypes.HANDLE
_kernel32.SetPriorityClass.argtypes = [wintypes.HANDLE, wintypes.DWORD]
_kernel32.SetPriorityClass.restype = wintypes.BOOL

_MUTEX_NAME = "Local\\AJ.Listener"
_STOP_NAME = "Local\\AJ.Listener.Stop"
_ERROR_ALREADY_EXISTS = 183
_SYNCHRONIZE = 0x00100000
_EVENT_MODIFY_STATE = 0x0002
_WAIT_OBJECT_0 = 0
_BELOW_NORMAL_PRIORITY_CLASS = 0x00004000
_held = []  # keep handles alive for the life of the process


def claim_instance(name: str = _MUTEX_NAME) -> bool:
    """Call once at startup. False means a listener is already running."""
    handle = _kernel32.CreateMutexW(None, False, name)
    if ctypes.get_last_error() == _ERROR_ALREADY_EXISTS:
        _kernel32.CloseHandle(handle)
        return False
    _held.append(handle)
    return True


def is_listener_running(name: str = _MUTEX_NAME) -> bool:
    handle = _kernel32.OpenMutexW(_SYNCHRONIZE, False, name)
    if handle:
        _kernel32.CloseHandle(handle)
        return True
    return False


def request_stop(wait_secs: float = 5.0, name: str = _STOP_NAME, mutex: str = _MUTEX_NAME) -> bool:
    """Ask the running listener to stop, and wait (up to `wait_secs`) until it has. False if none
    was running."""
    handle = _kernel32.OpenEventW(_EVENT_MODIFY_STATE, False, name)
    if not handle:
        return False
    _kernel32.SetEvent(handle)
    _kernel32.CloseHandle(handle)
    deadline = time.monotonic() + wait_secs
    while is_listener_running(mutex) and time.monotonic() < deadline:
        time.sleep(0.1)
    return True


def watch_for_stop(stop: threading.Event, name: str = _STOP_NAME) -> None:
    """Start a thread that sets `stop` when someone runs `--stop`."""
    event = _kernel32.CreateEventW(None, True, False, name)
    _kernel32.ResetEvent(event)
    _held.append(event)

    def wait() -> None:
        while not stop.is_set():
            if _kernel32.WaitForSingleObject(event, 250) == _WAIT_OBJECT_0:
                stop.set()

    threading.Thread(target=wait, daemon=True, name="listener-off").start()


# ---- starting AJ and handing over the command ----

def start_aj(wake_wav: Path | None = None) -> bool:
    """Start AJ as the on/off shortcut does (toggle.py): the venv's pythonw, detached, so AJ
    lives on whatever happens to the listener. With a command, AJ gets the recording to answer
    once it's up. True: AJ is on its way."""
    env = dict(os.environ)
    if _SET_BLAS:
        env.pop("OPENBLAS_NUM_THREADS", None)  # the listener's own setting, not AJ's
    toggle.start(["--wake-command", str(wake_wav)] if wake_wav is not None else [], env=env)
    return True


def save_wake_wav(samples: np.ndarray, folder: Path | None = None) -> Path:
    """The utterance as a 16 kHz mono 16-bit WAV in a new temporary file (AJ's --wav reader
    takes the same format). Old recordings in the folder are deleted first."""
    folder = Path(folder or tempfile.gettempdir())
    sweep_wake_files(folder)
    fd, name = tempfile.mkstemp(prefix=WAKE_PREFIX, suffix=".wav", dir=folder)
    pcm = (np.clip(samples, -1.0, 1.0) * 32767).astype(np.int16)
    with os.fdopen(fd, "wb") as fh, wave.open(fh, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(MIC_RATE)
        w.writeframes(pcm.tobytes())
    return Path(name)


def sweep_wake_files(folder: Path | None = None, max_age: float = WAKE_FILE_MAX_SECS) -> None:
    """Delete command recordings older than `max_age` seconds: AJ normally deletes its own, but
    one it never read (AJ failed to start) shouldn't lie around in %TEMP%."""
    cutoff = time.time() - max_age
    for path in Path(folder or tempfile.gettempdir()).glob(f"{WAKE_PREFIX}*.wav"):
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink()
        except OSError:
            pass  # in use or already gone: the next sweep tries again


def load_ears(voice: VoiceSettings):
    """AJ's ears (Silero VAD + Parakeet) with the listener's thread count. Imported here, not at
    the top, so on battery sherpa-onnx is never even loaded. The files are checked first:
    sherpa-onnx ends the whole process on a missing model instead of raising."""
    stt = Path(voice.stt_dir)
    for path in (Path(voice.vad_model), stt / "encoder.int8.onnx", stt / "decoder.int8.onnx",
                 stt / "joiner.int8.onnx", stt / "tokens.txt"):
        if not path.exists():
            raise FileNotFoundError(f"missing speech model {path} (run scripts\\fetch_speech.ps1)")
    from aj.core.speech import Ears
    return Ears(replace(voice, stt_threads=STT_THREADS))


# ---- the listener ----

class Listener:
    """Decides when to listen and what to do with what it hears. One thread runs it (run());
    everything it uses is passed in, so the tests drive it with fakes and no real mic."""

    def __init__(self, voice: VoiceSettings | None = None, stop: threading.Event | None = None, *,
                 plugged_in: Callable[[], bool | None] = on_ac_power,
                 aj_running: Callable[[], bool] = switch.is_running, mic=None,
                 ears: Callable[[VoiceSettings], object] = load_ears,
                 split: Callable[[str], tuple[bool, str]] = split_wake,
                 start: Callable[[Path | None], bool] = start_aj,
                 wake_dir: Path | None = None, clock: Callable[[], float] = time.monotonic,
                 speak_listening: Callable[[], bool] = mic_share.speak_listening):
        self.voice = voice or VoiceSettings()
        self.stop = stop or threading.Event()
        self._plugged_in, self._aj_running = plugged_in, aj_running
        self.mic = mic if mic is not None else Mic()
        self._make_ears, self._split, self._start = ears, split, start
        self.wake_dir, self.clock = wake_dir, clock
        self._speak_listening = speak_listening  # AJ (Speak) has his words: nothing here is a wake word
        self.ears = None              # loaded only while listening
        self.why_not: str | None = "not started"  # why the mic is shut, None while listening
        self._next_check = 0.0
        self._hold_until = 0.0        # the mic stays shut until then: AJ is coming up
        self._aj_due = False          # AJ was started and hasn't claimed its mutex yet
        self._last_sound = 0.0
        self._retry_at = 0.0
        self._last_error = ""

    # -- when to listen --

    def reason_not_to_listen(self) -> str | None:
        """Why it mustn't listen right now, or None when it may."""
        running = self._aj_running()
        if running:
            self._aj_due = False
        if self.clock() < self._hold_until:
            return "starting AJ"
        if self._aj_due:  # the hold is over and AJ never claimed its mutex
            self._aj_due = False
            self._error(f"AJ didn't come on within {HOLD_SECS:.0f} s of the wake word (see aj.log)")
        if self._plugged_in() is not True:
            return "on battery"  # or Windows can't say: the safe side is not listening
        if running:
            return "AJ is on"
        return None

    def check(self) -> None:
        """Open or close the mic to match power, AJ and any start in progress."""
        self.why_not = self.reason_not_to_listen()
        if self.why_not is not None:
            if self.mic.is_open or self.ears is not None:
                self._let_go()
        elif not self.mic.is_open and self.clock() >= self._retry_at:
            self._listen()

    def _listen(self) -> None:
        try:
            if self.ears is None:
                self.ears = self._make_ears(self.voice)
            self.mic.open()
        except Exception as exc:  # no microphone, a model missing, the device taken: try again later
            self._let_go()
            self._retry_at = self.clock() + RETRY_SECS
            self.why_not = "can't listen"
            self._error(f"can't listen: {exc}")
            return
        self._last_sound = self.clock()

    def _let_go(self) -> None:
        """Close the mic (no stream left open) and unload the models (~0.7 GB)."""
        self._close_mic()
        self.ears = None

    def _close_mic(self) -> None:
        try:
            self.mic.close()
        except Exception as exc:  # a device already gone: there is nothing left to close
            self._error(f"closing the microphone: {exc}")

    # -- hearing --

    def step(self) -> None:
        """One turn of the loop: look at power and AJ when due, then hear (or wait)."""
        if self.clock() >= self._next_check:
            self._next_check = self.clock() + CHECK_SECS
            self.check()
        if self.mic.is_open:
            self.hear()
        else:
            self.stop.wait(max(0.0, self._next_check - self.clock()))

    def hear(self) -> None:
        """Feed what the mic has sent (waiting up to a quarter second for it) to the VAD and look
        for the name in each utterance that just ended."""
        try:
            blocks = [self.mic.blocks.get(timeout=0.25)]
        except queue.Empty:
            if self.clock() - self._last_sound > STALL_SECS:
                self._error("the microphone stopped sending sound; reopening it")
                self._close_mic()  # the next check opens it again; the models stay loaded
                self.ears.reset()
            return
        while True:
            try:
                blocks.append(self.mic.blocks.get_nowait())
            except queue.Empty:
                break
        self._last_sound = self.clock()
        self._last_error = ""  # sound is coming in: a later failure is news again
        try:
            utterances = self.ears.feed(blocks[0] if len(blocks) == 1 else np.concatenate(blocks))
        except (ValueError, RuntimeError) as exc:  # as in pipeline.py: a VAD error costs what was being heard
            self._error(f"listening hiccup, starting the ears afresh: {exc}")
            self.ears.reset()
            return
        if utterances and self._speak_listening():
            return  # said to AJ (Speak), to be typed (2026-10-22): never looked at for the name
        for utterance in utterances:
            if self.on_utterance(utterance):
                break  # AJ is starting: whatever else was said is for AJ to hear

    def on_utterance(self, samples: np.ndarray) -> bool:
        """True when it started AJ. Anything else is ordinary talk: dropped here, never logged."""
        woken, command = self.heard_wake(samples)
        return self.wake(samples, command) if woken else False

    def heard_wake(self, samples: np.ndarray) -> tuple[bool, str]:
        """(did the utterance start with a wake word, the command after it)."""
        head = int(HEAD_SECS * MIC_RATE)
        if len(samples) > head + MIC_RATE // 2:
            woken, _ = self._split(self.ears.transcribe(samples[:head]))
            if not woken:
                return False, ""
        woken, command = self._split(self.ears.transcribe(samples))
        return (True, command) if woken else (False, "")

    def wake(self, samples: np.ndarray, command: str) -> bool:
        """Start AJ, with the recording when words followed the name. Once per wake: the mic shuts
        until AJ is up (or the hold runs out)."""
        if self.clock() < self._hold_until or self._aj_running():
            return False  # already starting, or AJ is on and hears for itself
        path = None
        if command.strip():
            try:
                path = save_wake_wav(samples, self.wake_dir)
            except OSError as exc:
                self._error(f"couldn't save the command for AJ, starting AJ without it: {exc}")
        self._let_go()  # AJ takes the mic, and its own models need the room
        self.why_not = "starting AJ"
        try:
            coming = self._start(path)
        except OSError as exc:
            self._error(f"couldn't start AJ: {exc}")
            if path is not None:
                path.unlink(missing_ok=True)
            self._hold_until = self.clock() + TRY_HOLD_SECS
            return True
        self._hold_until = self.clock() + (HOLD_SECS if coming else TRY_HOLD_SECS)
        self._aj_due = bool(coming)
        log.info("wake word heard%s: %s", " with a command" if path is not None else "",
                 "starting AJ" if coming else "AJ not started (--no-start)")
        return True

    def _error(self, message: str) -> None:
        if message != self._last_error:  # a failure that repeats every few seconds is logged once
            self._last_error = message
            log.error(message)

    def run(self) -> None:
        log.info("wake listener on")
        try:
            while not self.stop.is_set():
                try:
                    self.step()
                except Exception:  # never die quietly in the background: log it, rest, carry on
                    log.exception("wake listener error")
                    self._let_go()
                    self.stop.wait(RETRY_SECS)
        finally:
            self._let_go()
            log.info("wake listener off")


def _log_to_file() -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    handlers: list[logging.Handler] = [logging.FileHandler(LOG_DIR / "listener.log", encoding="utf-8")]
    if sys.stderr is not None:  # run from a console: show it there too
        handlers.append(logging.StreamHandler())
    logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(levelname)s %(message)s",
                        handlers=handlers)
    log.setLevel(logging.INFO)


def _try_start(path: Path | None) -> bool:
    """--no-start: start nothing (the wake is still logged) and delete the recording."""
    if path is not None:
        path.unlink(missing_ok=True)
    return False


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="aj.listener",
                                 description="Start AJ when you say Hey AJ or Computer (only while plugged in).")
    ap.add_argument("--stop", action="store_true", help="stop the running wake listener")
    ap.add_argument("--no-start", action="store_true", help="testing: log wake words but never start AJ")
    args = ap.parse_args(argv)
    if args.stop:
        print("Wake listener stopped." if request_stop() else "The wake listener isn't running.")
        return 0
    moved = migrate.run()  # before the log opens in the data folder an old install may still have to move
    _log_to_file()
    for note in moved:
        log.info("rename to AJ: %s", note)
    if not claim_instance():
        print("The wake listener is already running.")
        return 1
    stop = threading.Event()
    watch_for_stop(stop)
    # Below normal: a game or a build always comes first; the listener only needs a few
    # milliseconds every 32 ms block, and a wake word is caught a moment later at worst.
    _kernel32.SetPriorityClass(_kernel32.GetCurrentProcess(), _BELOW_NORMAL_PRIORITY_CLASS)
    sweep_wake_files()
    listener = Listener(load_settings().voice, stop, start=_try_start if args.no_start else start_aj)
    try:
        listener.run()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
