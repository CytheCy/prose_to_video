"""Recurring characters, prompt attributes and reference image preview."""
from pathlib import Path
from PySide6.QtCore import Qt, Signal, QSignalBlocker
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QFormLayout, QListWidget,
    QLabel, QPushButton, QPlainTextEdit, QFileDialog, QSplitter, QScrollArea, QSizePolicy)
from prose_to_video.pipeline.character_detection import listed_characters
from prose_to_video.pipeline.entity_extraction import APPEARANCE_FIELDS


class ReferencePreview(QLabel):
    """Scale from the original image whenever the available space changes."""

    def __init__(self):
        super().__init__()
        self._original = QPixmap()
        self.setAlignment(Qt.AlignCenter)
        self.setMinimumSize(240, 600)
        self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Expanding)
        self.setWordWrap(True)

    def setPixmap(self, pixmap):
        self._original = pixmap
        self._scale_image()

    def setText(self, text):
        self._original = QPixmap()
        super().setText(text)

    def _scale_image(self):
        if not self._original.isNull():
            super().setPixmap(self._original.scaled(
                self.contentsRect().size(), Qt.KeepAspectRatio, Qt.SmoothTransformation))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._scale_image()


class CharactersPanel(QWidget):
    prepare_references_requested = Signal()
    generate_reference_requested = Signal(str)
    generate_requested = Signal()
    cancel_requested = Signal()
    import_requested = Signal(str, object)
    generate_image_requested = Signal(str)
    changed = Signal()
    error = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.library = None
        self.characters = []
        self.busy = False
        self.generate_button = QPushButton('Detect Characters')
        self.remove_button = QPushButton('Remove Character')
        self.remove_button.setToolTip('Remove the selected character and its associations.')
        self.cancel_button = QPushButton('Cancel Generation')
        self.status = QLabel('Generate characters from the entire prose. Only characters in two or more paragraphs are listed.')
        self.status.setWordWrap(True)
        self.status.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
        self.list_widget = QListWidget()
        self.fields = {}
        form = QFormLayout()
        for field in sorted(APPEARANCE_FIELDS - {'colors_materials'}):
            edit = QPlainTextEdit()
            edit.setMaximumHeight(75)
            edit.setPlaceholderText('Not specified in the prose — add details here if needed')
            self.fields[field] = edit
            form.addRow(field.replace('_', ' ').capitalize(), edit)
        self.generate_image_button = QPushButton('Generate Image')
        self.generate_image_button.setToolTip('Generate a full-body reference with headroom using the current visual attributes and TTI settings.')
        self.file_button = QPushButton('Choose Reference Image…')
        self.folder_button = QPushButton('Choose Reference Folder…')
        # Retain reference selection without displaying a list of descriptions.
        self.gallery = QListWidget(self)
        self.gallery.hide()
        self.preview = ReferencePreview()
        self.reference_navigation = QWidget()
        navigation = QHBoxLayout(self.reference_navigation)
        navigation.setContentsMargins(0, 0, 0, 0)
        self.previous_reference = QPushButton('Previous Image')
        self.next_reference = QPushButton('Next Image')
        self.delete_reference = QPushButton('Delete Image')
        self.delete_reference.setToolTip('Remove the displayed reference image from this character.')
        self.reference_position = QLabel()
        self.reference_position.setAlignment(Qt.AlignCenter)
        navigation.addWidget(self.previous_reference)
        navigation.addWidget(self.reference_position, 1)
        navigation.addWidget(self.next_reference)
        navigation.addWidget(self.delete_reference)
        details = QWidget(); right = QVBoxLayout(details)
        right.addLayout(form)
        buttons = QHBoxLayout(); buttons.addWidget(self.generate_image_button); buttons.addWidget(self.file_button); buttons.addWidget(self.folder_button)
        right.addLayout(buttons); right.addWidget(self.preview, 1)
        right.addWidget(self.reference_navigation)
        reference_actions = QHBoxLayout()
        self.approve_reference_button = QPushButton('Approve Reference')
        self.primary_reference_button = QPushButton('Set Primary')
        self.approve_reference_button.clicked.connect(lambda: self._update_reference(False))
        self.primary_reference_button.clicked.connect(lambda: self._update_reference(True))
        reference_actions.addWidget(self.approve_reference_button)
        reference_actions.addWidget(self.primary_reference_button)
        right.addLayout(reference_actions)
        self.reference_prompts = QListWidget()
        self.reference_prompts.setMaximumHeight(100)
        right.addWidget(QLabel('Prepared character references'))
        right.addWidget(self.reference_prompts)
        self.prepare_references_button = QPushButton('Prepare Character References')
        self.prepare_references_button.clicked.connect(self.prepare_references_requested.emit)
        right.addWidget(self.prepare_references_button)
        self.generate_reference_button = QPushButton('Review / Generate Reference…')
        self.generate_reference_button.clicked.connect(self._generate_reference)
        right.addWidget(self.generate_reference_button)
        scroll = QScrollArea(); scroll.setWidgetResizable(True); scroll.setWidget(details)
        splitter = QSplitter(); splitter.addWidget(self.list_widget); splitter.addWidget(scroll)
        splitter.setStretchFactor(1, 3)
        layout = QVBoxLayout(self)
        controls = QHBoxLayout(); controls.addWidget(self.generate_button); controls.addWidget(self.remove_button); controls.addWidget(self.cancel_button)
        layout.addLayout(controls); layout.addWidget(self.status); layout.addWidget(splitter, 1)
        self.generate_button.clicked.connect(self.generate_requested)
        self.remove_button.clicked.connect(self._remove_character)
        self.cancel_button.clicked.connect(self.cancel_requested)
        self.list_widget.currentRowChanged.connect(self._selected)
        self.gallery.currentRowChanged.connect(self._preview)
        self.previous_reference.clicked.connect(lambda: self.gallery.setCurrentRow(self.gallery.currentRow() - 1))
        self.next_reference.clicked.connect(lambda: self.gallery.setCurrentRow(self.gallery.currentRow() + 1))
        self.delete_reference.clicked.connect(self._delete_reference)
        for widget in self.fields.values():
            widget.textChanged.connect(self._save)
        self.generate_image_button.clicked.connect(self._generate_image)
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
        for button in (self.approve_reference_button, self.primary_reference_button):
            button.setEnabled(not busy and self.gallery.currentItem() is not None)
        self.prepare_references_button.setEnabled(bool(self.library) and not busy)
        self.generate_reference_button.setEnabled(self.reference_prompts.count() > 0 and not busy)
        self.delete_reference.setEnabled(not busy and self.gallery.count() > 1 and self.gallery.currentItem() is not None)
        self.generate_button.setEnabled(bool(self.library) and not busy if can_generate is None else can_generate and not busy)
        self.cancel_button.setEnabled(cancelable)
        for widget in (*self.fields.values(), self.remove_button, self.generate_image_button, self.file_button, self.folder_button):
            widget.setEnabled(self.current_character() is not None and not busy)

    def _selected(self, *_):
        character = self.current_character()
        for field, widget in self.fields.items():
            with QSignalBlocker(widget):
                widget.setPlainText(getattr(character, field) if character else '')
        self.reference_prompts.clear()
        if character:
            from prose_to_video.pipeline.character_references import ReferenceStore
            for record in ReferenceStore(self.library).records(character.identifier):
                self.reference_prompts.addItem(f"{record['label']} [{record['status']}]")
                self.reference_prompts.item(self.reference_prompts.count()-1).setData(Qt.UserRole, record['identifier'])
            if self.reference_prompts.count():
                self.reference_prompts.setCurrentRow(self.reference_prompts.count()-1)
        self.generate_reference_button.setEnabled(self.reference_prompts.count() > 0 and not self.busy)
        self.gallery.clear()
        self.reference_navigation.hide()
        self.preview.setText('Choose a reference image for this character')
        if character:
            for ref in self.library.references(character.identifier):
                asset = self.library.asset(ref.asset_id)
                if asset:
                    self.gallery.addItem(ref.description or asset.path)
                    self.gallery.item(self.gallery.count()-1).setData(Qt.UserRole, asset.path)
                    self.gallery.item(self.gallery.count()-1).setData(Qt.UserRole + 1, asset.identifier)
            if self.gallery.count():
                self.gallery.setCurrentRow(0)
        for widget in (*self.fields.values(), self.remove_button, self.generate_image_button, self.file_button, self.folder_button):
            widget.setEnabled(character is not None and not self.busy)

    def _generate_reference(self):
        item = self.reference_prompts.currentItem()
        if item: self.generate_reference_requested.emit(item.data(Qt.UserRole))

    def _remove_character(self):
        character = self.current_character()
        if self.busy or not self.library or not character:
            return
        try:
            self.library.delete_entity(character.identifier, confirmed=True)
            self.refresh()
            self.status.setText(f'{character.name} removed.')
            self.changed.emit()
        except Exception as exc:
            self.error.emit(str(exc))

    def _preview(self, *_):
        row, count = self.gallery.currentRow(), self.gallery.count()
        self.reference_navigation.setVisible(count > 1)
        self.previous_reference.setEnabled(row > 0)
        self.next_reference.setEnabled(0 <= row < count - 1)
        self.delete_reference.setEnabled(not self.busy and count > 1 and row >= 0)
        self.reference_position.setText(f'{row + 1} / {count}')
        item = self.gallery.currentItem()
        for button in (self.approve_reference_button, self.primary_reference_button):
            button.setEnabled(not self.busy and item is not None)
        if not item or not self.library:
            self.preview.setText('Choose a reference image for this character'); return
        pixmap = QPixmap(str(self.library.root / item.data(Qt.UserRole)))
        if pixmap.isNull():
            self.preview.setText('Reference image is missing or unreadable')
        else:
            self.preview.setPixmap(pixmap)

    def _update_reference(self, primary):
        character = self.current_character()
        item = self.gallery.currentItem()
        if self.busy or not self.library or not character or not item:
            return
        asset_id = item.data(Qt.UserRole + 1)
        try:
            if primary:
                self.library.set_primary(character.identifier, asset_id)
            else:
                reference = next(r for r in self.library.references(character.identifier) if r.asset_id == asset_id)
                reference.status = 'Approved'
                self.library.attach_image(reference)
            self.status.setText('Primary reference selected.' if primary else 'Reference approved.')
            self.changed.emit()
        except Exception as exc:
            self.error.emit(str(exc))

    def _delete_reference(self):
        character = self.current_character()
        item = self.gallery.currentItem()
        if self.busy or not self.library or not character or not item or self.gallery.count() <= 1:
            return
        try:
            self.library.remove_reference(character.identifier, item.data(Qt.UserRole + 1), confirmed=True)
            row = self.gallery.currentRow()
            self.gallery.takeItem(row)
            self.gallery.setCurrentRow(min(row, self.gallery.count() - 1))
            self._preview()
            self.status.setText(f'Reference image deleted from {character.name}.')
            self.changed.emit()
        except Exception as exc:
            self.error.emit(str(exc))

    def _save(self):
        """Persist edits immediately without resetting the editor or image selection."""
        character = self.current_character()
        if not character or self.busy:
            return
        try:
            values = {field: widget.toPlainText().strip() for field, widget in self.fields.items()}
            edited = set(character.character_edited_fields) | {field for field, value in values.items()
                                                               if value != getattr(character, field)}
            if any(value != getattr(character, field) for field, value in values.items()):
                saved = self.library.save_entity(character.model_copy(update={**values, 'character_edited_fields': sorted(edited)}))
                self.characters[self.list_widget.currentRow()] = saved
                self.status.setText('Visual attributes saved automatically.')
                self.changed.emit()
            return True
        except Exception as exc:
            self.status.setText('Could not save visual attributes: ' + str(exc))
            self.error.emit(str(exc))

    def _generate_image(self):
        character = self.current_character()
        if not character or self.busy:
            return
        if self._save():
            self.generate_image_requested.emit(character.identifier)

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
