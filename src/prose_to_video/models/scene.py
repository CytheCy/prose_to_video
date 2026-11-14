"""Narrative scene records and exact-source partition validation."""

from __future__ import annotations

import hashlib
from uuid import uuid4
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


from prose_to_video.models.camera_movement import CameraMovement


class Scene(BaseModel):
    model_config = ConfigDict(extra="forbid")

    identifier: str = Field(default_factory=lambda: str(uuid4()))
    sequence: int = Field(ge=1)
    start: int = Field(ge=0)
    end: int = Field(gt=0)
    text: str
    title: str = "Untitled scene"
    location: str | None = None
    time_of_day: str | None = None
    characters: list[str] = Field(default_factory=list)
    point_of_view: str | None = None
    narrative_type: str | None = None
    narrative_formats: list[str] = Field(default_factory=list)
    viewpoint_category: str = "direct"
    production_included: bool = True
    summary: str = ""
    visual_description: str = ""
    render_type: Literal["cinematic", "concept_map", "blueprint", "cad_visualization", "product_showcase"] = "cinematic"
    explanatory_focus: str = ""
    environment: str = ""
    important_objects: list[str] = Field(default_factory=list)
    actions: list[str] = Field(default_factory=list)
    dialogue_participants: list[str] = Field(default_factory=list)
    visual_atmosphere: str = ""
    boundary_reason: str = ""

    source_version: str = ""
    identity_links: list[str] = Field(default_factory=list)
    foreground: str = ""
    middle_ground: str = ""
    background: str = ""
    composition: str = ""
    camera_framing: str = ""
    camera_angle: str = ""
    camera_position: str = ""
    camera_movement: CameraMovement = "static_hold"
    camera_focal_x: float = Field(default=.5, ge=0, le=1)
    camera_focal_y: float = Field(default=.5, ge=0, le=1)
    lighting: str = ""
    palette_materials: str = ""
    final_prompt: str = ""
    negative_prompt: str = ""
    prompt_dependency: str = ""
    manual_fields: list[str] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def discard_legacy_review_status(cls, data):
        # Older projects stored a review status in each scene's JSON record.
        if isinstance(data, dict) and "review_status" in data:
            data = {key: value for key, value in data.items() if key != "review_status"}
        return data

    @model_validator(mode="after")
    def valid_range(self) -> Scene:
        if self.end <= self.start or len(self.text) != self.end - self.start:
            raise ValueError("Scene offsets must match the exact scene text")
        return self


def source_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def validate_partition(text: str, scenes: list[Scene]) -> None:
    if not text or not scenes:
        raise ValueError("A nonempty manuscript requires at least one scene")
    position = 0
    identifiers: set[str] = set()
    for number, scene in enumerate(scenes, 1):
        if scene.sequence != number or scene.start != position or scene.end > len(text):
            raise ValueError(f"Scene {number} has a gap, overlap, or invalid order")
        if scene.identifier in identifiers or scene.text != text[scene.start:scene.end]:
            raise ValueError(f"Scene {number} does not match the source text")
        identifiers.add(scene.identifier)
        position = scene.end
    if position != len(text):
        raise ValueError("Scene analysis does not cover the complete manuscript")
