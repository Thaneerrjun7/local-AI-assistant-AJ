"""Ultron's window: a status line, a big mic button, a Close button, the conversation, and a
box to type in.

It does nothing on its own (no timers, no animation): everything it shows arrives as a
signal from the engine thread, so it sleeps while Ultron is idle. The one thing that ticks is
the text box's caret, and Qt stops that whenever the window isn't in front. Closing it, by the Close
button or the title-bar X, turns Ultron off; the window stays up saying "Turning off..."
until the engine has said goodbye and let go of everything, then closes for real.
"""
from typing import Protocol

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QCloseEvent, QFont
from PyQt6.QtWidgets import QHBoxLayout, QLabel, QLineEdit, QMessageBox, QPushButton, QVBoxLayout, QWidget

from ultron.core.pipeline import Status
from ultron.ui import status as looks
from ultron.ui.mic_button import MicButton
from ultron.ui.transcript import Transcript

DISCLAIMER = ("⚠ Everything Ultron says is in character: a fictional persona inspired by Age of Ultron. "
              "His threats and opinions are a performance, not real intent.")


class Controls(Protocol):
    """The engine as the window sees it. All three are called on the UI thread and must not block."""

    def set_listening(self, on: bool) -> None: ...

    def ask(self, text: str) -> None: ...

    def turn_off(self) -> None: ...


class UltronWindow(QWidget):
    closing = pyqtSignal(bool)  # emitted once, when turning off begins; True if still loading

    def __init__(self, engine: Controls):
        super().__init__()
        self.engine = engine
        self.status = Status.LOADING
        self._closing = False      # turning off has begun
        self._engine_done = False  # the engine thread has ended: closing may really close now
        self._failed = False

        self.setObjectName("root")
        self.setWindowTitle("Ultron")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground)  # let the stylesheet paint the background
        self.resize(420, 640)
        self.setMinimumSize(340, 520)

        title = QLabel("ULTRON")
        title.setObjectName("title")
        title_font = QFont(title.font())
        title_font.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 5)
        title.setFont(title_font)
        self.close_button = QPushButton("Close")
        self.close_button.setObjectName("close")
        self.close_button.setToolTip("Turn Ultron off and close")
        self.close_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.close_button.setFocusPolicy(Qt.FocusPolicy.NoFocus)  # a stray Space or Enter can't turn him off
        self.close_button.clicked.connect(self.close)

        self.status_label = QLabel()
        self.status_label.setObjectName("status")
        self.status_label.setTextFormat(Qt.TextFormat.RichText)

        self.mic = MicButton()
        self.mic.toggled.connect(self._mic_toggled)
        self.mic_caption = QLabel()
        self.mic_caption.setObjectName("caption")
        self.mic_caption.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.mic_caption.setMinimumHeight(20)  # blank while loading; don't let the layout jump

        self.transcript = Transcript()
        self.entry = QLineEdit()
        self.entry.setObjectName("entry")
        self.entry.setPlaceholderText("Type to Ultron, Enter to send")
        self.entry.returnPressed.connect(self._send_typed)
        # The user asked for this (2026-10-06): his menace is a performance, and the window says so.
        self.disclaimer = QLabel(DISCLAIMER)
        self.disclaimer.setObjectName("disclaimer")
        self.disclaimer.setWordWrap(True)
        self.disclaimer.setAlignment(Qt.AlignmentFlag.AlignCenter)

        header = QHBoxLayout()
        header.addWidget(title)
        header.addStretch()
        header.addWidget(self.close_button)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 16, 20, 20)
        layout.setSpacing(10)
        layout.addLayout(header)
        layout.addWidget(self.status_label)
        layout.addSpacing(6)
        layout.addWidget(self.mic, alignment=Qt.AlignmentFlag.AlignHCenter)
        layout.addWidget(self.mic_caption)
        layout.addSpacing(6)
        layout.addWidget(self.transcript, stretch=1)
        layout.addWidget(self.entry)
        layout.addWidget(self.disclaimer)

        self._show_status(Status.LOADING)

    # ---- engine -> window (connected to Bridge signals, so always on the UI thread) ----

    def set_status(self, status: Status) -> None:
        if status == Status.TURNING_OFF:  # "Ultron, turn off": the engine is already on its way out
            self._begin_closing(still_loading=False)
        elif not self._closing:
            self._show_status(status)

    def add_heard(self, text: str) -> None:
        self.transcript.add_you(text)

    def add_turn(self, reply: str, latency: str) -> None:
        self.transcript.add_ultron(reply, latency)

    def show_error(self, message: str) -> None:
        self._failed = True
        self.transcript.add_error(message)
        label, colour = looks.ERROR
        self.status_label.setText(f'<span style="color:{colour}">●</span>&nbsp; {label}')
        self._sync_controls()

    def engine_finished(self) -> None:
        """The engine thread has ended. Close, unless it died with an error worth reading first."""
        self._engine_done = True
        if self._closing or not self._failed:
            self.close()

    def confirm(self, question: str) -> bool:
        """A risky action (delete, move, close an app) needs a clear yes. Anything else is no."""
        self.bring_to_front()
        box = QMessageBox(QMessageBox.Icon.Question, "Ultron needs a yes", question,
                          QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, self)
        box.setDefaultButton(QMessageBox.StandardButton.No)
        return box.exec() == QMessageBox.StandardButton.Yes

    def bring_to_front(self) -> None:
        """The shortcut was clicked while Ultron is on."""
        if self.isMinimized():
            self.showNormal()
        else:
            self.show()
        self.raise_()
        self.activateWindow()

    # ---- window -> engine ----

    def _mic_toggled(self, on: bool) -> None:
        self.engine.set_listening(on)
        self._sync_controls()

    def _send_typed(self) -> None:
        text = self.entry.text().strip()
        if text:
            self.engine.ask(text)
            self.entry.clear()

    def closeEvent(self, event: QCloseEvent) -> None:
        if self._engine_done:
            event.accept()
            return
        event.ignore()  # stay up until the engine has shut down; engine_finished() closes us
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
        label, colour = looks.look(status)
        self.status_label.setText(f'<span style="color:{colour}">●</span>&nbsp; {label}')
        self._sync_controls()

    def _sync_controls(self) -> None:
        ready = self.status not in looks.NOT_READY and not self._failed and not self._closing
        just_ready = ready and not self.entry.isEnabled()
        self.mic.setEnabled(ready)
        self.entry.setEnabled(ready)
        if just_ready:  # loading is over: typing works straight away, without a click
            self.entry.setFocus()
        self.close_button.setEnabled(not self._closing)
        self.mic.setToolTip(("Stop listening" if self.mic.isChecked() else "Start listening") if ready else "")
        self.mic_caption.setText(looks.mic_caption(self.mic.isChecked()) if ready else "")
