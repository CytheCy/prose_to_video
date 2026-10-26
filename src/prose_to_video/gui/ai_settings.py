"""Application-wide AI preferences; credentials stay in the environment."""

from __future__ import annotations

from PySide6.QtCore import QSettings
from PySide6.QtWidgets import (QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox,
                               QFormLayout, QLabel, QLineEdit, QMessageBox, QSpinBox,
                               QVBoxLayout)

from prose_to_video.pipeline.providers import AISettings


def load_ai_settings() -> AISettings:
    store = QSettings("ProseToVideo", "ProseToVideo")
    defaults = AISettings()
    temperature = store.value("ai/temperature", "default")
    return AISettings(
        provider=str(store.value("ai/provider", defaults.provider)),
        model=str(store.value("ai/model", defaults.model)),
        credential_env=str(store.value("ai/credential_env", defaults.credential_env)),
        temperature=None if temperature == "default" else float(temperature),
        max_output_tokens=int(store.value("ai/max_output_tokens", defaults.max_output_tokens)),
        context_tokens=int(store.value("ai/context_tokens", defaults.context_tokens)),
        chunk_chars=int(store.value("ai/chunk_chars", defaults.chunk_chars)),
        overlap_chars=int(store.value("ai/overlap_chars", defaults.overlap_chars)),
    )


def save_ai_settings(settings: AISettings) -> None:
    settings.validate()
    store = QSettings("ProseToVideo", "ProseToVideo")
    for key in ("provider", "model", "credential_env", "max_output_tokens", "context_tokens", "chunk_chars", "overlap_chars"):
        store.setValue(f"ai/{key}", getattr(settings, key))
    store.setValue("ai/temperature", "default" if settings.temperature is None else settings.temperature)


class AISettingsDialog(QDialog):
    def __init__(self, settings: AISettings, parent=None):
        super().__init__(parent)
        self.setWindowTitle("AI Scene Detection Settings")
        self.provider = QComboBox()
        self.provider.addItem("OpenAI")
        self.provider.setCurrentText(settings.provider)
        self.model = QLineEdit(settings.model)
        self.credential_env = QLineEdit(settings.credential_env)
        self.temperature = QDoubleSpinBox()
        self.temperature.setRange(-1, 2)
        self.temperature.setSingleStep(0.1)
        self.temperature.setSpecialValueText("Model default")
        self.temperature.setValue(settings.temperature if settings.temperature is not None else -1)
        self.max_output = QSpinBox()
        self.max_output.setRange(256, 100000)
        self.max_output.setValue(settings.max_output_tokens)
        self.context_tokens = QSpinBox()
        self.context_tokens.setRange(4000, 2000000)
        self.context_tokens.setValue(settings.context_tokens)
        self.chunk_chars = QSpinBox()
        self.chunk_chars.setRange(1000, 1000000)
        self.chunk_chars.setValue(settings.chunk_chars)
        self.overlap_chars = QSpinBox()
        self.overlap_chars.setRange(0, 499999)
        self.overlap_chars.setValue(settings.overlap_chars)
        form = QFormLayout()
        for label, widget in (("Provider:", self.provider), ("Model:", self.model),
                              ("Credential environment variable:", self.credential_env),
                              ("Temperature:", self.temperature), ("Max output tokens:", self.max_output),
                              ("Model context tokens:", self.context_tokens),
                              ("Chunk characters:", self.chunk_chars), ("Overlap characters:", self.overlap_chars)):
            form.addRow(label, widget)
        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("Set the credential in your shell environment before launching the app. It is never saved here."))
        layout.addLayout(form)
        layout.addWidget(buttons)

    def values(self) -> AISettings:
        return AISettings(provider=self.provider.currentText(), model=self.model.text().strip(),
                          credential_env=self.credential_env.text().strip(),
                          temperature=None if self.temperature.value() < 0 else self.temperature.value(),
                          max_output_tokens=self.max_output.value(), context_tokens=self.context_tokens.value(),
                          chunk_chars=self.chunk_chars.value(),
                          overlap_chars=self.overlap_chars.value())

    def _accept(self) -> None:
        try:
            self.values().validate()
        except ValueError as exc:
            QMessageBox.warning(self, "Invalid AI settings", str(exc))
            return
        self.accept()
