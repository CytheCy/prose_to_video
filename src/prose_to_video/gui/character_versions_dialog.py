"""Edit the appearances of one person across their original story parts."""
from uuid import uuid4

from PySide6.QtCore import Qt, QSignalBlocker
from PySide6.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QLabel, QListWidget,
    QListWidgetItem, QLineEdit, QPushButton, QDialogButtonBox)


class CharacterVersionsDialog(QDialog):
    def __init__(self, character, catalogs, parts, parent=None):
        super().__init__(parent)
        self.setWindowTitle('Character Versions')
        self.resize(650, 480)
        group = character.character_group_id or character.identifier
        self.versions = [c.model_copy(deep=True) for c in catalogs
                         if c.identifier == character.identifier or c.character_group_id == group]
        allowed = set(character.character_source_part_ids or character.part_ids)
        self.parts = [(identifier, label) for identifier, label in parts if identifier in allowed]
        self.row = -1
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel('Versions of the same person. Assign each part to one version.'))
        body = QHBoxLayout()
        self.version_list = QListWidget()
        self.version_list.addItems([c.name for c in self.versions])
        body.addWidget(self.version_list)
        right = QVBoxLayout()
        right.addWidget(QLabel('Version name'))
        self.name_edit = QLineEdit()
        right.addWidget(self.name_edit)
        right.addWidget(QLabel('Appears in (checking a part moves it to this version)'))
        self.part_list = QListWidget()
        right.addWidget(self.part_list)
        body.addLayout(right, 2)
        layout.addLayout(body)
        self.add_button = QPushButton('Add Variant')
        layout.addWidget(self.add_button)
        self.validation = QLabel()
        self.validation.setWordWrap(True)
        layout.addWidget(self.validation)
        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Save).setText('Save and Close')
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.version_list.currentRowChanged.connect(self.select_version)
        self.name_edit.textEdited.connect(self.rename_version)
        self.part_list.itemChanged.connect(self.assign_part)
        self.add_button.clicked.connect(self.add_variant)
        self.version_list.setCurrentRow(next(i for i,c in enumerate(self.versions)
                                            if c.identifier == character.identifier))

    def select_version(self, row):
        self.row = row
        if row < 0:
            return
        version = self.versions[row]
        self.name_edit.setText(version.name)
        with QSignalBlocker(self.part_list):
            self.part_list.clear()
            for identifier, label in self.parts:
                owner = next((c.name for c in self.versions if identifier in c.part_ids), 'Unassigned')
                item = QListWidgetItem(label)
                item.setData(Qt.UserRole, identifier)
                item.setToolTip('Assigned to: ' + owner)
                item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
                item.setCheckState(Qt.Checked if identifier in version.part_ids else Qt.Unchecked)
                self.part_list.addItem(item)

    def rename_version(self, name):
        if self.row >= 0:
            self.versions[self.row].name = name
            self.version_list.item(self.row).setText(name)

    def add_variant(self):
        source = self.versions[self.row]
        self.versions.append(source.model_copy(deep=True, update={
            'identifier': str(uuid4()), 'name': '', 'part_ids': [], 'references': []}))
        self.version_list.addItem('New variant')
        self.version_list.setCurrentRow(len(self.versions) - 1)
        self.name_edit.setFocus()

    def assign_part(self, item):
        identifier = item.data(Qt.UserRole)
        version = self.versions[self.row]
        if item.checkState() == Qt.Checked:
            for c in self.versions:
                c.part_ids = [p for p in c.part_ids if p != identifier]
            version.part_ids.append(identifier)
        else:
            version.part_ids = [p for p in version.part_ids if p != identifier]

    def accept(self):
        names = [c.name.strip().casefold() for c in self.versions]
        if any(not n for n in names) or len(names) != len(set(names)):
            self.validation.setText('Give each version a different, nonempty name.')
            return
        assigned = {p for c in self.versions for p in c.part_ids}
        if any(identifier not in assigned for identifier, _ in self.parts):
            self.validation.setText('Assign every part to a version before saving.')
            return
        super().accept()
