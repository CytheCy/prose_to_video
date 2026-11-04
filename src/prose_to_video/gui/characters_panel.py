"""Recurring characters, prompt attributes and reference image preview."""
from pathlib import Path
from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QFormLayout, QListWidget,
    QLabel, QPushButton, QPlainTextEdit, QFileDialog, QSplitter, QScrollArea)
from prose_to_video.pipeline.character_detection import listed_characters
from prose_to_video.pipeline.entity_extraction import APPEARANCE_FIELDS


class CharactersPanel(QWidget):
    generate_requested = Signal()
    cancel_requested = Signal()
    import_requested = Signal(str, object)
    changed = Signal()
    error = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.library = None
        self.characters = []
        self.busy = False
        self.generate_button = QPushButton('Detect Characters')
        self.cancel_button = QPushButton('Cancel Generation')
        self.status = QLabel('Generate characters from the entire prose. Only characters in two or more paragraphs are listed.')
        self.status.setWordWrap(True)
        self.list_widget = QListWidget()
        self.fields = {}
        form = QFormLayout()
        for field in sorted(APPEARANCE_FIELDS):
            edit = QPlainTextEdit()
            edit.setMaximumHeight(75)
            edit.setPlaceholderText('Not specified in the prose — add details here if needed')
            self.fields[field] = edit
            form.addRow(field.replace('_', ' ').capitalize(), edit)
        self.save_button = QPushButton('Save Visual Attributes')
        self.file_button = QPushButton('Choose Reference Image…')
        self.folder_button = QPushButton('Choose Reference Folder…')
        self.gallery = QListWidget()
        self.preview = QLabel('Choose a reference image for this character')
        self.preview.setAlignment(Qt.AlignCenter)
        self.preview.setMinimumSize(240, 240)
        self.preview.setMaximumHeight(360)
        self.preview.setWordWrap(True)
        details = QWidget(); right = QVBoxLayout(details)
        right.addLayout(form); right.addWidget(self.save_button)
        buttons = QHBoxLayout(); buttons.addWidget(self.file_button); buttons.addWidget(self.folder_button)
        right.addLayout(buttons); right.addWidget(self.gallery); right.addWidget(self.preview)
        scroll = QScrollArea(); scroll.setWidgetResizable(True); scroll.setWidget(details)
        splitter = QSplitter(); splitter.addWidget(self.list_widget); splitter.addWidget(scroll)
        splitter.setStretchFactor(1, 3)
        layout = QVBoxLayout(self)
        controls = QHBoxLayout(); controls.addWidget(self.generate_button); controls.addWidget(self.cancel_button)
        layout.addLayout(controls); layout.addWidget(self.status); layout.addWidget(splitter)
        self.generate_button.clicked.connect(self.generate_requested)
        self.cancel_button.clicked.connect(self.cancel_requested)
        self.list_widget.currentRowChanged.connect(self._selected)
        self.gallery.currentRowChanged.connect(self._preview)
        self.save_button.clicked.connect(self._save)
        self.file_button.clicked.connect(self._choose_file)
        self.folder_button.clicked.connect(self._choose_folder)
        self.set_library(None)

    def current_character(self):
        row = self.list_widget.currentRow()
        return self.characters[row] if 0 <= row < len(self.characters) else None

    def set_library(self, library):
        self.library = library
        self.refresh()

    def refresh(self):
        previous = self.current_character()
        self.characters = listed_characters(self.library) if self.library else []
        self.list_widget.blockSignals(True)
        self.list_widget.clear()
        self.list_widget.addItems([f'{e.name} · {len(e.character_paragraphs)} paragraphs' for e in self.characters])
        row = next((i for i, e in enumerate(self.characters) if previous and e.identifier == previous.identifier), 0)
        self.list_widget.setCurrentRow(row if self.characters else -1)
        self.list_widget.blockSignals(False)
        self._selected()
        self.set_busy(self.busy)

    def set_busy(self, busy, can_generate=None, cancelable=False):
        self.busy = busy
        self.generate_button.setEnabled(bool(self.library) and not busy if can_generate is None else can_generate and not busy)
        self.cancel_button.setEnabled(cancelable)
        for widget in (*self.fields.values(), self.save_button, self.file_button, self.folder_button):
            widget.setEnabled(self.current_character() is not None and not busy)

    def _selected(self, *_):
        character = self.current_character()
        for field, widget in self.fields.items():
            widget.setPlainText(getattr(character, field) if character else '')
        self.gallery.clear()
        self.preview.setText('Choose a reference image for this character')
        if character:
            for ref in self.library.references(character.identifier):
                asset = self.library.asset(ref.asset_id)
                if asset:
                    self.gallery.addItem(ref.description or asset.path)
                    self.gallery.item(self.gallery.count()-1).setData(Qt.UserRole, asset.path)
            if self.gallery.count():
                self.gallery.setCurrentRow(0)
        for widget in (*self.fields.values(), self.save_button, self.file_button, self.folder_button):
            widget.setEnabled(character is not None and not self.busy)

    def _preview(self, *_):
        item = self.gallery.currentItem()
        if not item or not self.library:
            self.preview.setText('Choose a reference image for this character'); return
        pixmap = QPixmap(str(self.library.root / item.data(Qt.UserRole)))
        if pixmap.isNull():
            self.preview.setText('Reference image is missing or unreadable')
        else:
            self.preview.setPixmap(pixmap.scaled(480, 320, Qt.KeepAspectRatio, Qt.SmoothTransformation))

    def _save(self):
        character = self.current_character()
        if not character or self.busy:
            return
        try:
            values = {field: widget.toPlainText().strip() for field, widget in self.fields.items()}
            edited = set(character.character_edited_fields) | {field for field, value in values.items()
                                                               if value != getattr(character, field)}
            self.library.save_entity(character.model_copy(update={**values, 'character_edited_fields': sorted(edited)}))
            self.refresh(); self.changed.emit()
            self.status.setText('Visual attributes saved for text-to-image prompts.')
        except Exception as exc:
            self.error.emit(str(exc))

    def _choose_file(self):
        character = self.current_character()
        if not character or self.busy: return
        path, _ = QFileDialog.getOpenFileName(self, 'Character reference image', '', 'Images (*.png *.jpg *.jpeg *.webp)')
        if path: self.import_requested.emit(character.identifier, [Path(path)])

    def _choose_folder(self):
        character = self.current_character()
        if not character or self.busy: return
        path = QFileDialog.getExistingDirectory(self, 'Folder of references for ' + character.name)
        if not path: return
        try:
            files = sorted(p for p in Path(path).iterdir() if p.is_file() and p.suffix.lower() in {'.png', '.jpg', '.jpeg', '.webp'})
            if not files:
                self.status.setText('No PNG, JPEG or WebP images found in this folder.'); return
            self.import_requested.emit(character.identifier, files)
        except OSError as exc:
            self.error.emit(str(exc))
