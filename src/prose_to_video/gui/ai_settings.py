"""Application-wide AI preferences; only credential locations are saved."""

from __future__ import annotations

from PySide6.QtCore import QSettings
from PySide6.QtWidgets import (QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox,
                               QFileDialog, QFormLayout, QHBoxLayout, QLabel, QLineEdit,
                               QMessageBox, QPushButton, QSpinBox, QVBoxLayout, QWidget)

from prose_to_video.pipeline.providers import AISettings


TEXT_MODELS = ("gpt-6-luna", "gpt-6.1-sol", "gpt-6.1-terra", "gpt-6-astra")
# Presets supported by the application's image adapter.
IMAGE_MODELS = ("gpt-image-2.5-flare", "gpt-image-2.5-sunburst", "gpt-image-2")


def model_selector(models, current):
    selector = QComboBox()
    selector.setEditable(True)
    selector.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
    selector.addItems(models)
    if current and selector.findText(current) < 0:
        selector.addItem(current)
    selector.setCurrentText(current)
    return selector


def load_ai_settings() -> AISettings:
    store = QSettings("ProseToVideo", "ProseToVideo")
    defaults = AISettings()
    temperature = store.value("ai/temperature", "default")
    return AISettings(
        provider=str(store.value("ai/provider", defaults.provider)),
        model=str(store.value("ai/model", defaults.model)),
        credential_env=str(store.value("ai/credential_env", defaults.credential_env)),
        credential_file=str(store.value("ai/credential_file", defaults.credential_file)),
        temperature=None if temperature == "default" else float(temperature),
        max_output_tokens=int(store.value("ai/max_output_tokens", defaults.max_output_tokens)),
        context_tokens=int(store.value("ai/context_tokens", defaults.context_tokens)),
        chunk_chars=int(store.value("ai/chunk_chars", defaults.chunk_chars)),
        overlap_chars=int(store.value("ai/overlap_chars", defaults.overlap_chars)),
    )


def save_ai_settings(settings: AISettings) -> None:
    settings.validate()
    store = QSettings("ProseToVideo", "ProseToVideo")
    for key in ("provider", "model", "credential_env", "credential_file", "max_output_tokens", "context_tokens", "chunk_chars", "overlap_chars"):
        store.setValue(f"ai/{key}", getattr(settings, key))
    store.setValue("ai/temperature", "default" if settings.temperature is None else settings.temperature)
    store.sync()
    if store.status() != type(store).Status.NoError:
        raise ValueError("Could not save AI settings")


class AISettingsFields:
    def _build_fields(self, settings: AISettings):
        self.provider = QComboBox()
        self.provider.addItem("OpenAI")
        self.provider.setCurrentText(settings.provider)
        self.text_model = model_selector(TEXT_MODELS, settings.model)
        self.model = self.text_model.lineEdit()
        self.credential_env = QLineEdit(settings.credential_env)
        self.credential_file = QLineEdit(settings.credential_file)
        self.credential_file.setPlaceholderText("Leave empty to use the environment variable")
        browse = QPushButton("Browse…")
        browse.clicked.connect(self._browse_credential_file)
        file_row = QWidget()
        file_layout = QHBoxLayout(file_row)
        file_layout.setContentsMargins(0, 0, 0, 0)
        file_layout.addWidget(self.credential_file)
        file_layout.addWidget(browse)
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
        for label, widget in (("Provider:", self.provider), ("Words generation model:", self.text_model),
                              ("Credential environment variable:", self.credential_env),
                              ("API key file:", file_row),
                              ("Temperature:", self.temperature), ("Max output tokens:", self.max_output),
                              ("Model context tokens:", self.context_tokens),
                              ("Chunk characters:", self.chunk_chars), ("Overlap characters:", self.overlap_chars)):
            form.addRow(label, widget)
        layout = QVBoxLayout(self)
        credentials_help = QLabel("Choose a text file containing only the API key, or use an environment variable. Only the file path is saved.")
        credentials_help.setWordWrap(True)
        layout.addWidget(credentials_help)
        layout.addLayout(form)

    def values(self) -> AISettings:
        credential_file = self.credential_file.text().strip()
        credential_env = self.credential_env.text().strip()
        if credential_file and not credential_env.isidentifier():
            credential_env = AISettings().credential_env
        return AISettings(provider=self.provider.currentText(), model=self.model.text().strip(),
                          credential_env=credential_env,
                          credential_file=credential_file,
                          temperature=None if self.temperature.value() < 0 else self.temperature.value(),
                          max_output_tokens=self.max_output.value(), context_tokens=self.context_tokens.value(),
                          chunk_chars=self.chunk_chars.value(),
                          overlap_chars=self.overlap_chars.value())

    def _accept(self) -> None:
        try:
            save_ai_settings(self.values())
        except ValueError as exc:
            QMessageBox.warning(self, "Could not save AI settings", str(exc))
            return
        self.accept()

    def _browse_credential_file(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Choose API key file",
                                              self.credential_file.text(), "Text files (*.txt);;All files (*)")
        if path:
            self.credential_file.setText(path)


