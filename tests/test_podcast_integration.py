"""The embedded editor shares sources and survives project relocation."""
import os
import subprocess
import sys


def test_shared_project_round_trip(tmp_path):
    script = r'''
import json
import shutil
import sys
import wave
from pathlib import Path
from PySide6.QtWidgets import QApplication
from prose_to_video.gui.main_window import MainWindow
from prose_to_video.storage.project_files import create_project

app = QApplication([])
root = Path(sys.argv[1])
window = MainWindow()
window.load(create_project(root, 'Story'))
prose = root / 'story.md'
prose.write_text('# One\nThis is the first chapter.\n\n# Two\nThis is the next chapter.')
audio = root / 'narration.wav'
with wave.open(str(audio), 'wb') as stream:
    stream.setnchannels(1)
    stream.setsampwidth(2)
    stream.setframerate(8000)
    stream.writeframes(b'\0\0' * 80000)
panel = window.podcast
panel.choose_prose(str(prose))
panel.choose_audio(str(audio))
assert window.session.project.prose_path == 'source/story.md'
assert panel.project.audio == str(window.session.root / 'source/narration.wav')
assert 'first chapter' in window.source_view._source_text
panel.project.duration = 10
panel.project.waveform = [.1, .2, .3]
section = panel.project.sections[0]
section.start, section.end = 1, 4
section.reviewed = True
panel.project.sections[1].included = False
from podcast_splitter.core import fingerprint
panel.project.audio_fingerprint = fingerprint(panel.project.audio)
panel.refresh()
panel.part_padding.setValue(.25)
panel.format.setCurrentText('FLAC')
panel.model.setCurrentIndex(2)
window.save_project()
data = json.loads((window.session.root / 'podcast-splitter.json').read_text())
assert data['audio'] == 'source/narration.wav'
assert data['prose'] == 'source/story.md'
assert data['output'] == 'output/chapters'
old = window.session.root
window.load(create_project(root, 'Other'))
assert not panel.project.sections
assert panel.project.part_padding == 0
assert panel.format.currentText() == 'MP3'
moved = root / 'Moved'
shutil.move(old, moved)
prose.unlink()
audio.unlink()
from prose_to_video.storage.project_files import open_project
window.load(open_project(moved))
assert panel.project.audio == str(moved / 'source/narration.wav')
assert Path(panel.project.audio).is_file()
assert panel.project.cut_points(panel.project.sections[0]) == (.75, 4.25)
assert panel.project.sections[0].reviewed
assert panel.project.waveform == [.1, .2, .3]
assert panel.model.currentData() == 'small.en'
assert panel.format.currentText() == 'FLAC'
assert panel.project.output == str(moved / 'output/chapters')
# Exercise the embedded subprocess with only project-owned files after moving.
import time
panel.format.setCurrentText('WAV')
errors = []
panel.error = errors.append
panel.export()
deadline = time.monotonic() + 15
while panel.process and time.monotonic() < deadline:
    app.processEvents()
    time.sleep(.01)
assert panel.process is None, 'Audio worker did not finish'
assert not errors, errors
exported = moved / 'output/chapters/01 One.wav'
assert exported.is_file()
assert exported.with_suffix('.md').is_file()
with wave.open(str(exported), 'rb') as stream:
    assert stream.getnframes() / stream.getframerate() == 3.5
# Imports from the video tab invalidate the same splitter source state.
replacement = root / 'new.md'
replacement.write_text('# Replacement\nA different chapter.')
from prose_to_video.storage.project_files import import_prose
window.source_imported(import_prose(window.session, replacement), 'prose')
assert [s.title for s in panel.project.sections] == ['Replacement']
window.close()
'''
    result = subprocess.run(
        [sys.executable, '-c', script, str(tmp_path)], capture_output=True,
        text=True, timeout=30,
        env={**os.environ, 'QT_QPA_PLATFORM': 'offscreen',
             'XDG_CONFIG_HOME': str(tmp_path / 'config')},
    )
    assert result.returncode == 0, result.stderr
