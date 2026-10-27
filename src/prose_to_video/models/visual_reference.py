"""Stable visual identities and provenance-aware appearance records."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import PurePosixPath
from uuid import uuid4
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

ENTITY_TYPES = ('Character', 'Location', 'Robot', 'Vehicle', 'Prop or object', 'Architectural structure', 'Other')
REVIEW_STATUSES = ('Suggested', 'Needs Review', 'Reviewed', 'Approved', 'Rejected')
EVIDENCE_KINDS = ('explicit', 'inferred', 'user', 'ai_proposed')

def now():
    return datetime.now(timezone.utc)

def uid():
    return str(uuid4())

class SourceEvidence(BaseModel):
    model_config = ConfigDict(extra='forbid')
    identifier: str = Field(default_factory=uid)
    scene_id: str | None = None
    source_hash: str | None = None
    source_start: int | None = Field(default=None, ge=0)
    source_end: int | None = Field(default=None, ge=0)
    quote: str = ''
    field: str = ''
    value: str = ''
    kind: str = 'explicit'
    status: str = 'Suggested'

    @model_validator(mode='after')
    def check(self):
        if self.kind not in EVIDENCE_KINDS or self.status not in REVIEW_STATUSES:
            raise ValueError('Invalid evidence kind or review status')
        if (self.source_start is None) != (self.source_end is None):
            raise ValueError('Evidence offsets must be paired')
        if self.source_start is not None and self.source_end <= self.source_start:
            raise ValueError('Invalid evidence source range')
        return self

class VisualEntity(BaseModel):
    model_config = ConfigDict(extra='forbid')
    identifier: str = Field(default_factory=uid)
    name: str = Field(min_length=1)
    entity_type: str = 'Other'
    description: str = ''
    appearance_description: str = ''
    distinguishing_features: str = ''
    colors_materials: str = ''
    size_proportions: str = ''
    clothing_accessories: str = ''
    aliases: list[str] = Field(default_factory=list)
    recurring: bool = True
    review_status: str = 'Suggested'
    user_notes: str = ''
    source_hash: str | None = None
    created_at: datetime = Field(default_factory=now)
    modified_at: datetime = Field(default_factory=now)

    @model_validator(mode='after')
    def check(self):
        if not self.name.strip() or self.entity_type not in ENTITY_TYPES or self.review_status not in REVIEW_STATUSES:
            raise ValueError('Invalid entity name, type, or status')
        names = [x.strip().casefold() for x in [self.name, *self.aliases]]
        if any(not x for x in names) or len(names) != len(set(names)):
            raise ValueError('Aliases must be nonempty and unique within an entity')
        return self

class AppearanceVariant(BaseModel):
    model_config = ConfigDict(extra='forbid')
    identifier: str = Field(default_factory=uid)
    entity_id: str
    name: str = Field(min_length=1)
    description: str = ''
    scene_ids: list[str] = Field(default_factory=list)
    shot_ids: list[str] = Field(default_factory=list)
    status: str = 'Suggested'
    is_default: bool = False

    @field_validator('status')
    @classmethod
    def valid_status(cls, value):
        if value not in REVIEW_STATUSES: raise ValueError('Invalid variant status')
        return value

class ReferenceAsset(BaseModel):
    model_config = ConfigDict(extra='forbid')
    identifier: str = Field(default_factory=uid)
    path: str
    sha256: str
    width: int = Field(gt=0)
    height: int = Field(gt=0)
    format: str
    imported_at: datetime = Field(default_factory=now)

    @field_validator('path')
    @classmethod
    def relative_path(cls, value):
        path = PurePosixPath(value)
        if path.is_absolute() or len(path.parts) != 2 or path.parts[0] != 'references' or path.parts[1] in ('..', '.') or '\\' in value:
            raise ValueError('Reference path must be within references/')
        return value

class EntityReference(BaseModel):
    model_config = ConfigDict(extra='forbid')
    entity_id: str
    asset_id: str
    variant_id: str | None = None
    role: str = 'Additional view'
    description: str = ''
    status: str = 'Suggested'
    is_primary: bool = False

    @field_validator('status')
    @classmethod
    def valid_status(cls, value):
        if value not in REVIEW_STATUSES: raise ValueError('Invalid reference status')
        return value

class VisualStyle(BaseModel):
    model_config = ConfigDict(extra='forbid')
    style_name: str = ''
    general_aesthetic: str = ''
    rendering_approach: str = ''
    color_palette: str = ''
    lighting: str = ''
    camera_style: str = ''
    realism: str = ''
    textures: str = ''
    visual_rules: str = ''
    negative_constraints: str = ''

class ContinuityWarning(BaseModel):
    severity: str
    code: str
    message: str
    entity_ids: list[str] = Field(default_factory=list)
    shot_ids: list[str] = Field(default_factory=list)
