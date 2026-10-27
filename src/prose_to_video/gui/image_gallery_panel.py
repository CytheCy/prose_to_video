"""Thumbnail gallery and explicit candidate approval controls."""
from pathlib import Path
from PySide6.QtCore import Qt,Signal,QSize
from PySide6.QtGui import QPixmap,QIcon
from PySide6.QtWidgets import QWidget,QVBoxLayout,QGridLayout,QLabel,QListWidget,QPushButton


class ImageGalleryPanel(QWidget):
    generate_requested=Signal(str)
    approve_requested=Signal(str)
    reject_requested=Signal(str)
    lock_requested=Signal(bool)
    refine_requested=Signal(str)
    cancel_requested=Signal()
    settings_requested=Signal()
    export_requested=Signal()
    details_requested=Signal(str)
    compare_requested=Signal()
    recovery_requested=Signal()

    def __init__(self,parent=None):
        super().__init__(parent);self.root=None;self.assets=[];self.shot=None
        self.status=QLabel('Select a storyboard shot');self.status.setWordWrap(True)
        self.summary=QLabel('');self.summary.setWordWrap(True)
        self.preview=QLabel('No image generated');self.preview.setAlignment(Qt.AlignCenter)
        self.preview.setMinimumHeight(230);self.preview.setStyleSheet('background:#30343b;color:#ddd')
        self.meta=QLabel('');self.meta.setWordWrap(True)
        self.list_widget=QListWidget();self.list_widget.setIconSize(QSize(120,80));self.list_widget.setMinimumHeight(125)
        self.list_widget.currentRowChanged.connect(self._selected)
        names=[('Generate Image',lambda:self.generate_requested.emit('shot')),
            ('Generate Variations',lambda:self.generate_requested.emit('variations')),
            ('Generate Scene',lambda:self.generate_requested.emit('scene')),
            ('Generate Project',lambda:self.generate_requested.emit('project')),
            ('Regenerate',lambda:self.generate_requested.emit('shot')),
            ('Refine Selected',lambda:self.refine_requested.emit(self.current_id())),
            ('Approve Image',lambda:self.approve_requested.emit(self.current_id())),
            ('Reject Image',lambda:self.reject_requested.emit(self.current_id())),
            ('Lock Approved',lambda:self.lock_requested.emit(True)),
            ('Unlock Approved',lambda:self.lock_requested.emit(False)),
            ('Open Image',self._open),('Generation Details',lambda:self.details_requested.emit(self.current_id())),
            ('Compare Candidates',self.compare_requested),('Settings…',self.settings_requested.emit),
            ('Export…',self.export_requested.emit),('Cancel Pending',self.cancel_requested.emit),
            ('Resume / Reconcile Jobs',self.recovery_requested.emit)]
        self.buttons={};grid=QGridLayout()
        for i,(name,action) in enumerate(names):
            button=QPushButton(name);button.clicked.connect(action);self.buttons[name]=button
            grid.addWidget(button,i//4,i%4)
        layout=QVBoxLayout(self);layout.addWidget(self.status);layout.addWidget(self.summary);layout.addLayout(grid)
        layout.addWidget(self.preview,2);layout.addWidget(self.meta);layout.addWidget(self.list_widget,1)

    def current_id(self):
        index=self.list_widget.currentRow()
        return self.assets[index].identifier if 0<=index<len(self.assets) else ''

    def set_shot(self,shot,assets,approved,root):
        self.root=Path(root) if root else None;self.shot=shot;self.assets=assets
        self.list_widget.clear()
        if shot and root:
            self.status.setText(f'{shot.title} · '+('Approved' if approved else 'Awaiting review' if assets else 'Missing image'))
            for asset in assets:
                self.list_widget.addItem(f'{asset.candidate_number} · {asset.approval_status} · {asset.provider}')
                thumb=self.root/asset.thumbnail_path
                if thumb.is_file():self.list_widget.item(self.list_widget.count()-1).setIcon(QIcon(str(thumb)))
            if assets:
                selected=next((i for i,a in enumerate(assets) if approved and a.identifier==approved.identifier),0)
                self.list_widget.setCurrentRow(selected)
            else:self._selected(-1)
        else:
            self.status.setText('Select a storyboard shot');self._selected(-1)

    def _selected(self,index):
        if not self.root or not 0<=index<len(self.assets):
            self.preview.setText('No image generated');self.preview.setPixmap(QPixmap());self.meta.setText('');return
        asset=self.assets[index];path=self.root/asset.path
        pix=QPixmap(str(path)) if path.is_file() else QPixmap()
        if pix.isNull():self.preview.setPixmap(QPixmap());self.preview.setText('Image file missing')
        else:self.preview.setPixmap(pix.scaled(760,420,Qt.KeepAspectRatio,Qt.SmoothTransformation))
        self.meta.setText(f'{asset.provider} · {asset.model} · {asset.width}×{asset.height} · '
            f'{asset.created_at:%Y-%m-%d %H:%M} · prompt {asset.prompt_id} · {asset.approval_status}')

    def _open(self):
        from PySide6.QtGui import QDesktopServices
        from PySide6.QtCore import QUrl
        i=self.list_widget.currentRow()
        if self.root and 0<=i<len(self.assets):
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.root/self.assets[i].path)))
