"""Project metadata. Paths stored in the database are relative to project root."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator


PROJECT_FORMAT_VERSION = 1


class VideoSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    width: int = Field(default=1920, ge=16, le=16384)
    height: int = Field(default=1080, ge=16, le=16384)
    frame_rate: int = Field(default=30, ge=1, le=240)


class Project(BaseModel):
    model_config = ConfigDict(extra="forbid")

    identifier: str
    name: str = Field(min_length=1)
    format_version: int = PROJECT_FORMAT_VERSION
    created_at: datetime
    modified_at: datetime
    prose_path: str | None = None
    audio_path: str | None = None
    audio_duration: float | None = Field(default=None, ge=0)
    audio_codec: str | None = None
    audio_sample_rate: int | None = Field(default=None, ge=1)
    audio_channels: int | None = Field(default=None, ge=1)
    prose_word_count: int = Field(default=0, ge=0)
    settings: VideoSettings = Field(default_factory=VideoSettings)

    @field_validator("name")
    @classmethod
    def name_has_content(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Project name cannot be blank")
        return value.strip()

    @field_validator("prose_path", "audio_path")
    @classmethod
    def safe_relative_source_path(cls, value: str | None) -> str | None:
        if value is None:
            return None
        path = PurePosixPath(value)
        if path.is_absolute() or len(path.parts) != 2 or path.parts[0] != "source" or path.parts[1] in (".", "..") or "\\" in value or "\0" in value:
            raise ValueError("Source path must be a filename inside source/")
        return value

    @classmethod
    def create(cls, name: str) -> Project:
        now = datetime.now(timezone.utc)
        return cls(identifier=str(uuid4()), name=name, created_at=now, modified_at=now)

    def resolved_prose(self, root: Path) -> Path | None:
        return root / self.prose_path if self.prose_path else None

    def resolved_audio(self, root: Path) -> Path | None:
        return root / self.audio_path if self.audio_path else None
