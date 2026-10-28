"""Provider-neutral image-to-video requests, capabilities, and jobs."""
from __future__ import annotations
from datetime import datetime
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator
from prose_to_video.models.generated_video import now, uid

class VideoCapabilities(BaseModel):
    first_frame: bool = True
    last_frame: bool = False
    references: int = 0
    negative_prompt: bool = False
    duration: bool = False
    frame_count: bool = False
    resolution: bool = False
    frame_rate: bool = False
    seed: bool = False
    motion_intensity: bool = False
    continuation: bool = False
    cancel: bool = False
    job_lookup: bool = False
    max_duration: float | None = None
    supported_frame_rates: list[int] = Field(default_factory=list)
    supported_resolutions: list[tuple[int,int]] = Field(default_factory=list)
    formats: list[str] = Field(default_factory=lambda:['mp4'])

class VideoGenerationSettings(BaseModel):
    provider: str = 'ComfyUI'
    model: str = ''
    comfyui_url: str = 'http://127.0.0.1:8188'
    workflow_path: str = ''
    workflow_mapping: dict = Field(default_factory=dict)
    width: int = Field(default=832,ge=16,le=8192)
    height: int = Field(default=480,ge=16,le=8192)
    duration: float = Field(default=5.0,gt=0)
    frame_rate: int = Field(default=16,gt=0)
    seed: int | None = None
    candidates: int = Field(default=1,ge=1,le=16)
    motion_intensity: float | None = None
    camera_preference: str = 'Stationary'
    use_last_frame: bool = False
    use_references: bool = False
    max_concurrent_jobs: int = Field(default=1,ge=1,le=16)
    retry_limit: int = Field(default=0,ge=0,le=5)
    spending_cap: float | None = Field(default=None,ge=0)
    estimated_cost_per_clip: float | None = Field(default=None,ge=0)
    output_format: str = 'mp4'

class VideoRequest(BaseModel):
    model: str = ''
    starting_image_path: str
    ending_image_path: str | None = None
    reference_paths: list[str] = Field(default_factory=list)
    prompt: str
    negative_prompt: str = ''
    width: int | None = Field(default=None,gt=0)
    height: int | None = Field(default=None,gt=0)
    duration: float | None = Field(default=None,gt=0)
    frame_count: int | None = Field(default=None,gt=0)
    frame_rate: int | None = Field(default=None,gt=0)
    seed: int | None = None
    motion_intensity: float | None = None
    output_format: str = 'mp4'

class ProviderVideo(BaseModel):
    data: bytes = b''
    path: str | None = None
    filename: str = 'output.mp4'
    model: str = ''
    metadata: dict = Field(default_factory=dict)

class VideoProviderResult(BaseModel):
    status: str
    provider_job_id: str | None = None
    progress: float | None = None
    videos: list[ProviderVideo] = Field(default_factory=list)
    error: str = ''
    usage: dict = Field(default_factory=dict)
    actual_cost: float | None = None

class VideoJob(BaseModel):
    identifier: str = Field(default_factory=uid)
    project_id: str
    scene_id: str
    shot_id: str
    provider: str
    model: str
    starting_image_id: str
    ending_image_id: str | None = None
    prompt_id: str
    requested_duration: float
    requested_width: int
    requested_height: int
    settings: dict = Field(default_factory=dict)
    provider_job_id: str | None = None
    submitted_at: datetime | None = None
    completed_at: datetime | None = None
    status: Literal['Pending','Submitted','Running','Completed','Failed','Canceled','Needs Reconciliation'] = 'Pending'
    error: str = ''
    usage: dict = Field(default_factory=dict)
    estimated_cost: float | None = None
    actual_cost: float | None = None
    dependency_fingerprint: str
    parent_clip_id: str | None = None
    candidate_number: int = 1
    created_at: datetime = Field(default_factory=now)

class DurationCoverage(BaseModel):
    model_config = ConfigDict(extra='forbid')
    strategy: Literal['exact','still_tail','camera_tail','sequence'] = 'exact'
    clip_ids: list[str] = Field(default_factory=list)
    tail_image_id: str | None = None
    @model_validator(mode='after')
    def valid(self):
        if self.strategy in {'still_tail','camera_tail'} and not self.tail_image_id:
            raise ValueError('Tail strategy needs a selected still image')
        return self
