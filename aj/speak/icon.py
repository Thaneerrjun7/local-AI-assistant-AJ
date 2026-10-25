"""AJ (Speak)'s icon: a dark disc, a red ring and a white mic, so it can't be mistaken on the desktop
for the assistant's own icon (a solid red disc, ui/mic_button.app_icon).

Painted from the same mic shape the assistant uses, so there is no picture to ship. The desktop
shortcut needs a real .ico file though, so write_ico() makes one (PNG frames inside an ICO, which
Windows has read since Vista): `python -m aj.speak --make-icon aj\\speak\\aj_speak.ico`.
"""
import struct
from pathlib import Path

from PyQt6.QtCore import QBuffer, QByteArray, QIODevice, QRectF, Qt
from PyQt6.QtGui import QColor, QIcon, QImage, QPainter, QPen, QPixmap

from aj.ui import theme
from aj.ui.mic_button import paint_mic

SIZES = (16, 24, 32, 48, 64, 128, 256)


def _image(size: int) -> QImage:
    image = QImage(size, size, QImage.Format.Format_ARGB32)
    image.fill(Qt.GlobalColor.transparent)
    p = QPainter(image)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    ring = max(1.0, size / 14)
    p.setPen(QPen(QColor(theme.RED_BRIGHT), ring))
    p.setBrush(QColor(theme.PANEL))
    inset = ring / 2 + 0.5
    p.drawEllipse(QRectF(inset, inset, size - 2 * inset, size - 2 * inset))
    glyph = size * 0.54
    paint_mic(p, QRectF((size - glyph) / 2, (size - glyph) / 2, glyph, glyph), QColor("white"))
    p.end()
    return image


def speak_icon() -> QIcon:
    """The window's icon, at the sizes Windows asks for. Needs a QGuiApplication."""
    icon = QIcon()
    for size in SIZES:
        icon.addPixmap(QPixmap.fromImage(_image(size)))
    return icon


def _png(size: int) -> bytes:
    data = QByteArray()
    buffer = QBuffer(data)
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    _image(size).save(buffer, "PNG")
    buffer.close()
    return bytes(data)


def ico_bytes() -> bytes:
    """An .ico file with every size in SIZES."""
    frames = [(size, _png(size)) for size in SIZES]
    out = struct.pack("<HHH", 0, 1, len(frames))
    offset = 6 + 16 * len(frames)
    for size, png in frames:
        out += struct.pack("<BBBBHHII", size % 256, size % 256, 0, 0, 1, 32, len(png), offset)  # 256 is stored as 0
        offset += len(png)
    return out + b"".join(png for _, png in frames)


def write_ico(path: str | Path) -> None:
    Path(path).write_bytes(ico_bytes())
