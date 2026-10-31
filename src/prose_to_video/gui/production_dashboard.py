"""Stage Ten setup, progress, history, and issue review widgets."""
from __future__ import annotations

from pathlib import Path
from uuid import uuid4
from dataclasses import asdict

from PySide6.QtCore import QThread, Signal, Qt, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox,
    QFormLayout, QHBoxLayout, QLabel, QLineEdit, QListWidget, QMessageBox, QPushButton, QInputDialog,
    QSpinBox, QVBoxLayout, QWidget)

from prose_to_video.models.production import ProductionProfile, correction_workspace
from prose_to_video.pipeline.production_orchestrator import ProductionOrchestrator
from prose_to_video.pipeline.quality_control import QualityController
from prose_to_video.storage.generated_images import ImageStore
from prose_to_video.storage.generated_videos import VideoStore
from prose_to_video.storage.image_prompts import PromptStore
from prose_to_video.storage.production import ProductionStore
from prose_to_video.storage.video_renders import VideoRenderStore
from prose_to_video.gui.ai_settings import load_ai_settings


class ProductionSetupDialog(QDialog):
    def __init__(self, session, profile=None, parent=None):
        super().__init__(parent)
        self.session = session
        self.setWindowTitle('Generate Complete Video')
        render = profile.render_settings if profile else VideoRenderStore(session.root).settings()
        board_settings=session.database.storyboard_settings()
        self.base = profile or ProductionProfile.preset('Illustrated Story',
            render_settings=render.model_copy(update={'mode':'final'}),
            shot_density=board_settings.density,visual_style=board_settings.visual_style,
            narrative_settings=asdict(load_ai_settings()))
        self.project = QLabel(session.project.name)
        self.profile_name = QLineEdit(self.base.name)
        self.preset = QComboBox(); self.preset.addItems(['Illustrated Story','Cinematic Slideshow','AI Animated Production'])
        self.preset.setCurrentText(self.base.name if self.base.name in [self.preset.itemText(i) for i in range(self.preset.count())] else 'Illustrated Story')
        self.mode = QComboBox(); self.mode.addItems(['Review at Milestones','Fully Automatic','Manual Production'])
        self.mode.setCurrentText(self.base.automation_mode)
        self.width = QSpinBox(); self.width.setRange(16,8192); self.width.setValue(render.width)
        self.height = QSpinBox(); self.height.setRange(16,8192); self.height.setValue(render.height)
        self.fps = QSpinBox(); self.fps.setRange(1,120); self.fps.setValue(render.frame_rate)
        self.style = QLineEdit(self.base.visual_style)
        self.density = QComboBox(); self.density.addItems(['Low','Medium','High','Custom']); self.density.setCurrentText(self.base.shot_density)
        self.image_provider = QComboBox(); self.image_provider.addItems(['OpenAI','ComfyUI'])
        self.image_provider.setCurrentText(self.base.image_settings.get('provider') or ImageStore(session.root).settings().provider)
        self.video_provider = QComboBox(); self.video_provider.addItems(['ComfyUI'])
        self.video_provider.setCurrentText(self.base.video_settings.get('provider') or VideoStore(session.root).settings().provider)
        self.animation = QComboBox(); self.animation.addItems(['None','Limited','Moderate','Extensive','All eligible'])
        self.animation.setCurrentText(self.base.animation_policy)
        self.animation_percent = QSpinBox(); self.animation_percent.setRange(0,100); self.animation_percent.setValue(self.base.animation_percent)
        self.animation_max = QSpinBox(); self.animation_max.setRange(0,10000); self.animation_max.setSpecialValueText('No maximum')
        self.animation_max.setValue(self.base.animation_max or 0)
        self.allow_modes = QCheckBox('Allow policy to change existing shot modes')
        self.allow_modes.setChecked(self.base.allow_mode_changes)
        self.preset.currentTextChanged.connect(self._preset_changed)
        self.auto_approval = QCheckBox('Authorize automatic selection and approval of AI material')
        self.auto_approval.setChecked(self.base.authorize_auto_approval)
        self.unknown = QCheckBox('Authorize paid categories with unknown cost estimates')
        self.unknown.setChecked(self.base.authorize_unknown_cost)
        self.budget = QDoubleSpinBox(); self.budget.setRange(0,1000000); self.budget.setDecimals(2)
        self.budget.setSpecialValueText('No total limit'); self.budget.setValue(self.base.budget_total or 0)
        self.image_budget = QDoubleSpinBox(); self.image_budget.setRange(0,1000000); self.image_budget.setDecimals(2)
        self.image_budget.setSpecialValueText('No image limit'); self.image_budget.setValue(self.base.budget_image or 0)
        self.video_budget = QDoubleSpinBox(); self.video_budget.setRange(0,1000000); self.video_budget.setDecimals(2)
        self.video_budget.setSpecialValueText('No video limit'); self.video_budget.setValue(self.base.budget_video or 0)
        self.review_budget = QDoubleSpinBox(); self.review_budget.setRange(0,1000000); self.review_budget.setDecimals(2)
        self.review_budget.setSpecialValueText('No review limit'); self.review_budget.setValue(self.base.budget_review or 0)
        self.shot_budget = QDoubleSpinBox(); self.shot_budget.setRange(0,1000000); self.shot_budget.setDecimals(2)
        self.shot_budget.setSpecialValueText('No per-shot limit'); self.shot_budget.setValue(self.base.budget_per_shot or 0)
        self.fallback = QComboBox(); self.fallback.addItems(['Pause','Camera Motion','Static Image'])
        self.fallback.setCurrentText(self.base.fallback_animation)
        self.directory = QLineEdit(render.output_directory)
        self.filename = QLineEdit(render.output_filename)
        form = QFormLayout()
        for label, widget in [('Project',self.project),('Profile name',self.profile_name),('Preset',self.preset),('Automation',self.mode),
            ('Width',self.width),('Height',self.height),('Frame rate',self.fps),('Visual style',self.style),
            ('Shot density',self.density),('Image provider',self.image_provider),('Video provider',self.video_provider),
            ('Animation policy',self.animation),('Animation percent',self.animation_percent),
            ('Maximum animated shots',self.animation_max),('Total budget',self.budget),
            ('Image budget',self.image_budget),('Video budget',self.video_budget),
            ('Review budget',self.review_budget),('Per-shot budget',self.shot_budget),
            ('Animation fallback',self.fallback),
            ('Output directory',self.directory),('Final filename',self.filename)]: form.addRow(label,widget)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok|QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept); buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self); layout.addLayout(form)
        for widget in (self.allow_modes,self.auto_approval,self.unknown): layout.addWidget(widget)
        layout.addWidget(buttons)

    def _preset_changed(self, name):
        preset = ProductionProfile.preset(name)
        self.animation.setCurrentText(preset.animation_policy)
        self.allow_modes.setChecked(preset.allow_mode_changes)

    def value(self):
        render = self.base.render_settings.model_copy(update={
            'width':self.width.value(),'height':self.height.value(),'frame_rate':self.fps.value(),
            'output_directory':self.directory.text().strip(), 'output_filename':self.filename.text().strip(),
            'mode':'final'})
        if self.preset.currentText() == 'Cinematic Slideshow':
            render = render.model_copy(update={'motion_intensity':'Moderate','default_transition':'Crossfade'})
        image = self.base.image_settings or ImageStore(self.session.root).settings().model_dump(mode='json')
        video = self.base.video_settings or VideoStore(self.session.root).settings().model_dump(mode='json')
        image['provider'] = self.image_provider.currentText()
        video['provider'] = self.video_provider.currentText()
        value = ProductionProfile.preset(self.preset.currentText(),
            identifier=self.base.identifier if self.profile_name.text().strip() == self.base.name else str(uuid4()),
            narrative_settings=self.base.narrative_settings,
            alignment_settings=self.base.alignment_settings,
            approval_requirements=self.base.approval_requirements,
            quality_thresholds=self.base.quality_thresholds,
            concurrency=self.base.concurrency,
            retry_limit=self.base.retry_limit,
            fallback_animation=self.fallback.currentText(),
            allow_text_only_references=self.base.allow_text_only_references,
            automation_mode=self.mode.currentText(), shot_density=self.density.currentText(),
            visual_style=self.style.text().strip(), image_settings=image, video_settings=video,
            animation_policy=self.animation.currentText(), animation_percent=self.animation_percent.value(),
            animation_max=self.animation_max.value() or None, allow_mode_changes=self.allow_modes.isChecked(),
            authorize_auto_approval=self.auto_approval.isChecked(), authorize_unknown_cost=self.unknown.isChecked(),
            budget_total=self.budget.value() or None, budget_image=self.image_budget.value() or None,
            budget_video=self.video_budget.value() or None,budget_review=self.review_budget.value() or None,
            budget_per_shot=self.shot_budget.value() or None,render_settings=render)
        return ProductionProfile.model_validate(value.model_copy(update={'name':self.profile_name.text().strip()}).model_dump())


