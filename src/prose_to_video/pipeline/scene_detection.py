"""Chunked scene detection with source-owned offsets and complete partition checks."""

from __future__ import annotations

import re
from dataclasses import dataclass
from threading import Event
from typing import Callable

from prose_to_video.models.scene import Scene, validate_partition
from prose_to_video.pipeline.providers import AISettings, SceneDescription, SceneProvider


class AnalysisCanceled(Exception):
    pass


@dataclass(frozen=True)
class Unit:
    identifier: int
    start: int
    end: int
    eligible: bool


@dataclass(frozen=True)
class AnalysisChunk:
    units: tuple[Unit, ...]
    core_ids: frozenset[int]


def paragraph_starts(text: str) -> list[int]:
    starts = [0]
    for match in re.finditer(r"(?:\r?\n[ \t]*){2,}", text):
        next_start = match.end()
        if next_start < len(text) and text[next_start:].strip():
            starts.append(next_start)
    for match in re.finditer(r"(?m)^[ \t]*(?:#{1,6}[ \t]+\S|(?:\*[ \t]*){3,}|(?:-[ \t]*){3,}|(?:_[ \t]*){3,})[^\r\n]*", text):
        starts.append(match.start())
    return sorted(set(starts))


def make_units(text: str, chunk_chars: int) -> list[Unit]:
    if not text:
        return []
    starts = paragraph_starts(text) + [len(text)]
    units: list[Unit] = []
    # Reserve room for IDs and prompts, and split oversized paragraphs without
    # making the resulting pieces eligible scene starts.
    maximum = max(200, chunk_chars - 500)
    for begin, end in zip(starts, starts[1:]):
        position = begin
        while position < end:
            stop = min(end, position + maximum)
            units.append(Unit(len(units) + 1, position, stop, position == begin))
            position = stop
    return units


def make_chunks(text: str, chunk_chars: int = 12000, overlap_chars: int = 1500) -> list[AnalysisChunk]:
    if chunk_chars < 1000 or overlap_chars < 0 or 2 * overlap_chars >= chunk_chars:
        raise ValueError("Invalid chunk size or overlap")
    units = make_units(text, chunk_chars)
    if not units:
        return []
    def cost(unit: Unit) -> int:
        return len(text[unit.start:unit.end]) + len(f"[ID {unit.identifier}; context only] ") + 1

    budget = chunk_chars - 100  # reserve room for a bounded repair instruction
    target = max(200, budget - 2 * overlap_chars)
    cores: list[tuple[int, int]] = []
    first = 0
    while first < len(units):
        last = first + 1
        total = cost(units[first])
        while last < len(units) and total + cost(units[last]) <= target:
            total += cost(units[last])
            last += 1
        cores.append((first, last))
        first = last
    result = []
    for first, last in cores:
        left, right = first, last
        total = sum(cost(u) for u in units[first:last])
        while left > 0 and total + cost(units[left - 1]) <= budget:
            if left < first and units[first].start - units[left - 1].start > overlap_chars:
                break
            left -= 1
            total += cost(units[left])
        while right < len(units) and total + cost(units[right]) <= budget:
            if right > last and units[right].end - units[last - 1].end > overlap_chars:
                break
            total += cost(units[right])
            right += 1
        result.append(AnalysisChunk(tuple(units[left:right]), frozenset(u.identifier for u in units[first:last])))
    return result


def _labeled(text: str, units: tuple[Unit, ...]) -> str:
    return "\n".join(f"[ID {u.identifier}; {'eligible' if u.eligible else 'context only'}] {text[u.start:u.end]}" for u in units)


def _description(provider: SceneProvider, text: str, maximum: int, cancel: Event) -> SceneDescription:
    parts = []
    for start in range(0, len(text), maximum):
        if cancel.is_set():
            raise AnalysisCanceled("Scene detection canceled")
        parts.append(provider.describe_scene(text[start:start + maximum]))
    if len(parts) == 1:
        return parts[0]

    def unique(values: list[str]) -> list[str]:
        return list(dict.fromkeys(value for value in values if value))

    def join(name: str) -> str:
        return "\n".join(unique([getattr(part, name) for part in parts]))

    return SceneDescription(
        title=parts[0].title, location=next((p.location for p in parts if p.location), None),
        time_of_day=next((p.time_of_day for p in parts if p.time_of_day), None),
        characters=unique([item for p in parts for item in p.characters]),
        point_of_view=next((p.point_of_view for p in parts if p.point_of_view), None),
        narrative_type=next((p.narrative_type for p in parts if p.narrative_type), None),
        summary=join("summary"), environment=join("environment"),
        important_objects=unique([item for p in parts for item in p.important_objects]),
        actions=unique([item for p in parts for item in p.actions]),
        dialogue_participants=unique([item for p in parts for item in p.dialogue_participants]),
        visual_atmosphere=join("visual_atmosphere"), boundary_reason=parts[0].boundary_reason,
    )


