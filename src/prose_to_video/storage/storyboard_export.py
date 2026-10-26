"""Complete structured JSON and readable Markdown storyboard exports."""
import json
import re
from pathlib import Path
from prose_to_video.models.scene import Scene, source_hash
from prose_to_video.models.shot import Storyboard
from prose_to_video.pipeline.shot_planning import scene_definition_hash
from prose_to_video.utils.media_info import format_duration

def _validate(text: str, scenes: list[Scene], boards: list[Storyboard]):
    known = {s.identifier: s for s in scenes}
    for board in boards:
        if board.scene_id not in known: raise ValueError("Storyboard references an unknown scene")
        board.validate_against(text)
        if board.scene_definition_hash != scene_definition_hash(known[board.scene_id]):
            raise ValueError("Storyboard has outdated scene boundaries")

def export_storyboard_json(path: Path, text: str, scenes: list[Scene], boards: list[Storyboard]):
    _validate(text, scenes, boards)
    path.write_text(json.dumps({"format_version": 1, "source_sha256": source_hash(text),
        "storyboards": [board.model_dump(mode="json") | {"shots": [shot.model_dump(mode="json") | {"duration": shot.duration}
                                                               for shot in board.shots]}
                        for board in sorted(boards, key=lambda b: next(s.sequence for s in scenes if s.identifier == b.scene_id))]},
        ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

def export_storyboard_markdown(path: Path, text: str, scenes: list[Scene], boards: list[Storyboard]):
    _validate(text, scenes, boards)
    by_scene = {b.scene_id: b for b in boards}
    lines = ["# Visual storyboard", "", f"Source SHA-256: `{source_hash(text)}`", ""]
    for scene in scenes:
        board = by_scene.get(scene.identifier)
        if not board: continue
        lines.extend([f"## Scene {scene.sequence}: {scene.title}", "", f"Storyboard status: {board.status}", ""])
        for shot in board.shots:
            fence = "`" * max(3, max((len(x)+1 for x in re.findall(r"`+", shot.original_prose)), default=3))
            lines.extend([f"### Shot {shot.sequence}: {shot.title}", "",
                f"- Time: {format_duration(shot.audio_start) if shot.audio_start is not None else 'Unresolved'}–{format_duration(shot.audio_end) if shot.audio_end is not None else 'Unresolved'}",
                f"- Duration: {format_duration(shot.duration) if shot.duration is not None else 'Unresolved'}",
                f"- Source range: [{shot.source_start}, {shot.source_end})",
                f"- Timing: {shot.timing_status}{' — ' + shot.timing_note if shot.timing_note else ''}",
                f"- Shot type: {shot.shot_type}", f"- Review: {shot.review_status}",
                f"- Foreground: {shot.foreground}", f"- Background: {shot.background}",
                f"- Environment: {shot.environment}", f"- Character actions: {shot.character_actions}",
                f"- Camera framing: {shot.camera_framing}", f"- Camera angle: {shot.camera_angle}",
                f"- Camera position: {shot.camera_position}", f"- Camera movement: {shot.camera_movement}",
                f"- Lighting: {shot.lighting}", f"- Atmosphere: {shot.atmosphere}",
                f"- Transition: {shot.transition}", f"- Suggested motion: {shot.suggested_motion}", "",
                "**Visual description:**", "", shot.visual_description, "", "**Original prose:**", "",
                fence + "text", shot.original_prose, fence, ""])
    path.write_text("\n".join(lines), encoding="utf-8")
