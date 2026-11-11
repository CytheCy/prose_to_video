"""World building files are copied into a project and shown in Start."""
import os
import subprocess
import sys


def test_world_building_file_list_persists_and_stops_at_three(tmp_path):
    env = {**os.environ, 'QT_QPA_PLATFORM': 'offscreen',
           'XDG_CONFIG_HOME': str(tmp_path / 'config')}
    script = """
import sys
from pathlib import Path
from PySide6.QtWidgets import QApplication, QMessageBox
from prose_to_video.gui import stills_window as gui
from prose_to_video.storage.project_files import create_project, open_project

app = QApplication([])
root = Path(sys.argv[1])
window = gui.MainWindow()
session = create_project(root, 'Story')
window.load(session)
sources = []
for name in ('place.txt', 'history.md', 'characters.pdf', 'extra.txt'):
    path = root / name
    path.write_text(name)
    sources.append(path)
selected = iter(([str(path) for path in sources[:2]], [str(sources[2])], [str(sources[3])]))
gui.QFileDialog.getOpenFileNames = lambda *args: (next(selected), '')
warnings = []
gui.QMessageBox.warning = lambda *args: warnings.append(args[-1])
window.add_world_building_files()
window.add_world_building_files()
assert [window.world_building_list.item(i).text() for i in range(3)] == [
    'characters.pdf', 'history.md', 'place.txt']
assert all((session.root / 'world_building' / path.name).read_text() == path.name
           for path in sources[:3])
window.add_world_building_files()
assert len(warnings) == 1 and 'up to three' in warnings[0]
assert not (session.root / 'world_building' / 'extra.txt').exists()
window.load(create_project(root, 'Other'))
assert window.world_building_list.count() == 0
window.load(open_project(session.root))
assert window.world_building_list.count() == 3
assert not window.delete_world_building_button.isEnabled()
window.world_building_list.setCurrentRow(1)
assert window.delete_world_building_button.isEnabled()
questions = []
answers = iter((QMessageBox.No, QMessageBox.Yes))
def confirm(*args):
    questions.append((args[1], args[2]))
    return next(answers)
QMessageBox.question = confirm
window.delete_world_building_button.click()
assert (session.root / 'world_building' / 'history.md').exists()
assert window.world_building_list.count() == 3
window.delete_world_building_button.click()
assert questions == [('Delete history.md', 'Are you sure?')] * 2
assert not (session.root / 'world_building' / 'history.md').exists()
assert sources[1].exists()
assert [window.world_building_list.item(i).text() for i in range(2)] == [
    'characters.pdf', 'place.txt']
assert not window.delete_world_building_button.isEnabled()
window.load(open_project(session.root))
assert window.world_building_list.count() == 2
window.add_world_building_files()
assert window.world_building_list.count() == 3
assert (session.root / 'world_building' / 'extra.txt').exists()
window.close()
"""
    result = subprocess.run([sys.executable, '-c', script, str(tmp_path)],
                            capture_output=True, text=True, timeout=30, env=env)
    assert result.returncode == 0, result.stderr
