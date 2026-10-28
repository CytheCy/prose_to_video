"""Motion prompt editor, clip gallery, and generation controls."""
from __future__ import annotations
import json
import shutil
from pathlib import Path
from PySide6.QtCore import Qt, QThread, QUrl, Signal
from threading import Event
from PySide6.QtGui import QPixmap, QDesktopServices
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtMultimediaWidgets import QVideoWidget
from PySide6.QtWidgets import (QWidget,QVBoxLayout,QHBoxLayout,QFormLayout,QLabel,QPushButton,
    QComboBox,QPlainTextEdit,QListWidget,QMessageBox,QFileDialog,QDialog,QDialogButtonBox,
    QSpinBox,QDoubleSpinBox,QLineEdit,QCheckBox,QInputDialog)
from PySide6.QtWidgets import QAbstractItemView
from prose_to_video.models.video_job import VideoGenerationSettings
from prose_to_video.pipeline.video_generation import VideoGenerator,eligible_video_shots,make_video_provider
from prose_to_video.pipeline.video_providers.comfyui_provider import ComfyUIVideoProvider
from prose_to_video.models.video_job import VideoRequest
from prose_to_video.pipeline.motion_prompt_generation import generate_motion_prompt,resolve_ending_image
from prose_to_video.pipeline.providers import make_provider
from prose_to_video.gui.ai_settings import load_ai_settings
from prose_to_video.storage.generated_images import ImageStore
from prose_to_video.storage.generated_videos import VideoStore
from prose_to_video.storage.visual_library import VisualLibrary

