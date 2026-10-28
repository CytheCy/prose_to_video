"""Source-anchored visual storyboard records and coverage validation."""
from __future__ import annotations

import hashlib
import json
import math
from uuid import uuid4
from pydantic import BaseModel, ConfigDict, Field, model_validator

SHOT_TYPES = ("Establishing", "Extreme wide", "Wide", "Medium", "Close-up", "Extreme close-up", "Over-the-shoulder", "Point-of-view", "Insert", "Environmental detail")
CAMERA_MOVEMENTS = ("Static", "Slow pan left", "Slow pan right", "Pan up", "Pan down", "Tilt up", "Tilt down", "Push in", "Pull back", "Diagonal pan", "Combined pan and zoom", "Custom", "Tracking", "Slow lateral", "Aerial")
STATUSES = ("Not reviewed", "Reviewed", "Manually edited")
TIMING_STATUSES = ("fully timed", "partially timed", "untimed")

class StoryboardSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    density: str = "Medium"
    custom_target: float = 8.0
    minimum: float = 2.0
    maximum: float = 30.0
    visual_style: str = "Cinematic, grounded in the prose"
    movement_intensity: str = "Subtle"
    encourage_establishing: bool = True
    encourage_closeups: bool = True
    include_environmental_details: bool = True
    pause_policy: str = "previous"
    long_pause_threshold: float = 3.0
    allow_duration_exceptions: bool = False

    @model_validator(mode="after")
    def valid(self):
        if self.density not in {"Low", "Medium", "High", "Custom"}:
            raise ValueError("Invalid shot density")
        if not 0 < self.minimum <= self.maximum or self.custom_target <= 0:
            raise ValueError("Minimum, maximum, and target durations must be positive and ordered")
        if self.density == "Custom" and not self.minimum <= self.custom_target <= self.maximum:
            raise ValueError("Custom target duration must fall between minimum and maximum")
        if self.movement_intensity not in {"None", "Subtle", "Moderate"} or self.pause_policy not in {"previous", "next"}:
            raise ValueError("Invalid movement or pause policy")
        if self.long_pause_threshold < 0:
            raise ValueError("Pause threshold must be nonnegative")
        return self

    @property
    def target_range(self):
        return {"Low": (12, 20), "Medium": (6, 12), "High": (3, 6), "Custom": (self.custom_target, self.custom_target)}[self.density]

    def fingerprint(self):
        return hashlib.sha256(json.dumps(self.model_dump(), sort_keys=True).encode()).hexdigest()

class Shot(BaseModel):
    model_config = ConfigDict(extra="forbid")
    identifier: str = Field(default_factory=lambda: str(uuid4()))
    scene_id: str
    sequence: int = Field(ge=1)
    title: str
    source_start: int = Field(ge=0)
    source_end: int = Field(gt=0)
    original_prose: str
    audio_start: float | None = None
    audio_end: float | None = None
    shot_type: str = "Medium"
    camera_framing: str = ""
    camera_angle: str = ""
    camera_position: str = ""
    camera_movement: str = "Static"
    foreground: str = ""
    background: str = ""
    environment: str = ""
    characters: list[str] = Field(default_factory=list)
    character_appearances: dict[str, str] = Field(default_factory=dict)
    character_actions: str = ""
    objects: list[str] = Field(default_factory=list)
    lighting: str = ""
    atmosphere: str = ""
    visual_description: str
    transition: str = "Cut"
    suggested_motion: str = "None"
    ai_notes: str = ""
    entity_refs: list[str] = Field(default_factory=list)
    review_status: str = "Not reviewed"
    manually_edited: bool = False
    timing_status: str = "untimed"
    timing_note: str = ""

    @property
    def duration(self):
        return self.audio_end - self.audio_start if self.audio_start is not None and self.audio_end is not None else None

    @model_validator(mode="after")
    def valid(self):
        if self.source_end <= self.source_start or len(self.original_prose) != self.source_end - self.source_start:
            raise ValueError("Shot offsets must match its exact source passage")
        if not self.title.strip() or not self.visual_description.strip():
            raise ValueError("Shot needs a title and visual description")
        if self.shot_type not in SHOT_TYPES or self.camera_movement not in CAMERA_MOVEMENTS:
            raise ValueError("Unknown shot type or camera movement")
        if self.review_status not in STATUSES or self.timing_status not in TIMING_STATUSES:
            raise ValueError("Invalid shot status")
        for value in (self.audio_start, self.audio_end):
            if value is not None and (not math.isfinite(value) or value < 0):
                raise ValueError("Invalid shot timestamp")
        if self.audio_start is not None and self.audio_end is not None and self.audio_end <= self.audio_start:
            raise ValueError("Shot duration must be positive")
        if self.timing_status == "fully timed" and (self.audio_start is None or self.audio_end is None):
            raise ValueError("Fully timed shot needs both timestamps")
        if self.timing_status == "untimed" and (self.audio_start is not None or self.audio_end is not None):
            raise ValueError("Untimed shot cannot carry timestamps")
        return self

