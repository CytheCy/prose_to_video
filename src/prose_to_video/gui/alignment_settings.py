"""Small local-model settings dialog; downloads require explicit confirmation."""
from PySide6.QtWidgets import (QComboBox, QDialog, QDialogButtonBox, QFormLayout,
                               QLabel, QSpinBox, QVBoxLayout)
from prose_to_video.pipeline.audio_transcription import TranscriptionSettings


class AlignmentSettingsDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Audio Alignment Settings")
        self.model = QComboBox()
        self.model.setEditable(True)
        self.model.addItems(["tiny", "base", "small", "medium", "large-v3"])
        self.device = QComboBox()
        self.device.addItems(["auto", "cpu", "cuda"])
        self.precision = QComboBox()
        self.precision.addItems(["auto", "int8", "float32", "float16"])
        self.batch = QSpinBox()
        self.batch.setRange(1, 32)
        self.batch.setValue(4)
        self.chunk = QSpinBox()
        self.chunk.setRange(30, 900)
        self.chunk.setValue(300)
        self.language = QComboBox()
        self.language.setEditable(True)
        self.language.addItems(["auto", "en", "es", "fr", "de"])
        form = QFormLayout()
        for label, widget in (("Whisper model", self.model), ("Device", self.device),
                              ("Precision", self.precision), ("Batch size", self.batch),
                              ("Chunk seconds", self.chunk), ("Language", self.language)):
            form.addRow(label, widget)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("Local WhisperX transcription and language alignment models may need a first-time download."))
        layout.addLayout(form)
        layout.addWidget(buttons)

    def values(self, allow_download: bool) -> TranscriptionSettings:
        settings = TranscriptionSettings(model=self.model.currentText().strip(),
             device=self.device.currentText(), precision=self.precision.currentText(),
             batch_size=self.batch.value(), chunk_seconds=self.chunk.value(),
             language=None if self.language.currentText() == "auto" else self.language.currentText().strip(),
             allow_download=allow_download)
        settings.validate()
        return settings