class VideoGenerationSettingsDialog(QDialog):
    def __init__(self,settings,parent=None,root=None,starting_image_path=None):
        super().__init__(parent);self.setWindowTitle('Video Generation Settings')
        self.root=Path(root) if root else None;self.starting_image_path=starting_image_path
        form=QFormLayout(self)
        self.provider=QComboBox();self.provider.addItem('ComfyUI')
        self.first_frame=QLabel('First-frame conditioning: approved storyboard image (required)')
        self.model=QLineEdit(settings.model)
        self.url=QLineEdit(settings.comfyui_url)
        self.workflow=QLineEdit(settings.workflow_path)
        browse=QPushButton('Import API workflow JSON…')
        browse.clicked.connect(self._browse)
        test=QPushButton('Test Connection');test.clicked.connect(self.test_connection)
        validate=QPushButton('Validate Workflow');validate.clicked.connect(self.validate_workflow)
        self.mapping=QPlainTextEdit(json.dumps(settings.workflow_mapping,indent=2));self.mapping.setMinimumHeight(140)
        self.width=QSpinBox();self.width.setRange(64,8192);self.width.setValue(settings.width)
        self.height=QSpinBox();self.height.setRange(64,8192);self.height.setValue(settings.height)
        self.duration=QDoubleSpinBox();self.duration.setRange(.1,120);self.duration.setValue(settings.duration)
        self.fps=QSpinBox();self.fps.setRange(1,120);self.fps.setValue(settings.frame_rate)
        self.seed=QLineEdit('' if settings.seed is None else str(settings.seed))
        self.candidates=QSpinBox();self.candidates.setRange(1,16);self.candidates.setValue(settings.candidates)
        self.intensity=QLineEdit('' if settings.motion_intensity is None else str(settings.motion_intensity))
        self.camera=QComboBox();self.camera.addItems(['Stationary','Follow storyboard']);self.camera.setCurrentText(settings.camera_preference)
        self.last=QCheckBox();self.last.setChecked(settings.use_last_frame)
        self.references=QCheckBox();self.references.setChecked(settings.use_references)
        self.concurrent=QSpinBox();self.concurrent.setRange(1,16);self.concurrent.setValue(settings.max_concurrent_jobs)
        self.retries=QSpinBox();self.retries.setRange(0,5);self.retries.setValue(settings.retry_limit)
        self.cap=QLineEdit('' if settings.spending_cap is None else str(settings.spending_cap))
        self.estimate=QLineEdit('' if settings.estimated_cost_per_clip is None else str(settings.estimated_cost_per_clip))
        self.format=QComboBox();self.format.addItems(['mp4','webm','mov']);self.format.setCurrentText(settings.output_format)
        for label,control in [('Provider',self.provider),('',self.first_frame),('Workflow model (blank uses workflow)',self.model),
            ('ComfyUI URL',self.url),('',test),('Workflow JSON',self.workflow),('',browse),
            ('Node mapping JSON',self.mapping),('',validate),
            ('Width',self.width),('Height',self.height),('Duration (s)',self.duration),('Frame rate',self.fps),
            ('Seed (optional)',self.seed),('Candidates',self.candidates),('Motion intensity (optional)',self.intensity),
            ('Camera preference',self.camera),('Use ending image',self.last),('Use references',self.references),
            ('Maximum concurrent jobs',self.concurrent),('Retry limit',self.retries),('Project spending cap',self.cap),
            ('Estimated cost per clip',self.estimate),('Output format',self.format)]:form.addRow(label,control)
        self.capabilities=QLabel('Mapped controls depend on the imported workflow.');self.capabilities.setWordWrap(True)
        form.addRow(self.capabilities)
        self.mapping.textChanged.connect(self._capabilities)
        buttons=QDialogButtonBox(QDialogButtonBox.Save|QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept);buttons.rejected.connect(self.reject);form.addRow(buttons)
        self._capabilities()
    def _browse(self):
        path,_=QFileDialog.getOpenFileName(self,'Import ComfyUI API workflow','','JSON (*.json)')
        if path:self.workflow.setText(path)
    def test_connection(self):
        try:
            result=ComfyUIVideoProvider(self.url.text().strip(),timeout=3).test_connection()
            QMessageBox.information(self,'ComfyUI connection',result)
        except Exception as exc:QMessageBox.warning(self,'ComfyUI connection',str(exc))
    def validate_workflow(self):
        try:
            settings=self.values()
            path=Path(settings.workflow_path)
            if not path.is_absolute() and self.root:path=self.root/path
            workflow=json.loads(path.read_text())
            if not self.starting_image_path:raise ValueError('Approve a starting image before validating this workflow')
            provider=ComfyUIVideoProvider(settings.comfyui_url,workflow,settings.workflow_mapping)
            mapping=settings.workflow_mapping
            if 'duration' not in mapping and 'frame_count' not in mapping and mapping.get('fixed_duration')!=settings.duration:
                raise ValueError('Workflow cannot control the requested duration')
            if 'frame_rate' not in mapping and mapping.get('fixed_frame_rate')!=settings.frame_rate:
                raise ValueError('Workflow cannot control the requested frame rate')
            if not {'width','height'}<=mapping.keys() and (mapping.get('fixed_width'),mapping.get('fixed_height'))!=(settings.width,settings.height):
                raise ValueError('Workflow cannot control the requested resolution')
            if settings.use_last_frame and 'ending_image' not in mapping:
                raise ValueError('Workflow has no ending-image input')
            if settings.use_references and not mapping.get('references'):
                raise ValueError('Workflow has no reference-image inputs')
            request=VideoRequest(model=settings.model,starting_image_path=str(self.starting_image_path),
                prompt='Validation prompt',duration=settings.duration if 'duration' in mapping else None,
                frame_count=round(settings.duration*settings.frame_rate) if 'frame_count' in mapping else None,
                width=settings.width if 'width' in mapping else None,
                height=settings.height if 'height' in mapping else None,
                frame_rate=settings.frame_rate if 'frame_rate' in mapping else None,
                seed=settings.seed,motion_intensity=settings.motion_intensity,output_format=settings.output_format)
            provider.validate(request)
            QMessageBox.information(self,'Video workflow','Workflow mapping and requested controls are valid.')
        except Exception as exc:QMessageBox.warning(self,'Video workflow',str(exc))
    def _capabilities(self):
        try:
            m=json.loads(self.mapping.toPlainText());keys=[key for key in ('ending_image','references','negative_prompt','duration','frame_count','width','height','frame_rate','seed','motion_intensity') if key in m]
            self.capabilities.setText('Workflow controls: '+(', '.join(keys) if keys else 'none mapped'))
            self.last.setEnabled('ending_image' in m)
            self.references.setEnabled(bool(m.get('references')))
            self.seed.setEnabled('seed' in m)
            self.intensity.setEnabled('motion_intensity' in m)
            if 'ending_image' not in m:self.last.setChecked(False)
            if not m.get('references'):self.references.setChecked(False)
            if 'seed' not in m:self.seed.clear()
            if 'motion_intensity' not in m:self.intensity.clear()
            self.width.setEnabled('width' in m and 'height' in m)
            self.height.setEnabled('width' in m and 'height' in m)
            self.duration.setEnabled('duration' in m or 'frame_count' in m)
            self.fps.setEnabled('frame_rate' in m)
            self.format.setEnabled('output_format' in m)
            if 'width' not in m and 'fixed_width' in m:self.width.setValue(int(m['fixed_width']))
            if 'height' not in m and 'fixed_height' in m:self.height.setValue(int(m['fixed_height']))
            if 'duration' not in m and 'frame_count' not in m and 'fixed_duration' in m:self.duration.setValue(float(m['fixed_duration']))
            if 'frame_rate' not in m and 'fixed_frame_rate' in m:self.fps.setValue(int(m['fixed_frame_rate']))
            if 'output_format' not in m and 'fixed_output_format' in m:self.format.setCurrentText(m['fixed_output_format'])
        except ValueError:self.capabilities.setText('Mapping JSON is invalid')
    def values(self):
        return VideoGenerationSettings(provider=self.provider.currentText(),model=self.model.text().strip(),
            comfyui_url=self.url.text().strip(),workflow_path=self.workflow.text().strip(),
            workflow_mapping=json.loads(self.mapping.toPlainText()),width=self.width.value(),height=self.height.value(),
            duration=self.duration.value(),frame_rate=self.fps.value(),seed=int(self.seed.text()) if self.seed.text().strip() else None,
            candidates=self.candidates.value(),motion_intensity=float(self.intensity.text()) if self.intensity.text().strip() else None,
            camera_preference=self.camera.currentText(),use_last_frame=self.last.isChecked(),use_references=self.references.isChecked(),
            max_concurrent_jobs=self.concurrent.value(),retry_limit=self.retries.value(),
            spending_cap=float(self.cap.text()) if self.cap.text().strip() else None,
            estimated_cost_per_clip=float(self.estimate.text()) if self.estimate.text().strip() else None,
            output_format=self.format.currentText())

