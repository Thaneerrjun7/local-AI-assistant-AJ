"""Start Qt, the window and the engine thread, and make sure closing really ends the process.

Qt owns the main thread. The engine (model loading and the conversation) runs on a thread
of its own and reports through a Bridge, so the window only ever changes on the Qt thread.

Closing the window turns AJ off: the engine says goodbye, closes the audio and stops
llama-server, then the window closes and the process exits. If that hasn't happened within
OFF_GRACE_MS the process exits anyway (the job object in core/proc.py still takes
llama-server down). Closing while still loading exits at once: there's nothing to say
goodbye with yet, and the model load can't be interrupted part way.

The tray icon (ui/tray.py, 2026-10-22) is how a sleeping AJ stays within reach: its menu
wakes him, puts him to sleep or turns him off, and it shows his state. With the window hidden
in sleep mode, Qt must not take the last window closing (a message box, the tray menu) for the
end of the app, so the app quits only when the main window really closes.
"""
import ctypes
import os
import signal
import sys
import threading
import traceback
from typing import Protocol

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QColor, QFont, QPalette
from PyQt6.QtWidgets import QApplication

from aj.core import switch
from aj.core.pipeline import Status
from aj.engine import Events
from aj.ui import native, theme
from aj.ui.bridge import Bridge
from aj.ui.mic_button import app_icon
from aj.ui.tray import Tray, state_for
from aj.ui.window import Controls, AJWindow

OFF_GRACE_MS = 10_000  # goodbye (~3 s) plus stopping the model server, with room to spare


class Runnable(Controls, Protocol):
    """An engine the window can drive: aj.engine.Engine, or the demo's stand-in."""

    def run(self, events: Events) -> None: ...


def _palette() -> QPalette:
    """Dark base colours, so the parts the stylesheet doesn't reach (context menus, the
    placeholder text) match the window too."""
    palette = QPalette()
    for role, colour in ((QPalette.ColorRole.Window, theme.BG), (QPalette.ColorRole.Base, theme.PANEL),
                         (QPalette.ColorRole.Text, theme.TEXT), (QPalette.ColorRole.WindowText, theme.TEXT),
                         (QPalette.ColorRole.ButtonText, theme.TEXT), (QPalette.ColorRole.Button, theme.PANEL),
                         (QPalette.ColorRole.Highlight, theme.RED),
                         (QPalette.ColorRole.HighlightedText, "#ffffff"),
                         (QPalette.ColorRole.PlaceholderText, theme.DIM)):
        palette.setColor(role, QColor(colour))
    return palette


def make_app() -> QApplication:
    """The QApplication, styled: Fusion + dark palette + the stylesheet (for the parts that are
    still widgets: the yes-or-no box and the tray menu), Inter, the icon."""
    if sys.platform == "win32":  # group under AJ's own taskbar icon, not pythonw's
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("AJ.Assistant")
    app = QApplication.instance() or QApplication(sys.argv[:1])
    app.setApplicationName("AJ")
    app.setStyle("Fusion")
    app.setPalette(_palette())
    app.setFont(QFont(native.load_font(), 10))
    app.setStyleSheet(theme.STYLESHEET)
    app.setWindowIcon(app_icon())
    app.styleHints().setColorScheme(Qt.ColorScheme.Dark)  # dark title bar on Windows 11
    return app


def connect(bridge: Bridge, window: AJWindow, tray: Tray | None = None) -> None:
    bridge.status.connect(window.set_status)
    bridge.heard.connect(window.add_heard)
    bridge.turn.connect(window.add_turn)
    bridge.failed.connect(window.show_error)
    bridge.finished.connect(window.engine_finished)
    bridge.show_requested.connect(window.bring_to_front)  # the shortcut: asleep, that wakes him too
    bridge.confirm_requested.connect(lambda question: bridge.answer_confirm(window.confirm(question), question))
    bridge.confirm_settled.connect(window.close_confirm)
    bridge.working.connect(window.set_working)
    if tray is not None:
        bridge.status.connect(lambda status: tray.set_state(state_for(status)))
        window.closing.connect(lambda _: tray.set_state(state_for(Status.TURNING_OFF)))
        window.closed.connect(tray.hide)
        tray.wake_requested.connect(window.bring_to_front)
        tray.sleep_requested.connect(window.go_to_sleep)
        tray.off_requested.connect(window.close)
        tray.show_requested.connect(window.bring_to_front)


def _engine_thread(engine: Runnable, bridge: Bridge) -> None:
    try:
        engine.run(bridge)
    except Exception as exc:
        traceback.print_exc()
        bridge.failed.emit(f"{type(exc).__name__}: {exc}")
    finally:
        bridge.finished.emit()


def _exit_now(reason: str) -> None:
    print(f"{reason}; exiting now.", flush=True)
    os._exit(0)


def _arm_exit(still_loading: bool) -> None:
    """Called once, when turning off begins. One timer, only while shutting down."""
    if still_loading:
        QTimer.singleShot(0, lambda: _exit_now("Turned off while still loading"))
    else:
        QTimer.singleShot(OFF_GRACE_MS, lambda: _exit_now("Shutdown took too long"))


def _log_exception(kind, value, tb) -> None:
    """A bug in a window callback is logged instead of aborting (PyQt's default)."""
    traceback.print_exception(kind, value, tb)


def run_window(engine: Runnable, *, show_requests: bool = True) -> int:
    """Open the window and run `engine` behind it. Returns when AJ is off."""
    signal.signal(signal.SIGINT, signal.SIG_DFL)  # Ctrl+C in a console ends it at once (Qt would swallow it)
    sys.excepthook = _log_exception
    app = make_app()
    app.setQuitOnLastWindowClosed(False)  # asleep, the window is hidden: see the module docstring
    bridge = Bridge()
    window = AJWindow(engine)
    settings = getattr(engine, "settings", None)
    tray = Tray(settings.wake_names if settings is not None else ["AJ"])
    connect(bridge, window, tray)
    window.closing.connect(_arm_exit)
    window.closed.connect(app.quit)
    if show_requests:
        switch.watch_for_show(bridge.show_requested.emit)
    tray.show()
    if getattr(engine, "wake_command", None) is not None:
        window.show_quietly()  # started by "Hey AJ, ..." from the listener: leave the keyboard where it is
    else:
        window.show()
    threading.Thread(target=_engine_thread, args=(engine, bridge), daemon=True, name="engine").start()
    return app.exec()
