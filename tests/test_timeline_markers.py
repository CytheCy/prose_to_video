"""Dragging an audio cut edits both stills and persists the shared boundary."""
import os
import subprocess
import sys


def test_drag_still_boundaries(tmp_path):
    script = r'''
import sys
from pathlib import Path
from PySide6.QtCore import QPoint, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QMessageBox
from prose_to_video.gui.main_window import MainWindow
from prose_to_video.pipeline.stills_workflow import Interval, Workflow, load_workflow
from prose_to_video.storage.project_files import create_project

app = QApplication([])
window = MainWindow()
session = create_project(Path(sys.argv[1]), 'markers')
session = type(session)(session.root, session.project.model_copy(update={'audio_duration': 15.0}))
session.database.save(session.project)
window.load(session)
window.state.timeline = [Interval(scene_id=str(i), start=i*5, end=(i+1)*5,
    speech_start=i*5, speech_end=(i+1)*5) for i in range(3)]
window.state.save(session.root)
window.refresh()
wave = window.audio.waveform
wave.resize(900, 90)
wave.show()
app.processEvents()
assert wave.boundaries == [0, 5, 10, 15]
seeks = []
wave.seek_requested.connect(seeks.append)
def drag(start, end):
    QTest.mousePress(wave, Qt.LeftButton, pos=QPoint(start, 8))
    QTest.mouseMove(wave, QPoint(end, 8))
    QTest.mouseRelease(wave, Qt.LeftButton, pos=QPoint(end, 8))

drag(300, 360)  # 5s -> 6s
assert not seeks  # Dragging a cut must not seek the audio.
assert wave.boundaries == [0, 6, 10, 15]
saved = load_workflow(session).timeline
assert saved[0].end == saved[1].start == 6
assert saved[0].manual and saved[1].manual and not saved[2].manual
assert saved[0].speech_end == saved[1].speech_start == 5
assert float(window.timeline.item(0, 2).text()) == 6
assert float(window.timeline.item(1, 1).text()) == 6

# Clicking slightly off-center on a marker must not change its timing.
drag(363, 363)
assert wave.boundaries == [0, 6, 10, 15]
assert not seeks

drag(360, 850)  # Cannot cross the next cut.
assert wave.boundaries == [0, 9.999, 10, 15]
drag(600, 0)  # Cannot cross the previous cut.
assert wave.boundaries == [0, 9.999, 10, 15]
wave.set_boundaries_editable(False)
drag(600, 720)
assert wave.boundaries == [0, 9.999, 10, 15]
wave.set_boundaries_editable(True)

# An ordinary click still seeks.
QTest.mouseClick(wave, Qt.LeftButton, pos=QPoint(180, 45))
assert seeks[-1] == 3

# Failed saves restore markers and leave both saved and in-memory timing intact.
errors = []
QMessageBox.warning = lambda *args: errors.append(str(args[-1]))
original_save = Workflow.save
def fail(*args): raise OSError('Disk unavailable')
Workflow.save = fail
window.move_still_boundary(1, 4)
Workflow.save = original_save
assert 'Disk unavailable' in errors[-1]
assert wave.boundaries == [0, 9.999, 10, 15]
assert window.state.timeline[0].end == load_workflow(session).timeline[0].end == 9.999
assert window.close()
'''
    result = subprocess.run([sys.executable, '-c', script, str(tmp_path)],
                            capture_output=True, text=True, timeout=30,
                            env={**os.environ, 'QT_QPA_PLATFORM': 'offscreen',
                                 'XDG_CONFIG_HOME': str(tmp_path / 'config')})
    assert result.returncode == 0, result.stderr
