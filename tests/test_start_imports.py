"""Create a shared project and copy both sources using the Start controls."""
import os
import subprocess
import sys


def test_start_project_and_import_buttons(tmp_path):
    script = r'''
import sys
import time
import wave
from pathlib import Path
from PySide6.QtCore import QTimer, Qt
from PySide6.QtWidgets import QApplication, QFileDialog, QPushButton, QDialogButtonBox
from prose_to_video.gui.main_window import MainWindow
from prose_to_video.gui.create_project_dialog import CreateProjectDialog
app = QApplication([])
root = Path(sys.argv[1])
window = MainWindow()
buttons = {b.text(): b for b in window.start_page.findChildren(QPushButton)}
assert not buttons['Import Prose'].isEnabled()
assert 'Import Prose' not in [b.text() for b in window.start.findChildren(QPushButton)]
assert window.story_parts.count() == 0
assert window.selected_story_parts.count() == 0
assert 'Choose parts on Start' in window.selected_parts_summary.text()
assert 'Analyze Checked Parts' not in buttons
assert not window.analyze_story_button.isEnabled()
assert not buttons['Deselect All'].isEnabled()

def fill_dialog():
    dialog = app.activeModalWidget()
    assert isinstance(dialog, CreateProjectDialog)
    dialog.location.setText(str(root))
    dialog.name.setText('My Project')
    assert not (root / 'My Project').exists()
    dialog.buttons.button(QDialogButtonBox.Save).click()
QTimer.singleShot(0, fill_dialog)
buttons['Create Project'].click()
assert window.session.root == root / 'My Project'
assert (window.session.root / 'project.sqlite').is_file()
assert buttons['Import Prose'].isEnabled()
prose = root / 'narrative.md'
prose.write_text('# Chapter One\nThe traveler arrived.\n\n# Chapter Two\nThe traveler left.')
audio = root / 'podcast.wav'
with wave.open(str(audio), 'wb') as stream:
    stream.setparams((1, 2, 8000, 0, 'NONE', 'not compressed'))
    stream.writeframes(b'\0\0' * 8000)
for label, source in [('Import Prose', prose), ('Import Podcast Audio', audio)]:
    QFileDialog.getOpenFileName = lambda *args: (str(source), '')
    buttons[label].click()
    deadline = time.monotonic() + 10
    while window.worker and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(.01)
    assert window.worker is None
    assert (window.session.root / 'source' / source.name).read_bytes() == source.read_bytes()
assert window.podcast.project.prose == str(window.session.root / 'source/narrative.md')
assert window.podcast.project.audio == str(window.session.root / 'source/podcast.wav')
assert 'narrative.md' in window.project_summary.text()
assert 'podcast.wav' in window.project_summary.text()
assert window.story_parts.count() == 2
assert 'Chapter One' in window.story_parts.item(0).text()
assert window.story_parts.item(1).checkState() == Qt.Checked
assert [window.selected_story_parts.item(i).text() for i in range(2)] == [
    window.story_parts.item(i).text() for i in range(2)]
app.processEvents()
assert not window.source_view._source_text
chapters = window.session.root / 'output/chapters'
chapters.mkdir(parents=True)
(chapters / 'Chapter One.md').write_text('The traveler arrived.\n\n')
(chapters / 'Chapter Two.md').write_text('The traveler left.')
window.refresh()
app.processEvents()
assert window.source_view.toPlainText() == 'The traveler arrived.\n\n\n\nThe traveler left.'
assert window.prose.toPlainText() == prose.read_text()
window.story_parts.item(1).setCheckState(Qt.Unchecked)
app.processEvents()
assert window.source_view.toPlainText() == 'The traveler arrived.\n\n'
assert window.selected_story_parts.count() == 1
assert window.selected_story_parts.item(0).text() == window.story_parts.item(0).text()
window.refresh()
app.processEvents()
assert window.source_view.toPlainText() == 'The traveler arrived.\n\n'
assert window.story_parts.item(1).checkState() == Qt.Unchecked
assert '1 of 2' in window.parts_help.text()
window.load(window.session)
app.processEvents()
assert window.source_view.toPlainText() == 'The traveler arrived.\n\n'
assert window.story_parts.item(1).checkState() == Qt.Unchecked
assert window.selected_story_parts.count() == 1
assert window.selected_story_parts.item(0).text() == window.story_parts.item(0).text()
assert buttons['Deselect All'].isEnabled()
window.story_parts.item(1).setCheckState(Qt.Checked)
buttons['Deselect All'].click()
app.processEvents()
assert window.source_view.toPlainText() == ''
assert not window.analyze_story_button.isEnabled()
assert all(window.story_parts.item(i).checkState() == Qt.Unchecked
           for i in range(window.story_parts.count()))
assert '0 of 2' in window.parts_help.text()
assert window.selected_story_parts.count() == 0
assert 'No story parts selected' in window.selected_parts_summary.text()
assert not window.analyze_story_button.isEnabled()
assert not buttons['Deselect All'].isEnabled()
window.load(window.session)
assert all(window.story_parts.item(i).checkState() == Qt.Unchecked
           for i in range(window.story_parts.count()))
window.story_parts.item(0).setCheckState(Qt.Checked)
assert window.analyze_story_button.isEnabled()
assert buttons['Deselect All'].isEnabled()
assert window.selected_story_parts.count() == 1
assert window.selected_story_parts.item(0).text() == window.story_parts.item(0).text()
window.story_parts.item(0).setCheckState(Qt.Unchecked)
window.story_parts.item(1).setCheckState(Qt.Checked)
app.processEvents()
assert window.source_view.toPlainText() == 'The traveler left.'
assert window.analyze_story_button.isEnabled()
# An existing folder is preserved and the dialog remains open for correction.
dialog = CreateProjectDialog(window, root)
dialog.name.setText('My Project')
dialog.save()
assert dialog.session is None
assert 'already exists' in dialog.error.text()
window.close()
'''
    result = subprocess.run([sys.executable, '-c', script, str(tmp_path)],
                            capture_output=True, text=True, timeout=30,
                            env={**os.environ, 'QT_QPA_PLATFORM': 'offscreen',
                                 'XDG_CONFIG_HOME': str(tmp_path / 'config')})
    assert result.returncode == 0, result.stderr
