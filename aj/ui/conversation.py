"""The conversation the window shows: who said it, what was said, and the turn's one-line
latency summary. A list model the QML reads (qml/Face.qml); old messages fall off past
MAX_MESSAGES so a long session can't grow without bound.

Everything is shown as plain text (the QML uses Text.PlainText), so a message containing "<b>"
shows exactly that, and can never restyle the window.
"""
from PyQt6.QtCore import QAbstractListModel, QModelIndex, Qt

MAX_MESSAGES = 600
WHO, WORDS, TIMES = range(Qt.ItemDataRole.UserRole + 1, Qt.ItemDataRole.UserRole + 4)
YOU = "You"
ERROR = "Error"


class Conversation(QAbstractListModel):
    def __init__(self, name: str = "AJ", parent=None):
        super().__init__(parent)
        self.name = name  # how the assistant's own lines are labelled
        self._rows: list[tuple[str, str, str]] = []

    def roleNames(self) -> dict[int, bytes]:
        return {WHO: b"who", WORDS: b"words", TIMES: b"times"}

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self._rows)

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole):
        if not index.isValid() or not 0 <= index.row() < len(self._rows):
            return None
        who, words, times = self._rows[index.row()]
        return {WHO: who, WORDS: words, TIMES: times, Qt.ItemDataRole.DisplayRole: words}.get(role)

    def add(self, who: str, words: str, times: str = "") -> None:
        if len(self._rows) >= MAX_MESSAGES:
            drop = len(self._rows) - MAX_MESSAGES + 1
            self.beginRemoveRows(QModelIndex(), 0, drop - 1)
            del self._rows[:drop]
            self.endRemoveRows()
        self.beginInsertRows(QModelIndex(), len(self._rows), len(self._rows))
        self._rows.append((who, words, times))
        self.endInsertRows()

    def add_you(self, text: str) -> None:
        self.add(YOU, text)

    def add_aj(self, text: str, latency: str = "") -> None:
        self.add(self.name, text, latency)

    def add_error(self, message: str) -> None:
        self.add(ERROR, message)

    def plain_text(self) -> str:
        """The whole conversation as text: "you: ...", "aj: ...", each latency line under its
        reply, "error: ..."."""
        lines = []
        for who, words, times in self._rows:
            lines.append(f"{who.lower()}: {words}")
            if times:
                lines.append(times)
        return "\n".join(lines)
