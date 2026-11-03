"""Shared places, editable descriptions, and navigation to their scenes."""
from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QFormLayout, QGroupBox, QLabel, QLineEdit,
    QListWidget, QListWidgetItem, QPlainTextEdit, QPushButton, QVBoxLayout)

from prose_to_video.storage.visual_library import VisualLibrary
from prose_to_video.storage.database import ProjectDatabaseError
from prose_to_video.pipeline.place_detection import listed_places


class PlacesPanel(QGroupBox):
    detect_requested = Signal()
    cancel_requested = Signal()
    changed = Signal()
    scene_requested = Signal(str)
    error = Signal(str)

    def __init__(self, parent=None):
        super().__init__('Places', parent)
        self.library: VisualLibrary | None = None
        self.places = []
        self.busy = False
        self.detect_button = QPushButton('Detect Places')
        self.cancel_button = QPushButton('Cancel Place Detection')
        self.cancel_button.setEnabled(False)
        self.status = QLabel('Detect places from the original prose before linking scenes.')
        self.status.setWordWrap(True)
        self.list_widget = QListWidget()
        self.name_edit = QLineEdit()
        self.description_edit = QPlainTextEdit()
        self.description_edit.setPlaceholderText('Describe this place…')
        self.scene_list = QListWidget()
        self.open_button = QPushButton('Open Selected Scene')
        self.save_button = QPushButton('Save Place Details')
        layout = QVBoxLayout(self)
        layout.addWidget(self.detect_button)
        layout.addWidget(self.cancel_button)
        layout.addWidget(self.status)
        layout.addWidget(self.list_widget, 2)
        form = QFormLayout()
        form.addRow('Name:', self.name_edit)
        form.addRow('Description:', self.description_edit)
        layout.addLayout(form)
        layout.addWidget(self.save_button)
        layout.addWidget(QLabel('Scenes at this place (double-click to open):'))
        layout.addWidget(self.scene_list, 1)
        layout.addWidget(self.open_button)
        self.detect_button.clicked.connect(self.detect_requested)
        self.cancel_button.clicked.connect(self.cancel_requested)
        self.list_widget.currentRowChanged.connect(self._selected)
        self.save_button.clicked.connect(self._save)
        self.scene_list.itemDoubleClicked.connect(lambda _: self._open_scene())
        self.scene_list.currentRowChanged.connect(lambda _: self.open_button.setEnabled(self.scene_list.currentItem() is not None))
        self.open_button.clicked.connect(self._open_scene)
        self.set_library(None)

    def set_library(self, library):
        self.library = library
        self.refresh()

    def current_place(self):
        row = self.list_widget.currentRow()
        return self.places[row] if 0 <= row < len(self.places) else None

    def refresh(self):
        previous = self.current_place()
        self.places = listed_places(self.library) if self.library else []
        self.list_widget.blockSignals(True)
        self.list_widget.clear()
        self.list_widget.addItems([f"{p.place_order}. {p.name}" for p in self.places])
        row = next((i for i, p in enumerate(self.places) if previous and p.identifier == previous.identifier), 0)
        self.list_widget.setCurrentRow(row if self.places else -1)
        self.list_widget.blockSignals(False)
        self._selected()
        self.detect_button.setEnabled(bool(self.library))
        self.status.setText(f'{len(self.places)} place(s). Shared places can contain multiple scenes.'
                            if self.places else 'Detect places from the original prose. Scenes can be linked afterward.')

    def _selected(self, *_):
        place = self.current_place()
        self.name_edit.setText(place.name if place else '')
        self.description_edit.setPlainText(place.description if place else '')
        for widget in (self.name_edit, self.description_edit, self.save_button):
            widget.setEnabled(place is not None and not self.busy)
        self.scene_list.clear()
        if place and self.library:
            linked = set(self.library.scenes_for(place.identifier))
            for scene in self.library.db.load_scenes()[1]:
                if scene.identifier in linked:
                    item = QListWidgetItem(f'{scene.sequence}. {scene.title}')
                    item.setData(Qt.UserRole, scene.identifier)
                    self.scene_list.addItem(item)
        self.scene_list.setCurrentRow(0 if self.scene_list.count() else -1)
        self.open_button.setEnabled(self.scene_list.currentItem() is not None)

    def _save(self):
        place = self.current_place()
        if not place or not self.library or self.busy:
            return
        try:
            name = self.name_edit.text().strip()
            aliases = list(place.aliases)
            if name.casefold() != place.name.casefold():
                aliases = [a for a in aliases if a.casefold() != name.casefold()]
                aliases.append(place.name)
            self.library.save_entity(place.model_copy(update={
                'name': name, 'aliases': aliases, 'description': self.description_edit.toPlainText(),
                'place_description_edited': place.place_description_edited or self.description_edit.toPlainText() != place.description}))
            self.refresh()
            self.status.setText('Place details saved.')
            self.changed.emit()
        except (ValueError, ProjectDatabaseError) as exc:
            self.error.emit(str(exc))

    def _open_scene(self):
        item = self.scene_list.currentItem()
        if item:
            self.scene_requested.emit(item.data(Qt.UserRole))
