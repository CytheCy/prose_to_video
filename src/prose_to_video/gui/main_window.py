"""Project workspace, media controls, and scene review."""

from __future__ import annotations

import logging
import json
import hashlib
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
from dataclasses import replace
from pathlib import Path
from threading import Event
from typing import Callable

from PySide6.QtCore import QThread, Qt, Signal, QTimer, QUrl
from PySide6.QtGui import QAction, QCloseEvent, QPixmap
from PySide6.QtWidgets import (QDialog, QDialogButtonBox, QFileDialog, QFormLayout, QGroupBox, QHBoxLayout,
                               QInputDialog, QLabel, QListWidget, QListWidgetItem, QMainWindow, QMessageBox, QPushButton,
                               QPlainTextEdit, QScrollArea, QSplitter, QTabWidget, QVBoxLayout, QWidget)

from prose_to_video.gui.audio_player import AudioPlayer
from prose_to_video.gui.alignment_settings import AlignmentSettingsDialog
from prose_to_video.gui.waveform import waveform_peaks
from prose_to_video.gui.ai_settings import AISettingsDialog, load_ai_settings, save_ai_settings
from prose_to_video.gui.project_manager import NewProjectDialog, SettingsDialog
from prose_to_video.gui.prose_viewer import ProseViewer
from prose_to_video.gui.scenes_panel import ScenesPanel
from prose_to_video.gui.storyboard_panel import StoryboardPanel
from prose_to_video.gui.image_prompt_panel import ImagePromptPanel
from prose_to_video.gui.image_gallery_panel import ImageGalleryPanel
from prose_to_video.gui.image_generation_settings import ImageGenerationSettingsDialog
from prose_to_video.gui.image_prompt_settings import PromptSettingsDialog
from prose_to_video.gui.visual_library_panel import VisualLibraryPanel
from prose_to_video.gui.storyboard_settings import StoryboardSettingsDialog
from prose_to_video.gui.video_production import VideoProductionPanel, RenderSettingsDialog, RenderTask
from prose_to_video.gui.animation_panel import AnimationPanel
from prose_to_video.storage.generated_videos import VideoStore
from prose_to_video.pipeline.video_generation import animation_status
from prose_to_video.models.video_render import RenderSettings
from prose_to_video.pipeline.video_assembly import build_render_plan
from prose_to_video.storage.video_renders import VideoRenderStore, output_path
from prose_to_video.rendering.ffmpeg_renderer import FFmpegRenderer
from prose_to_video.models.scene import Scene, source_hash
from prose_to_video.models.alignment import AlignmentRun
from prose_to_video.models.shot import Storyboard
from prose_to_video.pipeline.audio_transcription import (AlignmentCanceled, TranscriptionSettings,
    dependency_check, source_file_hash)
from prose_to_video.pipeline.audio_alignment import align_project
from prose_to_video.pipeline.providers import make_provider
from prose_to_video.pipeline.entity_extraction import extract_entities, reconcile_shot_links, ExtractionCanceled
from prose_to_video.storage.visual_library import VisualLibrary
from prose_to_video.storage.image_prompts import PromptStore
from prose_to_video.storage.generated_images import ImageStore
from prose_to_video.storage.image_export import image_manifest, manifest_markdown, export_approved_images, visual_timeline_manifest
from prose_to_video.pipeline.image_generation import ImageGenerator, eligible_shots, make_image_provider, asset_freshness
from prose_to_video.models.image_job import ImageGenerationSettings
from prose_to_video.storage.image_prompt_export import export_prompt_json, export_prompt_markdown
from prose_to_video.pipeline.image_prompt_generation import generate_batch, PromptCanceled
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


class ImageTask(QThread):
    progress = Signal(int, int, str)
    succeeded = Signal(str)

    def __init__(self, generator, prepared, parent=None):
        super().__init__(parent)
        self.generator=generator
        self.prepared=prepared
        self.cancel_event=Event()

    def run(self):
        failures=[]
        completed=0
        maximum=min(self.generator.store.settings().max_concurrent_jobs,len(self.prepared))
        next_index=0
        def process(job,request):
            try:
                self.generator.run(job,request,cancel=self.cancel_event)
                if job.status in {'Submitted','Running'} and job.provider_job_id:
                    while not self.cancel_event.is_set() and job.status in {'Submitted','Running'}:
                        self.msleep(800)
                        self.generator.reconcile(job)
                if self.cancel_event.is_set() and job.status in {'Submitted','Running'}:
                    self.generator.cancel(job)
                return f'{job.shot_id}: {job.error or job.status}' if job.status in {'Failed','Needs Reconciliation'} else ''
            except Exception as exc:
                return f'{job.shot_id}: {exc}'
        with ThreadPoolExecutor(max_workers=maximum) as pool:
            running={}
            while next_index<len(self.prepared) or running:
                while not self.cancel_event.is_set() and next_index<len(self.prepared) and len(running)<maximum:
                    job,request=self.prepared[next_index];next_index+=1
                    self.progress.emit(completed,len(self.prepared),f'{job.shot_id}: submitting')
                    running[pool.submit(process,job,request)]=job
                if self.cancel_event.is_set():
                    for job,_ in self.prepared[next_index:]:
                        if job.status=='Pending':job.status='Canceled';self.generator.store.update_job(job)
                    next_index=len(self.prepared)
                if not running:break
                done,_=wait(running,timeout=.5,return_when=FIRST_COMPLETED)
                for future in done:
                    job=running.pop(future);completed+=1
                    message=future.result()
                    if message:failures.append(message)
                    self.progress.emit(completed,len(self.prepared),f'{job.shot_id}: {job.status}')
        self.succeeded.emit('\n'.join(failures))


class ImageRecoveryTask(QThread):
    succeeded=Signal(str)
    def __init__(self,session,jobs,parent=None):
        super().__init__(parent);self.session=session;self.jobs=jobs;self.cancel_event=Event()
    def run(self):
        errors=[]
        for job in self.jobs:
            if self.cancel_event.is_set():break
            try:
                settings=ImageGenerationSettings.model_validate(job.settings)
                generator=ImageGenerator(self.session,make_image_provider(settings,self.session.root))
                if job.status=='Pending':generator.run(job,generator.request_for_job(job),cancel=self.cancel_event)
                else:generator.reconcile(job)
                if job.status in {'Failed','Needs Reconciliation'}:
                    errors.append(f'{job.shot_id}: {job.error or job.status}')
            except Exception as exc:errors.append(f'{job.shot_id}: {exc}')
        self.succeeded.emit('\n'.join(errors))


