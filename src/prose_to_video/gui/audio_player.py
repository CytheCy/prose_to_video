"""Qt multimedia playback controls; metadata inspection lives outside Qt."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, QUrl, Signal
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtWidgets import (QHBoxLayout, QLabel, QPushButton, QSlider,
                               QVBoxLayout, QWidget)

from prose_to_video.utils.media_info import format_duration


class AudioPlayer(QWidget):
    playback_error = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.output = QAudioOutput(self)
        self.player = QMediaPlayer(self)
        self.player.setAudioOutput(self.output)
        self._loaded_path: Path | None = None
        self._duration_ms = 0
        self.play_button = QPushButton("Play")
        self.pause_button = QPushButton("Pause")
        self.stop_button = QPushButton("Stop")
        self.seek = QSlider(Qt.Horizontal)
        self.seek.setRange(0, 0)
        self.position_label = QLabel("00:00:00")
        self.duration_label = QLabel("00:00:00")
        self.volume = QSlider(Qt.Horizontal)
        self.volume.setRange(0, 100)
        self.volume.setValue(80)
        self.volume.setMaximumWidth(120)
        self.output.setVolume(0.8)
        self.play_button.clicked.connect(self.player.play)
        self.pause_button.clicked.connect(self.player.pause)
        self.stop_button.clicked.connect(self.player.stop)
        self.seek.sliderMoved.connect(self._seek)
        self.volume.valueChanged.connect(lambda value: self.output.setVolume(value / 100))
        self.player.positionChanged.connect(self._position_changed)
        self.player.durationChanged.connect(self._duration_changed)
        self.player.errorOccurred.connect(self._error)
        row = QHBoxLayout()
        for widget in (self.play_button, self.pause_button, self.stop_button,
                       self.position_label, self.seek, self.duration_label,
                       QLabel("Volume"), self.volume):
            row.addWidget(widget)
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("Audio playback"))
        layout.addLayout(row)
        self.set_audio(None)

    def set_audio(self, path: Path | None, known_duration: float | None = None) -> None:
        self.player.stop()
        self._loaded_path = path
        self._duration_ms = int(known_duration * 1000) if known_duration is not None else 0
        self.seek.setRange(0, self._duration_ms)
        self.seek.setValue(0)
        self.position_label.setText("00:00:00")
        self.duration_label.setText(format_duration(known_duration) if path else "00:00:00")
        self.player.setSource(QUrl.fromLocalFile(str(path)) if path else QUrl())
        for widget in (self.play_button, self.pause_button, self.stop_button, self.seek):
            widget.setEnabled(path is not None)

    def _seek(self, value: int) -> None:
        self.player.setPosition(value)
        self.position_label.setText(format_duration(value / 1000))

    def _position_changed(self, value: int) -> None:
        if not self.seek.isSliderDown():
            self.seek.setValue(value)
            self.position_label.setText(format_duration(value / 1000))

    def _duration_changed(self, value: int) -> None:
        if value > 0:
            self._duration_ms = value
            self.seek.setMaximum(value)
            self.duration_label.setText(format_duration(value / 1000))

    def _error(self, error: QMediaPlayer.Error, message: str) -> None:
        if error != QMediaPlayer.NoError and self._loaded_path is not None:
            self.playback_error.emit(message or "Qt could not play this audio codec or file.")
