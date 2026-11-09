"""Removing a character updates the active workspace and saved scene links."""
import os
import subprocess
import sys


def test_remove_character(tmp_path):
    result = subprocess.run([sys.executable, '-c', '''
import sys
from pathlib import Path
from PySide6.QtWidgets import QApplication, QMessageBox
from prose_to_video.gui.main_window import MainWindow
from prose_to_video.models.scene import Scene
from prose_to_video.pipeline.stills_workflow import Identity, Workflow, dependency, load_workflow
from prose_to_video.storage.project_files import create_project

app = QApplication([])
window = MainWindow()
assert not window.remove_character_button.isEnabled()
session = create_project(Path(sys.argv[1]), 'removal')
window.load(session)
ada = Identity(kind='Character', name='Ada', description='red coat')
mara = Identity(kind='Character', name='Mara', description='blue coat',
    references=['references/mara.png'])
one_off = Identity(kind='Character', name='Courier', description='')
place = Identity(kind='Place', name='Harbor', description='')
window.state.catalogs = [one_off, ada, mara, place]
window.state.reference_identities = [mara.model_copy(deep=True), ada.model_copy(deep=True)]
reference = session.root / mara.references[0]
reference.write_bytes(b'existing reference')
window.state.scenes = [Scene(sequence=i, start=i-1, end=i, text='A',
    characters=['Ada', 'Mara'], identity_links=[ada.identifier, mara.identifier, place.identifier],
    final_prompt='Existing prompt') for i in (1, 2)]
window.state.scenes[0].identity_links.append(one_off.identifier)
for scene in window.state.scenes:
    scene.prompt_dependency = dependency(window.state, scene)
window.state.save(session.root)
window.refresh()
listing = window.catalog_lists['Character']
assert listing.count() == 2
listing.setCurrentRow(1)
assert window.identity('Character').identifier == mara.identifier
assert window.remove_character_button.isEnabled()

# Saving must succeed before the current state or selection changes.
errors = []
QMessageBox.warning = lambda *args: errors.append(args[-1])
original_save = Workflow.save
def fail_save(*args):
    raise OSError('Cannot save')
Workflow.save = fail_save
window.remove_character_button.click()
assert errors == ['Cannot save']
assert listing.count() == 2 and window.identity('Character').identifier == mara.identifier
Workflow.save = original_save

# Background work owns the state; removal cannot change it.
window.worker = object()
window.show_identity('Character')
assert not window.remove_character_button.isEnabled()
window.remove_character()
assert len(window.state.catalogs) == 4
window.worker = None
window.show_identity('Character')
window.remove_character_button.click()
restored = load_workflow(session)
assert [c.identifier for c in restored.catalogs] == [one_off.identifier, ada.identifier, place.identifier]
assert [c.identifier for c in restored.reference_identities] == [ada.identifier]
assert all(mara.identifier not in scene.identity_links for scene in restored.scenes)
assert all(ada.identifier in scene.identity_links and place.identifier in scene.identity_links
           for scene in restored.scenes)
assert all(scene.final_prompt == 'Existing prompt' for scene in restored.scenes)
assert all(scene.prompt_dependency != dependency(restored, scene) for scene in restored.scenes)
assert reference.read_bytes() == b'existing reference'
assert listing.count() == 1 and window.identity('Character').identifier == ada.identifier
assert 'Mara removed.' in window.status.text()
window.remove_character_button.click()
assert listing.count() == 0 and window.identity('Character') is None
assert not window.remove_character_button.isEnabled()
assert window.catalog_edits['Character'].toPlainText() == ''
assert 'Select a character' in window.character_reference_preview.text()
window.remove_character()
assert [c.identifier for c in load_workflow(session).catalogs] == [one_off.identifier, place.identifier]
window.refresh()
assert listing.count() == 0
window.close()
''', str(tmp_path)], capture_output=True, text=True, timeout=30,
        env={**os.environ, 'QT_QPA_PLATFORM': 'offscreen',
             'XDG_CONFIG_HOME': str(tmp_path / 'config')})
    assert result.returncode == 0, result.stderr
