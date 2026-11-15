"""Video production uses one exported pair and preserves timing independently per part."""
import os
import subprocess
import sys

import pytest


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
def test_video_part_selection_and_sources(tmp_path, newline):
    script = r'''
import sys
import wave
from pathlib import Path
from threading import Event
from PySide6.QtCore import Qt
from PySide6.QtGui import QImage
from PySide6.QtWidgets import QApplication, QFileDialog
from podcast_splitter.core import Project, parse_prose, fingerprint
from podcast_splitter.engine import export
from prose_to_video.gui.main_window import MainWindow
from prose_to_video.models.scene import Scene, source_hash, validate_partition
from prose_to_video.pipeline.stills_workflow import Workflow, Still, Interval, load_workflow, render_plan
from prose_to_video.pipeline.audio_transcription import source_file_hash
from prose_to_video.pipeline.manuscript import parse_manuscript
from prose_to_video.storage.project_files import create_project, import_prose, import_audio

app = QApplication([])
root = Path(sys.argv[1])
prose = root / 'story.md'
text = '# 01 First\n\nFirst words. More words.\n\n# 02 Second\n\nSecond words.\n'.replace('\n', sys.argv[2])
prose.write_text(text)
audio = root / 'all.wav'
with wave.open(str(audio), 'wb') as stream:
    stream.setparams((1, 2, 8000, 0, 'NONE', 'not compressed'))
    stream.writeframes(b'\0\0' * 48000)
session = import_audio(import_prose(create_project(root, 'Story'), prose), audio)
sections = parse_prose(str(prose))
for i, section in enumerate(sections):
    section.start, section.end, section.reviewed = i * 3, i * 3 + 2, True
splitter = Project(prose=str(prose), audio=str(audio), output=str(session.root / 'output/chapters'),
                   duration=6, sections=sections, audio_fingerprint=fingerprint(str(audio)))
export(splitter, 'wav')
parts = parse_manuscript(text)
a = text.index('First words.')
b = text.index('More words.')
c = text.index('Second words.')
scenes = [Scene(sequence=i+1, start=start, end=end, text=text[start:end], title=f'Scene {i+1}')
          for i, (start, end) in enumerate([(a, b), (b, b+len('More words.')), (c, c+len('Second words.'))])]
state = Workflow(prose_hash=source_hash(text), scenes=scenes)
for i, scene in enumerate(scenes):
    path = session.root / 'images' / f'{i}.png'
    image = QImage(64, 64, QImage.Format_RGB32)
    image.fill(0xff123456)
    assert image.save(str(path))
    still = Still(scene_id=scene.identifier, source_hash=state.prose_hash, start=scene.start,
                  end=scene.end, excerpt=scene.text, path=f'images/{i}.png', prompt='Scene')
    state.stills.append(still)
    state.selected[scene.identifier] = still.identifier
state.save(session.root)
original = (session.root / 'stills-workflow.json').read_bytes()
window = MainWindow()
window.load(session)
assert window.video_parts.count() == 2
assert not window.video_parts.selectedItems()
assert window.audio._loaded_path is None
window.video_parts.setCurrentRow(0)
assert len(window.video_parts.selectedItems()) == 1
assert window._video_part_id == parts[0].identifier, window.video_sources.text()
assert window.production_session.project.audio_path == 'output/chapters/01 First.wav'
assert window.audio._loaded_path == session.root / 'output/chapters/01 First.wav'
assert abs(window.production_session.project.audio_duration - 2) < .01
assert window.video_prose._source_text == 'First words. More words.\n'
assert [s.identifier for s in window.production_state.scenes] == [s.identifier for s in scenes[:2]]
validate_partition(window.video_prose._source_text, window.production_state.scenes)
for scene in window.production_state.scenes:
    assert window.video_prose._source_text[scene.start:scene.end] == scene.text
assert window.session.project.audio_path == 'source/all.wav'
assert window.session.project.prose_path == 'source/story.md'

# Build receives only this part, and its state saves to its own location.
import prose_to_video.gui.stills_window as gui
calls = []
def assemble(session, state, *args, **kwargs):
    calls.append((session, state))
    state.timeline = [Interval(scene_id=s.identifier, start=i, end=i+1,
                     speech_start=i, speech_end=i+1) for i, s in enumerate(state.scenes)]
    state.timeline_audio_hash = source_file_hash(session.project.resolved_audio(session.root))
    state.save(session.root)
    return state.timeline
original_assemble = gui.assemble_timeline
gui.assemble_timeline = assemble
window.run = lambda work, on_result=None, **kwargs: work(Event(), lambda message: None)
window.build()
assert calls[0][0].project.audio_path == 'output/chapters/01 First.wav'
assert len(calls[0][1].scenes) == 2
window.refresh()
window.move_still_boundary(1, .75)
assert window.production_state.timeline[0].end == .75
window.undo()
assert window.production_state.timeline[0].end == 1
window.move_still_boundary(1, .75)
plan = render_plan(window.production_session, window.production_state)
assert plan.audio_path.endswith('/output/chapters/01 First.wav')
assert plan.audio_duration == 2
assert len(plan.segments) == 2
assert (session.root / 'stills-workflow.json').read_bytes() == original
assert session.database.load_alignment() is None
assert window.production_session.database.path != session.database.path

window.video_parts.setCurrentRow(1)
assert window.production_session.project.audio_path == 'output/chapters/02 Second.wav'
assert len(window.production_state.scenes) == 1
assert window.production_state.scenes[0].identifier == scenes[2].identifier
assert window.production_state.scenes[0].sequence == 1
validate_partition(window.video_prose._source_text, window.production_state.scenes)
assert window.production_state.current_still(scenes[2].identifier).identifier == state.current_still(scenes[2].identifier).identifier
assert [s.sequence for s in state.scenes] == [1, 2, 3]
assert not window.production_state.timeline
window.video_parts.setCurrentRow(0)
assert window.production_state.timeline[0].end == .75
window.load(session)
window.video_parts.setCurrentRow(0)
assert window.production_state.timeline[0].end == .75

# Missing split files clear the previous part; never use the full imported narration.
(session.root / 'output/chapters/02 Second.wav').unlink()
window.video_parts.setCurrentRow(1)
assert window._video_session is None
assert window.audio._loaded_path is None
assert window.timeline.rowCount() == 0
assert 'Podcast Splitter' in window.video_sources.text()
assert not window.video_parts.selectedItems()
window.video_parts.setCurrentRow(0)
window.story_parts.item(0).setCheckState(Qt.Unchecked)
assert window.video_parts.count() == 1
assert window._video_session is None
assert window.audio._loaded_path is None
assert window.timeline.rowCount() == 0
assert load_workflow(session).timeline == []
assert window.close()
'''
    result = subprocess.run(
        [sys.executable, '-c', script, str(tmp_path), newline], capture_output=True, text=True, timeout=40,
        env={**os.environ, 'QT_QPA_PLATFORM': 'offscreen', 'XDG_CONFIG_HOME': str(tmp_path / 'config')})
    assert result.returncode == 0, result.stdout + result.stderr
