"""Coverage-preserving storyboard edits. Prose and word alignment remain immutable."""
from __future__ import annotations
from uuid import uuid4
from prose_to_video.models.shot import Shot, Storyboard
from prose_to_video.models.alignment import AlignmentRun


def _finish(board: Storyboard, shots: list[Shot], text: str, audio_duration: float | None = None) -> Storyboard:
    shots = [Shot.model_validate(s.model_copy(update={"sequence": i}).model_dump()) for i, s in enumerate(shots, 1)]
    ready = all(s.timing_status == "fully timed" for s in shots)
    updated = Storyboard.model_validate(board.model_copy(update={"shots": shots, "status": "ready" if ready else "draft"}).model_dump())
    updated.validate_against(text, audio_duration)
    return updated


def update_shot(board: Storyboard, identifier: str, changes: dict, text: str,
                audio_duration: float | None = None) -> Storyboard:
    allowed = {"title", "visual_description", "foreground", "background", "environment", "character_actions",
               "camera_framing", "camera_angle", "camera_position", "camera_movement", "lighting",
               "atmosphere", "shot_type", "transition", "suggested_motion", "ai_notes", "review_status",
               "audio_start", "audio_end", "timing_status", "timing_note", "characters", "objects",
               "character_appearances", "entity_refs"}
    if set(changes) - allowed:
        raise ValueError("Unsupported shot edit")
    shots = list(board.shots)
    i = next((i for i, s in enumerate(shots) if s.identifier == identifier), None)
    if i is None: raise ValueError("Shot does not exist")
    changed = any(key != "review_status" and getattr(shots[i], key) != value for key, value in changes.items())
    status = "Manually edited" if changed else changes.get("review_status", shots[i].review_status)
    shots[i] = Shot.model_validate(shots[i].model_copy(update={**changes, "review_status": status,
                                                         "manually_edited": shots[i].manually_edited or changed}).model_dump())
    return _finish(board, shots, text, audio_duration)


def split_shot(board: Storyboard, identifier: str, source_offset: int, text: str,
               alignment: AlignmentRun | None = None) -> Storyboard:
    shots = list(board.shots)
    i = next((i for i, s in enumerate(shots) if s.identifier == identifier), None)
    if i is None: raise ValueError("Shot does not exist")
    old = shots[i]
    if not old.source_start < source_offset < old.source_end:
        raise ValueError("Split offset must be inside the shot")
    anchor = next((w for w in alignment.words if w.source_start == source_offset and w.audio_start is not None), None) if alignment else None
    boundary = anchor.audio_start if anchor and old.audio_start is not None and old.audio_end is not None and old.audio_start < anchor.audio_start < old.audio_end else None
    first = Shot.model_validate(old.model_copy(update={"source_end": source_offset,
        "original_prose": text[old.source_start:source_offset], "audio_end": boundary,
        "timing_status": "fully timed" if boundary and old.timing_status == "fully timed" else "partially timed" if old.audio_start is not None else "untimed",
        "timing_note": "Manual split; review description", "manually_edited": True,
        "review_status": "Manually edited"}).model_dump())
    second = Shot.model_validate(old.model_copy(update={"identifier": str(uuid4()),
        "source_start": source_offset, "original_prose": text[source_offset:old.source_end],
        "audio_start": boundary, "title": old.title + " (continued)",
        "timing_status": "fully timed" if boundary and old.timing_status == "fully timed" else "partially timed" if old.audio_end is not None else "untimed",
        "timing_note": "Manual split; review description", "manually_edited": True,
        "review_status": "Manually edited"}).model_dump())
    return _finish(board, shots[:i] + [first, second] + shots[i+1:], text,
                   alignment.audio_duration if alignment else None)


