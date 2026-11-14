from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import signal
import sys
import tempfile

from PySide6.QtCore import Qt, QProcess, QProcessEnvironment, QTimer, QUrl, Signal, QSignalBlocker
from PySide6.QtGui import QAction, QColor, QIcon, QPainter, QPen, QPolygonF
from PySide6.QtCore import QPointF
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QLineEdit, QFileDialog, QTableWidget, QTableWidgetItem, QHeaderView, QAbstractItemView,
    QSplitter, QGroupBox, QFormLayout, QComboBox, QProgressBar, QMessageBox,
    QDoubleSpinBox, QCheckBox, QPlainTextEdit, QSizePolicy,
    QScrollArea,
    QLayout,
)

from .core import Project, parse_prose, validate_export, fingerprint


def clock(seconds):
    if seconds is None:
        return "—"
    minutes, secs = divmod(max(0., seconds), 60)
    return f"{int(minutes):02d}:{secs:06.3f}"


class FitScrollArea(QScrollArea):
    """Keep transport visible when space permits; scroll on small displays."""
    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self.widget():
            minimum = self.widget().minimumSizeHint()
            self.widget().resize(max(minimum.width(), self.viewport().width()),
                                 max(minimum.height(), self.viewport().height()))


class PathPicker(QWidget):
    chosen = Signal(str)

    def __init__(self, title, directory=False, filters="All files (*)"):
        super().__init__()
        self.title, self.directory, self.filters = title, directory, filters
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        self.edit = QLineEdit()
        self.edit.setReadOnly(True)
        self.edit.setPlaceholderText(title)
        self.button = QPushButton("Browse…")
        self.button.clicked.connect(self.browse)
        row.addWidget(self.edit, 1)
        row.addWidget(self.button)

    def browse(self):
        if self.directory:
            path = QFileDialog.getExistingDirectory(self, self.title, self.edit.text())
        else:
            path, _ = QFileDialog.getOpenFileName(self, self.title, self.edit.text(), self.filters)
        if path:
            self.chosen.emit(path)


class Waveform(QWidget):
    seek = Signal(float)
    boundary = Signal(str, float)

    def __init__(self):
        super().__init__()
        self.setMinimumHeight(105)
        self.setAccessibleName("Audio waveform. Drag chapter start and end markers; click to seek.")
        self.project = Project()
        self.selected = None
        self.position = 0.
        self.zoom = False
        self.drag = None
        self.setMouseTracking(True)

    def bounds(self):
        if self.zoom and self.selected and self.selected.start is not None and self.selected.end is not None:
            start, end = self.project.cut_points(self.selected)
            return max(0., start - 4), min(self.project.duration, end + 4)
        return 0., max(self.project.duration, 1.)

    def x(self, t):
        a, b = self.bounds()
        return 12 + (t - a) / max(.001, b - a) * (self.width() - 24)

    def time_at(self, x):
        a, b = self.bounds()
        return max(a, min(b, a + (x - 12) / max(1, self.width() - 24) * (b - a)))

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        palette = self.palette()
        painter.fillRect(self.rect(), palette.base())
        a, b = self.bounds()
        top, bottom = 28, self.height() - 24
        mid = (top + bottom) / 2
        accent = palette.highlight().color()
        for s in self.project.sections:
            if s.included and s.start is not None and s.end is not None:
                tint = QColor(accent)
                tint.setAlpha(42 if s is self.selected else 16)
                start, end = self.project.cut_points(s)
                painter.fillRect(int(self.x(start)), top, max(1, int(self.x(end) - self.x(start))), bottom - top, tint)
        values = self.project.waveform
        if values:
            painter.setPen(QPen(accent, 1.2))
            for px in range(12, self.width() - 12, 2):
                t = self.time_at(px)
                i = min(len(values) - 1, int(t / max(1., self.project.duration) * len(values)))
                amp = max(1., values[i] * (bottom - top) / 2)
                painter.drawLine(QPointF(px, mid - amp), QPointF(px, mid + amp))
        else:
            painter.setPen(palette.text().color())
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "Analyze your audio to see the waveform and chapter markers")
        if self.selected:
            for key, label in (("start", "IN"), ("end", "OUT")):
                t = self.project.cut_points(self.selected)[0 if key == "start" else 1]
                if t is None:
                    continue
                x = self.x(t)
                color = QColor("#248b68") if key == "start" else QColor("#d17b32")
                painter.setPen(QPen(color, 2))
                painter.drawLine(QPointF(x, 20), QPointF(x, bottom))
                painter.drawText(int(x) - 14, 16, label)
        painter.setPen(QPen(palette.text().color(), 1))
        x = self.x(self.position)
        if 12 <= x <= self.width() - 12:
            painter.drawLine(QPointF(x, top), QPointF(x, bottom))
        painter.setPen(palette.text().color())
        painter.drawText(12, self.height() - 6, clock(a))
        painter.drawText(self.width() - 98, self.height() - 6, clock(b))

    def mousePressEvent(self, event):
        if event.button() != Qt.MouseButton.LeftButton:
            return
        x = event.position().x()
        if self.selected:
            for key in ("start", "end"):
                t = self.project.cut_points(self.selected)[0 if key == "start" else 1]
                if t is not None and abs(x - self.x(t)) < 9:
                    self.drag = key
                    return
        self.seek.emit(self.time_at(x))

    def mouseMoveEvent(self, event):
        if self.drag:
            self.boundary.emit(self.drag, self.time_at(event.position().x()))

    def mouseReleaseEvent(self, event):
        self.drag = None


