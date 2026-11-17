"""The assistant's icon in the notification area (the tray, by the clock): how it stays within
reach while it sleeps with its window hidden.

The user, 2026-10-22: in sleep mode "it minimizes the UI and stays in the ... currently running
section on Windows. Basically, it disappears, but it's still there." So the icon shows the state
at a glance, its tooltip says it in words, and its menu has Wake up, Sleep and Turn off. A click
on the icon wakes it when it's asleep and shows the window otherwise.

Like the window, it does nothing on its own: it changes only when set_state() is called, on the
Qt thread. The icons are painted from the mic shape (no image files), once per state.
"""
from PyQt6.QtCore import QObject, QRectF, Qt, pyqtSignal
from PyQt6.QtGui import QColor, QIcon, QPainter, QPen, QPixmap
from PyQt6.QtWidgets import QMenu, QSystemTrayIcon

from aj.core.pipeline import Status
from aj.ui import theme
from aj.ui.mic_button import paint_mic

# The states the tray shows. "asleep on battery": asleep with the mic closed, because listening
# for the wake words is for mains power only (the user, 2026-10-22); only this icon, the
# shortcut or plugging in brings it back.
LOADING = "loading"
AWAKE = "awake"
ASLEEP = "asleep"
ASLEEP_ON_BATTERY = "asleep on battery"
ASLEEP_MIC_OFF = "asleep, mic off"  # asleep with the window's mic button off: nothing said wakes it
TURNING_OFF = "turning off"
STATES = (LOADING, AWAKE, ASLEEP, ASLEEP_ON_BATTERY, ASLEEP_MIC_OFF, TURNING_OFF)
_ASLEEP_STATES = (ASLEEP, ASLEEP_ON_BATTERY, ASLEEP_MIC_OFF)
_FOR_STATUS = {Status.LOADING: LOADING, Status.TURNING_OFF: TURNING_OFF, Status.ASLEEP: ASLEEP,
               Status.ASLEEP_ON_BATTERY: ASLEEP_ON_BATTERY, Status.ASLEEP_MIC_OFF: ASLEEP_MIC_OFF}

# (disc, ring, mic, mic struck through). Awake is the app icon itself: a red disc, a white mic.
# Asleep is dark with a red mic: it's still listening for its name. On battery the mic is struck
# through: it isn't listening at all.
_LOOKS: dict[str, tuple[str, str | None, str, bool]] = {
    LOADING: (theme.MUTED, None, "white", False),
    AWAKE: (theme.RED, None, "white", False),
    ASLEEP: (theme.OFF_FILL, theme.RED_DARK, theme.RED_BRIGHT, False),
    ASLEEP_ON_BATTERY: (theme.OFF_FILL, theme.OFF_RING, theme.MUTED, True),
    ASLEEP_MIC_OFF: (theme.OFF_FILL, theme.OFF_RING, theme.FAINT, True),
    TURNING_OFF: (theme.OFF_FILL, theme.OFF_RING, theme.DIM, True),
}


def state_for(status: Status) -> str:
    """The tray's state for the engine's status: every status but these few is plain "awake"."""
    return _FOR_STATUS.get(status, AWAKE)


def tray_icon(state: str) -> QIcon:
    """The icon for a state, at the sizes Windows asks for (16 and 32 px at 100% and 200% scaling)."""
    disc, ring, glyph, slashed = _LOOKS[state]
    icon = QIcon()
    for size in (16, 20, 24, 32, 48, 64):
        pixmap = QPixmap(size, size)
        pixmap.fill(Qt.GlobalColor.transparent)
        p = QPainter(pixmap)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        width = max(1.0, size / 16)
        p.setPen(QPen(QColor(ring), width) if ring else Qt.PenStyle.NoPen)
        p.setBrush(QColor(disc))
        inset = width / 2 + 0.5 if ring else 0.5
        p.drawEllipse(QRectF(inset, inset, size - 2 * inset, size - 2 * inset))
        mic = size * 0.56
        paint_mic(p, QRectF((size - mic) / 2, (size - mic) / 2, mic, mic), QColor(glyph), slashed=slashed,
                  behind=QColor(disc))
        p.end()
        icon.addPixmap(pixmap)
    return icon