def merge_shots(board: Storyboard, identifier: str, text: str) -> Storyboard:
    shots = list(board.shots)
    i = next((i for i, s in enumerate(shots) if s.identifier == identifier), None)
    if i is None or i + 1 >= len(shots): raise ValueError("Select a shot with a following shot")
    a, b = shots[i:i+2]
    joined = Shot.model_validate(a.model_copy(update={"source_end": b.source_end,
        "original_prose": text[a.source_start:b.source_end], "audio_end": b.audio_end,
        "visual_description": a.visual_description + "\n" + b.visual_description,
        "character_actions": "\n".join(filter(None, (a.character_actions, b.character_actions))),
        "timing_status": "fully timed" if a.timing_status == b.timing_status == "fully timed" else "partially timed",
        "timing_note": "Merged; review combined description", "manually_edited": True,
        "review_status": "Manually edited"}).model_dump())
    return _finish(board, shots[:i] + [joined] + shots[i+2:], text)


def delete_shot(board: Storyboard, identifier: str, text: str, coverage: str) -> Storyboard:
    shots = list(board.shots)
    i = next((i for i, s in enumerate(shots) if s.identifier == identifier), None)
    if i is None or len(shots) == 1: raise ValueError("Cannot delete this shot")
    if coverage not in {"previous", "next"}:
        raise ValueError("Choose which adjacent shot covers the deleted interval")
    if coverage == "previous" and i == 0 or coverage == "next" and i == len(shots)-1:
        raise ValueError("No adjacent shot in the selected direction")
    removed = shots.pop(i)
    target = i-1 if coverage == "previous" else i
    other = shots[target]
    start, end = min(other.source_start, removed.source_start), max(other.source_end, removed.source_end)
    shots[target] = Shot.model_validate(other.model_copy(update={"source_start": start,
        "source_end": end, "original_prose": text[start:end],
        "audio_start": removed.audio_start if coverage == "next" else other.audio_start,
        "audio_end": removed.audio_end if coverage == "previous" else other.audio_end,
        "manually_edited": True, "review_status": "Manually edited",
        "timing_note": "Extended over deleted shot; review visual description"}).model_dump())
    return _finish(board, shots, text)


def insert_shot(board: Storyboard, after_id: str, source_offset: int, text: str,
                alignment: AlignmentRun | None = None) -> Storyboard:
    # Insertion partitions the selected shot at an aligned source anchor.
    board = split_shot(board, after_id, source_offset, text, alignment)
    shots = list(board.shots)
    i = next(i for i, s in enumerate(shots) if s.identifier == after_id)
    shots[i+1] = Shot.model_validate(shots[i+1].model_copy(update={
        "title": "New shot", "visual_description": "Describe the visible moment here.",
        "foreground": "", "background": "", "character_actions": "",
        "review_status": "Manually edited", "manually_edited": True}).model_dump())
    return _finish(board, shots, text, alignment.audio_duration if alignment else None)

def swap_visuals_with_next(board: Storyboard, identifier: str, text: str) -> Storyboard:
    """Reorder visual ideas while keeping source/timeline slots in narrative order."""
    shots = list(board.shots)
    i = next((i for i, s in enumerate(shots) if s.identifier == identifier), None)
    if i is None or i + 1 >= len(shots): raise ValueError("Select a shot with a following shot")
    fields = ("title", "shot_type", "camera_framing", "camera_angle", "camera_position", "camera_movement",
              "foreground", "background", "environment", "characters", "character_appearances",
              "character_actions", "objects", "lighting", "atmosphere", "visual_description",
              "transition", "suggested_motion", "ai_notes", "entity_refs")
    a, b = shots[i:i+2]
    shots[i] = Shot.model_validate(a.model_copy(update={**{key: getattr(b,key) for key in fields},
        "review_status":"Manually edited", "manually_edited":True,
        "timing_note":"Visual plan moved; verify against this prose passage"}).model_dump())
    shots[i+1] = Shot.model_validate(b.model_copy(update={**{key: getattr(a,key) for key in fields},
        "review_status":"Manually edited", "manually_edited":True,
        "timing_note":"Visual plan moved; verify against this prose passage"}).model_dump())
    return _finish(board, shots, text)