class VideoGenerationTask(QThread):
    result=Signal(str)
    completed=Signal()
    def __init__(self,session,items,parent=None):
        super().__init__(parent);self.session=session;self.items=items;self.cancel_requested=False
    def run(self):
        from concurrent.futures import ThreadPoolExecutor,as_completed
        try:
            workers=VideoStore(self.session.root).settings().max_concurrent_jobs
            with ThreadPoolExecutor(max_workers=workers) as pool:
                futures=[pool.submit(self._process,job,request) for job,request in self.items]
                for future in as_completed(futures):future.result()
        except Exception as exc:self.result.emit(f'Video queue failed: {exc}')
        finally:self.completed.emit()

    def _process(self,job,request):
        try:
            if self.cancel_requested:
                store=VideoStore(self.session.root)
                if job.status=='Pending':job.status='Canceled';store.update_job(job)
                return
            provider=make_video_provider(VideoGenerationSettings.model_validate(job.settings),self.session.root)
            generator=VideoGenerator(self.session,provider)
            if request is None:generator.reconcile(job)
            else:
                generator.provider.validate(request)
                generator.run(job,request)
            if job.status in {'Submitted','Running'} and job.provider_job_id:
                import time
                while not self.cancel_requested and job.status in {'Submitted','Running'}:
                    time.sleep(2);generator.reconcile(job)
                if self.cancel_requested and job.status in {'Submitted','Running'}:
                    generator.cancel(job)
            self.result.emit(f'{job.shot_id}: {job.status} {job.error}')
        except Exception as exc:self.result.emit(f'{job.shot_id}: {exc}')

