"""Tabbed application preferences."""

from PySide6.QtWidgets import (QComboBox, QDialog, QDialogButtonBox, QFormLayout,
                               QGroupBox, QLabel, QMessageBox,
                               QScrollArea, QTabWidget, QVBoxLayout, QWidget)

from prose_to_video.gui.ai_settings import (AISettingsWidget, IMAGE_MODELS, model_selector,
                                          load_ai_settings, save_ai_settings)
from prose_to_video.gui.project_manager import VideoSettingsWidget
from prose_to_video.gui.storyboard_settings import StoryboardSettingsWidget
from prose_to_video.gui.visual_style_settings import VisualStyleSettingsWidget
from prose_to_video.storage.visual_style_preferences import load_visual_styles, save_visual_styles
from prose_to_video.models.image_job import ImageGenerationSettings
from prose_to_video.models.project import VideoSettings
from prose_to_video.models.shot import StoryboardSettings
from prose_to_video.storage.ai_preferences import load_tti_defaults, save_tti_defaults
from prose_to_video.storage.generated_images import ImageStore
from prose_to_video.storage.database import ProjectDatabaseError
from prose_to_video.storage.project_files import ProjectError, save_settings


class SettingsWindow(QDialog):
    def __init__(self, parent):
        super().__init__(parent)
        self.setWindowTitle("Settings")
        self.resize(740, 720)
        self.image_store = ImageStore(parent.session.root) if parent.session else None
        self.image_settings = (self.image_store.settings() if self.image_store else
                               ImageGenerationSettings(**load_tti_defaults()))
        self.tabs = QTabWidget()
        page = QWidget()
        page_layout = QVBoxLayout(page)
        general = QGroupBox("General AI")
        general_layout = QVBoxLayout(general)
        description = QLabel("Default AI for scene detection, characters, places, storyboards, and text tasks. "
                             "Task-specific model overrides still apply.")
        description.setWordWrap(True)
        general_layout.addWidget(description)
        self.general = AISettingsWidget(load_ai_settings())
        general_layout.addWidget(self.general)
        page_layout.addWidget(general)

        tti = QGroupBox("Text-to-image (TTI)")
        tti_layout = QVBoxLayout(tti)
        self.tti_provider = QComboBox()
        self.tti_provider.addItems(["OpenAI", "ComfyUI"])
        self.tti_provider.setCurrentText(self.image_settings.provider)
        self.image_model = model_selector(
            IMAGE_MODELS if self.image_settings.provider == "OpenAI" else (), self.image_settings.model)
        self.tti_model = self.image_model.lineEdit()
        form = QFormLayout()
        form.addRow("Provider:", self.tti_provider)
        form.addRow("Image generation model:", self.image_model)
        tti_layout.addLayout(form)
        note = QLabel("OpenAI images use the API credentials above. For ComfyUI, configure the server "
                      "and workflow in the project's Image Generation Settings; leave Model empty "
                      "to use the workflow's model.")
        note.setWordWrap(True)
        tti_layout.addWidget(note)
        scope = QLabel("Saved as the default for projects without image settings" +
                       (" and applied to the open project." if self.image_store else "."))
        scope.setWordWrap(True)
        tti_layout.addWidget(scope)
        page_layout.addWidget(tti)
        page_layout.addStretch()
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(page)
        self.tabs.addTab(scroll, "AI")

        video_page = QWidget()
        video_layout = QVBoxLayout(video_page)
        self.video = VideoSettingsWidget(parent.session.project.settings if parent.session else VideoSettings())
        self.video.setEnabled(parent.session is not None)
        video_layout.addWidget(self.video)
        if not parent.session:
            video_layout.addWidget(QLabel("Open a project to change these settings."))
        video_layout.addStretch()
        self.tabs.addTab(video_page, "Video")

        storyboard_page = QWidget()
        storyboard_layout = QVBoxLayout(storyboard_page)
        self.storyboard = StoryboardSettingsWidget(
            parent.session.database.storyboard_settings() if parent.session else StoryboardSettings())
        self.storyboard.setEnabled(parent.session is not None)
        storyboard_layout.addWidget(self.storyboard)
        if not parent.session:
            storyboard_layout.addWidget(QLabel("Open a project to change these settings."))
        storyboard_layout.addStretch()
        storyboard_scroll = QScrollArea()
        storyboard_scroll.setWidgetResizable(True)
        storyboard_scroll.setWidget(storyboard_page)
        self.tabs.addTab(storyboard_scroll, "Storyboard")
        self.visual_style = VisualStyleSettingsWidget(load_visual_styles())
        self.tabs.addTab(self.visual_style, "Visual Style")
        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addWidget(self.tabs)
        layout.addWidget(buttons)
        self._tti_models = {self.image_settings.provider: self.image_settings.model}
        self._tti_previous_provider = self.image_settings.provider
        self.tti_provider.currentTextChanged.connect(self._provider_changed)

    def _provider_changed(self, provider):
        self._tti_models[self._tti_previous_provider] = self.tti_model.text()
        default = ImageGenerationSettings().model if provider == "OpenAI" else ""
        self.image_model.clear()
        if provider == "OpenAI":
            self.image_model.addItems(IMAGE_MODELS)
        self.tti_model.setText(self._tti_models.get(provider, default))
        self._tti_previous_provider = provider

    def _save(self):
        try:
            general = self.general.values()
            general.validate()
            parent = self.parent()
            video = self.video.values() if parent.session else None
            storyboard = self.storyboard.values() if parent.session else None
            provider = self.tti_provider.currentText()
            model = self.tti_model.text().strip()
            if provider == "OpenAI" and not model:
                raise ValueError("Enter a text-to-image model for OpenAI")
            images = self.image_settings.model_copy(update={"provider": provider, "model": model})
            if provider != self.image_settings.provider:
                images.quality = "auto"
                images.seed = None
                images.output_format = "png"
            save_ai_settings(general)
            save_tti_defaults(provider, model)
            save_visual_styles(self.visual_style.values())
            if self.image_store:
                self.image_store.save_settings(images)
            if storyboard is not None:
                parent.session.database.save_storyboard_settings(storyboard)
            if video is not None and video != parent.session.project.settings:
                parent.session = save_settings(parent.session, video)
                parent._refresh()
        except (ValueError, OSError, ProjectError, ProjectDatabaseError) as exc:
            QMessageBox.warning(self, "Could not save settings", str(exc))
            return
        self.accept()
