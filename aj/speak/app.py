"""Start AJ (Speak): `pythonw -m aj.speak`.

One process, one small window, three helper threads that sleep until something happens: the
hotkey's message loop, the wait for a second launch, and the front-window watch (core/dictation's
FrontWatch). The microphone, the voice detector's model and the offline ears are not touched until
he speaks, and the offline ears only if Gemini fails.

It does not import the assistant (aj.ui.app pulls in the whole engine, models and all), so the
Qt set-up is repeated here in a few lines: the same Fusion style, dark palette and red stylesheet.

Logging: problems only (warnings and errors) go to speak.log in the data folder's logs folder, a
small rotating file. What he says is never logged. pythonw has no console, so anything that goes
wrong has to reach that file or it is invisible.

Closing the window ends the program. As with the assistant, the process then exits without Python's
own teardown: exit-time destructors in the speech libraries can stall it, and a closed window must
mean a finished process.
"""
import argparse
import ctypes
import logging
import os
import signal
import sys
import threading
from logging.handlers import RotatingFileHandler

from aj.config import LOG_DIR, Settings, load_settings
from aj.core import migrate

log = logging.getLogger(__name__)


def setup_logging() -> None:
    root = logging.getLogger()
    root.setLevel(logging.WARNING)
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(LOG_DIR / "speak.log", maxBytes=200_000, backupCount=1, encoding="utf-8")
    except OSError:
        return  # no log file: carry on without one
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root.addHandler(handler)
    sys.excepthook = lambda kind, value, tb: root.error("uncaught error", exc_info=(kind, value, tb))
    threading.excepthook = lambda a: root.error("uncaught error in a thread", exc_info=(a.exc_type, a.exc_value,
                                                                                         a.exc_traceback))


def make_app():
    """The QApplication, styled as the assistant's window is (ui/app.make_app, without the engine)."""
    from PyQt6.QtCore import Qt
    from PyQt6.QtGui import QColor, QFont, QPalette
    from PyQt6.QtWidgets import QApplication
    from aj.ui import theme
    from aj.speak.icon import speak_icon

    if sys.platform == "win32":
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("AJ.Speak")
    app = QApplication.instance() or QApplication(sys.argv[:1])
    app.setApplicationName("AJ (Speak)")
    app.setQuitOnLastWindowClosed(False)  # a tool window doesn't count as a last window; closing ends it explicitly
    app.setStyle("Fusion")
    palette = QPalette()
    for role, colour in ((QPalette.ColorRole.Window, theme.BG), (QPalette.ColorRole.Base, theme.PANEL),
                         (QPalette.ColorRole.Text, theme.TEXT), (QPalette.ColorRole.WindowText, theme.TEXT),
                         (QPalette.ColorRole.ButtonText, theme.TEXT), (QPalette.ColorRole.Button, theme.PANEL),
                         (QPalette.ColorRole.Highlight, theme.RED), (QPalette.ColorRole.HighlightedText, "#ffffff")):
        palette.setColor(role, QColor(colour))
    app.setPalette(palette)
    app.setFont(QFont(theme.FONT, 10))
    app.setStyleSheet(theme.STYLESHEET)
    app.setWindowIcon(speak_icon())
    app.styleHints().setColorScheme(Qt.ColorScheme.Dark)
    return app


def run(listen: bool = False) -> int:
    """`listen`: start a dictation as soon as the window is up (the assistant's "AJ, speak"). If
    AJ (Speak) is already running, that one is shown and, with `listen`, starts listening."""
    from aj.speak import store
    from aj.speak.hotkey import Hotkey, describe
    from aj.speak.instance import Instance
    from aj.speak.speaker import Speaker
    from aj.speak.window import SpeakWindow

    instance = Instance()
    if not instance.claim():
        instance.request_show(listen=listen)  # already running: show that one instead of starting a second
        return 0
    try:
        settings = load_settings()
    except Exception:  # noqa: BLE001 - a broken settings.toml shouldn't stop dictation
        log.exception("couldn't read settings.toml; using the defaults")
        settings = Settings()
    signal.signal(signal.SIGINT, signal.SIG_DFL)  # Ctrl+C in a console ends it at once (Qt would swallow it)
    app = make_app()
    speaker = Speaker(settings)
    window = SpeakWindow(speaker, store.load())
    window.quit_requested.connect(app.quit)
    instance.watch_for_show(window.show_requested.emit, window.listen_requested.emit)
    hotkey = Hotkey(window.hotkey_pressed.emit, avoid=[settings.hotkey])  # never the assistant's own
    chord = hotkey.start()
    window.set_hotkey(chord.label if chord else "", describe(chord))
    speaker.start()
    window.show()
    if listen:
        window.listen_requested.emit()  # handled once the event loop runs, with the window up
    code = app.exec()
    hotkey.stop()
    instance.release()
    return code


def make_icon(path: str) -> int:
    from PyQt6.QtWidgets import QApplication
    from aj.speak.icon import write_ico
    app = QApplication.instance() or QApplication(sys.argv[:1])  # noqa: F841 - Qt wants one to paint
    write_ico(path)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="aj.speak")
    parser.add_argument("--make-icon", metavar="PATH", help="write the shortcut's .ico file there and exit")
    parser.add_argument("--listen", action="store_true", help="start listening as soon as it opens")
    args = parser.parse_args(argv)
    moved = migrate.run()  # before the log opens in the data folder an old install may still have to move
    setup_logging()
    for note in moved:
        log.warning("rename to AJ: %s", note)
    try:
        code = make_icon(args.make_icon) if args.make_icon else run(listen=args.listen)
    except Exception:  # noqa: BLE001 - pythonw has no console: the log is the only place it can say why
        log.exception("AJ (Speak) stopped")
        code = 1
    logging.shutdown()
    if args.make_icon:
        return code
    os._exit(code)
