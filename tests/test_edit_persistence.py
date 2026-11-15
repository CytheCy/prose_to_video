"""Active editor changes survive selection changes and application restarts."""
import os
import subprocess
import sys


def test_scenes_list_omits_excluded_material(tmp_path):
    script = r'''
import sys
from pathlib import Path
from PySide6.QtWidgets import QApplication, QMessageBox
from prose_to_video.gui.main_window import MainWindow
from prose_to_video.models.scene import Scene
from prose_to_video.pipeline.stills_workflow import load_workflow
from prose_to_video.storage.project_files import create_project

app = QApplication([])
window = MainWindow()
session = create_project(Path(sys.argv[1]), 'filtered scenes')
window.load(session)
excluded = Scene(sequence=1, start=0, end=1, text='X',
    title='Excluded manuscript material', production_included=False)
first = Scene(sequence=2, start=1, end=3, text='AB', title='First')
last = Scene(sequence=3, start=3, end=4, text='C', title='Last')
window.state.scenes = [excluded, first, last]
window.refresh()
assert [window.scene_list.item(i).text() for i in range(window.scene_list.count())] == ['2. First', '3. Last']
window.scene_list.setCurrentRow(0)
assert window.scene().identifier == first.identifier
window.fields['title'].setPlainText('Edited first')
window.save_scene()
assert load_workflow(session).scenes[1].title == 'Edited first'
assert window.state.scenes[0] == excluded
assert window.scene().identifier == first.identifier

# Both lists omit exclusions, and stills navigation uses the scene identity.
assert [window.still_list.item(i).text() for i in range(window.still_list.count())] == ['2. Edited first · missing', '3. Last · missing']
window.still_list.setCurrentRow(1)
assert window.still_scene().identifier == last.identifier
assert window.scene().identifier == last.identifier
assert window.scene_list.currentRow() == 1
window.still_list.setCurrentRow(0)
assert window.still_scene().identifier == first.identifier
assert window.scene().identifier == first.identifier

# Structural edits operate on the selected scene in the full manuscript.
window.scene_list.setCurrentRow(0)
window.prose.selected_source_range = lambda: (2, 3)
window.split_scene()
assert [s.text for s in window.state.scenes] == ['X', 'A', 'B', 'C']
assert window.state.scenes[0] == excluded
window.scene_list.setCurrentRow(0)
window.merge_scene()
assert [s.text for s in window.state.scenes] == ['X', 'AB', 'C']
assert window.state.scenes[0] == excluded

# A hidden exclusion remains a merge boundary.
window.state.scenes = [first, excluded, last]
window.refresh()
window.scene_list.setCurrentRow(0)
errors = []
QMessageBox.warning = lambda *args: errors.append(str(args[-1]))
window.merge_scene()
assert errors == ['Cannot merge across manuscript exclusions']
window.state.scenes = [excluded]
window.refresh()
assert window.scene_list.count() == 0 and window.scene() is None
assert window.still_list.count() == 0 and window.still_scene() is None
assert window.close()
'''
    result = subprocess.run([sys.executable, '-c', script, str(tmp_path)],
                            capture_output=True, text=True, timeout=30,
                            env={**os.environ, 'QT_QPA_PLATFORM': 'offscreen',
                                 'XDG_CONFIG_HOME': str(tmp_path / 'config')})
    assert result.returncode == 0, result.stderr


def test_editor_autosave_and_close_guard(tmp_path):
    script = r'''
import sys
from pathlib import Path
from PySide6.QtWidgets import QApplication, QMessageBox
from prose_to_video.gui.main_window import MainWindow
from prose_to_video.models.scene import Scene
from prose_to_video.pipeline.stills_workflow import Identity, Interval, Workflow, load_workflow
from prose_to_video.storage.project_files import create_project

app = QApplication([])
window = MainWindow()
session = create_project(Path(sys.argv[1]), 'edits')
session = type(session)(session.root, session.project.model_copy(update={'audio_duration': 10.0}))
session.database.save(session.project)
window.load(session)
person = Identity(kind='Character', name='Mira', description='Old description')
place = Identity(kind='Place', name='Harbor', description='Old place')
window.state.catalogs = [person, place]
window.state.scenes = [Scene(sequence=i+1, start=i, end=i+1, text='A',
    identity_links=[person.identifier, place.identifier]) for i in range(2)]
a, b = window.state.scenes
window.state.timeline = [Interval(scene_id=a.identifier, start=0, end=5, speech_start=0, speech_end=5),
                         Interval(scene_id=b.identifier, start=5, end=10, speech_start=5, speech_end=10)]
window.state.save(session.root)
window.refresh()
window.scene_list.setCurrentRow(0)
window.fields['final_prompt'].setPlainText('Manual image prompt')
window.fields['characters'].setPlainText('Mira\nJon')
window.fields['render_type'].setPlainText('concept_map')
window.fields['explanatory_focus'].setPlainText('The relay connects nearby devices.')
window.scene_list.setCurrentRow(1)
window.fields['title'].setPlainText('Second edited scene')
window.scene_list.setCurrentRow(0)
assert window.fields['final_prompt'].toPlainText() == 'Manual image prompt'
window.catalog_edits['Character'].setPlainText('New character description')
window.catalog_edits['Place'].setPlainText('New place description')
window.refresh()  # Programmatic population must never become an edit.
restored = load_workflow(session)
assert restored.catalogs[0].description == 'New character description'
assert restored.catalogs[1].description == 'New place description'
assert restored.scenes[0].characters == ['Mira', 'Jon']
assert restored.scenes[0].render_type == 'concept_map'
assert restored.scenes[0].explanatory_focus == 'The relay connects nearby devices.'
assert 'final_prompt' in restored.scenes[0].manual_fields
assert restored.scenes[1].title == 'Second edited scene'

errors = []
QMessageBox.warning = lambda *args: errors.append(str(args[-1]))
# Invalid scene edits survive selection changes and prevent a silent close.
window.fields['render_type'].setPlainText('invalid')
window.scene_list.setCurrentRow(1)
window.scene_list.setCurrentRow(0)
assert window.fields['render_type'].toPlainText() == 'invalid'
assert not window.close()
assert errors
window.fields['render_type'].setPlainText('cinematic')
# A failed autosave keeps the in-memory edit and prevents closing until retried.
original_save = Workflow.save
def fail(*args): raise OSError('Disk unavailable')
Workflow.save = fail
window.fields['lighting'].setPlainText('Warm light')
assert not window.close()
assert 'Disk unavailable' in errors[-1]
Workflow.save = original_save
# Timeline edits are a transaction: do not close with only half a boundary changed.
window.timeline.item(0, 2).setText('6')
assert not window.close()
assert load_workflow(session).timeline[0].end == 5
window.timeline.item(1, 1).setText('6')
assert window.close()
window = MainWindow()
assert window.session.root == session.root
assert window.state.scenes[0].lighting == 'Warm light'
assert window.state.scenes[0].final_prompt == 'Manual image prompt'
assert window.state.timeline[0].end == window.state.timeline[1].start == 6
assert all(t.manual for t in window.state.timeline)
assert window.close()
'''
    result = subprocess.run([sys.executable, '-c', script, str(tmp_path)],
                            capture_output=True, text=True, timeout=30,
                            env={**os.environ, 'QT_QPA_PLATFORM': 'offscreen',
                                 'XDG_CONFIG_HOME': str(tmp_path / 'config')})
    assert result.returncode == 0, result.stderr
