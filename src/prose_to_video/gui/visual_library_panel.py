"""Qt editor for visual identities, images, variants, and continuity warnings."""
from __future__ import annotations

from pathlib import Path
from PySide6.QtCore import Qt, Signal, QThread, QSize
from PySide6.QtGui import QIcon, QPixmap
from PySide6.QtWidgets import (QComboBox, QFileDialog, QFormLayout, QGridLayout, QHBoxLayout, QInputDialog,
    QLabel, QLineEdit, QListWidget, QMessageBox, QPlainTextEdit, QPushButton,
    QScrollArea, QSplitter, QVBoxLayout, QWidget)
from prose_to_video.models.visual_reference import (ENTITY_TYPES, REVIEW_STATUSES, AppearanceVariant,
    EntityReference, VisualEntity, VisualStyle)
from prose_to_video.storage.visual_export import export_json, export_markdown, export_style
from prose_to_video.storage.visual_library import VisualLibrary

class ImageImportTask(QThread):
    succeeded = Signal(object)
    failed = Signal(str)
    def __init__(self, root: Path, source: Path, parent=None):
        super().__init__(parent)
        self.root, self.source = root, source
    def run(self):
        try: self.succeeded.emit(VisualLibrary(self.root).import_image(self.source))
        except Exception as exc: self.failed.emit(str(exc))

class IntegrityTask(QThread):
    succeeded = Signal(object)
    def __init__(self, root: Path, parent=None):
        super().__init__(parent); self.root = root
    def run(self):
        try: self.succeeded.emit(VisualLibrary(self.root).asset_issues())
        except Exception as exc: self.succeeded.emit([str(exc)])

