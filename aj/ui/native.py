"""What the Qt Quick window needs from Windows and from its own folder, shared by the window
(ui/window.py) and the lab (ui/lab.py): the Inter font, the title bar in the background's colour,
Windows' accessibility settings, whether Qt is drawing without a GPU, and calling into the QML.
"""
import ctypes
import functools
import sys
from pathlib import Path

from PyQt6.QtCore import Q_ARG, Q_RETURN_ARG, QMetaObject, QObject
from PyQt6.QtGui import QColor, QFontDatabase, QGuiApplication, QWindow
from PyQt6.QtQuick import QQuickView, QQuickWindow, QSGRendererInterface

from aj.config import DATA_DIR

UI = Path(__file__).resolve().parent
QML_DIR = UI / "qml"
FONT = UI / "fonts" / "InterVariable.ttf"
FALLBACK_FONT = "Segoe UI Variable Text"  # Windows 11's own, if Inter won't load
TUNING = DATA_DIR / "orb_tuning.json"     # what the lab's Save tuning writes
BASE = "#0e0605"  # the background's darkest colour: what shows before the first frame


@functools.cache
def load_font() -> str:
    """Inter, from the app's own folder (loaded once per process). Returns the family name Qt
    gives it, which the QML is then told to use, or Windows' own font if Inter won't load (an
    unknown name would quietly become Tahoma)."""
    font_id = QFontDatabase.addApplicationFont(str(FONT))
    families = QFontDatabase.applicationFontFamilies(font_id) if font_id >= 0 else []
    if not families:
        print(f"Inter didn't load from {FONT}; using {FALLBACK_FONT}", flush=True)
        return FALLBACK_FONT
    return families[0]


def saved_tuning() -> str:
    """The lab's saved tuning (JSON), or "" for the built-in look (Theme.qml)."""
    try:
        return TUNING.read_text(encoding="utf-8")
    except OSError:
        return ""


def tint_title_bar(window: QWindow, colour: str = BASE) -> None:
    """Windows 11: the title bar in the background's colour with white text, so it blends in.
    Older Windows ignores the attributes; Qt's offscreen test platform has no real window."""
    if sys.platform != "win32" or QGuiApplication.platformName() != "windows":
        return
    hwnd = int(window.winId())
    c = QColor(colour)
    for attribute, value in ((35, c.red() | c.green() << 8 | c.blue() << 16),  # DWMWA_CAPTION_COLOR
                             (36, 0xFFFFFF)):                                   # DWMWA_TEXT_COLOR
        ref = ctypes.c_uint(value)
        ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, attribute, ctypes.byref(ref), ctypes.sizeof(ref))


def windows_prefers_less() -> tuple[bool, bool]:
    """(reduce motion, reduce transparency) from Windows' Accessibility settings: Animation
    effects and Transparency effects."""
    if sys.platform != "win32":
        return False, False
    animate = ctypes.c_int(1)
    ctypes.windll.user32.SystemParametersInfoW(0x1042, 0, ctypes.byref(animate), 0)  # SPI_GETCLIENTAREAANIMATION
    transparent = True
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize") as key:
            transparent = bool(winreg.QueryValueEx(key, "EnableTransparency")[0])
    except OSError:
        pass
    return not animate.value, not transparent


def software_rendering(view: QQuickView | None = None) -> bool:
    """True when Qt Quick draws without a GPU (QT_QUICK_BACKEND=software, the offscreen test
    platform, or no usable GPU): shaders don't run there, so plain shapes stand in."""
    if QQuickWindow.sceneGraphBackend() == "software":
        return True
    renderer = view.rendererInterface() if view is not None else None
    return renderer is not None and renderer.graphicsApi() == QSGRendererInterface.GraphicsApi.Software


def call(root: QObject, name: str, *args):
    """Call one of the QML root's functions (QML functions take and return variants)."""
    return QMetaObject.invokeMethod(root, name, Q_RETURN_ARG("QVariant"), *(Q_ARG("QVariant", a) for a in args))
