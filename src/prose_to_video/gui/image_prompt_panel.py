"""Editable shot prompt workspace."""
from PySide6.QtCore import Qt, Signal, QSize
from PySide6.QtGui import QIcon, QPixmap
from PySide6.QtWidgets import (QComboBox, QFormLayout, QGridLayout, QLabel, QListWidget,
    QPlainTextEdit, QPushButton, QScrollArea, QVBoxLayout, QWidget)


class ImagePromptPanel(QWidget):
    generate_requested = Signal(str)
    cancel_requested = Signal()
    settings_requested = Signal()
    save_requested = Signal()
    approve_requested = Signal()
    restore_requested = Signal(int)
    export_requested = Signal()
    FIELDS = ('positive_prompt', 'negative_prompt', 'composition', 'foreground', 'background',
              'environment', 'character_appearance', 'lighting', 'visual_style', 'generation_notes')

    def __init__(self, parent=None):
        super().__init__(parent)
        self.prompt = None
        self._versions = []
        self._root = None
        self.status = QLabel('Select a storyboard shot'); self.status.setWordWrap(True)
        self.version = QComboBox()
        self.fields = {name: QPlainTextEdit() for name in self.FIELDS}
        self.fields['positive_prompt'].setMinimumHeight(130)
        for name, widget in self.fields.items():
            if name != 'positive_prompt': widget.setFixedHeight(55)
        form = QFormLayout()
        for name, widget in self.fields.items(): form.addRow(name.replace('_',' ').capitalize(), widget)
        edit = QWidget(); edit.setLayout(form)
        scroll = QScrollArea(); scroll.setWidgetResizable(True); scroll.setWidget(edit)
        self.references = QListWidget(); self.references.setIconSize(QSize(64,64)); self.references.setFixedHeight(110)
        names = [('generate','Generate Prompt'),('regenerate','Regenerate Prompt'),('scene','Generate Scene'),
                 ('all','Generate All'),('cancel','Cancel'),('settings','Settings…'),('save','Save Changes'),
                 ('approve','Approve Prompt'),('revert','Revert Unsaved'),('copy','Copy Prompt'),
                 ('export','Export…'),('restore','Restore Version')]
        self.buttons = {}
        grid = QGridLayout()
        for i,(key,label) in enumerate(names):
            button = QPushButton(label); self.buttons[key] = button; grid.addWidget(button,i//4,i%4)
        self.buttons['generate'].clicked.connect(lambda: self.generate_requested.emit('shot'))
        self.buttons['regenerate'].clicked.connect(lambda: self.generate_requested.emit('regenerate'))
        self.buttons['scene'].clicked.connect(lambda: self.generate_requested.emit('scene'))
        self.buttons['all'].clicked.connect(lambda: self.generate_requested.emit('all'))
        self.buttons['cancel'].clicked.connect(self.cancel_requested)
        self.buttons['settings'].clicked.connect(self.settings_requested)
        self.buttons['save'].clicked.connect(self.save_requested)
        self.buttons['approve'].clicked.connect(self.approve_requested)
        self.buttons['revert'].clicked.connect(self.revert)
        self.buttons['copy'].clicked.connect(self.copy)
        self.buttons['export'].clicked.connect(self.export_requested)
        self.buttons['restore'].clicked.connect(lambda: self.restore_requested.emit(self.version.currentData() or 0))
        self.version.currentIndexChanged.connect(self._version_changed)
        layout = QVBoxLayout(self); layout.addWidget(self.status); layout.addLayout(grid)
        layout.addWidget(QLabel('Version history')); layout.addWidget(self.version)
        layout.addWidget(scroll,1); layout.addWidget(QLabel('Approved reference images')); layout.addWidget(self.references)
        self.set_prompt(None, [], None)

    def set_prompt(self, prompt, versions, root):
        self.prompt = prompt
        self._versions = versions
        self._root = root
        self.version.blockSignals(True); self.version.clear()
        for p in versions: self.version.addItem(f'v{p.version} · {p.review_status}', p.version)
        if prompt: self.version.setCurrentIndex(next((i for i,p in enumerate(versions) if p.identifier == prompt.identifier),0))
        self.version.blockSignals(False)
        self._display(prompt, root)

    def _display(self, prompt, root):
        values = prompt.components.model_dump() if prompt else {}
        values.update(positive_prompt=prompt.positive_prompt if prompt else '',
                      negative_prompt=prompt.negative_prompt if prompt else '')
        for key, widget in self.fields.items(): widget.setPlainText(values.get(key, ''))
        self.references.clear()
        if prompt and root:
            for ref in prompt.references:
                path = root / ref.path
                label = f'{ref.role}: {path.name}' + ('' if path.is_file() else ' (missing)')
                self.references.addItem(label)
                if path.is_file():
                    pix = QPixmap(str(path)).scaled(64,64,Qt.KeepAspectRatio,Qt.SmoothTransformation)
                    self.references.item(self.references.count()-1).setIcon(QIcon(pix))
        self.status.setText(f'v{prompt.version} · {prompt.review_status}' if prompt else 'No prompt for selected shot')
        for key in ('save','approve','revert','copy','restore'):
            self.buttons[key].setEnabled(prompt is not None)

    def _version_changed(self):
        version = self.version.currentData()
        candidate = next((p for p in self._versions if p.version == version), None)
        self._display(candidate, self._root)
        if candidate and self.prompt and candidate.identifier != self.prompt.identifier:
            self.status.setText(f'Viewing v{candidate.version} · restore to edit')
            self.buttons['save'].setEnabled(False)
            self.buttons['approve'].setEnabled(False)

    def changes(self):
        if not self.prompt: raise ValueError('No prompt selected')
        components = self.prompt.components.model_copy(deep=True)
        for key in self.FIELDS:
            if key not in {'positive_prompt','negative_prompt'}:
                setattr(components,key,self.fields[key].toPlainText().strip())
        return self.fields['positive_prompt'].toPlainText().strip(), self.fields['negative_prompt'].toPlainText().strip(), components

    def revert(self):
        if self.prompt: self.set_prompt(self.prompt, self._versions, self._root)

    def copy(self):
        from PySide6.QtWidgets import QApplication
        QApplication.clipboard().setText(self.fields['positive_prompt'].toPlainText())
