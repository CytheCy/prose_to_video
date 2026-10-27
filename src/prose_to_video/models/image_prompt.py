"""Provider-neutral, versioned image prompt records."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from math import gcd
from uuid import uuid4
from pydantic import BaseModel, ConfigDict, Field, model_validator


def now():
    return datetime.now(timezone.utc)


class PromptSettings(BaseModel):
    model_config = ConfigDict(extra='forbid')
    provider: str = 'OpenAI'
    model: str = ''  # empty uses the configured application model
    detail_level: str = 'Detailed'
    cinematic_emphasis: bool = True
    environmental_emphasis: bool = True
    character_emphasis: bool = True
    continuity_strictness: str = 'Normal'
    allow_environmental_additions: bool = True
    generate_negative: bool = True
    include_adjacent_shots: bool = True
    pause_for_approval: bool = False

    @model_validator(mode='after')
    def valid(self):
        if self.detail_level not in {'Concise', 'Detailed', 'Highly detailed'}:
            raise ValueError('Invalid prompt detail level')
        if self.continuity_strictness not in {'Relaxed', 'Normal', 'Strict'}:
            raise ValueError('Invalid continuity strictness')
        if self.provider != 'OpenAI':
            raise ValueError('Unsupported prompt provider')
        return self


class PromptComponents(BaseModel):
    model_config = ConfigDict(extra='forbid')
    shot_description: str = ''
    composition: str = ''
    camera_framing: str = ''
    camera_angle: str = ''
    camera_position: str = ''
    foreground: str = ''
    middle_ground: str = ''
    background: str = ''
    environment: str = ''
    character_appearance: str = ''
    character_positions_actions: str = ''
    objects: str = ''
    lighting: str = ''
    color_material: str = ''
    visual_style: str = ''
    generation_notes: str = ''


def assemble_prompt(components: PromptComponents) -> str:
    """Stable, readable assembly with no provider syntax or hidden additions."""
    names = ('shot_description', 'composition', 'camera_framing', 'camera_angle',
             'camera_position', 'foreground', 'middle_ground', 'background',
             'environment', 'character_appearance', 'character_positions_actions',
             'objects', 'lighting', 'color_material', 'visual_style')
    return ' '.join(getattr(components, name).strip().rstrip('.') + '.' for name in names
                    if getattr(components, name).strip())


class PromptReference(BaseModel):
    model_config = ConfigDict(extra='forbid')
    asset_id: str
    path: str
    role: str
    entity_id: str | None = None
    variant_id: str | None = None
    primary: bool = False
    width: int | None = None
    height: int | None = None
    sha256: str = ''
    description: str = ''


class ImagePrompt(BaseModel):
    model_config = ConfigDict(extra='forbid')
    identifier: str = Field(default_factory=lambda: str(uuid4()))
    shot_id: str
    scene_id: str
    version: int = Field(ge=1)
    positive_prompt: str
    negative_prompt: str = ''
    components: PromptComponents
    entity_ids: list[str] = Field(default_factory=list)
    references: list[PromptReference] = Field(default_factory=list)
    variant_ids: dict[str, str] = Field(default_factory=dict)
    source_start: int = Field(ge=0)
    source_end: int = Field(gt=0)
    aspect_ratio: str
    dependency_fingerprint: str
    generation_status: str = 'Generated'
    review_status: str = 'Generated'
    manually_edited: bool = False
    model_name: str = ''
    provider: str = ''
    settings: PromptSettings = Field(default_factory=PromptSettings)
    warnings: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=now)
    modified_at: datetime = Field(default_factory=now)

    @model_validator(mode='after')
    def valid(self):
        if not self.shot_id or not self.scene_id or self.source_end <= self.source_start:
            raise ValueError('Invalid image prompt shot or source range')
        if self.review_status not in {'Generated', 'Needs Review', 'Manually Edited', 'Approved', 'Outdated'}:
            raise ValueError('Invalid prompt review status')
        if self.generation_status not in {'Generated', 'Failed'}:
            raise ValueError('Invalid prompt generation status')
        if not self.aspect_ratio or ':' not in self.aspect_ratio:
            raise ValueError('Invalid aspect ratio')
        return self


def aspect_ratio(width: int, height: int) -> str:
    if width <= 0 or height <= 0:
        raise ValueError('Output dimensions must be positive')
    divisor = gcd(width, height)
    return f'{width // divisor}:{height // divisor}'


def fingerprint(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     default=str, separators=(',', ':')).encode()).hexdigest()
