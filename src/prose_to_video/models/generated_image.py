"""Portable generated image metadata; approval and freshness are independent."""
from datetime import datetime, timezone
from pathlib import PurePosixPath
from uuid import uuid4
from pydantic import BaseModel, ConfigDict, Field, field_validator


def utcnow():
    return datetime.now(timezone.utc)


class GeneratedImage(BaseModel):
    model_config = ConfigDict(extra='forbid')
    identifier: str = Field(default_factory=lambda: str(uuid4()))
    job_id: str
    shot_id: str
    path: str
    thumbnail_path: str
    format: str
    width: int = Field(gt=0)
    height: int = Field(gt=0)
    sha256: str
    candidate_number: int = Field(ge=1)
    approval_status: str = 'Candidate'
    provider: str
    model: str
    seed: int | None = None
    created_at: datetime = Field(default_factory=utcnow)
    prompt_id: str
    dependency_fingerprint: str
    parent_id: str | None = None
    warnings: list[str] = Field(default_factory=list)

    @field_validator('path', 'thumbnail_path')
    @classmethod
    def safe_path(cls, value):
        p = PurePosixPath(value)
        if p.is_absolute() or not p.parts or p.parts[0] != 'images' or '..' in p.parts or '\\' in value:
            raise ValueError('Image path must be inside images/')
        return value
