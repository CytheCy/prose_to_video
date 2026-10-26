"""Read-only prose display with incremental insertion for large stories."""

from __future__ import annotations

from PySide6.QtCore import QTimer
from PySide6.QtGui import QTextCursor
from PySide6.QtWidgets import QPlainTextEdit


class ProseViewer(QPlainTextEdit):
    CHUNK_SIZE = 32_768

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setReadOnly(True)
        self.setPlaceholderText("Import a Markdown or plain text story to read it here.")
        self._generation = 0

    def show_text(self, text: str) -> None:
        """Insert a few chunks per event-loop turn, preserving character offsets."""
        self._generation += 1
        generation = self._generation
        self.clear()
        cursor = self.textCursor()
        cursor.movePosition(QTextCursor.End)
        position = 0

        def insert_more() -> None:
            nonlocal position
            if generation != self._generation:
                return
            for _ in range(4):
                if position >= len(text):
                    self.moveCursor(QTextCursor.Start)
                    return
                chunk = text[position:position + self.CHUNK_SIZE]
                cursor.insertText(chunk)
                position += len(chunk)
            QTimer.singleShot(0, insert_more)

        QTimer.singleShot(0, insert_more)

    def clear_story(self, message: str | None = None) -> None:
        self._generation += 1
        self.clear()
        self.setPlaceholderText(message or "Import a Markdown or plain text story to read it here.")
