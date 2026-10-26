"""Per-project visual planning preferences."""
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QDialogButtonBox,
    QDoubleSpinBox, QFormLayout, QLineEdit, QMessageBox, QVBoxLayout)
from prose_to_video.models.shot import StoryboardSettings

class StoryboardSettingsDialog(QDialog):
    def __init__(self, settings: StoryboardSettings, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Storyboard Settings")
        self.density = QComboBox(); self.density.addItems(("Low", "Medium", "High", "Custom")); self.density.setCurrentText(settings.density)
        self.target = QDoubleSpinBox(); self.target.setRange(.1, 600); self.target.setValue(settings.custom_target); self.target.setSuffix(" s")
        self.minimum = QDoubleSpinBox(); self.minimum.setRange(.1, 600); self.minimum.setValue(settings.minimum); self.minimum.setSuffix(" s")
        self.maximum = QDoubleSpinBox(); self.maximum.setRange(.1, 600); self.maximum.setValue(settings.maximum); self.maximum.setSuffix(" s")
        self.style = QLineEdit(settings.visual_style)
        self.movement = QComboBox(); self.movement.addItems(("None", "Subtle", "Moderate")); self.movement.setCurrentText(settings.movement_intensity)
        self.establishing = QCheckBox(); self.establishing.setChecked(settings.encourage_establishing)
        self.closeups = QCheckBox(); self.closeups.setChecked(settings.encourage_closeups)
        self.details = QCheckBox(); self.details.setChecked(settings.include_environmental_details)
        self.pause = QComboBox(); self.pause.addItem("Continue previous shot", "previous"); self.pause.addItem("Start next shot", "next"); self.pause.setCurrentIndex(0 if settings.pause_policy == "previous" else 1)
        self.threshold = QDoubleSpinBox(); self.threshold.setRange(0, 60); self.threshold.setValue(settings.long_pause_threshold); self.threshold.setSuffix(" s")
        self.exceptions = QCheckBox(); self.exceptions.setChecked(settings.allow_duration_exceptions)
        form = QFormLayout()
        for label, widget in (("Shot density",self.density),("Custom target",self.target),("Minimum duration",self.minimum),
            ("Maximum duration",self.maximum),("Preferred visual style",self.style),("Camera movement",self.movement),
            ("Encourage establishing shots",self.establishing),("Encourage close-ups",self.closeups),
            ("Dedicated environment details",self.details),("Narration pauses",self.pause),
            ("Long pause threshold",self.threshold),("Allow duration exceptions",self.exceptions)):
            form.addRow(label, widget)
        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._accept); buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self); layout.addLayout(form); layout.addWidget(buttons)

    def values(self):
        return StoryboardSettings(density=self.density.currentText(), custom_target=self.target.value(),
            minimum=self.minimum.value(), maximum=self.maximum.value(), visual_style=self.style.text().strip(),
            movement_intensity=self.movement.currentText(), encourage_establishing=self.establishing.isChecked(),
            encourage_closeups=self.closeups.isChecked(), include_environmental_details=self.details.isChecked(),
            pause_policy=self.pause.currentData(), long_pause_threshold=self.threshold.value(),
            allow_duration_exceptions=self.exceptions.isChecked())

    def _accept(self):
        try: self.values()
        except ValueError as exc:
            QMessageBox.warning(self, "Invalid storyboard settings", str(exc)); return
        self.accept()