class Storyboard(BaseModel):
    model_config = ConfigDict(extra="forbid")
    identifier: str = Field(default_factory=lambda: str(uuid4()))
    scene_id: str
    scene_start: int
    scene_end: int
    scene_hash: str
    prose_hash: str
    audio_hash: str | None = None
    alignment_id: str | None = None
    scene_definition_hash: str
    model_name: str
    settings: StoryboardSettings
    settings_fingerprint: str
    coverage_start: float | None = None
    coverage_end: float | None = None
    status: str = "draft"
    shots: list[Shot]

    def validate_against(self, text: str, audio_duration: float | None = None):
        if self.prose_hash != hashlib.sha256(text.encode()).hexdigest():
            raise ValueError("Storyboard prose is outdated")
        if self.scene_hash != hashlib.sha256(text[self.scene_start:self.scene_end].encode()).hexdigest():
            raise ValueError("Storyboard scene prose is outdated")
        if self.settings_fingerprint != self.settings.fingerprint():
            raise ValueError("Storyboard settings fingerprint is invalid")
        if not self.shots or len({s.identifier for s in self.shots}) != len(self.shots):
            raise ValueError("Storyboard needs unique shots")
        position = self.scene_start
        last_time = self.coverage_start
        previous_end = None
        for i, shot in enumerate(self.shots, 1):
            if shot.scene_id != self.scene_id or shot.sequence != i or shot.source_start != position or shot.source_end > self.scene_end or shot.original_prose != text[shot.source_start:shot.source_end]:
                raise ValueError("Shot source ranges have a gap, overlap, or mismatch")
            position = shot.source_end
            if shot.audio_start is not None and shot.audio_end is not None:
                if previous_end is not None and shot.audio_start < previous_end - .001:
                    raise ValueError("Shot timeline has an overlap")
                if self.coverage_start is not None and shot.audio_start < self.coverage_start - .001:
                    raise ValueError("Shot begins before scene visual coverage")
                if self.coverage_end is not None and shot.audio_end > self.coverage_end + .001:
                    raise ValueError("Shot ends after scene visual coverage")
                if audio_duration is not None and shot.audio_end > audio_duration + .001:
                    raise ValueError("Shot is beyond the audio duration")
                previous_end = shot.audio_end
            if self.status == "ready":
                if shot.timing_status != "fully timed" or shot.audio_start is None or shot.audio_end is None:
                    raise ValueError("Ready storyboard contains untimed shots")
                if not self.settings.allow_duration_exceptions and not self.settings.minimum <= shot.duration <= self.settings.maximum:
                    raise ValueError("Shot duration is outside the configured limits")
                if last_time is None or abs(shot.audio_start - last_time) > .001:
                    raise ValueError("Shot timeline has a gap or overlap")
                if audio_duration is not None and shot.audio_end > audio_duration + .001:
                    raise ValueError("Shot is beyond the audio duration")
                last_time = shot.audio_end
        if position != self.scene_end:
            raise ValueError("Shot plan does not cover the scene prose")
        if self.status == "ready" and (self.coverage_end is None or abs(last_time - self.coverage_end) > .001):
            raise ValueError("Shot timeline does not cover the scene")
