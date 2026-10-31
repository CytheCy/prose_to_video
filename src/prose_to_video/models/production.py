"""Reproducible production configuration and persistent workflow records."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator
from prose_to_video.models.video_render import RenderSettings


def fingerprint(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str,
                                   separators=(',', ':')).encode()).hexdigest()


class ProductionProfile(BaseModel):
    model_config = ConfigDict(extra='forbid')
    identifier: str = Field(default_factory=lambda: str(uuid4()))
    name: str = 'Illustrated Story'
    automation_mode: Literal['Fully Automatic', 'Review at Milestones', 'Manual Production'] = 'Review at Milestones'
    narrative_settings: dict = Field(default_factory=dict)
    alignment_settings: dict = Field(default_factory=dict)
    shot_density: Literal['Low', 'Medium', 'High', 'Custom'] = 'Medium'
    visual_style: str = 'Cinematic, grounded in the prose'
    image_settings: dict = Field(default_factory=dict)
    video_settings: dict = Field(default_factory=dict)
    animation_policy: Literal['None', 'Limited', 'Moderate', 'Extensive', 'All eligible'] = 'None'
    animation_percent: int = Field(default=100, ge=0, le=100)
    animation_max: int | None = Field(default=None, ge=0)
    allow_mode_changes: bool = False
    approval_requirements: dict[str, bool] = Field(default_factory=dict)
    authorize_auto_approval: bool = False
    quality_thresholds: dict = Field(default_factory=lambda: {'min_alignment_coverage': .9,
        'max_unresolved_shots': 0, 'max_timeline_gap': .1, 'max_duration_mismatch': .12,
        'max_critical_issues': 0, 'accept_render_warnings': False})
    concurrency: dict[str, int] = Field(default_factory=lambda: {'language': 1, 'image': 1, 'video': 1, 'cpu': 1, 'render': 1})
    retry_limit: int = Field(default=1, ge=0, le=5)
    budget_total: float | None = Field(default=None, ge=0)
    budget_image: float | None = Field(default=None, ge=0)
    budget_video: float | None = Field(default=None, ge=0)
    budget_review: float | None = Field(default=None, ge=0)
    budget_per_shot: float | None = Field(default=None, ge=0)
    authorize_unknown_cost: bool = False
    fallback_animation: Literal['Pause', 'Camera Motion', 'Static Image'] = 'Pause'
    allow_text_only_references: bool = False
    render_settings: RenderSettings = Field(default_factory=RenderSettings)

    @model_validator(mode='after')
    def valid(self):
        if not self.name.strip() or not self.visual_style.strip():
            raise ValueError('Profile name and visual style are required')
        if set(self.concurrency) != {'language', 'image', 'video', 'cpu', 'render'} or any(
                not isinstance(v, int) or not 1 <= v <= 8 for v in self.concurrency.values()):
            raise ValueError('Concurrency limits must be between one and eight')
        if self.automation_mode == 'Fully Automatic' and not self.authorize_auto_approval:
            raise ValueError('Fully Automatic requires explicit automatic approval authorization')
        permitted = {'Scene Analysis Approval','Storyboard Approval','Visual Reference Approval',
                     'Image Prompt Approval','Image Approval','Motion Prompt Approval',
                     'Animation Approval','Final Video Approval'}
        if set(self.approval_requirements) - permitted:
            raise ValueError('Unknown production approval checkpoint')
        if self.render_settings.mode != 'final':
            raise ValueError('Complete production requires final rendering')
        limits = self.quality_thresholds
        if not 0 <= limits.get('min_alignment_coverage', .9) <= 1:
            raise ValueError('Alignment coverage threshold must be between zero and one')
        if any(limits.get(key, 0) < 0 for key in ('max_unresolved_shots','max_timeline_gap',
                                                  'max_duration_mismatch','max_critical_issues')):
            raise ValueError('Quality thresholds must be nonnegative')
        return self

    @classmethod
    def preset(cls, name: str, **changes):
        options = {
            'Illustrated Story': {'animation_policy': 'None'},
            'Cinematic Slideshow': {'animation_policy': 'None', 'render_settings': RenderSettings(motion_intensity='Moderate', default_transition='Crossfade')},
            'AI Animated Production': {'animation_policy': 'Moderate', 'allow_mode_changes': True},
        }
        if name not in options:
            raise ValueError('Unknown production preset')
        return cls(name=name, **{**options[name], **changes})


class ProductionTask(BaseModel):
    identifier: str = Field(default_factory=lambda: str(uuid4()))
    run_id: str = ''
    project_id: str
    kind: str
    scene_id: str | None = None
    shot_id: str | None = None
    inputs: dict = Field(default_factory=dict)
    dependencies: list[str] = Field(default_factory=list)
    fingerprint: str
    output_ids: list[str] = Field(default_factory=list)
    status: Literal['Pending', 'Ready', 'Running', 'Reused', 'Completed', 'Awaiting Approval', 'Blocked', 'Failed', 'Canceled', 'Interrupted'] = 'Pending'
    error: str | None = None
    failure_kind: Literal['Transient', 'Validation', 'Credentials', 'Unsupported', 'Budget',
                          'Uncertain External', 'User Intervention'] | None = None
    attempts: int = 0


class ProductionRun(BaseModel):
    identifier: str = Field(default_factory=lambda: str(uuid4()))
    project_id: str
    profile: ProductionProfile
    status: Literal['Not Started', 'Preparing', 'Running', 'Awaiting Approval', 'Paused', 'Blocked', 'Completed', 'Completed With Warnings', 'Failed', 'Canceled', 'Interrupted'] = 'Not Started'
    current_stage: str = ''
    started_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    output_path: str | None = None
    manifest_path: str | None = None
    completed_task_count: int = 0
    pending_task_count: int = 0
    failed_task_count: int = 0
    blocked_task_count: int = 0
    estimated_remaining_work: int = 0
    output_artifact_ids: list[str] = Field(default_factory=list)
    known_cost: float = 0
    unknown_cost_count: int = 0
    error: str | None = None


class QualityIssue(BaseModel):
    identifier: str = Field(default_factory=lambda: str(uuid4()))
    run_id: str | None = None
    project_id: str
    scene_id: str | None = None
    shot_id: str | None = None
    asset_id: str | None = None
    category: str
    severity: Literal['Critical', 'Error', 'Warning', 'Suggestion']
    description: str
    evidence: dict = Field(default_factory=dict)
    detection_method: Literal['Technical', 'AI Suggestion', 'Human'] = 'Technical'
    status: Literal['Open', 'Needs Review', 'Accepted', 'Resolved', 'Dismissed'] = 'Open'
    suggested_correction: str = ''
    resolution_notes: str = ''
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    resolved_at: datetime | None = None


def correction_workspace(issue: QualityIssue) -> str:
    return {'Narrative': 'Storyboard', 'Timing': 'Storyboard',
            'Visual Continuity': 'Visual Library', 'Image': 'Image Gallery',
            'Animation': 'AI Animation', 'Motion Prompt': 'AI Animation',
            'Render': 'Video Production'}.get(issue.category, 'Production Dashboard')
