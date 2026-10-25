"""The scrolling conversation: "you:" and "aj:" lines, plus a small grey latency line per
turn. Old lines fall off past MAX_LINES so a long session can't grow without bound.

Everything said is escaped before it becomes rich text, so a transcript containing "<b>"
shows exactly that, and can never restyle the window.
"""
import html

from PyQt6.QtGui import QTextBlockFormat, QTextCursor
from PyQt6.QtWidgets import QTextEdit, QWidget

from aj.ui import theme

MAX_LINES = 600
TURN_GAP = 10  # px of space above each "you:" line, so turns read as separate blocks


def you_html(text: str) -> str:
    return f'<span style="color:{theme.MUTED}">you:</span> {html.escape(text)}'


def aj_html(text: str) -> str:
    return f'<span style="color:{theme.RED_BRIGHT}; font-weight:600">aj:</span> {html.escape(text)}'


def latency_html(report: str) -> str:
    return f'<span style="color:{theme.FAINT}; font-size:8pt">{html.escape(report)}</span>'


def error_html(message: str) -> str:
    return f'<span style="color:{theme.RED_BRIGHT}; font-weight:600">error:</span> {html.escape(message)}'


class Transcript(QTextEdit):
    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("transcript")
        self.setReadOnly(True)
        self.document().setMaximumBlockCount(MAX_LINES)
        self.setPlaceholderText("Talk, or type below. The conversation shows up here.")

    def add(self, line: str, gap: int = 0) -> None:
        """Append one line of (already escaped) rich text. Keeps the newest line in view unless
        you've scrolled up to read something."""
        bar = self.verticalScrollBar()
        follow = bar.value() >= bar.maximum() - 4
        cursor = QTextCursor(self.document())
        cursor.movePosition(QTextCursor.MoveOperation.End)
        block = QTextBlockFormat()
        if self.document().isEmpty():
            cursor.setBlockFormat(block)
        else:
            block.setTopMargin(gap)
            cursor.insertBlock(block)
        cursor.insertHtml(line)
        if follow:
            bar.setValue(bar.maximum())

    def add_you(self, text: str) -> None:
        self.add(you_html(text), gap=TURN_GAP)

    def add_aj(self, text: str, latency: str) -> None:
        self.add(aj_html(text), gap=2)
        if latency:
            self.add(latency_html(latency))

    def add_error(self, message: str) -> None:
        self.add(error_html(message), gap=TURN_GAP)
