"""The microphone drawn with QPainter, and the app icon made from it: the tray (ui/tray.py) and
AJ (Speak) draw their icons with it. Painted from a few shapes, so there are no image files to
ship. (The window's own mic button is QML now: qml/MicGlyph.qml.)
"""
from PyQt6.QtCore import QPointF, QRectF, Qt
from PyQt6.QtGui import QColor, QIcon, QPainter, QPen, QPixmap

from aj.ui import theme


def paint_mic(p: QPainter, box: QRectF, color: QColor, slashed: bool = False,
              behind: QColor | None = None) -> None:
    """Draw a microphone filling the square `box`. `slashed` adds the mic-off strike, edged in
    `behind` (the colour under the glyph) so the strike stays readable over the mic body."""
    w, x0, y0 = box.width(), box.left(), box.top()
    cx = x0 + w / 2
    stroke = w * 0.08
    pen = QPen(color, stroke, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap)

    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(color)
    capsule_w = w * 0.34
    p.drawRoundedRect(QRectF(cx - capsule_w / 2, y0 + w * 0.04, capsule_w, w * 0.54),
                      capsule_w / 2, capsule_w / 2)

    p.setPen(pen)
    p.setBrush(Qt.BrushStyle.NoBrush)
    holder_w = w * 0.6
    p.drawArc(QRectF(cx - holder_w / 2, y0 + w * 0.2, holder_w, w * 0.52), 180 * 16, 180 * 16)
    p.drawLine(QPointF(cx, y0 + w * 0.72), QPointF(cx, y0 + w * 0.9))
    p.drawLine(QPointF(cx - w * 0.18, y0 + w * 0.92), QPointF(cx + w * 0.18, y0 + w * 0.92))

    if slashed:
        start, end = QPointF(x0 + w * 0.1, y0 + w * 0.06), QPointF(x0 + w * 0.9, y0 + w * 0.94)
        if behind is not None:
            p.setPen(QPen(behind, stroke * 2.6, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
            p.drawLine(start, end)
        p.setPen(pen)
        p.drawLine(start, end)


def app_icon() -> QIcon:
    """A red disc with a white mic, at the sizes Windows asks for (taskbar, title bar, alt-tab)."""
    icon = QIcon()
    for size in (16, 24, 32, 48, 64, 256):
        pixmap = QPixmap(size, size)
        pixmap.fill(Qt.GlobalColor.transparent)
        p = QPainter(pixmap)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(theme.RED))
        p.drawEllipse(QRectF(0.5, 0.5, size - 1, size - 1))
        glyph = size * 0.56
        paint_mic(p, QRectF((size - glyph) / 2, (size - glyph) / 2, glyph, glyph), QColor("white"))
        p.end()
        icon.addPixmap(pixmap)
    return icon