def wake_hint(names: list[str]) -> str:
    """'Say "Hey AJ" or "Computer"' for names ["AJ", "Computer"]."""
    said = [f'"Hey {names[0]}"', *(f'"{n}"' for n in names[1:])] if names else []
    if len(said) > 1:
        return f"Say {', '.join(said[:-1])} or {said[-1]}"
    return f"Say {said[0]}" if said else "Use this icon"


def tooltip(state: str, names: list[str]) -> str:
    """What hovering over the icon says. Windows cuts tooltips at 127 characters."""
    name = names[0] if names else "Assistant"
    return {
        LOADING: f"{name}: starting up...",
        AWAKE: f"{name}: awake",
        ASLEEP: f"{name}: asleep. {wake_hint(names)} to wake it.",
        ASLEEP_ON_BATTERY: f"{name}: asleep, not listening on battery. Click to wake it.",
        ASLEEP_MIC_OFF: f"{name}: asleep with the mic off. Click to wake it.",
        TURNING_OFF: f"{name}: turning off...",
    }[state]


class Tray(QObject):
    """The tray icon and its menu. It only reports what was clicked; ui/app.py decides what that
    does (the engine wakes or sleeps, the window shows or hides).

    A QObject that owns a QSystemTrayIcon, rather than one: the menu and the icon then live and
    die together, and tests can drive the menu where Windows has no tray (the offscreen platform)."""

    wake_requested = pyqtSignal()
    sleep_requested = pyqtSignal()
    off_requested = pyqtSignal()
    show_requested = pyqtSignal()  # a click on the icon while awake: bring the window back

    def __init__(self, names: list[str] | tuple[str, ...] = ("AJ", "Computer"), parent: QObject | None = None):
        super().__init__(parent)
        self.names = [n for n in names if n]
        self.icon = QSystemTrayIcon(self)
        self.menu = QMenu()  # no parent: a tray menu is a window of its own
        self.wake_action = self.menu.addAction("Wake up")
        self.sleep_action = self.menu.addAction("Sleep")
        self.menu.addSeparator()
        self.off_action = self.menu.addAction("Turn off")
        self.wake_action.triggered.connect(self.wake_requested.emit)
        self.sleep_action.triggered.connect(self.sleep_requested.emit)
        self.off_action.triggered.connect(self.off_requested.emit)
        self.icon.setContextMenu(self.menu)
        self.icon.activated.connect(self._activated)
        self._icons = {state: tray_icon(state) for state in STATES}
        self.state = ""
        self.set_state(LOADING)

    @staticmethod
    def available() -> bool:
        """False where there's no notification area (or the offscreen test platform)."""
        return QSystemTrayIcon.isSystemTrayAvailable()

    def show(self) -> None:
        self.icon.show()

    def hide(self) -> None:
        """Gone from the tray (turning off): Windows otherwise leaves a dead icon until hovered."""
        self.icon.hide()

    def set_state(self, state: str) -> None:
        if state not in _LOOKS:
            raise ValueError(f"unknown tray state {state!r}")
        if state == self.state:
            return
        self.state = state
        self.icon.setIcon(self._icons[state])
        self.icon.setToolTip(tooltip(state, self.names))
        asleep = state in _ASLEEP_STATES
        self.wake_action.setEnabled(asleep)
        self.sleep_action.setEnabled(state == AWAKE)
        self.off_action.setEnabled(state != TURNING_OFF)

    def tooltip(self) -> str:
        return self.icon.toolTip()

    def _activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        """A left click (Trigger; a double click starts with one too). Right clicks open the menu."""
        if reason != QSystemTrayIcon.ActivationReason.Trigger:
            return
        if self.state in _ASLEEP_STATES:
            self.wake_requested.emit()
        elif self.state in (AWAKE, LOADING):
            self.show_requested.emit()
