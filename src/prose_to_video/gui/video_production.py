"""Video Production controls, render settings, and streamed MP4 preview."""
from __future__ import annotations

from pathlib import Path
from threading import Event

from PySide6.QtCore import QThread, Qt, QUrl, Signal
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtMultimediaWidgets import QVideoWidget
from PySide6.QtWidgets import (QComboBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout,
    QGridLayout, QHBoxLayout, QLabel, QLineEdit, QListWidget, QPushButton, QSlider,
    QSpinBox, QDoubleSpinBox, QVBoxLayout, QWidget, QCheckBox)

from prose_to_video.models.video_render import RenderSettings, ShotRenderOverride
from prose_to_video.models.shot import CAMERA_MOVEMENTS
from prose_to_video.pipeline.video_rendering import run_render
from prose_to_video.utils.media_info import format_duration


class RenderSettingsDialog(QDialog):
    def __init__(self, settings: RenderSettings, parent=None):
        super().__init__(parent)
        self.setWindowTitle('Video Render Settings')
        form = QFormLayout(self)
        self.width = QSpinBox(); self.width.setRange(16, 8192); self.width.setSingleStep(2); self.width.setValue(settings.width)
        self.height = QSpinBox(); self.height.setRange(16, 8192); self.height.setSingleStep(2); self.height.setValue(settings.height)
        self.fps = QSpinBox(); self.fps.setRange(1, 120); self.fps.setValue(settings.frame_rate)
        self.codec = QComboBox(); self.codec.addItem('H.264 (libx264)')
        self.crf = QSpinBox(); self.crf.setRange(0, 51); self.crf.setValue(settings.crf)
        self.preset = QComboBox(); self.preset.addItems(['ultrafast','superfast','veryfast','faster','fast','medium','slow']); self.preset.setCurrentText(settings.preset)
        self.fit = QComboBox(); self.fit.addItems(['fit','crop']); self.fit.setCurrentText(settings.fit_policy)
        self.background = QLineEdit(settings.background_color)
        self.motion = QComboBox(); self.motion.addItems(['None','Subtle','Moderate','Strong','Custom']); self.motion.setCurrentText(settings.motion_intensity)
        self.custom_motion = QDoubleSpinBox(); self.custom_motion.setRange(0, .3); self.custom_motion.setSingleStep(.01); self.custom_motion.setDecimals(2); self.custom_motion.setValue(settings.custom_motion)
        self.transition = QComboBox(); self.transition.addItems(['Cut','Crossfade','Fade through black']); self.transition.setCurrentText(settings.default_transition)
        self.transition_seconds = QDoubleSpinBox(); self.transition_seconds.setRange(0, 10); self.transition_seconds.setSingleStep(.1); self.transition_seconds.setValue(settings.crossfade_duration)
        self.mode = QComboBox(); self.mode.addItems(['final','draft']); self.mode.setCurrentText(settings.mode)
        self.directory = QLineEdit(settings.output_directory)
        self.filename = QLineEdit(settings.output_filename)
        self.pause = QComboBox(); self.pause.addItems(['storyboard','previous','next']); self.pause.setCurrentText(settings.pause_policy)
        for label, control in [('Width',self.width),('Height',self.height),('Frame rate',self.fps),
            ('Video codec',self.codec),('Quality (CRF; lower is better)',self.crf),('Encoder preset',self.preset),
            ('Image policy',self.fit),('Letterbox color',self.background),('Motion intensity',self.motion),('Custom motion',self.custom_motion),
            ('Default transition',self.transition),('Transition seconds',self.transition_seconds),
            ('Render mode',self.mode),('Pause coverage',self.pause),('Output directory',self.directory),
            ('Output filename',self.filename)]: form.addRow(label,control)
        buttons=QDialogButtonBox(QDialogButtonBox.Ok|QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept);buttons.rejected.connect(self.reject);form.addRow(buttons)

    def value(self) -> RenderSettings:
        return RenderSettings(width=self.width.value(),height=self.height.value(),frame_rate=self.fps.value(),
            crf=self.crf.value(),preset=self.preset.currentText(),fit_policy=self.fit.currentText(),
            background_color=self.background.text().strip(),
            motion_intensity=self.motion.currentText(),custom_motion=self.custom_motion.value(),
            default_transition=self.transition.currentText(),crossfade_duration=self.transition_seconds.value(),
            mode=self.mode.currentText(),pause_policy=self.pause.currentText(),
            output_directory=self.directory.text().strip(),output_filename=self.filename.text().strip())


