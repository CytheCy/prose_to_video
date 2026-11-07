"""Broader prose continuity and layered visual briefs for detected scenes."""
from __future__ import annotations

import json
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict

RenderType = Literal["cinematic", "concept_map", "blueprint", "cad_visualization", "product_showcase"]


class SceneContinuity(BaseModel):
    model_config = ConfigDict(extra="forbid")
    notes: str


class SceneVisualDescription(BaseModel):
    model_config = ConfigDict(extra="forbid")
    visual_description: str
    render_type: RenderType = "cinematic"


def earlier_paragraph_contexts(text, ranges, sections, provider, settings, cancel, progress):
    """Snapshot earlier prose only; carry established details forward across paragraphs."""
    from prose_to_video.pipeline.scene_detection import AnalysisCanceled, _scene_prose

    analyze = getattr(provider, "summarize_scene_context", None)
    if not callable(analyze):
        return None
    limit = settings.chunk_chars
    note_limit = limit // 3
    step = limit - note_limit
    headings = [(s.heading_start, s.heading_end) for s in sections if s.heading_start is not None]
    contexts = {}
    notes = ""
    for number, (start, end) in enumerate(ranges, 1):
        if cancel.is_set():
            raise AnalysisCanceled("Scene detection canceled")
        contexts[start] = notes
        if not any(s.included and s.body_start < end and s.end > start for s in sections):
            continue
        progress("Reading earlier paragraph details", number, len(ranges))
        prose = _scene_prose(text, start, end, headings)
        for offset in range(0, len(prose), step):
            if cancel.is_set():
                raise AnalysisCanceled("Scene detection canceled")
            result = analyze(json.dumps({
                "source_prose": prose[offset:offset + step],
                "previous_notes": notes,
                "notes_character_limit": note_limit,
            }, ensure_ascii=False))
            notes = SceneContinuity.model_validate(result).notes[:note_limit]
    if cancel.is_set():
        raise AnalysisCanceled("Scene detection canceled")
    return contexts


def enrich_scene_visuals(text, scenes, sections, provider, settings, cancel, progress, library=None, *, contexts=None):
    """Describe each paragraph using continuity from earlier paragraphs only."""
    from prose_to_video.pipeline.scene_detection import AnalysisCanceled, _scene_prose

    analyze = getattr(provider, "summarize_scene_context", None)
    describe = getattr(provider, "generate_scene_visual", None)
    if not callable(analyze) or not callable(describe):
        return scenes

    def check_cancel():
        if cancel.is_set():
            raise AnalysisCanceled("Scene detection canceled")

    limit = settings.chunk_chars
    note_limit = limit // 3
    headings = [(s.heading_start, s.heading_end) for s in sections if s.heading_start is not None]
    if contexts is None:
        contexts = earlier_paragraph_contexts(
            text, [(scene.start, scene.end) for scene in scenes],
            sections, provider, settings, cancel, progress)

    entities = library.entities() if library else []
    style = library.prompt_style_values(library.style()) if library else {}
    for number, scene in enumerate(scenes, 1):
        check_cancel()
        if not scene.production_included:
            continue
        progress("Writing visual scene descriptions", number, len(scenes))
        notes = contexts.get(scene.start, "")
        # Resolve catalog names/aliases against scene participants and location, never
        # against earlier continuity notes (which may contain offstage characters).
        names = " ".join([*scene.characters, scene.location or ""])
        references = []
        for entity in entities:
            if entity.review_status == "Rejected":
                continue
            if any(re.search(r"(?<!\w)" + re.escape(name) + r"(?!\w)", names, re.I)
                   for name in [entity.name, *entity.aliases]):
                references.append({
                    "name": entity.name, "type": entity.entity_type,
                    "description": entity.description, "appearance": entity.appearance_description,
                    "features": entity.distinguishing_features, "materials": entity.colors_materials,
                    "proportions": entity.size_proportions, "clothing": entity.clothing_accessories,
                })
        # Keep all requests bounded even with large catalogs or very long scenes.
        style_text = "\n".join(f"{key}: {value}" for key, value in style.items() if value)
        reference_text = "\n".join(
            "; ".join(f"{key}: {value}" for key, value in reference.items() if value)
            for reference in references)
        shared = ("STYLE: " + style_text[:note_limit // 3] +
                  "\nCHARACTERS AND PLACES: " + reference_text[:note_limit * 2 // 3])[:note_limit]
        prose = _scene_prose(text, scene.start, scene.end, headings)
        source_step = max(1, limit - len(notes) - len(shared))
        descriptions = []
        render_type = "cinematic"
        for offset in range(0, len(prose), source_step):
            check_cancel()
            result = describe(json.dumps({
                "source_prose": prose[offset:offset + source_step],
                "earlier_paragraph_continuity": notes,
                "visual_reference_notes": shared,
                "part": len(descriptions) + 1,
            }, ensure_ascii=False))
            result = SceneVisualDescription.model_validate(result)
            if not result.visual_description.strip():
                raise ValueError("AI returned an empty visual scene description")
            descriptions.append(result.visual_description.strip())
            if len(descriptions) == 1:
                render_type = result.render_type
        scene.visual_description = "\n\n".join(descriptions)
        scene.render_type = render_type
    check_cancel()
    return scenes