class VisualLibraryPanel(QWidget):
    extract_requested = Signal(str)
    cancel_requested = Signal()
    changed = Signal()
    style_override_requested = Signal(str, object)
    ENTITY_FIELDS = ('name', 'description', 'appearance_description', 'distinguishing_features',
                     'colors_materials', 'size_proportions', 'clothing_accessories', 'user_notes')
    STYLE_FIELDS = tuple(VisualStyle.model_fields)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.library: VisualLibrary | None = None
        self.current_id = None
        self._image_task = None
        self._integrity_task = None
        self._all_entities = []
        self.search = QLineEdit(); self.search.setPlaceholderText('Search entities')
        self.type_filter = QComboBox(); self.type_filter.addItems(['All types', *ENTITY_TYPES])
        self.entities = QListWidget()
        self.status = QLabel('Open a project to use the Visual Library'); self.status.setWordWrap(True)
        self.fields = {}
        form = QFormLayout()
        for field in self.ENTITY_FIELDS:
            widget = QLineEdit() if field == 'name' else QPlainTextEdit()
            if isinstance(widget, QPlainTextEdit): widget.setFixedHeight(55)
            self.fields[field] = widget; form.addRow(field.replace('_', ' ').capitalize(), widget)
        self.entity_type = QComboBox(); self.entity_type.addItems(ENTITY_TYPES)
        self.review_status = QComboBox(); self.review_status.addItems(REVIEW_STATUSES)
        self.recurring = QComboBox(); self.recurring.addItems(['Recurring', 'Scene-specific'])
        self.aliases = QLineEdit(); self.aliases.setPlaceholderText('Comma-separated aliases')
        for label, widget in [('Type', self.entity_type), ('Review', self.review_status),
                              ('Scope', self.recurring), ('Aliases', self.aliases)]: form.addRow(label, widget)
        self.scene_links = QListWidget(); self.shot_links = QListWidget()
        self.evidence = QListWidget(); self.variants = QListWidget(); self.gallery = QListWidget()
        self.gallery.setIconSize(QSize(72, 72))
        self.preview = QLabel('Select a reference image'); self.preview.setAlignment(Qt.AlignCenter)
        self.preview.setMinimumHeight(180); self.preview.setMaximumHeight(260)
        self.preview.setScaledContents(False)
        self.role = QComboBox(); self.role.setEditable(True)
        self.role.addItems(['Primary portrait', 'Full-body view', 'Side view', 'Back view', 'Clothing reference',
            'Alternate appearance', 'Establishing view', 'Interior', 'Exterior', 'Architectural detail',
            'Layout reference', 'Front view', 'Rear view', 'Detail view', 'Additional view'])
        self.reference_description = QLineEdit()
        self.style_fields = {}
        style_form = QFormLayout()
        for field in self.STYLE_FIELDS:
            widget = QLineEdit() if field == 'style_name' else QPlainTextEdit()
            if isinstance(widget, QPlainTextEdit): widget.setFixedHeight(48)
            self.style_fields[field] = widget
            style_form.addRow(field.replace('_', ' ').capitalize(), widget)
        self.warnings = QListWidget()
        left = QWidget(); left_layout = QVBoxLayout(left)
        left_layout.addWidget(self.search); left_layout.addWidget(self.type_filter); left_layout.addWidget(self.entities)
        for label, callback in [('Add Entity', self.add_entity), ('Delete Entity', self.delete_entity),
                                ('Merge Entities', self.merge_entities), ('Extract Selected Scene', lambda: self.extract_requested.emit('selected')),
                                ('Extract Multiple Scenes…', lambda: self.extract_requested.emit('multiple')),
                                ('Extract All Scenes', lambda: self.extract_requested.emit('all')),
                                ('Cancel Extraction', self.cancel_requested.emit)]:
            button = QPushButton(label); button.clicked.connect(callback); left_layout.addWidget(button)
        right = QWidget(); right_layout = QVBoxLayout(right)
        right_layout.addWidget(self.status)
        editor = QWidget(); editor.setLayout(form)
        editor_scroll = QScrollArea(); editor_scroll.setWidgetResizable(True); editor_scroll.setWidget(editor)
        editor_scroll.setMinimumHeight(260)
        right_layout.addWidget(editor_scroll)
        save = QPushButton('Save Entity'); save.clicked.connect(self.save_entity); right_layout.addWidget(save)
        right_layout.addWidget(QLabel('Related scenes (double-click to remove)')); right_layout.addWidget(self.scene_links)
        right_layout.addWidget(QLabel('Related shots (double-click to remove)')); right_layout.addWidget(self.shot_links)
        link_buttons = QHBoxLayout()
        for label, callback in [('Add Scene', self.add_scene_link), ('Add Shot', self.add_shot_link),
                                ('Separate Selected Links', self.separate_links)]:
            button = QPushButton(label); button.clicked.connect(callback); link_buttons.addWidget(button)
        right_layout.addLayout(link_buttons)
        right_layout.addWidget(QLabel('Source evidence (double-click to approve or reject)')); right_layout.addWidget(self.evidence)
        right_layout.addWidget(QLabel('Appearance variants')); right_layout.addWidget(self.variants)
        variant_button = QPushButton('Add Variant'); variant_button.clicked.connect(self.add_variant); right_layout.addWidget(variant_button)
        right_layout.addWidget(QLabel('Reference images')); right_layout.addWidget(self.gallery)
        right_layout.addWidget(self.preview)
        ref_form = QFormLayout(); ref_form.addRow('Role', self.role); ref_form.addRow('Description', self.reference_description)
        right_layout.addLayout(ref_form)
        ref_buttons = QGridLayout()
        for index, (label, callback) in enumerate([('Import Image', self.import_image), ('Save Role', self.save_reference),
                                ('Approve', self.approve_reference), ('Set Primary', self.set_primary),
                                ('Remove', self.remove_reference)]):
            button = QPushButton(label); button.clicked.connect(callback); ref_buttons.addWidget(button, index // 3, index % 3)
        right_layout.addLayout(ref_buttons)
        style_widget = QWidget(); style_widget.setLayout(style_form)
        style_scroll = QScrollArea(); style_scroll.setWidgetResizable(True); style_scroll.setWidget(style_widget)
        style_scroll.setMinimumHeight(180)
        right_layout.addWidget(QLabel('Project visual style')); right_layout.addWidget(style_scroll)
        style_buttons = QGridLayout()
        for index, (label, callback) in enumerate([('Save Style', self.save_style), ('Save Preset', self.save_preset), ('Load Preset', self.load_preset),
                                ('Import Style', self.import_style),
                                ('Override Scene', lambda: self.style_override_requested.emit('scene', self._style_value())),
                                ('Override Shot', lambda: self.style_override_requested.emit('shot', self._style_value())),
                                ('Clear Scene Override', lambda: self.style_override_requested.emit('scene', None)),
                                ('Clear Shot Override', lambda: self.style_override_requested.emit('shot', None))]):
            button = QPushButton(label); button.clicked.connect(callback); style_buttons.addWidget(button, index // 3, index % 3)
        right_layout.addLayout(style_buttons)
        right_layout.addWidget(QLabel('Continuity warnings')); right_layout.addWidget(self.warnings)
        footer = QHBoxLayout()
        for label, callback in [('Inspect Continuity', self.inspect), ('Export JSON', lambda: self.export('json')),
                                ('Export Markdown', lambda: self.export('md')), ('Export Style', lambda: self.export('style'))]:
            button = QPushButton(label); button.clicked.connect(callback); footer.addWidget(button)
        right_layout.addLayout(footer)
        right_scroll = QScrollArea(); right_scroll.setWidgetResizable(True); right_scroll.setWidget(right)
        splitter = QSplitter(Qt.Horizontal); splitter.addWidget(left); splitter.addWidget(right_scroll)
        splitter.setStretchFactor(0, 1); splitter.setStretchFactor(1, 3)
        layout = QVBoxLayout(self); layout.addWidget(splitter)
        self.search.textChanged.connect(self._filter); self.type_filter.currentTextChanged.connect(self._filter)
        self.entities.currentItemChanged.connect(self._selected)
        self.gallery.currentItemChanged.connect(self._reference_selected)
        self.scene_links.itemDoubleClicked.connect(lambda item: self._remove_link('scene', item))
        self.shot_links.itemDoubleClicked.connect(lambda item: self._remove_link('shot', item))
        self.evidence.itemDoubleClicked.connect(self.review_evidence)
        self.variants.itemDoubleClicked.connect(self.edit_variant)

    def set_library(self, library: VisualLibrary | None):
        self.library = library; self.current_id = None
        self.refresh(); self._load_style()
        if library:
            task = IntegrityTask(library.root, self); self._integrity_task = task
            task.succeeded.connect(lambda issues, root=library.root: self._integrity_ready(root, issues))
            task.finished.connect(task.deleteLater)
            task.finished.connect(lambda: setattr(self, '_integrity_task', None) if self._integrity_task is task else None)
            task.start()

    def _integrity_ready(self, root, issues):
        if not self.library or self.library.root != root: return
        for issue in issues:
            self.warnings.addItem(f'{issue.severity}: {issue.message}' if hasattr(issue, 'severity') else str(issue))
        if issues: self.status.setText(f'{len(issues)} reference asset warning(s); inspect Continuity')

    def _error(self, exc):
        QMessageBox.warning(self, 'Visual Library', str(exc))

    def refresh(self, selected_id=None):
        self._all_entities = self.library.entities() if self.library else []
        selected_id = selected_id or self.current_id
        self._filter()
        for i in range(self.entities.count()):
            if self.entities.item(i).data(Qt.UserRole) == selected_id:
                self.entities.setCurrentRow(i); break
        if self.entities.currentRow() < 0 and self.entities.count(): self.entities.setCurrentRow(0)
        if not self.library: self.status.setText('Open a project to use the Visual Library')

    def _filter(self):
        selected = self.current_id
        self.entities.clear()
        query = self.search.text().casefold(); kind = self.type_filter.currentText()
        for entity in self._all_entities:
            if kind != 'All types' and kind != entity.entity_type: continue
            if query and query not in ' '.join([entity.name, *entity.aliases, entity.description]).casefold(): continue
            self.entities.addItem(f'{entity.name} · {entity.entity_type} [{entity.review_status}]')
            self.entities.item(self.entities.count()-1).setData(Qt.UserRole, entity.identifier)
        for i in range(self.entities.count()):
            if self.entities.item(i).data(Qt.UserRole) == selected:
                self.entities.setCurrentRow(i); break

    def _selected(self, item, previous):
        self.current_id = item.data(Qt.UserRole) if item else None
        entity = self.library.entity(self.current_id) if self.library and self.current_id else None
        for field, widget in self.fields.items():
            value = getattr(entity, field) if entity else ''
            widget.setText(value) if isinstance(widget, QLineEdit) else widget.setPlainText(value)
        self.entity_type.setCurrentText(entity.entity_type if entity else 'Other')
        self.review_status.setCurrentText(entity.review_status if entity else 'Suggested')
        self.recurring.setCurrentIndex(0 if entity is None or entity.recurring else 1)
        self.aliases.setText(', '.join(entity.aliases) if entity else '')
        self._load_related()

    def _load_related(self):
        self.scene_links.clear(); self.shot_links.clear(); self.evidence.clear(); self.variants.clear(); self.gallery.clear()
        self.preview.clear()
        if not self.library or not self.current_id: return
        db = self.library.db
        scenes = {s.identifier: s for s in db.load_scenes()[1]}
        for scene_id in self.library.scenes_for(self.current_id):
            s = scenes.get(scene_id); self.scene_links.addItem(f'{s.sequence}. {s.title}' if s else f'Missing scene: {scene_id}')
            self.scene_links.item(self.scene_links.count()-1).setData(Qt.UserRole, scene_id)
        for link in self.library.shots_for(self.current_id):
            self.shot_links.addItem(f'{link["shot_id"]}' + (f' · variant {link["variant_id"]}' if link['variant_id'] else ''))
            self.shot_links.item(self.shot_links.count()-1).setData(Qt.UserRole, link['shot_id'])
        for e in self.library.evidence(self.current_id):
            self.evidence.addItem(f'{e.kind} · {e.field}: {e.value} [{e.status}]')
            self.evidence.item(self.evidence.count()-1).setData(Qt.UserRole, e.identifier)
        for v in self.library.variants(self.current_id):
            self.variants.addItem(f'{v.name} [{v.status}]' + (' · default' if v.is_default else ''))
            self.variants.item(self.variants.count()-1).setData(Qt.UserRole, v.identifier)
        for ref in self.library.references(self.current_id):
            asset = self.library.asset(ref.asset_id)
            if not asset: continue
            self.gallery.addItem(f'{"★ " if ref.is_primary else ""}{ref.role} · {asset.path} [{ref.status}]')
            item = self.gallery.item(self.gallery.count()-1); item.setData(Qt.UserRole, ref.asset_id)
            thumb = self.library.thumbnail_path(asset)
            if thumb.is_file(): item.setIcon(QIcon(str(thumb)))
        if self.gallery.count(): self.gallery.setCurrentRow(0)
        self.status.setText(f'{self.library.entity(self.current_id).name} · {len(self.library.shots_for(self.current_id))} affected shots')

    def _selected_reference(self):
        item = self.gallery.currentItem()
        if not item or not self.current_id: return None
        return next((r for r in self.library.references(self.current_id) if r.asset_id == item.data(Qt.UserRole)), None)

    def _reference_selected(self, item, previous):
        ref = self._selected_reference()
        if not ref: self.preview.clear(); return
        asset = self.library.asset(ref.asset_id)
        if asset:
            path = self.library.thumbnail_path(asset)
            pixmap = QPixmap(str(path)) if path.is_file() else QPixmap()
            self.preview.setPixmap(pixmap.scaled(350, 240, Qt.KeepAspectRatio, Qt.SmoothTransformation))
        self.role.setCurrentText(ref.role); self.reference_description.setText(ref.description)

    def add_entity(self):
        if not self.library: return
        name, ok = QInputDialog.getText(self, 'Add Entity', 'Name:')
        if ok and name.strip():
            try:
                entity = self.library.save_entity(VisualEntity(name=name.strip(), review_status='Reviewed'))
                self.refresh(entity.identifier); self.changed.emit()
            except Exception as exc: self._error(exc)

    def save_entity(self):
        if not self.library or not self.current_id: return
        try:
            entity = self.library.entity(self.current_id)
            for field, widget in self.fields.items():
                setattr(entity, field, widget.text().strip() if isinstance(widget, QLineEdit) else widget.toPlainText().strip())
            entity.entity_type = self.entity_type.currentText(); entity.review_status = self.review_status.currentText()
            entity.recurring = self.recurring.currentIndex() == 0
            entity.aliases = [s.strip() for s in self.aliases.text().split(',') if s.strip()]
            self.library.save_entity(entity); self.refresh(entity.identifier); self.changed.emit()
        except Exception as exc: self._error(exc)

    def delete_entity(self):
        if not self.library or not self.current_id: return
        if QMessageBox.question(self, 'Delete Entity', 'Delete this entity and its associations? Images remain in the library.') != QMessageBox.Yes: return
        try:
            self.library.delete_entity(self.current_id, confirmed=True); self.current_id=None; self.refresh(); self.changed.emit()
        except Exception as exc: self._error(exc)

    def merge_entities(self):
        if not self.library or not self.current_id: return
        options = [e for e in self.library.entities() if e.identifier != self.current_id]
        if not options: return
        labels = [f'{e.name} [{e.identifier[:8]}]' for e in options]
        choice, ok = QInputDialog.getItem(self, 'Merge Entities', 'Merge into selected entity:', labels, editable=False)
        if not ok: return
        source = options[labels.index(choice)]
        if QMessageBox.question(self, 'Merge Entities', f'Merge {source.name} into {self.library.entity(self.current_id).name}?') != QMessageBox.Yes: return
        try: self.library.merge_entities(self.current_id, source.identifier, confirmed=True); self.refresh(); self.changed.emit()
        except Exception as exc: self._error(exc)

    def _remove_link(self, kind, item):
        if not self.library or not self.current_id: return
        try:
            identifier = item.data(Qt.UserRole)
            if kind == 'scene': self.library.relate_scene(self.current_id, identifier, False)
            else: self.library.relate_shot(self.current_id, identifier, present=False)
            self._load_related(); self.changed.emit()
        except Exception as exc: self._error(exc)

    def add_scene_link(self):
        if not self.library or not self.current_id: return
        scenes = self.library.db.load_scenes()[1]
        choices = [f'{s.sequence}. {s.title}' for s in scenes]
        if not choices: return
        choice, ok = QInputDialog.getItem(self, 'Associate Scene', 'Scene:', choices, editable=False)
        if ok:
            try: self.library.relate_scene(self.current_id, scenes[choices.index(choice)].identifier); self._load_related(); self.changed.emit()
            except Exception as exc: self._error(exc)

    def add_shot_link(self):
        if not self.library or not self.current_id: return
        shots = []
        for scene in self.library.db.load_scenes()[1]:
            board = self.library.db.load_storyboard(scene.identifier)
            if board: shots.extend((scene, shot) for shot in board.shots)
        choices = [f'{scene.sequence}. {shot.title} [{shot.identifier[:8]}]' for scene, shot in shots]
        if not choices: return
        choice, ok = QInputDialog.getItem(self, 'Associate Shot', 'Shot:', choices, editable=False)
        if not ok: return
        scene, shot = shots[choices.index(choice)]
        variants = [v for v in self.library.variants(self.current_id) if v.status == 'Approved']
        options = ['Default', *[v.name for v in variants]]
        selection, ok = QInputDialog.getItem(self, 'Appearance Variant', 'Use variant:', options, editable=False)
        if not ok: return
        variant_id = None if selection == 'Default' else variants[options.index(selection)-1].identifier
        try: self.library.relate_shot(self.current_id, shot.identifier, variant_id); self._load_related(); self.changed.emit()
        except Exception as exc: self._error(exc)

    def separate_links(self):
        if not self.library or not self.current_id: return
        options = []
        for kind, widget in [('scene', self.scene_links), ('shot', self.shot_links),
                             ('image', self.gallery), ('evidence', self.evidence), ('variant', self.variants)]:
            for index in range(widget.count()):
                item = widget.item(index)
                options.append((f'{kind}: {item.text()}', kind, item.data(Qt.UserRole)))
        if not options: return
        label, ok = QInputDialog.getItem(self, 'Separate Identity', 'Association to move:',
                                         [item[0] for item in options], editable=False)
        if not ok: return
        _, kind, identifier = next(item for item in options if item[0] == label)
        name, ok = QInputDialog.getText(self, 'Separate Identity', 'New entity name:')
        if not ok or not name.strip(): return
        try:
            created = self.library.separate(self.current_id,
                [identifier] if kind == 'scene' else [], [identifier] if kind == 'shot' else [],
                [identifier] if kind == 'image' else [], [identifier] if kind == 'evidence' else [],
                [identifier] if kind == 'variant' else [], new_name=name.strip())
            self.refresh(created.identifier); self.changed.emit()
        except Exception as exc: self._error(exc)

    def review_evidence(self, item):
        if not self.library: return
        choice, ok = QInputDialog.getItem(self, 'Review Detail', 'Status:', ['Approved', 'Reviewed', 'Needs Review', 'Rejected'], editable=False)
        if ok:
            try: self.library.set_evidence_status(item.data(Qt.UserRole), choice); self.refresh(self.current_id)
            except Exception as exc: self._error(exc)

    def add_variant(self):
        if not self.library or not self.current_id: return
        name, ok = QInputDialog.getText(self, 'Appearance Variant', 'Variant name:')
        if not ok or not name.strip(): return
        description, ok = QInputDialog.getMultiLineText(self, 'Appearance Variant', 'Description:')
        if not ok: return
        try:
            self.library.add_variant(AppearanceVariant(entity_id=self.current_id, name=name.strip(),
                description=description.strip(), status='Suggested'))
            self._load_related(); self.changed.emit()
        except Exception as exc: self._error(exc)

    def edit_variant(self, item):
        if not self.library or not self.current_id: return
        variant = next((v for v in self.library.variants(self.current_id)
                        if v.identifier == item.data(Qt.UserRole)), None)
        if not variant: return
        description, ok = QInputDialog.getMultiLineText(self, 'Edit Variant', 'Description:', variant.description)
        if not ok: return
        status, ok = QInputDialog.getItem(self, 'Variant Status', 'Status:', list(REVIEW_STATUSES),
                                          list(REVIEW_STATUSES).index(variant.status), editable=False)
        if not ok: return
        scenes = self.library.db.load_scenes()[1]
        options = ['All scenes', *[f'{s.sequence}. {s.title}' for s in scenes]]
        context, ok = QInputDialog.getItem(self, 'Variant Context', 'Relevant scene:', options, editable=False)
        if not ok: return
        default = QMessageBox.question(self, 'Default Variant', 'Make this the default appearance?') == QMessageBox.Yes
        variant.description = description.strip(); variant.status = status; variant.is_default = default
        variant.scene_ids = [] if context == 'All scenes' else [scenes[options.index(context)-1].identifier]
        try: self.library.update_variant(variant); self._load_related(); self.changed.emit()
        except Exception as exc: self._error(exc)

    def import_image(self):
        if not self.library or not self.current_id: return
        path, _ = QFileDialog.getOpenFileName(self, 'Import Reference Image', '', 'Images (*.png *.jpg *.jpeg *.webp)')
        if not path: return
        if self._image_task: return
        root, entity_id = self.library.root, self.current_id
        role, description = self.role.currentText(), self.reference_description.text()
        task = ImageImportTask(root, Path(path), self); self._image_task = task
        task.succeeded.connect(lambda asset: self._image_imported(root, entity_id, role, description, asset))
        task.failed.connect(self._error)
        task.finished.connect(task.deleteLater)
        task.finished.connect(lambda: setattr(self, '_image_task', None))
        self.status.setText('Importing image and creating thumbnail…')
        task.start()

    def _image_imported(self, root, entity_id, role, description, asset):
        if not self.library or self.library.root != root: return
        try:
            if any(ref.asset_id == asset.identifier for ref in self.library.references(entity_id)):
                self.refresh(entity_id)
                self.status.setText('This image is already attached to the entity')
                return
            self.library.attach_image(EntityReference(entity_id=entity_id, asset_id=asset.identifier,
                role=role, description=description))
            self.refresh(entity_id); self.changed.emit()
        except Exception as exc: self._error(exc)

    def save_reference(self):
        ref = self._selected_reference()
        if not ref: return
        ref.role = self.role.currentText(); ref.description = self.reference_description.text()
        try: self.library.attach_image(ref); self._load_related(); self.changed.emit()
        except Exception as exc: self._error(exc)

    def approve_reference(self):
        ref = self._selected_reference()
        if not ref: return
        ref.status = 'Approved'
        try: self.library.attach_image(ref); self._load_related(); self.changed.emit()
        except Exception as exc: self._error(exc)

    def set_primary(self):
        ref = self._selected_reference()
        if not ref: return
        try: self.library.set_primary(self.current_id, ref.asset_id); self._load_related(); self.changed.emit()
        except Exception as exc: self._error(exc)

    def remove_reference(self):
        ref = self._selected_reference()
        if not ref: return
        if ref.status == 'Approved' and QMessageBox.question(self, 'Remove Approved Reference', 'Remove this approved continuity reference?') != QMessageBox.Yes: return
        try: self.library.remove_reference(self.current_id, ref.asset_id, confirmed=True); self._load_related(); self.changed.emit()
        except Exception as exc: self._error(exc)

    def _load_style(self):
        style = self.library.style() if self.library else VisualStyle()
        for field, widget in self.style_fields.items():
            value = getattr(style, field)
            widget.setText(value) if isinstance(widget, QLineEdit) else widget.setPlainText(value)

    def _style_value(self):
        return VisualStyle(**{field: widget.text() if isinstance(widget, QLineEdit) else widget.toPlainText()
                              for field, widget in self.style_fields.items()})

    def save_style(self):
        if not self.library: return
        try: self.library.save_style(self._style_value()); self.changed.emit()
        except Exception as exc: self._error(exc)

    def save_preset(self):
        if not self.library: return
        name, ok = QInputDialog.getText(self, 'Save Style Preset', 'Preset name:')
        if ok and name.strip():
            try: self.library.save_style(self._style_value(), name.strip()); self.changed.emit()
            except Exception as exc: self._error(exc)

    def load_preset(self):
        if not self.library: return
        choices = self.library.style_presets()
        if not choices: return
        choice, ok = QInputDialog.getItem(self, 'Load Style Preset', 'Preset:', choices, editable=False)
        if ok:
            try: self.library.save_style(self.library.load_style_preset(choice)); self._load_style(); self.changed.emit()
            except Exception as exc: self._error(exc)

    def import_style(self):
        if not self.library: return
        path, _ = QFileDialog.getOpenFileName(self, 'Import Visual Style', '', 'JSON files (*.json)')
        if not path: return
        try:
            style = VisualStyle.model_validate_json(Path(path).read_text(encoding='utf-8'))
            self.library.save_style(style); self._load_style(); self.changed.emit()
        except Exception as exc: self._error(exc)

    def inspect(self):
        self.warnings.clear()
        if not self.library: return
        try:
            for issue in self.library.inspect_continuity(): self.warnings.addItem(f'{issue.severity}: {issue.message}')
            if not self.warnings.count(): self.warnings.addItem('No continuity warnings')
        except Exception as exc: self._error(exc)

    def export(self, kind):
        if not self.library: return
        extension = 'md' if kind == 'md' else 'json'
        default = self.library.root / 'references' / f'{"visual_style" if kind == "style" else "visual_guide"}.{extension}'
        path, _ = QFileDialog.getSaveFileName(self, 'Export Visual References', str(default), f'{extension.upper()} (*.{extension})')
        if not path: return
        try: {'json': export_json, 'md': export_markdown, 'style': export_style}[kind](self.library, Path(path))
        except Exception as exc: self._error(exc)
