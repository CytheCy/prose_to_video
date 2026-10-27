"""Per-project prompt generation preferences."""
from PySide6.QtWidgets import QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFormLayout, QLineEdit, QVBoxLayout
from prose_to_video.models.image_prompt import PromptSettings


class PromptSettingsDialog(QDialog):
    def __init__(self, settings: PromptSettings, parent=None):
        super().__init__(parent)
        self.setWindowTitle('Prompt Generation Settings')
        self.provider = QComboBox(); self.provider.addItems(['OpenAI']); self.provider.setCurrentText(settings.provider)
        self.model = QLineEdit(settings.model); self.model.setPlaceholderText('Use application AI model')
        self.detail = QComboBox(); self.detail.addItems(['Concise', 'Detailed', 'Highly detailed']); self.detail.setCurrentText(settings.detail_level)
        self.strictness = QComboBox(); self.strictness.addItems(['Relaxed', 'Normal', 'Strict']); self.strictness.setCurrentText(settings.continuity_strictness)
        self.checks = {}
        form = QFormLayout()
        for label, widget in [('Provider', self.provider), ('Model', self.model), ('Detail level', self.detail), ('Continuity', self.strictness)]: form.addRow(label, widget)
        for key, label in [('cinematic_emphasis','Cinematic composition'), ('environmental_emphasis','Environmental detail'),
                           ('character_emphasis','Character detail'), ('allow_environmental_additions','Reasonable environmental additions'),
                           ('generate_negative','Generate negative prompt'), ('include_adjacent_shots','Consider adjacent shots'),
                           ('pause_for_approval','Pause after each prompt')]:
            box = QCheckBox(); box.setChecked(getattr(settings, key)); self.checks[key] = box; form.addRow(label, box)
        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept); buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self); layout.addLayout(form); layout.addWidget(buttons)

    def values(self):
        return PromptSettings(provider=self.provider.currentText(), model=self.model.text().strip(),
            detail_level=self.detail.currentText(), continuity_strictness=self.strictness.currentText(),
            **{k: v.isChecked() for k,v in self.checks.items()})