class RenderTask(QThread):
    progress = Signal(str, float)
    finished_job = Signal(object)
    def __init__(self, session, plan, path, accept_warnings, parent=None):
        super().__init__(parent)
        self.session=session;self.plan=plan;self.path=path;self.accept_warnings=accept_warnings
        self.cancel_event=Event()
    def run(self):
        job=run_render(self.session,self.plan,self.path,cancel=self.cancel_event,
                       progress=lambda stage,seconds:self.progress.emit(stage,seconds),
                       accept_warnings=self.accept_warnings)
        self.finished_job.emit(job)


class VideoProductionPanel(QWidget):
    validate_requested=Signal()
    settings_requested=Signal()
    render_requested=Signal(str)
    cancel_requested=Signal()
    open_folder_requested=Signal()
    history_requested=Signal()
    clear_cache_requested=Signal()
    shot_override_requested=Signal(object)
    shot_mode_requested=Signal(str)
    shot_coverage_requested=Signal(str,bool)

    def __init__(self,parent=None):
        super().__init__(parent)
        layout=QVBoxLayout(self)
        self.summary=QLabel('Open a project to inspect video readiness.');self.summary.setWordWrap(True)
        self.status=QLabel('Not validated');self.status.setWordWrap(True)
        layout.addWidget(self.summary);layout.addWidget(self.status)
        row=QGridLayout()
        buttons=[('Validate Timeline',lambda:self.validate_requested.emit()),
            ('Render Settings…',lambda:self.settings_requested.emit()),
            ('Render Selected Shot',lambda:self.render_requested.emit('shot')),
            ('Render Selected Scene',lambda:self.render_requested.emit('scene')),
            ('Render Full Video',lambda:self.render_requested.emit('full')),
            ('Render Draft Preview',lambda:self.render_requested.emit('draft')),
            ('Open Output Folder',lambda:self.open_folder_requested.emit()),
            ('View Render History',lambda:self.history_requested.emit()),
            ('Clear Render Cache',lambda:self.clear_cache_requested.emit())]
        self.buttons=[]
        for i,(title,callback) in enumerate(buttons):
            button=QPushButton(title);button.clicked.connect(callback);row.addWidget(button,i//3,i%3);self.buttons.append(button)
        layout.addLayout(row)
        self.shot_label=QLabel('Select a storyboard shot to edit render-only motion.')
        layout.addWidget(self.shot_label)
        self.animation_state=QLabel('Production mode: —');layout.addWidget(self.animation_state)
        self.shot_statuses=QListWidget();self.shot_statuses.setMaximumHeight(110)
        layout.addWidget(self.shot_statuses)
        motion_form=QFormLayout()
        self.production_mode=QComboBox();self.production_mode.addItems(['Static Image','Camera Motion','AI Animation'])
        self.production_mode.currentTextChanged.connect(lambda value:self.shot_mode_requested.emit(value) if self.save_shot_button.isEnabled() else None)
        motion_form.addRow('Shot production mode',self.production_mode)
        self.duration_strategy=QComboBox();self.duration_strategy.addItems(['exact','still_tail','camera_tail','sequence'])
        self.draft_still_fallback=QCheckBox('Use approved still if AI clip is missing in draft preview')
        self.save_coverage=QPushButton('Save Animation Coverage')
        self.save_coverage.clicked.connect(lambda:self.shot_coverage_requested.emit(self.duration_strategy.currentText(),self.draft_still_fallback.isChecked()))
        motion_form.addRow('Duration strategy',self.duration_strategy)
        motion_form.addRow(self.draft_still_fallback)
        motion_form.addRow(self.save_coverage)
        self.shot_motion=QComboBox();self.shot_motion.addItems(['Use storyboard',*CAMERA_MOVEMENTS])
        self.shot_transition=QComboBox();self.shot_transition.addItems(['Use default','Cut','Crossfade','Fade through black','Fade in','Fade out'])
        self.shot_transition_duration=QDoubleSpinBox();self.shot_transition_duration.setRange(0,10);self.shot_transition_duration.setSingleStep(.1)
        self.shot_transition_duration.setSpecialValueText('Use default')
        motion_form.addRow('Shot camera movement',self.shot_motion)
        motion_form.addRow('Shot transition',self.shot_transition)
        motion_form.addRow('Shot transition seconds',self.shot_transition_duration)
        self.camera_values={}
        for name,initial in [('start_x',.5),('start_y',.5),('end_x',.5),('end_y',.5),('start_zoom',1),('end_zoom',1)]:
            spin=QDoubleSpinBox();spin.setDecimals(3);spin.setSingleStep(.01)
            spin.setRange(1,1.3) if 'zoom' in name else spin.setRange(0,1)
            spin.setValue(initial);self.camera_values[name]=spin
            motion_form.addRow(name.replace('_',' ').capitalize(),spin)
        self.custom_framing=QComboBox();self.custom_framing.addItems(['Automatic framing','Approved custom framing'])
        motion_form.addRow('Framing',self.custom_framing)
        self.save_shot_button=QPushButton('Save Shot Render Settings')
        self.save_shot_button.clicked.connect(lambda:self.shot_override_requested.emit(self.shot_override()))
        motion_form.addRow(self.save_shot_button)
        layout.addLayout(motion_form)
        self.cancel_button=QPushButton('Cancel Render');self.cancel_button.clicked.connect(self.cancel_requested.emit)
        self.cancel_button.setEnabled(False);layout.addWidget(self.cancel_button)
        self.video=QVideoWidget();self.video.setMinimumHeight(220);layout.addWidget(self.video,1)
        self.audio=QAudioOutput(self);self.player=QMediaPlayer(self);self.player.setAudioOutput(self.audio);self.player.setVideoOutput(self.video)
        self.audio.setVolume(.8)
        controls=QHBoxLayout()
        self.play=QPushButton('Play');self.pause=QPushButton('Pause');self.stop=QPushButton('Stop')
        self.seek=QSlider(Qt.Horizontal);self.seek.setRange(0,0)
        self.volume=QSlider(Qt.Horizontal);self.volume.setRange(0,100);self.volume.setValue(80)
        self.time=QLabel('00:00:00 / 00:00:00')
        self.play.clicked.connect(self.player.play);self.pause.clicked.connect(self.player.pause);self.stop.clicked.connect(self.player.stop)
        self.seek.sliderMoved.connect(self.player.setPosition)
        self.volume.valueChanged.connect(lambda v:self.audio.setVolume(v/100))
        self.player.durationChanged.connect(lambda ms:self.seek.setMaximum(ms))
        self.player.positionChanged.connect(self._position)
        for item in [self.play,self.pause,self.stop,self.seek,self.time,QLabel('Volume'),self.volume]:controls.addWidget(item)
        layout.addLayout(controls)
        self.history=QListWidget();self.history.setMaximumHeight(120);layout.addWidget(self.history)
        self.history.itemDoubleClicked.connect(lambda item:self.open_preview(Path(item.data(Qt.UserRole))))

    def _position(self, ms):
        if not self.seek.isSliderDown():self.seek.setValue(ms)
        self.time.setText(f'{format_duration(ms/1000)} / {format_duration(self.player.duration()/1000)}')

    def open_preview(self,path:Path):
        if path.is_file():
            self.player.stop();self.player.setSource(QUrl.fromLocalFile(str(path)));self.player.play()

    def set_busy(self,busy):
        for button in self.buttons:button.setEnabled(not busy)
        self.cancel_button.setEnabled(busy)

    def set_shot_override(self, shot, override: ShotRenderOverride | None):
        self.shot_label.setText(f'Scene {shot.scene_id} · Shot {shot.sequence}: {shot.title}' if shot else 'Select a storyboard shot to edit render-only motion.')
        self.save_shot_button.setEnabled(shot is not None)
        self.shot_motion.setCurrentText(override.camera_movement if override and override.camera_movement else 'Use storyboard')
        self.shot_transition.setCurrentText(override.transition if override and override.transition else 'Use default')
        self.shot_transition_duration.setValue(override.transition_duration if override and override.transition_duration else 0)
        self.custom_framing.setCurrentIndex(1 if override and override.camera_parameters else 0)
        for name,spin in self.camera_values.items():
            spin.setValue(override.camera_parameters[name] if override and override.camera_parameters else 1 if 'zoom' in name else .5)

    def set_production_mode(self, mode):
        self.production_mode.blockSignals(True)
        self.production_mode.setCurrentText(mode)
        self.production_mode.blockSignals(False)

    def set_coverage(self, coverage, draft_fallback):
        self.duration_strategy.setCurrentText(coverage.get('strategy','exact'))
        self.draft_still_fallback.setChecked(draft_fallback)

    def shot_override(self) -> ShotRenderOverride:
        return ShotRenderOverride(camera_movement=None if self.shot_motion.currentIndex()==0 else self.shot_motion.currentText(),
            transition=None if self.shot_transition.currentIndex()==0 else self.shot_transition.currentText(),
            transition_duration=self.shot_transition_duration.value() or None,
            camera_parameters={name:spin.value() for name,spin in self.camera_values.items()}
                if self.custom_framing.currentIndex()==1 else None)