class PromptTask(QThread):
    succeeded = Signal(object)
    failed = Signal(str)
    progress = Signal(int, int)

    def __init__(self, shots, scenes, boards, root, project, settings, provider, source_path, regenerate=False, parent=None):
        super().__init__(parent)
        self.shots, self.scenes, self.boards = shots, scenes, boards
        self.root, self.project, self.settings, self.provider = root, project, settings, provider
        self.source_path, self.regenerate = source_path, regenerate
        self.cancel_event = Event()

    def run(self):
        try:
            from prose_to_video.utils.prose import read_prose
            initial = source_hash(read_prose(self.source_path))
            if any(board and board.prose_hash != initial for board in self.boards.values()):
                raise ValueError('Storyboard prose is outdated')
            def check():
                if source_hash(read_prose(self.source_path)) != initial:
                    raise ValueError('Prose changed during prompt generation')
            result = generate_batch(self.shots, self.scenes, self.boards, VisualLibrary(self.root),
                self.project, self.settings, self.provider, PromptStore(self.root),
                regenerate=self.regenerate, cancel=self.cancel_event,
                progress=lambda n,total: self.progress.emit(n,total), source_check=check)
            self.succeeded.emit(result)
        except PromptCanceled:
            self.failed.emit('Prompt generation canceled; completed versions were saved')
        except Exception as exc:
            self.failed.emit(f'Prompt generation stopped: {type(exc).__name__}: {exc}')


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

