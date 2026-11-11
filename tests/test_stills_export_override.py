"""Changed narration requires a deliberate choice before using saved still cuts."""
import os
import subprocess
import sys


def test_export_anyway_choice(tmp_path):
    script = r'''
import sys
import wave
from pathlib import Path
from threading import Event
from PySide6.QtGui import QImage
from PySide6.QtWidgets import QApplication, QFileDialog, QMessageBox
from prose_to_video.gui.main_window import MainWindow
from prose_to_video.models.scene import Scene, source_hash
from prose_to_video.pipeline.audio_transcription import source_file_hash
from prose_to_video.pipeline.stills_workflow import Workflow, Still, Interval
from prose_to_video.rendering.ffmpeg_renderer import FFmpegRenderer
from prose_to_video.storage.project_files import create_project, import_prose, import_audio

app = QApplication([])
root = Path(sys.argv[1])
session = create_project(root, 'Story')
prose = root / 'story.txt'
prose.write_text('A', encoding='utf-8')
session = import_prose(session, prose)
audio = root / 'narration.wav'
with wave.open(str(audio), 'wb') as stream:
    stream.setnchannels(1)
    stream.setsampwidth(2)
    stream.setframerate(16000)
    stream.writeframes(b'\0\0' * 16000)
session = import_audio(session, audio)
scene = Scene(sequence=1, start=0, end=1, text='A')
image = QImage(64, 64, QImage.Format_RGB32)
image.fill(0xff123456)
still_path = session.root / 'images' / 'a.png'
image.save(str(still_path))
still = Still(scene_id=scene.identifier, source_hash=source_hash('A'), start=0, end=1,
              excerpt='A', path='images/a.png', prompt='A')
state = Workflow(prose_hash=source_hash('A'), scenes=[scene], stills=[still],
                 selected={scene.identifier: still.identifier}, timeline=[Interval(
                     scene_id=scene.identifier, start=0, end=1, speech_start=.1, speech_end=.9)],
                 timeline_audio_hash='old-audio')
state.save(session.root)
window = MainWindow()
window.load(session)
answers = iter((QMessageBox.No, QMessageBox.Yes))
prompts = []
QMessageBox.question = lambda *args: (prompts.append(args[1:3]), next(answers))[1]
paths = []
output = session.root / 'output' / 'video.mp4'
QFileDialog.getSaveFileName = lambda *args: (paths.append(args), (str(output), ''))[1]
plans = []
FFmpegRenderer.render = lambda self, plan, path, **kwargs: (plans.append(plan), {})[1]
window.run = lambda work, **kwargs: work(Event(), lambda message: None)
window.render()
assert len(prompts) == 1 and not paths and not plans
window.render()
assert len(prompts) == 2 and len(paths) == 1 and len(plans) == 1
assert plans[0].audio_fingerprint == source_file_hash(session.project.resolved_audio(session.root))
assert window.state.timeline_audio_hash == 'old-audio'
assert output.with_suffix('.mp4.json').is_file()
assert window.close()
'''
    env = {**os.environ, 'QT_QPA_PLATFORM': 'offscreen'}
    result = subprocess.run([sys.executable, '-c', script, str(tmp_path)],
                            capture_output=True, text=True, env=env)
    assert result.returncode == 0, result.stdout + result.stderr
