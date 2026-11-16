"""The shared sound uses real audio, with a system-bell fallback."""
import os
import subprocess
import sys


def test_completion_sound_and_podcast_outcomes(tmp_path):
    result = subprocess.run([sys.executable, '-c', r'''
import tempfile
import wave
from pathlib import Path
from PySide6.QtCore import QProcess
from PySide6.QtWidgets import QApplication
from podcast_splitter import notifications
from podcast_splitter import app as gui

app = QApplication([])
plays = []
beeps = []
QApplication.beep = lambda: beeps.append(True)

class Effect:
    class Status:
        Error = 'error'
    def __init__(self, parent):
        self.statusChanged = self
        self.state = 'loading'
    def connect(self, callback): self.callback = callback
    def setVolume(self, value): assert value > 0
    def setLoopCount(self, count): assert count == 1
    def setSource(self, url):
        with wave.open(url.toLocalFile()) as sound:
            assert sound.getnframes() / sound.getframerate() > .4
    def status(self): return self.state
    def stop(self): pass
    def play(self): plays.append(True)

notifications.QSoundEffect = Effect
notifications.completion_sound()
player = app._completion_sound
notifications.completion_sound()
assert app._completion_sound is player
assert len(plays) == 2
player.effect.state = Effect.Status.Error
player.effect.callback()
assert len(beeps) == 1
notifications.completion_sound()
assert len(beeps) == 2

window = gui.Window()
sounds = []
errors = []
gui.completion_sound = lambda: sounds.append(True)
window.error = errors.append
for action in ('analyze', 'export'):
    for outcome in ('success', 'failed', 'canceled', 'crashed', 'failed_to_start'):
        sounds.clear()
        window.temp = tempfile.TemporaryDirectory()
        window.work_path = str(Path(window.temp.name) / 'project.json')
        window.project.save(window.work_path)
        window.action = action
        window.buffer = b''
        window.stderr = ''
        window.worker_done = outcome == 'success'
        window.failed = False
        window.cancelled = outcome == 'canceled'
        window.process = QProcess(window)
        if outcome == 'failed_to_start':
            window.process_error(QProcess.ProcessError.FailedToStart)
        else:
            window.worker_finished(0 if outcome == 'success' else 1,
                QProcess.ExitStatus.CrashExit if outcome == 'crashed' else QProcess.ExitStatus.NormalExit)
        assert window.process is None, (action, outcome)
        assert sounds == [True], (action, outcome)
        window.worker_finished(1, QProcess.ExitStatus.CrashExit)
        assert sounds == [True], 'Duplicate finish must not chime twice'
window.dirty = False
window.close()
'''], capture_output=True, text=True, timeout=30,
        env={**os.environ, 'QT_QPA_PLATFORM': 'offscreen',
             'XDG_CONFIG_HOME': str(tmp_path / 'config')})
    assert result.returncode == 0, result.stderr
