"""Six-tab workspace for the direct stills workflow. No legacy workers are started."""
from pathlib import Path
from threading import Event
from time import monotonic
import json
import logging
from uuid import uuid4
from PySide6.QtCore import QThread, Signal, Slot, Qt, QUrl, QSignalBlocker, QSettings, QEvent
from PySide6.QtGui import QPixmap, QDesktopServices, QAction, QKeySequence, QPainter
from PySide6.QtWidgets import (QApplication, QMainWindow, QWidget, QTabWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QLabel, QPlainTextEdit, QListWidget, QFormLayout, QScrollArea, QSplitter,
    QFileDialog, QInputDialog, QMessageBox, QDialog, QDialogButtonBox, QComboBox, QTableWidget, QTableWidgetItem, QLineEdit, QDoubleSpinBox)
from prose_to_video.pipeline.stills_workflow import (Workflow, Identity, Interval, load_workflow,
    analyze, dependency, regenerate_prompt, generate_still, store_still, assemble_timeline,
    validate_timeline, render_plan, listed_identities, generate_character_reference, set_world_prompt,
    WORLD_PROMPT_LIMIT)
from prose_to_video.storage.project_files import (create_project, open_project, import_prose, import_audio,
    import_world_building_files, remove_world_building_file, world_building_files)
from prose_to_video.utils.prose import read_prose
from prose_to_video.models.scene import Scene, source_hash
from prose_to_video.models.camera_movement import CAMERA_MOVEMENT_IDS, CAMERA_RENDER_FIELDS
from prose_to_video.gui.prose_viewer import ProseViewer
from prose_to_video.gui.audio_player import AudioPlayer
from prose_to_video.gui.video_preview import VideoPreview
from prose_to_video.pipeline.still_camera import still_camera
from prose_to_video.gui.characters_panel import ReferencePreview
from prose_to_video.gui.ai_settings import load_ai_settings
from prose_to_video.gui.stills_settings import StillsSettingsWindow
from prose_to_video.pipeline.providers import make_provider


LOGGER = logging.getLogger(__name__)


class WorldBuildingList(QListWidget):
    def paintEvent(self, event):
        super().paintEvent(event)
        if self.count() == 0:
            painter = QPainter(self.viewport())
            font = painter.font()
            font.setItalic(True)
            painter.setFont(font)
            painter.setPen(self.palette().color(self.foregroundRole()))
            painter.drawText(self.viewport().rect(), Qt.AlignCenter,
                             'Recomended: custom world build file.')


class CameraMovementComboBox(QComboBox):
    def wheelEvent(self, event):
        event.ignore()


