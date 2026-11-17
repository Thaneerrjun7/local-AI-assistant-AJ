"""AJ (Speak)'s window: a small always-on-top pill with a mic button, the state in words, a level bar
while it listens, a Polite switch and a small x.

The one thing it must get right: clicking it must NOT take the focus from the text box he is typing
in. The user, 2026-10-22: "anywhere where I can type anything, I should be able to activate speech
to text." If this window took the focus, the cursor would leave his message, UI Automation would see
this window's buttons instead of his text box, and the typing would have nowhere to go. So it is a
non-activating tool window, three ways over:
- Qt's own flags: Tool, WindowStaysOnTopHint, WindowDoesNotAcceptFocus, and the attribute
  WA_ShowWithoutActivating (it is shown without being activated, too);
- WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW set on the real window with SetWindowLongPtrW once it exists,
  and again each time it is shown. NOACTIVATE is what stops a click activating it; TOOLWINDOW keeps
  it off the taskbar and out of Alt+Tab;
- no widget takes keyboard focus (every button is NoFocus), so there is nothing to focus.
Tooltips need WA_AlwaysShowToolTips, because Qt shows them only for the active window otherwise.

Like the assistant's window it does nothing on its own: it changes only when the Speaker reports
(through two Qt signals, which carry the report from the dictation's thread to the Qt thread), when
the hotkey is pressed, or when he clicks. No timers run while it is idle; the only timers are the
short ones that put "Ready" back after a result is shown and end the flash of a second launch.

The look is AJ's (ui/theme.py): near-black, red when it is listening. The small x cancels a
dictation in progress (nothing is typed) and, when there is none, closes the window; right after a
dictation ends it ignores clicks for a moment, so reaching for the cancel a split second late can't
close it by accident.
"""
import ctypes
import html
import logging
import time
from ctypes import wintypes
from collections.abc import Callable

from PyQt6.QtCore import QPoint, QRectF, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QColor, QGuiApplication, QPainter, QPen
from PyQt6.QtWidgets import QAbstractButton, QHBoxLayout, QLabel, QMenu, QPushButton, QVBoxLayout, QWidget

from aj.speak import speaker as sp
from aj.speak import store
from aj.speak.icon import speak_icon
from aj.speak.speaker import Speaker
from aj.ui import theme
from aj.ui.mic_button import paint_mic

log = logging.getLogger(__name__)

TITLE = "AJ (Speak)"
WIDTH, HEIGHT = 320, 76
GOOD, WARN = "#5fd08d", "#e8a93a"   # a typed result and "typed, but look at it": the theme has no green or amber
REVERT_MS = {sp.TYPED: 2500, sp.CHECK: 9000, sp.ERROR: 7000, sp.CANCELLED: 1800}  # how long a result stays up
FLASH_MS = 900
QUIT_GUARD_SECS = 0.8   # the x ignores clicks this long after a dictation ends
BUSY = (sp.LISTENING, sp.TRANSCRIBING, sp.POLISHING, sp.TYPING)
_SHORT = 16             # up to this many characters is a state word (big); longer is a message (small)

# ---- the real window, below Qt ------------------------------------------------------------------
_GWL_EXSTYLE = -20
WS_EX_NOACTIVATE, WS_EX_TOOLWINDOW, WS_EX_APPWINDOW = 0x08000000, 0x00000080, 0x00040000
_HWND_TOPMOST = -1
_SWP_NOSIZE, _SWP_NOMOVE, _SWP_NOACTIVATE = 0x0001, 0x0002, 0x0010


class Win32Window:
    """The few user32 calls on a window handle. The tests give make_no_activate a fake instead."""

    def __init__(self):
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.GetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int]
        user32.GetWindowLongPtrW.restype = ctypes.c_ssize_t
        user32.SetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_ssize_t]
        user32.SetWindowLongPtrW.restype = ctypes.c_ssize_t
        user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                        ctypes.c_int, wintypes.UINT]
        user32.SetWindowPos.restype = wintypes.BOOL
        self._user32 = user32

    def ex_style(self, hwnd: int) -> int:
        return int(self._user32.GetWindowLongPtrW(hwnd, _GWL_EXSTYLE))

    def set_ex_style(self, hwnd: int, style: int) -> None:
        self._user32.SetWindowLongPtrW(hwnd, _GWL_EXSTYLE, style)

    def raise_topmost(self, hwnd: int) -> None:
        """Back on top of the other always-on-top windows, still without taking the focus."""
        self._user32.SetWindowPos(hwnd, _HWND_TOPMOST, 0, 0, 0, 0, _SWP_NOMOVE | _SWP_NOSIZE | _SWP_NOACTIVATE)


