"""Paragraph-based scenes with source-owned offsets and complete partition checks."""

from __future__ import annotations

import re
from dataclasses import dataclass
from threading import Event
from typing import Callable

from prose_to_video.models.scene import Scene, validate_partition
from prose_to_video.pipeline.manuscript import ManuscriptSection, classify_narrative, parse_manuscript
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
    # Manuscripts may put consecutive paragraphs on separate lines without
    # blank lines. Keep source whitespace attached to the preceding scene.
    starts = [0]
    for line in re.finditer(r"[^\r\n]*(?:\r\n|\n|\r|$)", text):
        if line.start() and line.group().strip():
            starts.append(line.start())
    return starts


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


def _description(provider: SceneProvider, text: str, maximum: int, cancel: Event,
                 previous_context: str = "", context_limit: int = 1500) -> SceneDescription:
    parts = []
    contextual = getattr(provider, "describe_scene_with_context", None)
    context_limit = min(context_limit, maximum // 4) if contextual else 0
    step = maximum - context_limit
    for start in range(0, len(text), step):
        if cancel.is_set():
            raise AnalysisCanceled("Scene detection canceled")
        passage = text[start:start + step]
        context = (previous_context + "\n" + text[:start])[-context_limit:].strip() if context_limit else ""
        parts.append(contextual(passage, context) if contextual else provider.describe_scene(passage))
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


def _scene_prose(text: str, start: int, end: int,
                 heading_ranges: list[tuple[int, int]]) -> str:
    """Return scene prose without unspoken heading text."""
    parts = []
    position = start
    for heading_start, heading_end in heading_ranges:
        if heading_end <= position or heading_start >= end:
            continue
        parts.append(text[position:max(position, heading_start)])
        position = min(end, heading_end)
    parts.append(text[position:end])
    return "".join(parts).strip()


def detect_scenes(
    text: str, provider: SceneProvider, settings: AISettings,
    cancel: Event | None = None, progress: Callable[[str, int, int], None] | None = None,
    sections: list[ManuscriptSection] | None = None,
) -> list[Scene]:
    settings.validate()
    if not text.strip():
        raise ValueError("The imported prose is empty")
    cancel = cancel or Event()
    progress = progress or (lambda stage, current, total: None)
    sections = sections if sections is not None else parse_manuscript(text)
    heading_ranges = [(s.heading_start, s.heading_end) for s in sections
                      if s.heading_start is not None]
    offsets = [0]
    pending_heading = None
    prose_paragraphs = 0
    for line in re.finditer(r"[^\r\n]*(?:\r\n|\n|\r|$)", text):
        if not line.group().strip():
            continue
        start = line.start()
        if any(begin <= start < end for begin, end in heading_ranges):
            if prose_paragraphs and pending_heading is None:
                pending_heading = start
            continue
        if prose_paragraphs:
            offsets.append(pending_heading if pending_heading is not None else start)
        prose_paragraphs += 1
        pending_heading = None
    if not prose_paragraphs:
        raise ValueError("The manuscript has no prose paragraphs to turn into scenes")
    scenes = []
    for sequence, (start, end) in enumerate(zip(offsets, offsets[1:] + [len(text)]), 1):
        if cancel.is_set():
            raise AnalysisCanceled("Scene detection canceled")
        progress("Describing scenes", sequence, len(offsets))
        applicable = [s for s in sections if s.start < end and s.end > start]
        included = any(s.included and s.body_start < end and s.end > start for s in applicable)
        prose = _scene_prose(text, start, end, heading_ranges)
        if included:
            # Stay within the current included section: headings and editorial material
            # are never pronoun evidence. The preceding text cannot change source offsets.
            section = next((s for s in applicable if s.included and s.body_start < end and s.end > start), None)
            context_start = max(section.body_start, start - settings.overlap_chars) if section else start
            previous_context = _scene_prose(text, context_start, start, heading_ranges) if context_start < start else ""
            description = _description(provider, prose, settings.chunk_chars, cancel,
                                       previous_context, settings.overlap_chars)
            details = description.model_dump()
        else:
            details = {"title": "Excluded manuscript material", "boundary_reason": "Production inclusion map"}
        formats, viewpoint = classify_narrative(prose)
        scenes.append(Scene(sequence=sequence, start=start, end=end, text=text[start:end],
                            narrative_formats=formats, viewpoint_category=viewpoint,
                            production_included=included, **details))
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
