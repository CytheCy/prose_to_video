"""AI chooses source-anchored visual changes; Python owns all timestamps."""
from __future__ import annotations

import json
import difflib
import re
from threading import Event
from typing import Literal, Protocol
from pydantic import BaseModel, ConfigDict, Field

from prose_to_video.models.scene import Scene, source_hash
from prose_to_video.models.alignment import AlignmentRun
from prose_to_video.models.shot import Shot, Storyboard, StoryboardSettings
from prose_to_video.pipeline.text_matching import sentence_ranges

class PlanningCanceled(Exception):
    pass

class ShotIdea(BaseModel):
    model_config = ConfigDict(extra="forbid")
    start_id: int = Field(ge=0)
    title: str
    shot_type: Literal["Establishing", "Extreme wide", "Wide", "Medium", "Close-up",
                       "Extreme close-up", "Over-the-shoulder", "Point-of-view", "Insert",
                       "Environmental detail"] = "Medium"
    camera_framing: str = ""
    camera_angle: str = ""
    camera_position: str = ""
    camera_movement: Literal["Static", "Slow pan left", "Slow pan right", "Tilt up", "Tilt down",
                             "Push in", "Pull back", "Tracking", "Slow lateral", "Aerial"] = "Static"
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

class ShotIdeas(BaseModel):
    model_config = ConfigDict(extra="forbid")
    shots: list[ShotIdea]

class ShotProvider(Protocol):
    def propose_shots(self, payload: str) -> ShotIdeas: ...

def scene_definition_hash(scene: Scene) -> str:
    import hashlib
    data = {"id": scene.identifier, "start": scene.start, "end": scene.end}
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()