def make_no_activate(hwnd: int, api=None) -> bool:
    """Set WS_EX_NOACTIVATE and WS_EX_TOOLWINDOW (and clear WS_EX_APPWINDOW) on a real window. True
    once the window has them; False if the handle isn't a real window (Qt's offscreen platform)."""
    api = api or Win32Window()
    try:
        style = api.ex_style(hwnd)
        if not style:
            return False
        wanted = (style | WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW) & ~WS_EX_APPWINDOW
        if wanted != style:
            api.set_ex_style(hwnd, wanted)
        return True
    except Exception as exc:  # noqa: BLE001 - worth knowing, but the Qt flags still apply
        log.error("couldn't make the window non-activating: %s", exc)
        return False


def screen_rects() -> list[store.Rect]:
    """The usable area (without the taskbar) of each screen, the primary one first."""
    rects = []
    for screen in QGuiApplication.screens():
        g = screen.availableGeometry()
        rects.append((g.left(), g.top(), g.width(), g.height()))
    return rects


# ---- the parts --------------------------------------------------------------------------------

class RecordButton(QAbstractButton):
    """The big round mic. Dark with a red ring when ready, solid red while it listens, dim while it
    works. Painted from the mic shape; it never takes keyboard focus."""
    READY, LISTENING, BUSY = "ready", "listening", "busy"

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.mode = self.READY
        self.setFixedSize(52, 52)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover)
        self.setAccessibleName("Speak")

    def set_mode(self, mode: str) -> None:
        if mode != self.mode:
            self.mode = mode
            self.update()

    def hitButton(self, pos) -> bool:
        r = self.width() / 2
        return (pos.x() - r) ** 2 + (pos.y() - r) ** 2 <= r * r

    def paintEvent(self, event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        if self.mode == self.LISTENING:
            fill, ring, glyph = QColor(theme.RED), QColor(theme.RED_BRIGHT), QColor("white")
        elif self.mode == self.BUSY:
            fill, ring, glyph = QColor("#1b1b1f"), QColor(theme.BORDER), QColor(theme.DIM)
        else:
            fill, ring, glyph = QColor(theme.PANEL), QColor(theme.RED), QColor("white")
        if self.mode != self.BUSY:
            if self.isDown():
                fill = fill.darker(125)
            elif self.underMouse():
                fill = fill.lighter(150) if self.mode == self.READY else fill.lighter(118)
        body = QRectF(self.rect()).adjusted(2, 2, -2, -2)
        p.setPen(QPen(ring, 2.5))
        p.setBrush(fill)
        p.drawEllipse(body)
        size = body.width() * 0.46
        c = body.center()
        paint_mic(p, QRectF(c.x() - size / 2, c.y() - size / 2, size, size), glyph)


class LevelBar(QWidget):
    """How loud it is hearing him: a thin bar, drawn only while listening."""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.level = 0.0
        self.active = False
        self.setFixedHeight(6)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)

    def set_active(self, on: bool) -> None:
        self.active = on
        self.level = 0.0
        self.update()

    def set_level(self, level: float) -> None:
        if self.active:
            self.level = max(0.0, min(1.0, float(level)))
            self.update()

    def paintEvent(self, event) -> None:
        if not self.active:
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(Qt.PenStyle.NoPen)
        track = QRectF(self.rect())
        p.setBrush(QColor(theme.BORDER))
        p.drawRoundedRect(track, 3, 3)
        if self.level > 0.01:
            p.setBrush(QColor(theme.RED_BRIGHT))
            p.drawRoundedRect(QRectF(0, 0, max(6.0, track.width() * self.level), track.height()), 3, 3)


# ---- the window -------------------------------------------------------------------------------

