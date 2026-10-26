"""Project workspace, media controls, and scene review."""

from __future__ import annotations

import logging
from pathlib import Path
from threading import Event
from typing import Callable

from PySide6.QtCore import QThread, Qt, Signal
from PySide6.QtGui import QAction, QCloseEvent
from PySide6.QtWidgets import (QDialog, QDialogButtonBox, QFileDialog, QFormLayout, QGroupBox, QHBoxLayout,
                               QInputDialog, QLabel, QListWidget, QMainWindow, QMessageBox, QPushButton,
                               QPlainTextEdit, QScrollArea, QSplitter, QTabWidget, QVBoxLayout, QWidget)

from prose_to_video.gui.audio_player import AudioPlayer
from prose_to_video.gui.alignment_settings import AlignmentSettingsDialog
from prose_to_video.gui.waveform import waveform_peaks
from prose_to_video.gui.ai_settings import AISettingsDialog, load_ai_settings, save_ai_settings
from prose_to_video.gui.project_manager import NewProjectDialog, SettingsDialog
from prose_to_video.gui.prose_viewer import ProseViewer
from prose_to_video.gui.scenes_panel import ScenesPanel
from prose_to_video.gui.storyboard_panel import StoryboardPanel
from prose_to_video.gui.storyboard_settings import StoryboardSettingsDialog
from prose_to_video.models.scene import Scene, source_hash
from prose_to_video.models.alignment import AlignmentRun
from prose_to_video.models.shot import Storyboard
from prose_to_video.pipeline.audio_transcription import (AlignmentCanceled, TranscriptionSettings,
    dependency_check, source_file_hash)
from prose_to_video.pipeline.audio_alignment import align_project
from prose_to_video.pipeline.providers import make_provider
from prose_to_video.pipeline.shot_planning import PlanningCanceled, plan_scene
from prose_to_video.pipeline.shot_editing import update_shot, split_shot, merge_shots, insert_shot, delete_shot, swap_visuals_with_next
from prose_to_video.storage.storyboard_export import export_storyboard_json, export_storyboard_markdown
from prose_to_video.pipeline.scene_detection import (AnalysisCanceled, detect_scenes,
                                                     merge_with_next, paragraph_starts,
                                                     split_scene)
from prose_to_video.storage.database import ProjectDatabaseError
from prose_to_video.storage.scene_export import export_json, export_markdown
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


class SceneTask(QThread):
    succeeded = Signal(object, str)
    failed = Signal(str)
    progress = Signal(str, int, int)

    def __init__(self, path: Path, provider, settings, parent=None):
        super().__init__(parent)
        self.path = path
        self.provider = provider
        self.settings = settings
        self.cancel_event = Event()

    def run(self) -> None:
        try:
            text = read_prose(self.path)
            scenes = detect_scenes(text, self.provider, self.settings, self.cancel_event,
                                   lambda stage, current, total: self.progress.emit(stage, current, total))
            if self.cancel_event.is_set():
                raise AnalysisCanceled()
            if source_hash(read_prose(self.path)) != source_hash(text):
                raise ValueError("The prose changed during analysis. Run detection again.")
            self.succeeded.emit(scenes, text)
        except AnalysisCanceled:
            self.failed.emit("Scene detection canceled; saved scenes were preserved")
        except (OSError, ValueError) as exc:
            self.failed.emit(str(exc))
        except Exception:
            LOGGER.exception("Scene analysis failed")
            self.failed.emit("Scene analysis failed unexpectedly; saved scenes were preserved")


class AlignmentTask(QThread):
    succeeded = Signal(object)
    failed = Signal(str)
    progress = Signal(str, int, int)

    def __init__(self, prose_path, audio_path, scenes, database, settings, parent=None):
        super().__init__(parent)
        self.prose_path, self.audio_path = prose_path, audio_path
        self.scenes, self.database, self.settings = scenes, database, settings
        self.cancel_event = Event()

    def run(self):
        try:
            result = align_project(self.prose_path, self.audio_path, self.scenes,
                self.database, self.settings, cancel=self.cancel_event,
                progress=lambda stage, current, total: self.progress.emit(stage, current, total))
            self.succeeded.emit(result)
        except AlignmentCanceled:
            self.failed.emit("Audio alignment canceled; prior results were preserved")
        except Exception as exc:
            LOGGER.exception("Audio alignment failed")
            self.failed.emit(f"Audio alignment failed: {exc}. Prior results were preserved")


class WaveformTask(QThread):
    succeeded = Signal(object, object, object)
    failed = Signal(str)

    def __init__(self, path: Path, cache: Path, parent=None):
        super().__init__(parent)
        self.path, self.cache = path, cache

    def run(self):
        try:
            digest = source_file_hash(self.path)
            try:
                peaks = waveform_peaks(self.path, self.cache)
            except Exception as exc:
                self.failed.emit(str(exc))
                peaks = []
            self.succeeded.emit(self.path, digest, peaks)
        except Exception as exc:
            self.failed.emit(str(exc))