def plan_scene(scene: Scene, manuscript: str, settings: StoryboardSettings, provider: ShotProvider,
               alignment: AlignmentRun | None = None, model_name: str = "mock",
               context: str = "", chunk_chars: int = 12000, cancel: Event | None = None) -> Storyboard:
    if scene.text != manuscript[scene.start:scene.end]:
        raise ValueError("Scene text does not match current manuscript")
    if alignment and alignment.prose_hash != source_hash(manuscript):
        raise ValueError("Audio alignment is outdated for this prose")
    if chunk_chars < 1000:
        raise ValueError("Shot planning chunk size is too small")
    ranges = sentence_ranges(scene.text)
    if not ranges:
        ranges = [(0, len(scene.text))]
    anchors = []
    for a, b in ranges:
        while b - a > chunk_chars:
            candidate = scene.text.rfind(" ", a + 1, a + chunk_chars + 1)
            if candidate <= a:
                raise ValueError("A source token exceeds the shot planning chunk size")
            anchors.append((scene.start + a, scene.start + candidate + 1))
            a = candidate + 1
        if a < b:
            anchors.append((scene.start + a, scene.start + b))
    # The first shot owns leading whitespace or headings before the first sentence.
    chunks = []
    current = []
    current_size = 0
    for i, (a, b) in enumerate(anchors):
        size = b - a
        if current and current_size + size > chunk_chars:
            chunks.append(current)
            current, current_size = [], 0
        current.append(i)
        current_size += size
    if current:
        chunks.append(current)
    ideas: dict[int, ShotIdea] = {}
    for indices in chunks:
        if cancel and cancel.is_set():
            raise PlanningCanceled("Storyboard generation canceled")
        labeled = "\n".join(f"[ID {i}] {manuscript[anchors[i][0]:anchors[i][1]]}" for i in indices)
        payload = json.dumps({"scene": scene.model_dump(exclude={"text"}), "context": context[-1200:],
                              "settings": settings.model_dump(), "target_seconds": settings.target_range,
                              "sentences": labeled, "valid_ids": indices,
                              "instruction": "Choose meaningful visual changes. Return one shot for the first ID only when it begins a new visual moment; include ID 0 for the first chunk. Do not force a shot at chunk edges. Preserve chronology and established appearances. Do not provide timestamps."}, ensure_ascii=False)
        try:
            response = provider.propose_shots(payload)
            response = ShotIdeas.model_validate(response)
        except Exception as exc:
            if isinstance(exc, PlanningCanceled):
                raise
            raise ValueError(f"Invalid AI shot response: {exc}") from exc
        for idea in response.shots:
            if idea.start_id not in indices or idea.start_id in ideas:
                raise ValueError("Invalid or duplicate source anchor in AI shot response")
            ideas[idea.start_id] = idea
    if cancel and cancel.is_set():
        raise PlanningCanceled("Storyboard generation canceled")
    if 0 not in ideas:
        raise ValueError("AI shot plan must start at the scene's first sentence")
    ordered = sorted(ideas.items())
    timing = next((s for s in alignment.scenes if s.scene_id == scene.identifier and s.source_start == scene.start and s.source_end == scene.end), None) if alignment else None
    coverage_start = timing.audio_start if timing else None
    coverage_end = timing.audio_end if timing else None
    scene_pause_note = ""
    if timing and alignment:
        previous_scene = max((s for s in alignment.scenes if s.source_end <= scene.start and s.audio_end is not None),
                             key=lambda s: s.source_end, default=None)
        next_scene = min((s for s in alignment.scenes if s.source_start >= scene.end and s.audio_start is not None),
                         key=lambda s: s.source_start, default=None)
        if settings.pause_policy == "next" and previous_scene and coverage_start is not None and previous_scene.audio_end < coverage_start:
            pause = coverage_start - previous_scene.audio_end
            if pause >= settings.long_pause_threshold:
                scene_pause_note = f"{pause:.2f}s inter-scene pause assigned to this scene's first shot"
            coverage_start = previous_scene.audio_end
        if settings.pause_policy == "previous" and next_scene and coverage_end is not None and next_scene.audio_start > coverage_end:
            pause = next_scene.audio_start - coverage_end
            if pause >= settings.long_pause_threshold:
                scene_pause_note = f"{pause:.2f}s inter-scene pause assigned to this scene's last shot"
            coverage_end = next_scene.audio_start
    words = [w for w in alignment.words if scene.start <= w.source_start < scene.end] if alignment else []
    boundaries: list[float | None] = [coverage_start]
    uncertain: list[bool] = []
    pause_notes: dict[int, str] = {}
    for idx, _ in ordered[1:]:
        a, b = anchors[idx]
        word = next((w for w in words if a <= w.source_start < b and w.audio_start is not None), None)
        # An anchor with unmatched words before its first verified word is uncertain.
        preceding = [w for w in words if a <= w.source_start < (word.source_start if word else b)]
        previous = next((w for w in reversed(words) if w.source_end <= a and w.audio_end is not None), None)
        pause = word.audio_start - previous.audio_end if word and previous else 0
        boundary = (previous.audio_end if settings.pause_policy == "next" and previous and pause > 0
                    else word.audio_start if word else None)
        boundaries.append(boundary)
        if pause >= settings.long_pause_threshold and pause > 0:
            pause_notes[idx] = f"{pause:.2f}s narration pause assigned to the {'next' if settings.pause_policy == 'next' else 'previous'} shot"
        uncertain.append(word is None or any(w.audio_start is None for w in preceding))
    boundaries.append(coverage_end)
    shots = []
    canonical_names = list(scene.characters)
    known_appearances: dict[str, str] = {}
    for number, (item, next_item) in enumerate(zip(ordered, ordered[1:] + [(len(anchors), None)]), 1):
        idx, idea = item
        end_idx = next_item[0]
        start = scene.start if number == 1 else anchors[idx][0]
        end = scene.end if end_idx == len(anchors) else anchors[end_idx][0]
        a, b = boundaries[number-1:number+1]
        section_words = [w for w in words if start <= w.source_start < end]
        reliable = bool(section_words) and all(w.audio_start is not None for w in section_words)
        reliable &= not (number > 1 and uncertain[number-2]) and not (number <= len(uncertain) and uncertain[number-1])
        valid = a is not None and b is not None and b > a and (not alignment or b <= alignment.audio_duration + .001)
        duration_ok = valid and (settings.allow_duration_exceptions or settings.minimum <= b-a <= settings.maximum)
        status = "fully timed" if reliable and duration_ok else "partially timed" if a is not None or b is not None else "untimed"
        notes = []
        if not reliable: notes.append("Unmatched or uncertain word anchor; review timing")
        if valid and not duration_ok: notes.append("Shot duration outside configured limits; review before rendering")
        if not valid and a is not None and b is not None: notes.append("Conflicting aligned boundaries; review timing")
        if idx in pause_notes: notes.append(pause_notes[idx])
        if scene_pause_note and ((settings.pause_policy == "next" and number == 1) or
                                 (settings.pause_policy == "previous" and number == len(ordered))):
            notes.append(scene_pause_note)
        data = idea.model_dump(exclude={"start_id"})
        def canonical(name: str) -> str:
            key = re.sub(r"[^\w]", "", name.casefold())
            for known in canonical_names:
                other = re.sub(r"[^\w]", "", known.casefold())
                if key == other or (len(key) >= 4 and key[:1] == other[:1] and
                                    difflib.SequenceMatcher(None, key, other).ratio() >= .82):
                    return known
            canonical_names.append(name)
            return name
        data["characters"] = list(dict.fromkeys(canonical(name) for name in data["characters"]))
        appearances = {canonical(name): detail for name, detail in data["character_appearances"].items()}
        for name in data["characters"]:
            if name in known_appearances and not appearances.get(name):
                appearances[name] = known_appearances[name]
            elif name in known_appearances and appearances.get(name) != known_appearances[name]:
                data["ai_notes"] = (data["ai_notes"] + "\n" if data["ai_notes"] else "") + \
                    f"Review appearance continuity for {name}; this shot differs from the previous description."
        for name, description in appearances.items():
            if description and name not in known_appearances:
                known_appearances[name] = description
        data["character_appearances"] = appearances
        def entity_key(name: str) -> str:
            return re.sub(r"[^\w]", "", name.casefold())
        data["entity_refs"] = list(dict.fromkeys([*data["entity_refs"],
            *(f"character:{entity_key(name)}" for name in data["characters"]),
            *(f"prop:{entity_key(name)}" for name in data["objects"])]))
        shots.append(Shot(scene_id=scene.identifier, sequence=number, source_start=start,
                          source_end=end, original_prose=manuscript[start:end],
                          audio_start=a, audio_end=b if b is not None and (a is None or b > a) else None,
                          timing_status=status, timing_note="; ".join(notes), **data))
    ready = all(s.timing_status == "fully timed" for s in shots)
    board = Storyboard(scene_id=scene.identifier, scene_start=scene.start, scene_end=scene.end,
                       scene_hash=source_hash(scene.text), prose_hash=source_hash(manuscript),
                       audio_hash=alignment.audio_hash if alignment else None,
                       alignment_id=alignment.identifier if alignment else None,
                       scene_definition_hash=scene_definition_hash(scene), model_name=model_name,
                       settings=settings, settings_fingerprint=settings.fingerprint(),
                       coverage_start=coverage_start, coverage_end=coverage_end,
                       status="ready" if ready else "draft", shots=shots)
    board.validate_against(manuscript, alignment.audio_duration if alignment else None)
    return board
