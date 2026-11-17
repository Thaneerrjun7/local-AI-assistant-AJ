"""AJ's window: the orb, a status line, a mic button, a Close button, the conversation, and a
box to type in. Drawn by Qt Quick (qml/Main.qml, qml/Face.qml) with the look of
docs/UI_REDESIGN_PLAN.md: liquid glass on a dark ember background, the neural orb, Inter.

This is the window as the rest of AJ sees it, with the same methods and signals the widget
window had (set_status, add_turn, confirm, bring_to_front, closing, closed and the rest), so
app.py, the tray and the engine use it unchanged. Python sets the QML's properties when the
engine reports something and answers its three signals (mic, send, close). Everything that moves
runs in Qt's render thread and the QML: Python never runs per frame, and nothing is drawn while
the window is hidden. Closing it, by the Close button or the title-bar X, turns AJ off; the
window stays up saying "Turning off..." until the engine has said goodbye and let go of
everything, then closes for real.

Sleep mode (2026-10-22): asleep, the window hides (the tray icon stays, ui/tray.py) and comes
back when it wakes. Woken by voice it comes back WITHOUT taking the keyboard: the user may be in
WhatsApp about to dictate, and dictation types into the window that was in front. Woken from
the tray or the shortcut it comes to the front, as he asked for it.
"""
from typing import Protocol

from PyQt6.QtCore import QObject, QSize, Qt, QTimer, QUrl, pyqtSignal
from PyQt6.QtGui import QColor
from PyQt6.QtQuick import QQuickView
from PyQt6.QtWidgets import QMessageBox

from aj.core.pipeline import ASLEEP_STATUSES, Status
from aj.ui import native
from aj.ui import status as looks
from aj.ui.conversation import Conversation
from aj.ui.levels import Follower

DISCLAIMER = "⚠ Some lines are quotes from fiction, said in character, not real intent."
MAIN_QML = native.QML_DIR / "Main.qml"
LEVEL_EVERY_MS = 33  # the voice levels, 30 times a second (the orb smooths between readings)


class Controls(Protocol):
    """The engine as the window sees it. All three are called on the UI thread and must not block.
    An engine may also have sleep() and wake() (sleep mode, from the tray), and voice_level() and
    mic_level() (0..1, how loud AJ's voice and yours are right now, for the orb); the window uses
    them when they're there."""

    def set_listening(self, on: bool) -> None: ...

    def ask(self, text: str) -> None: ...

    def turn_off(self) -> None: ...


