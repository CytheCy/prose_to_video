"""Portable video candidate and versioned motion instructions."""
from __future__ import annotations
from datetime import datetime, timezone
from pathlib import PurePosixPath
from uuid import uuid4
from pydantic import BaseModel, Field, model_validator

def now(): return datetime.now(timezone.utc)
def uid(): return str(uuid4())

class MotionPrompt(BaseModel):
    identifier: str = Field(default_factory=uid)
    shot_id: str
    scene_id: str
    version: int = Field(ge=1)
    starting_image_id: str
    ending_image_id: str | None = None
    positive_prompt: str
    negative_prompt: str = ''
    subject_movements: str = ''
    object_movements: str = ''
    environmental_movements: str = ''
    camera_movement: str = 'Stationary'
    intended_duration: float = Field(gt=0)
    continuity_constraints: str = ''
    settings: dict = Field(default_factory=dict)
    review_status: str = 'Draft'
    dependency_fingerprint: str
    created_at: datetime = Field(default_factory=now)
    modified_at: datetime = Field(default_factory=now)

    @model_validator(mode='after')
    def valid(self):
        if not self.positive_prompt.strip() or self.review_status not in {'Draft','Reviewed','Approved','Rejected'}:
            raise ValueError('Invalid motion prompt')
        return self

class GeneratedVideo(BaseModel):
    identifier: str = Field(default_factory=uid)
    job_id: str
    shot_id: str
    path: str
    poster_path: str
    sha256: str
    format: str
    codec: str
    width: int = Field(gt=0)
    height: int = Field(gt=0)
    frame_rate: float = Field(gt=0)
    frame_count: int = Field(gt=0)
    duration: float = Field(gt=0)
    has_audio: bool = False
    provider: str
    model: str
    prompt_id: str
    starting_image_id: str
    ending_image_id: str | None = None
    parent_id: str | None = None
    candidate_number: int = 1
    dependency_fingerprint: str
    approval_status: str = 'Candidate'
    review_notes: str = ''
    review_issues: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=now)

    @model_validator(mode='after')
    def paths(self):
        for value in (self.path,self.poster_path):
            path=PurePosixPath(value)
            if not path.parts or path.is_absolute() or '..' in path.parts or path.parts[0]!='video_clips' or '\\' in value:
                raise ValueError('Video paths must be project-relative under video_clips')
        return self
