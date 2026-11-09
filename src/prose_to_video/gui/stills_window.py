"""Six-tab workspace for the direct stills workflow. No legacy workers are started."""
from pathlib import Path
from threading import Event
import json
import logging
from uuid import uuid4
from PySide6.QtCore import QThread, Signal, Slot, Qt, QUrl, QTimer, QSignalBlocker, QSettings
from PySide6.QtGui import QPixmap, QDesktopServices
from PySide6.QtWidgets import (QApplication, QMainWindow, QWidget, QTabWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QLabel, QPlainTextEdit, QListWidget, QFormLayout, QScrollArea, QSplitter,
    QFileDialog, QInputDialog, QMessageBox, QDialog, QDialogButtonBox, QComboBox, QTableWidget, QTableWidgetItem)
from prose_to_video.pipeline.stills_workflow import (Workflow, Identity, Interval, load_workflow,
    analyze, dependency, regenerate_prompt, generate_still, store_still, assemble_timeline,
    validate_timeline, render_plan, listed_identities, generate_character_reference)
from prose_to_video.storage.project_files import create_project, open_project, import_prose, import_audio
from prose_to_video.utils.prose import read_prose
from prose_to_video.models.scene import Scene, source_hash
from prose_to_video.gui.prose_viewer import ProseViewer
from prose_to_video.gui.audio_player import AudioPlayer
from prose_to_video.gui.characters_panel import ReferencePreview
from prose_to_video.gui.ai_settings import load_ai_settings
from prose_to_video.gui.stills_settings import StillsSettingsWindow
from prose_to_video.pipeline.providers import make_provider


