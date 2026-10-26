"""Stage One workspace and actions; persistence stays in storage modules."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Callable

from PySide6.QtCore import QThread, Qt, Signal
from PySide6.QtGui import QAction, QCloseEvent
from PySide6.QtWidgets import (QFileDialog, QFormLayout, QGroupBox, QHBoxLayout,
                               QLabel, QMainWindow, QMessageBox, QPushButton,
                               QScrollArea, QSplitter, QVBoxLayout, QWidget)

from prose_to_video.gui.audio_player import AudioPlayer
from prose_to_video.gui.project_manager import NewProjectDialog, SettingsDialog
from prose_to_video.gui.prose_viewer import ProseViewer
from prose_to_video.storage.project_files import (ProjectError, ProjectSession,
                                                  create_project, import_audio,
                                                  import_prose, open_project,
                                                  save_project, save_settings)
from prose_to_video.utils.prose import ProseStats, count_prose, read_prose


LOGGER = logging.getLogger(__name__)


class FileTask(QThread):
    succeeded = Signal(object)
    failed = Signal(str)

    def __init__(self, function: Callable[[], object], parent=None):
        super().__init__(parent)
        self.function = function

    def run(self) -> None:
        try:
            self.succeeded.emit(self.function())
        except (ProjectError, OSError, ValueError) as exc:
            self.failed.emit(str(exc))
        except Exception as exc:
            LOGGER.exception("Unexpected background task failure")
            self.failed.emit(f"Operation failed: {exc}")


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.session: ProjectSession | None = None
        self._task: FileTask | None = None
        self._pending_prose_load = False
        self._display_stats: ProseStats | None = None
        self.setWindowTitle("Prose to Video")
        self.resize(1150, 760)
        self._make_actions()
        self._make_workspace()
        self._refresh()

    def _make_actions(self) -> None:
        file_menu = self.menuBar().addMenu("&File")
        self.new_action = QAction("New Project…", self)
        self.open_action = QAction("Open Project…", self)
        self.save_action = QAction("Save Project", self)
        self.close_action = QAction("Close Project", self)
        self.settings_action = QAction("Video Output Settings…", self)
        exit_action = QAction("Exit", self)
        for action, callback in (
            (self.new_action, self._new_project), (self.open_action, self._open_project),
            (self.save_action, self._save_project), (self.close_action, self._close_project),
            (self.settings_action, self._settings), (exit_action, self.close),
        ):
            action.triggered.connect(callback)
        for action in (self.new_action, self.open_action, self.save_action, self.close_action,
                       self.settings_action):
            file_menu.addAction(action)
        file_menu.addSeparator()
        file_menu.addAction(exit_action)
        self.menuBar().addMenu("&Edit").setEnabled(False)
        self.menuBar().addMenu("&View").setEnabled(False)
        help_menu = self.menuBar().addMenu("&Help")
        about_action = QAction("About", self)
        about_action.triggered.connect(lambda: QMessageBox.about(
            self, "About Prose to Video",
            "Prose to Video — Stage One\nProject management, prose viewing, and audio playback."
        ))
        help_menu.addAction(about_action)

    def _make_workspace(self) -> None:
        self.name_label = QLabel()
        self.directory_label = QLabel()
        self.prose_file_label = QLabel()
        self.audio_file_label = QLabel()
        self.source_status = QLabel()
        self.prose_stats = QLabel()
        self.audio_info = QLabel()
        self.settings_label = QLabel()
        for label in (self.name_label, self.directory_label, self.prose_file_label,
                      self.audio_file_label, self.source_status, self.prose_stats,
                      self.audio_info, self.settings_label):
            label.setWordWrap(True)
            label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.import_prose_button = QPushButton("Import Prose…")
        self.import_audio_button = QPushButton("Import Audio…")
        self.import_prose_button.clicked.connect(self._import_prose)
        self.import_audio_button.clicked.connect(self._import_audio)
        buttons = QHBoxLayout()
        buttons.addWidget(self.import_prose_button)
        buttons.addWidget(self.import_audio_button)
        form = QFormLayout()
        for title, label in (
            ("Project:", self.name_label), ("Directory:", self.directory_label),
            ("Prose:", self.prose_file_label), ("Audio:", self.audio_file_label),
            ("Source status:", self.source_status), ("Prose statistics:", self.prose_stats),
            ("Audio metadata:", self.audio_info), ("Video output:", self.settings_label),
        ):
            form.addRow(title, label)
        info = QGroupBox("Project and source files")
        info_layout = QVBoxLayout(info)
        info_layout.addLayout(form)
        info_layout.addLayout(buttons)
        info_layout.addStretch()
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(info)
        scroll.setMinimumWidth(280)

        self.prose_viewer = ProseViewer()
        prose_panel = QGroupBox("Prose")
        prose_layout = QVBoxLayout(prose_panel)
        prose_layout.addWidget(self.prose_viewer)
        horizontal = QSplitter(Qt.Horizontal)
        horizontal.addWidget(scroll)
        horizontal.addWidget(prose_panel)
        horizontal.setStretchFactor(0, 1)
        horizontal.setStretchFactor(1, 3)

        self.audio_player = AudioPlayer()
        self.audio_player.playback_error.connect(self._playback_error)
        vertical = QSplitter(Qt.Vertical)
        vertical.addWidget(horizontal)
        vertical.addWidget(self.audio_player)
        vertical.setStretchFactor(0, 5)
        vertical.setStretchFactor(1, 1)
        self.setCentralWidget(vertical)
        self.statusBar().showMessage("Create or open a project to begin")

    def _show_error(self, title: str, message: str) -> None:
        LOGGER.warning("%s: %s", title, message)
        QMessageBox.warning(self, title, message)
        self.statusBar().showMessage(message, 10_000)

    def _refresh(self) -> None:
        session = self.session
        busy = self._task is not None
        for action in (self.new_action, self.open_action):
            action.setEnabled(not busy)
        for action in (self.save_action, self.close_action, self.settings_action):
            action.setEnabled(session is not None and not busy)
        self.import_prose_button.setEnabled(session is not None and not busy)
        self.import_audio_button.setEnabled(session is not None and not busy)
        if session is None:
            for label in (self.name_label, self.directory_label, self.prose_file_label,
                          self.audio_file_label, self.source_status, self.prose_stats,
                          self.audio_info, self.settings_label):
                label.setText("—")
            self.audio_player.set_audio(None)
            return
        project = session.project
        prose_available, audio_available = session.file_status()
        self.name_label.setText(project.name)
        self.directory_label.setText(str(session.root))
        self.prose_file_label.setText(Path(project.prose_path).name if project.prose_path else "None imported")
        self.audio_file_label.setText(Path(project.audio_path).name if project.audio_path else "None imported")
        self.source_status.setText(
            f"Prose: {'available' if prose_available else 'missing' if project.prose_path else 'not imported'}\n"
            f"Audio: {'available' if audio_available else 'missing' if project.audio_path else 'not imported'}"
        )
        if prose_available and self._display_stats is not None:
            stats = self._display_stats
            self.prose_stats.setText(
                f"{stats.characters:,} characters\n{stats.words:,} words\n{stats.paragraphs:,} paragraphs"
            )
        else:
            self.prose_stats.setText(f"{project.prose_word_count:,} words" if prose_available else "—")
        self.audio_info.setText(
            f"Duration: {self._audio_duration()}\n"
            f"Codec: {project.audio_codec or 'Unknown'}\n"
            f"Sample rate: {project.audio_sample_rate or 'Unknown'} Hz\n"
            f"Channels: {project.audio_channels or 'Unknown'}"
            if audio_available else "—"
        )
        settings = project.settings
        self.settings_label.setText(f"{settings.width} × {settings.height} at {settings.frame_rate} fps")
        audio_path = project.resolved_audio(session.root) if audio_available else None
        if audio_path != self.audio_player._loaded_path:
            self.audio_player.set_audio(audio_path, project.audio_duration)

    def _audio_duration(self) -> str:
        from prose_to_video.utils.media_info import format_duration
        return format_duration(self.session.project.audio_duration) if self.session else "Unknown"

    def _set_session(self, session: ProjectSession | None, load_prose: bool = True) -> None:
        self.session = session
        self._display_stats = None
        self.prose_viewer.clear_story()
        self._refresh()
        if session and load_prose:
            self._queue_prose_load()
        self.statusBar().showMessage(
            f"Project: {session.project.name}" if session else "Project closed", 5_000
        )

    def _queue_prose_load(self) -> None:
        if self._task is not None:
            self._pending_prose_load = True
            return
        session = self.session
        if session is None:
            return
        path = session.project.resolved_prose(session.root)
        if path is None or not path.is_file():
            if path is not None:
                self.prose_viewer.clear_story("The imported prose file is missing. Import a replacement.")
            return

        def load() -> tuple[str, ProseStats]:
            text = read_prose(path)
            return text, count_prose(text)

        def loaded(result: object) -> None:
            text, stats = result
            self._display_stats = stats
            self.prose_viewer.show_text(text)
            self._refresh()

        self._start_task(load, loaded, "Could not read prose")

    def _start_task(self, function: Callable[[], object], success: Callable[[object], None], title: str) -> None:
        if self._task is not None:
            return
        task = FileTask(function, self)
        self._task = task
        task.succeeded.connect(success)
        task.failed.connect(lambda message: self._show_error(title, message))
        task.finished.connect(self._task_finished)
        task.finished.connect(task.deleteLater)
        self._refresh()
        task.start()

    def _task_finished(self) -> None:
        self._task = None
        self._refresh()
        if self._pending_prose_load:
            self._pending_prose_load = False
            self._queue_prose_load()

    def _new_project(self) -> None:
        dialog = NewProjectDialog(self)
        if dialog.exec() != dialog.Accepted:
            return
        try:
            self._set_session(create_project(dialog.parent_directory, dialog.project_name))
        except ProjectError as exc:
            self._show_error("Could not create project", str(exc))

    def _open_project(self) -> None:
        directory = QFileDialog.getExistingDirectory(self, "Open Project Directory", str(Path.home()))
        if not directory:
            return
        try:
            self._set_session(open_project(Path(directory)))
        except ProjectError as exc:
            self._show_error("Could not open project", str(exc))

    def _save_project(self) -> None:
        if self.session is None:
            return
        try:
            self.session = save_project(self.session)
            self.statusBar().showMessage("Project saved", 5_000)
        except ProjectError as exc:
            self._show_error("Could not save project", str(exc))

    def _close_project(self) -> None:
        self._set_session(None)

    def _settings(self) -> None:
        if self.session is None:
            return
        dialog = SettingsDialog(self.session.project.settings, self)
        if dialog.exec() != dialog.Accepted:
            return
        try:
            self.session = save_settings(self.session, dialog.values())
            self._refresh()
            self.statusBar().showMessage("Video settings saved", 5_000)
        except ProjectError as exc:
            self._show_error("Could not save settings", str(exc))

    def _import_prose(self) -> None:
        if self.session is None:
            return
        filename, _ = QFileDialog.getOpenFileName(
            self, "Import Prose", str(Path.home()), "Prose files (*.md *.txt)"
        )
        if filename:
            session = self.session
            self._start_task(
                lambda: import_prose(session, Path(filename)),
                lambda result: self._set_session(result), "Could not import prose"
            )

    def _import_audio(self) -> None:
        if self.session is None:
            return
        filename, _ = QFileDialog.getOpenFileName(
            self, "Import Audio", str(Path.home()), "Audio files (*.mp3 *.wav *.flac *.m4a)"
        )
        if filename:
            session = self.session
            self._start_task(
                lambda: import_audio(session, Path(filename)),
                lambda result: self._audio_imported(result), "Could not import audio"
            )

    def _audio_imported(self, result: object) -> None:
        self.session = result
        self._refresh()
        self.statusBar().showMessage("Audio imported", 5_000)

    def _playback_error(self, message: str) -> None:
        self._show_error("Audio playback error", message)

    def closeEvent(self, event: QCloseEvent) -> None:
        if self._task is not None:
            event.ignore()
            self.statusBar().showMessage("Please wait for the current file operation to finish", 5_000)
            return
        self.audio_player.player.stop()
        super().closeEvent(event)