def detect_scenes(
    text: str, provider: SceneProvider, settings: AISettings,
    cancel: Event | None = None, progress: Callable[[str, int, int], None] | None = None,
) -> list[Scene]:
    settings.validate()
    if not text.strip():
        raise ValueError("The imported prose is empty")
    cancel = cancel or Event()
    progress = progress or (lambda stage, current, total: None)
    chunks = make_chunks(text, settings.chunk_chars, settings.overlap_chars)
    all_units = {unit.identifier: unit for chunk in chunks for unit in chunk.units}
    votes: dict[int, list[bool]] = {}
    for index, chunk in enumerate(chunks, 1):
        if cancel.is_set():
            raise AnalysisCanceled("Scene detection canceled")
        progress("Finding boundaries", index, len(chunks))
        labeled = _labeled(text, chunk.units)
        if len(labeled) > settings.chunk_chars:
            raise ValueError("Analysis chunk exceeds the configured input size")
        proposed = provider.propose_boundaries(labeled)
        valid_ids = {unit.identifier for unit in chunk.units if unit.eligible}
        if any(identifier not in valid_ids for identifier in proposed.starts):
            proposed = provider.propose_boundaries(
                labeled + "\nCorrect the previous answer. Only use eligible IDs shown above.")
            if any(identifier not in valid_ids for identifier in proposed.starts):
                raise ValueError(f"AI returned an invalid paragraph ID in chunk {index}")
        for identifier in valid_ids:
            votes.setdefault(identifier, []).append(identifier in proposed.starts)

    # Overlapping chunks can disagree about a boundary. Recheck only those
    # anchors with their surrounding source context, then deterministically
    # deduplicate all accepted boundaries.
    disagreements = [identifier for identifier, answers in votes.items() if len(set(answers)) > 1]
    for index, identifier in enumerate(disagreements, 1):
        if cancel.is_set():
            raise AnalysisCanceled("Scene detection canceled")
        progress("Reconciling boundaries", index, len(disagreements))
        center = all_units[identifier].start
        near = tuple(sorted((unit for unit in all_units.values() if
                     unit.end > center - settings.chunk_chars // 3 and
                     unit.start < center + settings.chunk_chars // 3), key=lambda unit: unit.start))
        while len(_labeled(text, near)) > settings.chunk_chars and len(near) > 1:
            near = near[1:] if center - near[0].start > near[-1].end - center else near[:-1]
        proposed = provider.propose_boundaries(_labeled(text, near))
        if any(i not in {u.identifier for u in near if u.eligible} for i in proposed.starts):
            raise ValueError("AI returned an invalid paragraph ID during reconciliation")
        votes[identifier] = [identifier in proposed.starts]

    offsets = sorted({0} | {all_units[i].start for i, answers in votes.items() if any(answers)})
    offsets = [offset for offset in offsets if 0 <= offset < len(text)]
    scenes = []
    for sequence, (start, end) in enumerate(zip(offsets, offsets[1:] + [len(text)]), 1):
        if cancel.is_set():
            raise AnalysisCanceled("Scene detection canceled")
        progress("Describing scenes", sequence, len(offsets))
        description = _description(provider, text[start:end], settings.chunk_chars, cancel)
        scenes.append(Scene(sequence=sequence, start=start, end=end, text=text[start:end],
                            **description.model_dump()))
    validate_partition(text, scenes)
    return scenes


def split_scene(text: str, scenes: list[Scene], identifier: str, offset: int) -> list[Scene]:
    validate_partition(text, scenes)
    index = next((i for i, scene in enumerate(scenes) if scene.identifier == identifier), None)
    if index is None:
        raise ValueError("Scene does not exist")
    old = scenes[index]
    if offset not in paragraph_starts(text) or not old.start < offset < old.end:
        raise ValueError("Choose a paragraph boundary inside the selected scene")
    left = old.model_copy(update={"end": offset, "text": text[old.start:offset],
                                  "summary": "", "actions": [], "boundary_reason": "Manual split; review description",
                                  "review_status": "Not reviewed"})
    right = Scene(sequence=old.sequence + 1, start=offset, end=old.end, text=text[offset:old.end],
                  title=f"{old.title} (continued)", location=old.location,
                  characters=old.characters.copy(), boundary_reason="Manual split; review description")
    result = scenes[:index] + [left, right] + scenes[index + 1:]
    result = [s.model_copy(update={"sequence": i}) for i, s in enumerate(result, 1)]
    validate_partition(text, result)
    return result


def merge_with_next(text: str, scenes: list[Scene], identifier: str) -> list[Scene]:
    validate_partition(text, scenes)
    index = next((i for i, scene in enumerate(scenes) if scene.identifier == identifier), None)
    if index is None or index + 1 >= len(scenes):
        raise ValueError("Choose a scene with a following scene")
    left, right = scenes[index:index + 2]
    merged = left.model_copy(update={"end": right.end, "text": text[left.start:right.end],
                                     "summary": "", "actions": [], "environment": "",
                                     "visual_atmosphere": "", "boundary_reason": "Manual merge; review description",
                                     "review_status": "Not reviewed",
                                     "characters": list(dict.fromkeys(left.characters + right.characters)),
                                     "important_objects": list(dict.fromkeys(left.important_objects + right.important_objects))})
    result = scenes[:index] + [merged] + scenes[index + 2:]
    result = [s.model_copy(update={"sequence": i}) for i, s in enumerate(result, 1)]
    validate_partition(text, result)
    return result
