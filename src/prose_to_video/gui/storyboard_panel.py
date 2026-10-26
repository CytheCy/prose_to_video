"""Storyboard list, lightweight painted timeline, and structured shot editor."""
from __future__ import annotations
from PySide6.QtCore import Qt, QRectF, Signal
from PySide6.QtGui import QColor, QPainter
from PySide6.QtWidgets import (QComboBox, QFormLayout, QGridLayout, QGroupBox, QHBoxLayout, QLabel,
    QLineEdit, QListWidget, QPlainTextEdit, QPushButton, QScrollArea, QVBoxLayout, QWidget)
from prose_to_video.models.shot import CAMERA_MOVEMENTS, SHOT_TYPES, Storyboard, Shot
from prose_to_video.utils.media_info import format_duration

class ShotTimeline(QWidget):
    selected = Signal(str)
    def __init__(self, parent=None):
        super().__init__(parent)
        self.board: Storyboard | None = None
        self.selected_id = ""
        self.position = 0.0
        self.setMinimumHeight(74)

    def set_board(self, board):
        self.board = board
        self.update()

    def set_active(self, identifier: str, position: float):
        if (identifier, position) != (self.selected_id, self.position):
            self.selected_id, self.position = identifier, position
            self.update()

    def _rects(self):
        board = self.board
        if not board or board.coverage_start is None or board.coverage_end is None or board.coverage_end <= board.coverage_start:
            return []
        total = board.coverage_end - board.coverage_start
        return [(shot, QRectF(5 + (shot.audio_start-board.coverage_start)/total*(self.width()-10), 18,
                max(2, (shot.audio_end-shot.audio_start)/total*(self.width()-10)), 38))
                for shot in board.shots if shot.audio_start is not None and shot.audio_end is not None]

    def paintEvent(self, event):
        p = QPainter(self)
        p.fillRect(self.rect(), QColor("#20242d"))
        rects = self._rects()
        if not rects:
            p.setPen(QColor("#ddd")); p.drawText(self.rect(), Qt.AlignCenter, "Timing unresolved — draft storyboard")
            return
        for shot, rect in rects:
            p.fillRect(rect, QColor("#f6bd60") if shot.identifier == self.selected_id else QColor("#4f86ad"))
            p.setPen(QColor("#101820")); p.drawText(rect.adjusted(3, 0, -2, 0), Qt.AlignVCenter, str(shot.sequence))
        board = self.board
        if board.coverage_start <= self.position <= board.coverage_end:
            x = 5 + (self.position-board.coverage_start)/(board.coverage_end-board.coverage_start)*(self.width()-10)
            p.setPen(QColor("#ff595e")); p.drawLine(int(x), 2, int(x), self.height()-2)

    def mousePressEvent(self, event):
        for shot, rect in self._rects():
            if rect.contains(event.position()):
                self.selected.emit(shot.identifier)
                break

