"""The active workspace remembers successful project loads across app launches."""
import os
import subprocess
import sys


def test_last_project_restored_on_startup(tmp_path):
    env = {**os.environ, 'QT_QPA_PLATFORM': 'offscreen',
           'XDG_CONFIG_HOME': str(tmp_path / 'config')}
    setup = """
import sys
from pathlib import Path
from PySide6.QtWidgets import QApplication
from prose_to_video.gui.main_window import MainWindow
from prose_to_video.storage.project_files import create_project, import_prose

app = QApplication([])
root = Path(sys.argv[1])
window = MainWindow()
assert window.session is None
first = create_project(root, 'First')
window.load(first)
source = root / 'story.txt'
source.write_text('Mira waits by the door.', encoding='utf-8')
second = import_prose(create_project(root, 'Second'), source)
window.load(second)
window.state.style = 'Watercolor'
window.state.save(second.root)
window.close()
"""
    restore = """
import sys
from pathlib import Path
from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QApplication
from prose_to_video.gui.main_window import MainWindow

app = QApplication([])
root = Path(sys.argv[1])
window = MainWindow()
assert window.session.root == root / 'Second'
assert window.state.style == 'Watercolor'
assert window.source_view._source_text == 'Mira waits by the door.'
window.close()

# Unavailable or corrupt projects must not block startup or replace the history.
settings = QSettings('ProseToVideo', 'ProseToVideo')
corrupt = root / 'Corrupt'
corrupt.mkdir()
recent = [str(root / 'Missing'), str(corrupt), str(root / 'Second')]
settings.setValue('projects/recent', recent)
settings.sync()
window = MainWindow()
assert window.session.root == root / 'Second'
window.close()

# A failed workflow load must also allow startup to finish without a project.
(root / 'Second' / 'stills-workflow.json').write_text('invalid json', encoding='utf-8')
settings.setValue('projects/recent', recent)
settings.sync()
window = MainWindow()
assert window.session is None
assert not window.state.scenes
assert 'Create or open a project' in window.status.text()
assert settings.value('projects/recent', [], type=list) == recent
window.close()
"""
    for script in (setup, restore):
        result = subprocess.run([sys.executable, '-c', script, str(tmp_path)],
                                capture_output=True, text=True, timeout=30, env=env)
        assert result.returncode == 0, result.stderr
