"""Scene movement persistence and actual rendered frame coverage."""
import shutil
import subprocess

import pytest
from PySide6.QtGui import QImage

from prose_to_video.models.camera_movement import CAMERA_MOVEMENT_IDS
from prose_to_video.models.scene import Scene
from prose_to_video.models.video_render import PlannedSegment, RenderSettings
from prose_to_video.pipeline.still_camera import still_camera
from prose_to_video.pipeline.stills_workflow import Workflow, dependency
from prose_to_video.rendering.ffmpeg_renderer import image_filter


def test_movement_does_not_stale_still_prompt():
    scene = Scene(sequence=1, start=0, end=1, text='A')
    state = Workflow(scenes=[scene])
    original = dependency(state, scene)
    scene.camera_movement = 'zoom_in_focal_point'
    scene.camera_focal_x = .1
    scene.camera_focal_y = .9
    assert dependency(state, scene) == original
    restored = Workflow.model_validate_json(state.model_dump_json()).scenes[0]
    assert (restored.camera_movement, restored.camera_focal_x, restored.camera_focal_y) == ('zoom_in_focal_point', .1, .9)
    with pytest.raises(ValueError):
        Scene.model_validate(scene.model_dump() | {'camera_focal_x': 1.1})
    with pytest.raises(ValueError):
        Scene.model_validate(scene.model_dump() | {'camera_movement': 'invalid'})


@pytest.mark.parametrize('label', CAMERA_MOVEMENT_IDS)
def test_camera_path_stays_bounded(label):
    camera = still_camera(label, RenderSettings(motion_intensity='Strong'), .1, .9, 'scene')
    assert 1 <= camera.start_zoom <= 1.3
    assert 1 <= camera.end_zoom <= 1.3
    for name in ('start_x', 'end_x', 'start_y', 'end_y'):
        assert 0 <= getattr(camera, name) <= 1
    if 'pan_left' in label or label == 'pan_left':
        assert camera.end_x < camera.start_x
    if 'pan_right' in label or label == 'pan_right':
        assert camera.end_x > camera.start_x
    if label == 'zoom_out':
        assert camera.start_zoom > camera.end_zoom
    if label == 'zoom_in_focal_point':
        assert camera.start_x == camera.end_x == .1
        assert camera.start_y == camera.end_y == .9
    assert still_camera('slow_drift', RenderSettings(), seed='scene') == still_camera('slow_drift', RenderSettings(), seed='scene')


@pytest.mark.skipif(not shutil.which('ffmpeg'), reason='FFmpeg unavailable')
@pytest.mark.parametrize('size', [(90, 160), (250, 70)])
def test_rendered_moving_images_never_expose_gaps(tmp_path, size):
    image = QImage(*size, QImage.Format_RGB32); image.fill(0xffff0000)
    path = tmp_path / 'red.png'; assert image.save(str(path))
    settings = RenderSettings(width=160, height=90, frame_rate=6, motion_intensity='Strong')
    for label in CAMERA_MOVEMENT_IDS:
        if label == 'static_hold':
            continue  # Static holds retain the user's letterbox setting.
        segment = PlannedSegment(scene_id='s', shot_id='s', source_start=0, source_end=1,
            start_frame=0, end_frame=6, width=160, height=90, frame_rate=6, fit_policy='fit',
            camera=still_camera(label, settings, 0, 1, 'scene'))
        result = subprocess.run(['ffmpeg', '-v', 'error', '-i', str(path), '-vf', image_filter(segment, 'black'),
            '-frames:v', '6', '-pix_fmt', 'rgb24', '-f', 'rawvideo', '-'], capture_output=True, timeout=20)
        assert result.returncode == 0, (label, result.stderr.decode())
        assert len(result.stdout) == 160 * 90 * 3 * 6
        # Inspect every pixel of every frame, including corners and end frames.
        assert min(result.stdout[0::3]) > 240, label
        assert max(result.stdout[1::3]) < 10, label
        assert max(result.stdout[2::3]) < 10, label


def test_movement_editor_persists_and_undoes(tmp_path):
    import os
    import sys
    script = r'''
import sys
from pathlib import Path
from PySide6.QtWidgets import QApplication
from prose_to_video.gui.main_window import MainWindow
from prose_to_video.models.scene import Scene
from prose_to_video.pipeline.stills_workflow import load_workflow
from prose_to_video.storage.project_files import create_project
app = QApplication([])
window = MainWindow()
session = create_project(Path(sys.argv[1]), 'camera')
window.load(session)
window.state.scenes = [Scene(sequence=1, start=0, end=1, text='A')]
window.state.save(session.root)
window.refresh()
window.scene_list.setCurrentRow(0)
scene = window.state.scenes[0]
fields = list(window.fields)
assert fields.index('camera_movement') == fields.index('camera_position') + 1
box = window.camera_movement_boxes[scene.identifier]
box.setCurrentText('pan_up_left')
assert load_workflow(session).scenes[0].camera_movement == 'pan_up_left'
assert window.fields['camera_movement'].toPlainText() == 'pan_up_left'
window.undo()
assert load_workflow(session).scenes[0].camera_movement == 'static_hold'
assert window.camera_movement_boxes[scene.identifier].currentText() == 'static_hold'
window.fields['camera_movement'].setPlainText('zoom_in_focal_point')
window.update_scene_camera(scene.identifier, 'camera_focal_x', .2)
assert load_workflow(session).scenes[0].camera_focal_x == .2
assert window.close()
'''
    result = subprocess.run([sys.executable, '-c', script, str(tmp_path)], capture_output=True,
        text=True, timeout=30, env={**os.environ, 'QT_QPA_PLATFORM': 'offscreen',
                                 'XDG_CONFIG_HOME': str(tmp_path / 'config')})
    assert result.returncode == 0, result.stderr