class AISettingsWidget(QWidget, AISettingsFields):
    """Reusable general AI preferences form."""

    def __init__(self, settings: AISettings, parent=None):
        super().__init__(parent)
        self._build_fields(settings)


class AISettingsDialog(QDialog, AISettingsFields):
    def __init__(self, settings: AISettings, parent=None):
        super().__init__(parent)
        self.setWindowTitle("AI Settings")
        self._build_fields(settings)
        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        self.layout().addWidget(buttons)


class GenerationAISettingsDialog(QDialog):
    """Text and image model choices for the active stills workspace."""

    def __init__(self, settings, image_settings, save, parent=None):
        super().__init__(parent)
        self.setWindowTitle("AI Settings")
        self._image_settings = image_settings
        self._save = save
        self.general = AISettingsWidget(settings)
        self.image_provider = QComboBox()
        self.image_provider.addItems(["OpenAI", "ComfyUI"])
        self.image_provider.setCurrentText(image_settings.provider)
        self.image_model = model_selector(
            IMAGE_MODELS if image_settings.provider == "OpenAI" else (), image_settings.model)
        self._models = {image_settings.provider: image_settings.model}
        self._previous_provider = image_settings.provider
        self.image_provider.currentTextChanged.connect(self._provider_changed)
        form = QFormLayout()
        form.addRow("Image generation provider:", self.image_provider)
        form.addRow("Image generation model:", self.image_model)
        note = QLabel("Text and images use separate models. Image choices are saved as defaults "
                      "and applied to the open project. ComfyUI may leave the model empty to use "
                      "its workflow; configure the workflow in Image Settings.")
        note.setWordWrap(True)
        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addWidget(self.general)
        layout.addLayout(form)
        layout.addWidget(note)
        layout.addWidget(buttons)

    def _provider_changed(self, provider):
        self._models[self._previous_provider] = self.image_model.currentText()
        self.image_model.clear()
        if provider == "OpenAI":
            self.image_model.addItems(IMAGE_MODELS)
        self.image_model.setCurrentText(self._models.get(
            provider, IMAGE_MODELS[0] if provider == "OpenAI" else ""))
        self._previous_provider = provider

    def _accept(self):
        try:
            general = self.general.values()
            general.validate()
            provider = self.image_provider.currentText()
            model = self.image_model.currentText().strip()
            if provider == "OpenAI" and not model:
                raise ValueError("Choose an image generation model for OpenAI")
            images = self._image_settings.model_copy(update={"provider": provider, "model": model})
            self._save(general, images)
        except (ValueError, OSError) as exc:
            QMessageBox.warning(self, "Could not save AI settings", str(exc))
            return
        self.accept()
