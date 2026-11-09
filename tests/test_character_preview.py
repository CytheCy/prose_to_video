"""Character selection displays the identity's images in the active workspace."""
import os
import subprocess
import sys


def test_character_reference_preview(tmp_path):
    result = subprocess.run([sys.executable, '-c', """
import sys
from pathlib import Path
from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtGui import QImage, QColor
from PySide6.QtWidgets import QApplication, QListWidget
from prose_to_video.gui.main_window import MainWindow
from prose_to_video.pipeline.stills_workflow import Identity, load_workflow
from prose_to_video.models.scene import Scene
from prose_to_video.storage.project_files import create_project

app = QApplication([])
window = MainWindow()
session = create_project(Path(sys.argv[1]), 'preview')
window.load(session)
for name, color in [('mira.png', 'red'), ('mira-side.png', 'green'), ('jon.png', 'blue')]:
    image = QImage(100, 200, QImage.Format_RGB32)
    image.fill(QColor(color))
    assert image.save(str(session.root / 'references' / name))
window.state.catalogs = [
    Identity(kind='Character', name='Mira', description='green coat',
        references=['references/mira.png', 'references/mira-side.png']),
    Identity(kind='Character', name='Jon', description='', references=['references/jon.png']),
    Identity(kind='Character', name='No image', description=''),
    Identity(kind='Character', name='Missing image', description='', references=['references/missing.png']),
]
recurring_ids = [c.identifier for c in window.state.catalogs]
window.state.catalogs.insert(0, Identity(kind='Character', name='One-off', description=''))
window.state.scenes = [
    Scene(sequence=1, start=0, end=1, text='A',
          identity_links=[window.state.catalogs[0].identifier, *recurring_ids]),
    Scene(sequence=2, start=1, end=2, text='B', identity_links=recurring_ids),
]
window.refresh()
window.workspace_tabs.setCurrentWidget(window.character)
window.show()
app.processEvents()
listing = window.catalog_lists['Character']
preview = window.character_reference_preview
def color():
    return preview.pixmap().toImage().pixelColor(0, 0)
assert listing.currentRow() == 0
assert listing.count() == 4 and listing.item(0).text() == 'Mira'
assert window.character.findChildren(QListWidget) == [listing]
assert color() == QColor('red')
assert window.character_reference_position.text() == '1 / 2'
window.next_character_reference.click()
assert color() == QColor('green')
assert window.character_reference_position.text() == '2 / 2'
window.previous_character_reference.click()
assert color() == QColor('red')
listing.setCurrentRow(1)
assert color() == QColor('blue')
assert window.character_reference_navigation.isHidden()
window.catalog_edits['Character'].setPlainText('blue coat')
window.save_identity('Character')
assert load_workflow(session).catalogs[2].references == ['references/jon.png']
window.refresh()
assert listing.currentRow() == 1 and color() == QColor('blue')
window.resize(1000, 700)
app.processEvents()
assert preview.pixmap().width() <= preview.width()
assert preview.pixmap().height() <= preview.height()
listing.setCurrentRow(2)
assert preview.pixmap().isNull() and 'No reference image' in preview.text()
listing.setCurrentRow(3)
assert preview.pixmap().isNull() and 'missing or unreadable' in preview.text()
listing.setCurrentRow(0)
window.remove_character_reference.click()
assert color() == QColor('green')
assert load_workflow(session).catalogs[1].references == ['references/mira-side.png']
assert (session.root / 'references/mira.png').exists()
window.state.catalogs = []
window.refresh()
assert preview.pixmap().isNull() and 'Select a character' in preview.text()
window.close()
window.deleteLater()
QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
app.processEvents()
""", str(tmp_path)], capture_output=True, text=True, timeout=30,
        env={**os.environ, 'QT_QPA_PLATFORM': 'offscreen',
             'XDG_CONFIG_HOME': str(tmp_path / 'config')})
    assert result.returncode == 0, result.stderr