class ProductionWorker(QThread):
    progress = Signal(str,int,int)
    result = Signal(object)
    failed = Signal(str)
    def __init__(self, orchestrator, run_id, parent=None):
        super().__init__(parent)
        self.orchestrator=orchestrator; self.run_id=run_id
    def run(self):
        try: self.result.emit(self.orchestrator.run_until_pause(self.run_id,
            progress=lambda stage,done,total:self.progress.emit(stage,done,total)))
        except Exception as exc: self.failed.emit(str(exc))


class QualityWorker(QThread):
    result = Signal()
    failed = Signal(str)
    def __init__(self, session, run, parent=None):
        super().__init__(parent)
        self.session=session;self.production_run=run
    def run(self):
        try:
            QualityController(self.session).inspect(self.production_run)
            self.result.emit()
        except Exception as exc:self.failed.emit(str(exc))


class ProductionDashboard(QWidget):
    navigate = Signal(str)
    updated = Signal()
    def __init__(self, parent=None):
        super().__init__(parent)
        self.session=None; self.profile=None; self.run_id=None; self.worker=None; self.orchestrator=None
        self.summary=QLabel('Open a project to inspect production readiness.')
        self.summary.setWordWrap(True)
        self.profile_selector=QComboBox()
        self.profile_selector.currentIndexChanged.connect(self._select_profile)
        self.progress=QLabel('Not started')
        self.costs=QLabel('Known and unknown usage will appear here.')
        self.plan=QListWidget()
        self.generate=QPushButton('Generate Complete Video')
        self.preview=QPushButton('Preview Production Plan')
        self.start_button=QPushButton('Start')
        self.pause_button=QPushButton('Pause'); self.resume_button=QPushButton('Resume')
        self.cancel_button=QPushButton('Cancel'); self.review=QPushButton('Review Issues')
        self.retry_button=QPushButton('Retry Failed')
        self.resolve_button=QPushButton('Resolve Interrupted')
        self.open_output=QPushButton('Open Output'); self.history_button=QPushButton('View Production History')
        self.approve=QPushButton('Approve Checkpoint and Continue')
        row=QHBoxLayout()
        for button in (self.generate,self.preview,self.start_button,self.pause_button,self.resume_button,
                       self.cancel_button,self.retry_button,self.resolve_button,self.review,self.open_output,self.history_button,self.approve): row.addWidget(button)
        layout=QVBoxLayout(self); layout.addWidget(self.profile_selector);layout.addWidget(self.summary); layout.addWidget(self.progress);layout.addWidget(self.costs)
        layout.addLayout(row); layout.addWidget(self.plan)
        self.generate.clicked.connect(self.configure); self.preview.clicked.connect(self.show_plan)
        self.start_button.clicked.connect(self.start); self.pause_button.clicked.connect(self.pause)
        self.resume_button.clicked.connect(self.resume); self.cancel_button.clicked.connect(self.cancel)
        self.retry_button.clicked.connect(self.retry_failed)
        self.resolve_button.clicked.connect(self.resolve_interrupted)
        self.review.clicked.connect(lambda:self.navigate.emit('Quality Control'))
        self.open_output.clicked.connect(self._open_output); self.history_button.clicked.connect(self.history)
        self.approve.clicked.connect(self.approve_checkpoint)

    def bind(self, session):
        self.session=session; self.run_id=None; self.profile=None
        self.orchestrator=ProductionOrchestrator(session) if session else None
        if not session:self.profile_selector.clear()
        if session:
            self.orchestrator.store.recover()
            profiles=self.orchestrator.store.profiles()
            board_settings=session.database.storyboard_settings()
            self.profile=profiles[0] if profiles else ProductionProfile.preset('Illustrated Story',
                render_settings=VideoRenderStore(session.root).settings().model_copy(update={'mode':'final'}),
                narrative_settings=asdict(load_ai_settings()),
                shot_density=board_settings.density,visual_style=board_settings.visual_style)
            self._load_profiles()
            runs=self.orchestrator.store.runs()
            self.run_id=runs[0].identifier if runs else None
        self.refresh()

    def _load_profiles(self):
        self.profile_selector.blockSignals(True)
        self.profile_selector.clear()
        if self.session:
            for value in self.orchestrator.store.profiles():
                self.profile_selector.addItem(value.name,value.identifier)
            if self.profile:
                index=self.profile_selector.findData(self.profile.identifier)
                if index>=0:self.profile_selector.setCurrentIndex(index)
        self.profile_selector.blockSignals(False)

    def _select_profile(self, index):
        if not self.session or index<0:return
        identifier=self.profile_selector.itemData(index)
        self.profile=next((p for p in self.orchestrator.store.profiles() if p.identifier==identifier),self.profile)
        self.refresh()

    def refresh(self):
        enabled=self.session is not None
        for button in (self.generate,self.preview,self.start_button,self.pause_button,self.resume_button,
                       self.cancel_button,self.retry_button,self.resolve_button,self.review,self.open_output,self.history_button,self.approve):
            button.setEnabled(enabled)
        if not enabled:
            self.summary.setText('Open a project to inspect production readiness.'); self.plan.clear(); return
        report=self.orchestrator.dry_run(self.profile)
        run=self.orchestrator.store.run(self.run_id) if self.run_id else None
        shot_ids={t.shot_id for t in report['tasks'] if t.shot_id}
        approved_map=ImageStore(self.session.root).approved_map()
        video_store=VideoStore(self.session.root)
        approved_images=sum(sid in approved_map for sid in shot_ids)
        completed_animations=sum(bool(video_store.approved(sid)[0]) for sid in shot_ids)
        review_count=sum(i.status in {'Open','Needs Review'} for i in self.orchestrator.store.issues())
        self.summary.setText(f'{self.session.project.name} · {self.profile.name}\n'
            f'{report["scenes"]} scenes · {report["shots"]} shots · '
            f'{approved_images} approved images · {completed_animations} approved animations\n'
            f'{len(report["will_run"])} tasks to run · {len(report["will_reuse"])} reusable\n'
            f'{review_count} quality findings requiring review · '
            f'{"Ready" if report["ready"] else "; ".join(report["blocked"])}')
        if run:
            tasks=self.orchestrator.store.tasks(run.identifier)
            done=sum(t.status in {'Completed','Reused'} for t in tasks)
            self.progress.setText(f'{run.status} · {run.current_stage or "Preparing"} · {done}/{len(tasks)} tasks · '
                                  f'{sum(t.status=="Failed" for t in tasks)} failed · '
                                  f'{sum(t.status=="Awaiting Approval" for t in tasks)} checkpoints · '
                                  f'known cost {run.known_cost:.2f} · '
                                  f'{run.unknown_cost_count} unknown charges · '
                                  f'render {"complete" if run.output_path else "pending"}' +
                                  (f'\n{run.error}' if run.error else ''))
            usage=self.orchestrator.store.usage(run.identifier)
            categories=[]
            for category in ('language','image','video','review'):
                rows=[r for r in usage if r['category']==category and not r['estimated']]
                actual=sum(r['amount'] for r in rows if r['amount'] is not None)
                unknown=sum(r['amount'] is None for r in rows)
                categories.append(f'{category}: {actual:.2f} known, {unknown} unknown')
            self.costs.setText(' · '.join(categories))
        else:
            self.progress.setText('Not started')
            self.costs.setText('Known and unknown usage will appear here.')

    def configure(self):
        if not self.session:return
        dialog=ProductionSetupDialog(self.session,self.profile,self)
        if dialog.exec()!=QDialog.Accepted:return
        try:
            self.profile=dialog.value(); self.orchestrator.store.save_profile(self.profile)
            self._load_profiles();self.show_plan()
        except Exception as exc:QMessageBox.warning(self,'Production setup',str(exc))

    def show_plan(self):
        if not self.session:return
        report=self.orchestrator.dry_run(self.profile)
        self.plan.clear()
        for task, item in zip(report['tasks'], report['items']):
            self.plan.addItem(f'{"Reuse" if task.status=="Reused" else "Run"}: {task.kind} · {item["state"]}' +
                              (f' · {task.shot_id or task.scene_id}' if task.shot_id or task.scene_id else ''))
        for problem in report['blocked']: self.plan.addItem('Blocked: '+problem)
        for kind in report['unknown_cost_categories']: self.plan.addItem(f'Unknown {kind} generation cost')
        if report['provisional_workload']:
            self.plan.addItem('Workload is provisional until scene detection and storyboard planning finish')
        self.plan.addItem(f'{report["approved_assets"]} approved assets reusable · '
                          f'{report["review_tasks"]} tasks needing review · '
                          f'{len(report["selected_animation_shots"])} animation shots selected')
        self.plan.addItem('Output: '+report['output'])
        self.refresh()
        return report

    def start(self):
        if not self.session:return
        report=self.show_plan()
        if report['blocked']:
            QMessageBox.warning(self,'Production blocked','\n'.join(report['blocked']));return
        if self.profile.automation_mode=='Manual Production':
            QMessageBox.information(self,'Manual Production','Use the existing stage workspaces to process individual stages.');return
        text=(f'Run {len(report["will_run"])} tasks; reuse {len(report["will_reuse"])} tasks.\n'
              f'Estimated image cost: {report["estimated_image_cost"] if report["estimated_image_cost"] is not None else "unknown"}\n'
              f'Estimated video cost: {report["estimated_video_cost"] if report["estimated_video_cost"] is not None else "unknown"}\n'
              + ('Scene and shot counts are not known yet; later paid tasks will be added within your budget policy.\n'
                 if report['provisional_workload'] else '') +
              'Confirm the plan and authorize its paid work?')
        if QMessageBox.question(self,'Start Production',text)!=QMessageBox.Yes:return
        try:
            run=self.orchestrator.start(self.profile,paid_confirmed=True)
            self.run_id=run.identifier;self._launch()
        except Exception as exc:QMessageBox.warning(self,'Production',str(exc))

    def _launch(self):
        if self.worker and self.worker.isRunning():return
        self.worker=ProductionWorker(self.orchestrator,self.run_id,self)
        self.worker.progress.connect(lambda stage,done,total:self.progress.setText(f'{stage}: {done}/{total} tasks complete'))
        self.worker.result.connect(lambda _run:(self.refresh(),self.updated.emit()))
        self.worker.failed.connect(lambda message:QMessageBox.warning(self,'Production',message))
        self.worker.finished.connect(self.refresh)
        self.worker.start()

    def pause(self):
        if self.run_id:self.orchestrator.pause(self.run_id);self.refresh()
    def resume(self):
        if not self.run_id:return
        try:self.orchestrator.resume(self.run_id);self._launch()
        except Exception as exc:QMessageBox.warning(self,'Resume',str(exc))
    def cancel(self):
        if self.run_id:self.orchestrator.cancel(self.run_id);self.refresh()
    def retry_failed(self):
        if not self.run_id:return
        tasks=[t for t in self.orchestrator.store.tasks(self.run_id) if t.status in {'Failed','Blocked'}]
        if not tasks:return
        paid=any(t.kind in {'scenes','storyboard','entities','image_prompt','image','motion_prompt','video'} for t in tasks)
        if paid and QMessageBox.question(self,'Retry Failed Tasks',
            f'Retry {len(tasks)} failed tasks? Some requests may incur another charge.')!=QMessageBox.Yes:return
        try:
            self.orchestrator.retry_failed(self.run_id,paid_confirmed=paid)
            self.orchestrator.resume(self.run_id)
            self._launch()
        except Exception as exc:QMessageBox.warning(self,'Retry',str(exc))
    def resolve_interrupted(self):
        if not self.run_id:return
        task=next((t for t in self.orchestrator.store.tasks(self.run_id) if t.status=='Interrupted'),None)
        if not task:return
        paid=task.kind in {'scenes','storyboard','entities','image_prompt','image','motion_prompt','video'}
        if paid and QMessageBox.question(self,'Resolve Interrupted Request',
            'Check the provider job first. Confirm this request did not complete or incur a charge before retrying?')!=QMessageBox.Yes:return
        try:
            self.orchestrator.resolve_interrupted(self.run_id,task.identifier,confirmed_no_charge=paid)
            self.orchestrator.resume(self.run_id);self._launch()
        except Exception as exc:QMessageBox.warning(self,'Recovery',str(exc))
    def approve_checkpoint(self):
        if not self.run_id:return
        task=next((t for t in self.orchestrator.store.tasks(self.run_id) if t.status=='Awaiting Approval'),None)
        if not task:return
        try:self.orchestrator.approve_checkpoint(self.run_id,task.identifier);self._launch()
        except Exception as exc:QMessageBox.warning(self,'Approval',str(exc))
    def history(self):
        if not self.session:return
        self.plan.clear()
        for run in self.orchestrator.store.runs():
            tasks=self.orchestrator.store.tasks(run.identifier)
            issues=[i for i in self.orchestrator.store.issues() if i.run_id==run.identifier]
            self.plan.addItem(f'{run.started_at:%Y-%m-%d %H:%M} · {run.profile.name} · {run.status} · '
                f'{sum(t.kind=="storyboard" for t in tasks)} scenes · '
                f'{sum(t.kind=="image" and t.status=="Completed" for t in tasks)} images generated · '
                f'{sum(t.kind=="video" and t.status=="Completed" for t in tasks)} animations generated · '
                f'{len(issues)} QC findings · known cost {run.known_cost:.2f} · '
                f'{run.output_path or "no output"} · {run.manifest_path or "no manifest"}')
    def _open_output(self):
        if not self.run_id:return
        run=self.orchestrator.store.run(self.run_id)
        if run and run.output_path:
            path=Path(run.output_path)
            if not path.is_absolute():path=self.session.root/path
            if path.is_file():QDesktopServices.openUrl(QUrl.fromLocalFile(str(path.parent)))


