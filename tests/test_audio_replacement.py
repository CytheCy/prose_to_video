"""Edited narration replaces the active source atomically and reloads playback."""
import os
import subprocess
import sys

import pytest

from prose_to_video.storage.database import ProjectDatabase, ProjectDatabaseError
from prose_to_video.storage.project_files import ProjectError, create_project, import_audio, open_project
from prose_to_video.utils.media_info import AudioInfo, AudioInspectionError


def test_replace_current_audio(tmp_path, monkeypatch):
    from prose_to_video.storage import project_files

    monkeypatch.setattr(project_files, 'inspect_audio', lambda path: AudioInfo(len(path.read_bytes()), 'pcm', 48000, 1))
    source = tmp_path / 'narration.wav'
    source.write_bytes(b'original')
    session = import_audio(create_project(tmp_path, 'Story'), source)
    source.write_bytes(b'edited narration')
    session = import_audio(session, source)
    assert (session.root / 'source/narration.wav').read_bytes() == b'edited narration'
    assert open_project(session.root).project.audio_duration == len(b'edited narration')
    assert sorted(p.name for p in (session.root / 'source').iterdir()) == ['narration.wav']


@pytest.mark.parametrize('failure', ['invalid_audio', 'database'])
def test_failed_audio_replacement_keeps_original(tmp_path, monkeypatch, failure):
    from prose_to_video.storage import project_files

    monkeypatch.setattr(project_files, 'inspect_audio', lambda path: AudioInfo(5, 'pcm', 48000, 1))
    source = tmp_path / 'narration.wav'
    source.write_bytes(b'original')
    session = import_audio(create_project(tmp_path, 'Story'), source)
    source.write_bytes(b'edited')
    if failure == 'invalid_audio':
        def fail(path):
            raise AudioInspectionError('Invalid audio')
        monkeypatch.setattr(project_files, 'inspect_audio', fail)
    else:
        monkeypatch.setattr(project_files, 'inspect_audio', lambda path: AudioInfo(7, 'pcm', 48000, 1))
        def fail(self, project):
            raise ProjectDatabaseError('Disk unavailable')
        monkeypatch.setattr(ProjectDatabase, 'save', fail)
    with pytest.raises(ProjectError):
        import_audio(session, source)
    assert (session.root / 'source/narration.wav').read_bytes() == b'original'
    assert open_project(session.root).project.audio_duration == 5
    assert sorted(p.name for p in (session.root / 'source').iterdir()) == ['narration.wav']


def test_replacement_does_not_overwrite_inactive_audio(tmp_path, monkeypatch):
    from prose_to_video.storage import project_files

    monkeypatch.setattr(project_files, 'inspect_audio', lambda path: AudioInfo(5, 'pcm', 48000, 1))
    source = tmp_path / 'narration.wav'
    source.write_bytes(b'original')
    session = import_audio(create_project(tmp_path, 'Story'), source)
    other = tmp_path / 'other.wav'
    other.write_bytes(b'other')
    session = import_audio(session, other)
    source.write_bytes(b'edited')
    with pytest.raises(ProjectError, match='already exists'):
        import_audio(session, source)
    assert (session.root / 'source/narration.wav').read_bytes() == b'original'
    assert open_project(session.root).project.audio_path == 'source/other.wav'


@pytest.mark.parametrize('immediate', [False, True])
def test_audio_replacement_reloads_player(tmp_path, immediate):
    script = r'''
import sys
import time
import wave
from pathlib import Path
from PySide6.QtWidgets import QApplication
from prose_to_video.gui import stills_window as gui
from prose_to_video.storage import project_files
from prose_to_video.utils.media_info import AudioInfo
from prose_to_video.models.scene import Scene

app = QApplication([])
root = Path(sys.argv[1])
def inspect(path):
    with wave.open(str(path), 'rb') as audio:
        return AudioInfo(audio.getnframes() / audio.getframerate(), 'pcm_s16le', audio.getframerate(), 1)
project_files.inspect_audio = inspect
if sys.argv[2] == 'True':
    class ImmediateTask(gui.Task):
        def start(self):
            self.run()
            self.finished.emit()
    gui.Task = ImmediateTask
window = gui.MainWindow()
window.load(project_files.create_project(root, 'Story'))
window.state.scenes = [Scene(sequence=1, start=0, end=1, text='A')]
original = window.state.scenes[0].model_dump()
errors = []
gui.QMessageBox.warning = lambda *args: errors.append(args[-1])
source = root / 'narration.wav'
gui.QFileDialog.getOpenFileName = lambda *args: (str(source), '')
loads = []
set_audio = window.audio.set_audio
def record_load(path, duration=None):
    loads.append((path, duration))
    set_audio(path, duration)
window.audio.set_audio = record_load
for duration in (1, 2):
    with wave.open(str(source), 'wb') as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(8000)
        audio.writeframes(b'\0\0' * 8000 * duration)
    loads.clear()
    window.import_source('audio')
    deadline = time.monotonic() + 5
    while window.worker is not None and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(.005)
    assert window.worker is None
    assert not errors, errors
    target = window.session.root / 'source/narration.wav'
    assert loads == [(None, None), (target, duration)], loads
    assert window.audio._loaded_path == target
    assert window.session.project.audio_duration == duration
    assert project_files.open_project(window.session.root).project.audio_duration == duration
    assert window.state.scenes[0].model_dump() == original
    assert 'build the timeline' in window.status.text()
assert window.close()
'''
    result = subprocess.run([sys.executable, '-c', script, str(tmp_path), str(immediate)],
                            capture_output=True, text=True, timeout=30,
                            env={**os.environ, 'QT_QPA_PLATFORM': 'offscreen',
                                 'XDG_CONFIG_HOME': str(tmp_path / 'config')})
    assert result.returncode == 0, result.stderr
