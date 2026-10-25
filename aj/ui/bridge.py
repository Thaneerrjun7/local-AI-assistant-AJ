"""Engine thread -> Qt thread.

Qt widgets may only be touched on the thread that runs Qt. The engine reports on its own
threads by calling the on_* methods below; each emits a signal, and because the Bridge lives
on the Qt thread, Qt queues the call over to it. Only plain values (strings, a Status)
cross the boundary.
"""
import threading

from PyQt6.QtCore import QObject, pyqtSignal

from aj.core.pipeline import Status, TurnTimes

CONFIRM_TIMEOUT_SECS = 30.0  # no answer means no


class Bridge(QObject):
    status = pyqtSignal(object)  # a Status
    heard = pyqtSignal(str)      # what you said (or typed), as AJ starts answering it
    turn = pyqtSignal(str, str)  # AJ's reply, and the turn's one-line latency summary
    failed = pyqtSignal(str)     # the engine died: what went wrong
    finished = pyqtSignal()      # the engine thread has ended
    show_requested = pyqtSignal()  # the shortcut was clicked while AJ is on
    confirm_requested = pyqtSignal(str)  # a CONFIRM tool needs a yes or no from the user
    confirm_settled = pyqtSignal()       # answered (maybe by voice) or timed out: close the box

    def __init__(self):
        super().__init__()
        self._answered = threading.Event()
        self._answer = False

    def ask_confirm(self, question: str) -> bool:
        """Engine thread: show the question in the window and wait for the answer."""
        self._answer = False
        self._answered.clear()
        self._asking = question
        self.confirm_requested.emit(question)
        self._answered.wait(CONFIRM_TIMEOUT_SECS)
        self._asking = None
        self.confirm_settled.emit()
        return self._answer

    def answer_confirm(self, yes: bool, question: str | None = None) -> None:
        """The user's answer: a click (UI thread) or a spoken yes or no (the listening thread).
        A late click on a box for an earlier question is ignored."""
        if question is not None and question != getattr(self, "_asking", None):
            return
        self._answer = yes is True
        self._answered.set()

    # The aj.engine.Events interface, called on engine threads.

    def on_status(self, status: Status) -> None:
        self.status.emit(status)

    def on_heard(self, text: str) -> None:
        self.heard.emit(text)

    def on_turn(self, text: str, reply: str, times: TurnTimes) -> None:
        self.turn.emit(reply, times.summary())  # the full report goes to the log (engine.print_turn)
