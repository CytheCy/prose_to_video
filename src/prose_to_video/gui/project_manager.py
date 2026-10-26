"""Small dialogs for creating a project and changing output settings."""

from __future__ import annotations

from pathlib import Path

from pydantic import ValidationError
from PySide6.QtWidgets import (QDialog, QDialogButtonBox, QFileDialog, QFormLayout,
                               QHBoxLayout, QLineEdit, QMessageBox, QPushButton,
                               QSpinBox, QVBoxLayout)

from prose_to_video.models.project import VideoSettings


class NewProjectDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("New Project")
        self.name_edit = QLineEdit()
        self.name_edit.setPlaceholderText("My Story")
        self.parent_edit = QLineEdit(str(Path.home()))
        browse = QPushButton("Browse…")
        browse.clicked.connect(self._browse)
        location = QHBoxLayout()
        location.addWidget(self.parent_edit)
        location.addWidget(browse)
        form = QFormLayout()
        form.addRow("Project name:", self.name_edit)
        form.addRow("Store in:", location)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(buttons)
        self.name_edit.setFocus()

    def _browse(self) -> None:
        directory = QFileDialog.getExistingDirectory(self, "Choose Parent Directory", self.parent_edit.text())
        if directory:
            self.parent_edit.setText(directory)

    def _accept(self) -> None:
        if not self.name_edit.text().strip() or not self.parent_edit.text().strip():
            QMessageBox.warning(self, "New Project", "Enter a project name and parent directory.")
            return
        self.accept()

    @property
    def project_name(self) -> str:
        return self.name_edit.text()

    @property
    def parent_directory(self) -> Path:
        return Path(self.parent_edit.text()).expanduser()


class SettingsDialog(QDialog):
    def __init__(self, settings: VideoSettings, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Video Output Settings")
        self.width = QSpinBox()
        self.height = QSpinBox()
        self.frame_rate = QSpinBox()
        for widget, value, maximum in (
            (self.width, settings.width, 16384),
            (self.height, settings.height, 16384),
            (self.frame_rate, settings.frame_rate, 240),
        ):
            widget.setRange(1, maximum)
            widget.setValue(value)
        form = QFormLayout()
        form.addRow("Width (pixels):", self.width)
        form.addRow("Height (pixels):", self.height)
        form.addRow("Frame rate (fps):", self.frame_rate)
        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(buttons)

    def _accept(self) -> None:
        try:
            self.values()
        except ValidationError as exc:
            QMessageBox.warning(self, "Invalid Settings", "Width and height must be at least 16 pixels; frame rate must be 1–240 fps.")
            return
        self.accept()

    def values(self) -> VideoSettings:
        return VideoSettings(width=self.width.value(), height=self.height.value(),
                             frame_rate=self.frame_rate.value())
