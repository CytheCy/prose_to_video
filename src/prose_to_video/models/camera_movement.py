"""Stable camera movement IDs shared by analysis, scene editing and rendering."""
from typing import Literal

CameraMovement = Literal[
    'static_hold', 'zoom_in', 'zoom_out',
    'pan_left', 'pan_right', 'pan_up', 'pan_down',
    'pan_up_left', 'pan_up_right', 'pan_down_left', 'pan_down_right',
    'zoom_in_pan_left', 'zoom_in_pan_right', 'zoom_in_pan_up', 'zoom_in_pan_down',
    'zoom_out_pan_left', 'zoom_out_pan_right', 'zoom_out_pan_up', 'zoom_out_pan_down',
    'zoom_in_top', 'zoom_in_bottom', 'zoom_in_left', 'zoom_in_right',
    'zoom_in_top_left', 'zoom_in_top_right', 'zoom_in_bottom_left', 'zoom_in_bottom_right',
    'zoom_in_focal_point', 'zoom_out_from_focal_point', 'slow_drift', 'breathe',
]
CAMERA_MOVEMENT_IDS = CameraMovement.__args__
CAMERA_RENDER_FIELDS = {'camera_movement', 'camera_focal_x', 'camera_focal_y'}