class QualityControlPanel(QWidget):
    navigate=Signal(object)
    def __init__(self,parent=None):
        super().__init__(parent)
        self.session=None
        self.worker=None
        self.summary=QLabel('Open a project to inspect quality issues.')
        self.issues=QListWidget()
        self.severity=QComboBox();self.severity.addItems(['All severities','Critical','Error','Warning','Suggestion'])
        self.category=QComboBox();self.category.addItems(['All categories','Narrative','Timing','Visual Continuity','Image','Animation','Render'])
        self.status=QComboBox();self.status.addItems(['All statuses','Open','Needs Review','Accepted','Resolved','Dismissed'])
        self.scene=QLineEdit();self.scene.setPlaceholderText('Scene ID filter')
        self.check=QPushButton('Run Technical Checks');self.resolve=QPushButton('Resolve Selected')
        self.open_editor=QPushButton('Open Relevant Editor')
        row=QHBoxLayout();row.addWidget(self.check);row.addWidget(self.resolve);row.addWidget(self.open_editor)
        filters=QHBoxLayout()
        for widget in (self.severity,self.category,self.status,self.scene):filters.addWidget(widget)
        layout=QVBoxLayout(self);layout.addWidget(self.summary);layout.addLayout(filters);layout.addLayout(row);layout.addWidget(self.issues)
        self.check.clicked.connect(self.inspect);self.resolve.clicked.connect(self._resolve)
        self.open_editor.clicked.connect(self._open_editor)
        for widget in (self.severity,self.category,self.status):widget.currentIndexChanged.connect(lambda _index:self.refresh())
        self.scene.editingFinished.connect(self.refresh)
    def bind(self,session):self.session=session;self.refresh()
    def refresh(self):
        self.issues.clear()
        if not self.session:return
        records=ProductionStore(self.session.root).issues(
            severity=self.severity.currentText() if self.severity.currentIndex() else None,
            category=self.category.currentText() if self.category.currentIndex() else None,
            status=self.status.currentText() if self.status.currentIndex() else None,
            scene_id=self.scene.text().strip() or None)
        self.summary.setText(f'{len(records)} persistent quality findings; AI suggestions require human review.')
        for issue in records:
            item=f'{issue.severity} · {issue.category} · {issue.status}: {issue.description}'
            self.issues.addItem(item);self.issues.item(self.issues.count()-1).setData(Qt.UserRole,issue.identifier)
    def inspect(self):
        if not self.session:return
        if self.worker and self.worker.isRunning():return
        store=ProductionStore(self.session.root);runs=store.runs()
        self.worker=QualityWorker(self.session,runs[0] if runs else None,self)
        self.worker.result.connect(self.refresh)
        self.worker.failed.connect(lambda message:QMessageBox.warning(self,'Quality Control',message))
        self.worker.start()
    def _selected(self):
        if not self.session or not self.issues.currentItem():return None
        identifier=self.issues.currentItem().data(Qt.UserRole)
        return next((i for i in ProductionStore(self.session.root).issues() if i.identifier==identifier),None)
    def _resolve(self):
        issue=self._selected()
        if not issue:return
        from datetime import datetime,timezone
        notes, accepted=QInputDialog.getText(self,'Resolve issue','Resolution notes:')
        if not accepted:return
        issue.status='Resolved';issue.resolved_at=datetime.now(timezone.utc)
        issue.resolution_notes=notes.strip()
        ProductionStore(self.session.root).save_issue(issue);self.refresh()
    def _open_editor(self):
        issue=self._selected()
        if issue:self.navigate.emit(issue)
