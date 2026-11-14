"""Embed the original chapter editor using the host's project and source files."""
from dataclasses import replace
from pathlib import Path

from PySide6.QtCore import Qt, QTimer, QUrl
from podcast_splitter.app import Window
from podcast_splitter.core import Project, parse_prose
from prose_to_video.storage.project_files import import_audio, import_prose


class PodcastPanel(Window):
    def __init__(self, host):
        self.host = host
        self.session = None
        super().__init__()
        self.setParent(host.workspace_tabs)
        self.setWindowFlags(Qt.Widget)
        # Project commands belong to the application menu, with one save target.
        for action in self.file_menu.actions():
            action.setShortcut('')
        self.menuBar().hide()
        self.model.currentIndexChanged.connect(self.mark_dirty)
        self.format.currentIndexChanged.connect(self.mark_dirty)
        self.autosave = QTimer(self)
        self.autosave.setInterval(1000)
        self.autosave.timeout.connect(self.autosave_changes)
        self.autosave.start()
        self.setEnabled(False)

    def mark_dirty(self, *_):
        if self.sender() is self.model:
            self.project.model = self.model.currentData()
        elif self.sender() is self.format:
            self.project.export_format = self.format.currentText()
        self.dirty = True

    def autosave_changes(self):
        if self.dirty and self.session and not self.process:
            try:
                self.save_shared()
            except (OSError, ValueError) as exc:
                self.message.setText(f'Changes are not saved: {exc}. Retry with File → Save Project.')

    def bind(self, session):
        """Load relative state against the current project, including moved folders."""
        path = session.root / 'podcast-splitter.json'
        project = Project.load(str(path)) if path.exists() else Project()
        for key in ('prose', 'audio', 'output'):
            value = getattr(project, key)
            if value and not Path(value).is_absolute():
                setattr(project, key, str(session.root / value))
        self.cancel_preview_render()
        self.player.stop()
        self.playback_mode = self.preview_offset = self.play_until = None
        self.session, self.project = session, project
        self.project_path = str(path)
        if not project.output:
            project.output = str(session.root / 'output' / 'chapters')
        self.sync_sources(session)
        self.player.setSource(QUrl.fromLocalFile(self.project.audio))
        self.refresh()
        self.format.setCurrentText(project.export_format)
        self.dirty = False
        self.setEnabled(True)

    def sync_sources(self, session, changed=None):
        self.session = session
        prose = session.project.resolved_prose(session.root)
        audio = session.project.resolved_audio(session.root)
        if prose and (str(prose) != self.project.prose or changed == 'prose'):
            # Plain prose remains usable by the video workflow without headings.
            try:
                self.project.sections = parse_prose(str(prose))
            except ValueError:
                self.project.sections = []
            self.project.prose = str(prose)
            self.project.prose_fingerprint = ''
            self.dirty = True
        if audio and (str(audio) != self.project.audio or changed == 'audio'):
            self.project.audio = ''  # Force reset when replacing a file in place.
            super().choose_audio(str(audio))
        self.refresh()

    def choose_source(self, path, kind):
        def work():
            self.host.require()
            if self.process:
                raise ValueError('Cancel the audio task before replacing sources')
            session = (import_prose if kind == 'prose' else import_audio)(self.host.session, Path(path))
            self.host.source_imported(session, kind)
            self.save_shared()
        self.host.guarded(work)

    def choose_prose(self, path):
        self.choose_source(path, 'prose')

    def choose_audio(self, path):
        self.choose_source(path, 'audio')

    def start_worker(self, action, args):
        try:
            self.host.require()
        except (ValueError, OSError) as exc:
            self.error(exc)
            return
        super().start_worker(action, args)

    def save_shared(self, *, force=False):
        if not self.session:
            return
        if self.process:
            raise ValueError('Wait for the audio task or cancel it before saving or switching projects')
        if not self.dirty and not force:
            return
        self.project.model = self.model.currentData()
        self.project.export_format = self.format.currentText()
        stored = replace(self.project)
        for key in ('prose', 'audio', 'output'):
            value = getattr(stored, key)
            if value and Path(value).is_relative_to(self.session.root):
                setattr(stored, key, Path(value).relative_to(self.session.root).as_posix())
        stored.save(str(self.session.root / 'podcast-splitter.json'))
        self.dirty = False

    def save_project(self, save_as=False):
        self.host.save_project()
        return not self.dirty

    def open_project(self):
        self.host.open_project()

    def shutdown(self):
        self.autosave.stop()
        self.cancel_preview_render()
        self.cutoff_timer.stop()
        self.player.stop()
        self.preview_files.cleanup()
