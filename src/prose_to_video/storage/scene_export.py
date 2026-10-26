"""Portable scene breakdown exports."""

from __future__ import annotations

import json
import re
from pathlib import Path

from prose_to_video.models.scene import Scene, source_hash, validate_partition
from prose_to_video.models.alignment import AlignmentRun
from prose_to_video.utils.media_info import format_duration


def _scene_data(scene: Scene, run: AlignmentRun | None) -> dict:
    data = scene.model_dump()
    timing = next((t for t in run.scenes if t.scene_id == scene.identifier), None) if run else None
    data["audio_start"] = timing.audio_start if timing else None
    data["audio_end"] = timing.audio_end if timing else None
    data["duration"] = timing.duration if timing else None
    data["alignment_status"] = timing.status if timing else "unavailable"
    data["alignment_coverage"] = timing.coverage if timing else 0.0
    data["alignment_warnings"] = timing.warnings if timing else ["No current alignment"]
    data["timing_manually_corrected"] = timing.manual if timing else False
    return data


def export_json(path: Path, text: str, scenes: list[Scene], run: AlignmentRun | None = None,
                detailed: bool = False) -> None:
    validate_partition(text, scenes)
    if run and run.prose_hash != source_hash(text):
        raise ValueError("Alignment is outdated for this manuscript")
    data = {"format_version": 2, "source_sha256": source_hash(text),
            "audio_sha256": run.audio_hash if run else None,
            "scenes": [_scene_data(scene, run) for scene in scenes]}
    if detailed and run:
        data["sentences"] = [s.model_dump() for s in run.sentences]
        data["words"] = [w.model_dump() for w in run.words]
        data["transcript"] = [s.model_dump() for s in run.transcript]
        data["issues"] = [issue.model_dump() for issue in run.issues]
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def export_markdown(path: Path, text: str, scenes: list[Scene], run: AlignmentRun | None = None) -> None:
    validate_partition(text, scenes)
    if run and run.prose_hash != source_hash(text):
        raise ValueError("Alignment is outdated for this manuscript")
    lines = ["# Narrative scene breakdown", "", f"Source SHA-256: `{source_hash(text)}`", ""]
    for scene in scenes:
        timing = next((t for t in run.scenes if t.scene_id == scene.identifier), None) if run else None
        longest_fence = max((len(run) for run in re.findall(r"`+", scene.text)), default=0)
        fence = "`" * max(3, longest_fence + 1)
        lines.extend([
            f"## Scene {scene.sequence}: {scene.title}", "",
            f"- Location: {scene.location or 'Unspecified'}",
            f"- Time of day: {scene.time_of_day or 'Unspecified'}",
            f"- Characters: {', '.join(scene.characters) or 'Unspecified'}",
            f"- Point of view: {scene.point_of_view or 'Unspecified'}",
            f"- Narrative type: {scene.narrative_type or 'Unspecified'}",
            f"- Review status: {scene.review_status}", "",
            f"- Audio start: {format_duration(timing.audio_start) if timing and timing.audio_start is not None else 'Unresolved'}",
            f"- Audio end: {format_duration(timing.audio_end) if timing and timing.audio_end is not None else 'Unresolved'}",
            f"- Audio duration: {format_duration(timing.duration) if timing and timing.duration is not None else 'Unresolved'}",
            f"- Alignment status: {timing.status if timing else 'unavailable'}",
            f"- Alignment coverage: {timing.coverage:.0%}" if timing else "- Alignment coverage: 0%",
            f"- Alignment warnings: {'; '.join(timing.warnings) if timing else 'No current alignment'}", "",
            f"**Summary:** {scene.summary}", "",
            f"**Environment:** {scene.environment}", "",
            f"**Objects:** {', '.join(scene.important_objects)}", "",
            "**Actions:**", "",
            *[f"- {action}" for action in scene.actions], "",
            f"**Dialogue participants:** {', '.join(scene.dialogue_participants)}", "",
            f"**Visual atmosphere:** {scene.visual_atmosphere}", "",
            f"**Boundary reason:** {scene.boundary_reason}", "",
            f"**Source range:** [{scene.start}, {scene.end})", "",
            "**Original prose:**", "", fence + "text", scene.text, fence, "",
        ])
    path.write_text("\n".join(lines), encoding="utf-8")