class SpeakWindow(QWidget):
    hotkey_pressed = pyqtSignal()   # from the hotkey's thread
    show_requested = pyqtSignal()   # from the thread that waits for a second launch
    listen_requested = pyqtSignal()  # ... and when that launch asked for listening too (--listen)
    quit_requested = pyqtSignal()   # the window has closed: the program ends
    _state = pyqtSignal(str, str)   # from the dictation's thread (Speaker.on_state)
    _level = pyqtSignal(float)      # ... and Speaker.on_level

    def __init__(self, speaker: Speaker, saved: dict | None = None, store_path=None,
                 screens: list[store.Rect] | None = None, clock: Callable[[], float] = time.monotonic):
        super().__init__()
        saved = saved or {}
        self.speaker = speaker
        self.hotkey_text = ""
        self.hotkey_detail = "none"
        self.shown: tuple[str, str] = (sp.READY, "")
        self.caption = ""
        self._store_path = store_path
        self._clock = clock
        self._was_busy = False
        self._idle_since = float("-inf")
        self._drag: QPoint | None = None
        self._moved = False
        self._flash = False

        self.setWindowTitle(TITLE)
        self.setWindowIcon(speak_icon())
        self.setWindowFlags(Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint
                            | Qt.WindowType.WindowStaysOnTopHint | Qt.WindowType.WindowDoesNotAcceptFocus)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WidgetAttribute.WA_AlwaysShowToolTips)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setFixedSize(WIDTH, HEIGHT)
        self.move(*store.place(saved, WIDTH, HEIGHT, screens if screens is not None else screen_rects()))

        self.mic = RecordButton()
        self.mic.clicked.connect(lambda _checked=False: self.speaker.toggle())
        self.label = QLabel()
        self.label.setTextFormat(Qt.TextFormat.RichText)
        self.label.setWordWrap(True)
        self.label.setAlignment(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft)
        self.label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.bar = LevelBar()
        self.close_button = QPushButton("×")
        self.close_button.setFixedSize(22, 22)
        self.close_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.close_button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.close_button.setAccessibleName("Cancel or close")
        self.close_button.setStyleSheet(
            f"QPushButton {{ background: transparent; border: none; color: {theme.MUTED}; font-size: 14pt; }}"
            f"QPushButton:hover {{ color: {theme.RED_BRIGHT}; }}")
        self.close_button.clicked.connect(self._x_clicked)
        self.polite_button = QPushButton("Polite")
        self.polite_button.setCheckable(True)
        self.polite_button.setChecked(bool(saved.get("polite", False)))
        self.polite_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.polite_button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.polite_button.setToolTip("Polite: rewrite what you say as refined, polite English before typing it")
        self.polite_button.setStyleSheet(
            f"QPushButton {{ background: transparent; color: {theme.MUTED}; border: 1px solid {theme.BORDER};"
            f" border-radius: 9px; padding: 2px 9px; font-size: 8pt; }}"
            f"QPushButton:hover {{ border-color: {theme.RED}; color: {theme.TEXT}; }}"
            f"QPushButton:checked {{ background: {theme.RED}; color: white; border-color: {theme.RED_BRIGHT}; }}")
        self.polite_button.toggled.connect(self._polite_toggled)
        self.speaker.polite = self.polite_button.isChecked()

        middle = QVBoxLayout()
        middle.setSpacing(4)
        middle.addWidget(self.label, 1)
        middle.addWidget(self.bar)
        right = QVBoxLayout()
        right.setSpacing(4)
        right.addWidget(self.close_button, 0, Qt.AlignmentFlag.AlignRight)
        right.addStretch(1)
        right.addWidget(self.polite_button)
        row = QHBoxLayout(self)
        row.setContentsMargins(10, 10, 10, 10)
        row.setSpacing(10)
        row.addWidget(self.mic)
        row.addLayout(middle, 1)
        row.addLayout(right)

        speaker.on_state = self._state.emit
        speaker.on_level = self._level.emit
        self._state.connect(self.show_state)
        self._level.connect(self.bar.set_level)
        self.hotkey_pressed.connect(self.speaker.toggle)
        self.show_requested.connect(self.bring_forward)
        self.listen_requested.connect(self.speaker.listen)
        self._revert = QTimer(self)
        self._revert.setSingleShot(True)
        self._revert.timeout.connect(self._back_to_ready)

        self.set_hotkey("")
        make_no_activate(int(self.winId()))  # the native window exists from here, before it is shown

    # -- what it shows -------------------------------------------------------------------------

    def set_hotkey(self, text: str, detail: str = "") -> None:
        """Say which hotkey is live: `text` is its name (Ctrl+Alt+S, or Ctrl+Alt+D when S was taken, or
        "" for none), `detail` the longer line for tooltips ("... is taken by another program")."""
        self.hotkey_text = text
        self.hotkey_detail = detail or text or "none"
        also = f"or press {self.hotkey_detail} " if text else ""
        self.mic.setToolTip(f"Click {also}and speak. It stops when you pause, and types where your cursor is. "
                            "It never presses Enter: you send it yourself.")
        self.setToolTip(f"AJ (Speak) types what you say where your cursor is. Hotkey: {self.hotkey_detail}. "
                        "Drag to move. Right-click for the menu.")
        if self.shown[0] == sp.READY:
            self.show_state(sp.READY)

    def _words(self, state: str, text: str) -> tuple[str, str, str]:
        """(what to say, its colour, a second small line) for a state."""
        if state in sp.RESULTS:
            colour = {sp.TYPED: GOOD, sp.CHECK: WARN, sp.ERROR: theme.RED_BRIGHT, sp.CANCELLED: theme.MUTED}[state]
            return text, colour, ""
        say = {sp.READY: "Ready", sp.LISTENING: "Listening", sp.TRANSCRIBING: "Transcribing",
               sp.POLISHING: "Polishing", sp.TYPING: "Typing"}[state]
        colour = theme.RED_BRIGHT if state == sp.LISTENING else theme.TEXT
        return say, colour, (self.hotkey_text if state == sp.READY else "")

    def show_state(self, state: str, text: str = "") -> None:
        """Show what the Speaker reports. On the Qt thread."""
        self.shown = (state, text)
        say, colour, small = self._words(state, text)
        self.caption = say
        if len(say) <= _SHORT:
            body = f'<span style="font-size:13pt;font-weight:600;color:{colour}">{html.escape(say)}</span>'
        else:
            body = f'<span style="font-size:9pt;color:{colour}">{html.escape(say)}</span>'
        if small:
            body += f'<br><span style="font-size:8pt;color:{theme.MUTED}">{html.escape(small)}</span>'
        self.label.setText(body)
        self.label.setToolTip(text if state in sp.RESULTS else "")
        self.bar.set_active(state == sp.LISTENING)
        self.bar.setVisible(state == sp.LISTENING)  # its room goes to a long message otherwise
        self.mic.set_mode(RecordButton.LISTENING if state == sp.LISTENING
                          else RecordButton.BUSY if state in BUSY else RecordButton.READY)
        busy = state in BUSY
        if self._was_busy and not busy:
            self._idle_since = self._clock()
        self._was_busy = busy
        self.close_button.setToolTip("Cancel (nothing is typed)" if busy else f"Close {TITLE}")
        if state in REVERT_MS:
            self._revert.start(REVERT_MS[state])
        else:
            self._revert.stop()
        self.update()

    def _back_to_ready(self) -> None:
        if not self.speaker.busy:
            self.show_state(sp.READY)

    # -- clicks ----------------------------------------------------------------------------------

    def _polite_toggled(self, on: bool) -> None:
        self.speaker.polite = on
        self._save()

    def _x_clicked(self) -> None:
        """Cancel a dictation in progress; with none, close the window (unless one has only just ended)."""
        if self.speaker.busy:
            self.speaker.cancel()  # False while the keys are going in: too late, nothing to do
            return
        if self._clock() - self._idle_since < QUIT_GUARD_SECS:
            return
        self.close()

    def menu(self) -> QMenu:
        menu = QMenu(self)
        menu.addAction(f"Hotkey: {self.hotkey_detail}").setEnabled(False)
        menu.addAction(f"Quit {TITLE}", self.close)
        return menu

    def contextMenuEvent(self, event) -> None:
        self.menu().exec(event.globalPos())

    # -- where it sits ---------------------------------------------------------------------------

    def _save(self) -> None:
        store.save({"x": self.x(), "y": self.y(), "polite": self.polite_button.isChecked()}, self._store_path)

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
            self._moved = False
            event.accept()

    def mouseMoveEvent(self, event) -> None:
        if self._drag is not None and event.buttons() & Qt.MouseButton.LeftButton:
            target = event.globalPosition().toPoint() - self._drag
            if target != self.pos():
                self.move(target)
                self._moved = True

    def mouseReleaseEvent(self, event) -> None:
        if self._drag is not None:
            self._drag = None
            if self._moved:
                self._save()

    def bring_forward(self) -> None:
        """A second launch asked for this window: show it, on a screen, on top, with a brief red
        outline so he sees where it is. It does not take the focus."""
        if not self.isVisible():
            self.show()
        x, y = store.place({"x": self.x(), "y": self.y()}, WIDTH, HEIGHT, screen_rects())
        if (x, y) != (self.x(), self.y()):
            self.move(x, y)
        try:
            Win32Window().raise_topmost(int(self.winId()))
        except Exception as exc:  # noqa: BLE001 - the Qt flags keep it on top anyway
            log.error("couldn't raise the window: %s", exc)
        self._flash = True
        self.update()
        QTimer.singleShot(FLASH_MS, self._end_flash)

    def _end_flash(self) -> None:
        self._flash = False
        self.update()

    def showEvent(self, event) -> None:
        make_no_activate(int(self.winId()))
        super().showEvent(event)

    def closeEvent(self, event) -> None:
        self._save()
        self.speaker.close()
        self.quit_requested.emit()
        event.accept()

    def paintEvent(self, event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        lit = self.shown[0] == sp.LISTENING or self._flash
        p.setPen(QPen(QColor(theme.RED_BRIGHT if lit else theme.BORDER), 2 if lit else 1))
        p.setBrush(QColor(theme.BG))
        p.drawRoundedRect(QRectF(self.rect()).adjusted(1, 1, -1, -1), 16, 16)
