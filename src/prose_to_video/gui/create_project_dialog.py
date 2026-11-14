"""Choose a parent directory and name, then create a shared project on Save."""
from pathlib import Path

from PySide6.QtWidgets import (
    QDialog, QDialogButtonBox, QFileDialog, QFormLayout, QHBoxLayout,
    QLabel, QLineEdit, QPushButton, QVBoxLayout,
)
from prose_to_video.storage.project_files import ProjectError, create_project


class CreateProjectDialog(QDialog):
    def __init__(self, parent=None, location=None):
        super().__init__(parent)
        self.setWindowTitle('Create Project')
        self.setMinimumWidth(520)
        self.session = None
        layout = QVBoxLayout(self)
        form = QFormLayout()
        layout.addLayout(form)
        self.location = QLineEdit(str(location or Path.home()))
        browse = QPushButton('Browse…')
        browse.clicked.connect(self.browse)
        location_row = QHBoxLayout()
        location_row.addWidget(self.location, 1)
        location_row.addWidget(browse)
        form.addRow('Project folder location', location_row)
        self.name = QLineEdit()
        self.name.setPlaceholderText('Project folder name')
        form.addRow('Project name', self.name)
        self.destination = QLabel()
        self.destination.setWordWrap(True)
        layout.addWidget(self.destination)
        self.error = QLabel()
        self.error.setWordWrap(True)
        layout.addWidget(self.error)
        self.buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        self.buttons.accepted.connect(self.save)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)
        self.location.textChanged.connect(self.update_destination)
        self.name.textChanged.connect(self.update_destination)
        self.update_destination()
        self.name.setFocus()

    def browse(self):
        selected = QFileDialog.getExistingDirectory(self, 'Project folder location', self.location.text())
        if selected:
            self.location.setText(selected)

    def update_destination(self):
        self.destination.setText('Create folder: ' + str(
            Path(self.location.text()).expanduser() / self.name.text().strip()))
        self.buttons.button(QDialogButtonBox.Save).setEnabled(
            bool(self.location.text().strip() and self.name.text().strip()))
        self.error.clear()

    def save(self):
        try:
            self.session = create_project(Path(self.location.text()).expanduser(), self.name.text())
        except ProjectError as exc:
            self.error.setText(str(exc))
            return
        self.accept()
