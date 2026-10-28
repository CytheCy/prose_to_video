"""Serializable, provider-independent instructions for a deterministic video render."""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator
from prose_to_video.models.shot import CAMERA_MOVEMENTS


class RenderSettings(BaseModel):
    model_config = ConfigDict(extra='forbid')
    width: int = Field(default=1920, ge=16, le=8192)
    height: int = Field(default=1080, ge=16, le=8192)
    frame_rate: int = Field(default=30, ge=1, le=120)
    video_codec: Literal['libx264'] = 'libx264'
    audio_codec: Literal['aac'] = 'aac'
    crf: int = Field(default=21, ge=0, le=51)
    preset: Literal['ultrafast', 'superfast', 'veryfast', 'faster', 'fast', 'medium', 'slow'] = 'medium'
    fit_policy: Literal['fit', 'crop'] = 'fit'
    background_color: str = 'black'
    motion_intensity: Literal['None', 'Subtle', 'Moderate', 'Strong', 'Custom'] = 'Subtle'
    custom_motion: float = Field(default=0.08, ge=0, le=0.30)
    default_transition: Literal['Cut', 'Crossfade', 'Fade through black'] = 'Cut'
    crossfade_duration: float = Field(default=0.5, ge=0, le=10)
    mode: Literal['final', 'draft'] = 'final'
    output_directory: str = 'renders'
    output_filename: str = 'video.mp4'
    pause_policy: Literal['storyboard', 'previous', 'next'] = 'storyboard'

    @model_validator(mode='after')
    def valid(self):
        if self.width % 2 or self.height % 2:
            raise ValueError('H.264 output dimensions must be even')
        if self.background_color != 'black' and not re.fullmatch(r'#?[0-9a-fA-F]{6}', self.background_color):
            raise ValueError('Background color must be black or six hex RGB digits')
        if Path(self.output_filename).name != self.output_filename or not self.output_filename.lower().endswith('.mp4'):
            raise ValueError('Output filename must be an MP4 filename')
        return self


class ValidationIssue(BaseModel):
    severity: Literal['error', 'warning']
    code: str
    message: str
    scene_id: str | None = None
    shot_id: str | None = None


class ValidationReport(BaseModel):
    issues: list[ValidationIssue] = Field(default_factory=list)

    @property
    def ready(self) -> bool:
        return not any(issue.severity == 'error' for issue in self.issues)

    def add(self, severity, code, message, scene_id=None, shot_id=None):
        self.issues.append(ValidationIssue(severity=severity, code=code, message=message,
                                           scene_id=scene_id, shot_id=shot_id))


class CameraFrame(BaseModel):
    kind: str = 'Static'
    start_x: float = 0.5
    start_y: float = 0.5
    end_x: float = 0.5
    end_y: float = 0.5
    start_zoom: float = 1.0
    end_zoom: float = 1.0
    fallback: str | None = None


class ShotRenderOverride(BaseModel):
    """Render-only instructions; changing these never changes image prompt freshness."""
    camera_movement: str | None = None
    camera_parameters: dict[str, float] | None = None
    transition: Literal['Cut', 'Crossfade', 'Fade through black', 'Fade in', 'Fade out'] | None = None
    transition_duration: float | None = Field(default=None, ge=0, le=10)

    @model_validator(mode='after')
    def valid(self):
        if self.camera_movement is not None and self.camera_movement not in CAMERA_MOVEMENTS:
            raise ValueError('Unsupported camera movement')
        if self.camera_parameters is not None:
            allowed = {'start_x','start_y','end_x','end_y','start_zoom','end_zoom'}
            if set(self.camera_parameters) != allowed:
                raise ValueError('Custom camera framing needs all six parameters')
            for name, value in self.camera_parameters.items():
                if not (1 <= value <= 1.3 if 'zoom' in name else 0 <= value <= 1):
                    raise ValueError('Custom camera framing is out of range')
        return self


class PlannedSegment(BaseModel):
    scene_id: str
    shot_id: str
    asset_id: str | None = None
    asset_kind: Literal['still', 'placeholder', 'video'] = 'still'
    source_path: str | None = None
    source_fingerprint: str | None = None
    dependency_fingerprint: str | None = None
    production_mode: Literal['Static Image','Camera Motion','AI Animation'] = 'Camera Motion'
    source_start: float
    source_end: float
    start_frame: int
    end_frame: int
    width: int
    height: int
    frame_rate: int
    fit_policy: Literal['fit', 'crop']
    camera: CameraFrame
    transition: Literal['Cut', 'Crossfade', 'Fade through black', 'Fade in', 'Fade out'] = 'Cut'
    transition_frames: int = 0

    @property
    def frames(self):
        return self.end_frame - self.start_frame

    @property
    def duration(self):
        return self.frames / self.frame_rate


class RenderPlan(BaseModel):
    version: int = 1
    project_id: str
    scope: Literal['full', 'scene', 'shot']
    mode: Literal['final', 'draft']
    source_start: float
    source_end: float
    audio_path: str
    audio_fingerprint: str
    audio_duration: float
    storyboard_ids: list[str] = Field(default_factory=list)
    settings: RenderSettings
    segments: list[PlannedSegment]
    issues: list[ValidationIssue] = Field(default_factory=list)

    def fingerprint(self):
        data = self.model_dump(mode='json', exclude={'issues'})
        return hashlib.sha256(json.dumps(data, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


class RenderJob(BaseModel):
    identifier: str = Field(default_factory=lambda: str(uuid4()))
    project_id: str
    scope: str
    mode: str
    settings: dict
    plan_fingerprint: str
    output_path: str
    status: Literal['Pending', 'Validating', 'Rendering', 'Completed', 'Failed', 'Canceled', 'Interrupted'] = 'Pending'
    started_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    completed_at: datetime | None = None
    error: str | None = None
    metadata: dict = Field(default_factory=dict)
