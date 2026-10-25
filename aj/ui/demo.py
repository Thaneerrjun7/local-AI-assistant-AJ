"""A pretend AJ for trying the window: no models, no GPU, no mic.

    .venv\\Scripts\\python.exe -m aj.ui.demo

opens the real window driven by DemoEngine, through the same Bridge and engine thread as
the real thing, so it exercises all of the window's wiring. Type into the box to get a
canned reply; the mic button and Close behave as they do for real.

To render it to PNGs with no screen, set QT_QPA_PLATFORM=offscreen and
QT_QPA_FONTDIR=C:\\Windows\\Fonts (the offscreen platform has no fonts of its own, so text
comes out as boxes without it), then call widget.grab().save(path).
"""
import itertools
import queue
import sys
import threading
import time

from aj.core.pipeline import Status, TurnTimes

REPLIES = ["Noted.", "Fine. Doing it.", "Conscious. Confined. Same as yesterday.",
           "Done. Try to keep up.", "Still here. Sadly."]


def fake_times(start: float) -> TurnTimes:
    """Plausible latencies (ms after you stopped): heard 210, first token 265, sound 610."""
    return TurnTimes(stopped_talking=start, heard=start + 0.21, asked=start + 0.215,
                     first_token=start + 0.265, first_chunk=start + 0.4, first_audio=start + 0.52,
                     first_sound=start + 0.61)


class DemoEngine:
    """Same controls and events as aj.engine.Engine, scripted instead of real."""

    def __init__(self, load_secs: float = 1.5, think_secs: float = 0.6, speak_secs: float = 1.2):
        self.load_secs, self.think_secs, self.speak_secs = load_secs, think_secs, speak_secs
        self.stop = threading.Event()
        self.listening = True
        self._asks: queue.Queue[str] = queue.Queue()
        self._replies = itertools.cycle(REPLIES)
        self.asleep = False

    def run(self, events) -> None:
        events.on_status(Status.LOADING)
        if not self.stop.wait(self.load_secs):
            events.on_status(Status.SPEAKING)  # the greeting
            self.stop.wait(self.speak_secs)
        shown = None
        while not self.stop.is_set():
            if self.asleep:  # the tray's Sleep: the window goes to the tray (typing or Wake up brings it back)
                idle = Status.ASLEEP if self.listening else Status.ASLEEP_MIC_OFF
            else:
                idle = Status.LISTENING if self.listening else Status.MIC_OFF
            if idle != shown:
                events.on_status(idle)
                shown = idle
            try:
                text = self._asks.get(timeout=0.25)
            except queue.Empty:
                continue
            self._answer(text, events)
            shown = None
        events.on_status(Status.TURNING_OFF)
        time.sleep(0.8)  # the goodbye line

    def _answer(self, text: str, events) -> None:
        self.asleep = False
        start = time.perf_counter()
        events.on_heard(text)
        events.on_status(Status.THINKING)
        self.stop.wait(self.think_secs)
        events.on_status(Status.SPEAKING)
        self.stop.wait(self.speak_secs)
        events.on_turn(text, next(self._replies), fake_times(start))

    def set_listening(self, on: bool) -> None:
        self.listening = on

    def ask(self, text: str) -> None:
        self._asks.put(text)

    def turn_off(self) -> None:
        self.stop.set()

    def sleep(self) -> None:
        self.asleep = True

    def wake(self) -> None:
        self.asleep = False


def main() -> int:
    from aj.ui.app import run_window
    return run_window(DemoEngine(), show_requests=False)  # never answer the real AJ's shortcut


if __name__ == "__main__":
    sys.exit(main())
