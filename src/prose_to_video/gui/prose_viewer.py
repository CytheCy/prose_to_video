"""Read-only prose display with incremental insertion for large stories."""

from __future__ import annotations

from PySide6.QtCore import QTimer, Signal
from PySide6.QtGui import QColor, QTextCharFormat, QTextCursor
from PySide6.QtWidgets import QPlainTextEdit, QTextEdit


class ProseViewer(QPlainTextEdit):
    loaded = Signal()
    CHUNK_SIZE = 32_768

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setReadOnly(True)
        self.setPlaceholderText("Import a Markdown or plain text story to read it here.")
        self._generation = 0
        self._source_text = ""
        self._layers = {"scene": None, "shot": None, "playback": None}

    def show_text(self, text: str) -> None:
        """Insert a few chunks per event-loop turn, preserving character offsets."""
        self._generation += 1
        self._source_text = text
        self._layers = {"scene": None, "shot": None, "playback": None}
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
                    self.loaded.emit()
                    return
                chunk = text[position:position + self.CHUNK_SIZE]
                cursor.insertText(chunk)
                position += len(chunk)
            QTimer.singleShot(0, insert_more)

        QTimer.singleShot(0, insert_more)

    def highlight_range(self, start: int, end: int) -> None:
        self._layers = {"scene": (start, end), "shot": None, "playback": None}
        self._render_layers(scroll=True)

    def set_layer(self, name: str, span: tuple[int, int] | None, scroll: bool = False) -> None:
        if name not in self._layers:
            raise ValueError("Unknown prose highlight layer")
        self._layers[name] = span
        self._render_layers(scroll)

    def _render_layers(self, scroll: bool = False) -> None:
        spans = [(name, span) for name in ("playback", "shot", "scene")
                 if (span := self._layers[name]) is not None]
        selections = []
        higher = []
        for name, span in spans:
            visible = [span]
            for occupied in higher:
                next_visible = []
                for start, end in visible:
                    if occupied[1] <= start or occupied[0] >= end:
                        next_visible.append((start, end))
                    else:
                        if start < occupied[0]: next_visible.append((start, occupied[0]))
                        if occupied[1] < end: next_visible.append((occupied[1], end))
                visible = next_visible
            selections.extend(self._selection(start, end, name) for start, end in visible)
            higher.append(span)
        self.setExtraSelections(selections)
        if scroll and spans:
            cursor = selections[0].cursor
            cursor.clearSelection()
            self.setTextCursor(cursor)
            self.centerCursor()

    def _selection(self, start: int, end: int, name: str):
        if not 0 <= start < end <= len(self._source_text):
            raise ValueError("Highlight range is outside the displayed prose")

        def qt_position(offset: int) -> int:
            # Qt normalizes line endings and counts UTF-16 code units.
            portion = self._source_text[:offset].replace("\r\n", "\n").replace("\r", "\n")
            return len(portion.encode("utf-16-le")) // 2

        begin, finish = qt_position(start), qt_position(end)
        cursor = self.textCursor()
        cursor.setPosition(begin)
        cursor.setPosition(finish, QTextCursor.KeepAnchor)
        selection = QTextEdit.ExtraSelection()
        selection.cursor = cursor
        selection.format = QTextCharFormat()
        selection.format.setBackground(QColor({"scene":"#ffe28a", "shot":"#b5e3f4", "playback":"#ffa86b"}[name]))
        return selection

    def selected_source_range(self) -> tuple[int, int] | None:
        cursor = self.textCursor()
        if not cursor.hasSelection():
            return None
        # QTextDocument uses normalized newlines and UTF-16 positions.
        wanted = (cursor.selectionStart(), cursor.selectionEnd())
        positions = {}
        qt = 0
        offset = 0
        source = self._source_text
        while offset <= len(source) and len(positions) < 2:
            if qt in wanted:
                positions[qt] = offset
            if offset == len(source):
                break
            if source.startswith("\r\n", offset):
                offset += 2
                qt += 1
            else:
                char = source[offset]
                offset += 1
                qt += 1 if ord(char) <= 0xffff else 2
        if len(positions) != 2:
            return None
        return positions[wanted[0]], positions[wanted[1]]

    def clear_story(self, message: str | None = None) -> None:
        self._generation += 1
        self._source_text = ""
        self._layers = {"scene": None, "shot": None, "playback": None}
        self.clear()
        self.setExtraSelections([])
        self.setPlaceholderText(message or "Import a Markdown or plain text story to read it here.")
