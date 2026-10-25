"""Turn AJ on: `python -m aj` opens his window. The AJ shortcut runs aj.toggle,
which does the same, or brings the window to the front if he's already on.
Closing the window turns him off. Off means fully exited: nothing left in RAM or VRAM.

Console test modes (no window; every turn prints its latency). Any of these flags, alone or
combined, keeps the window closed, exactly as these modes ran before there was one:
  --text           type to AJ instead of talking
  --wav a.wav ...  feed recordings as if spoken
  --mute           no voice, text replies only
  --headphones     allow interrupting AJ by talking over him
  --headless       just talk through the mic, console only

Started by the wake listener (aj/listener.py) after "Hey AJ, <command>":
  --wake-command a.wav   answer what was said after the name first, instead of the greeting.
                         Not a console mode: the window and the tray open as usual. The file is
                         deleted once read.
"""
import argparse
import os
import sys
import time
from pathlib import Path

from aj.config import LOG_DIR, load_settings
from aj.core import migrate, switch
from aj.core.llm import LlamaServer
# The rest of AJ (the speech libraries above all) is imported in main(), after the model
# server has been started, so the model loads while Python imports (2026-10-21).


def chat_by_text(engine) -> None:
    """--text: each line you type is one turn, timed from the moment you pressed Enter. Lines go
    through sleep mode as speech does (core/wake.py), so "go to sleep", ignored talk and "Hey AJ,
    ..." can all be tried from the console."""
    from aj.core.pipeline import TurnTimes, is_off_command, mode_line
    from aj.core.wake import Heard
    brain, mouth, player = engine.brain, engine.mouth, engine.player
    while not engine.stop.is_set():
        line = input("you    > ").strip()
        if not line:
            continue
        heard = engine.gate.route(line)
        if heard.action == Heard.IGNORE:
            print("         (asleep: only its name wakes it)", flush=True)
            continue
        if heard.action in (Heard.WAKE, Heard.SLEEP):
            said = mode_line(engine.lines, heard.action)
            if mouth is not None:
                mouth.say(said)
                mouth.wait_done()
            print(f"aj     > {said}  ({engine.gate.mode})", flush=True)
            continue
        if is_off_command(heard.text):
            break
        engine.model_ready()  # back from sleep with Settings.sleep.free_gpu: the model first
        times = TurnTimes(stopped_talking=time.perf_counter())
        if player is not None:
            player.new_turn()
        reply = brain.reply(heard.text, mouth, times)
        if mouth is not None:
            mouth.wait_done()
            times.first_sound = player.first_sound_at or 0.0
        print(f"aj     > {reply}\n         {times.report()}", flush=True)


def run_console(args: argparse.Namespace, engine) -> int:
    """The test modes: the same engine the window runs, driven from the console."""
    from aj.core.audio import Mic
    from aj.core.pipeline import TurnTimes, WavMic
    from aj.engine import print_turn
    try:
        print(f"AJ is on ({engine.load():.1f} s to load).", flush=True)
        engine.greet()
        if args.text:
            chat_by_text(engine)
        else:
            session = engine.new_session(on_turn=print_turn)
            session.source = WavMic(args.wav, session.quiet) if args.wav else Mic()
            session.run()
    except (KeyboardInterrupt, EOFError):
        pass
    finally:
        engine.shutdown()
    return 0


def parse_args(argv: list[str] | None = None) -> tuple[argparse.Namespace, bool]:
    """(the arguments, whether it's a console test mode). --wake-command is not one: started by
    the listener, AJ opens his window and tray as usual."""
    ap = argparse.ArgumentParser(prog="aj")
    ap.add_argument("--text", action="store_true")
    ap.add_argument("--wav", nargs="+", type=Path)
    ap.add_argument("--mute", action="store_true")
    ap.add_argument("--headphones", action="store_true")
    ap.add_argument("--headless", action="store_true")
    ap.add_argument("--wake-command", type=Path, help=argparse.SUPPRESS)  # from aj/listener.py
    args = ap.parse_args(argv)
    return args, bool(args.text or args.wav or args.mute or args.headphones or args.headless)


def main() -> int:
    args, console = parse_args()

    # Before the log opens: it lives in the data folder an old install may still have to move.
    moved = migrate.run()
    if sys.stdout is None:  # started by the shortcut (pythonw): no console, so log to a file
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        sys.stdout = sys.stderr = open(LOG_DIR / "aj.log", "a", encoding="utf-8", buffering=1)
    for note in moved:
        print(f"Rename to AJ: {note}", flush=True)
    if not switch.claim_instance():
        if not console:
            switch.request_show()  # he's already on: show his window (and wake him) rather than start a second
        if args.wake_command is not None:
            args.wake_command.unlink(missing_ok=True)  # the listener's recording: nobody will read it now
        print("AJ is already on.")
        return 1

    settings = load_settings()
    server = LlamaServer(settings.llm)
    server.launch()
    from aj.engine import Engine
    engine = Engine(settings, hear=not args.text, speak=not args.mute, barge_in=args.headphones, server=server,
                    wake_command=args.wake_command)
    switch.watch_for_off(engine.stop)
    if console:
        return run_console(args, engine)
    from aj.ui.app import run_window  # Qt is loaded only when there's a window to show
    code = run_window(engine)
    # The window closes only after the engine has let go of the mic, the speakers and
    # llama-server. Skip Python's own teardown from there: exit-time destructors in the GPU
    # speech libraries can stall it, and a closed window must mean a finished process.
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(code)


if __name__ == "__main__":
    sys.exit(main())
