"""Source imports update both prose viewers, including fast worker completion."""
import os
import subprocess
import sys

import pytest


@pytest.mark.parametrize('immediate', [False, True])
def test_import_updates_start_and_scenes(tmp_path, immediate):
    env = {**os.environ, 'QT_QPA_PLATFORM': 'offscreen',
           'XDG_CONFIG_HOME': str(tmp_path / 'config')}
    script = """
import sys
import time
from pathlib import Path
from PySide6.QtWidgets import QApplication
import prose_to_video.gui.stills_window as gui
from prose_to_video.storage.project_files import create_project, open_project

app = QApplication([])
root = Path(sys.argv[1])
if sys.argv[2] == 'True':
    class ImmediateTask(gui.Task):
        def start(self):
            self.run()
            self.finished.emit()
    gui.Task = ImmediateTask

window = gui.MainWindow()
window.load(create_project(root, 'Story'))
errors = []
gui.QMessageBox.warning = lambda *args: errors.append(args[-1])

def wait_until(predicate):
    deadline = time.monotonic() + 5
    while not predicate() and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(.005)
    assert predicate(), (errors, window.status.text())

try:
    for name, text in [('first.txt', 'Mira waits.\\n' * 15000),
                       ('first.txt', 'The same source file has now been edited.'),
                       ('replacement.md', 'A different, shorter story.')]:
        source = root / name
        source.write_text(text, encoding='utf-8')
        gui.QFileDialog.getOpenFileName = lambda *args: (str(source), '')
        window.import_source('prose')
        wait_until(lambda: window.worker is None
                   and window.source_view.toPlainText() == text
                   and window.prose.toPlainText() == text)
        assert not errors
        assert window.session.project.prose_path == 'source/' + name
        assert open_project(window.session.root).project.prose_path == 'source/' + name

    # Switching to a project without prose must clear the previous story.
    window.load(create_project(root, 'Empty'))
    app.processEvents()
    assert window.source_view.toPlainText() == ''
    assert window.prose.toPlainText() == ''
finally:
    window.close()
"""
    result = subprocess.run([sys.executable, '-c', script, str(tmp_path), str(immediate)],
                            capture_output=True, text=True, timeout=30, env=env)
    assert result.returncode == 0, result.stderr
