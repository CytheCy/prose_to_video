"""Project undo works from focused editors and restores autosaved changes."""
import os
import subprocess
import sys


def test_project_undo(tmp_path):
    script = r'''
import sys
from pathlib import Path
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QMessageBox
from prose_to_video.gui.main_window import MainWindow
from prose_to_video.models.scene import Scene
from prose_to_video.pipeline.stills_workflow import Identity, Interval, Workflow, load_workflow
from prose_to_video.storage.project_files import create_project

app = QApplication([])
window = MainWindow()
session = create_project(Path(sys.argv[1]), 'undo')
session = type(session)(session.root, session.project.model_copy(update={'audio_duration': 10.0}))
session.database.save(session.project)
window.load(session)
person = Identity(kind='Character', name='Mira', description='Original')
window.state.catalogs = [person]
window.state.scenes = [Scene(sequence=i+1, start=i, end=i+1, text='A',
    title=f'Scene {i+1}', identity_links=[person.identifier]) for i in range(2)]
a, b = window.state.scenes
window.state.timeline = [Interval(scene_id=a.identifier, start=0, end=5, speech_start=0, speech_end=5),
                         Interval(scene_id=b.identifier, start=5, end=10, speech_start=5, speech_end=10)]
window.state.save(session.root)
window.refresh()
window.show()
window.workspace_tabs.setCurrentWidget(window.scenes_page)
window.scene_list.setCurrentRow(0)
editor = window.fields['title']
editor.setFocus()
app.processEvents()
QTest.keyClick(editor, Qt.Key_A, Qt.ControlModifier)
QTest.keyClicks(editor, 'Changed title')
assert window.state.scenes[0].title == 'Changed title'
# Undo groups consecutive typing and beats the text editor's native shortcut.
QTest.keyClick(editor, Qt.Key_Z, Qt.ControlModifier)
assert window.state.scenes[0].title == 'Scene 1'
assert editor.toPlainText() == 'Scene 1'
assert load_workflow(session).scenes[0].title == 'Scene 1'
assert not window.undo_action.isEnabled()

# History survives scene navigation and does not record refresh/population.
editor.setPlainText('Revised')
window.scene_list.setCurrentRow(1)
window.refresh()
window.undo_action.trigger()
assert window.state.scenes[0].title == 'Scene 1'
assert window.scene_list.currentRow() == 0
assert not window._undo_history

# Invalid drafts can be undone without validation blocking the shortcut.
window.fields['render_type'].setPlainText('invalid')
assert window._scene_drafts
window.undo()
assert not window._scene_drafts
assert window.fields['render_type'].toPlainText() == 'cinematic'

# Description edits and character removal restore manual flags and scene links.
window.catalog_edits['Character'].setPlainText('New description')
window.undo()
assert window.identity('Character').description == 'Original'
assert not window.identity('Character').manual
window.remove_character()
assert not window.state.catalogs
window.undo()
assert window.state.catalogs[0].identifier == person.identifier
assert all(person.identifier in s.identity_links for s in window.state.scenes)
assert load_workflow(session).catalogs[0].identifier == person.identifier

# Markers restore both sides of the boundary and their manual flags.
window.move_still_boundary(1, 6)
assert window.state.timeline[0].end == 6
window.undo()
assert window.audio.waveform.boundaries == [0, 5, 10]
assert all(not t.manual for t in window.state.timeline)
assert load_workflow(session).timeline[1].start == 5

# Undo discards an invalid table draft, then can undo a saved table transaction.
window.timeline.item(0, 2).setText('bad')
window.undo()
assert not window._timeline_dirty
assert float(window.timeline.item(0, 2).text()) == 5
window.timeline.item(0, 2).setText('6')
window.timeline.item(1, 1).setText('6')
window.save_timeline()
window.undo()
assert window.state.timeline[0].end == window.state.timeline[1].start == 5
assert not window._undo_history

# A failed undo save preserves the current state and the retryable history.
window.move_still_boundary(1, 7)
errors = []
QMessageBox.warning = lambda *args: errors.append(str(args[-1]))
original_save = Workflow.save
def fail(*args): raise OSError('Disk unavailable')
Workflow.save = fail
window.undo()
Workflow.save = original_save
assert errors == ['Disk unavailable']
assert window.state.timeline[0].end == 7
assert len(window._undo_history) == 1
window.undo()
assert window.state.timeline[0].end == 5

# Workers own the state until finished; their changes undo as one operation.
from PySide6.QtTest import QSignalSpy
def job(cancel, progress):
    window.state.scenes[0].title = 'Generated title'
    window.state.save(session.root)
window.run(job)
assert not window.undo_action.isEnabled()
window.undo()
spy = QSignalSpy(window.worker.finished)
assert spy.wait(5000)
app.processEvents()
assert window.worker is None
window.undo()
assert window.state.scenes[0].title == 'Scene 1'

# Structural scene changes undo together with renumbering.
window.state.timeline = []
window.refresh()
window.scene_list.setCurrentRow(0)
window.merge_scene()
assert len(window.state.scenes) == 1
window.undo()
assert [s.identifier for s in window.state.scenes] == [a.identifier, b.identifier]
assert [s.sequence for s in load_workflow(session).scenes] == [1, 2]

# History belongs to the open project only.
window.fields['title'].setPlainText('Another edit')
other = create_project(Path(sys.argv[1]), 'other')
window.load(other)
assert not window._undo_history
assert not window.undo_action.isEnabled()
window.undo()
assert not window.state.scenes
assert window.close()
'''
    result = subprocess.run([sys.executable, '-c', script, str(tmp_path)],
                            capture_output=True, text=True, timeout=30,
                            env={**os.environ, 'QT_QPA_PLATFORM': 'offscreen',
                                 'XDG_CONFIG_HOME': str(tmp_path / 'config')})
    assert result.returncode == 0, result.stderr
