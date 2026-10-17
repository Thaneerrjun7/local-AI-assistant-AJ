"""Turn Ultron on: `python -m ultron` opens his window. The Ultron shortcut runs ultron.toggle,
which does the same, or brings the window to the front if he's already on.
Closing the window turns him off. Off means fully exited: nothing left in RAM or VRAM.

Console test modes (no window; every turn prints its latency). Any of these flags, alone or
combined, keeps the window closed, exactly as these modes ran before there was one:
  --text           type to Ultron instead of talking
  --wav a.wav ...  feed recordings as if spoken
  --mute           no voice, text replies only
  --headphones     allow interrupting Ultron by talking over him
  --headless       just talk through the mic, console only
"""
import argparse
import os
import sys
import time
from pathlib import Path

from ultron.config import LOG_DIR, load_settings
from ultron.core import switch
from ultron.core.audio import Mic
from ultron.core.pipeline import TurnTimes, WavMic
from ultron.engine import Engine, print_turn


def chat_by_text(engine: Engine) -> None:
    """--text: each line you type is one turn, timed from the moment you pressed Enter."""
    brain, mouth, player = engine.brain, engine.mouth, engine.player
    while not engine.stop.is_set():
        line = input("you    > ").strip()
        if not line:
            continue
        times = TurnTimes(stopped_talking=time.perf_counter())
        if player is not None:
            player.new_turn()
        reply = brain.reply(line, mouth, times)
        if mouth is not None:
            mouth.wait_done()
            times.first_sound = player.first_sound_at or 0.0
        print(f"ultron > {reply}\n         {times.report()}", flush=True)


def run_console(args: argparse.Namespace, engine: Engine) -> int:
    """The test modes: the same engine the window runs, driven from the console."""
    try:
        print(f"Ultron is on ({engine.load():.1f} s to load).", flush=True)
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


def main() -> int:
    ap = argparse.ArgumentParser(prog="ultron")
    ap.add_argument("--text", action="store_true")
    ap.add_argument("--wav", nargs="+", type=Path)
    ap.add_argument("--mute", action="store_true")
    ap.add_argument("--headphones", action="store_true")
    ap.add_argument("--headless", action="store_true")
    args = ap.parse_args()
    console = bool(args.text or args.wav or args.mute or args.headphones or args.headless)

    if sys.stdout is None:  # started by the shortcut (pythonw): no console, so log to a file
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        sys.stdout = sys.stderr = open(LOG_DIR / "ultron.log", "a", encoding="utf-8", buffering=1)
    if not switch.claim_instance():
        if not console:
            switch.request_show()  # he's already on: show his window rather than start a second Ultron
        print("Ultron is already on.")
        return 1

    engine = Engine(load_settings(), hear=not args.text, speak=not args.mute, barge_in=args.headphones)
    switch.watch_for_off(engine.stop)
    if console:
        return run_console(args, engine)
    from ultron.ui.app import run_window  # Qt is loaded only when there's a window to show
    code = run_window(engine)
    # The window closes only after the engine has let go of the mic, the speakers and
    # llama-server. Skip Python's own teardown from there: exit-time destructors in the GPU
    # speech libraries can stall it, and a closed window must mean a finished process.
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(code)


if __name__ == "__main__":
    sys.exit(main())
