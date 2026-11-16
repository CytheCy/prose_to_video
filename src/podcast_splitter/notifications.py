"""Audible task completion, independent of the desktop's system bell setting."""
from pathlib import Path

from PySide6.QtCore import QObject, QUrl
from PySide6.QtMultimedia import QSoundEffect
from PySide6.QtWidgets import QApplication


class _CompletionSound(QObject):
    def __init__(self, app):
        super().__init__(app)
        self.effect = QSoundEffect(self)
        self.effect.setVolume(0.8)
        self.effect.setLoopCount(1)
        self.effect.statusChanged.connect(self._status_changed)
        self.pending = False
        self.effect.setSource(QUrl.fromLocalFile(str(Path(__file__).with_name('completion.wav'))))

    def _status_changed(self):
        if self.pending and self.effect.status() == QSoundEffect.Status.Error:
            self.pending = False
            QApplication.beep()

    def play(self):
        if self.effect.status() == QSoundEffect.Status.Error:
            QApplication.beep()
            return
        self.pending = True
        # QSoundEffect also queues playback while the WAV is loading.
        self.effect.stop()
        self.effect.play()


def completion_sound():
    """Play on the GUI thread; retain the player until application shutdown."""
    app = QApplication.instance()
    if app is None:
        return
    if not hasattr(app, '_completion_sound'):
        app._completion_sound = _CompletionSound(app)
    app._completion_sound.play()
