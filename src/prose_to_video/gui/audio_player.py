"""Qt multimedia playback controls; metadata inspection lives outside Qt."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, QUrl, Signal
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtWidgets import (QHBoxLayout, QLabel, QPushButton, QSlider,
                               QVBoxLayout, QWidget)

from prose_to_video.utils.media_info import format_duration
from prose_to_video.gui.waveform import Waveform


class AudioPlayer(QWidget):
    playback_error = Signal(str)
    position_seconds = Signal(float)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.output = QAudioOutput(self)
        self.player = QMediaPlayer(self)
        self.player.setAudioOutput(self.output)
        self._loaded_path: Path | None = None
        self._duration_ms = 0
        self._play_until_ms: int | None = None
        self.waveform = Waveform()
        self.waveform.seek_requested.connect(self.seek_to)
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
        layout.addWidget(self.waveform)
        layout.addLayout(row)
        self.set_audio(None)

    def set_audio(self, path: Path | None, known_duration: float | None = None) -> None:
        self.player.stop()
        self._play_until_ms = None
        self._loaded_path = path
        self._duration_ms = int(known_duration * 1000) if known_duration is not None else 0
        self.seek.setRange(0, self._duration_ms)
        self.seek.setValue(0)
        self.position_label.setText("00:00:00")
        self.duration_label.setText(format_duration(known_duration) if path else "00:00:00")
        self.player.setSource(QUrl.fromLocalFile(str(path)) if path else QUrl())
        self.waveform.set_data([], known_duration or 0)
        self.waveform.set_timings([])
        for widget in (self.play_button, self.pause_button, self.stop_button, self.seek):
            widget.setEnabled(path is not None)

    def _seek(self, value: int) -> None:
        self._play_until_ms = None
        self.player.setPosition(value)
        self.position_label.setText(format_duration(value / 1000))

    def _position_changed(self, value: int) -> None:
        if self._play_until_ms is not None and value >= self._play_until_ms:
            self.player.pause()
            self._play_until_ms = None
        self.waveform.set_position(value / 1000)
        self.position_seconds.emit(value / 1000)
        if not self.seek.isSliderDown():
            self.seek.setValue(value)
            self.position_label.setText(format_duration(value / 1000))

    def _duration_changed(self, value: int) -> None:
        if value > 0:
            self._duration_ms = value
            self.seek.setMaximum(value)
            self.duration_label.setText(format_duration(value / 1000))
            self.waveform.duration = value / 1000
            self.waveform.update()

    def seek_to(self, seconds: float) -> None:
        self._play_until_ms = None
        self.player.setPosition(int(seconds * 1000))

    def play_range(self, start: float, end: float) -> None:
        if self._loaded_path is None or not 0 <= start < end:
            raise ValueError("No timed audio range is available")
        self._play_until_ms = int(end * 1000)
        self.player.setPosition(int(start * 1000))
        self.player.play()

    def _error(self, error: QMediaPlayer.Error, message: str) -> None:
        if error != QMediaPlayer.NoError and self._loaded_path is not None:
            self.playback_error.emit(message or "Qt could not play this audio codec or file.")