class Window(QMainWindow):
    def __init__(self, project_path=None):
        super().__init__()
        self.project = Project()
        self.project_path = None
        self.dirty = False
        self.process = None
        self.buffer = b""
        self.stderr = ""
        self.failed = False
        self.cancelled = False
        self.play_until = None
        self.playback_mode = None
        self.preview_offset = None
        self.preview_process = None
        self.preview_files = tempfile.TemporaryDirectory(prefix="podcast-splitter-preview-")
        self.preview_serial = 0
        self.preview_timer = QTimer(self)
        self.preview_timer.setSingleShot(True)
        self.preview_timer.setInterval(180)
        self.preview_timer.timeout.connect(self.render_preview)
        self.setWindowTitle("Podcast Splitter")
        self.setWindowIcon(QIcon.fromTheme("audio-x-generic"))
        self.resize(1180, 880)
        self.audio_output = QAudioOutput(self)
        self.audio_output.setVolume(.8)
        self.player = QMediaPlayer(self)
        self.player.setAudioOutput(self.audio_output)
        self.player.positionChanged.connect(self.position_changed)
        self.player.errorOccurred.connect(lambda *_: self.message.setText(f"Playback: {self.player.errorString()}"))
        self.player.playbackStateChanged.connect(self.play_state)
        self.cutoff_timer = QTimer(self)
        self.cutoff_timer.setInterval(10)
        self.cutoff_timer.timeout.connect(lambda: self.check_cutoff(self.player.position() / 1000 + (self.preview_offset or 0)))
        self.cutoff_timer.start()
        self.build_ui()
        if project_path:
            self.load_project(project_path)

    def build_ui(self):
        file_menu = self.menuBar().addMenu("&File")
        for label, shortcut, callback in (("Open project…", "Ctrl+O", self.open_project),
                                          ("Save project", "Ctrl+S", self.save_project),
                                          ("Save project as…", "Ctrl+Shift+S", lambda: self.save_project(True))):
            action = QAction(label, self)
            action.setShortcut(shortcut)
            action.triggered.connect(callback)
            file_menu.addAction(action)
        self.file_menu = file_menu
        help_menu = self.menuBar().addMenu("&Help")
        about = help_menu.addAction("About / workflow")
        about.triggered.connect(lambda: QMessageBox.information(self, "Podcast Splitter",
            "Choose Markdown prose, narration audio, and an export folder.\n\n"
            "Select the sections you want, then Analyze. The first run downloads a speech model. "
            "Audio and prose are processed locally.\n\n"
            "Preview cuts, adjust the IN/OUT markers, and approve any flagged sections. "
            "Export creates one file per selected heading. Music overlapping speech cannot be removed by trimming.\n\n"
            "Models and transcripts are cached in ~/.cache/podcast-splitter.\nVersion 0.1.3 · MIT license"))
        root = QWidget()
        root.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Ignored)
        scroll = FitScrollArea()
        scroll.setWidgetResizable(False)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        scroll.setWidget(root)
        self.setCentralWidget(scroll)
        layout = QVBoxLayout(root)
        layout.setSizeConstraint(QLayout.SizeConstraint.SetMinAndMaxSize)
        layout.setContentsMargins(20, 10, 20, 10)
        layout.setSpacing(7)
        title = QLabel("Podcast Splitter")
        title.setStyleSheet("font-size: 26px; font-weight: 650;")
        layout.addWidget(title)
        subtitle = QLabel("Turn your narrated prose into chapters. Match, review, export.")
        subtitle.setForegroundRole(self.palette().ColorRole.PlaceholderText)
        layout.addWidget(subtitle)
        sources = QGroupBox("1   Source files")
        form = QFormLayout(sources)
        form.setVerticalSpacing(4)
        self.prose_picker = PathPicker("Choose prose", filters="Markdown / text (*.md *.markdown *.txt);;All files (*)")
        self.audio_picker = PathPicker("Choose narration", filters="Audio (*.mp3 *.wav *.flac *.m4a *.ogg *.opus);;All files (*)")
        self.output_picker = PathPicker("Choose export folder", directory=True)
        for name, picker in (("Prose", self.prose_picker), ("Audio", self.audio_picker), ("Export to", self.output_picker)):
            form.addRow(name, picker)
        self.prose_picker.chosen.connect(self.choose_prose)
        self.audio_picker.chosen.connect(self.choose_audio)
        self.output_picker.chosen.connect(self.choose_output)
        layout.addWidget(sources)
        tools = QHBoxLayout()
        tools.addWidget(QLabel("Speech model"))
        self.model = QComboBox()
        for label, value in (("Tiny · fastest", "tiny.en"), ("Base · balanced", "base.en"), ("Small · more accurate", "small.en"), ("Medium · slower, more RAM", "medium.en")):
            self.model.addItem(label, value)
        self.model.setCurrentIndex(1)
        tools.addWidget(self.model)
        self.analyze_button = QPushButton("Analyze audio")
        self.analyze_button.setIcon(QIcon.fromTheme("system-search"))
        self.analyze_button.clicked.connect(self.analyze)
        tools.addWidget(self.analyze_button)
        tools.addSpacing(16)
        tools.addWidget(QLabel("Widen all parts"))
        self.part_padding = QDoubleSpinBox()
        self.part_padding.setRange(0, 86400)
        self.part_padding.setDecimals(3)
        self.part_padding.setSingleStep(.05)
        self.part_padding.setSuffix(" s per end")
        self.part_padding.setKeyboardTracking(False)
        self.part_padding.setToolTip("Move every IN earlier and every OUT later by this amount. "
                                     "Stops at the source limits; adjacent parts may share audio. "
                                     "Set to zero to restore the original cuts.")
        self.part_padding.valueChanged.connect(self.padding_changed)
        tools.addWidget(self.part_padding)
        tools.addStretch()
        self.summary = QLabel("Choose your source files to begin")
        tools.addWidget(self.summary)
        layout.addLayout(tools)
        splitter = QSplitter(Qt.Orientation.Horizontal)
        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(["Chapter / include", "Start", "End", "Duration", "Match"])
        self.table.verticalHeader().hide()
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        self.table.setMinimumWidth(480)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for col in range(1, 4):
            self.table.horizontalHeader().setSectionResizeMode(col, QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(4, QHeaderView.ResizeMode.Fixed)
        self.table.setColumnWidth(4, 145)
        self.table.itemSelectionChanged.connect(self.selection_changed)
        self.table.itemChanged.connect(self.include_changed)
        splitter.addWidget(self.table)
        self.editor = QGroupBox("2   Review selected chapter")
        review = QVBoxLayout(self.editor)
        review.setSpacing(5)
        self.chapter_title = QLabel("Select a chapter")
        self.chapter_title.setWordWrap(True)
        self.chapter_title.setStyleSheet("font-weight: 600; font-size: 15px;")
        review.addWidget(self.chapter_title)
        self.chapter_status = QLabel("")
        self.chapter_status.setWordWrap(True)
        review.addWidget(self.chapter_status)
        times = QFormLayout()
        self.start = QDoubleSpinBox()
        self.end = QDoubleSpinBox()
        for spin in (self.start, self.end):
            spin.setDecimals(3)
            spin.setSingleStep(.05)
            spin.setSuffix(" s")
            spin.setKeyboardTracking(False)
        times.addRow("Start (IN)", self.start)
        times.addRow("End (OUT)", self.end)
        review.addLayout(times)
        self.start.valueChanged.connect(lambda v: self.set_boundary("start", v))
        self.end.valueChanged.connect(lambda v: self.set_boundary("end", v))
        preview = QHBoxLayout()
        self.preview_start = QPushButton("Hear start")
        self.preview_end = QPushButton("Hear end")
        self.preview_start.setToolTip("Hear the retained audio starting exactly at IN")
        self.preview_end.setToolTip("Hear the retained audio ending exactly at OUT")
        self.preview_start.clicked.connect(lambda: self.audition(False))
        self.preview_end.clicked.connect(lambda: self.audition(True))
        preview.addWidget(self.preview_start)
        preview.addWidget(self.preview_end)
        review.addLayout(preview)
        self.approve = QCheckBox("I reviewed and approve these cut points")
        self.approve.toggled.connect(self.approve_changed)
        review.addWidget(self.approve)
        self.prose_preview = QPlainTextEdit()
        self.prose_preview.setReadOnly(True)
        self.prose_preview.setMinimumHeight(70)
        self.prose_preview.setPlaceholderText("The selected section’s prose appears here.")
        review.addWidget(self.prose_preview, 1)
        splitter.addWidget(self.editor)
        splitter.setSizes([740, 350])
        layout.addWidget(splitter, 1)
        self.wave = Waveform()
        self.wave.seek.connect(self.seek)
        self.wave.boundary.connect(self.set_boundary)
        layout.addWidget(self.wave)
        transport = QHBoxLayout()
        self.play = QPushButton("Play chapter")
        self.play.setIcon(QIcon.fromTheme("media-playback-start"))
        self.play.clicked.connect(self.toggle_play)
        transport.addWidget(self.play)
        self.position_label = QLabel("00:00.000 / 00:00.000")
        transport.addWidget(self.position_label)
        transport.addStretch()
        self.zoom = QCheckBox("Zoom to selected chapter")
        self.zoom.toggled.connect(self.zoom_changed)
        transport.addWidget(self.zoom)
        layout.addLayout(transport)
        footer = QHBoxLayout()
        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        footer.addWidget(self.progress, 1)
        self.cancel_button = QPushButton("Cancel")
        self.cancel_button.setEnabled(False)
        self.cancel_button.clicked.connect(self.cancel)
        footer.addWidget(self.cancel_button)
        self.format = QComboBox()
        self.format.addItems(["MP3", "FLAC", "WAV"])
        footer.addWidget(self.format)
        self.export_button = QPushButton("Export chapters")
        self.export_button.setIcon(QIcon.fromTheme("document-export"))
        self.export_button.clicked.connect(self.export)
        footer.addWidget(self.export_button)
        layout.addLayout(footer)
        self.message = QLabel("Ready. Your audio stays on this computer; models download on first use.")
        self.message.setWordWrap(True)
        layout.addWidget(self.message)
        self.editor.setEnabled(False)
        self.setStyleSheet("QGroupBox { font-weight: 600; } QGroupBox QLabel, QGroupBox QCheckBox { font-weight: 400; } QPushButton { padding: 3px 10px; } QLineEdit { padding: 2px; } QTableWidget { border-radius: 5px; }")

    def error(self, message):
        QMessageBox.warning(self, "Podcast Splitter", str(message))

    def choose_prose(self, path):
        try:
            sections = parse_prose(path)
        except Exception as e:
            self.error(e)
            return
        self.project.prose, self.project.sections = path, sections
        self.project.prose_fingerprint = ""
        self.dirty = True
        self.refresh()

    def choose_audio(self, path):
        if path == self.project.audio:
            return
        self.cancel_preview_render()
        self.player.pause()
        self.playback_mode = self.preview_offset = self.play_until = None
        self.project.audio = path
        self.project.duration = 0
        self.project.waveform = []
        self.project.audio_fingerprint = ""
        for s in self.project.sections:
            s.start = s.end = None
            s.status, s.confidence, s.reviewed = "Not analyzed", 0., False
        self.dirty = True
        self.player.setSource(QUrl.fromLocalFile(path))
        self.refresh()

    def choose_output(self, path):
        self.project.output = path
        self.output_picker.edit.setText(path)
        self.dirty = True

    def refresh(self):
        self.prose_picker.edit.setText(self.project.prose)
        self.audio_picker.edit.setText(self.project.audio)
        self.output_picker.edit.setText(self.project.output)
        self.model.setCurrentIndex(max(0, self.model.findData(self.project.model)))
        with QSignalBlocker(self.part_padding):
            self.part_padding.setMaximum(max(86400, self.project.part_padding))
            self.part_padding.setValue(self.project.part_padding)
        row = max(0, self.table.currentRow())
        with QSignalBlocker(self.table):
            self.table.setRowCount(len(self.project.sections))
            for i, s in enumerate(self.project.sections):
                self.update_row(i, s)
        chosen = [s for s in self.project.sections if s.included]
        flagged = sum(s.status != "Matched" and not s.reviewed for s in chosen)
        self.summary.setText(f"{len(chosen)} selected · {flagged} need review")
        self.wave.project = self.project
        if self.project.sections:
            self.table.selectRow(min(row, len(self.project.sections) - 1))
        self.selection_changed()
        self.wave.update()
        self.position_changed(self.player.position())

    def update_row(self, i, s):
        title = QTableWidgetItem(s.title)
        title.setCheckState(Qt.CheckState.Checked if s.included else Qt.CheckState.Unchecked)
        self.table.setItem(i, 0, title)
        start, end = self.project.cut_points(s)
        duration = end - start if end is not None and start is not None else None
        status = "Approved" if s.reviewed else (f"{round(s.confidence * 100)}% · {s.status}" if s.confidence else s.status)
        short = "Approved" if s.reviewed else (f"{s.confidence:.0%} · " + ("Matched" if s.status == "Matched" else "Review") if s.confidence else s.status)
        for j, text in enumerate((clock(start), clock(end), clock(duration), short), 1):
            item = QTableWidgetItem(text)
            item.setToolTip(status)
            if j == 4 and s.included and s.status.startswith("Needs review") and not s.reviewed:
                item.setForeground(QColor("#bc692b"))
            self.table.setItem(i, j, item)

    def selected(self):
        row = self.table.currentRow()
        return self.project.sections[row] if 0 <= row < len(self.project.sections) else None

    def selection_changed(self):
        s = self.selected()
        if s is not self.wave.selected:
            self.cancel_preview_render()
            self.player.pause()
            self.play_until = None
            self.playback_mode = None
            self.restore_source()
        self.editor.setEnabled(s is not None and not self.process)
        self.wave.selected = s
        if s:
            self.chapter_title.setText(s.title)
            self.chapter_status.setText(f"{s.status} · text coverage {s.confidence:.0%}")
            start, end = self.project.cut_points(s)
            for spin, value in ((self.start, start), (self.end, end)):
                with QSignalBlocker(spin):
                    spin.setRange(0, self.project.duration)
                    spin.setValue(value if value is not None else 0.)
            with QSignalBlocker(self.approve):
                self.approve.setChecked(s.reviewed)
            self.prose_preview.setPlainText(s.text)
        self.wave.update()

    def include_changed(self, item):
        if item.column() == 0:
            self.project.sections[item.row()].included = item.checkState() == Qt.CheckState.Checked
            self.dirty = True
            chosen = [s for s in self.project.sections if s.included]
            self.summary.setText(f"{len(chosen)} selected · {sum(s.status != 'Matched' and not s.reviewed for s in chosen)} need review")
            self.wave.update()

    def padding_changed(self, value):
        if self.process:
            return
        self.project.part_padding = value
        self.dirty = True
        self.refresh()
        self.update_playback_cut()

    def set_boundary(self, key, value):
        s = self.selected()
        if not s or self.process:
            return
        # Editors and markers show the widened cut; store the underlying cut.
        value += self.project.part_padding if key == "start" else -self.project.part_padding
        value = max(0., min(self.project.duration, value))
        setattr(s, key, round(value, 3))
        s.status, s.reviewed = "Manual edit", False
        self.dirty = True
        with QSignalBlocker(self.table):
            self.update_row(self.table.currentRow(), s)
        self.selection_changed()
        self.update_playback_cut()

    def approve_changed(self, checked):
        s = self.selected()
        if s:
            s.reviewed = checked
            self.dirty = True
            with QSignalBlocker(self.table):
                self.update_row(self.table.currentRow(), s)

    def zoom_changed(self, checked):
        self.wave.zoom = checked
        self.wave.update()

    def play_state(self, state):
        playing = state == QMediaPlayer.PlaybackState.PlayingState
        self.play.setText("Pause" if playing else "Play chapter")
        self.play.setIcon(QIcon.fromTheme("media-playback-pause" if playing else "media-playback-start"))

    def seek(self, seconds):
        self.cancel_preview_render()
        self.playback_mode = None
        self.play_until = None
        self.restore_source()
        self.player.setPosition(round(seconds * 1000))

    def toggle_play(self):
        if self.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            self.player.pause()
        else:
            self.cancel_preview_render()
            s = self.selected()
            self.restore_source()
            if s and s.start is not None and s.end is not None and s.start < s.end:
                self.playback_mode = "chapter"
                start, end = self.project.cut_points(s)
                self.play_until = end
                if not start <= self.player.position() / 1000 < end:
                    self.player.setPosition(round(start * 1000))
            else:
                self.playback_mode = None
                self.play_until = None
            self.player.play()

    def audition(self, ending):
        s = self.selected()
        if not s or s.start is None or s.end is None:
            return
        if s.start >= s.end:
            self.error("Start must be before end to preview a chapter.")
            return
        self.playback_mode = "end" if ending else "start"
        self.render_preview()

    def cancel_preview_render(self):
        self.preview_timer.stop()
        if self.preview_process:
            job = self.preview_process
            self.preview_process = None
            job.kill()
            job.waitForFinished(1000)

    def restore_source(self):
        if self.preview_offset is not None:
            position = self.player.position() / 1000 + self.preview_offset
            self.preview_offset = None
            self.player.setSource(QUrl.fromLocalFile(self.project.audio))
            self.player.setPosition(round(position * 1000))

    def update_playback_cut(self):
        """Apply edits to the active preview instead of keeping old stop points."""
        s = self.selected()
        if not s:
            return
        if self.playback_mode in ("start", "end"):
            self.player.pause()
            self.cancel_preview_render()
            self.play_until = None
            self.preview_timer.start()
        elif self.playback_mode == "chapter" and s.start is not None and s.end is not None:
            start, end = self.project.cut_points(s)
            self.play_until = end
            position = self.player.position() / 1000
            if position < start:
                self.player.setPosition(round(start * 1000))
            elif position >= end:
                self.player.pause()
                self.player.setPosition(round(end * 1000))

    def render_preview(self):
        """Render a short, precisely trimmed WAV so OUT is the actual file end."""
        mode = self.playback_mode
        self.cancel_preview_render()
        s = self.selected()
        if mode not in ("start", "end") or not s or s.start is None or s.end is None or s.start >= s.end:
            return
        start, end = self.project.cut_points(s)
        begin = max(start, end - 4) if mode == "end" else start
        finish = end if mode == "end" else min(end, start + 5)
        self.player.pause()
        self.play_until = None
        self.preview_serial += 1
        path = str(Path(self.preview_files.name) / f"preview-{self.preview_serial}.wav")
        job = QProcess(self)
        self.preview_process = job
        self.message.setText("Preparing preview from the current cut points…")
        def completed(code, status):
            current = self.preview_process is job
            if current:
                self.preview_process = None
                if code == 0:
                    self.preview_offset = begin
                    self.player.setSource(QUrl.fromLocalFile(path))
                    self.play_until = finish
                    self.player.play()
                    self.message.setText(f"Previewing retained audio: {clock(begin)} → {clock(finish)}")
                    # Keep only the current clip; old previews are never reused.
                    for old in Path(self.preview_files.name).glob("*.wav"):
                        if str(old) != path:
                            old.unlink(missing_ok=True)
                else:
                    self.message.setText("Preview failed: " + bytes(job.readAllStandardError()).decode(errors="replace")[-500:])
            job.deleteLater()
        job.finished.connect(completed)
        def failed(error):
            if error == QProcess.ProcessError.FailedToStart:
                if self.preview_process is job:
                    self.preview_process = None
                    self.message.setText("Cannot start FFmpeg for preview. Check that FFmpeg is installed.")
                job.deleteLater()
        job.errorOccurred.connect(failed)
        job.start("ffmpeg", ["-v", "error", "-nostdin", "-ss", str(begin), "-i", self.project.audio,
                             "-t", str(finish - begin), "-vn", "-c:a", "pcm_s16le", "-y", path])

    def position_changed(self, position):
        seconds = position / 1000 + (self.preview_offset or 0)
        self.wave.position = seconds
        self.position_label.setText(f"{clock(seconds)} / {clock(self.project.duration)}")
        self.wave.update()
        self.check_cutoff(seconds)

    def check_cutoff(self, seconds):
        if self.play_until is not None and seconds >= self.play_until:
            self.player.pause()
            self.play_until = None

    def confirm_discard(self):
        if not self.dirty:
            return True
        answer = QMessageBox.question(self, "Unsaved project", "Save your current project before continuing?",
                                      QMessageBox.StandardButton.Save | QMessageBox.StandardButton.Discard | QMessageBox.StandardButton.Cancel)
        if answer == QMessageBox.StandardButton.Save:
            return self.save_project()
        return answer == QMessageBox.StandardButton.Discard

    def open_project(self):
        if self.process or not self.confirm_discard():
            return
        path, _ = QFileDialog.getOpenFileName(self, "Open project", "", "Podcast project (*.json)")
        if path:
            self.load_project(path)

    def load_project(self, path):
        try:
            project = Project.load(path)
            self.cancel_preview_render()
            self.player.pause()
            self.playback_mode = self.preview_offset = self.play_until = None
            self.project, self.project_path = project, path
            self.dirty = False
            self.player.setSource(QUrl.fromLocalFile(project.audio))
            self.refresh()
            if not Path(project.audio).is_file():
                self.message.setText("Source audio is missing. Select its new location and analyze again.")
        except Exception as e:
            self.error(e)

    def save_project(self, save_as=False):
        if self.process:
            return False
        path = self.project_path
        if save_as or not path:
            path, _ = QFileDialog.getSaveFileName(self, "Save project", path or "Chapters.podcast.json", "Podcast project (*.json)")
        if not path:
            return False
        try:
            self.project.model = self.model.currentData()
            self.project.save(path)
            self.project_path, self.dirty = path, False
            self.message.setText(f"Project saved: {path}")
            return True
        except Exception as e:
            self.error(e)
            return False

    def analyze(self):
        if not self.project.sections or not Path(self.project.prose).is_file() or not Path(self.project.audio).is_file():
            self.error("Choose a readable prose file and source audio first.")
            return
        if not any(s.included for s in self.project.sections):
            self.error("Select the sections to match before analysis.")
            return
        if any(s.start is not None for s in self.project.sections):
            if QMessageBox.question(self, "Analyze again", "Replace existing cut points and approvals for selected chapters?") != QMessageBox.StandardButton.Yes:
                return
        if self.project.prose_fingerprint and fingerprint(self.project.prose) != self.project.prose_fingerprint:
            self.error("The prose changed on disk. Select the prose file again to reload its sections.")
            return
        self.project.model = self.model.currentData()
        self.start_worker("analyze", ["--model", self.project.model])

    def export(self):
        try:
            validate_export(self.project)
            if not self.project.output:
                raise ValueError("Choose an export folder first.")
            if not Path(self.project.audio).is_file():
                raise ValueError("Source audio is missing.")
        except Exception as e:
            self.error(e)
            return
        self.start_worker("export", ["--format", self.format.currentText().lower()])

    def busy(self, active):
        for widget in (self.prose_picker, self.audio_picker, self.output_picker, self.model,
                       self.analyze_button, self.export_button, self.table, self.editor, self.format, self.file_menu, self.part_padding):
            widget.setEnabled(not active)
        self.cancel_button.setEnabled(active)
        self.wave.setEnabled(not active)

    def start_worker(self, action, args):
        self.cancel_preview_render()
        self.player.pause()
        self.play_until = self.playback_mode = None
        self.restore_source()
        self.temp = tempfile.TemporaryDirectory(prefix="podcast-splitter-project-")
        self.work_path = str(Path(self.temp.name) / "project.json")
        self.project.save(self.work_path)
        self.action, self.buffer, self.stderr = action, b"", ""
        self.failed, self.cancelled, self.worker_done = False, False, False
        process = QProcess(self)
        self.process = process
        env = QProcessEnvironment.systemEnvironment()
        root = str(Path(__file__).resolve().parent.parent)
        env.insert("PYTHONPATH", root + os.pathsep + os.environ.get("PYTHONPATH", ""))
        env.insert("PYTHONUNBUFFERED", "1")
        process.setProcessEnvironment(env)
        process.readyReadStandardOutput.connect(self.read_stdout)
        process.readyReadStandardError.connect(self.read_stderr)
        process.finished.connect(self.worker_finished)
        process.errorOccurred.connect(self.process_error)
        self.busy(True)
        self.progress.setValue(0)
        self.message.setText("Starting local worker…")
        process.start(sys.executable, ["-m", "podcast_splitter.engine", action, self.work_path, *args])

    def process_error(self, error):
        if error == QProcess.ProcessError.FailedToStart:
            self.failed = True
            self.error(self.process.errorString())
            self.worker_finished(1, QProcess.ExitStatus.CrashExit)

    def read_stderr(self):
        if self.process:
            self.stderr = (self.stderr + bytes(self.process.readAllStandardError()).decode(errors="replace"))[-8000:]

    def read_stdout(self):
        if not self.process:
            return
        self.buffer += bytes(self.process.readAllStandardOutput())
        while b"\n" in self.buffer:
            line, self.buffer = self.buffer.split(b"\n", 1)
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if event["event"] == "progress":
                self.progress.setValue(event["value"])
                self.message.setText(event["message"])
            elif event["event"] == "waveform":
                self.wave.project.duration = event["duration"]
                self.wave.project.waveform = event["values"]
                self.wave.update()
            elif event["event"] == "error":
                self.failed = True
                if not self.cancelled:
                    self.error(event["message"])
                self.message.setText(event["message"])
            elif event["event"] == "done":
                self.worker_done = True

    def worker_finished(self, code, status):
        if not self.process:
            return
        self.read_stdout()
        self.read_stderr()
        if code == 0 and self.worker_done and not self.cancelled:
            if self.action == "analyze":
                self.project = Project.load(self.work_path)
                self.dirty = True
                self.refresh()
        elif self.cancelled:
            self.message.setText("Cancelled. Existing cut points and source files were preserved.")
        elif not self.failed:
            self.error(self.stderr or f"Worker exited with code {code}.")
        self.process.deleteLater()
        self.process = None
        self.temp.cleanup()
        self.busy(False)
        self.selection_changed()

    def cancel(self):
        if not self.process:
            return
        self.cancelled = True
        self.cancel_button.setEnabled(False)
        self.message.setText("Cancelling…")
        pid = self.process.processId()
        try:
            os.killpg(pid, signal.SIGTERM)
        except ProcessLookupError:
            self.process.terminate()
        QTimer.singleShot(4000, lambda: self.kill_worker(pid))

    def kill_worker(self, pid):
        if self.process and self.process.processId() == pid:
            try:
                os.killpg(pid, signal.SIGKILL)
            except ProcessLookupError:
                self.process.kill()

    def closeEvent(self, event):
        if self.process:
            self.error("Cancel the running task before closing the window.")
            event.ignore()
        elif self.confirm_discard():
            self.cancel_preview_render()
            self.cutoff_timer.stop()
            self.player.stop()
            self.preview_files.cleanup()
            event.accept()
        else:
            event.ignore()


def main():
    parser = argparse.ArgumentParser(description="Prose-guided podcast chapter editor")
    parser.add_argument("project", nargs="?", help="Saved .podcast.json project")
    args = parser.parse_args()
    application = QApplication(sys.argv[:1])
    application.setApplicationName("Podcast Splitter")
    application.setOrganizationName("PodcastSplitter")
    application.setDesktopFileName("io.github.podcastsplitter.PodcastSplitter")
    window = Window(args.project)
    available = application.primaryScreen().availableGeometry()
    window.resize(min(1180, available.width() - 40), min(880, available.height() - 70))
    window.show()
    sys.exit(application.exec())


if __name__ == "__main__":
    main()