class Task(QThread):
    progress = Signal(str)
    result = Signal(object)
    error = Signal(str)

    def __init__(self, work, parent):
        super().__init__(parent); self.work = work; self.cancel = Event()

    def run(self):
        try: self.result.emit(self.work(self.cancel, self.progress.emit))
        except Exception as exc: self.error.emit(str(exc))


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__(); self.setWindowTitle('Prose to Video'); self.resize(1350, 950)
        self.session = None; self.state = Workflow(); self.worker = None
        self._unsaved_edits = False
        self._scene_drafts = {}
        self._timeline_dirty = False
        self._updating_timeline = False
        self._undo_history = []
        self._undo_group = None
        self._undo_time = 0
        self._worker_snapshot = None
        self.workspace_tabs = QTabWidget(); self.setCentralWidget(self.workspace_tabs)
        self.start = self.page('Start'); self.place = self.page('Place'); self.character = self.page('Character')
        self.scenes_page = self.page('Scenes'); self.stills_page = self.page('Stills'); self.video = self.page('Video Production')
        self.footer_status = QLabel('Create or open a project to begin')
        self.status = self.footer_status
        self.footer_status.setWordWrap(True)
        self.footer_status.setTextFormat(Qt.PlainText)
        self.footer_status.setTextInteractionFlags(Qt.TextSelectableByMouse | Qt.TextSelectableByKeyboard)
        self.statusBar().addWidget(self.footer_status, 1)
        start_split = QSplitter(Qt.Horizontal)
        start_split.setChildrenCollapsible(False)
        self.start.layout().addWidget(start_split)
        start_controls = QWidget()
        QVBoxLayout(start_controls)
        self.buttons(start_controls, [('Import Prose', lambda:self.import_source('prose')),
            ('Link / Replace Narration Audio', lambda:self.import_source('audio')),
            ('Analyze Story', self.analyze),
            ('Stop Analyzing', self.cancel)], vertical=True)
        self.buttons(start_controls, [('Add World Building Files', self.add_world_building_files)], vertical=True)
        self.world_building_list = WorldBuildingList()
        self.world_building_list.setMaximumHeight(110)
        start_controls.layout().addWidget(self.world_building_list)
        self.delete_world_building_button = QPushButton('Delete Selected File')
        self.delete_world_building_button.setEnabled(False)
        self.delete_world_building_button.clicked.connect(self.delete_world_building_file)
        self.world_building_list.currentItemChanged.connect(self.update_world_building_delete_button)
        start_controls.layout().addWidget(self.delete_world_building_button)
        start_controls.layout().addStretch()
        start_split.addWidget(start_controls)
        self.source_view = ProseViewer(); start_split.addWidget(self.source_view)
        start_split.setStretchFactor(0, 0)
        start_split.setStretchFactor(1, 1)
        self.catalog_lists = {}; self.catalog_edits = {}
        self.character_reference_index = 0
        self._character_reference_id = None
        for kind,page in [('Place',self.place),('Character',self.character)]:
            listing = QListWidget(); edit = QPlainTextEdit()
            self.catalog_lists[kind]=listing; self.catalog_edits[kind]=edit
            edit.textChanged.connect(lambda k=kind:self.identity_edited(k))
            listing.currentRowChanged.connect(lambda _,k=kind:self.show_identity(k))
            if kind == 'Character':
                page.layout().addWidget(QLabel('Characters participating in two or more included scenes.'))
                self.remove_character_button = QPushButton('Remove Character')
                self.remove_character_button.setToolTip('Remove the selected character and its scene links.')
                self.remove_character_button.clicked.connect(self.remove_character)
                page.layout().addWidget(self.remove_character_button)
                split = QSplitter(); split.addWidget(listing)
                details = QWidget(); QVBoxLayout(details); split.addWidget(details)
                split.setStretchFactor(1, 3); page.layout().addWidget(split)
                self.character_reference_preview = ReferencePreview()
                self.character_reference_preview.setMinimumSize(240, 300)
                details.layout().addWidget(self.character_reference_preview, 1)
                self.character_reference_navigation = QWidget()
                navigation = QHBoxLayout(self.character_reference_navigation)
                self.previous_character_reference = QPushButton('Previous Image')
                self.next_character_reference = QPushButton('Next Image')
                self.character_reference_position = QLabel()
                self.character_reference_position.setAlignment(Qt.AlignCenter)
                navigation.addWidget(self.previous_character_reference)
                navigation.addWidget(self.character_reference_position, 1)
                navigation.addWidget(self.next_character_reference)
                details.layout().addWidget(self.character_reference_navigation)
                self.previous_character_reference.clicked.connect(lambda:self.change_character_reference(-1))
                self.next_character_reference.clicked.connect(lambda:self.change_character_reference(1))
                edit.setMaximumHeight(140)
            else:
                details = page; details.layout().addWidget(listing)
                self.world_prompt_count = QLabel()
                details.layout().addWidget(self.world_prompt_count)
            details.layout().addWidget(QLabel('Canonical description · Edits save automatically')); details.layout().addWidget(edit)
            self.buttons(details, [('Add Identity',lambda _,k=kind:self.add_identity(k)),
                ('Save Description' if kind == 'Place' else 'Save Description / References',lambda _,k=kind:self.save_identity(k)),
                ('Import Reference',lambda _,k=kind:self.import_reference(k))])
            if kind == 'Character':
                self.generate_character_reference_button = QPushButton('Generate Reference')
                self.generate_character_reference_button.setToolTip(
                    'Generate a full-body reference using the current description, project style and image settings.')
                self.generate_character_reference_button.clicked.connect(self.generate_reference)
                details.layout().addWidget(self.generate_character_reference_button)
                self.remove_character_reference = QPushButton('Remove Reference')
                self.remove_character_reference.clicked.connect(self.remove_character_reference_image)
                details.layout().addWidget(self.remove_character_reference)
        self.scene_list = QListWidget(); self.scene_list.currentRowChanged.connect(self.show_scene)
        self.scenes_page.layout().addWidget(self.scene_list)
        split = QSplitter(); self.scenes_page.layout().addWidget(split)
        self.prose = ProseViewer(); split.addWidget(self.prose)
        self.prose.loaded.connect(self.show_scene)
        detail = QWidget(); form = QFormLayout(detail); self.fields = {}
        excluded = {'identifier','sequence','start','end','text','source_version','production_included','prompt_dependency','manual_fields'}
        self.list_fields = {'characters','narrative_formats','important_objects','actions','dialogue_participants','identity_links'}
        for name in Scene.model_fields:
            if name in excluded: continue
            edit = QPlainTextEdit(); edit.setMaximumHeight(100 if name=='final_prompt' else 65)
            if name in self.list_fields: edit.setToolTip('One item per line; identity links use catalog IDs')
            self.fields[name] = edit; form.addRow(name.replace('_',' ').capitalize(), edit)
            edit.textChanged.connect(lambda n=name:self.scene_edited(n))
        scroll = QScrollArea(); scroll.setWidgetResizable(True); scroll.setWidget(detail); split.addWidget(scroll)
        self.prompt_status = QLabel(); self.scenes_page.layout().addWidget(self.prompt_status)
        self.buttons(self.scenes_page,[('Save Scene',self.save_scene), ('Regenerate Prompt',self.regenerate),
            ('Split at Prose Selection',self.split_scene), ('Merge With Next',self.merge_scene),
            ('Generate Selected',lambda:self.generate('selected')), ('Generate All',lambda:self.generate('all'))])
        self.still_list = QListWidget(); self.still_list.currentRowChanged.connect(self.show_still)
        self.stills_page.layout().addWidget(self.still_list)
        self.preview = QLabel(); self.preview.setMinimumHeight(220); self.preview.setAlignment(Qt.AlignCenter)
        self.stills_page.layout().addWidget(self.preview)
        self.still_details = QPlainTextEdit(); self.still_details.setReadOnly(True); self.stills_page.layout().addWidget(self.still_details)
        self.candidates = QListWidget(); self.stills_page.layout().addWidget(self.candidates)
        self.buttons(self.stills_page,[('Generate Selected',lambda:self.generate('still')), ('Generate Missing',lambda:self.generate('missing')),
            ('Regenerate Selected',lambda:self.generate('still')), ('Import Image',self.import_still), ('Select Candidate',self.select_candidate)])
        self.timeline_mode = QComboBox()
        self.timeline_mode.addItem('Nearest fit — build despite narration discrepancies', 'nearest')
        self.timeline_mode.addItem('Strict — require verified narration throughout', 'strict')
        self.video.layout().addWidget(QLabel('Timeline timing'))
        self.video.layout().addWidget(self.timeline_mode)
        self.video_actions = self.buttons(self.video,[('Build Timeline from Stills + Audio',self.build), ('Align Narration…',self.align),
            ('Review Narration…',self.correct_alignment), ('Preview Export',self.preview_export), ('Export Video',self.render), ('Cancel Work',self.cancel)])
        self.video.layout().addWidget(QLabel('Press Play to preview the video. Drag timeline markers to adjust cuts; manual intervals survive rebuilds.'))
        self.video_actions[0].setToolTip('Use every included scene’s selected still and the full narration. Automatically aligns audio when needed; first use may download local speech models.')
        self.video_preview = VideoPreview()
        self.video.layout().addWidget(self.video_preview)
        self.timeline = QTableWidget(0, 5, self)
        self.timeline.itemChanged.connect(self.timeline_edited)
        self.timeline.setHorizontalHeaderLabels(['Scene', 'Display start', 'Display end', 'Speech / estimate start', 'Speech / estimate end'])
        self.timeline.horizontalHeader().setStretchLastSection(True)
        self.video.layout().addWidget(self.timeline)
        self.audio = AudioPlayer(); self.video.layout().addWidget(self.audio)
        self.audio.position_seconds.connect(self.update_video_preview)
        self.audio.waveform.boundary_moved.connect(self.move_still_boundary)
        self.video_scene_cards = QWidget(); QVBoxLayout(self.video_scene_cards)
        scene_scroll = QScrollArea(); scene_scroll.setWidgetResizable(True)
        scene_scroll.setWidget(self.video_scene_cards); scene_scroll.setMinimumHeight(160); scene_scroll.setMaximumHeight(280)
        self.video.layout().addWidget(scene_scroll)
        self.video_message = QLabel(); self.video_message.setWordWrap(True); self.video.layout().addWidget(self.video_message)
        file_menu = self.menuBar().addMenu('File')
        new_project_action = file_menu.addAction('New Project', self.new_project)
        new_project_action.setShortcuts(QKeySequence.keyBindings(QKeySequence.New))
        open_project_action = file_menu.addAction('Open Project', self.open_project)
        open_project_action.setShortcuts(QKeySequence.keyBindings(QKeySequence.Open))
        file_menu.addSeparator()
        self.undo_action = QAction('Undo', self)
        self.undo_action.setShortcuts(QKeySequence.keyBindings(QKeySequence.Undo))
        self.undo_action.setEnabled(False)
        self.undo_action.triggered.connect(self.undo)
        file_menu.addAction(self.undo_action)
        # Text editors normally consume Undo before window shortcuts see it.
        QApplication.instance().installEventFilter(self)
        self.menuBar().addAction('Settings', lambda:self.settings())
        self.refresh()
        self._project_settings = QSettings('ProseToVideo', 'ProseToVideo')
        self._restore_last_project()

    def _restore_last_project(self):
        recent = self._project_settings.value('projects/recent', [], type=list)
        for path in recent:
            try:
                self.load(open_project(Path(path)))
            except Exception as exc:
                LOGGER.warning('Could not restore project %s: %s', path, exc)
                self.session = None
                self.state = Workflow()
            else:
                return
        self.refresh()
        if recent:
            self.message('Could not reopen the last project. Create or open a project to begin.')

    def page(self,name):
        page=QWidget(); QVBoxLayout(page); self.workspace_tabs.addTab(page,name); return page

    def buttons(self,page,items,vertical=False):
        row=QVBoxLayout() if vertical else QHBoxLayout(); page.layout().addLayout(row)
        buttons = []
        for label,callback in items:
            button=QPushButton(label); button.clicked.connect(callback); row.addWidget(button)
            buttons.append(button)
        return buttons

    def edit_snapshot(self):
        return {
            'root': self.session.root if self.session else None,
            'state': self.state.model_copy(deep=True),
            'drafts': dict(self._scene_drafts),
            'scene_row': self.scene_list.currentRow(),
            'still_row': self.still_list.currentRow(),
            'catalog_rows': {kind: listing.currentRow() for kind, listing in self.catalog_lists.items()},
            'reference_index': self.character_reference_index,
        }

    def record_edit(self, before, group=None):
        if not self.session or before['root'] != self.session.root:
            return
        if before['state'] == self.state and before['drafts'] == self._scene_drafts:
            return
        if self._undo_history:
            latest = self._undo_history[-1]
            if latest['state'] == before['state'] and latest['drafts'] == before['drafts']:
                return
        now = monotonic()
        if group is None or group != self._undo_group or now - self._undo_time > 1:
            self._undo_history.append(before)
            del self._undo_history[:-100]
        self._undo_group, self._undo_time = group, now
        self.update_undo_action()

    def update_undo_action(self):
        self.undo_action.setEnabled(bool(self.session and not self.worker and
                                        (self._undo_history or self._timeline_dirty)))

    def eventFilter(self, watched, event):
        if isinstance(watched, QWidget) and watched.window() is self:
            # An open table cell has its own uncommitted text buffer.
            if isinstance(watched, QLineEdit) and self.timeline.isAncestorOf(watched):
                return super().eventFilter(watched, event)
            if event.type() == QEvent.FocusOut:
                self._undo_group = None
            if event.type() in (QEvent.ShortcutOverride, QEvent.KeyPress) and event.matches(QKeySequence.Undo):
                event.accept()
                if event.type() == QEvent.KeyPress:
                    self.undo()
                return True
        return super().eventFilter(watched, event)

    def undo(self):
        if not self.session or self.worker:
            return
        # Uncommitted boundary cells can be invalid as a pair; discard that draft
        # without trying to validate or save it first.
        if self._timeline_dirty:
            self._timeline_dirty = False
            self.refresh_timeline_table()
            self._undo_group = None
            self.update_undo_action()
            self.message('Undid timeline edits.')
            return
        if not self._undo_history:
            return
        before = self._undo_history[-1]
        restored = before['state'].model_copy(deep=True)
        try:
            restored.save(self.session.root)
        except OSError as exc:
            QMessageBox.warning(self, 'Could not undo change', str(exc))
            return
        self._undo_history.pop()
        self.state = restored
        self._scene_drafts = dict(before['drafts'])
        self._unsaved_edits = False
        self._undo_group = None
        self.refresh()
        self.still_list.setCurrentRow(before['still_row'])
        self.scene_list.setCurrentRow(before['scene_row'])
        self.show_scene()
        for kind, row in before['catalog_rows'].items():
            self.catalog_lists[kind].setCurrentRow(row)
            self.show_identity(kind)
        self.character_reference_index = before['reference_index']
        self.show_character_reference()
        self.update_undo_action()
        self.message('Change undone · Saved')

    def guarded(self,fn):
        before = self.edit_snapshot() if not self.worker else None
        self._undo_group = None
        try: return fn()
        except Exception as exc: QMessageBox.warning(self,'Prose to Video',str(exc))
        finally:
            if before is not None and not self.worker:
                self.record_edit(before)

    def require(self):
        if not self.session: raise ValueError('Create or open a project first')
        if self.worker: raise ValueError('Wait for the current operation or cancel it')
        self.flush_edits()

    def run(self,work,on_result=None,description='Background work'):
        self.require()
        self._worker_snapshot = self.edit_snapshot()
        self.worker=Task(work,self)
        self.update_undo_action()
        self.update_world_building_delete_button()
        self.worker.description = description
        self.message(description + ' · Starting')
        self.worker.progress.connect(self.message)
        self.worker.error.connect(lambda msg:self.message('Stopped: '+msg))
        self.worker.result.connect(self.completed)
        if on_result is not None:
            self.worker.result.connect(on_result)
        self.worker.finished.connect(self.finished); self.worker.start()
        # Disable editing while a worker owns the state; cancellation stays available in Start/Video.
        for page in (self.place,self.character,self.scenes_page,self.stills_page): page.setEnabled(False)
        for button in self.video_actions[:-1]: button.setEnabled(False)
        self.timeline.setEnabled(False)
        self.video_scene_cards.setEnabled(False)
        self.audio.waveform.set_boundaries_editable(False)
        self.timeline_mode.setEnabled(False)

    def message(self,msg):
        self.video_message.setText(msg); self.footer_status.setText(msg)
        self.footer_status.setToolTip(msg)

    @Slot(object)
    def completed(self,result):
        self.message(self.worker.description + ' · Completed')
        if isinstance(result, Workflow):
            self.message(f'Analysis complete · {len(listed_identities(result))} recurring characters and places; '
                f'{len(result.scenes)} scenes with staging and image prompts. Review Scenes or generate stills.')
        if self.worker and not self.worker.cancel.is_set():
            QApplication.beep()

    def finished(self):
        self.worker.deleteLater(); self.worker=None
        self.update_world_building_delete_button()
        for page in (self.place,self.character,self.scenes_page,self.stills_page): page.setEnabled(True)
        for button in self.video_actions[:-1]: button.setEnabled(True)
        self.timeline.setEnabled(True)
        self.video_scene_cards.setEnabled(True)
        self.audio.waveform.set_boundaries_editable(True)
        self.timeline_mode.setEnabled(True)
        if self._worker_snapshot is not None:
            self.record_edit(self._worker_snapshot)
            self._worker_snapshot = None
        self.update_undo_action()
        self.refresh()

    def cancel(self):
        if self.worker: self.worker.cancel.set(); self.message('Canceling after current request; checkpoints preserved')

    def new_project(self):
        def work():
            if self.worker: return
            parent=QFileDialog.getExistingDirectory(self,'Project parent folder')
            if not parent:return
            name,ok=QInputDialog.getText(self,'New Project','Project name')
            if ok:self.load(create_project(Path(parent),name))
        self.guarded(work)

    def open_project(self):
        def work():
            if self.worker:return
            path=QFileDialog.getExistingDirectory(self,'Open project folder')
            if path:self.load(open_project(Path(path)))
        self.guarded(work)

    def load(self,session):
        self.flush_edits()
        state=load_workflow(session)
        self._undo_history.clear()
        self._undo_group = None
        self._scene_drafts.clear()
        self.session=session; self.state=state; self.refresh()
        self.update_undo_action()
        self.message(session.project.name + (' · '+ '; '.join(self.state.migration_notes) if self.state.migration_notes else ''))
        path = str(session.root.resolve())
        recent = self._project_settings.value('projects/recent', [], type=list)
        self._project_settings.setValue('projects/recent', [path, *(item for item in recent if item != path)][:10])
        self._project_settings.sync()

    def refresh(self):
        if self.session and self._timeline_dirty:
            self.commit_timeline_edits()
        self.world_building_list.clear()
        if self.session:
            for path in world_building_files(self.session):
                self.world_building_list.addItem(path.name)
        self.update_world_building_delete_button()
        scene_id=self.scene().identifier if self.scene() else None
        path=self.session.project.resolved_prose(self.session.root) if self.session else None
        if path:
            text=read_prose(path); self.prose.show_text(text); self.source_view.show_text(text)
        else:
            self.prose.clear_story(); self.source_view.clear_story()
        for kind,listing in self.catalog_lists.items():
            previous = self.identity(kind)
            values = listed_identities(self.state, kind)
            with QSignalBlocker(listing):
                listing.clear()
                if kind == 'Place' and self.state.world_prompt:
                    listing.addItem('World Building Prompt')
                for c in values:listing.addItem(c.name if kind == 'Character' else c.name+' · '+c.identifier)
                offset = 1 if kind == 'Place' and self.state.world_prompt else 0
                row = next((i + offset for i,c in enumerate(values) if previous and c.identifier == previous.identifier), 0)
                listing.setCurrentRow(row if listing.count() else -1)
            self.show_identity(kind)
        self.scene_list.clear(); self.still_list.clear()
        for s in self.state.scenes:
            self.scene_list.addItem(f'{s.sequence}. {s.title}'+(' [excluded]' if not s.production_included else ''))
            self.still_list.addItem(f'{s.sequence}. {s.title} · '+('image selected' if self.state.current_still(s.identifier) else 'missing'))
        if scene_id:
            self.scene_list.setCurrentRow(next((i for i,s in enumerate(self.state.scenes) if s.identifier==scene_id),0))
        self.timeline_mode.setCurrentIndex(self.timeline_mode.findData(self.state.timeline_mode))
        self.refresh_timeline_table()
        self.refresh_video_scenes()
        if self.session:
            path=self.session.project.resolved_prose(self.session.root)
            if path:
                text=read_prose(path)
                if self.state.prose_hash and source_hash(text)!=self.state.prose_hash:self.message('Prose changed; click Analyze Story to rebuild scenes, places, characters and prompts while keeping reference images')
            audio=self.session.project.resolved_audio(self.session.root)
            if audio != self.audio._loaded_path:
                self.audio.set_audio(audio, self.session.project.audio_duration)
        self.sync_timeline_markers()
        self.update_video_preview()

    def refresh_video_scenes(self):
        layout = self.video_scene_cards.layout()
        while layout.count():
            item = layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self.camera_movement_boxes = {}
        for scene in self.state.scenes:
            if not scene.production_included:
                continue
            card = QWidget(); row = QHBoxLayout(card)
            preview = QLabel('No selected still'); preview.setFixedSize(180, 104)
            preview.setAlignment(Qt.AlignCenter)
            asset = self.state.current_still(scene.identifier)
            if asset and self.session:
                pixmap = QPixmap(str(self.session.root / asset.path))
                preview.setPixmap(pixmap.scaled(180, 104, Qt.KeepAspectRatio, Qt.SmoothTransformation))
            row.addWidget(preview)
            controls = QWidget(); form = QFormLayout(controls)
            form.addRow(QLabel(f'{scene.sequence}. {scene.title}'))
            movement = CameraMovementComboBox(); movement.addItems(CAMERA_MOVEMENT_IDS)
            movement.setCurrentText(scene.camera_movement)
            movement.setToolTip('Directions describe camera travel. Moving shots fill the frame and crop safely.')
            movement.setEnabled(self.session is not None and not self.worker)
            form.addRow('Camera Movement', movement)
            focal = QWidget(); focal_row = QHBoxLayout(focal); focal_row.setContentsMargins(0, 0, 0, 0)
            for axis in ('x', 'y'):
                spin = QDoubleSpinBox(); spin.setRange(0, 1); spin.setDecimals(3); spin.setSingleStep(.05)
                spin.setValue(getattr(scene, 'camera_focal_' + axis))
                spin.setToolTip('Normalized image coordinate: 0 = left/top, 1 = right/bottom')
                spin.setEnabled(self.session is not None and not self.worker)
                spin.valueChanged.connect(lambda value, sid=scene.identifier, a=axis:
                    self.update_scene_camera(sid, 'camera_focal_' + a, value))
                focal_row.addWidget(QLabel(axis.upper())); focal_row.addWidget(spin)
            focal.setVisible(scene.camera_movement in {'zoom_in_focal_point', 'zoom_out_from_focal_point'})
            form.addRow(focal)
            movement.currentTextChanged.connect(lambda value, sid=scene.identifier, f=focal:
                (f.setVisible(value in {'zoom_in_focal_point', 'zoom_out_from_focal_point'}),
                 self.update_scene_camera(sid, 'camera_movement', value)))
            self.camera_movement_boxes[scene.identifier] = movement
            row.addWidget(controls, 1); layout.addWidget(card)
        layout.addStretch()

    def update_scene_camera(self, identifier, name, value):
        if not self.session or self.worker:
            return
        scene = next(s for s in self.state.scenes if s.identifier == identifier)
        if getattr(scene, name) == value:
            return
        before = self.edit_snapshot()
        updated = Scene.model_validate(scene.model_dump() | {name: value})
        updated.manual_fields = sorted(set(scene.manual_fields + [name]))
        self.state.scenes[self.state.scenes.index(scene)] = updated
        self.persist_edits()
        self.record_edit(before, ('scene', identifier, name))
        self.update_video_preview()
        if self.scene() and self.scene().identifier == identifier:
            self.show_scene()

    def refresh_timeline_table(self):
        self._updating_timeline = True
        self.timeline.setRowCount(len(self.state.timeline))
        for row, interval in enumerate(self.state.timeline):
            scene=next((s for s in self.state.scenes if s.identifier==interval.scene_id), None)
            values=[(scene.title if scene else interval.scene_id)+(' · manual' if interval.manual else '')+(' · approximate' if interval.estimated else ''), interval.start, interval.end, interval.speech_start, interval.speech_end]
            for col, value in enumerate(values):
                item=QTableWidgetItem(str(value))
                if col not in {1,2}:item.setFlags(item.flags() & ~Qt.ItemIsEditable)
                self.timeline.setItem(row,col,item)
        self._updating_timeline = False

    def sync_timeline_markers(self):
        intervals = self.state.timeline
        self.audio.waveform.set_boundaries([intervals[0].start, *(t.end for t in intervals)] if intervals else [])
        if intervals and self.audio.waveform.duration <= 0:
            self.audio.waveform.set_data([], intervals[-1].end)

    def update_video_preview(self, seconds=None):
        if not self.session or not self.state.timeline:
            self.video_preview.show_frame(None, None, 0, None)
            return
        if seconds is None:
            seconds = self.audio.player.position() / 1000
        index = next((i for i, t in enumerate(self.state.timeline) if t.start <= seconds < t.end), None)
        if index is None and seconds >= self.state.timeline[-1].end:
            index = len(self.state.timeline) - 1
        interval = self.state.timeline[index] if index is not None else None
        if interval is None:
            self.video_preview.show_frame(None, None, 0, None)
            return
        scene = next((s for s in self.state.scenes if s.identifier == interval.scene_id), None)
        asset = self.state.current_still(interval.scene_id)
        if scene is None or asset is None:
            self.video_preview.show_frame(None, None, 0, self.state.render_settings)
            return
        settings = self.state.render_settings
        camera = still_camera(scene.camera_movement, settings, scene.camera_focal_x,
                              scene.camera_focal_y, scene.identifier)
        progress = (seconds - interval.start) / (interval.end - interval.start)
        previous_path = previous_camera = None
        transition = settings.default_transition
        transition_frames = round(settings.crossfade_duration * settings.frame_rate)
        transition_duration = transition_frames / settings.frame_rate
        transition_progress = 1.0
        if index and transition != 'Cut' and transition_duration and seconds < interval.start + transition_duration:
            previous = self.state.timeline[index - 1]
            previous_scene = next((s for s in self.state.scenes if s.identifier == previous.scene_id), None)
            previous_asset = self.state.current_still(previous.scene_id)
            if previous_scene and previous_asset:
                previous_path = self.session.root / previous_asset.path
                previous_camera = still_camera(previous_scene.camera_movement, settings,
                                               previous_scene.camera_focal_x,
                                               previous_scene.camera_focal_y, previous_scene.identifier)
                transition_progress = (seconds - interval.start) / transition_duration
        self.video_preview.show_frame(self.session.root / asset.path, camera, progress, settings,
                                      previous_path=previous_path, previous_camera=previous_camera,
                                      transition=transition, transition_progress=transition_progress)

    def move_still_boundary(self, index, seconds):
        def work():
            self.require()
            values = [t.model_copy(deep=True) for t in self.state.timeline]
            if not 0 < index < len(values):
                return
            left, right = values[index - 1:index + 1]
            if left.end == right.start == seconds:
                return
            left.end = right.start = seconds
            left.manual = right.manual = True
            validate_timeline(values, self.session.project.audio_duration)
            updated = self.state.model_copy(update={'timeline': values})
            updated.save(self.session.root)
            self.state = updated
            self.refresh_timeline_table()
            self.update_video_preview()
            self.message(f'Still {index} → {index + 1} moved to {seconds:.3f}s · Saved')
        try:
            self.guarded(work)
        finally:
            self.sync_timeline_markers()

    def import_source(self,kind):
        def work():
            self.require(); path,_=QFileDialog.getOpenFileName(self,'Import '+kind)
            if not path:return
            session=self.session
            def job(cancel,progress):
                progress(f'Importing {kind}: {Path(path).name} · Copying the selected file into the project and checking its contents')
                return (import_prose if kind=='prose' else import_audio)(session,Path(path))
            self.run(job, lambda session:self.source_imported(session, kind),
                description=f'Import {kind}: {Path(path).name}')
        self.guarded(work)

    def add_world_building_files(self):
        def work():
            self.require()
            if len(world_building_files(self.session)) >= 3:
                raise ValueError('A project can have up to three world building files')
            paths, _ = QFileDialog.getOpenFileNames(self, 'Add World Building Files')
            if not paths:
                return
            files = import_world_building_files(self.session, [Path(path) for path in paths])
            self.world_building_list.clear()
            for path in files:
                self.world_building_list.addItem(path.name)
            self.message(f'{len(files)} world building file(s) in project')
        self.guarded(work)

    def update_world_building_delete_button(self):
        self.delete_world_building_button.setEnabled(bool(
            self.session and self.world_building_list.currentItem() and not self.worker))

    def delete_world_building_file(self):
        def work():
            self.require()
            item = self.world_building_list.currentItem()
            if item is None:
                return
            name = item.text()
            if QMessageBox.question(self, f'Delete {name}', 'Are you sure?',
                                    QMessageBox.Yes | QMessageBox.No, QMessageBox.No) != QMessageBox.Yes:
                return
            files = remove_world_building_file(self.session, name)
            self.world_building_list.clear()
            for path in files:
                self.world_building_list.addItem(path.name)
            self.message(f'{name} deleted from world building files')
        self.guarded(work)

    @Slot(object)
    def source_imported(self,session,kind='prose'):
        self.session=session
        if kind == 'audio':
            # Qt otherwise keeps the old source open when the path is unchanged.
            self.audio.set_audio(None)
        # Source imports change project files outside the workflow sidecar.
        self._undo_history.clear()
        self._worker_snapshot = None
        self.update_undo_action()
        self.refresh()
        if kind == 'audio':
            self.message('Narration audio updated. In Video Production, build the timeline to align the new audio. '
                         'Review any manual cuts afterward.')

    def settings(self, initial_tab=0):
        def work():
            if self.worker:
                self.message('Wait for the current operation or cancel it before changing settings')
                return
            self.flush_edits()
            StillsSettingsWindow(self, initial_tab).exec()
        self.guarded(work)

    def ai_settings(self):
        self.settings(0)

    def image_settings(self):
        self.settings(1)

    def export_settings(self):
        self.settings(2)

    def analyze(self):
        def work():
            self.require(); settings=load_ai_settings()
            self.run(lambda cancel,progress:analyze(self.session,self.state,make_provider(settings),settings,cancel,progress),
                description='Analyze Story · Reading prose, identifying characters and places, and building scenes and image prompts')
        self.guarded(work)

    def identity(self,kind):
        values=listed_identities(self.state, kind); row=self.catalog_lists[kind].currentRow()
        if kind == 'Place' and self.catalog_lists['Place'].count() and self.catalog_lists['Place'].item(0).text() == 'World Building Prompt':
            row -= 1
        return values[row] if 0<=row<len(values) else None

    def world_prompt_selected(self):
        listing = self.catalog_lists['Place']
        return bool(listing.count() and listing.currentRow() == 0 and listing.item(0).text() == 'World Building Prompt')

    def show_identity(self,kind):
        c=self.identity(kind)
        with QSignalBlocker(self.catalog_edits[kind]):
            self.catalog_edits[kind].setPlainText(c.description if c else
                self.state.world_prompt if kind == 'Place' and self.world_prompt_selected() else '')
        if kind == 'Place':
            selected = self.world_prompt_selected()
            self.world_prompt_count.setText(f'World building prompt: {len(self.state.world_prompt)}/{WORLD_PROMPT_LIMIT} characters' if selected else '')
        if kind == 'Character':
            self.remove_character_button.setEnabled(
                c is not None and self.session is not None and self.worker is None)
            identifier = c.identifier if c else None
            if identifier != self._character_reference_id:
                self.character_reference_index = 0
            self._character_reference_id = identifier
            self.show_character_reference()

    def show_character_reference(self):
        character = self.identity('Character')
        paths = character.references if character else []
        count = len(paths)
        self.character_reference_index = min(max(0, self.character_reference_index), max(0, count - 1))
        index = self.character_reference_index
        self.character_reference_navigation.setVisible(count > 1)
        self.previous_character_reference.setEnabled(index > 0)
        self.next_character_reference.setEnabled(index < count - 1)
        self.character_reference_position.setText(f'{index + 1} / {count}' if count else '')
        self.remove_character_reference.setEnabled(bool(count))
        self.generate_character_reference_button.setEnabled(
            character is not None and self.session is not None and self.worker is None)
        if not character:
            self.character_reference_preview.setText('Select a character to see its reference image.')
        elif not paths or not self.session:
            self.character_reference_preview.setText('No reference image for this character. Use Generate Reference or Import Reference to add one.')
        else:
            pixmap = QPixmap(str(self.session.root / paths[index]))
            if pixmap.isNull():
                self.character_reference_preview.setText('Reference image is missing or unreadable.')
            else:
                self.character_reference_preview.setPixmap(pixmap)

    def change_character_reference(self, step):
        self.character_reference_index += step
        self.show_character_reference()

    def remove_character(self):
        def work():
            self.require(); character = self.identity('Character')
            if not character:
                return
            row = self.catalog_lists['Character'].currentRow()
            updated = self.state.model_copy(deep=True)
            updated.catalogs = [c for c in updated.catalogs if c.identifier != character.identifier]
            updated.reference_identities = [c for c in updated.reference_identities
                                            if c.identifier != character.identifier]
            for scene in updated.scenes:
                scene.identity_links = [identifier for identifier in scene.identity_links
                                        if identifier != character.identifier]
            updated.save(self.session.root)
            self.state = updated
            self.refresh()
            listing = self.catalog_lists['Character']
            listing.setCurrentRow(min(row, listing.count() - 1))
            self.message(f'{character.name} removed.')
        self.guarded(work)

    def remove_character_reference_image(self):
        def work():
            self.require(); character = self.identity('Character')
            if character and character.references:
                character.references.pop(self.character_reference_index)
                character.manual = True
                self.state.save(self.session.root)
                self.show_character_reference()
        self.guarded(work)

    def add_identity(self,kind):
        def work():
            self.require(); name,ok=QInputDialog.getText(self,'Add '+kind,'Name')
            if ok and name.strip():self.state.catalogs.append(Identity(kind=kind,name=name,description='',manual=True)); self.state.save(self.session.root); self.refresh()
        self.guarded(work)

    def save_identity(self,kind):
        def work():
            self.require(); c=self.identity(kind)
            if kind == 'Place' and self.world_prompt_selected():
                set_world_prompt(self.state, self.catalog_edits[kind].toPlainText())
                self.state.save(self.session.root)
                return
            if not c:return
            c.description=self.catalog_edits[kind].toPlainText(); c.manual=True
            self.state.save(self.session.root)
        self.guarded(work)

    def import_reference(self,kind):
        def work():
            self.require(); c=self.identity(kind)
            if not c:return
            path,_=QFileDialog.getOpenFileName(self,'Import reference')
            if not path:return
            # Reuse the existing image integrity/import service; no status filtering.
            from prose_to_video.storage.visual_library import VisualLibrary
            def done(asset):
                c.references.append(asset.path); self.state.save(self.session.root)
            self.run(lambda cancel,progress:VisualLibrary(self.session.root).import_image(Path(path)), done,
                description=f'Import reference: {Path(path).name} · Checking and saving the image for {c.name}')
        self.guarded(work)

    def generate_reference(self):
        def work():
            self.require()
            character = self.identity('Character')
            if not character:
                return
            character.description = self.catalog_edits['Character'].toPlainText()
            character.manual = True
            self.state.save(self.session.root)
            def job(cancel, progress):
                from prose_to_video.pipeline.image_generation import make_image_provider
                provider = make_image_provider(self.state.image_settings, self.session.root)
                return generate_character_reference(
                    self.session, self.state, character, provider, cancel, progress)
            def done(path):
                self.character_reference_index = character.references.index(path)
                self.show_character_reference()
            self.run(job, done, description=f'Generate reference · {character.name}')
        self.guarded(work)

    def scene(self):
        row=self.scene_list.currentRow(); return self.state.scenes[row] if 0<=row<len(self.state.scenes) else None

    def persist_edits(self):
        self._unsaved_edits = True
        try:
            self.state.save(self.session.root)
        except OSError as exc:
            self.message(f'Changes are not saved: {exc}. Keep the app open and retry saving.')
        else:
            self._unsaved_edits = False

    def flush_edits(self):
        if self.session and self._timeline_dirty:
            self.commit_timeline_edits()
        if self._scene_drafts:
            identifier, name = next(iter(self._scene_drafts))
            scene = next(s for s in self.state.scenes if s.identifier == identifier)
            raise ValueError(f'Scene {scene.sequence}: {scene.title} has an invalid {name.replace("_", " ")} value. '
                             'Correct it in Scenes before closing or starting another operation.')
        if self.session and self._unsaved_edits:
            self.state.save(self.session.root)
            self._unsaved_edits = False

    def identity_edited(self, kind):
        if not self.session or self.worker:
            return
        identity = self.identity(kind)
        if kind == 'Place' and self.world_prompt_selected():
            edit = self.catalog_edits[kind]
            value = edit.toPlainText()
            if len(value) > WORLD_PROMPT_LIMIT:
                cursor = edit.textCursor()
                position = cursor.position()
                value = value[:WORLD_PROMPT_LIMIT]
                with QSignalBlocker(edit):
                    edit.setPlainText(value)
                cursor = edit.textCursor()
                cursor.setPosition(min(position, WORLD_PROMPT_LIMIT))
                edit.setTextCursor(cursor)
            before = self.edit_snapshot()
            set_world_prompt(self.state, value)
            self.world_prompt_count.setText(f'World building prompt: {len(self.state.world_prompt)}/{WORLD_PROMPT_LIMIT} characters')
            self.persist_edits()
            self.record_edit(before, ('world_prompt',))
            return
        if identity:
            before = self.edit_snapshot()
            identity.description = self.catalog_edits[kind].toPlainText()
            identity.manual = True
            self.persist_edits()
            self.record_edit(before, ('identity', identity.identifier))

    def scene_edited(self, name):
        if not self.session or self.worker:
            return
        scene = self.scene()
        if not scene:
            return
        before = self.edit_snapshot()
        text = self.fields[name].toPlainText()
        value = text.splitlines() if name in self.list_fields else text
        key = (scene.identifier, name)
        try:
            updated = Scene.model_validate(scene.model_dump() | {name: value})
        except ValueError:
            self._scene_drafts[key] = text
            self.record_edit(before, ('scene', *key))
            self.message(f'Changes to {name.replace("_", " ")} need a valid value before they can be saved.')
            return
        self._scene_drafts.pop(key, None)
        if getattr(scene, name) != value:
            updated.manual_fields = sorted(set(scene.manual_fields + [name]))
            if name == 'final_prompt':
                updated.prompt_dependency = dependency(self.state, updated)
            self.state.scenes[self.scene_list.currentRow()] = updated
            self.persist_edits()
            if name in CAMERA_RENDER_FIELDS:
                self.refresh_video_scenes()
        self.record_edit(before, ('scene', *key))

    def show_scene(self,*_):
        s=self.scene()
        for name,edit in self.fields.items():
            value=getattr(s,name) if s else ''
            text = '\n'.join(value) if isinstance(value,list) else str(value) if value is not None else ''
            with QSignalBlocker(edit):
                edit.setPlainText(self._scene_drafts.get((s.identifier, name), text) if s else text)
        if s and s.end <= len(self.prose._source_text):
            self.prose.highlight_range(s.start,s.end)
            self.prompt_status.setText(f'Exact prose [{s.start}:{s.end}] · source {s.source_version[:12]} · '+
                ('Prompt out of date; edit or regenerate' if s.prompt_dependency!=dependency(self.state,s) else 'Prompt current'))

    def save_scene(self):
        def work():
            self.require(); s=self.scene()
            if not s:return
            changes={k:(edit.toPlainText().splitlines() if k in self.list_fields else edit.toPlainText()) for k,edit in self.fields.items()}
            updated=Scene.model_validate(s.model_dump()|changes)
            changed=[k for k in changes if getattr(s,k)!=getattr(updated,k)]
            updated.manual_fields=list(set(s.manual_fields+changed))
            if 'final_prompt' in changed:updated.prompt_dependency=dependency(self.state,updated)
            self.state.scenes[self.scene_list.currentRow()]=updated; self.state.save(self.session.root); self.refresh()
        self.guarded(work)

    def regenerate(self):
        def work():
            self.require(); s=self.scene()
            if not s:return
            self.run(lambda cancel,progress:regenerate_prompt(self.session,self.state,s,make_provider(load_ai_settings())),
                description=f'Regenerate prompt · Scene {s.sequence}: {s.title} · Asking AI to revise staging and the image prompt')
        self.guarded(work)

    def split_scene(self):
        def work():
            self.require(); s=self.scene(); span=self.prose.selected_source_range()
            if not s or not span or not s.start<span[0]<s.end:raise ValueError('Select prose inside the scene; its selection start becomes the split')
            if any(t.scene_id==s.identifier for t in self.state.timeline):raise ValueError('Preserve/export timeline edits before restructuring this scene in a new project')
            cut=span[0]; a=s.model_dump(); b=s.model_dump()
            a.update(end=cut,text=s.text[:cut-s.start],final_prompt='',prompt_dependency='')
            b.update(identifier=str(uuid4()),start=cut,text=s.text[cut-s.start:],final_prompt='',prompt_dependency='')
            i=self.scene_list.currentRow(); self.state.scenes[i:i+1]=[Scene(**a),Scene(**b)]
            self.renumber()
        self.guarded(work)

    def merge_scene(self):
        def work():
            self.require(); i=self.scene_list.currentRow()
            if i<0 or i+1>=len(self.state.scenes):return
            a,b=self.state.scenes[i:i+2]
            if a.production_included!=b.production_included:raise ValueError('Cannot merge across manuscript exclusions')
            if any(t.scene_id in {a.identifier,b.identifier} for t in self.state.timeline):raise ValueError('Timeline placements preserved; merge requires a project without these placements')
            data=a.model_dump(); data.update(end=b.end,text=a.text+b.text,final_prompt='',prompt_dependency='',identity_links=list(set(a.identity_links+b.identity_links)))
            self.state.scenes[i:i+2]=[Scene(**data)]; self.renumber()
        self.guarded(work)

    def renumber(self):
        for i,s in enumerate(self.state.scenes,1):s.sequence=i
        self.state.save(self.session.root); self.refresh()

    def still_scene(self):
        row=self.still_list.currentRow(); return self.state.scenes[row] if 0<=row<len(self.state.scenes) else None

    def show_still(self,*_):
        scene=self.still_scene(); self.candidates.clear(); self.preview.clear(); self.still_details.clear()
        if not scene:return
        self.scene_list.setCurrentRow(self.still_list.currentRow()); self.prose.highlight_range(scene.start,scene.end)
        assets=[s for s in self.state.stills if s.scene_id==scene.identifier]
        for s in assets:self.candidates.addItem(s.identifier+(' · selected' if self.state.selected.get(scene.identifier)==s.identifier else ''))
        asset=self.state.current_still(scene.identifier)
        if asset:
            self.preview.setPixmap(QPixmap(str(self.session.root/asset.path)).scaled(800,350,Qt.KeepAspectRatio,Qt.SmoothTransformation))
            self.still_details.setPlainText(asset.model_dump_json(indent=2))
            if asset.dependency and asset.dependency!=source_hash(scene.final_prompt+dependency(self.state,scene)):
                self.still_details.appendPlainText('\nImage inputs out of date; existing selection preserved.')

    def generate(self,scope):
        def work():
            self.require()
            if scope in {'all','missing'}:scenes=[s for s in self.state.scenes if s.production_included and (scope=='all' or not self.state.current_still(s.identifier))]
            else:scenes=[self.still_scene() if scope=='still' else self.scene()]
            scenes=[s for s in scenes if s]
            def job(cancel,progress):
                from prose_to_video.pipeline.image_generation import make_image_provider
                provider=make_image_provider(self.state.image_settings,self.session.root)
                for i,s in enumerate(scenes,1):
                    if cancel.is_set():return
                    def image_progress(message):
                        progress(f'Image {i}/{len(scenes)} · {message}')
                    generate_still(self.session,self.state,s,provider,cancel,image_progress)
            self.run(job, description=f'Generate stills · {len(scenes)} scenes')
        self.guarded(work)

    def import_still(self):
        def work():
            self.require(); scene=self.still_scene()
            if not scene:return
            path,_=QFileDialog.getOpenFileName(self,'Import still')
            if path:self.run(lambda cancel,progress:store_still(self.session,self.state,scene,Path(path).read_bytes()),
                description=f'Import still: {Path(path).name} · Checking and saving the image for scene {scene.sequence}')
        self.guarded(work)

    def select_candidate(self):
        def work():
            self.require(); scene=self.still_scene(); row=self.candidates.currentRow()
            assets=[s for s in self.state.stills if scene and s.scene_id==scene.identifier]
            if 0<=row<len(assets):self.state.selected[scene.identifier]=assets[row].identifier; self.state.save(self.session.root); self.show_still()
        self.guarded(work)

    def align(self):
        def work():
            self.require()
            from prose_to_video.gui.alignment_settings import AlignmentSettingsDialog
            from prose_to_video.pipeline.audio_alignment import align_project
            if not self.session.project.audio_path:raise ValueError('Link narration audio in Start first')
            dialog=AlignmentSettingsDialog(self)
            if dialog.exec()!=QDialog.Accepted:return
            settings=dialog.values(allow_download=True)
            self.run(lambda cancel,progress:align_project(self.session.project.resolved_prose(self.session.root),
                self.session.project.resolved_audio(self.session.root),self.state.scenes,self.session.database,
                settings,cancel=cancel,progress=lambda stage,n,total:progress(f'Aligning narration · {stage}: {n}/{total}')),
                description='Align narration · Matching spoken words to prose for scene timing')
        self.guarded(work)

    def correct_alignment(self):
        def work():
            self.require()
            from prose_to_video.gui.transcript_review import TranscriptReviewDialog
            from prose_to_video.pipeline.transcript_review import prepare_review, realign_review
            run, text = prepare_review(self.session, self.state.scenes)
            dialog = TranscriptReviewDialog(self.session, run, text, self)
            self.audio.player.pause()
            if dialog.exec() != QDialog.Accepted: return
            def apply(cancel, progress):
                realign_review(self.session, self.state.scenes, run, dialog.draft,
                    cancel=cancel, progress=progress)
                return assemble_timeline(self.session, self.state, cancel, progress)
            self.run(apply, lambda intervals:self.message(
                f'Narration checked; timeline ready with {len(intervals)} stills. Play to preview or Export Video.'),
                description='Review narration · Applying transcript corrections, realigning words and rebuilding the timeline')
        self.guarded(work)

    def build(self):
        def work():
            self.require()
            mode = self.timeline_mode.currentData()
            self.run(lambda cancel,progress:assemble_timeline(self.session,self.state,cancel,progress,timing_mode=mode),
                lambda intervals:self.message(
                    f"{'Approximate timeline' if any(t.estimated for t in intervals) else 'Timeline'} ready: "
                    f'{len(intervals)} stills · {intervals[-1].end:.1f}s of narration. Preview and adjust cuts, or Export Video.'),
                description='Build timeline · Checking selected stills and matching scene passages to narration')
        self.guarded(work)

    def timeline_edited(self, item):
        if not self._updating_timeline and item.column() in {1, 2}:
            self._timeline_dirty = True
            self.update_undo_action()

    def save_timeline(self):
        def work():
            self.require(); self.commit_timeline_edits(); self.refresh()
        self.guarded(work)

    def commit_timeline_edits(self):
        if not self._timeline_dirty:
            return
        before = self.edit_snapshot()
        values=[t.model_copy(deep=True) for t in self.state.timeline]
        for row, interval in enumerate(values):
            interval.start=float(self.timeline.item(row,1).text())
            interval.end=float(self.timeline.item(row,2).text())
        if [t.scene_id for t in values]!=[t.scene_id for t in self.state.timeline]:raise ValueError('Preserve scene order and IDs')
        validate_timeline(values,self.session.project.audio_duration)
        for a,b in zip(values,self.state.timeline):
            if (a.speech_start,a.speech_end)!=(b.speech_start,b.speech_end):raise ValueError('Speech spans come from alignment; edit display start/end only')
            a.manual=b.manual or a.start!=b.start or a.end!=b.end
        if values != self.state.timeline:
            updated = self.state.model_copy(update={'timeline': values})
            updated.save(self.session.root)
            self.state = updated
        self._timeline_dirty = False
        self.record_edit(before)
        self.update_undo_action()
        self.sync_timeline_markers()

    def render(self):
        def work():
            self.require()
            from prose_to_video.pipeline.audio_transcription import source_file_hash
            audio = self.session.project.resolved_audio(self.session.root)
            if not audio: raise ValueError('Link narration audio first')
            current_audio_hash = source_file_hash(audio)
            approved_audio_hash = None
            if current_audio_hash != self.state.timeline_audio_hash:
                # Check timing and assets before asking the user to approve this audio.
                plan = render_plan(self.session, self.state, approved_audio_hash=current_audio_hash)
                answer = QMessageBox.question(self, 'Export anyway?',
                    f'Audio changed since the timeline was built. The saved cuts cover the current '
                    f'{plan.audio_duration:.3f}s narration. Export anyway with this audio and these cuts?',
                    QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
                if answer != QMessageBox.Yes:
                    return
                approved_audio_hash = current_audio_hash
            path,_=QFileDialog.getSaveFileName(self,'Export video',str(self.session.root/'output'/'video.mp4'),'MP4 (*.mp4)')
            if not path:return
            def job(cancel,progress):
                from prose_to_video.rendering.ffmpeg_renderer import FFmpegRenderer
                progress('Preparing video export · Checking scene images, timing, narration and render settings')
                plan=render_plan(self.session,self.state,approved_audio_hash=approved_audio_hash)
                metadata=FFmpegRenderer(self.session.root).render(plan,Path(path),cancel=cancel,progress=lambda stage,seconds:progress(f'Exporting {Path(path).name} · {stage}: {seconds:.1f}s processed of {plan.audio_duration:.1f}s'))
                Path(path+'.json').write_text(plan.model_dump_json(indent=2),encoding='utf-8'); return metadata
            self.run(job, description=f'Export video: {Path(path).name}')
        self.guarded(work)

    def preview_export(self):
        path,_=QFileDialog.getOpenFileName(self,'Preview exported video',str(self.session.root/'output') if self.session else '', 'Video (*.mp4)')
        if path:QDesktopServices.openUrl(QUrl.fromLocalFile(path))

    def closeEvent(self,event):
        if self.worker:self.cancel(); event.ignore(); return
        try:
            self.flush_edits()
        except (ValueError, OSError) as exc:
            QMessageBox.warning(self, 'Could not save changes', str(exc))
            event.ignore()
            return
        self.audio.set_audio(None)
        event.accept()
