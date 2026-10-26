"""Scene list and editable factual descriptions."""

from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (QComboBox, QFormLayout, QGroupBox, QHBoxLayout,
                               QLabel, QLineEdit, QListWidget, QPlainTextEdit, QDoubleSpinBox,
                               QProgressBar, QPushButton, QScrollArea, QVBoxLayout,
                               QWidget)

from prose_to_video.models.scene import Scene
from prose_to_video.models.alignment import AlignmentRun, SceneTiming
from prose_to_video.utils.media_info import format_duration


class ScenesPanel(QGroupBox):
    detect_requested = Signal()
    cancel_requested = Signal()
    export_requested = Signal()
    split_requested = Signal()
    merge_requested = Signal()
    save_requested = Signal()
    selected = Signal(str)
    align_requested = Signal()
    align_cancel_requested = Signal()
    play_scene_requested = Signal()
    correction_requested = Signal(float, float)
    transcript_requested = Signal()

    FIELDS = ("title", "summary", "location", "time_of_day", "characters",
              "point_of_view", "narrative_type", "environment", "important_objects",
              "actions", "dialogue_participants", "visual_atmosphere", "boundary_reason")
    LIST_FIELDS = {"characters", "important_objects", "actions", "dialogue_participants"}
    LONG_FIELDS = {"summary", "environment", "actions", "visual_atmosphere", "boundary_reason",
                   "characters", "important_objects", "dialogue_participants"}

    def __init__(self, parent=None):
        super().__init__("Scenes", parent)
        self.scenes: list[Scene] = []
        self.outdated = False
        self.alignment: AlignmentRun | None = None
        self.timings: dict[str, SceneTiming] = {}
        self.detect_button = QPushButton("Detect Scenes")
        self.align_button = QPushButton("Align Audio")
        self.align_cancel_button = QPushButton("Cancel Alignment")
        self.play_scene_button = QPushButton("Play Scene")
        self.correct_button = QPushButton("Save Scene Times")
        self.transcript_button = QPushButton("View Transcript")
        self.timing_label = QLabel("No audio alignment")
        self.timing_label.setWordWrap(True)
        self.review_text = QPlainTextEdit()
        self.review_text.setReadOnly(True)
        self.review_text.setPlaceholderText("Select a scene to inspect its transcript and mismatches.")
        self.review_text.setFixedHeight(150)
        self.start_time = QDoubleSpinBox()
        self.end_time = QDoubleSpinBox()
        for spin in (self.start_time, self.end_time):
            spin.setDecimals(3)
            spin.setRange(0, 864000)
            spin.setSuffix(" s")
        self.cancel_button = QPushButton("Cancel")
        self.export_button = QPushButton("Export Scenes…")
        self.split_button = QPushButton("Split Scene…")
        self.merge_button = QPushButton("Merge With Next Scene")
        self.save_button = QPushButton("Save Scene Details")
        self.review_status = QComboBox()
        self.review_status.addItems(["Not reviewed", "Reviewed", "Manually edited"])
        self.list_widget = QListWidget()
        self.stage_label = QLabel("")
        self.progress = QProgressBar()
        self.progress.setRange(0, 1)
        self.progress.setValue(0)
        self.fields = {}
        form = QFormLayout()
        for name in self.FIELDS:
            widget = QPlainTextEdit() if name in self.LONG_FIELDS else QLineEdit()
            if isinstance(widget, QPlainTextEdit):
                widget.setFixedHeight(65)
            if name in self.LIST_FIELDS:
                widget.setToolTip("One item per line")
            self.fields[name] = widget
            form.addRow(name.replace("_", " ").capitalize() + ":", widget)
        form.addRow("Review status:", self.review_status)
        detail = QWidget()
        detail.setLayout(form)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(detail)
        top = QHBoxLayout()
        for button in (self.detect_button, self.cancel_button, self.align_button,
                       self.align_cancel_button, self.export_button):
            top.addWidget(button)
        bottom = QHBoxLayout()
        for button in (self.split_button, self.merge_button, self.save_button):
            bottom.addWidget(button)
        layout = QVBoxLayout(self)
        layout.addLayout(top)
        layout.addWidget(self.stage_label)
        layout.addWidget(self.progress)
        layout.addWidget(self.list_widget, 2)
        layout.addWidget(self.timing_label)
        timing_buttons = QHBoxLayout()
        for widget in (self.play_scene_button, QLabel("Start"), self.start_time,
                       QLabel("End"), self.end_time, self.correct_button, self.transcript_button):
            timing_buttons.addWidget(widget)
        layout.addLayout(timing_buttons)
        layout.addWidget(self.review_text)
        layout.addWidget(scroll, 3)
        layout.addLayout(bottom)
        self.detect_button.clicked.connect(self.detect_requested)
        self.align_button.clicked.connect(self.align_requested)
        self.align_cancel_button.clicked.connect(self.align_cancel_requested)
        self.play_scene_button.clicked.connect(self.play_scene_requested)
        self.correct_button.clicked.connect(lambda: self.correction_requested.emit(
            self.start_time.value(), self.end_time.value()))
        self.transcript_button.clicked.connect(self.transcript_requested)
        self.cancel_button.clicked.connect(self.cancel_requested)
        self.export_button.clicked.connect(self.export_requested)
        self.split_button.clicked.connect(self.split_requested)
        self.merge_button.clicked.connect(self.merge_requested)
        self.save_button.clicked.connect(self.save_requested)
        self.list_widget.currentRowChanged.connect(self._selected)
        self.set_state(False, False)

    def _selected(self, index: int) -> None:
        scene = self.current_scene()
        self._show_scene(scene)
        self.selected.emit(scene.identifier if scene else "")

    def current_scene(self) -> Scene | None:
        index = self.list_widget.currentRow()
        return self.scenes[index] if 0 <= index < len(self.scenes) else None

    def set_scenes(self, scenes: list[Scene], outdated: bool = False, select_id: str | None = None) -> None:
        self.scenes = scenes
        self.outdated = outdated
        self.list_widget.clear()
        for scene in scenes:
            label = f"{scene.sequence}. {scene.title} — {scene.location or 'Setting unspecified'} [{scene.review_status}]"
            timing = self.timings.get(scene.identifier)
            if timing:
                label += (f" | {format_duration(timing.audio_start)}–{format_duration(timing.audio_end)}"
                          if timing.audio_start is not None else " | timing unresolved")
            self.list_widget.addItem(label)
        self.stage_label.setText("Scene analysis is outdated: prose changed" if outdated else "")
        index = next((i for i, scene in enumerate(scenes) if scene.identifier == select_id), 0)
        if scenes:
            self.list_widget.setCurrentRow(index)
        else:
            self._show_scene(None)

    def _show_scene(self, scene: Scene | None) -> None:
        for name, widget in self.fields.items():
            value = getattr(scene, name) if scene else ""
            display = "\n".join(value) if isinstance(value, list) else value or ""
            if isinstance(widget, QPlainTextEdit):
                widget.setPlainText(display)
            else:
                widget.setText(display)
        self.review_status.setCurrentText(scene.review_status if scene else "Not reviewed")
        timing = self.timings.get(scene.identifier) if scene else None
        if timing:
            duration = format_duration(timing.duration) if timing.duration is not None else "Unresolved"
            self.timing_label.setText(f"Start: {format_duration(timing.audio_start) if timing.audio_start is not None else 'Unresolved'}  "
                                      f"End: {format_duration(timing.audio_end) if timing.audio_end is not None else 'Unresolved'}  "
                                      f"Duration: {duration}  Coverage: {timing.coverage:.0%}  "
                                      f"Status: {timing.status}{' (manually corrected)' if timing.manual else ''}")
            self.start_time.setValue(timing.audio_start or 0)
            self.end_time.setValue(timing.audio_end or 0)
        else:
            self.timing_label.setText("No current audio alignment" if scene else "No scene selected")
            self.start_time.setValue(0)
            self.end_time.setValue(0)
        if scene and self.alignment:
            transcript = [s.text for s in self.alignment.transcript
                          if timing and timing.audio_start is not None and
                          s.end >= timing.audio_start and s.start <= timing.audio_end]
            unmatched = [w.text for w in self.alignment.words if scene.start <= w.source_start < scene.end
                         and w.status != "matched"]
            issues = [issue.message for issue in self.alignment.issues
                      if (issue.source_start is not None and scene.start <= issue.source_start < scene.end) or
                      (timing and timing.audio_start is not None and issue.audio_start is not None and
                       timing.audio_start <= issue.audio_start <= timing.audio_end)]
            self.review_text.setPlainText("Original prose:\n" + scene.text + "\n\nTranscribed speech:\n" +
                (" ".join(transcript) or "No matched transcript") + "\n\nUnmatched manuscript words:\n" +
                (" ".join(unmatched) or "None") + "\n\nWarnings:\n" +
                ("\n".join([*timing.warnings, *issues]) if timing else "None"))
        else:
            self.review_text.clear()

    def set_alignment(self, alignment: AlignmentRun | None) -> None:
        current = self.current_scene()
        self.alignment = alignment
        self.timings = {t.scene_id: t for t in alignment.scenes} if alignment else {}
        self.set_scenes(self.scenes, self.outdated, current.identifier if current else None)

    def edited_scene(self) -> Scene:
        current = self.current_scene()
        if current is None:
            raise ValueError("Select a scene")
        changes = {}
        for name, widget in self.fields.items():
            value = widget.toPlainText() if isinstance(widget, QPlainTextEdit) else widget.text()
            if name in self.LIST_FIELDS:
                changes[name] = [line.strip() for line in value.splitlines() if line.strip()]
            else:
                changes[name] = value.strip() or (None if name in {"location", "time_of_day", "point_of_view", "narrative_type"} else "")
        changed = any(getattr(current, key) != value for key, value in changes.items())
        changes["review_status"] = "Manually edited" if changed else self.review_status.currentText()
        return current.model_copy(update=changes)

    def set_state(self, available: bool, running: bool, audio_available: bool = False,
                  aligning: bool = False) -> None:
        selected = self.current_scene() is not None and not self.outdated
        busy = running or aligning
        self.detect_button.setEnabled(available and not busy)
        self.align_button.setEnabled(available and audio_available and not running and not aligning)
        self.align_cancel_button.setEnabled(aligning)
        timing = self.timings.get(self.current_scene().identifier) if self.current_scene() else None
        valid_time = bool(timing and timing.audio_start is not None and timing.audio_end is not None)
        self.play_scene_button.setEnabled(valid_time and not aligning and not self.outdated)
        self.play_scene_button.setToolTip("" if valid_time else "This scene has no reliable audio timestamps")
        self.correct_button.setEnabled(bool(timing) and not aligning and not self.outdated)
        self.transcript_button.setEnabled(bool(self.alignment) and not aligning)
        self.start_time.setEnabled(bool(timing) and not aligning)
        self.end_time.setEnabled(bool(timing) and not aligning)
        self.cancel_button.setEnabled(running)
        self.export_button.setEnabled(selected and not busy)
        self.split_button.setEnabled(selected and not busy)
        self.merge_button.setEnabled(selected and not busy and self.list_widget.currentRow() < len(self.scenes) - 1)
        self.save_button.setEnabled(selected and not busy)
        self.list_widget.setEnabled(not running)
        for widget in self.fields.values():
            widget.setEnabled(selected and not busy)
        self.review_status.setEnabled(selected and not busy)

    def set_progress(self, stage: str, current: int, total: int) -> None:
        self.stage_label.setText(f"{stage}: {current}/{total}")
        self.progress.setRange(0, max(1, total))
        self.progress.setValue(current)
