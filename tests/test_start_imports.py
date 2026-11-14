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
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication, QFileDialog, QPushButton, QDialogButtonBox
from prose_to_video.gui.main_window import MainWindow
from prose_to_video.gui.create_project_dialog import CreateProjectDialog
app = QApplication([])
root = Path(sys.argv[1])
window = MainWindow()
buttons = {b.text(): b for b in window.start_page.findChildren(QPushButton)}
assert not buttons['Import Prose'].isEnabled()

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
prose.write_text('# Chapter\nThe traveler arrived.')
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