class MotionPromptTask(QThread):
    result=Signal(str)
    completed=Signal()
    def __init__(self,session,scene,shot,ending,parent=None):
        super().__init__(parent);self.session=session;self.scene=scene;self.shot=shot;self.ending=ending
        self.cancel_event=Event()
    def run(self):
        try:
            provider=make_provider(load_ai_settings())
            generate_motion_prompt(self.session,self.scene,self.shot,provider=provider,
                ending_image_id=self.ending,cancel=self.cancel_event)
            self.result.emit('Motion prompt generated for review')
        except Exception as exc:self.result.emit(str(exc))
        self.completed.emit()

class AnimationPanel(QWidget):
    status_changed=Signal()
    def __init__(self,parent=None):
        super().__init__(parent);self.session=None;self.scene=None;self.shot=None;self.task=None;self.prompt_task=None
        layout=QVBoxLayout(self)
        self.status=QLabel('Select a storyboard shot.');self.status.setWordWrap(True);layout.addWidget(self.status)
        self.cost_summary=QLabel('Video spending: —');layout.addWidget(self.cost_summary)
        self.version=QComboBox();self.version.currentIndexChanged.connect(self._version_changed)
        layout.addWidget(self.version)
        self.start_image=QLabel('No approved starting image');self.start_image.setMinimumHeight(100)
        layout.addWidget(self.start_image)
        self.prose=QLabel();self.prose.setWordWrap(True);layout.addWidget(self.prose)
        self.description=QLabel();self.description.setWordWrap(True);layout.addWidget(self.description)
        self.duration_label=QLabel();layout.addWidget(self.duration_label)
        self.provider_label=QLabel();layout.addWidget(self.provider_label)
        self.ending=QComboBox();self.ending.addItem('No ending image',None)
        self.ending.currentIndexChanged.connect(self._ending_selected)
        end_row=QHBoxLayout();end_row.addWidget(QLabel('Ending image'));end_row.addWidget(self.ending);layout.addLayout(end_row)
        self.ending_preview=QLabel();self.ending_preview.setMinimumHeight(75);layout.addWidget(self.ending_preview)
        import_end=QPushButton('Import Ending Image…');import_end.clicked.connect(self.import_ending)
        layout.addWidget(import_end)
        self.prompt=QPlainTextEdit();self.prompt.setPlaceholderText('Describe only the motion in this shot.')
        self.negative=QPlainTextEdit();self.negative.setMaximumHeight(55)
        self.camera=QLineEdit();self.continuity=QPlainTextEdit();self.continuity.setMaximumHeight(55)
        layout.addWidget(QLabel('Motion prompt'));layout.addWidget(self.prompt)
        layout.addWidget(QLabel('Negative prompt'));layout.addWidget(self.negative)
        layout.addWidget(QLabel('Camera movement'));layout.addWidget(self.camera)
        layout.addWidget(QLabel('Continuity constraints'));layout.addWidget(self.continuity)
        row=QHBoxLayout()
        for title,callback in [('Generate Motion Prompt',self.generate_prompt),('Edit Prompt',lambda:self.prompt.setFocus()),
            ('Save Changes',self.save_prompt),
            ('Regenerate Prompt',self.generate_prompt),('Approve Prompt',self.approve_prompt),
            ('Revert Changes',self.revert_changes),('Copy Prompt',self.copy_prompt),
            ('Video Settings…',self.settings_dialog)]:
            button=QPushButton(title);button.clicked.connect(callback);row.addWidget(button)
        layout.addLayout(row)
        self.clips=QListWidget();self.clips.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.clips.setMaximumHeight(130);layout.addWidget(self.clips)
        self.poster=QLabel();self.poster.setMinimumHeight(80);layout.addWidget(self.poster)
        self.clips.currentRowChanged.connect(self._clip_selected)
        row2=QHBoxLayout()
        for title,callback in [('Generate Clip',self.generate_clip),('Regenerate Clip',self.generate_clip),
            ('Cancel Generation',self.cancel_generation),('Generate Scene',lambda:self.generate_batch('scene')),
            ('Generate Project',lambda:self.generate_batch('project')),
            ('Resume / Reconcile Jobs',self.reconcile_jobs),('Approve Clip',self.approve_clip),
            ('Compare Candidates',self.compare_clips),('Reject Clip',self.reject_clip),('Lock / Unlock',self.toggle_lock),
            ('Generate Continuation',self.continuation),('Open Clip File',self.open_clip),
            ('Generation Details',self.details),('Export…',self.export)]:
            button=QPushButton(title);button.clicked.connect(callback);row2.addWidget(button)
        layout.addLayout(row2)
        self.video=QVideoWidget();self.video.setMinimumHeight(180);layout.addWidget(self.video)
        self.audio=QAudioOutput(self);self.player=QMediaPlayer(self);self.player.setAudioOutput(self.audio);self.player.setVideoOutput(self.video)
        self.audio.setMuted(True)
        play_row=QHBoxLayout()
        for title,callback in [('Play Clip',self.play_clip),('Pause',self.player.pause),('Stop',self.player.stop)]:
            button=QPushButton(title);button.clicked.connect(callback);play_row.addWidget(button)
        layout.addLayout(play_row)
    def bind(self,session,scene,shot):
        if session is not self.session and self.session is not None:self.cancel_generation()
        self.session,self.scene,self.shot=session,scene,shot;self.refresh()
    def closeEvent(self,event):
        self.cancel_generation()
        self.player.stop()
        for task in (self.prompt_task,self.task):
            if task and task.isRunning():task.wait(35_000)
        super().closeEvent(event)
    def _error(self,exc):QMessageBox.warning(self,'AI Animation',str(exc))
    def refresh(self):
        self.clips.clear();self.ending.clear();self.ending.addItem('No ending image',None)
        self.version.blockSignals(True);self.version.clear();self._versions=[]
        if not self.session or not self.shot:
            self.status.setText('Select a storyboard shot.');self.version.blockSignals(False);return
        store=VideoStore(self.session.root);images=ImageStore(self.session.root)
        approved=images.approved(self.shot.identifier)
        if approved and (self.session.root/approved[0].path).is_file():
            self.start_image.setPixmap(QPixmap(str(self.session.root/approved[0].path)).scaled(280,130,Qt.KeepAspectRatio))
        else:self.start_image.setText('No approved starting image')
        for asset,_ in images.approved_map().values():
            if approved and asset.identifier!=approved[0].identifier:
                self.ending.addItem(f'Approved · {asset.identifier[:8]}',asset.identifier)
        for asset in VisualLibrary(self.session.root).assets():
            self.ending.addItem(f'Imported · {asset.identifier[:8]}',asset.identifier)
        self._versions=store.prompts(self.shot.identifier)
        for item in self._versions:self.version.addItem(f'Motion prompt v{item.version} · {item.review_status}',item.identifier)
        self.version.blockSignals(False);self._version_changed(0)
        self.prose.setText('Prose: '+self.shot.original_prose)
        self.description.setText('Shot: '+self.shot.visual_description)
        self.duration_label.setText(f'Shot duration: {self.shot.duration or 0:.2f}s')
        settings=store.settings();self.provider_label.setText(f'Provider: {settings.provider} · {settings.model or "workflow model"}')
        jobs=store.jobs();actual=sum(j.actual_cost for j in jobs if j.actual_cost is not None)
        estimated=sum(j.estimated_cost for j in jobs if j.actual_cost is None and j.estimated_cost is not None)
        unknown=sum(j.actual_cost is None and j.estimated_cost is None and j.status not in {'Failed','Canceled'} for j in jobs)
        cap='unlimited' if settings.spending_cap is None else f'{settings.spending_cap:.2f}'
        self.cost_summary.setText(f'Video spending · actual {actual:.2f} · estimated {estimated:.2f} · '
            f'unknown-cost jobs {unknown} · cap {cap}')
        selected,locked=store.approved(self.shot.identifier)
        active=self._versions[0] if self._versions else None
        self.status.setText(f'Motion prompt: {active.review_status if active else "missing"} · Approved clips: {len(selected)}'+(' · Locked' if locked else ''))
        self._assets=store.clips(self.shot.identifier)
        for clip in self._assets:
            self.clips.addItem(f'{clip.candidate_number}: {clip.duration:.2f}s · {clip.width}×{clip.height} · '
                f'{clip.provider}/{clip.model} · {clip.created_at.date()} · {clip.approval_status}')
            self.clips.item(self.clips.count()-1).setToolTip(
                (store.prompt(clip.prompt_id).positive_prompt if store.prompt(clip.prompt_id) else 'Motion prompt missing'))
        self.status_changed.emit()
    def _version_changed(self,index):
        versions=getattr(self,'_versions',[])
        if not 0<=index<len(versions):
            self.prompt.clear();self.negative.clear();self.camera.setText('Stationary');self.continuity.clear();return
        prompt=versions[index]
        self.prompt.setPlainText(prompt.positive_prompt);self.negative.setPlainText(prompt.negative_prompt)
        self.camera.setText(prompt.camera_movement);self.continuity.setPlainText(prompt.continuity_constraints)
        self.ending.setCurrentIndex(max(0,self.ending.findData(prompt.ending_image_id)))
    def _ending_selected(self,index):
        if not self.session:return
        asset=resolve_ending_image(self.session.root,self.ending.itemData(index)) if index>=0 else None
        if asset and (self.session.root/asset.path).is_file():
            self.ending_preview.setPixmap(QPixmap(str(self.session.root/asset.path)).scaled(200,100,Qt.KeepAspectRatio))
        else:self.ending_preview.setPixmap(QPixmap())
    def revert_changes(self):
        self._version_changed(self.version.currentIndex())
    def _clip_selected(self,index):
        if 0<=index<len(getattr(self,'_assets',[])):
            clip=self._assets[index];path=self.session.root/clip.poster_path
            if path.is_file():self.poster.setPixmap(QPixmap(str(path)).scaled(280,130,Qt.KeepAspectRatio))
    def selected_clip(self):
        row=self.clips.currentRow();return self._assets[row] if 0<=row<len(getattr(self,'_assets',[])) else None
    def generate_prompt(self):
        if not self.session or not self.shot or not self.scene or self.prompt_task:return
        task=MotionPromptTask(self.session,self.scene,self.shot,self.ending.currentData(),self)
        self.prompt_task=task;task.result.connect(self.status.setText)
        task.completed.connect(self._prompt_complete);task.start()
        self.status.setText('Generating motion prompt…')
    def _prompt_complete(self):
        self.prompt_task=None;self.refresh()
    def save_prompt(self):
        if not self.shot:return
        try:
            store=VideoStore(self.session.root)
            if self.version.currentIndex()<0:raise ValueError('Generate a motion prompt first')
            old=self._versions[self.version.currentIndex()]
            store.edit_prompt(old.identifier,positive_prompt=self.prompt.toPlainText(),negative_prompt=self.negative.toPlainText(),
                camera_movement=self.camera.text(),continuity_constraints=self.continuity.toPlainText(),
                ending_image_id=self.ending.currentData())
            self.refresh()
        except Exception as exc:self._error(exc)
    def approve_prompt(self):
        if not self.shot:return
        try:
            store=VideoStore(self.session.root)
            if self.version.currentIndex()<0:raise ValueError('Generate a motion prompt first')
            store.approve_prompt(self._versions[self.version.currentIndex()].identifier);self.refresh()
        except Exception as exc:self._error(exc)
    def copy_prompt(self):
        from PySide6.QtWidgets import QApplication
        QApplication.clipboard().setText(self.prompt.toPlainText())
    def import_ending(self):
        if not self.session:return
        path,_=QFileDialog.getOpenFileName(self,'Import ending image','','Images (*.png *.jpg *.jpeg *.webp)')
        if path:
            try:
                asset=VisualLibrary(self.session.root).import_image(Path(path))
                self.refresh();self.ending.setCurrentIndex(self.ending.findData(asset.identifier))
            except Exception as exc:self._error(exc)
    def settings_dialog(self):
        if not self.session:return
        store=VideoStore(self.session.root)
        approved=ImageStore(self.session.root).approved(self.shot.identifier) if self.shot else None
        start=self.session.root/approved[0].path if approved else None
        dialog=VideoGenerationSettingsDialog(store.settings(),self,root=self.session.root,starting_image_path=start)
        if dialog.exec()!=QDialog.Accepted:return
        try:
            store.save_settings(dialog.values());self.refresh()
        except Exception as exc:self._error(exc)
    def _submit(self,items):
        if self.task:return
        task=VideoGenerationTask(self.session,items,self);self.task=task
        task.result.connect(self.status.setText)
        task.completed.connect(self._complete)
        task.start();self.status.setText(f'Generating {len(items)} video clip(s)…')
    def _complete(self):
        self.task=None;self.refresh()
    def cancel_generation(self):
        if self.task:self.task.cancel_requested=True
        if self.prompt_task:self.prompt_task.cancel_event.set()
    def reconcile_jobs(self,automatic=False):
        if not self.session or self.task:return
        try:
            store=VideoStore(self.session.root);jobs=store.jobs()
            uncertain=[j for j in jobs if j.status=='Needs Reconciliation' and not j.provider_job_id]
            if uncertain and not automatic:QMessageBox.information(self,'Uncertain video jobs',
                f'{len(uncertain)} request(s) have no provider ID and will not be resubmitted.')
            pending=[] if automatic else [j for j in jobs if j.status=='Pending']
            known=[j for j in jobs if j.status in {'Submitted','Running','Needs Reconciliation'} and j.provider_job_id]
            if pending:
                if QMessageBox.question(self,'Submit pending video jobs',
                    f'Submit {len(pending)} never-submitted video job(s)? This may incur charges.',
                    QMessageBox.Yes|QMessageBox.No,QMessageBox.No)!=QMessageBox.Yes:pending=[]
            generator=VideoGenerator(self.session) if pending else None;items=[]
            for job in pending:items.append((job,generator.request_for_job(job)))
            items.extend((job,None) for job in known)
            if items:self._submit(items)
        except Exception as exc:
            if automatic:self.status.setText(f'Video jobs need reconciliation: {exc}')
            else:self._error(exc)
    def generate_clip(self):
        if not self.scene or not self.shot:return
        try:
            gen=VideoGenerator(self.session);items=[]
            for n in range(1,gen.store.settings().candidates+1):items.append(gen.enqueue(self.scene,self.shot,candidate_number=n))
            self._submit(items)
        except Exception as exc:self._error(exc)
    def generate_batch(self,scope):
        if not self.session:return
        try:
            scenes=self.session.database.load_scenes()[1]
            selected,counts=eligible_video_shots(self.session,scenes,
                [self.scene.identifier] if scope=='scene' and self.scene else None)
            settings=VideoStore(self.session.root).settings()
            estimate='unknown' if settings.estimated_cost_per_clip is None else f'{len(selected)*settings.candidates*settings.estimated_cost_per_clip:.2f}'
            message=(f"Selected: {counts['selected']} · Approved: {counts['approved']} · Static: {counts['static']} · "
                f"Missing prompts: {counts['missing_prompt']} · Skipped: {counts['skipped']}\n"
                f"Will submit {len(selected)*settings.candidates} clips to {settings.provider}, {settings.duration}s each. Estimated cost: {estimate}.")
            if QMessageBox.question(self,'Generate animation batch',message,QMessageBox.Yes|QMessageBox.No,QMessageBox.No)!=QMessageBox.Yes:return
            gen=VideoGenerator(self.session);items=[]
            for scene,shot in selected:
                for n in range(1,settings.candidates+1):items.append(gen.enqueue(scene,shot,candidate_number=n))
            self._submit(items)
        except Exception as exc:self._error(exc)
    def approve_clip(self):
        rows=sorted({self.clips.row(item) for item in self.clips.selectedItems()})
        if not rows and self.clips.currentRow()>=0:rows=[self.clips.currentRow()]
        clips=[self._assets[row] for row in rows]
        if clips:
            try:
                store=VideoStore(self.session.root);store.approve(self.shot.identifier,[clip.identifier for clip in clips])
                if len(clips)>1:store.set_production(self.shot.identifier,'AI Animation',{'strategy':'sequence'})
                if self.shot.duration and sum(c.duration for c in clips)<self.shot.duration-1/store.settings().frame_rate:
                    answer=QMessageBox.question(self,'Duration coverage','Clip is shorter than shot. Continue with approved still frame?',QMessageBox.Yes|QMessageBox.No,QMessageBox.No)
                    if answer==QMessageBox.Yes:store.set_production(self.shot.identifier,'AI Animation',{'strategy':'still_tail','tail_image_id':clips[0].starting_image_id})
                self.refresh()
            except Exception as exc:self._error(exc)
    def compare_clips(self):
        if not self.session:return
        selected=[self._assets[self.clips.row(item)] for item in self.clips.selectedItems()]
        if len(selected)<2:selected=self._assets[:4]
        if len(selected)<2:return
        dialog=QDialog(self);dialog.setWindowTitle('Compare video candidates')
        row=QHBoxLayout(dialog)
        for clip in selected[:4]:
            label=QLabel(f'{clip.duration:.2f}s · {clip.provider}');label.setPixmap(QPixmap(str(self.session.root/clip.poster_path)).scaled(240,140,Qt.KeepAspectRatio))
            row.addWidget(label)
        dialog.exec()
    def reject_clip(self):
        clip=self.selected_clip()
        if clip:
            issues=['Character appearance changed','Distorted anatomy','Object appeared or disappeared',
                'Unwanted character motion','Background instability','Camera motion mismatch','Narrative action mismatch','Other']
            issue,ok=QInputDialog.getItem(self,'Reject clip','Visual issue:',issues,0,False)
            if ok:
                notes,noted=QInputDialog.getText(self,'Reject clip','Review notes (optional):')
                if noted:
                    try:VideoStore(self.session.root).reject(clip.identifier,notes,issue);self.refresh()
                    except Exception as exc:self._error(exc)
    def toggle_lock(self):
        if self.shot:
            try:
                store=VideoStore(self.session.root);_,locked=store.approved(self.shot.identifier);store.lock(self.shot.identifier,not locked);self.refresh()
            except Exception as exc:self._error(exc)
    def continuation(self):
        clip=self.selected_clip()
        if clip:
            try:
                words,ok=QInputDialog.getMultiLineText(self,'Continue animation',
                    'Describe only the next short action after this clip:')
                if not ok or not words.strip():return
                gen=VideoGenerator(self.session);job,request=gen.enqueue(self.scene,self.shot,
                    parent_clip_id=clip.identifier,continuation_prompt=words.strip())
                self._submit([(job,request)])
            except Exception as exc:self._error(exc)
    def play_clip(self):
        clip=self.selected_clip()
        if clip:self.player.setSource(QUrl.fromLocalFile(str(self.session.root/clip.path)));self.player.play()
    def open_clip(self):
        clip=self.selected_clip()
        if clip:QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.session.root/clip.path)))
    def details(self):
        clip=self.selected_clip()
        if clip:
            job=VideoStore(self.session.root).job(clip.job_id)
            QMessageBox.information(self,'Video generation details',json.dumps({'clip':clip.model_dump(mode='json'),'job':job.model_dump(mode='json') if job else None},indent=2))
    def export(self):
        if not self.session:return
        from prose_to_video.storage.video_export import video_manifest,manifest_markdown,export_approved_clips
        choice,ok=QInputDialog.getItem(self,'Export video clips','Format',['JSON','Markdown','Approved clips and manifest'],0,False)
        if not ok:return
        try:
            if choice=='Approved clips and manifest':
                folder=QFileDialog.getExistingDirectory(self,'Export approved clips')
                if folder:export_approved_clips(self.session,folder)
            else:
                suffix='.md' if choice=='Markdown' else '.json'
                path,_=QFileDialog.getSaveFileName(self,'Export animation metadata',str(self.session.root/'video_clips'/('manifest'+suffix)))
                if path:
                    rows=video_manifest(self.session)
                    Path(path).write_text(manifest_markdown(rows) if suffix=='.md' else json.dumps(rows,indent=2),encoding='utf-8')
        except Exception as exc:self._error(exc)
