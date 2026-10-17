"""The big round mic button, and the app icon drawn from the same mic shape.

Painted from a few shapes, so there are no image files to ship. It repaints only when its
state changes or the pointer enters or leaves it: nothing animates, so it costs nothing idle.
Checked = listening (bright red), unchecked = mic off (grey, struck through).
"""
from PyQt6.QtCore import QPointF, QRectF, Qt
from PyQt6.QtGui import QColor, QIcon, QPainter, QPen, QPixmap
from PyQt6.QtWidgets import QAbstractButton, QWidget

from ultron.ui import theme


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


class MicButton(QAbstractButton):
    def __init__(self, diameter: int = 136, parent: QWidget | None = None):
        super().__init__(parent)
        self.setCheckable(True)
        self.setChecked(True)
        self.setFixedSize(diameter, diameter)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover)  # repaint on pointer enter/leave only
        self.setFocusPolicy(Qt.FocusPolicy.TabFocus)  # a mouse click shouldn't leave a focus ring
        self.setAccessibleName("Microphone")

    def hitButton(self, pos) -> bool:
        """Only the disc is clickable, not the square corners around it."""
        r = self.width() / 2
        return (pos.x() - r) ** 2 + (pos.y() - r) ** 2 <= r * r

    def _colours(self) -> tuple[QColor, QColor, QColor]:
        """(fill, ring, glyph) for the current state."""
        if not self.isEnabled():
            return QColor("#1b1b1f"), QColor(theme.BORDER), QColor(theme.DIM)
        if self.isChecked():
            fill, ring, glyph = QColor(theme.RED), QColor(theme.RED_BRIGHT), QColor("white")
        else:
            fill, ring, glyph = QColor(theme.OFF_FILL), QColor(theme.OFF_RING), QColor("#a4a4ac")
        if self.isDown():
            fill = fill.darker(125)
        elif self.underMouse():
            fill = fill.lighter(118)
        return fill, ring, glyph

    def paintEvent(self, event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        fill, ring, glyph = self._colours()
        outer = QRectF(self.rect()).adjusted(1, 1, -1, -1)
        if self.isEnabled() and self.isChecked():  # a soft halo, so "listening" reads at a glance
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(255, 42, 42, 38))
            p.drawEllipse(outer)
        body = outer.adjusted(11, 11, -11, -11)
        p.setPen(QPen(ring, 3))
        p.setBrush(fill)
        p.drawEllipse(body)
        if self.hasFocus() and self.isEnabled():  # keyboard users can see where Space will go
            p.setPen(QPen(QColor(theme.TEXT), 1, Qt.PenStyle.DotLine))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawEllipse(body.adjusted(-6, -6, 6, 6))
        size = body.width() * 0.44
        c = body.center()
        paint_mic(p, QRectF(c.x() - size / 2, c.y() - size / 2, size, size), glyph,
                  slashed=not self.isChecked(), behind=fill)
