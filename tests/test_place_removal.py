"""Delete Place acts on the selection in the active Place tab."""
import os
import subprocess
import sys


def test_delete_selected_place(tmp_path):
    result = subprocess.run([sys.executable, '-c', '''
import sys
from pathlib import Path
from PySide6.QtWidgets import QApplication, QMessageBox
from prose_to_video.gui.main_window import MainWindow
from prose_to_video.models.scene import Scene
from prose_to_video.pipeline.stills_workflow import Identity, Workflow, load_workflow
from prose_to_video.storage.project_files import create_project

app = QApplication([])
window = MainWindow()
button = window.delete_place_button
assert button.text() == 'Delete Place'
assert not button.isEnabled()
session = create_project(Path(sys.argv[1]), 'places')
window.load(session)
harbor = Identity(kind='Place', name='Harbor', description='docks')
garden = Identity(kind='Place', name='Garden', description='roses', references=['references/garden.png'])
character = Identity(kind='Character', name='Ada', description='red coat', manual=True)
window.state.world_prompt = 'Shared world description'
window.state.catalogs = [harbor, garden, character]
window.state.reference_identities = [garden.model_copy(deep=True), character.model_copy(deep=True)]
window.state.scenes = [Scene(sequence=1, start=0, end=1, text='A',
    identity_links=[harbor.identifier, garden.identifier, character.identifier], final_prompt='Existing prompt')]
reference = session.root / garden.references[0]
reference.write_bytes(b'existing reference')
window.state.save(session.root)
window.refresh()
window.workspace_tabs.setCurrentWidget(window.place)
window.show()
app.processEvents()
assert button.isVisible()
listing = window.catalog_lists['Place']
assert window.world_prompt_selected() and not button.isEnabled()
listing.setCurrentRow(2)
assert window.identity('Place').identifier == garden.identifier and button.isEnabled()

errors = []
QMessageBox.warning = lambda *args: errors.append(args[-1])
original_save = Workflow.save
def fail_save(*args):
    raise OSError('Cannot save')
Workflow.save = fail_save
button.click()
assert errors == ['Cannot save']
assert listing.count() == 3 and window.identity('Place').identifier == garden.identifier
Workflow.save = original_save

window.worker = object()
window.show_identity('Place')
assert not button.isEnabled()
window.delete_place()
assert len(window.state.catalogs) == 3
window.worker = None
window.show_identity('Place')
button.click()
restored = load_workflow(session)
assert [c.identifier for c in restored.catalogs] == [harbor.identifier, character.identifier]
assert [c.identifier for c in restored.reference_identities] == [character.identifier]
assert restored.scenes[0].identity_links == [harbor.identifier, character.identifier]
assert restored.scenes[0].final_prompt == 'Existing prompt'
assert restored.world_prompt == 'Shared world description'
assert reference.read_bytes() == b'existing reference'
assert listing.count() == 2 and window.identity('Place').identifier == harbor.identifier
assert window.catalog_edits['Place'].toPlainText() == 'docks'
assert 'Garden deleted.' in window.status.text()
button.click()
assert listing.count() == 1 and window.world_prompt_selected()
assert not button.isEnabled()
window.delete_place()
assert [c.identifier for c in load_workflow(session).catalogs] == [character.identifier]
window.state.world_prompt = ''
window.refresh()
assert listing.count() == 0 and not button.isEnabled()
window.close()
''', str(tmp_path)], capture_output=True, text=True, timeout=30,
        env={**os.environ, 'QT_QPA_PLATFORM': 'offscreen',
             'XDG_CONFIG_HOME': str(tmp_path / 'config')})
    assert result.returncode == 0, result.stderr
