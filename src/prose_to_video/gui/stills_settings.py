"""Tabbed preferences for the direct stills workflow."""
import json

from PySide6.QtWidgets import (QComboBox, QDialog, QDialogButtonBox, QFormLayout,
    QLabel, QMessageBox, QPlainTextEdit, QScrollArea, QTabWidget, QVBoxLayout, QWidget)

from prose_to_video.gui.ai_settings import (
    AISettingsWidget, IMAGE_MODELS, load_ai_settings, model_selector, save_ai_settings)
from prose_to_video.storage.ai_preferences import load_tti_defaults, save_tti_defaults


class StillsSettingsWindow(QDialog):
    def __init__(self, parent, initial_tab=0):
        super().__init__(parent)
        self.setWindowTitle('Settings')
        self.resize(780, 740)
        self.state = parent.state
        images = self.state.image_settings
        if not parent.session:
            images = images.model_copy(update=load_tti_defaults())
        self._images = images
        self.tabs = QTabWidget()
        self.general = AISettingsWidget(load_ai_settings())
        ai = QWidget()
        ai_layout = QVBoxLayout(ai)
        ai_layout.addWidget(self.general)
        self.image_provider = QComboBox()
        self.image_provider.addItems(['OpenAI', 'ComfyUI'])
        self.image_provider.setCurrentText(images.provider)
        self.image_model = model_selector(IMAGE_MODELS if images.provider == 'OpenAI' else (), images.model)
        self._models = {images.provider: images.model}
        self._previous_provider = images.provider
        self.image_provider.currentTextChanged.connect(self._provider_changed)
        form = QFormLayout()
        form.addRow('Image generation provider:', self.image_provider)
        form.addRow('Image generation model:', self.image_model)
        ai_layout.addLayout(form)
        note = QLabel('Text and images use separate models. AI choices are saved as defaults and applied '
                      'to the open project. ComfyUI may leave the model empty to use its workflow; '
                      'configure the workflow in Image Settings.')
        note.setWordWrap(True)
        ai_layout.addWidget(note)
        self._add_tab(ai, 'AI Settings')

        self.image_json = QPlainTextEdit(json.dumps(images.model_dump(exclude={'provider', 'model'}), indent=2))
        self._json_tab('Image Settings', self.image_json,
                       'Edit image settings JSON. Choose the provider and model in AI Settings.')
        self.export_json = QPlainTextEdit(self.state.render_settings.model_dump_json(indent=2))
        self._json_tab('Export Settings', self.export_json, 'Edit export settings JSON.')
        pre_stills = QWidget()
        pre_layout = QFormLayout(pre_stills)
        self.style = QPlainTextEdit(self.state.style)
        self.density = QComboBox()
        self.density.addItems(['Low', 'Medium', 'High'])
        self.density.setCurrentText(self.state.density)
        self.scene_detection = QComboBox()
        self.scene_detection.addItem('AI scene detection', 'ai')
        self.scene_detection.addItem('Scene Per Paragraph', 'paragraph')
        self.scene_detection.setCurrentIndex(self.scene_detection.findData(self.state.scene_detection))
        self.scene_detection.currentIndexChanged.connect(self._scene_detection_changed)
        self._scene_detection_changed()
        pre_layout.addRow('Scene detection', self.scene_detection)
        pre_layout.addRow('Project visual style', self.style)
        pre_layout.addRow('Image density', self.density)
        note = QLabel('Scene Per Paragraph uses blank lines as paragraph boundaries; wrapped lines stay '
                      'together. AI still prepares identities and image prompts. Image density applies to '
                      'AI scene detection. After changing the mode, Analyze Story rebuilds scenes and '
                      'clears the timeline; image files are kept.')
        note.setWordWrap(True)
        pre_layout.addRow(note)
        self._add_tab(pre_stills, 'Pre-Stills', project_only=True)
        self.tabs.setCurrentIndex(initial_tab)
        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addWidget(self.tabs)
        layout.addWidget(buttons)

    def _add_tab(self, page, title, project_only=False):
        if project_only and not self.parent().session:
            page.setEnabled(False)
            container = QWidget()
            layout = QVBoxLayout(container)
            layout.addWidget(QLabel('Open a project to change these settings.'))
            layout.addWidget(page)
            page = container
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(page)
        self.tabs.addTab(scroll, title)

    def _json_tab(self, title, editor, description):
        page = QWidget()
        layout = QVBoxLayout(page)
        note = QLabel(description)
        note.setWordWrap(True)
        layout.addWidget(note)
        layout.addWidget(editor)
        self._add_tab(page, title, project_only=True)

    def _scene_detection_changed(self, *_):
        self.density.setEnabled(self.scene_detection.currentData() == 'ai')

    def _provider_changed(self, provider):
        self._models[self._previous_provider] = self.image_model.currentText()
        self.image_model.clear()
        if provider == 'OpenAI':
            self.image_model.addItems(IMAGE_MODELS)
        self.image_model.setCurrentText(self._models.get(provider, IMAGE_MODELS[0] if provider == 'OpenAI' else ''))
        self._previous_provider = provider

    def _accept(self):
        try:
            parent = self.parent()
            if parent.worker:
                raise ValueError('Wait for the current operation or cancel it')
            general = self.general.values()
            general.validate()
            provider = self.image_provider.currentText()
            model = self.image_model.currentText().strip()
            if provider == 'OpenAI' and not model:
                raise ValueError('Choose an image generation model for OpenAI')
            image_values = (json.loads(self.image_json.toPlainText()) if parent.session
                            else self._images.model_dump(exclude={'provider', 'model'}))
            if not isinstance(image_values, dict):
                raise ValueError('Image settings must be a JSON object')
            images = type(self._images).model_validate({**image_values, 'provider': provider, 'model': model})
            updated = self.state.model_copy(update={'image_settings': images})
            if parent.session:
                render = type(self.state.render_settings).model_validate_json(self.export_json.toPlainText())
                updated = updated.model_copy(update={'render_settings': render,
                    'style': self.style.toPlainText(), 'density': self.density.currentText(),
                    'scene_detection': self.scene_detection.currentData()})
                updated.save(parent.session.root)
            save_ai_settings(general)
            save_tti_defaults(provider, model)
            parent.state = updated
        except (ValueError, OSError) as exc:
            QMessageBox.warning(self, 'Could not save settings', str(exc))
            return
        self.accept()
