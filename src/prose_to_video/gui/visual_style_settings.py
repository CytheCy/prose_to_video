"""Saved still-image styles with an exclusive selection."""
from PySide6.QtWidgets import (QButtonGroup, QDialog, QDialogButtonBox, QFormLayout,
                               QLabel, QLineEdit, QMessageBox, QPlainTextEdit,
                               QPushButton, QRadioButton, QScrollArea, QVBoxLayout, QWidget)

from prose_to_video.storage.visual_style_preferences import VisualStyle


class AddVisualStyleDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle('Add Visual Style')
        self.resize(500, 320)
        self.name = QLineEdit()
        self.prompt = QPlainTextEdit()
        form = QFormLayout()
        form.addRow('Name:', self.name)
        form.addRow('Style prompt:', self.prompt)
        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(buttons)

    def _save(self):
        if not self.name.text().strip() or not self.prompt.toPlainText().strip():
            QMessageBox.warning(self, 'Missing style details', 'Enter a name and style prompt.')
            return
        self.accept()

    def style(self):
        return VisualStyle(name=self.name.text().strip(), prompt=self.prompt.toPlainText().strip())


class VisualStyleSettingsWidget(QWidget):
    def __init__(self, preferences, parent=None):
        super().__init__(parent)
        self.preferences = preferences.model_copy(deep=True)
        layout = QVBoxLayout(self)
        note = QLabel('Choose one style for still images. Its prompt is appended as “Style: …” '
                      'immediately before sending to TTI. Saved scene prompts and reference '
                      'generation are unchanged. Styles are shared across projects.')
        note.setWordWrap(True)
        layout.addWidget(note)
        self.group = QButtonGroup(self)
        self.group.setExclusive(True)
        self.choices = QWidget()
        self.choices_layout = QVBoxLayout(self.choices)
        self.none = None
        if not self.preferences.styles:
            self.none = QRadioButton('No visual style')
            self.none.setProperty('style_id', '')
            self.group.addButton(self.none)
            self.choices_layout.addWidget(self.none)
            self.none.setChecked(True)
        for style in self.preferences.styles:
            self._add_choice(style)
        if self.preferences.styles and self.group.checkedButton() is None:
            self.group.buttons()[0].setChecked(True)
        self.choices_layout.addStretch()
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(self.choices)
        layout.addWidget(scroll)
        self.add_button = QPushButton('Add Visual Style')
        self.add_button.clicked.connect(self._add_style)
        layout.addWidget(self.add_button)

    def _add_choice(self, style):
        button = QRadioButton(style.name)
        button.setProperty('style_id', style.identifier)
        button.setToolTip(style.prompt)
        self.group.addButton(button)
        # Keep the expanding spacer below all choices.
        index = self.choices_layout.count()
        if index and self.choices_layout.itemAt(index - 1).spacerItem():
            index -= 1
        self.choices_layout.insertWidget(index, button)
        if style.identifier == self.preferences.selected_id:
            button.setChecked(True)
        return button

    def _add_style(self):
        dialog = AddVisualStyleDialog(self)
        if dialog.exec() == QDialog.Accepted:
            style = dialog.style()
            self.preferences.styles.append(style)
            if self.none is not None:
                self.group.removeButton(self.none)
                self.choices_layout.removeWidget(self.none)
                self.none.deleteLater()
                self.none = None
            self._add_choice(style).setChecked(True)

    def values(self):
        selected = self.group.checkedButton()
        return self.preferences.model_copy(update={
            'selected_id': selected.property('style_id') or None})