class AJWindow(QObject):
    closing = pyqtSignal(bool)  # emitted once, when turning off begins; True if still loading
    closed = pyqtSignal()       # the window really closed: AJ is off (the app quits on it)

    def __init__(self, engine: Controls):
        super().__init__()
        self.engine = engine
        self.status = Status.LOADING
        self._closing = False      # turning off has begun
        self._engine_done = False  # the engine thread has ended: closing may really close now
        self._failed = False
        self._mic_on = True
        self._working = ""          # the tool running, if any
        self._tinted = False
        self._box: QMessageBox | None = None

        self.name = getattr(getattr(engine, "settings", None), "name", "AJ")  # the assistant's name (Settings.name)
        self.conversation = Conversation(self.name, self)
        self.view = QQuickView()
        self.view.setTitle(self.name)
        self.view.setColor(QColor(native.BASE))
        self.view.setResizeMode(QQuickView.ResizeMode.SizeRootObjectToView)
        self.view.setMinimumSize(QSize(340, 560))
        self.view.rootContext().setContextProperty("conversation", self.conversation)
        self.view.setSource(QUrl.fromLocalFile(str(MAIN_QML)))
        if self.view.status() == QQuickView.Status.Error:
            raise RuntimeError("the window's QML didn't load:\n" + "\n".join(e.toString() for e in self.view.errors()))
        self.view.resize(420, 720)  # the orb above, room for the conversation below

        self.root = self.view.rootObject()
        self.root.setProperty("name", self.name)
        self.root.setProperty("footnote", DISCLAIMER)  # the user asked for this (2026-10-06)
        native.call(self.root, "setFont", native.load_font())
        native.call(self.root, "setAccessibility", *native.windows_prefers_less())
        native.call(self.root, "loadTuning", native.saved_tuning())  # the lab's tuning, if saved
        if native.software_rendering():
            native.call(self.root, "setSoftware", True)
        self.root.micClicked.connect(self._mic_toggled)
        self.root.sent.connect(self._send_typed)
        self.root.closeClicked.connect(self.close)
        self.root.closeRefused.connect(self._close_refused)
        self.root.closedForReal.connect(self.closed.emit)
        # Python takes no part in the window's events (see Main.qml, `mayClose`), nor in Qt's
        # render thread: whether Qt fell back to drawing without a GPU is looked at once, a
        # moment after the window first shows.
        QTimer.singleShot(1500, self._check_renderer)

        # The voice levels, read only while the orb moves with a voice (hearing, speaking,
        # dictating): one small Python call 30 times a second then, none at all otherwise.
        self._voice, self._mic = Follower(), Follower()
        self._levels = QTimer(self)
        self._levels.setInterval(LEVEL_EVERY_MS)
        self._levels.timeout.connect(self._read_levels)
        self._show_status(Status.LOADING)

    @property
    def _engine_done(self) -> bool:
        """The engine thread has ended: a close may really close now (Main.qml, `mayClose`)."""
        return self._done

    @_engine_done.setter
    def _engine_done(self, done: bool) -> None:
        self._done = done
        if hasattr(self, "root"):
            self.root.setProperty("mayClose", done)

    # ---- the window, as a widget would have it ----

    def show(self) -> None:
        self._tint()
        self.view.show()

    def hide(self) -> None:
        self.view.hide()

    def close(self) -> bool:
        return self.view.close()

    def showNormal(self) -> None:
        self._tint()
        self.view.showNormal()

    def raise_(self) -> None:
        self.view.raise_()

    def activateWindow(self) -> None:
        self.view.requestActivate()

    def isVisible(self) -> bool:
        return self.view.isVisible()

    def isMinimized(self) -> bool:
        return bool(self.view.windowStates() & Qt.WindowState.WindowMinimized)

    def deleteLater(self) -> None:
        self.view.deleteLater()
        super().deleteLater()

    # ---- engine -> window (connected to Bridge signals, so always on the UI thread) ----

    def set_status(self, status: Status) -> None:
        if status == Status.TURNING_OFF:  # "Computer, turn off": the engine is already on its way out
            self._begin_closing(still_loading=False)
        elif not self._closing:
            was_asleep = self.status in ASLEEP_STATUSES
            self._show_status(status)
            if status in ASLEEP_STATUSES and not was_asleep:
                self.hide()  # to the tray
            elif (was_asleep and status not in ASLEEP_STATUSES) or status == Status.MIC_FAILED:
                # Woken (by voice, or by typing), or the mic won't open (asleep, nothing else would
                # tell him: the review of 2026-10-22): back, keyboard left alone.
                self.show_quietly()

    def set_working(self, tool: str) -> None:
        """A tool started (its name) or finished (""): while one runs, Thinking shows as Working."""
        self._working = tool
        if not self._closing and not self._failed:
            self._show_status(self.status)

    def add_heard(self, text: str) -> None:
        self.conversation.add_you(text)

    def add_turn(self, reply: str, latency: str) -> None:
        self.conversation.add_aj(reply, latency)

    def show_error(self, message: str) -> None:
        self._failed = True
        self._follow_levels(False)
        self.conversation.add_error(message)
        label, _ = looks.ERROR
        self.root.setProperty("statusText", label)
        self.root.setProperty("statusTone", looks.ERROR_TONE)
        self.root.setProperty("mode", looks.ERROR_MODE)
        self._sync_controls()

    def engine_finished(self) -> None:
        """The engine thread has ended. Close, unless it died with an error worth reading first."""
        self._engine_done = True
        if self._closing or not self._failed:
            self.close()

    def confirm(self, question: str) -> bool:
        """A risky action (delete, move, close an app) needs a clear yes. Anything else is no."""
        self.bring_to_front()
        box = QMessageBox(QMessageBox.Icon.Question, f"{self.name} needs a yes", question,
                          QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        box.setDefaultButton(QMessageBox.StandardButton.No)
        box.winId()  # so it has a native window to keep over ours
        box.windowHandle().setTransientParent(self.view)
        self._box = box
        try:
            return box.exec() == QMessageBox.StandardButton.Yes
        finally:
            self._box = None

    def close_confirm(self) -> None:
        """The question was settled without a click (a spoken answer, or no answer in time)."""
        if self._box is not None:
            self._box.done(QMessageBox.StandardButton.No)

    def bring_to_front(self) -> None:
        """The shortcut was clicked while AJ is on (or the tray icon): asleep, that wakes him."""
        if self.status in ASLEEP_STATUSES:
            self.wake_up()
        if self.isMinimized():
            self.showNormal()
        else:
            self.show()
        self.raise_()
        self.activateWindow()

    def show_quietly(self) -> None:
        """Back on screen without taking the keyboard from the window in front (Windows shows it
        with SW_SHOWNOACTIVATE), so dictation still types where the user was. The property is
        the one QWidget's WA_ShowWithoutActivating sets; Qt's Windows plugin reads it on show."""
        if self.isVisible() and not self.isMinimized():
            return
        self.view.setProperty("_q_showWithoutActivating", True)
        try:
            if self.isMinimized():
                self.showNormal()
            else:
                self.show()
        finally:
            self.view.setProperty("_q_showWithoutActivating", False)

    def wake_up(self) -> None:
        """Out of sleep mode (the tray's Wake up)."""
        wake = getattr(self.engine, "wake", None)
        if wake is not None:
            wake()

    def go_to_sleep(self) -> None:
        """Into sleep mode (the tray's Sleep). The window hides once the engine says it's asleep."""
        sleep = getattr(self.engine, "sleep", None)
        if sleep is not None and not self._closing:
            sleep()

    # ---- window -> engine ----

    def _mic_toggled(self) -> None:
        self._mic_on = not self._mic_on
        self.engine.set_listening(self._mic_on)
        self._sync_controls()

    def _send_typed(self, text: str) -> None:
        text = text.strip()
        if text:
            self.engine.ask(text)

    def _close_refused(self) -> None:
        """A close before the engine has ended: turn AJ off. The window stays up until
        engine_finished(), which lets the next close through."""
        if not self._closing:
            self.engine.turn_off()
            self._begin_closing(still_loading=self.status == Status.LOADING)

    # ---- display ----

    def _begin_closing(self, still_loading: bool) -> None:
        if self._closing:
            return
        self._closing = True
        self._show_status(Status.TURNING_OFF)
        self.closing.emit(still_loading)

    def _show_status(self, status: Status) -> None:
        self.status = status
        label, _ = looks.look(status)
        mode = looks.orb_mode(status)
        if self._working and status == Status.THINKING:
            label, mode = looks.working_text(self._working), looks.WORKING_MODE
        self.root.setProperty("statusText", label)
        self.root.setProperty("statusTone", looks.tone(status))
        self.root.setProperty("mode", mode)
        self._follow_levels(status in looks.LEVEL_STATUSES)
        self._sync_controls()

    def _follow_levels(self, on: bool) -> None:
        if on and not self._levels.isActive() and hasattr(self.engine, "voice_level"):
            self._levels.start()
        elif not on and self._levels.isActive():
            self._levels.stop()
            self._voice.reset()
            self._mic.reset()
            self.root.setProperty("voiceLevel", 0.0)
            self.root.setProperty("micLevel", 0.0)

    def _read_levels(self) -> None:
        dt = LEVEL_EVERY_MS / 1000
        try:
            voice = self._voice.step(self.engine.voice_level(), dt)
            mic = self._mic.step(self.engine.mic_level(), dt) if hasattr(self.engine, "mic_level") else 0.0
        except Exception:  # a level is only for show: never let it break the window
            return
        self.root.setProperty("voiceLevel", voice)
        self.root.setProperty("micLevel", mic)

    def _sync_controls(self) -> None:
        ready = self.status not in looks.NOT_READY and not self._failed and not self._closing
        self.root.setProperty("ready", ready)
        self.root.setProperty("micOn", self._mic_on)
        self.root.setProperty("closeEnabled", not self._closing)
        self.root.setProperty("micCaption", looks.mic_caption(self._mic_on) if ready else "")

    def _tint(self) -> None:
        if not self._tinted:
            native.tint_title_bar(self.view)
            self._tinted = True

    def _check_renderer(self) -> None:
        if native.software_rendering(self.view):
            native.call(self.root, "setSoftware", True)
