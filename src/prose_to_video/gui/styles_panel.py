"""Project-wide descriptions included in every text-to-image prompt."""
from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QFormLayout, QGroupBox, QLabel, QLineEdit, QListWidget,
                               QListWidgetItem, QMessageBox, QPlainTextEdit, QPushButton,
                               QVBoxLayout)

from prose_to_video.storage.database import ProjectDatabaseError


class StylesPanel(QGroupBox):
    changed = Signal()

    def __init__(self, parent=None):
        super().__init__('Styles', parent)
        self.library = None
        layout = QVBoxLayout(self)
        help_text = QLabel('Check styles to include their descriptions in every text-to-image (TTI) prompt. '
                           'Styles are saved with this project. Regenerate existing prompts after changing your selection.')
        help_text.setWordWrap(True)
        layout.addWidget(help_text)
        self.styles = QListWidget()
        self.styles.setMinimumHeight(130)
        self.styles.setWordWrap(True)
        self.styles.itemChanged.connect(self._selection_changed)
        layout.addWidget(self.styles)
        self.name = QLineEdit()
        self.name.setPlaceholderText('e.g. Watercolor')
        self.description = QPlainTextEdit()
        self.description.setPlaceholderText('Describe the look to include in every TTI prompt…')
        self.description.setMaximumHeight(100)
        form = QFormLayout()
        form.addRow('Style name', self.name)
        form.addRow('Description', self.description)
        layout.addLayout(form)
        self.add_button = QPushButton('Add Style')
        self.add_button.clicked.connect(self._add_style)
        layout.addWidget(self.add_button)
        self.setEnabled(False)

    def set_library(self, library):
        self.library = library
        self.name.clear()
        self.description.clear()
        self.setEnabled(library is not None)
        self.refresh()

    def refresh(self):
        self.styles.blockSignals(True)
        try:
            self.styles.clear()
            for style in self.library.prompt_styles() if self.library else []:
                item = QListWidgetItem(f"{style['name']} — {style['description']}")
                item.setData(Qt.UserRole, style['identifier'])
                item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
                item.setCheckState(Qt.Checked if style['selected'] else Qt.Unchecked)
                self.styles.addItem(item)
        finally:
            self.styles.blockSignals(False)

    def _add_style(self):
        if not self.library:
            return
        try:
            self.library.add_prompt_style(self.name.text(), self.description.toPlainText())
            self.name.clear()
            self.description.clear()
            self.refresh()
        except (ValueError, ProjectDatabaseError) as exc:
            QMessageBox.warning(self, 'Style', str(exc))

    def _selection_changed(self, item):
        if not self.library:
            return
        try:
            self.library.select_prompt_style(item.data(Qt.UserRole), item.checkState() == Qt.Checked)
            self.changed.emit()
        except (ValueError, ProjectDatabaseError) as exc:
            QMessageBox.warning(self, 'Style', str(exc))
            self.refresh()