class EntityTask(QThread):
    succeeded = Signal(object)
    failed = Signal(str)
    progress = Signal(str)

    def __init__(self, scenes, library, provider, digest, source_path, chunk_chars, parent=None):
        super().__init__(parent)
        self.scenes, self.library, self.provider, self.digest = scenes, library, provider, digest
        self.source_path, self.chunk_chars = source_path, chunk_chars
        self.cancel_event = Event()

    def run(self):
        try:
            result = extract_entities(self.scenes, self.library, self.provider,
                cancel=self.cancel_event, manuscript_hash=self.digest,
                chunk_chars=self.chunk_chars,
                source_check=lambda: self._check_source(),
                progress=lambda current, total: self.progress.emit(f'Extracting entities: {current}/{total} scenes'))
            self.succeeded.emit(result)
        except ExtractionCanceled:
            self.failed.emit('Entity extraction canceled; completed scenes were preserved')
        except Exception as exc:
            self.failed.emit(f'Entity extraction failed: {exc}')

    def _check_source(self):
        if source_hash(read_prose(self.source_path)) != self.digest:
            raise ValueError('Prose changed during entity extraction; current scene was not saved')


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.session: ProjectSession | None = None
        self._task: FileTask | None = None
        self._scene_task: SceneTask | None = None
        self._alignment_task: AlignmentTask | None = None
        self._waveform_task: WaveformTask | None = None
        self._storyboard_task: StoryboardTask | None = None
        self._entity_task: EntityTask | None = None
        self._prompt_task: PromptTask | None = None
        self._image_task: ImageTask | None = None
        self._render_task: RenderTask | None = None
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
            "Prose to Video — Stage Eight\nNarrated video assembly, camera motion, and MP4 rendering."
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
        self.image_prompt_panel = ImagePromptPanel()
        self.image_gallery_panel = ImageGalleryPanel()
        self.visual_library_panel = VisualLibraryPanel()
        self.video_production_panel = VideoProductionPanel()
        self.animation_panel = AnimationPanel()
        self.animation_panel.status_changed.connect(self._refresh_animation_states)
        self.workspace_tabs = QTabWidget()
        self.workspace_tabs.addTab(self.scenes_panel, "Scenes")
        self.workspace_tabs.addTab(self.storyboard_panel, "Storyboard")
        self.workspace_tabs.addTab(self.visual_library_panel, "Visual Library")
        self.workspace_tabs.addTab(self.image_prompt_panel, "Image Prompts")
        self.workspace_tabs.addTab(self.image_gallery_panel, "Image Gallery")
        self.workspace_tabs.addTab(self.animation_panel, "AI Animation")
        self.workspace_tabs.addTab(self.video_production_panel, "Video Production")
        self.video_production_panel.validate_requested.connect(self._validate_video)
        self.video_production_panel.settings_requested.connect(self._render_settings)
        self.video_production_panel.render_requested.connect(self._render_video)
        self.video_production_panel.cancel_requested.connect(self._cancel_render)
        self.video_production_panel.open_folder_requested.connect(self._open_render_folder)
        self.video_production_panel.history_requested.connect(self._refresh_render_history)
        self.video_production_panel.clear_cache_requested.connect(self._clear_render_cache)
        self.video_production_panel.shot_override_requested.connect(self._save_shot_render_override)
        self.video_production_panel.shot_mode_requested.connect(self._save_shot_production_mode)
        self.video_production_panel.shot_coverage_requested.connect(self._save_shot_coverage)
        self.image_gallery_panel.generate_requested.connect(self._generate_images)
        self.image_gallery_panel.approve_requested.connect(self._approve_image)
        self.image_gallery_panel.reject_requested.connect(self._reject_image)
        self.image_gallery_panel.lock_requested.connect(self._lock_image)
        self.image_gallery_panel.refine_requested.connect(self._refine_image)
        self.image_gallery_panel.cancel_requested.connect(self._cancel_images)
        self.image_gallery_panel.settings_requested.connect(self._image_settings)
        self.image_gallery_panel.export_requested.connect(self._export_images)
        self.image_gallery_panel.details_requested.connect(self._image_details)
        self.image_gallery_panel.compare_requested.connect(self._compare_images)
        self.image_gallery_panel.recovery_requested.connect(self._recover_image_jobs)
        self.visual_library_panel.extract_requested.connect(self._extract_entities)
        self.visual_library_panel.cancel_requested.connect(self._cancel_entity_extraction)
        self.visual_library_panel.changed.connect(self._visuals_changed)
        self.visual_library_panel.style_override_requested.connect(self._visual_style_override)
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
        self.image_prompt_panel.generate_requested.connect(self._generate_prompts)
        self.image_prompt_panel.cancel_requested.connect(self._cancel_prompts)
        self.image_prompt_panel.settings_requested.connect(self._prompt_settings)
        self.image_prompt_panel.save_requested.connect(self._save_prompt)
        self.image_prompt_panel.approve_requested.connect(self._approve_prompt)
        self.image_prompt_panel.restore_requested.connect(self._restore_prompt)
        self.image_prompt_panel.export_requested.connect(self._export_prompts)
        self.storyboard_panel.add_visual_requested.connect(self._add_shot_visual)
        self.storyboard_panel.remove_visual_requested.connect(self._remove_shot_visual)
        self.storyboard_panel.pin_reference_requested.connect(self._pin_shot_reference)
        self.storyboard_panel.unpin_reference_requested.connect(self._unpin_shot_reference)
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
        busy = self._task is not None or self._scene_task is not None or self._alignment_task is not None or self._storyboard_task is not None or self._entity_task is not None or self._prompt_task is not None or self._image_task is not None or self._render_task is not None
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
        self.video_production_panel.player.stop()
        self.video_production_panel.player.setSource(QUrl())
        self.video_production_panel.summary.setText('Open a project to inspect video readiness.' if not session else f'{session.project.name} · Video Production')
        self.video_production_panel.status.setText('Not validated')
        self.video_production_panel.set_shot_override(None, None)
        self.video_production_panel.shot_statuses.clear()
        self.animation_panel.bind(session,None,None)
        if session:
            VideoRenderStore(session.root).recover()
            video_interrupted=VideoStore(session.root).recover()
            if any(job.provider_job_id for job in video_interrupted):
                QTimer.singleShot(0,lambda opened=session:
                    self.animation_panel.reconcile_jobs(automatic=True) if self.session==opened else None)
            self._refresh_render_history()
        self._display_stats = None
        self._source_text = ""
        self._prose_ready = False
        self._source_stamp = None
        self._audio_hash = None
        self._audio_stamp = None
        self._alignment = None
        self._storyboard_stale = False
        self.storyboard_panel.set_board(None)
        self.image_prompt_panel.set_prompt(None, [], None)
        self.image_gallery_panel.set_shot(None, [], None, None)
        image_notice=''
        if session:
            image_store=ImageStore(session.root)
            interrupted=image_store.recover()
            missing=image_store.missing_assets()
            if missing:
                image_notice=f'{len(missing)} generated image files are missing'
            if interrupted:
                image_notice+=(' · ' if image_notice else '')+f'{len(interrupted)} image jobs need reconciliation'
                QTimer.singleShot(0,lambda opened=session:
                    self._recover_image_jobs(automatic=True) if self.session==opened else None)
        self.visual_library_panel.set_library(VisualLibrary(session.root) if session else None)
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
            image_notice or (f"Project: {session.project.name}" if session else "Project closed"), 15_000 if image_notice else 5_000
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
        self.video_production_panel.set_shot_override(shot,
            VideoRenderStore(self.session.root).shot_override(shot.identifier) if self.session and shot else None)
        self.video_production_panel.set_production_mode(VideoStore(self.session.root).production(shot.identifier)[0] if self.session and shot else 'Camera Motion')
        if self.session and shot:
            _,coverage,fallback=VideoStore(self.session.root).production(shot.identifier)
            self.video_production_panel.set_coverage(coverage,fallback)
        self.video_production_panel.animation_state.setText(animation_status(self.session,shot) if self.session and shot else 'Production mode: —')
        self.animation_panel.bind(self.session,self.scenes_panel.current_scene(),shot)
        if shot and self._prose_ready and self._source_unchanged():
            self.prose_viewer.set_layer("shot", (shot.source_start, shot.source_end), scroll=True)
        else:
            self.prose_viewer.set_layer("shot", None)
        self._refresh_shot_visuals()
        self._load_prompt()
        self._load_gallery()

    def _refresh_shot_visuals(self) -> None:
        widget = self.storyboard_panel.visual_references
        widget.clear()
        shot = self.storyboard_panel.current_shot()
        if not self.session or not shot: return
        library = VisualLibrary(self.session.root)
        for entity in library.entities_for_shot(shot.identifier):
            refs = [r for r in library.references(entity.identifier) if r.status == 'Approved']
            roles = ', '.join(f'{r.role}: {Path(library.asset(r.asset_id).path).name}' for r in refs if library.asset(r.asset_id))
            item = f'{entity.name} · {entity.entity_type} · {roles or "No approved images"}'
            widget.addItem(item)
            widget.item(widget.count()-1).setData(Qt.UserRole, entity.identifier)
            widget.item(widget.count()-1).setData(Qt.UserRole + 1, 'entity')
        for asset in library.shot_assets(shot.identifier):
            widget.addItem(f'Pinned image · {asset.path}')
            widget.item(widget.count()-1).setData(Qt.UserRole, asset.identifier)
            widget.item(widget.count()-1).setData(Qt.UserRole + 1, 'asset')

    def _add_shot_visual(self):
        shot = self.storyboard_panel.current_shot()
        if not self.session or not shot: return
        library = VisualLibrary(self.session.root)
        entities = library.entities()
        if not entities: return
        labels = [f'{e.name} [{e.entity_type}, {e.identifier[:8]}]' for e in entities]
        choice, ok = QInputDialog.getItem(self, 'Add Visual Entity', 'Entity:', labels, editable=False)
        if not ok: return
        entity = entities[labels.index(choice)]
        variants = [v for v in library.variants(entity.identifier) if v.status == 'Approved']
        options = ['Default', *[v.name for v in variants]]
        selection, ok = QInputDialog.getItem(self, 'Appearance Variant', 'Use variant:', options, editable=False)
        if not ok: return
        variant_id = None if selection == 'Default' else variants[options.index(selection)-1].identifier
        try:
            library.relate_shot(entity.identifier, shot.identifier, variant_id)
            self._refresh_shot_visuals(); self.visual_library_panel.refresh()
        except (ValueError, ProjectDatabaseError) as exc: self._show_error('Visual reference', str(exc))

    def _remove_shot_visual(self):
        shot = self.storyboard_panel.current_shot()
        item = self.storyboard_panel.visual_references.currentItem()
        if not self.session or not shot or not item or item.data(Qt.UserRole + 1) != 'entity': return
        try:
            VisualLibrary(self.session.root).relate_shot(item.data(Qt.UserRole), shot.identifier, present=False)
            self._refresh_shot_visuals(); self.visual_library_panel.refresh()
        except (ValueError, ProjectDatabaseError) as exc: self._show_error('Visual reference', str(exc))

    def _pin_shot_reference(self):
        shot = self.storyboard_panel.current_shot()
        if not self.session or not shot: return
        library = VisualLibrary(self.session.root)
        options = []
        for entity in library.entities_for_shot(shot.identifier):
            for ref in library.references(entity.identifier):
                if ref.status == 'Approved' and (asset := library.asset(ref.asset_id)):
                    options.append((f'{entity.name} · {ref.role} · {asset.path}', asset.identifier))
        if not options:
            self.statusBar().showMessage('Associate an entity with an approved image first', 5000)
            return
        label, ok = QInputDialog.getItem(self, 'Pin Approved Image', 'Reference:',
                                          [option[0] for option in options], editable=False)
        if not ok: return
        try:
            library.relate_shot_asset(shot.identifier, next(asset_id for title, asset_id in options if title == label))
            self._refresh_shot_visuals()
        except (ValueError, ProjectDatabaseError) as exc: self._show_error('Visual reference', str(exc))

    def _unpin_shot_reference(self):
        shot = self.storyboard_panel.current_shot()
        item = self.storyboard_panel.visual_references.currentItem()
        if not self.session or not shot or not item or item.data(Qt.UserRole + 1) != 'asset': return
        try:
            VisualLibrary(self.session.root).relate_shot_asset(shot.identifier, item.data(Qt.UserRole), False)
            self._refresh_shot_visuals()
        except (ValueError, ProjectDatabaseError) as exc: self._show_error('Visual reference', str(exc))

    def _visuals_changed(self):
        self._refresh_shot_visuals()

    def _visual_style_override(self, scope, style):
        if not self.session: return
        target = self.scenes_panel.current_scene() if scope == 'scene' else self.storyboard_panel.current_shot()
        if not target:
            self._show_error('Visual style', f'Select a {scope} first')
            return
        try:
            VisualLibrary(self.session.root).save_style_override(scope, target.identifier, style)
            self.statusBar().showMessage(f'Visual style override saved for {scope}', 5000)
        except (ValueError, ProjectDatabaseError) as exc: self._show_error('Visual style', str(exc))

    def _extract_entities(self, scope: str):
        if not self.session or self._entity_task: return
        try:
            self._require_current_scenes()
            if scope == 'all':
                scenes = self.scenes_panel.scenes
            elif scope == 'multiple':
                dialog = QDialog(self); dialog.setWindowTitle('Select Scenes for Entity Extraction')
                choices = QListWidget(dialog); choices.setSelectionMode(QListWidget.MultiSelection)
                for scene in self.scenes_panel.scenes: choices.addItem(f'{scene.sequence}. {scene.title}')
                buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
                buttons.accepted.connect(dialog.accept); buttons.rejected.connect(dialog.reject)
                layout = QVBoxLayout(dialog); layout.addWidget(choices); layout.addWidget(buttons)
                if dialog.exec() != QDialog.Accepted: return
                scenes = [self.scenes_panel.scenes[choices.row(item)] for item in choices.selectedItems()]
            else:
                scenes = [self.scenes_panel.current_scene()]
            scenes = [s for s in scenes if s]
            if not scenes: raise ValueError('Select a narrative scene')
            settings = load_ai_settings()
            provider = make_provider(settings)
            task = EntityTask(scenes, VisualLibrary(self.session.root), provider, source_hash(self._source_text),
                              self.session.project.resolved_prose(self.session.root), settings.chunk_chars, self)
            self._entity_task = task
            task.progress.connect(self.visual_library_panel.status.setText)
            task.succeeded.connect(self._entities_ready)
            task.failed.connect(lambda message: self._show_error('Entity extraction', message))
            task.finished.connect(self._entity_finished)
            task.finished.connect(task.deleteLater)
            self._refresh(); task.start()
        except (ValueError, ProjectDatabaseError, OSError) as exc:
            self._show_error('Entity extraction', str(exc))

    def _cancel_entity_extraction(self):
        if self._entity_task:
            self._entity_task.cancel_event.set()
            self.visual_library_panel.status.setText('Canceling after the current AI request…')

    def _entities_ready(self, created):
        if not self.session: return
        self.visual_library_panel.refresh()
        self.visual_library_panel.status.setText(f'Extracted entities; {len(created)} new suggestion(s)')

    def _entity_finished(self):
        self._entity_task = None
        self._refresh()

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
            visual_library = VisualLibrary(self.session.root)
            for board in reconciled:
                scene = next((s for s in self.scenes_panel.scenes if s.identifier == board.scene_id), None)
                if scene: reconcile_shot_links(scene, visual_library)
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

    def _load_prompt(self):
        shot = self.storyboard_panel.current_shot()
        if not self.session or not shot:
            self.image_prompt_panel.set_prompt(None, [], None)
            return
        store = PromptStore(self.session.root)
        prompt = store.active(shot.identifier)
        versions = store.versions(shot.identifier)
        self.image_prompt_panel.set_prompt(prompt, versions, self.session.root)
        if prompt:
            scene = self.scenes_panel.current_scene()
            board = self.storyboard_panel.board
            if scene and board:
                state = store.state(prompt, scene, shot, board, self.session.project,
                                    VisualLibrary(self.session.root))
                self.image_prompt_panel.status.setText(f'v{prompt.version} · {state}')

    def _load_gallery(self):
        shot=self.storyboard_panel.current_shot()
        if not self.session or not shot:
            self.image_gallery_panel.set_shot(None,[],None,None)
            return
        store=ImageStore(self.session.root)
        totals=store.totals()
        actual=f"{totals['actual_cost']:.2f}" if totals['actual_cost_count'] else 'Unavailable'
        estimate=f"{totals['estimated_cost']:.2f}" if totals['estimated_cost_count'] else 'Unavailable'
        self.image_gallery_panel.summary.setText(
            f"Images: {totals['images']} · Completed jobs: {totals['completed']} · "
            f"Failed: {totals['failed']} · Pending: {totals['pending']} · "
            f"Recorded actual cost: {actual} · Estimated cost: {estimate}")
        approved=store.approved(shot.identifier)
        assets=store.assets(shot.identifier)
        self.image_gallery_panel.set_shot(shot,assets,approved[0] if approved else None,self.session.root)
        if approved and (self.session.root/approved[0].path).is_file():
            pix=QPixmap(str(self.session.root/approved[0].path))
            self.storyboard_panel.preview.setPixmap(pix.scaled(360,220,Qt.KeepAspectRatio,Qt.SmoothTransformation))
        else:
            self.storyboard_panel.preview.setPixmap(QPixmap())
            self.storyboard_panel.preview.setText('Image missing' if approved else 'No approved image')
        board=self.storyboard_panel.board
        if board:
            states={}
            selections=store.approved_map()
            jobs_by_shot={}
            for image_job in store.jobs():jobs_by_shot.setdefault(image_job.shot_id,[]).append(image_job)
            asset_shots={asset.shot_id for asset in store.assets()}
            for s in board.shots:
                selection=selections.get(s.identifier)
                jobs=jobs_by_shot.get(s.identifier,[])
                if selection:
                    scene=self.scenes_panel.current_scene()
                    freshness=asset_freshness(self.session,scene,s,board,selection[0]) if scene else 'Outdated'
                    states[s.identifier]='Approved' if freshness=='Current' else freshness
                elif any(j.status in {'Pending','Submitted','Running','Needs Reconciliation'} for j in jobs):states[s.identifier]='Generating'
                elif s.identifier in asset_shots:states[s.identifier]='Awaiting review'
                elif any(j.status=='Failed' for j in jobs):states[s.identifier]='Failed'
                else:states[s.identifier]='Missing'
            self.storyboard_panel.set_image_states(states)
        self._refresh_animation_states()

    def _image_settings(self):
        if not self.session:return
        store=ImageStore(self.session.root)
        dialog=ImageGenerationSettingsDialog(store.settings(),self)
        if dialog.exec()==QDialog.Accepted:
            try:
                values=dialog.values()
                if values.provider=='ComfyUI' and values.workflow_path:
                    source=Path(values.workflow_path)
                    if not source.is_absolute():source=self.session.root/source
                    data=source.read_bytes()
                    if not isinstance(json.loads(data),dict):raise ValueError('ComfyUI API workflow must be a JSON object')
                    directory=self.session.root/'storyboard'/'workflows';directory.mkdir(parents=True,exist_ok=True)
                    target=directory/(hashlib.sha256(data).hexdigest()+'.json')
                    if not target.exists():target.write_bytes(data)
                    values.workflow_path=target.relative_to(self.session.root).as_posix()
                store.save_settings(values)
                self._load_gallery()
            except (OSError,ValueError) as exc:self._show_error('Image settings',str(exc))

    def _generate_images(self,scope):
        if not self.session or self._image_task:return
        scene=self.scenes_panel.current_scene();shot=self.storyboard_panel.current_shot()
        if scope in {'shot','variations'} and (not scene or not shot):return
        if scope=='scene' and not scene:return
        settings=ImageStore(self.session.root).settings()
        if scope=='variations':settings=settings.model_copy(update={'candidates':max(2,settings.candidates)})
        try:
            generator=ImageGenerator(self.session)
            scenes=self.session.database.load_scenes()[1]
            if scope in {'shot','variations'}:
                board=self.storyboard_panel.board
                targets=[(scene,shot,board)]
                counts={'selected':1,'approved':int(bool(generator.store.approved(shot.identifier))),
                        'missing':int(not bool(generator.store.assets(shot.identifier))),'outdated':0,'skipped':0}
            else:
                targets,counts=eligible_shots(self.session,scenes,
                    scene_ids={scene.identifier} if scope=='scene' and scene else None)
            if not targets:
                QMessageBox.information(self,'Image generation','No eligible shots with approved, current prompts');return
            cost=(len(targets)*settings.candidates*settings.estimated_cost_per_image
                  if settings.estimated_cost_per_image is not None else None)
            summary=(f"Selected shots: {counts['selected']}\nAlready approved: {counts['approved']}\n"
                f"Missing images: {counts['missing']}\nOutdated prompts: {counts['outdated']}\n"
                f"Skipped: {counts['skipped']}\nProvider: {settings.provider}\n"
                f"Candidates per shot: {settings.candidates}\n"
                f"Estimated cost: {cost:.2f}" if cost is not None else
                f"Selected shots: {counts['selected']}\nAlready approved: {counts['approved']}\n"
                f"Missing images: {counts['missing']}\nOutdated prompts: {counts['outdated']}\n"
                f"Skipped: {counts['skipped']}\nProvider: {settings.provider}\n"
                f"Candidates per shot: {settings.candidates}\nCost estimate unavailable")
            prepared=[]
            for target_scene,target_shot,target_board in targets:
                try:
                    job,request,_=generator.prepare(target_scene,target_shot,target_board,settings=settings)
                    prepared.append((job,request))
                except ValueError as exc:
                    if scope in {'shot','variations'} and 'outdated' in str(exc).lower():
                        if QMessageBox.question(self,'Outdated approved prompt',str(exc)+'\n\nGenerate with this approved version anyway?')==QMessageBox.Yes:
                            job,request,_=generator.prepare(target_scene,target_shot,target_board,
                                settings=settings,allow_outdated=True)
                            prepared.append((job,request))
                    else:raise
            if not prepared:return
            omitted=[f'{job.shot_id}: {", ".join(job.excluded_references)}' for job,_ in prepared if job.excluded_references]
            if omitted:summary+='\nExcluded references (provider limit):\n'+'\n'.join(omitted)
            transforms=sorted({t for job,_ in prepared for t in job.transformations})
            if transforms:summary+='\nProvider translation: '+'; '.join(transforms)
            if QMessageBox.question(self,'Submit image generation',summary+'\n\nSubmit these jobs?')!=QMessageBox.Yes:return
            for job,_ in prepared:generator.store.save_job(job)
            task=ImageTask(generator,prepared,self);self._image_task=task
            task.progress.connect(lambda n,total,msg:self.image_gallery_panel.status.setText(f'{n}/{total} · {msg}'))
            task.succeeded.connect(self._images_finished)
            task.finished.connect(self._image_thread_finished)
            task.start()
            self._refresh()
        except Exception as exc:self._show_error('Image generation',str(exc))

    def _images_finished(self,failures):
        self._load_gallery()
        if failures:QMessageBox.warning(self,'Image generation',failures)

    def _image_thread_finished(self):
        self._image_task=None
        self._refresh()
        self._load_gallery()

    def _cancel_images(self):
        if self._image_task:self._image_task.cancel_event.set()
        elif self.session:
            store=ImageStore(self.session.root)
            pending=store.jobs('Pending')
            if pending and QMessageBox.question(self,'Cancel pending jobs',
                    f'Cancel {len(pending)} jobs that have not been submitted?')==QMessageBox.Yes:
                for job in pending:
                    job.status='Canceled';store.update_job(job)
                self._load_gallery()
            elif not pending:self.image_gallery_panel.status.setText('No pending image jobs')

    def _recover_image_jobs(self,automatic=False):
        if not self.session or self._image_task:return
        jobs=ImageStore(self.session.root).jobs()
        if automatic:
            jobs=[j for j in jobs if j.status in {'Needs Reconciliation','Submitted','Running'} and j.provider_job_id]
        else:
            pending=[j for j in jobs if j.status=='Pending']
            uncertain=[j for j in jobs if j.status in {'Needs Reconciliation','Submitted','Running'} and j.provider_job_id]
            unresolved=[j for j in jobs if j.status=='Needs Reconciliation' and not j.provider_job_id]
            if unresolved:
                QMessageBox.information(self,'Uncertain image jobs',
                    f'{len(unresolved)} requests have no provider job ID. They will not be resubmitted automatically.')
            if pending and QMessageBox.question(self,'Resume image jobs',
                    f'Submit {len(pending)} pending image jobs? This may incur charges.')==QMessageBox.Yes:
                jobs=pending+uncertain
            else:jobs=uncertain
        if not jobs:return
        task=ImageRecoveryTask(self.session,jobs,self);self._image_task=task
        task.succeeded.connect(self._images_finished);task.finished.connect(self._image_thread_finished);task.start()
        self._refresh()

    def _approve_image(self,asset_id):
        if not self.session or not asset_id:return
        try:ImageStore(self.session.root).approve(asset_id);self._load_gallery()
        except Exception as exc:self._show_error('Approve image',str(exc))

    def _reject_image(self,asset_id):
        if not self.session or not asset_id:return
        try:ImageStore(self.session.root).reject(asset_id);self._load_gallery()
        except Exception as exc:self._show_error('Reject image',str(exc))

    def _lock_image(self,locked):
        shot=self.storyboard_panel.current_shot()
        if not self.session or not shot:return
        try:ImageStore(self.session.root).lock(shot.identifier,locked);self._load_gallery()
        except Exception as exc:self._show_error('Lock image',str(exc))

    def _refine_image(self,asset_id):
        if not self.session or not asset_id:return
        scene=self.scenes_panel.current_scene();shot=self.storyboard_panel.current_shot()
        if not scene or not shot:return
        instruction,ok=QInputDialog.getMultiLineText(self,'Refine image',
            'Describe the requested change. The approved prompt remains unchanged:')
        if not ok or not instruction.strip():return
        try:
            generator=ImageGenerator(self.session)
            job,request,_=generator.prepare(scene,shot,self.storyboard_panel.board,parent_id=asset_id,
                revision_instruction=instruction.strip())
            if QMessageBox.question(self,'Refine image','Generate a new candidate from the selected image?')!=QMessageBox.Yes:return
            generator.store.save_job(job)
            task=ImageTask(generator,[(job,request)],self);self._image_task=task
            task.progress.connect(lambda n,total,msg:self.image_gallery_panel.status.setText(msg))
            task.succeeded.connect(self._images_finished);task.finished.connect(self._image_thread_finished);task.start()
            self._refresh()
        except Exception as exc:self._show_error('Refine image',str(exc))

    def _image_details(self,asset_id):
        if not self.session or not asset_id:return
        store=ImageStore(self.session.root);asset=store.asset(asset_id)
        if asset:
            job=store.job(asset.job_id)
            QMessageBox.information(self,'Generation details',json.dumps(job.model_dump(mode='json'),indent=2))

    def _compare_images(self):
        if not self.session:return
        assets=self.image_gallery_panel.assets[:4]
        if len(assets)<2:return
        dialog=QDialog(self);dialog.setWindowTitle('Compare Candidates')
        layout=QHBoxLayout(dialog)
        for asset in assets:
            label=QLabel();label.setPixmap(QPixmap(str(self.session.root/asset.thumbnail_path)).scaled(300,240,Qt.KeepAspectRatio))
            layout.addWidget(label)
        dialog.exec()

    def _export_images(self):
        if not self.session:return
        choice,ok=QInputDialog.getItem(self,'Export images','Format',
            ['Approved images and manifest','Generation JSON','Generation Markdown','Visual timeline JSON'],0,False)
        if not ok:return
        try:
            scenes=self.session.database.load_scenes()[1]
            if choice=='Approved images and manifest':
                folder=QFileDialog.getExistingDirectory(self,'Export approved images')
                if folder:export_approved_images(self.session,scenes,folder)
            else:
                suffix='.md' if choice=='Generation Markdown' else '.json'
                path,_=QFileDialog.getSaveFileName(self,'Export image manifest',str(self.session.root/'images'/('manifest'+suffix)))
                if path:
                    rows=visual_timeline_manifest(self.session,scenes) if choice=='Visual timeline JSON' else image_manifest(self.session,scenes)
                    Path(path).write_text(manifest_markdown(rows) if suffix=='.md' else json.dumps(rows,indent=2),encoding='utf-8')
        except Exception as exc:self._show_error('Export images',str(exc))

    def _prompt_settings(self):
        if not self.session: return
        store = PromptStore(self.session.root)
        dialog = PromptSettingsDialog(store.settings(), self)
        if dialog.exec() == QDialog.Accepted:
            store.save_settings(dialog.values())
            self._load_prompt()

    def _generate_prompts(self, scope):
        if not self.session or self._prompt_task: return
        try:
            self._require_current_scenes()
            scene = self.scenes_panel.current_scene()
            if not scene: raise ValueError('Select a scene and storyboard shot')
            selected = self.storyboard_panel.current_shot()
            scenes = self.scenes_panel.scenes if scope == 'all' else [scene]
            boards = {s.identifier: self.session.database.load_storyboard(s.identifier) for s in scenes}
            if scope in {'shot', 'regenerate'} and selected is None:
                raise ValueError('Select a storyboard shot')
            shots = ([selected] if scope in {'shot','regenerate'} and selected else
                     [shot for s in scenes if boards[s.identifier] for shot in boards[s.identifier].shots])
            if not shots: raise ValueError('No storyboard shots selected')
            regenerate = scope == 'regenerate'
            if regenerate and PromptStore(self.session.root).approved(shots[0].identifier):
                answer = QMessageBox.question(self, 'Generate another version?',
                    'The approved prompt will remain active. Generate a new version for review?',
                    QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
                if answer != QMessageBox.Yes: return
            ai = load_ai_settings()
            settings = PromptStore(self.session.root).settings()
            if settings.model: ai = replace(ai, model=settings.model)
            provider = make_provider(ai)
            path = self.session.project.resolved_prose(self.session.root)
            task = PromptTask(shots, {s.identifier:s for s in scenes}, boards, self.session.root,
                self.session.project, settings, provider, path, regenerate, self)
            self._prompt_task = task
            task.progress.connect(lambda n,total: self.image_prompt_panel.status.setText(f'Generating prompts {n}/{total}'))
            task.succeeded.connect(lambda result: self._prompt_done(f'{len(result)} prompt(s) generated'))
            task.failed.connect(self._prompt_done)
            task.finished.connect(self._prompt_finished)
            task.finished.connect(task.deleteLater)
            self._refresh(); task.start()
        except (ValueError, ProjectDatabaseError) as exc:
            self._show_error('Image prompts', str(exc))

    def _cancel_prompts(self):
        if self._prompt_task:
            self._prompt_task.cancel_event.set()
            self.image_prompt_panel.status.setText('Canceling after the current request…')

    def _prompt_done(self, message):
        if isinstance(message, str) and message.startswith('Prompt generation stopped'):
            self._show_error('Image prompts', message)
        else:
            self.statusBar().showMessage(str(message), 6000)
        self._load_prompt()

    def _prompt_finished(self):
        self._prompt_task = None
        self._refresh()

    def _save_prompt(self):
        if not self.session: return
        try:
            shot = self.storyboard_panel.current_shot()
            positive, negative, components = self.image_prompt_panel.changes()
            PromptStore(self.session.root).edit(shot.identifier, positive_prompt=positive,
                negative_prompt=negative, components=components,
                notes=components.generation_notes)
            self._load_prompt()
        except (ValueError, ProjectDatabaseError) as exc:
            self._show_error('Save prompt', str(exc))

    def _approve_prompt(self):
        if not self.session: return
        try:
            shot = self.storyboard_panel.current_shot()
            scene = self.scenes_panel.current_scene()
            prompt = self.image_prompt_panel.prompt
            PromptStore(self.session.root).approve(shot.identifier, prompt.version, scene, shot,
                self.storyboard_panel.board, self.session.project, VisualLibrary(self.session.root))
            self._load_prompt()
        except (ValueError, ProjectDatabaseError) as exc:
            self._show_error('Approve prompt', str(exc))

    def _restore_prompt(self, version):
        if not self.session: return
        try:
            shot = self.storyboard_panel.current_shot()
            PromptStore(self.session.root).restore(shot.identifier, version)
            self._load_prompt()
        except (ValueError, ProjectDatabaseError) as exc:
            self._show_error('Restore prompt', str(exc))

    def _export_prompts(self):
        if not self.session: return
        scope, accepted = QInputDialog.getItem(self, 'Export prompts', 'Scope',
            ['Selected prompt', 'Selected scene', 'All scenes'], 0, False)
        if not accepted: return
        path, _ = QFileDialog.getSaveFileName(self, 'Export image prompts',
            str(self.session.root / 'storyboard' / 'image_prompts.json'),
            'JSON (*.json);;Markdown (*.md)')
        if not path: return
        try:
            scenes = self.scenes_panel.scenes if scope == 'All scenes' else [self.scenes_panel.current_scene()]
            boards = {s.identifier:self.session.database.load_storyboard(s.identifier) for s in scenes if s}
            shots = {self.storyboard_panel.current_shot().identifier} if scope == 'Selected prompt' else None
            store = PromptStore(self.session.root)
            content = (export_prompt_markdown if path.lower().endswith('.md') else export_prompt_json)(
                scenes, boards, store, shot_ids=shots, project=self.session.project,
                library=VisualLibrary(self.session.root))
            Path(path).write_text(content, encoding='utf-8')
            self.statusBar().showMessage(f'Prompts exported to {path}', 8000)
        except (OSError, ValueError, ProjectDatabaseError) as exc:
            self._show_error('Export prompts', str(exc))

    def _render_settings(self):
        if not self.session: return
        store = VideoRenderStore(self.session.root)
        dialog = RenderSettingsDialog(store.settings(), self)
        if dialog.exec() != QDialog.Accepted: return
        try:
            settings = dialog.value()
            store.save_settings(settings)
            self.video_production_panel.status.setText(f'Saved {settings.width} × {settings.height} at {settings.frame_rate} fps')
        except ValueError as exc:
            QMessageBox.warning(self, 'Invalid render settings', str(exc))

    def _save_shot_render_override(self, override):
        shot = self.storyboard_panel.current_shot()
        if not self.session or not shot: return
        try:
            VideoRenderStore(self.session.root).save_shot_override(shot.identifier, override)
            self.video_production_panel.status.setText(f'Saved render settings for shot {shot.sequence}. Approved image and prompt are unchanged.')
        except Exception as exc:
            QMessageBox.warning(self, 'Shot render settings', str(exc))

    def _save_shot_production_mode(self, mode):
        shot=self.storyboard_panel.current_shot()
        if not self.session or not shot:return
        try:
            store=VideoStore(self.session.root)
            _,coverage,fallback=store.production(shot.identifier)
            store.set_production(shot.identifier,mode,coverage,fallback)
            self.video_production_panel.status.setText(f'Shot {shot.sequence}: {mode}')
            self._refresh_animation_states()
        except Exception as exc:self._show_error('Shot production mode',str(exc))

    def _save_shot_coverage(self,strategy,draft_fallback):
        shot=self.storyboard_panel.current_shot()
        if not self.session or not shot:return
        try:
            store=VideoStore(self.session.root);mode,_,_=store.production(shot.identifier)
            selected=ImageStore(self.session.root).approved(shot.identifier)
            if strategy in {'still_tail','camera_tail'} and not selected:
                raise ValueError('Approve a still image for the tail strategy')
            coverage={'strategy':strategy}
            if strategy in {'still_tail','camera_tail'}:coverage['tail_image_id']=selected[0].identifier
            store.set_production(shot.identifier,mode,coverage,draft_fallback)
            self.video_production_panel.status.setText(f'Shot {shot.sequence}: {strategy} coverage saved')
            self._refresh_animation_states()
        except Exception as exc:self._show_error('Animation coverage',str(exc))

    def _video_plan(self, scope='full', draft=False):
        if not self.session: return None, None
        scene = self.scenes_panel.current_scene()
        shot = self.storyboard_panel.current_shot()
        settings = VideoRenderStore(self.session.root).settings()
        if draft or scope == 'full': settings = settings.model_copy(update={'mode': 'draft' if draft else 'final'})
        return build_render_plan(self.session, settings, scope=scope,
            scene_id=scene.identifier if scene else None,
            shot_id=shot.identifier if shot else None)

    def _validate_video(self):
        if not self.session: return
        try:
            plan, report = self._video_plan()
            scenes = self.session.database.load_scenes()[1]
            boards = [self.session.database.load_storyboard(scene.identifier) for scene in scenes]
            shots = [shot for board in boards if board for shot in board.shots]
            self._refresh_animation_states()
            rows = visual_timeline_manifest(self.session, scenes)
            approved = sum(row['approved_image'] is not None for row in rows)
            unresolved = sum(s.audio_start is None or s.audio_end is None for s in shots)
            render_settings = VideoRenderStore(self.session.root).settings()
            estimate = round((self.session.project.audio_duration or 0) * render_settings.frame_rate)
            self.video_production_panel.summary.setText(
                f'{self.session.project.name} · narration {self._audio_duration()} · '
                f'{len(shots)} shots · {approved} approved images · '
                f'{len(shots)-approved} missing images · {unresolved} unresolved times · '
                f'{render_settings.width} × {render_settings.height} at '
                f'{render_settings.frame_rate} fps · approximately {estimate:,} frames')
            lines = ['Ready for final render' if report.ready else 'Final render blocked']
            lines.extend(f'{issue.severity.upper()} · scene {issue.scene_id or "—"} shot {issue.shot_id or "—"}: {issue.message}'
                         for issue in report.issues)
            self.video_production_panel.status.setText('\n'.join(lines))
        except Exception as exc:
            self.video_production_panel.status.setText(f'Validation failed: {exc}')

    def _refresh_animation_states(self):
        listing=self.video_production_panel.shot_statuses
        listing.clear()
        if not self.session:return
        for scene in self.session.database.load_scenes()[1]:
            board=self.session.database.load_storyboard(scene.identifier)
            if not board:continue
            for shot in board.shots:
                listing.addItem(f'Scene {scene.sequence} · Shot {shot.sequence}: {animation_status(self.session,shot)}')

    def _render_video(self, choice):
        if not self.session or self._render_task: return
        scope = 'full' if choice in {'full','draft'} else choice
        draft = choice == 'draft'
        try:
            plan, report = self._video_plan(scope, draft)
            if not plan or not report.ready:
                self.video_production_panel.status.setText('Render blocked:\n'+'\n'.join(i.message for i in report.issues))
                return
            warnings = [i for i in report.issues if i.severity == 'warning']
            if warnings:
                detail = '\n'.join(f'{i.scene_id or "—"}/{i.shot_id or "—"}: {i.message}' for i in warnings[:12])
                if QMessageBox.question(self, 'Render with timeline warnings?',
                    detail + '\n\nContinue rendering?',
                    QMessageBox.Yes | QMessageBox.No, QMessageBox.No) != QMessageBox.Yes: return
            settings = plan.settings
            path = output_path(self.session.root, settings)
            if scope != 'full': path = path.with_stem(path.stem + f'_{scope}_{plan.segments[0].shot_id[:8]}')
            if draft: path = path.with_stem(path.stem + '_DRAFT')
            if path.exists():
                index = 2
                while path.with_stem(path.stem + f'_{index}').exists(): index += 1
                next_path = path.with_stem(path.stem + f'_{index}')
                QMessageBox.information(self, 'Output exists', f'Existing render preserved. New output: {next_path.name}')
                path = next_path
            task = RenderTask(self.session, plan, path, bool(warnings), self)
            self._render_task = task
            self.video_production_panel.set_busy(True)
            total = plan.segments[-1].end_frame / settings.frame_rate
            started = __import__('time').monotonic()
            task.progress.connect(lambda stage, seconds: self.video_production_panel.status.setText(
                f'{stage} · {seconds:.1f}s / {total:.1f}s'
                + (f' · {min(100, seconds/total*100):.0f}% of shot frames' if stage.startswith('Shot') else '')
                + f' · elapsed {__import__("time").monotonic()-started:.0f}s'))
            def done(job):
                self._render_task = None
                self.video_production_panel.set_busy(False)
                self.video_production_panel.status.setText(
                    f'{job.status}: {job.output_path}' if job.status == 'Completed' else
                    f'{job.status}: {job.error}')
                self._refresh_render_history()
                if job.status == 'Completed': self.video_production_panel.open_preview(Path(job.output_path))
                task.deleteLater();self._refresh()
            task.finished_job.connect(done)
            task.start()
            self._refresh()
        except Exception as exc:
            QMessageBox.warning(self, 'Cannot render video', str(exc))

    def _cancel_render(self):
        if self._render_task: self._render_task.cancel_event.set()

    def _refresh_render_history(self):
        panel = self.video_production_panel
        panel.history.clear()
        if not self.session: return
        for job in VideoRenderStore(self.session.root).jobs():
            item = QListWidgetItem(f'{job.started_at:%Y-%m-%d %H:%M} · {job.scope} {job.mode} · {job.status} · {Path(job.output_path).name}')
            item.setData(Qt.UserRole, job.output_path)
            panel.history.addItem(item)

    def _open_render_folder(self):
        if not self.session: return
        from PySide6.QtGui import QDesktopServices
        path = output_path(self.session.root, VideoRenderStore(self.session.root).settings()).parent
        path.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))

    def _clear_render_cache(self):
        if not self.session or self._render_task: return
        FFmpegRenderer(self.session.root).clear_cache()
        self.video_production_panel.status.setText('Render cache cleared; source images and finished videos preserved')

    def closeEvent(self, event: QCloseEvent) -> None:
        if self._render_task is not None:
            self._cancel_render()
            event.ignore()
            self.statusBar().showMessage('Canceling render; close when it finishes', 5000)
            return
        if self._image_task is not None:
            self._cancel_images()
            event.ignore()
            self.statusBar().showMessage('Finishing current image request; close when it finishes',5000)
            return
        if self._prompt_task is not None:
            self._cancel_prompts()
            event.ignore()
            return
        if self._entity_task is not None:
            self._cancel_entity_extraction()
            event.ignore()
            self.statusBar().showMessage('Canceling entity extraction; close when it finishes', 5000)
            return
        if self.visual_library_panel._image_task is not None or self.visual_library_panel._integrity_task is not None:
            event.ignore()
            self.statusBar().showMessage('Finishing reference image operation; close when it finishes', 5000)
            return
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
