"""Provider-independent request, result, capability, and persistent job models."""
from datetime import datetime
from typing import Any
from uuid import uuid4
from pydantic import BaseModel, ConfigDict, Field, model_validator
from prose_to_video.models.generated_image import utcnow


class ImageCapabilities(BaseModel):
    text_to_image: bool = True
    references: bool = False
    multiple_references: bool = False
    negative_prompt: bool = False
    seeds: bool = False
    multiple_candidates: bool = True
    exact_dimensions: bool = False
    aspect_ratios: list[str] = Field(default_factory=list)
    editing: bool = False
    asynchronous: bool = False
    progress: bool = False
    cancellation: bool = False
    max_references: int = 0
    max_candidates: int = 1
    formats: list[str] = Field(default_factory=lambda: ['png'])
    qualities: list[str] = Field(default_factory=list)


class ImageGenerationSettings(BaseModel):
    provider: str = 'OpenAI'
    model: str = 'gpt-image-2.5-flare'
    width: int = Field(default=1536, ge=64, le=3840)
    height: int = Field(default=864, ge=64, le=2160)
    quality: str = 'medium'
    candidates: int = Field(default=1, ge=1, le=10)
    seed: int | None = None
    use_references: bool = True
    output_format: str = 'png'
    max_concurrent_jobs: int = Field(default=1, ge=1, le=8)
    retry_limit: int = Field(default=1, ge=0, le=5)
    batch_approval: str = 'manual'
    spending_cap: float | None = Field(default=None, ge=0)
    estimated_cost_per_image: float | None = Field(default=None, ge=0)
    comfyui_url: str = 'http://127.0.0.1:8188'
    workflow_path: str = ''
    workflow_mapping: dict[str, Any] = Field(default_factory=dict)


class ImageReference(BaseModel):
    asset_id: str
    path: str
    role: str
    primary: bool = False


class ImageRequest(BaseModel):
    prompt: str
    negative_prompt: str = ''
    model: str
    width: int
    height: int
    quality: str
    candidates: int = 1
    output_format: str = 'png'
    seed: int | None = None
    references: list[ImageReference] = Field(default_factory=list)
    parent_path: str | None = None
    options: dict[str, Any] = Field(default_factory=dict)


class ProviderImage(BaseModel):
    data: bytes
    model: str
    seed: int | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class ProviderResult(BaseModel):
    status: str
    provider_job_id: str | None = None
    images: list[ProviderImage] = Field(default_factory=list)
    usage: dict[str, Any] = Field(default_factory=dict)
    actual_cost: float | None = None
    error: str = ''
    progress: float | None = None


class ImageJob(BaseModel):
    model_config = ConfigDict(extra='forbid')
    identifier: str = Field(default_factory=lambda: str(uuid4()))
    project_id: str
    scene_id: str
    shot_id: str
    prompt_id: str
    provider: str
    model: str
    width: int
    height: int
    quality: str
    candidates: int
    reference_ids: list[str] = Field(default_factory=list)
    settings: dict[str, Any] = Field(default_factory=dict)
    dependency_fingerprint: str
    created_at: datetime = Field(default_factory=utcnow)
    submitted_at: datetime | None = None
    completed_at: datetime | None = None
    provider_job_id: str | None = None
    status: str = 'Pending'
    error: str = ''
    usage: dict[str, Any] = Field(default_factory=dict)
    actual_cost: float | None = None
    estimated_cost: float | None = None
    included_references: list[str] = Field(default_factory=list)
    excluded_references: list[str] = Field(default_factory=list)
    parent_id: str | None = None
    transformations: list[str] = Field(default_factory=list)
    revision_instruction: str = ''

    @model_validator(mode='after')
    def valid_status(self):
        if self.status not in {'Pending','Submitted','Running','Completed','Failed','Canceled','Needs Reconciliation'}:
            raise ValueError('Invalid image job status')
        return self