LOGGER = logging.getLogger(__name__)


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
        self.workspace_tabs = QTabWidget(); self.setCentralWidget(self.workspace_tabs)
        self.start = self.page('Start'); self.place = self.page('Place'); self.character = self.page('Character')
        self.scenes_page = self.page('Scenes'); self.stills_page = self.page('Stills'); self.video = self.page('Video Production')
        self.status = QLabel('Create or open a project to begin'); self.status.setWordWrap(True)
        self.footer_status = QLabel()
        self.footer_status.setWordWrap(True)
        self.footer_status.setTextFormat(Qt.PlainText)
        self.footer_status.setTextInteractionFlags(Qt.TextSelectableByMouse | Qt.TextSelectableByKeyboard)
        self.statusBar().addWidget(self.footer_status, 1)
        self.start.layout().addWidget(self.status)
        self.buttons(self.start, [('New Project', self.new_project), ('Open Project', self.open_project),
            ('Import Prose', lambda:self.import_source('prose')), ('Link Narration Audio', lambda:self.import_source('audio')),
            ('Settings', lambda:self.settings())])
        self.buttons(self.start, [('Analyze Story', self.analyze), ('Cancel Work', self.cancel)])
        self.source_view = ProseViewer(); self.start.layout().addWidget(self.source_view)
        self.catalog_lists = {}; self.catalog_edits = {}
        self.character_reference_index = 0
        self._character_reference_id = None
        for kind,page in [('Place',self.place),('Character',self.character)]:
            listing = QListWidget(); edit = QPlainTextEdit()
            self.catalog_lists[kind]=listing; self.catalog_edits[kind]=edit
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
            details.layout().addWidget(QLabel('Canonical description')); details.layout().addWidget(edit)
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
            ('Review Narration…',self.correct_alignment), ('Save Timeline Edits',self.save_timeline), ('Preview Export',self.preview_export), ('Export Video',self.render), ('Cancel Work',self.cancel)])
        self.video.layout().addWidget(QLabel('Edit display start/end in seconds. Adjust both sides of a shared boundary. Approximate rows use nearest matches or interpolation. Preview and adjust cuts; manual intervals survive rebuilds.'))
        self.video_actions[0].setToolTip('Use every included scene’s selected still and the full narration. Automatically aligns audio when needed; first use may download local speech models.')
        self.timeline = QTableWidget(0, 5)
        self.timeline.setHorizontalHeaderLabels(['Scene', 'Display start', 'Display end', 'Speech / estimate start', 'Speech / estimate end'])
        self.timeline.horizontalHeader().setStretchLastSection(True)
        self.video.layout().addWidget(self.timeline)
        self.audio = AudioPlayer(); self.video.layout().addWidget(self.audio)
        self.timeline_preview = QLabel(); self.timeline_preview.setMinimumHeight(220); self.video.layout().addWidget(self.timeline_preview)
        self.preview_timer = QTimer(self); self.preview_timer.timeout.connect(self.preview_timeline); self.preview_timer.start(100)
        self._preview_asset = None
        self.video_message = QLabel(); self.video_message.setWordWrap(True); self.video.layout().addWidget(self.video_message)
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

    def buttons(self,page,items):
        row=QHBoxLayout(); page.layout().addLayout(row)
        buttons = []
        for label,callback in items:
            button=QPushButton(label); button.clicked.connect(callback); row.addWidget(button)
            buttons.append(button)
        return buttons

    def guarded(self,fn):
        try: return fn()
        except Exception as exc: QMessageBox.warning(self,'Prose to Video',str(exc))

    def require(self):
        if not self.session: raise ValueError('Create or open a project first')
        if self.worker: raise ValueError('Wait for the current operation or cancel it')

    def run(self,work,on_result=None,description='Background work'):
        self.require(); self.worker=Task(work,self)
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
        self.timeline_mode.setEnabled(False)

    def message(self,msg):
        self.status.setText(msg); self.video_message.setText(msg); self.footer_status.setText(msg)
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
        for page in (self.place,self.character,self.scenes_page,self.stills_page): page.setEnabled(True)
        for button in self.video_actions[:-1]: button.setEnabled(True)
        self.timeline.setEnabled(True)
        self.timeline_mode.setEnabled(True)
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
        state=load_workflow(session)
        self.session=session; self.state=state; self.refresh()
        self.message(session.project.name + (' · '+ '; '.join(self.state.migration_notes) if self.state.migration_notes else ''))
        path = str(session.root.resolve())
        recent = self._project_settings.value('projects/recent', [], type=list)
        self._project_settings.setValue('projects/recent', [path, *(item for item in recent if item != path)][:10])
        self._project_settings.sync()

    def refresh(self):
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
                for c in values:listing.addItem(c.name if kind == 'Character' else c.name+' · '+c.identifier)
                row = next((i for i,c in enumerate(values) if previous and c.identifier == previous.identifier), 0)
                listing.setCurrentRow(row if values else -1)
            self.show_identity(kind)
        self.scene_list.clear(); self.still_list.clear()
        for s in self.state.scenes:
            self.scene_list.addItem(f'{s.sequence}. {s.title}'+(' [excluded]' if not s.production_included else ''))
            self.still_list.addItem(f'{s.sequence}. {s.title} · '+('image selected' if self.state.current_still(s.identifier) else 'missing'))
        if scene_id:
            self.scene_list.setCurrentRow(next((i for i,s in enumerate(self.state.scenes) if s.identifier==scene_id),0))
        self.timeline_mode.setCurrentIndex(self.timeline_mode.findData(self.state.timeline_mode))
        self.timeline.setRowCount(len(self.state.timeline))
        for row, interval in enumerate(self.state.timeline):
            scene=next((s for s in self.state.scenes if s.identifier==interval.scene_id), None)
            values=[(scene.title if scene else interval.scene_id)+(' · manual' if interval.manual else '')+(' · approximate' if interval.estimated else ''), interval.start, interval.end, interval.speech_start, interval.speech_end]
            for col, value in enumerate(values):
                item=QTableWidgetItem(str(value))
                if col not in {1,2}:item.setFlags(item.flags() & ~Qt.ItemIsEditable)
                self.timeline.setItem(row,col,item)
        if self.session:
            path=self.session.project.resolved_prose(self.session.root)
            if path:
                text=read_prose(path)
                if self.state.prose_hash and source_hash(text)!=self.state.prose_hash:self.message('Prose changed; click Analyze Story to rebuild scenes, places, characters and prompts while keeping reference images')
            audio=self.session.project.resolved_audio(self.session.root)
            if audio:self.audio.set_audio(audio)

    def import_source(self,kind):
        def work():
            self.require(); path,_=QFileDialog.getOpenFileName(self,'Import '+kind)
            if not path:return
            session=self.session
            def job(cancel,progress):
                progress(f'Importing {kind}: {Path(path).name} · Copying the selected file into the project and checking its contents')
                return (import_prose if kind=='prose' else import_audio)(session,Path(path))
            self.run(job, self.source_imported, description=f'Import {kind}: {Path(path).name}')
        self.guarded(work)

    @Slot(object)
    def source_imported(self,session):
        self.session=session
        self.refresh()

    def settings(self, initial_tab=0):
        def work():
            if self.worker:
                self.message('Wait for the current operation or cancel it before changing settings')
                return
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
        return values[row] if 0<=row<len(values) else None

    def show_identity(self,kind):
        c=self.identity(kind); self.catalog_edits[kind].setPlainText(c.description if c else '')
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
            self.run(lambda cancel,progress:VisualLibrary(self.session.root).import_image(Path(path)),
                description=f'Import reference: {Path(path).name} · Checking and saving the image for {c.name}')
            def done(asset):
                c.references.append(asset.path); self.state.save(self.session.root)
            self.worker.result.connect(done)
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

    def show_scene(self,*_):
        s=self.scene()
        for name,edit in self.fields.items():
            value=getattr(s,name) if s else ''; edit.setPlainText('\n'.join(value) if isinstance(value,list) else value or '')
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

    def preview_timeline(self):
        if not self.session:return
        seconds=self.audio.player.position()/1000
        interval=next((t for t in self.state.timeline if t.start<=seconds<t.end),None)
        asset=self.state.current_still(interval.scene_id) if interval else None
        if asset and asset.identifier!=self._preview_asset:
            self._preview_asset=asset.identifier
            self.timeline_preview.setPixmap(QPixmap(str(self.session.root/asset.path)).scaled(900,280,Qt.KeepAspectRatio,Qt.SmoothTransformation))

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
            self.run(apply, description='Review narration · Applying transcript corrections, realigning words and rebuilding the timeline')
            self.worker.result.connect(lambda intervals:self.message(
                f'Narration checked; timeline ready with {len(intervals)} stills. Play to preview or Export Video.'))
        self.guarded(work)

    def build(self):
        def work():
            self.require()
            mode = self.timeline_mode.currentData()
            self.run(lambda cancel,progress:assemble_timeline(self.session,self.state,cancel,progress,timing_mode=mode),
                description='Build timeline · Checking selected stills and matching scene passages to narration')
            self.worker.result.connect(lambda intervals:self.message(
                f"{'Approximate timeline' if any(t.estimated for t in intervals) else 'Timeline'} ready: "
                f'{len(intervals)} stills · {intervals[-1].end:.1f}s of narration. Preview and adjust cuts, or Export Video.'))
        self.guarded(work)

    def save_timeline(self):
        def work():
            self.require(); values=[t.model_copy(deep=True) for t in self.state.timeline]
            for row, interval in enumerate(values):
                interval.start=float(self.timeline.item(row,1).text())
                interval.end=float(self.timeline.item(row,2).text())
            if [t.scene_id for t in values]!=[t.scene_id for t in self.state.timeline]:raise ValueError('Preserve scene order and IDs')
            validate_timeline(values,self.session.project.audio_duration)
            for a,b in zip(values,self.state.timeline):
                if (a.speech_start,a.speech_end)!=(b.speech_start,b.speech_end):raise ValueError('Speech spans come from alignment; edit display start/end only')
                a.manual=b.manual or a.start!=b.start or a.end!=b.end
            self.state.timeline=values; self.state.save(self.session.root); self.refresh()
        self.guarded(work)

    def render(self):
        def work():
            self.require(); path,_=QFileDialog.getSaveFileName(self,'Export video',str(self.session.root/'output'/'video.mp4'),'MP4 (*.mp4)')
            if not path:return
            def job(cancel,progress):
                from prose_to_video.rendering.ffmpeg_renderer import FFmpegRenderer
                progress('Preparing video export · Checking scene images, timing, narration and render settings')
                plan=render_plan(self.session,self.state)
                metadata=FFmpegRenderer(self.session.root).render(plan,Path(path),cancel=cancel,progress=lambda stage,seconds:progress(f'Exporting {Path(path).name} · {stage}: {seconds:.1f}s processed of {plan.audio_duration:.1f}s'))
                Path(path+'.json').write_text(plan.model_dump_json(indent=2),encoding='utf-8'); return metadata
            self.run(job, description=f'Export video: {Path(path).name}')
        self.guarded(work)

    def preview_export(self):
        path,_=QFileDialog.getOpenFileName(self,'Preview exported video',str(self.session.root/'output') if self.session else '', 'Video (*.mp4)')
        if path:QDesktopServices.openUrl(QUrl.fromLocalFile(path))

    def closeEvent(self,event):
        if self.worker:self.cancel(); event.ignore(); return
        self.preview_timer.stop()
        self.audio.set_audio(None)
        event.accept()
