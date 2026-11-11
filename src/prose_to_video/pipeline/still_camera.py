"""Resolve movement IDs into bounded viewports. Directions refer to the camera."""
import hashlib
from prose_to_video.models.video_render import CameraFrame


def still_camera(label, settings, focal_x=.5, focal_y=.5, seed=''):
    amount = {'None': 0, 'Subtle': .06, 'Moderate': .12, 'Strong': .20,
              'Custom': settings.custom_motion}[settings.motion_intensity]
    if label == 'static_hold' or not amount:
        return CameraFrame()
    frame = CameraFrame(kind=label)
    if label == 'slow_drift':
        direction = int(hashlib.sha256(seed.encode()).hexdigest()[:8], 16) % 4
        dx, dy = [(-1, 0), (1, 0), (0, -1), (0, 1)][direction]
        amount /= 3
    else:
        dx = -1 if 'left' in label else 1 if 'right' in label else 0
        dy = -1 if 'up' in label else 1 if 'down' in label else 0
    if label.startswith('pan_') or '_pan_' in label or label == 'slow_drift':
        frame.start_x, frame.end_x = .5 - dx*.5, .5 + dx*.5
        frame.start_y, frame.end_y = .5 - dy*.5, .5 + dy*.5
        frame.start_zoom = frame.end_zoom = 1 + amount
        if label.startswith('zoom_in_'):
            frame.end_zoom += amount / 2
        elif label.startswith('zoom_out_'):
            frame.start_zoom += amount / 2
        return frame
    if label in {'zoom_in_focal_point', 'zoom_out_from_focal_point'}:
        frame.start_x = frame.end_x = focal_x
        frame.start_y = frame.end_y = focal_y
    elif label.startswith('zoom_in_'):
        frame.start_x = frame.end_x = .2 if 'left' in label else .8 if 'right' in label else .5
        frame.start_y = frame.end_y = .2 if 'top' in label else .8 if 'bottom' in label else .5
    if label.startswith('zoom_out'):
        frame.start_zoom = 1 + amount
    else:
        frame.end_zoom = 1 + amount
    return frame