class StoryboardTask(QThread):
    succeeded = Signal(object)
    failed = Signal(str)
    progress = Signal(str)

    def __init__(self, scenes, text, settings, provider, alignment, model, chunk_chars, parent=None):
        super().__init__(parent)
        self.scenes, self.text, self.settings = scenes, text, settings
        self.provider, self.alignment, self.model = provider, alignment, model
        self.chunk_chars = chunk_chars
        self.cancel_event = Event()

    def run(self):
        try:
            boards = []
            for i, scene in enumerate(self.scenes, 1):
                if self.cancel_event.is_set(): raise PlanningCanceled("Storyboard generation canceled")
                self.progress.emit(f"Planning scene {i} of {len(self.scenes)}: {scene.title}")
                context = self.text[max(0, scene.start-1200):scene.start]
                boards.append(plan_scene(scene, self.text, self.settings, self.provider,
                    self.alignment, self.model, context, self.chunk_chars, self.cancel_event))
            if self.cancel_event.is_set(): raise PlanningCanceled("Storyboard generation canceled")
            self.succeeded.emit(boards)
        except Exception as exc:
            self.failed.emit(str(exc))


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.session: ProjectSession | None = None
        self._task: FileTask | None = None
        self._scene_task: SceneTask | None = None
        self._alignment_task: AlignmentTask | None = None
        self._waveform_task: WaveformTask | None = None
        self._storyboard_task: StoryboardTask | None = None
        self._alignment: AlignmentRun | None = None
        self._cached_transcript = None
        self._last_alignment_settings: TranscriptionSettings | None = None
        self._audio_hash: str | None = None
        self._audio_stamp: tuple[int, int] | None = None
        self._highlighted_span: tuple[int, int] | None = None
        self._storyboard_stale = False
        self._pending_prose_load = False
        self._display_stats: ProseStats | None = None
        self._source_text = ""
        self._prose_ready = False
        self._source_stamp: tuple[int, int] | None = None
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
        self.ai_settings_action = QAction("AI Scene Detection Settings…", self)
        self.ai_settings_action.triggered.connect(self._ai_settings)
        self.storyboard_settings_action = QAction("Storyboard Settings…", self)
        self.storyboard_settings_action.triggered.connect(self._storyboard_settings)
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
        file_menu.addAction(self.ai_settings_action)
        file_menu.addAction(self.storyboard_settings_action)
        file_menu.addSeparator()
        file_menu.addAction(exit_action)
        self.menuBar().addMenu("&Edit").setEnabled(False)
        self.menuBar().addMenu("&View").setEnabled(False)
        help_menu = self.menuBar().addMenu("&Help")
        about_action = QAction("About", self)
        about_action.triggered.connect(lambda: QMessageBox.about(
            self, "About Prose to Video",
            "Prose to Video — Stage Four\nScenes, local audio alignment, and visual storyboards."
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
        self.prose_viewer.loaded.connect(self._prose_display_ready)
        self.play_prose_button = QPushButton("Play Selected Prose")
        self.play_prose_button.clicked.connect(self._play_selected_prose)
        prose_panel = QGroupBox("Prose")
        prose_layout = QVBoxLayout(prose_panel)
        prose_layout.addWidget(self.prose_viewer)
        prose_layout.addWidget(self.play_prose_button)
        horizontal = QSplitter(Qt.Horizontal)
        horizontal.addWidget(scroll)
        horizontal.addWidget(prose_panel)
        self.scenes_panel = ScenesPanel()
        self.storyboard_panel = StoryboardPanel()
        self.workspace_tabs = QTabWidget()
        self.workspace_tabs.addTab(self.scenes_panel, "Scenes")
        self.workspace_tabs.addTab(self.storyboard_panel, "Storyboard")
        horizontal.addWidget(self.workspace_tabs)
        self.storyboard_panel.generate_requested.connect(lambda: self._generate_storyboards(False))
        self.storyboard_panel.generate_selected_requested.connect(self._choose_storyboard_scenes)
        self.storyboard_panel.generate_all_requested.connect(lambda: self._generate_storyboards(True))
        self.storyboard_panel.cancel_requested.connect(self._cancel_storyboard)
        self.storyboard_panel.settings_requested.connect(self._storyboard_settings)
        self.storyboard_panel.export_requested.connect(self._export_storyboard)
        self.storyboard_panel.save_requested.connect(self._save_shot)
        self.storyboard_panel.split_requested.connect(self._split_shot)
        self.storyboard_panel.merge_requested.connect(self._merge_shot)
        self.storyboard_panel.reorder_requested.connect(self._reorder_shot)
        self.storyboard_panel.insert_requested.connect(self._insert_shot)
        self.storyboard_panel.delete_requested.connect(self._delete_shot)
        self.storyboard_panel.play_requested.connect(self._play_shot)
        self.storyboard_panel.selected.connect(self._shot_selected)
        self.prose_viewer.selectionChanged.connect(self._prose_selection_changed)
        self.scenes_panel.detect_requested.connect(self._detect_scenes)
        self.scenes_panel.cancel_requested.connect(self._cancel_scenes)
        self.scenes_panel.export_requested.connect(self._export_scenes)
        self.scenes_panel.split_requested.connect(self._split_scene)
        self.scenes_panel.merge_requested.connect(self._merge_scene)
        self.scenes_panel.save_requested.connect(self._save_scene)
        self.scenes_panel.selected.connect(self._scene_selected)
        self.scenes_panel.align_requested.connect(self._align_audio)
        self.scenes_panel.align_cancel_requested.connect(self._cancel_alignment)
        self.scenes_panel.play_scene_requested.connect(self._play_scene)
        self.scenes_panel.correction_requested.connect(self._correct_scene_time)
        self.scenes_panel.transcript_requested.connect(self._view_transcript)
        horizontal.setStretchFactor(0, 1)
        horizontal.setStretchFactor(1, 3)
        horizontal.setStretchFactor(2, 2)

        self.audio_player = AudioPlayer()
        self.audio_player.playback_error.connect(self._playback_error)
        self.audio_player.position_seconds.connect(self._audio_position_changed)
        self.audio_player.waveform.scene_selected.connect(self._select_scene_id)
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
        busy = self._task is not None or self._scene_task is not None or self._alignment_task is not None or self._storyboard_task is not None
        for action in (self.new_action, self.open_action):
            action.setEnabled(not busy)
        for action in (self.save_action, self.close_action, self.settings_action):
            action.setEnabled(session is not None and not busy)
        self.import_prose_button.setEnabled(session is not None and not busy)
        self.import_audio_button.setEnabled(session is not None and not busy)
        prose_available = bool(session and session.file_status()[0])
        audio_available = bool(session and session.file_status()[1])
        self.scenes_panel.set_state(prose_available and self._task is None,
            self._scene_task is not None, audio_available, self._alignment_task is not None)
        self.storyboard_panel.generate_button.setEnabled(bool(session and self.scenes_panel.current_scene()) and not busy and not self.scenes_panel.outdated)
        self.storyboard_panel.generate_all_button.setEnabled(bool(session and self.scenes_panel.scenes) and not busy and not self.scenes_panel.outdated)
        self.storyboard_panel.generate_selected_button.setEnabled(bool(session and self.scenes_panel.scenes) and not busy and not self.scenes_panel.outdated)
        self.storyboard_panel.cancel_button.setEnabled(self._storyboard_task is not None)
        self.scenes_panel.transcript_button.setEnabled(bool(self._alignment or self._cached_transcript))
        self.play_prose_button.setEnabled(bool(self._alignment and not busy))
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
        self._source_text = ""
        self._prose_ready = False
        self._source_stamp = None
        self._audio_hash = None
        self._audio_stamp = None
        self._alignment = None
        self._storyboard_stale = False
        self.storyboard_panel.set_board(None)
        self._cached_transcript = None
        self._highlighted_span = None
        self.scenes_panel.set_alignment(None)
        self.scenes_panel.set_scenes([])
        self.prose_viewer.clear_story()
        self._refresh()
        if session and load_prose:
            self._queue_prose_load()
        if session and session.file_status()[1]:
            self._start_waveform()
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
            self._source_text = text
            self._source_stamp = (path.stat().st_mtime_ns, path.stat().st_size)
            self._display_stats = stats
            self.prose_viewer.show_text(text)
            self._load_scene_records(text)
            self._load_current_alignment()
            self._refresh()

        self._start_task(load, loaded, "Could not read prose")

    def _load_scene_records(self, text: str) -> None:
        if self.session is None:
            return
        try:
            digest, scenes = self.session.database.load_scenes()
            self.scenes_panel.set_scenes(scenes, bool(digest and digest != source_hash(text)))
            if self._alignment:
                self._load_current_alignment()
        except ProjectDatabaseError as exc:
            self._show_error("Could not load scenes", str(exc))

    def _prose_display_ready(self) -> None:
        self._prose_ready = True
        scene = self.scenes_panel.current_scene()
        if scene:
            self._scene_selected(scene.identifier)

    def _scene_selected(self, identifier: str) -> None:
        scene = self.scenes_panel.current_scene()
        if scene and not self.scenes_panel.outdated and not self._source_unchanged():
            self.scenes_panel.outdated = True
            self.scenes_panel.stage_label.setText("Scene analysis is outdated: prose changed")
        if scene and self._prose_ready and not self.scenes_panel.outdated:
            try:
                self.prose_viewer.set_layer("scene", (scene.start, scene.end), scroll=True)
            except ValueError:
                self.scenes_panel.outdated = True
                self.scenes_panel.stage_label.setText("Scene offsets do not match displayed prose")
        self._load_selected_storyboard()
        self.scenes_panel.set_state(bool(self.session and self.session.file_status()[0]),
                                    self._scene_task is not None,
                                    bool(self.session and self.session.file_status()[1]),
                                    self._alignment_task is not None)

    def _source_unchanged(self) -> bool:
        if self.session is None:
            return False
        path = self.session.project.resolved_prose(self.session.root)
        try:
            if path is None:
                return False
            stat = path.stat()
            stamp = (stat.st_mtime_ns, stat.st_size)
            if stamp == self._source_stamp:
                return True
            current = read_prose(path)
            if source_hash(current) != source_hash(self._source_text):
                return False
            self._source_stamp = stamp
            return True
        except (OSError, ValueError):
            return False

    def _require_current_scenes(self) -> None:
        if self.scenes_panel.outdated or not self._source_unchanged():
            self.scenes_panel.outdated = True
            self.scenes_panel.stage_label.setText("Scene analysis is outdated: prose changed")
            self.scenes_panel.set_state(False, False)
            raise ValueError("The imported prose changed. Run scene detection again.")

    def _load_current_alignment(self) -> None:
        if self.session is None or not self._source_text or not self._audio_hash:
            return
        run = self.session.database.load_alignment(source_hash(self._source_text), self._audio_hash)
        self._alignment = run
        self.scenes_panel.set_alignment(run)
        if run is None and self.session.database.load_alignment() is not None:
            self.scenes_panel.stage_label.setText("Audio alignment outdated: prose or audio changed")
        self.audio_player.waveform.set_timings(run.scenes if run else [])
        self._load_selected_storyboard()
        self._refresh()

    def _start_waveform(self) -> None:
        if self.session is None or self._waveform_task is not None:
            return
        path = self.session.project.resolved_audio(self.session.root)
        if path is None or not path.is_file():
            return
        task = WaveformTask(path, self.session.root / "alignment", self)
        self._waveform_task = task
        task.succeeded.connect(self._waveform_ready)
        task.failed.connect(lambda message: self.statusBar().showMessage(f"Waveform unavailable: {message}", 8_000))
        task.finished.connect(self._waveform_finished)
        task.finished.connect(task.deleteLater)
        task.start()

    def _waveform_ready(self, path: Path, digest: str, peaks: list[float]) -> None:
        if self.session is None or self.session.project.resolved_audio(self.session.root) != path:
            return
        self._audio_hash = digest
        stat = path.stat()
        self._audio_stamp = (stat.st_mtime_ns, stat.st_size)
        self.audio_player.waveform.set_data(peaks, self.session.project.audio_duration or len(peaks) / 2)
        self._load_current_alignment()

    def _waveform_finished(self) -> None:
        prior = self._waveform_task.path if self._waveform_task else None
        self._waveform_task = None
        if (self.session and self._audio_hash is None and self.session.file_status()[1]
                and self.session.project.resolved_audio(self.session.root) != prior):
            # A project switch may have happened while the old waveform was loading.
            self._start_waveform()

    def _align_audio(self) -> None:
        if self.session is None or self._alignment_task or self._scene_task or self._task:
            return
        prose = self.session.project.resolved_prose(self.session.root)
        audio = self.session.project.resolved_audio(self.session.root)
        if prose is None or audio is None or not prose.is_file() or not audio.is_file():
            self._show_error("Audio alignment", "Import both prose and narration audio first")
            return
        try:
            dependency_check()
        except ValueError as exc:
            self._show_error("Audio alignment dependencies", str(exc))
            return
        dialog = AlignmentSettingsDialog(self)
        if dialog.exec() != dialog.Accepted:
            return
        answer = QMessageBox.question(self, "Model downloads",
            "WhisperX may download the transcription model, Silero speech detector, "
            "NLTK sentence data, and a language alignment model. These can occupy hundreds of MB "
            "in standard user caches. Allow downloads for this run? "
            "Choose No to use cached models only.", QMessageBox.Yes | QMessageBox.No | QMessageBox.Cancel,
            QMessageBox.No)
        if answer == QMessageBox.Cancel:
            return
        try:
            settings = dialog.values(answer == QMessageBox.Yes)
            if not self._source_unchanged():
                raise ValueError("The prose changed; reload it before alignment")
            scenes = [] if self.scenes_panel.outdated else self.scenes_panel.scenes
            task = AlignmentTask(prose, audio, scenes, self.session.database, settings, self)
            self._last_alignment_settings = settings
            self._alignment_task = task
            task.progress.connect(self.scenes_panel.set_progress)
            task.succeeded.connect(self._alignment_ready)
            task.failed.connect(self._alignment_failed)
            task.finished.connect(self._alignment_finished)
            task.finished.connect(task.deleteLater)
            self._refresh()
            task.start()
        except ValueError as exc:
            self._show_error("Audio alignment", str(exc))

    def _cancel_alignment(self) -> None:
        if self._alignment_task:
            self._alignment_task.cancel_event.set()
            self.scenes_panel.stage_label.setText("Canceling at the next processing boundary…")

    def _alignment_ready(self, run: AlignmentRun) -> None:
        if self._alignment_task and self._alignment_task.cancel_event.is_set():
            self._alignment_failed("Audio alignment canceled")
            return
        self._audio_hash = run.audio_hash
        self._cached_transcript = None
        audio = self.session.project.resolved_audio(self.session.root) if self.session else None
        if audio:
            stat = audio.stat()
            self._audio_stamp = (stat.st_mtime_ns, stat.st_size)
        self._load_current_alignment()
        self.statusBar().showMessage(f"Aligned {sum(w.status == 'matched' for w in run.words)} words", 8_000)

    def _alignment_failed(self, message: str) -> None:
        self.scenes_panel.stage_label.setText(message)
        if self.session and self._last_alignment_settings:
            audio = self.session.project.resolved_audio(self.session.root)
            try:
                digest = source_file_hash(audio) if audio else None
                cached = self.session.database.load_transcript_cache(
                    digest, self._last_alignment_settings.fingerprint()) if digest else None
                self._cached_transcript = cached[0] if cached else None
            except Exception:
                self._cached_transcript = None
        self._refresh()
        if "canceled" not in message.lower():
            self._show_error("Audio alignment", message)

    def _alignment_finished(self) -> None:
        self._alignment_task = None
        self._refresh()

    def _current_alignment(self) -> AlignmentRun | None:
        if self.session is None or self._alignment is None:
            return None
        if not self._source_unchanged():
            self._alignment = None
            self._storyboard_stale = True
            self.scenes_panel.set_alignment(None)
            self.scenes_panel.stage_label.setText("Audio alignment outdated: prose changed")
            return None
        audio = self.session.project.resolved_audio(self.session.root)
        try:
            if audio is None or source_file_hash(audio) != self._alignment.audio_hash:
                self._alignment = None
                self._storyboard_stale = True
                self.scenes_panel.set_alignment(None)
                self.scenes_panel.stage_label.setText("Audio alignment outdated: audio changed")
                return None
        except OSError:
            return None
        return self._alignment

    def _play_scene(self) -> None:
        scene = self.scenes_panel.current_scene()
        run = self._current_alignment()
        timing = next((t for t in run.scenes if scene and t.scene_id == scene.identifier), None) if run else None
        if timing is None or timing.audio_start is None or timing.audio_end is None:
            self.statusBar().showMessage("Scene has no current reliable audio interval", 7_000)
            return
        self.audio_player.play_range(timing.audio_start, timing.audio_end)

    def _correct_scene_time(self, start: float, end: float) -> None:
        scene = self.scenes_panel.current_scene()
        run = self._current_alignment()
        if not scene or not run or self.session is None:
            self._show_error("Scene timing", "No current alignment for this scene")
            return
        try:
            self.session.database.correct_scene_time(scene.identifier, start, end,
                run.prose_hash, run.audio_hash)
            self._load_current_alignment()
            self.statusBar().showMessage("Scene timing corrected", 5_000)
        except ValueError as exc:
            self._show_error("Scene timing", str(exc))

    def _view_transcript(self) -> None:
        segments = self._alignment.transcript if self._alignment else self._cached_transcript
        if not segments:
            self.statusBar().showMessage("No transcript is available", 5_000)
            return
        from prose_to_video.utils.media_info import format_duration
        dialog = QDialog(self)
        dialog.setWindowTitle("Narration Transcript")
        dialog.resize(760, 550)
        view = QPlainTextEdit()
        view.setReadOnly(True)
        view.setPlainText("\n".join(f"[{format_duration(segment.start)}–{format_duration(segment.end)}] "
                                    f"{segment.text}" for segment in segments))
        layout = QVBoxLayout(dialog)
        layout.addWidget(view)
        close = QPushButton("Close")
        close.clicked.connect(dialog.accept)
        layout.addWidget(close)
        dialog.exec()

    def _select_scene_id(self, identifier: str) -> None:
        for index, scene in enumerate(self.scenes_panel.scenes):
            if scene.identifier == identifier:
                self.scenes_panel.list_widget.setCurrentRow(index)
                return

    def _play_selected_prose(self) -> None:
        run = self._current_alignment()
        if run is None:
            self.statusBar().showMessage("Alignment is outdated or unavailable", 7_000)
            return
        selected = self.prose_viewer.selected_source_range()
        if selected is None:
            self.statusBar().showMessage("Select prose text first", 5_000)
            return
        words = [w for w in run.words if selected[0] <= w.source_start and w.source_end <= selected[1]
                 and w.audio_start is not None]
        if not words:
            self.statusBar().showMessage("Selected prose has no matched narration", 6_000)
            return
        self.audio_player.play_range(words[0].audio_start, words[-1].audio_end)

    def _audio_position_changed(self, seconds: float) -> None:
        if self.storyboard_panel.board and not self._storyboard_stale:
            self.storyboard_panel.playback_position(seconds)
        run = self._alignment
        if run is None or not self._prose_ready:
            return
        if not self._source_unchanged():
            self._alignment = None
            self._storyboard_stale = True
            self.scenes_panel.set_alignment(None)
            self.scenes_panel.stage_label.setText("Audio alignment outdated: prose changed")
            return
        audio = self.session.project.resolved_audio(self.session.root) if self.session else None
        try:
            stat = audio.stat() if audio else None
            if stat is None or (stat.st_mtime_ns, stat.st_size) != self._audio_stamp:
                self._alignment = None
                self._storyboard_stale = True
                self.scenes_panel.set_alignment(None)
                self.scenes_panel.stage_label.setText("Audio alignment outdated: audio changed")
                return
        except OSError:
            return
        span = next((s for s in run.sentences if s.audio_start is not None and
                     s.audio_start <= seconds <= s.audio_end), None)
        key = (span.source_start, span.source_end) if span else None
        if key != self._highlighted_span:
            self._highlighted_span = key
            if key:
                self.prose_viewer.set_layer("playback", key)
            else:
                self.prose_viewer.set_layer("playback", None)

    def _ai_settings(self) -> None:
        try:
            dialog = AISettingsDialog(load_ai_settings(), self)
        except ValueError as exc:
            self._show_error("AI settings", str(exc))
            return
        if dialog.exec() == dialog.Accepted:
            try:
                save_ai_settings(dialog.values())
                self.statusBar().showMessage("AI scene settings saved", 5_000)
            except ValueError as exc:
                self._show_error("AI settings", str(exc))

    def _detect_scenes(self) -> None:
        if self.session is None:
            self._show_error("Scene detection", "Open a project first")
            return
        path = self.session.project.resolved_prose(self.session.root)
        if path is None or not path.is_file():
            self._show_error("Scene detection", "Import a prose file first, or replace the missing file")
            return
        if self._task is not None or self._scene_task is not None or self._alignment_task is not None:
            return
        try:
            settings = load_ai_settings()
            provider = make_provider(settings)
        except ValueError as exc:
            self._show_error("Scene detection settings", str(exc))
            return
        if self.scenes_panel.scenes:
            answer = QMessageBox.question(
                self, "Replace scene analysis?",
                "Running detection again replaces all saved scenes, boundaries, and manual edits. Continue?",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
            )
            if answer != QMessageBox.Yes:
                return
        task = SceneTask(path, provider, settings, self)
        self._scene_task = task
        task.progress.connect(self.scenes_panel.set_progress)
        task.succeeded.connect(self._scene_analysis_ready)
        task.failed.connect(self._scene_analysis_failed)
        task.finished.connect(self._scene_task_finished)
        task.finished.connect(task.deleteLater)
        self._refresh()
        task.start()

    def _cancel_scenes(self) -> None:
        if self._scene_task is not None:
            self._scene_task.cancel_event.set()
            self.scenes_panel.stage_label.setText("Canceling after the current AI request…")

    def _scene_analysis_ready(self, scenes: list[Scene], text: str) -> None:
        if self.session is None:
            return
        try:
            if self._scene_task is not None and self._scene_task.cancel_event.is_set():
                raise AnalysisCanceled("Scene detection canceled; saved scenes were preserved")
            path = self.session.project.resolved_prose(self.session.root)
            if path is None or source_hash(read_prose(path)) != source_hash(text):
                raise ValueError("The prose changed during analysis. Saved scenes were preserved.")
            self.session.database.replace_scenes(text, scenes)
            self._source_text = text
            if self.prose_viewer._source_text != text:
                self._prose_ready = False
                self._display_stats = count_prose(text)
                self.prose_viewer.show_text(text)
            stat = path.stat()
            self._source_stamp = (stat.st_mtime_ns, stat.st_size)
            self._load_scene_records(text)
            self.statusBar().showMessage(f"Detected {len(scenes)} scenes", 8_000)
        except AnalysisCanceled as exc:
            self._scene_analysis_failed(str(exc))
        except (OSError, ValueError, ProjectDatabaseError) as exc:
            self._show_error("Could not save scenes", str(exc))

    def _scene_analysis_failed(self, message: str) -> None:
        if "canceled" in message.lower():
            self.scenes_panel.stage_label.setText(message)
            self.statusBar().showMessage(message, 8_000)
        else:
            self._show_error("Scene detection failed", message)

    def _scene_task_finished(self) -> None:
        self._scene_task = None
        self._refresh()

    def _save_scene(self) -> None:
        if self.session is None or self.scenes_panel.outdated or self._alignment_task:
            return
        try:
            self._require_current_scenes()
            scene = self.scenes_panel.edited_scene()
            self.session.database.update_scene(scene, self._source_text)
            self._load_scene_records(self._source_text)
            self.scenes_panel.set_scenes(self.scenes_panel.scenes, select_id=scene.identifier)
            self.statusBar().showMessage("Scene details saved", 5_000)
        except (ValueError, ProjectDatabaseError) as exc:
            self._show_error("Could not save scene", str(exc))

    def _split_scene(self) -> None:
        scene = self.scenes_panel.current_scene()
        if scene is None or self.session is None or self.scenes_panel.outdated or self._alignment_task:
            return
        choices = {}
        for offset in paragraph_starts(self._source_text):
            if scene.start < offset < scene.end:
                preview = self._source_text[offset:offset + 70].splitlines()[0]
                choices[f"Character {offset}: {preview}"] = offset
        if not choices:
            self._show_error("Split scene", "This scene has no internal paragraph boundary")
            return
        choice, accepted = QInputDialog.getItem(self, "Split Scene", "New scene begins at:",
                                                list(choices), 0, False)
        if accepted:
            try:
                self._require_current_scenes()
                scenes = split_scene(self._source_text, self.scenes_panel.scenes,
                                     scene.identifier, choices[choice])
                self.session.database.replace_scenes(self._source_text, scenes)
                self.scenes_panel.set_scenes(scenes, select_id=scene.identifier)
                self._load_current_alignment()
            except (ValueError, ProjectDatabaseError) as exc:
                self._show_error("Could not split scene", str(exc))

    def _merge_scene(self) -> None:
        scene = self.scenes_panel.current_scene()
        if scene is None or self.session is None or self.scenes_panel.outdated or self._alignment_task:
            return
        try:
            self._require_current_scenes()
            scenes = merge_with_next(self._source_text, self.scenes_panel.scenes, scene.identifier)
            self.session.database.replace_scenes(self._source_text, scenes)
            self.scenes_panel.set_scenes(scenes, select_id=scene.identifier)
            self._load_current_alignment()
        except (ValueError, ProjectDatabaseError) as exc:
            self._show_error("Could not merge scenes", str(exc))

    def _export_scenes(self) -> None:
        if self.scenes_panel.outdated or not self.scenes_panel.scenes:
            return
        filename, selected_filter = QFileDialog.getSaveFileName(
            self, "Export Scenes", str(self.session.root / "scenes" / "scene_breakdown.json"),
            "JSON files (*.json);;Detailed JSON (*.json);;Markdown files (*.md)")
        if not filename:
            return
        try:
            self._require_current_scenes()
            run = self._current_alignment()
            path = Path(filename)
            if selected_filter.startswith("Markdown") or path.suffix.lower() == ".md":
                export_markdown(path, self._source_text, self.scenes_panel.scenes, run)
            else:
                export_json(path, self._source_text, self.scenes_panel.scenes, run,
                            detailed=selected_filter.startswith("Detailed"))
            self.statusBar().showMessage(f"Scenes exported to {path}", 8_000)
        except (OSError, ValueError) as exc:
            self._show_error("Could not export scenes", str(exc))

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
        self._audio_hash = None
        self._audio_stamp = None
        self._alignment = None
        self._cached_transcript = None
        self.scenes_panel.set_alignment(None)
        self._refresh()
        self._start_waveform()
        self.statusBar().showMessage("Audio imported", 5_000)

    def _load_selected_storyboard(self) -> None:
        scene = self.scenes_panel.current_scene()
        if not self.session or not scene or not self._source_text:
            self.storyboard_panel.set_board(None)
            self._storyboard_stale = False
            return
        try:
            board = self.session.database.load_storyboard(scene.identifier)
            state = self.session.database.storyboard_state(board, self._source_text, self._alignment) if board else ""
            self._storyboard_stale = state.startswith(("Audio alignment changed", "Prose changed", "Scene boundaries changed"))
            self.storyboard_panel.set_board(board, state)
        except (ProjectDatabaseError, ValueError) as exc:
            self._storyboard_stale = True
            self.storyboard_panel.set_board(None, str(exc))

    def _shot_selected(self, identifier: str) -> None:
        shot = self.storyboard_panel.current_shot()
        if shot and self._prose_ready and self._source_unchanged():
            self.prose_viewer.set_layer("shot", (shot.source_start, shot.source_end), scroll=True)
        else:
            self.prose_viewer.set_layer("shot", None)

    def _prose_selection_changed(self) -> None:
        # User selections map through source offsets; programmed highlight scrolling has no selection.
        selected = self.prose_viewer.selected_source_range()
        board = self.storyboard_panel.board
        if not selected or not board or not self._source_unchanged(): return
        shot = next((s for s in board.shots if s.source_start <= selected[0] < s.source_end), None)
        if shot and selected[1] <= shot.source_end:
            self.storyboard_panel.select_id(shot.identifier)

    def _storyboard_settings(self) -> None:
        if self.session is None: return
        try:
            dialog = StoryboardSettingsDialog(self.session.database.storyboard_settings(), self)
            if dialog.exec() == dialog.Accepted:
                self.session.database.save_storyboard_settings(dialog.values())
                self.statusBar().showMessage("Storyboard settings saved for this project", 5000)
        except (ValueError, ProjectDatabaseError) as exc:
            self._show_error("Storyboard settings", str(exc))

    def _choose_storyboard_scenes(self) -> None:
        dialog = QDialog(self); dialog.setWindowTitle("Select Scenes for Storyboard")
        selection = QListWidget(); selection.setSelectionMode(QListWidget.MultiSelection)
        for scene in self.scenes_panel.scenes:
            selection.addItem(f"{scene.sequence}. {scene.title}")
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(dialog.accept); buttons.rejected.connect(dialog.reject)
        layout = QVBoxLayout(dialog); layout.addWidget(selection); layout.addWidget(buttons)
        if dialog.exec() == QDialog.Accepted:
            indices = [self.scenes_panel.scenes[selection.row(item)] for item in selection.selectedItems()]
            self._generate_storyboards(scenes_override=indices)

    def _generate_storyboards(self, all_scenes: bool = False, scenes_override=None) -> None:
        if not self.session or self._storyboard_task or self._task or self._scene_task or self._alignment_task: return
        try:
            self._require_current_scenes()
            scenes = scenes_override if scenes_override is not None else self.scenes_panel.scenes if all_scenes else [self.scenes_panel.current_scene()]
            scenes = [s for s in scenes if s]
            if not scenes: raise ValueError("Select a narrative scene")
            existing = [s for s in scenes if self.session.database.load_storyboard(s.identifier)]
            if existing:
                answer = QMessageBox.question(self, "Replace storyboards?",
                    f"{len(existing)} selected scene(s) already have storyboards. Replacing them will discard manual shot edits. Continue?",
                    QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
                if answer != QMessageBox.Yes: return
            ai = load_ai_settings()
            provider = make_provider(ai)
            settings = self.session.database.storyboard_settings()
            task = StoryboardTask(scenes, self._source_text, settings, provider,
                self._current_alignment(), ai.model, ai.chunk_chars, self)
            self._storyboard_task = task
            task.progress.connect(self.storyboard_panel.status.setText)
            task.succeeded.connect(self._storyboards_ready)
            task.failed.connect(self._storyboard_failed)
            task.finished.connect(self._storyboard_finished)
            task.finished.connect(task.deleteLater)
            self._refresh(); task.start()
        except (ValueError, ProjectDatabaseError) as exc:
            self._show_error("Storyboard generation", str(exc))

    def _cancel_storyboard(self) -> None:
        if self._storyboard_task:
            self._storyboard_task.cancel_event.set()
            self.storyboard_panel.status.setText("Canceling after the current AI request…")

    def _storyboards_ready(self, boards: list[Storyboard]) -> None:
        if not self.session or not self._storyboard_task: return
        try:
            if self._storyboard_task.cancel_event.is_set(): raise PlanningCanceled("Storyboard generation canceled")
            self._require_current_scenes()
            if any(b.prose_hash != source_hash(self._source_text) for b in boards):
                raise ValueError("Prose changed during planning")
            audio_path = self.session.project.resolved_audio(self.session.root)
            if any(b.audio_hash for b in boards) and (not audio_path or
                source_file_hash(audio_path) != boards[0].audio_hash):
                raise ValueError("Audio changed during planning")
            reconciled = []
            for board in boards:
                old = self.session.database.load_storyboard(board.scene_id)
                if old:
                    identities = {(s.source_start, s.source_end): s.identifier for s in old.shots}
                    board.shots = [s.model_copy(update={"identifier": identities.get((s.source_start, s.source_end), s.identifier)})
                                   for s in board.shots]
                reconciled.append(board)
            self.session.database.save_storyboards(reconciled, self._source_text, replace=True,
                audio_duration=self._alignment.audio_duration if self._alignment else None)
            self._load_selected_storyboard()
            self.statusBar().showMessage(f"Planned {sum(len(b.shots) for b in boards)} shots across {len(boards)} scenes", 8000)
        except (ValueError, ProjectDatabaseError, PlanningCanceled, OSError) as exc:
            self._storyboard_failed(str(exc))

    def _storyboard_failed(self, message: str) -> None:
        self.storyboard_panel.status.setText(message)
        if "canceled" not in message.lower(): self._show_error("Storyboard generation", message)

    def _storyboard_finished(self) -> None:
        self._storyboard_task = None
        self._refresh()

    def _edit_board(self, action) -> None:
        board = self.storyboard_panel.board
        if not board or not self.session: return
        try:
            self._require_current_scenes()
            state = self.session.database.storyboard_state(board, self._source_text, self._current_alignment())
            if state.startswith(("Prose changed", "Scene boundaries changed", "Audio alignment changed")):
                raise ValueError(state)
            updated = action(board)
            self.session.database.save_storyboard(updated, self._source_text,
                audio_duration=self._alignment.audio_duration if self._alignment else None)
            self.storyboard_panel.set_board(updated)
            self.statusBar().showMessage("Storyboard change saved", 5000)
        except (ValueError, ProjectDatabaseError) as exc:
            self._show_error("Storyboard edit", str(exc))

    def _save_shot(self) -> None:
        shot = self.storyboard_panel.current_shot()
        if shot:
            try:
                changes = self.storyboard_panel.changes()
            except ValueError:
                self._show_error("Storyboard edit", "Audio times must be numeric seconds or blank")
                return
            if (changes["audio_start"], changes["audio_end"]) == (shot.audio_start, shot.audio_end):
                for key in ("audio_start", "audio_end", "timing_status"):
                    changes.pop(key)
            self._edit_board(lambda board: update_shot(board, shot.identifier,
                changes, self._source_text,
                self._alignment.audio_duration if self._alignment else None))

    def _shot_split_offset(self, shot):
        from prose_to_video.pipeline.text_matching import sentence_ranges
        offsets = [shot.source_start+a for a,b in sentence_ranges(shot.original_prose)
                   if 0 < a < len(shot.original_prose)]
        if not offsets: raise ValueError("No internal sentence boundary for this shot")
        choices = {f"Character {o}: {self._source_text[o:o+65].splitlines()[0]}": o for o in offsets}
        label, ok = QInputDialog.getItem(self, "Shot boundary", "New shot begins at:", list(choices), 0, False)
        return choices[label] if ok else None

    def _split_shot(self) -> None:
        shot = self.storyboard_panel.current_shot()
        if not shot: return
        try:
            offset = self._shot_split_offset(shot)
            if offset is not None:
                self._edit_board(lambda board: split_shot(board, shot.identifier, offset,
                    self._source_text, self._current_alignment()))
        except ValueError as exc: self._show_error("Split shot", str(exc))

    def _merge_shot(self) -> None:
        shot = self.storyboard_panel.current_shot()
        if shot: self._edit_board(lambda board: merge_shots(board, shot.identifier, self._source_text))

    def _reorder_shot(self) -> None:
        shot = self.storyboard_panel.current_shot()
        if shot: self._edit_board(lambda board: swap_visuals_with_next(board, shot.identifier, self._source_text))

    def _insert_shot(self) -> None:
        shot = self.storyboard_panel.current_shot()
        if not shot: return
        try:
            offset = self._shot_split_offset(shot)
            if offset is not None:
                self._edit_board(lambda board: insert_shot(board, shot.identifier, offset,
                    self._source_text, self._current_alignment()))
        except ValueError as exc: self._show_error("Insert shot", str(exc))

    def _delete_shot(self) -> None:
        shot = self.storyboard_panel.current_shot()
        if not shot: return
        options = []
        board = self.storyboard_panel.board
        if shot.sequence > 1: options.append("Extend previous shot")
        if shot.sequence < len(board.shots): options.append("Extend next shot")
        if not options: return
        selected, ok = QInputDialog.getItem(self, "Delete shot", "Who covers the deleted interval?", options, 0, False)
        if ok:
            coverage = "previous" if selected == "Extend previous shot" else "next"
            self._edit_board(lambda b: delete_shot(b, shot.identifier, self._source_text, coverage))

    def _play_shot(self) -> None:
        shot = self.storyboard_panel.current_shot()
        board = self.storyboard_panel.board
        if not self._source_unchanged() or self.scenes_panel.outdated:
            self.statusBar().showMessage("Storyboard prose or scene boundaries are outdated", 6000); return
        if not shot or not board or shot.audio_start is None or shot.audio_end is None:
            self.statusBar().showMessage("This shot has no complete audio interval", 6000); return
        if self.session.database.storyboard_state(board, self._source_text, self._current_alignment()).startswith(
            ("Audio alignment changed", "Prose changed", "Scene boundaries changed")):
            self.statusBar().showMessage("Shot timing is outdated", 6000); return
        self.audio_player.play_range(shot.audio_start, shot.audio_end)

    def _export_storyboard(self) -> None:
        if not self.session: return
        filename, selected_filter = QFileDialog.getSaveFileName(self, "Export Storyboard",
            str(self.session.root / "storyboard" / "storyboard.json"), "JSON files (*.json);;Markdown files (*.md)")
        if not filename: return
        try:
            self._require_current_scenes()
            boards = [b for s in self.scenes_panel.scenes if (b := self.session.database.load_storyboard(s.identifier))]
            for b in boards:
                state = self.session.database.storyboard_state(b, self._source_text, self._current_alignment())
                if state.startswith(("Prose changed", "Scene boundaries changed", "Audio alignment changed")):
                    raise ValueError(state)
            path = Path(filename)
            if selected_filter.startswith("Markdown") or path.suffix == ".md":
                export_storyboard_markdown(path, self._source_text, self.scenes_panel.scenes, boards)
            else:
                export_storyboard_json(path, self._source_text, self.scenes_panel.scenes, boards)
            self.statusBar().showMessage(f"Storyboard exported to {path}", 8000)
        except (OSError, ValueError, ProjectDatabaseError) as exc:
            self._show_error("Storyboard export", str(exc))

    def _playback_error(self, message: str) -> None:
        self._show_error("Audio playback error", message)

    def closeEvent(self, event: QCloseEvent) -> None:
        if self._storyboard_task is not None:
            self._cancel_storyboard()
            event.ignore()
            self.statusBar().showMessage("Canceling storyboard generation; close when it finishes", 5000)
            return
        if self._alignment_task is not None:
            self._cancel_alignment()
            event.ignore()
            self.statusBar().showMessage("Canceling audio alignment; close when it finishes", 5_000)
            return
        if self._waveform_task is not None:
            event.ignore()
            self.statusBar().showMessage("Finishing waveform generation; close when it finishes", 5_000)
            return
        if self._scene_task is not None:
            self._cancel_scenes()
            event.ignore()
            self.statusBar().showMessage("Canceling scene analysis; close again when it finishes", 5_000)
            return
        if self._task is not None:
            event.ignore()
            self.statusBar().showMessage("Please wait for the current file operation to finish", 5_000)
            return
        self.audio_player.player.stop()
        super().closeEvent(event)