class StoryboardPanel(QGroupBox):
    selected = Signal(str)
    generate_requested = Signal()
    generate_all_requested = Signal()
    generate_selected_requested = Signal()
    cancel_requested = Signal()
    settings_requested = Signal()
    save_requested = Signal()
    split_requested = Signal()
    merge_requested = Signal()
    reorder_requested = Signal()
    insert_requested = Signal()
    delete_requested = Signal()
    play_requested = Signal()
    export_requested = Signal()
    FIELDS = ("title", "visual_description", "foreground", "background", "environment",
              "character_actions", "camera_framing", "camera_angle", "camera_position", "lighting",
              "atmosphere", "transition", "suggested_motion", "ai_notes", "characters",
              "character_appearances", "objects", "entity_refs")
    LONG = {"visual_description", "foreground", "background", "environment", "character_actions", "ai_notes",
            "characters", "character_appearances", "objects", "entity_refs"}
    LISTS = {"characters", "objects", "entity_refs"}
    def __init__(self, parent=None):
        super().__init__("Storyboard", parent)
        self.board: Storyboard | None = None
        self.status = QLabel("Select a narrative scene")
        self.status.setWordWrap(True)
        self.generate_button = QPushButton("Generate Scene")
        self.generate_all_button = QPushButton("Generate All")
        self.generate_selected_button = QPushButton("Generate Selected…")
        self.cancel_button = QPushButton("Cancel")
        self.settings_button = QPushButton("Settings…")
        self.export_button = QPushButton("Export…")
        self.list_widget = QListWidget()
        self.timeline = ShotTimeline()
        self.preview = QLabel("Image preview will be added in a later stage")
        self.preview.setAlignment(Qt.AlignCenter)
        self.preview.setMinimumHeight(60)
        self.preview.setStyleSheet("background:#30343b;color:#ddd")
        self.original_prose = QPlainTextEdit(); self.original_prose.setReadOnly(True)
        self.original_prose.setPlaceholderText("Exact source passage")
        self.original_prose.setFixedHeight(75)
        self.fields = {}
        form = QFormLayout()
        for name in self.FIELDS:
            widget = QPlainTextEdit() if name in self.LONG else QLineEdit()
            if isinstance(widget, QPlainTextEdit): widget.setFixedHeight(62)
            self.fields[name] = widget
            form.addRow(name.replace("_", " ").capitalize(), widget)
        self.shot_type = QComboBox(); self.shot_type.addItems(SHOT_TYPES)
        self.camera_movement = QComboBox(); self.camera_movement.addItems(CAMERA_MOVEMENTS)
        self.review_status = QComboBox(); self.review_status.addItems(("Not reviewed", "Reviewed", "Manually edited"))
        self.audio_start = QLineEdit(); self.audio_start.setPlaceholderText("Unresolved")
        self.audio_end = QLineEdit(); self.audio_end.setPlaceholderText("Unresolved")
        for label, widget in (("Shot type", self.shot_type), ("Camera movement", self.camera_movement),
                              ("Review status", self.review_status), ("Audio start (s)", self.audio_start),
                              ("Audio end (s)", self.audio_end)):
            form.addRow(label, widget)
        editor = QWidget(); editor.setLayout(form)
        scroll = QScrollArea(); scroll.setWidgetResizable(True); scroll.setWidget(editor)
        self.save_button = QPushButton("Save Shot")
        self.split_button = QPushButton("Split…")
        self.merge_button = QPushButton("Merge Next")
        self.reorder_button = QPushButton("Move Visual Down")
        self.insert_button = QPushButton("Insert…")
        self.delete_button = QPushButton("Delete…")
        self.play_button = QPushButton("Play Shot")
        top = QGridLayout()
        for i, button in enumerate((self.generate_button, self.generate_selected_button, self.generate_all_button,
                                    self.cancel_button, self.settings_button, self.export_button)):
            top.addWidget(button, i // 3, i % 3)
        actions = QGridLayout()
        for i, button in enumerate((self.save_button, self.split_button, self.merge_button, self.reorder_button,
                                    self.insert_button, self.delete_button, self.play_button)):
            actions.addWidget(button, i // 4, i % 4)
        layout = QVBoxLayout(self)
        layout.addLayout(top); layout.addWidget(self.status); layout.addWidget(self.timeline)
        layout.addWidget(self.list_widget, 2); layout.addWidget(self.preview); layout.addWidget(scroll, 3); layout.addLayout(actions)
        layout.addWidget(self.original_prose)
        self.list_widget.currentRowChanged.connect(self._selected)
        self.timeline.selected.connect(self.select_id)
        for button, signal in ((self.generate_button,self.generate_requested),(self.generate_selected_button,self.generate_selected_requested),
            (self.generate_all_button,self.generate_all_requested),
            (self.cancel_button,self.cancel_requested),(self.settings_button,self.settings_requested),
            (self.export_button,self.export_requested),(self.save_button,self.save_requested),
            (self.split_button,self.split_requested),(self.merge_button,self.merge_requested),
            (self.reorder_button,self.reorder_requested),
            (self.insert_button,self.insert_requested),(self.delete_button,self.delete_requested),
            (self.play_button,self.play_requested)):
            button.clicked.connect(signal)
        self.set_board(None)

    def current_shot(self) -> Shot | None:
        row = self.list_widget.currentRow()
        return self.board.shots[row] if self.board and 0 <= row < len(self.board.shots) else None

    def set_board(self, board: Storyboard | None, status: str = ""):
        selected = self.current_shot().identifier if self.current_shot() else None
        self.board = board
        self.timeline.set_board(board)
        self.list_widget.clear()
        if board:
            for shot in board.shots:
                timing = (f"{format_duration(shot.audio_start)}–{format_duration(shot.audio_end)} "
                    f"({format_duration(shot.duration)})" if shot.duration is not None else "Timing unresolved")
                self.list_widget.addItem(f"{shot.sequence}. {shot.title} | {timing} | {shot.shot_type} | "
                    f"{shot.review_status} | {shot.visual_description[:90]}")
        self.status.setText(status or (f"{board.status.capitalize()} · {len(board.shots)} shots" if board else "No storyboard for this scene"))
        self.select_id(selected) if selected and board else self.list_widget.setCurrentRow(0 if board else -1)
        for button in (self.save_button,self.split_button,self.merge_button,self.reorder_button,self.insert_button,self.delete_button,self.play_button,self.export_button):
            button.setEnabled(bool(board))

    def select_id(self, identifier: str | None):
        if not self.board or not identifier: return
        i = next((i for i,s in enumerate(self.board.shots) if s.identifier == identifier), None)
        if i is not None and self.list_widget.currentRow() != i: self.list_widget.setCurrentRow(i)

    def _selected(self, row):
        shot = self.current_shot()
        for name, widget in self.fields.items():
            value = getattr(shot, name) if shot else ""
            if isinstance(value, list): value = "\n".join(value)
            if isinstance(value, dict): value = "\n".join(f"{k}: {v}" for k,v in value.items())
            widget.setPlainText(value) if isinstance(widget, QPlainTextEdit) else widget.setText(value)
        self.shot_type.setCurrentText(shot.shot_type if shot else "Medium")
        self.camera_movement.setCurrentText(shot.camera_movement if shot else "Static")
        self.review_status.setCurrentText(shot.review_status if shot else "Not reviewed")
        self.audio_start.setText(str(shot.audio_start) if shot and shot.audio_start is not None else "")
        self.audio_end.setText(str(shot.audio_end) if shot and shot.audio_end is not None else "")
        self.original_prose.setPlainText(shot.original_prose if shot else "")
        if shot: self.timeline.set_active(shot.identifier, self.timeline.position)
        self.selected.emit(shot.identifier if shot else "")

    def changes(self):
        data = {name: widget.toPlainText().strip() if isinstance(widget, QPlainTextEdit) else widget.text().strip()
                for name, widget in self.fields.items()}
        for name in self.LISTS:
            data[name] = [line.strip() for line in data[name].splitlines() if line.strip()]
        appearance = {}
        for line in data["character_appearances"].splitlines():
            if line.strip():
                if ":" not in line: raise ValueError("Character appearances must use Name: description")
                name, description = line.split(":",1)
                appearance[name.strip()] = description.strip()
        data["character_appearances"] = appearance
        data.update(shot_type=self.shot_type.currentText(), camera_movement=self.camera_movement.currentText(),
                    review_status=self.review_status.currentText())
        for name, widget in (("audio_start",self.audio_start),("audio_end",self.audio_end)):
            data[name] = float(widget.text()) if widget.text().strip() else None
        data["timing_status"] = "fully timed" if data["audio_start"] is not None and data["audio_end"] is not None else "partially timed" if any(data[n] is not None for n in ("audio_start","audio_end")) else "untimed"
        return data

    def playback_position(self, seconds: float):
        board = self.board
        if not board: return
        active = next((s for s in board.shots if s.audio_start is not None and s.audio_end is not None
                       and s.audio_start <= seconds < s.audio_end), None)
        active_id = active.identifier if active else ""
        if active_id != self.timeline.selected_id:
            self.timeline.set_active(active_id, seconds)
            if active: self.select_id(active_id)
        elif abs(seconds - self.timeline.position) > .1:
            self.timeline.set_active(active_id, seconds)
