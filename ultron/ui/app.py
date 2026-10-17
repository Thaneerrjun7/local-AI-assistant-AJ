"""Start Qt, the window and the engine thread, and make sure closing really ends the process.

Qt owns the main thread. The engine (model loading and the conversation) runs on a thread
of its own and reports through a Bridge, so the window only ever changes on the Qt thread.

Closing the window turns Ultron off: the engine says goodbye, closes the audio and stops
llama-server, then the window closes and the process exits. If that hasn't happened within
OFF_GRACE_MS the process exits anyway (the job object in core/proc.py still takes
llama-server down). Closing while still loading exits at once: there's nothing to say
goodbye with yet, and the model load can't be interrupted part way.
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

from ultron.core import switch
from ultron.engine import Events
from ultron.ui import theme
from ultron.ui.bridge import Bridge
from ultron.ui.mic_button import app_icon
from ultron.ui.window import Controls, UltronWindow

OFF_GRACE_MS = 10_000  # goodbye (~3 s) plus stopping the model server, with room to spare


class Runnable(Controls, Protocol):
    """An engine the window can drive: ultron.engine.Engine, or the demo's stand-in."""

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
    """The QApplication, styled: Fusion + dark palette + the red stylesheet, Segoe UI, the icon."""
    if sys.platform == "win32":  # group under Ultron's own taskbar icon, not pythonw's
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("Ultron.Assistant")
    app = QApplication.instance() or QApplication(sys.argv[:1])
    app.setApplicationName("Ultron")
    app.setStyle("Fusion")
    app.setPalette(_palette())
    app.setFont(QFont(theme.FONT, 10))
    app.setStyleSheet(theme.STYLESHEET)
    app.setWindowIcon(app_icon())
    app.styleHints().setColorScheme(Qt.ColorScheme.Dark)  # dark title bar on Windows 11
    return app


def connect(bridge: Bridge, window: UltronWindow) -> None:
    bridge.status.connect(window.set_status)
    bridge.heard.connect(window.add_heard)
    bridge.turn.connect(window.add_turn)
    bridge.failed.connect(window.show_error)
    bridge.finished.connect(window.engine_finished)
    bridge.show_requested.connect(window.bring_to_front)
    bridge.confirm_requested.connect(lambda question: bridge.answer_confirm(window.confirm(question)))


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
    """Open the window and run `engine` behind it. Returns when Ultron is off."""
    signal.signal(signal.SIGINT, signal.SIG_DFL)  # Ctrl+C in a console ends it at once (Qt would swallow it)
    sys.excepthook = _log_exception
    app = make_app()
    bridge = Bridge()
    window = UltronWindow(engine)
    connect(bridge, window)
    window.closing.connect(_arm_exit)
    if show_requests:
        switch.watch_for_show(bridge.show_requested.emit)
    window.show()
    threading.Thread(target=_engine_thread, args=(engine, bridge), daemon=True, name="engine").start()
    return app.exec()
