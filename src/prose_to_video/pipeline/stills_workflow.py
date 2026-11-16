"""Direct prose -> scene -> still -> narration timeline workflow.

The v2 sidecar is independent of historical workflow state. Legacy databases and
media remain untouched. Every checkpoint is replaced atomically.
"""
from __future__ import annotations
from bisect import bisect_left
import json
import math
import os
import re
import tempfile
from pathlib import Path
from threading import Event
from uuid import uuid4
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, field_validator
from prose_to_video.models.scene import Scene, source_hash, validate_partition
from prose_to_video.models.visual_treatment import TECHNICAL_PLANNING, TECHNICAL_TREATMENTS
from prose_to_video.models.camera_movement import CameraMovement, CAMERA_RENDER_FIELDS
from prose_to_video.pipeline.still_camera import still_camera
from prose_to_video.models.image_job import ImageRequest, ImageReference
from prose_to_video.models.video_render import RenderPlan, RenderSettings, PlannedSegment
from prose_to_video.pipeline.audio_transcription import source_file_hash
from prose_to_video.pipeline.character_detection import PRONOUNS
from prose_to_video.pipeline.character_appearance import (
    CharacterAppearance, APPEARANCE_INSTRUCTIONS, grounded_appearances, age_version_groups)
from prose_to_video.pipeline.scene_detection import _scene_prose
from prose_to_video.pipeline.reference_composition import (
    CHARACTER_REFERENCE_PEOPLE_ONLY, CHARACTER_REFERENCE_SUBJECTS)
from prose_to_video.utils.prose import read_prose


class Record(BaseModel):
    model_config = ConfigDict(extra='forbid')


class StillImageSettings(Record):
    provider: Literal['OpenAI', 'ComfyUI'] = 'OpenAI'
    model: str = 'gpt-image-2.5-flare'
    width: int = Field(default=1536, ge=64, le=3840)
    height: int = Field(default=864, ge=64, le=2160)
    quality: str = 'medium'
    seed: int | None = None
    use_references: bool = True
    output_format: Literal['png', 'jpeg', 'webp'] = 'png'
    comfyui_url: str = 'http://127.0.0.1:8188'
    workflow_path: str = ''
    workflow_mapping: dict = Field(default_factory=dict)


class Identity(Record):
    identifier: str = Field(default_factory=lambda: str(uuid4()))
    kind: Literal['Character', 'Place']
    name: str
    aliases: list[str] = Field(default_factory=list)
    description: str
    references: list[str] = Field(default_factory=list)  # project-relative assets
    manual: bool = False
    part_ids: list[str] = Field(default_factory=list)
    character_appearances: list[CharacterAppearance] = Field(default_factory=list)
    character_group_id: str | None = None
    character_source_part_ids: list[str] = Field(default_factory=list)


class Still(Record):
    identifier: str = Field(default_factory=lambda: str(uuid4()))
    scene_id: str
    source_hash: str
    start: int
    end: int
    excerpt: str
    path: str
    prompt: str
    request: dict = Field(default_factory=dict)
    identities: list[str] = Field(default_factory=list)
    references: list[str] = Field(default_factory=list)
    provider: str = 'import'
    job_id: str | None = None
    dependency: str = ''


class Interval(Record):
    scene_id: str
    start: float
    end: float
    speech_start: float
    speech_end: float
    manual: bool = False
    estimated: bool = False


class Workflow(Record):
    version: int = 2
    prose_hash: str = ''
    style: str = 'Cohesive, restrained illustrated story'
    world_prompt: str = ''
    world_building_hash: str = ''
    density: Literal['Low', 'Medium', 'High'] = 'Medium'
    scene_detection: Literal['ai', 'paragraph'] = 'ai'
    analysis_scene_detection: Literal['ai', 'paragraph'] = 'ai'
    image_settings: StillImageSettings = Field(default_factory=StillImageSettings)
    render_settings: RenderSettings = Field(default_factory=RenderSettings)
    catalog_hash: str = ''
    character_appearance_analysis_version: int = 0
    selected_part_ids: list[str] = Field(default_factory=list)
    part_world_prompts: dict[str, str] = Field(default_factory=dict)
    part_world_hashes: dict[str, str] = Field(default_factory=dict)
    scene_world_prompts: dict[str, str] = Field(default_factory=dict)
    catalogs: list[Identity] = Field(default_factory=list)
    reference_identities: list[Identity] = Field(default_factory=list)
    scenes: list[Scene] = Field(default_factory=list)
    stills: list[Still] = Field(default_factory=list)
    selected: dict[str, str] = Field(default_factory=dict)
    timeline: list[Interval] = Field(default_factory=list)
    timeline_audio_hash: str = ''
    timeline_mode: Literal['nearest', 'strict'] = 'nearest'
    analysis_plan: list[tuple[int, int, bool]] = Field(default_factory=list)
    analysis_inclusion: str = ''
    checkpoints: dict[str, dict] = Field(default_factory=dict)
    jobs: list[dict] = Field(default_factory=list)
    migration_notes: list[str] = Field(default_factory=list)

    def save(self, root: Path):
        fd, name = tempfile.mkstemp(prefix='.workflow-', dir=root)
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as stream:
                stream.write(self.model_dump_json(indent=2)); stream.flush(); os.fsync(stream.fileno())
            os.replace(name, root / 'stills-workflow.json')
        finally:
            Path(name).unlink(missing_ok=True)

    def current_still(self, scene_id):
        return next((s for s in self.stills if s.identifier == self.selected.get(scene_id)), None)


def load_workflow(session):
    path = session.root / 'stills-workflow.json'
    if path.exists():
        return Workflow.model_validate_json(path.read_text(encoding='utf-8'))
    from prose_to_video.storage.ai_preferences import load_tti_defaults
    state = Workflow(image_settings=StillImageSettings(**load_tti_defaults()))
    from prose_to_video.storage.visual_library import VisualLibrary
    from prose_to_video.storage.generated_images import ImageStore
    from prose_to_video.storage.image_prompts import PromptStore
    library = VisualLibrary(session.root)
    for entity in library.entities():
        if entity.entity_type not in {'Character', 'Location'}:
            continue
        refs = [library.asset(r.asset_id) for r in library.references(entity.identifier)]
        state.catalogs.append(Identity(identifier=entity.identifier,
            kind='Character' if entity.entity_type == 'Character' else 'Place', name=entity.name,
            aliases=entity.aliases, description='; '.join(filter(None, [entity.description,
                entity.appearance_description, entity.distinguishing_features, entity.colors_materials,
                entity.size_proportions, entity.clothing_accessories, entity.spatial_relationship])),
            references=[r.path for r in refs if r], manual=True))
    legacy_times = {}
    digest, old_scenes = session.database.load_scenes()
    state.prose_hash = digest or ''
    for old in old_scenes:
        board = session.database.load_storyboard(old.identifier)
        shots = board.shots if board else []
        # Only migrate a whole, exact partition; uncertain mappings stay historical.
        usable = bool(shots) and shots[0].source_start == old.start and shots[-1].source_end == old.end
        usable = usable and all(a.source_end == b.source_start for a, b in zip(shots, shots[1:]))
        usable = usable and all(s.original_prose == old.text[s.source_start-old.start:s.source_end-old.start] for s in shots)
        if not usable:
            state.scenes.append(old.model_copy(update={'sequence': len(state.scenes)+1, 'source_version': state.prose_hash}))
            if shots: state.migration_notes.append(f'{old.title}: uncertain shot anchors retained in historical database')
            continue
        for shot in shots:
            data = old.model_dump()
            data.update(identifier=shot.identifier, sequence=len(state.scenes)+1, start=shot.source_start,
                end=shot.source_end, text=shot.original_prose, title=shot.title, visual_description=shot.visual_description,
                foreground=shot.foreground, background=shot.background, camera_framing=shot.camera_framing,
                camera_angle=shot.camera_angle, camera_position=shot.camera_position, camera_movement={
                    "Static": "static_hold", "Push in": "zoom_in", "Pull back": "zoom_out",
                    "Slow pan left": "pan_left", "Slow pan right": "pan_right",
                    "Tilt up": "pan_up", "Tilt down": "pan_down"}.get(shot.camera_movement,
                        shot.camera_movement if shot.camera_movement in CameraMovement.__args__ else "static_hold"), lighting=shot.lighting,
                source_version=state.prose_hash, identity_links=shot.entity_refs, characters=shot.characters,
                environment=shot.environment, important_objects=shot.objects, visual_atmosphere=shot.atmosphere)
            prompt_store = PromptStore(session.root)
            prompts = prompt_store.versions(shot.identifier)
            active_prompt = prompt_store.active(shot.identifier) or (prompts[0] if prompts else None)
            if active_prompt:
                data['final_prompt'] = active_prompt.positive_prompt
                data['negative_prompt'] = active_prompt.negative_prompt
            scene = Scene(**data); state.scenes.append(scene)
            if shot.audio_start is not None and shot.audio_end is not None:
                legacy_times[scene.identifier] = (shot.audio_start, shot.audio_end)
            image_store = ImageStore(session.root)
            legacy_selected = image_store.approved(shot.identifier)
            for asset in reversed(image_store.assets(shot.identifier)):
                snapshot = next((p for p in prompts if p.identifier == asset.prompt_id), None)
                still = Still(scene_id=scene.identifier, source_hash=state.prose_hash, start=scene.start,
                    end=scene.end, excerpt=scene.text, path=asset.path, prompt=snapshot.positive_prompt if snapshot else scene.final_prompt,
                    provider='legacy', identities=scene.identity_links, request={'legacy_asset_id': asset.identifier, 'prompt_id': asset.prompt_id})
                state.stills.append(still)
                if (not legacy_selected or asset.identifier == legacy_selected[0].identifier):
                    state.selected[scene.identifier] = still.identifier
    for scene in state.scenes:
        names = {n.casefold() for n in [*scene.characters, scene.location or '']}
        for identity in state.catalogs:
            if names & {n.casefold() for n in [identity.name, *identity.aliases]} and identity.identifier not in scene.identity_links:
                scene.identity_links.append(identity.identifier)
    included = [s for s in state.scenes if s.production_included]
    if included and all(s.identifier in legacy_times for s in included) and session.project.audio_duration:
        intervals = []
        for i, scene in enumerate(included):
            start, end = legacy_times[scene.identifier]
            intervals.append(Interval(scene_id=scene.identifier, start=0 if i==0 else start,
                end=legacy_times[included[i+1].identifier][0] if i+1<len(included) else session.project.audio_duration,
                speech_start=start, speech_end=end, manual=True))
        try:
            validate_timeline(intervals, session.project.audio_duration)
            state.timeline = intervals
            audio = session.project.resolved_audio(session.root)
            if audio and audio.is_file(): state.timeline_audio_hash = source_file_hash(audio)
        except ValueError:
            state.migration_notes.append('Legacy timing boundaries are ambiguous; preserved in the historical database.')
    if state.scenes:
        state.migration_notes.append('Legacy media and anchors imported. Valid timing boundaries preserved as manual intervals; all historical records remain in project.sqlite.')
    state.save(session.root)
    return state


class CatalogResult(Record):
    identities: list[Identity]


class WorldPromptResult(Record):
    prompt: str = Field(min_length=1)


WORLD_PROMPT_LIMIT = 750


def world_building_content(session, part_ids=None):
    """Read the project copies, including text embedded in PDF reference files."""
    from prose_to_video.storage.project_files import world_building_files
    content = []
    for path in world_building_files(session, part_ids):
        if path.suffix.casefold() == '.pdf':
            from pypdf import PdfReader
            body = '\n'.join(page.extract_text() or '' for page in PdfReader(str(path)).pages)
        elif path.suffix.casefold() == '.docx':
            from zipfile import ZipFile
            from xml.etree import ElementTree
            with ZipFile(path) as document:
                root = ElementTree.fromstring(document.read('word/document.xml'))
            ns = '{http://schemas.openxmlformats.org/wordprocessingml/2006/main}'
            body = '\n'.join(''.join(node.text or '' for node in paragraph.iter(ns + 't'))
                             for paragraph in root.iter(ns + 'p'))
        else:
            body = read_prose(path)
        if not body.strip():
            raise ValueError(f'World building file {path.name} contains no extractable text')
        content.append({'name': path.name, 'text': body})
    return content


def scene_location(world_prompt, location):
    """Keep the shared world description ahead of the scene's specific place."""
    world_prompt = world_prompt.strip()
    location = (location or '').strip()
    if world_prompt and location.startswith(world_prompt):
        return location
    return '\n'.join(part for part in (world_prompt, location) if part)


def set_world_prompt(state, prompt):
    if len(prompt.strip()) > WORLD_PROMPT_LIMIT:
        raise ValueError(f'World building prompt cannot exceed {WORLD_PROMPT_LIMIT} characters')
    old_prompt = state.world_prompt.strip()
    state.world_prompt = prompt.strip()
    for part_id in state.selected_part_ids:
        state.part_world_prompts[part_id] = state.world_prompt
    for scene in state.scenes:
        if state.selected_part_ids and not scene.production_included:
            continue
        previous = state.scene_world_prompts.get(scene.identifier, old_prompt)
        state.scene_world_prompts[scene.identifier] = state.world_prompt
        specific = (scene.location or '').strip()
        if previous and specific.startswith(previous):
            specific = specific[len(previous):].lstrip('\n').strip()
        scene.location = scene_location(state.world_prompt, specific)
        scene.prompt_dependency = dependency(state, scene)


def listed_identities(state, kind=None):
    """Filter shared identities to selected parts, keeping recurrence story-wide.

    Older workflows without part membership use their existing scene links.
    """
    if state.catalog_hash:
        selected = set(state.selected_part_ids)
        return [c for c in state.catalogs if (kind is None or c.kind == kind)
                and (selected.intersection(c.part_ids) or c.character_group_id and
                     selected.intersection(c.character_source_part_ids))
                and c.name.strip().casefold() not in PRONOUNS
                and (c.manual or c.kind == 'Place' or c.character_group_id or len(c.part_ids) > 1 or sum(
                    c.identifier in scene.identity_links or bool(
                        {n.casefold() for n in [c.name, *c.aliases]} &
                        {n.casefold() for n in scene.characters})
                    for scene in state.scenes) > 1)]
    characters = [c for c in state.catalogs if c.kind == 'Character']
    names = {}
    for character in characters:
        for name in [character.name, *character.aliases]:
            key = name.strip().casefold()
            if key and key not in PRONOUNS:
                names.setdefault(key, set()).add(character.identifier)
    appearances = {c.identifier: set() for c in characters}
    for scene in state.scenes:
        if not scene.production_included:
            continue
        participants = set(scene.identity_links)
        for name in scene.characters:
            matches = names.get(name.strip().casefold(), set())
            if len(matches) == 1:
                participants.update(matches)
        for identifier in participants & appearances.keys():
            appearances[identifier].add(scene.identifier)
    return [c for c in state.catalogs if (kind is None or c.kind == kind) and
            (c.kind != 'Character' or (c.name.strip().casefold() not in PRONOUNS and
                                      (c.character_group_id or len(appearances[c.identifier]) > 1)))]


def assign_character_versions(state, character_id, versions, sections):
    """Apply a complete version edit to a copy, preserving one owner per part."""
    original = next(c for c in state.catalogs if c.identifier == character_id and c.kind == 'Character')
    group = original.character_group_id or original.identifier
    members = [c for c in state.catalogs if c.identifier == original.identifier or c.character_group_id == group]
    source_parts = original.character_source_part_ids or original.part_ids
    allowed = set(source_parts)
    if not versions or not allowed:
        raise ValueError('This character has no recorded part appearances.')
    assigned = [p for c in versions for p in c.part_ids]
    if set(assigned) != allowed or len(assigned) != len(set(assigned)):
        raise ValueError('Assign each original part to exactly one version.')
    names = [c.name.strip().casefold() for c in versions]
    if any(not name or name in PRONOUNS for name in names) or len(names) != len(set(names)):
        raise ValueError('Give each version a different, nonempty character name.')
    member_ids = {c.identifier for c in members}
    ids = [c.identifier for c in versions]
    if len(ids) != len(set(ids)) or not member_ids.issubset(ids) or any(
            c.identifier in ids and c.identifier not in member_ids for c in state.catalogs):
        raise ValueError('Invalid character versions.')
    updated = state.model_copy(deep=True)
    replacements = []
    for version in versions:
        version = version.model_copy(deep=True)
        version.name = version.name.strip()
        version.character_group_id = group
        version.character_source_part_ids = list(source_parts)
        version.manual = True
        # Retain the prose name for identity resolution after the display name changes.
        version.aliases = list(dict.fromkeys(n for n in [*version.aliases, original.name, *original.aliases]
                                            if n.casefold() != version.name.casefold()))
        replacements.append(Identity.model_validate(version.model_dump()))
    index = min(i for i, c in enumerate(updated.catalogs) if c.identifier in member_ids)
    updated.catalogs = [c for c in updated.catalogs if c.identifier not in member_ids]
    updated.catalogs[index:index] = replacements
    names = {n.casefold() for c in members for n in [c.name, *c.aliases]} - PRONOUNS
    for scene in updated.scenes:
        if not (member_ids.intersection(scene.identity_links) or names.intersection(n.casefold() for n in scene.characters)):
            continue
        part = next((s.identifier for s in sections if s.body_start <= scene.start < s.end), None)
        owner = next((c for c in replacements if part in c.part_ids), None)
        if owner:
            scene.identity_links = list(dict.fromkeys([
                *(i for i in scene.identity_links if i not in member_ids), owner.identifier]))
            scene.characters = list(dict.fromkeys([
                *(n for n in scene.characters if n.casefold() not in names), owner.name]))
    return updated


def create_detected_age_versions(state, sections):
    """Use existing version ownership/relinking; preserve manual groups and references."""
    for character in list(state.catalogs):
        if character.kind != 'Character' or character.character_group_id or character.manual:
            continue
        if len(character.part_ids) < 2:
            continue
        groups = age_version_groups(character)
        if not groups:
            continue
        versions = []
        for index, (parts, observations) in enumerate(groups):
            anchor = observations[0]
            label = f'age {anchor.age}' if anchor.age is not None else anchor.stage.replace('_', ' ')
            version = character.model_copy(deep=True, update={
                'identifier': character.identifier if index == 0 else str(uuid4()),
                'name': f'{character.name} ({label})',
                'description': '; '.join(dict.fromkeys(o.description or o.reason for o in observations)),
                'part_ids': [p for p in character.part_ids if p in parts],
                # Do not copy one age's references to every inferred version.
                'references': [],
            })
            versions.append(version)
        # Keep existing reference images with the original version for user review.
        versions[0].references = list(character.references)
        updated = assign_character_versions(state, character.identifier, versions, sections)
        state.catalogs = updated.catalogs
        state.scenes = updated.scenes


class Beat(Record):
    first_id: int
    last_id: int


class Beats(Record):
    beats: list[Beat]


class Staging(Record):
    title: str
    summary: str
    location: str
    time_of_day: str
    characters: list[str]
    identity_links: list[str]
    point_of_view: str
    narrative_type: str
    narrative_formats: list[str]
    viewpoint_category: str
    visual_description: str
    render_type: Literal['cinematic', 'concept_map', 'blueprint', 'cad_visualization', 'product_showcase']
    explanatory_focus: str = ''
    environment: str
    important_objects: list[str]
    actions: list[str]
    dialogue_participants: list[str]
    visual_atmosphere: str
    boundary_reason: str
    foreground: str
    middle_ground: str
    background: str
    composition: str
    camera_framing: str
    camera_angle: str
    camera_position: str
    camera_movement: CameraMovement = "zoom_in"
    lighting: str
    palette_materials: str
    final_prompt: str = Field(min_length=1)
    negative_prompt: str

    @field_validator("camera_movement", mode="before")
    @classmethod
    def default_movement(cls, value):
        return value or "zoom_in"


ART_DIRECTION = '''Act as an expert storyboard artist, cinematographer and production designer.
Depict one legible instant, never multiple sequential actions. Respect chronology and reveals.
Use canonical identity descriptions, contextual clothing/age, positions, expressions, gestures,
eyelines and object placement. Describe foreground, middle ground, background, composition,
camera position/framing/angle, lighting direction, palette and materials when relevant.
Distinguish action involving technology from exposition explaining how it works, including
invented sci-fi technology. Technology vocabulary alone never requires an explanatory view.
Set explanatory_focus to one concise, source-supported idea the viewer should understand;
leave it empty for narrative action. Choose render_type by that purpose, then write ALL
staging, composition, camera fields and final_prompt to suit that treatment. For explanatory
views, omit irrelevant character/environment layers rather than forcing a cinematic room scene.
If a saved scene is supplied, its render_type and explanatory_focus are binding: regenerate
the prompt to match them, even if you would otherwise choose a different treatment.
Preserve the approved artistic medium and distinguish source facts from proposed design.
Distinguish observer's physical location from remote screens, memories and other media;
never put offstage people in frame. No invented plot facts. Restrained consistent design
choices are allowed for unspecified appearance. Final prompt must be self-contained, repeat
essential identity/place traits rather than relying on fictional names or references.
When a shared world prompt is supplied, carry its visual era, materials and mood into
the final prompt and location, while keeping the scene's specific setting clear.
Include the supplied project style and aspect ratio. No generic masterpiece adjective lists,
unintended text, captions, watermarks, collages or contradictory staging.
Choose camera_movement from the schema for animating the still over its scene duration.
Favor restrained zooms, pans or static holds suited to composition and pacing; vary movements
where appropriate. This is a video instruction; do not include movement in the still image prompt.
Do not put administrative fields such as boundary_reason in the prompt.
Return every structured field; use empty strings/lists where irrelevant.'''

ART_DIRECTION += '\n' + TECHNICAL_PLANNING + '\nTreatment definitions:\n' + '\n'.join(
    f'{name}: {instruction}' for name, instruction in TECHNICAL_TREATMENTS.items())


def treatment_prompt(prompt, render_type, explanatory_focus):
    """Carry technical composition constraints into the actual image request."""
    instruction = TECHNICAL_TREATMENTS.get(render_type)
    if not instruction:
        return prompt
    parts = [instruction] if instruction not in prompt else []
    if explanatory_focus.strip():
        focus = f'Visually communicate: {explanatory_focus.strip()}'
        if focus not in prompt:
            parts.append(focus)
    return ' '.join([*parts, prompt])

IDENTITY_LINKS = '''identity_links must contain only exact identifier values from the supplied catalog.
Never use character/place names, aliases, pronouns, or newly invented IDs in identity_links.
Use an empty list when no catalog identity is depicted.
In characters, list the canonical names of all characters actually participating in this scene,
including those referred to only by unambiguous pronouns or aliases. Resolve he/she/they and
descriptive references using the prose, earlier context and catalog; do not create a new person
for a pronoun, clothing change or unnamed reference to an existing person. Do not guess ambiguous
references or count people merely mentioned, hypothetical people, or background crowds as participants.'''


def resolve_identity_links(links, catalog):
    """Accept catalog IDs or unambiguous names; never guess or drop unknown links."""
    identifiers = {c.identifier for c in catalog}
    names = {}
    for identity in catalog:
        for name in [identity.name, *identity.aliases]:
            normalized = name.strip().casefold()
            if normalized:
                names.setdefault(normalized, set()).add(identity.identifier)
    resolved, unknown = [], []
    for link in links:
        value = link.strip()
        matches = {value} if value in identifiers else names.get(value.casefold(), set())
        if len(matches) == 1:
            identifier = next(iter(matches))
            if identifier not in resolved:
                resolved.append(identifier)
        else:
            unknown.append(link)
    return resolved, unknown


def units(text, limit):
    """Sentence IDs refer to original characters, including untouched CRLFs."""
    starts = [0] + [m.end() for m in re.finditer(r'(?<=[.!?])\s+|\r\n|[\r\n]', text) if m.end() < len(text)]
    result = []
    for begin, end in zip(starts, starts[1:] + [len(text)]):
        for offset in range(begin, end, limit):
            result.append((offset, min(end, offset + limit)))
    return result


def paragraph_spans(text):
    """Blank-line paragraphs with separators retained at their original offsets."""
    newline = r'(?:\r\n|\r(?!\n)|(?<!\r)\n)'
    separator = newline + r'[^\S\r\n]*(?:' + newline + r'[^\S\r\n]*)+'
    spans = []
    start = 0
    for match in re.finditer(separator, text):
        if text[start:match.start()].strip():
            spans.append((start, match.end()))
            start = match.end()
    if text[start:].strip():
        spans.append((start, len(text)))
    elif spans:
        spans[-1] = (spans[-1][0], len(text))
    return spans


def scene_world_prompt(state, scene):
    return state.scene_world_prompts.get(scene.identifier, state.world_prompt)


def dependency(state, scene):
    data = scene.model_dump(exclude={'final_prompt', 'negative_prompt', 'prompt_dependency', 'manual_fields'} | CAMERA_RENDER_FIELDS)
    identities = [c.model_dump(exclude={'character_appearances'} |
                               ({'character_group_id', 'character_source_part_ids'} if not c.character_group_id else set()))
                  for c in state.catalogs if c.identifier in scene.identity_links]
    return source_hash(json.dumps([data, state.style, state.image_settings.width, state.image_settings.height,
        scene_world_prompt(state, scene), identities], sort_keys=True))


def prose_ranges(text, ranges, heading_ranges):
    """Remove headings instead of attaching them to neighboring scenes."""
    from prose_to_video.utils.prose import prose_content_ranges
    return [(index, a, b) for index, (lo, hi) in enumerate(ranges)
            for a, b in prose_content_ranges(text, lo, hi, heading_ranges)]


def analyze(session, state, provider, settings, cancel=None, progress=lambda *_: None):
    cancel = cancel or Event()
    progress('Reading selected chapter exports and checking whether the story has changed')
    text = read_prose(session.project.resolved_prose(session.root))
    digest = source_hash(text)
    if not text.strip(): raise ValueError('Import nonempty prose first')
    from prose_to_video.storage.manuscript import ManuscriptStore
    from prose_to_video.pipeline.manuscript import inclusion_fingerprint
    sections = ManuscriptStore(session.database).sections(text)
    state.selected_part_ids = [s.identifier for s in sections if s.included]
    inclusion = source_hash("chapter-sources-v2" + inclusion_fingerprint(sections))
    from prose_to_video.pipeline.chapter_sources import analysis_source
    analysis_text = analysis_source(session, sections, text)
    heading_ranges = [(s.heading_start, s.heading_end) for s in sections
                      if s.heading_start is not None]
    if not _scene_prose(text, 0, len(text), heading_ranges):
        raise ValueError('The manuscript has no prose paragraphs to turn into scenes')
    if not any(s.included and _scene_prose(text, s.body_start, s.end, heading_ranges) for s in sections):
        raise ValueError('Check at least one story part with prose in Start before analyzing')
    if state.prose_hash != digest and (state.prose_hash or state.scenes or state.checkpoints):
        if cancel.is_set():
            raise ValueError('Canceled; existing analysis retained')
        # Retain only the names/aliases needed to reconnect user reference images.
        # Removed identities keep their references here without polluting the new catalog.
        references = {c.identifier: c.model_copy(update={'description': '', 'manual': False}, deep=True)
            for c in [*state.reference_identities, *state.catalogs] if c.references}
        state.reference_identities = list(references.values())
        state.catalogs = []
        state.catalog_hash = ''
        state.part_world_prompts = {}
        state.part_world_hashes = {}
        state.scene_world_prompts = {}
        state.scenes = []
        state.stills = []
        state.selected = {}
        state.timeline = []
        state.timeline_audio_hash = ''
        state.analysis_plan = []
        state.checkpoints = {}
        state.jobs = []
        state.prose_hash = digest
        state.analysis_scene_detection = state.scene_detection
        state.save(session.root)
        progress('Prose changed; rebuilding analysis and prompts, preserving reference images')
    if state.analysis_inclusion != inclusion:
        # Reuse completed work wherever the new choices still allow the same
        # scene boundary. Newly included placeholder scenes need staging.
        try:
            validate_partition(text, state.scenes)
        except ValueError:
            pass
        else:
            ranges = prose_ranges(text, [(s.start, s.end) for s in state.scenes], heading_ranges)
            from collections import Counter
            counts = Counter(index for index, _, _ in ranges)
            # A legacy scene spanning multiple parts needs new, part-local staging.
            state.scenes = [state.scenes[index].model_copy(update={
                'start': start, 'end': end, 'text': text[start:end], 'sequence': number})
                for number, (index, start, end) in enumerate(
                    (span for span in ranges if counts[span[0]] == 1), 1)]
        retained = []
        for scene in state.scenes:
            choices = {s.included for s in sections
                       if s.body_start < scene.end and s.end > scene.start
                       and _scene_prose(text, s.body_start, s.end, heading_ranges)}
            if not choices:
                retained.append(scene)
                continue
            if len(choices) != 1:
                continue
            included = choices.pop()
            if included and not scene.production_included and not scene.final_prompt:
                continue
            scene.production_included = included
            retained.append(scene)
        state.scenes = retained
        state.analysis_plan = []
        state.analysis_inclusion = inclusion
        state.save(session.root)
    spans = units(text, max(200, settings.chunk_chars // 2))
    # Catalog all source parts, regardless of the production selection. Keep
    # batches inside a part so membership is explicit, including pronoun-only appearances.
    batches = []
    for section in sections:
        catalog_text = _scene_prose(text, section.body_start, section.end, heading_ranges)
        batch, size = [], 0
        for index, (start, end) in enumerate(units(catalog_text, max(200, settings.chunk_chars // 2)), 1):
            passage = catalog_text[start:end]
            if not passage.strip():
                continue
            if batch and size + len(passage) > settings.chunk_chars:
                batches.append((section.identifier, batch)); batch, size = [], 0
            batch.append({'id': index, 'text': passage}); size += len(passage)
        if batch:
            batches.append((section.identifier, batch))
    def check():
        if cancel.is_set(): raise ValueError('Canceled; completed work saved. Analyze Story resumes it.')
        if analysis_source(session, sections, text) != analysis_text:
            raise ValueError('Selected chapter exports changed during analysis; retry analysis')
        if source_hash(read_prose(session.project.resolved_prose(session.root))) != digest:
            raise ValueError('Prose changed during analysis; completed work retained')
    def request(key, instructions, content, schema, description):
        check()
        # Identity checkpoints are independent of catalog additions. Beat
        # checkpoints also track passages, which can change as parts are added.
        cache_content = {'passages': content['passages']} if key.startswith('catalog-') else content
        cache_inputs = [digest, key, instructions, cache_content]
        if key.startswith('beats-'):
            cache_inputs.append(inclusion)
        cache_key = source_hash(json.dumps(cache_inputs, sort_keys=True))
        if cache_key in state.checkpoints:
            progress(description + ' · Reusing a saved response from the current story')
            return schema.model_validate(state.checkpoints[cache_key])
        progress(description + f' · Waiting for {settings.model} to respond')
        result = schema.model_validate(provider._request(instructions, json.dumps(content), schema))
        progress(description + ' · Checking the response and saving progress')
        if analysis_source(session, sections, text) != analysis_text:
            raise ValueError('Selected chapter exports changed during analysis; retry analysis')
        if source_hash(read_prose(session.project.resolved_prose(session.root))) != digest:
            raise ValueError('Prose changed during request; prior work retained')
        state.checkpoints[cache_key] = result.model_dump(); state.save(session.root)
        check()
        return result
    def discard_invalid(result):
        invalid = result.model_dump()
        state.checkpoints = {k:v for k,v in state.checkpoints.items() if v != invalid}
        state.save(session.root)
    for section in sections:
        if not section.included:
            continue
        files = world_building_content(session, [section.identifier])
        files_hash = source_hash(json.dumps(files, sort_keys=True)) if files else ''
        if files_hash != state.part_world_hashes.get(section.identifier, ''):
            if files:
                instructions = '''Extracting World-Building Details for Text-to-Image Prompt.
    Read every supplied world building file as context for the same story world. Return one compact,
    visual prompt that can be reused across many generated images. Synthesize the overall feel:
    place, era, architecture, materials, palette, light, atmosphere and visual mood where supported.
    Favor recurring or defining traits over isolated mentions. Do not list individual places,
    characters, events, or plot details. Do not invent unsupported specifics.
    Aim for 300–500 characters. The absolute maximum is 750 characters, including spaces.'''
                for attempt in range(2):
                    result = request(f'world-building-{attempt}', instructions,
                        {'files': files}, WorldPromptResult, 'Places · Extracting the shared world building prompt')
                    new_prompt = result.prompt.strip()
                    if new_prompt and len(new_prompt) <= WORLD_PROMPT_LIMIT:
                        break
                    discard_invalid(result)
                    if attempt == 1:
                        raise ValueError(f'AI did not return a world building prompt within {WORLD_PROMPT_LIMIT} characters; retry analysis')
                    instructions += '\nYour previous response exceeded the limit or was empty. Rewrite it more concisely.'
            else:
                new_prompt = ''
            old_prompt = state.part_world_prompts.get(section.identifier, '')
            state.part_world_prompts[section.identifier] = new_prompt
            state.part_world_hashes[section.identifier] = files_hash
            for scene in state.scenes:
                if section.body_start <= scene.start < section.end:
                    specific = (scene.location or '').strip()
                    if old_prompt and specific.startswith(old_prompt):
                        specific = specific[len(old_prompt):].strip()
                    scene.location = scene_location(new_prompt, specific)
                    state.scene_world_prompts[scene.identifier] = new_prompt
                    scene.prompt_dependency = dependency(state, scene)
            state.save(session.root)
    state.world_prompt = '\n'.join(dict.fromkeys(
        state.part_world_prompts.get(part_id, '') for part_id in state.selected_part_ids)).strip()
    # Catalog pass reads the entire manuscript before the scene pass.
    for number, (part_id, batch) in enumerate(batches if state.catalog_hash != digest or state.character_appearance_analysis_version < 1 else []):
        description = (f'Characters and places: section {number+1}/{len(batches)} · '
            'Finding names, aliases, appearances and setting details')
        result = request(f'catalog-{number}', '''Identify all participating characters and places, including one-offs and
unambiguous pronouns. Resolve aliases/context to existing stable IDs; pronouns are never global aliases.
Return new identities or canonical updates. Preserve physical traits, architecture, layout, materials,
colors and spatial relationships; age/clothing/condition changes are contextual, not new identities.
Do not invent events or relationships. Use existing IDs for existing identities. References stay empty. Return only identities participating in the supplied passages; do not return other known identities.''' + APPEARANCE_INSTRUCTIONS,
            {'passages': batch, 'known': [c.model_dump() for c in state.catalogs]}, CatalogResult, description)
        for identity in result.identities:
            if identity.kind == 'Character':
                if identity.name.strip().casefold() in PRONOUNS:
                    continue
                identity.aliases = [a for a in identity.aliases if a.strip().casefold() not in PRONOUNS]
            identity.character_appearances = grounded_appearances(identity.character_appearances, batch, part_id) if identity.kind == 'Character' else []
            identity.character_group_id = None
            identity.character_source_part_ids = []
            identity.part_ids = [part_id]
            identity.references = []  # Only locally imported, user-selected assets may condition jobs.
            identity.manual = False
            existing = next((c for c in state.catalogs if (not c.character_group_id or part_id in c.part_ids) and (c.identifier == identity.identifier or
                set(n.casefold() for n in [c.name, *c.aliases]) & set(n.casefold() for n in [identity.name, *identity.aliases]) and c.kind == identity.kind)), None)
            if existing:
                existing.part_ids = list(dict.fromkeys([*existing.part_ids, part_id]))
                for observation in identity.character_appearances:
                    if observation not in existing.character_appearances:
                        existing.character_appearances.append(observation)
                if not existing.manual: existing.description = identity.description
                existing.aliases = list(dict.fromkeys(existing.aliases + [a for a in [identity.name, *identity.aliases] if a != existing.name]))
            else:
                # Only user-owned references survive a prose reset; descriptions,
                # staging and prompts always come from the new manuscript.
                names = {n.casefold().strip() for n in [identity.name, *identity.aliases]}
                matches = [c for c in state.reference_identities if c.kind == identity.kind and
                    (c.identifier == identity.identifier or names &
                     {n.casefold().strip() for n in [c.name, *c.aliases]})]
                if len(matches) == 1:
                    identity.references = list(matches[0].references)
                    state.reference_identities.remove(matches[0])
                state.catalogs.append(identity)
        state.prose_hash = digest; state.save(session.root)
    create_detected_age_versions(state, sections)
    state.character_appearance_analysis_version = 1
    state.catalog_hash = digest
    state.save(session.root)
    if state.analysis_scene_detection != state.scene_detection:
        check()
        # Keep image records/files, but old scene anchors and timing no longer apply.
        state.scenes = []
        state.analysis_plan = []
        state.selected = {}
        state.timeline = []
        state.timeline_audio_hash = ''
        state.analysis_scene_detection = state.scene_detection
        state.save(session.root)
    if state.scenes:
        try:
            validate_partition(text, state.scenes)
            retained = []
            for index, start, end in prose_ranges(
                    text, [(s.start, s.end) for s in state.scenes], heading_ranges):
                scene = state.scenes[index]
                if (scene.start, scene.end, scene.sequence) != (start, end, len(retained) + 1):
                    scene = scene.model_copy(update={'start': start, 'end': end,
                        'text': text[start:end], 'sequence': len(retained) + 1})
                    scene.prompt_dependency = dependency(state, scene)
                retained.append(scene)
            if len({s.identifier for s in retained}) != len(retained):
                state.scenes = []
                state.analysis_plan = []
                raise ValueError('Legacy scene crosses part headings; rebuilding scene boundaries')
            removed = {s.identifier for s in state.scenes} - {s.identifier for s in retained}
            state.scenes = retained
            state.selected = {k: v for k, v in state.selected.items() if k not in removed}
            state.timeline = [i for i in state.timeline if i.scene_id not in removed]
            state.analysis_plan = [(s.start, s.end, s.production_included) for s in retained]
            state.save(session.root)
            return state  # Reopening analysis never replaces edits or split/merged beats.
        except ValueError:
            pass  # An interrupted analysis resumes its frozen plan below.
    # Exclusions are boundaries; never let a visual beat cross included/excluded material.
    cuts = sorted({0, len(text), *(s.body_start for s in sections), *(s.end for s in sections),
                   *(s.start for s in state.scenes), *(s.end for s in state.scenes)})
    blocks = []
    for lo, hi in zip(cuts, cuts[1:]):
        included = any(s.included and s.body_start <= lo < s.end for s in sections)
        local = [(max(a, lo), min(b, hi)) for a,b in spans if a < hi and b > lo]
        current, size = [], 0
        for a,b in local:
            if current and size+b-a > settings.chunk_chars:
                blocks.append((included, current)); current, size = [], 0
            current.append((a,b)); size += b-a
        if current: blocks.append((included, current))
    planned = []
    if state.scene_detection == 'paragraph' and not state.analysis_plan:
        progress('Planning one scene per paragraph · Finding paragraph boundaries and honoring excluded prose')
        # Honor manuscript exclusions without letting a scene cross their boundaries.
        for start, end in paragraph_spans(text):
            boundaries = [start, *(cut for cut in cuts if start < cut < end), end]
            for lo, hi in zip(boundaries, boundaries[1:]):
                included = any(s.included and s.body_start <= lo < s.end for s in sections)
                planned.append((lo, hi, included))
    for number, (included, block) in enumerate(
            blocks if not state.analysis_plan and state.scene_detection == 'ai' else []):
        description = (f'Planning visual beats {number+1}/{len(blocks)} · '
            f'Choosing scene boundaries from changes in action, setting and viewpoint ({state.density.lower()} image density)')
        progress(description)
        if included:
            result = request(f'beats-{number}', 'Divide passages into meaningful visual beats, one still per beat. '
                'Choose action, setting, viewpoint, reveal or composition changes, not one per sentence. '
                'For mixed action and technical explanation, use a separate explanatory beat when one '
                'image cannot clearly communicate both. Keep continuous action together otherwise. '
                'Cover every supplied ID once, in order, with contiguous inclusive first_id/last_id. '
                f'Image density: {state.density}. Low favors fewer images; High favors more.',
                {'passages': [{'id': i, 'text': analysis_text[a:b]} for i,(a,b) in enumerate(block,1)]}, Beats, description)
            pos = 1
            for beat in result.beats:
                if beat.first_id != pos or beat.last_id < pos or beat.last_id > len(block):
                    discard_invalid(result)
                    raise ValueError('AI returned a gap or overlap; retry analysis')
                planned.append((block[pos-1][0], block[beat.last_id-1][1], included)); pos = beat.last_id+1
            if pos != len(block)+1:
                discard_invalid(result)
                raise ValueError('AI did not cover every passage; retry analysis')
        else: planned.append((block[0][0], block[-1][1], False))
    if state.analysis_plan:
        planned = state.analysis_plan
    planned = [(start, end, planned[index][2]) for index, start, end in prose_ranges(
        text, [(a, b) for a, b, _ in planned], heading_ranges)]
    state.analysis_plan = planned; state.save(session.root)
    for sequence, (start,end,included) in enumerate(planned,1):
        check()
        existing = next((s for s in state.scenes if s.start == start and s.end == end), None)
        if existing: continue
        description = (f'Writing scene {sequence}/{len(planned)} · '
            'Developing staging, composition, camera, lighting and the final image prompt')
        progress(description if included else f'Scene {sequence}/{len(planned)} · Preserving excluded prose without an image prompt')
        if included:
            part_id = next((s.identifier for s in sections if s.body_start <= start < s.end), None)
            scene_catalog = [c for c in state.catalogs
                             if not c.character_group_id or part_id in c.part_ids]
            content = {'prose': _scene_prose(analysis_text, start, end, heading_ranges),
                'earlier_context': _scene_prose(analysis_text, max(0,start-settings.overlap_chars), start, heading_ranges),
                'catalog': [c.model_dump() for c in scene_catalog], 'style': state.style,
                'world_prompt': next((state.part_world_prompts.get(s.identifier, '') for s in sections
                                      if s.body_start <= start < s.end), ''),
                'aspect_ratio': f'{state.image_settings.width}:{state.image_settings.height}'}
            for attempt in range(3):
                result = request(f'scene-{start}-{end}', ART_DIRECTION + '\n' + IDENTITY_LINKS,
                    content, Staging, description)
                links, unknown = resolve_identity_links(result.identity_links, scene_catalog)
                if not unknown:
                    result.identity_links = links
                    break
                discard_invalid(result)
                if attempt == 2:
                    raise ValueError(f'Scene {sequence}: AI returned unknown or ambiguous identity links '
                        f'after 3 attempts: {", ".join(repr(link) for link in unknown)}. '
                        'Completed scenes saved; Analyze Story resumes here.')
                progress(f'Scene {sequence}: correcting invalid identity links · Retry {attempt+1}/2')
                content['identity_link_correction'] = {
                    'invalid_links': unknown,
                    'instruction': 'Return the complete scene again using only catalog identifiers in identity_links. '
                        'Resolve identities against the catalog; do not invent IDs.'}
            data = result.model_dump()
            data['final_prompt'] = treatment_prompt(
                result.final_prompt, result.render_type, result.explanatory_focus)
        else: data = {'title': 'Excluded manuscript material'}
        scene = Scene(sequence=sequence, start=start, end=end, text=text[start:end],
            source_version=digest, production_included=included, **data)
        prompt = next((state.part_world_prompts.get(s.identifier, '') for s in sections
                       if s.body_start <= start < s.end), '')
        state.scene_world_prompts[scene.identifier] = prompt
        scene.location = scene_location(prompt, scene.location)
        scene.prompt_dependency = dependency(state, scene)
        state.scenes.append(scene); state.scenes.sort(key=lambda s:s.start)
        for i,s in enumerate(state.scenes,1): s.sequence=i
        state.save(session.root)
    progress(f'Validating {len(state.scenes)} scenes · Checking that scene passages cover the story without gaps or overlaps')
    validate_partition(text, state.scenes)
    progress(f'Analysis ready · {len(listed_identities(state))} recurring characters and places; {len(state.scenes)} scenes saved with staging and image prompts')
    return state


def regenerate_prompt(session, state, scene, provider):
    from prose_to_video.pipeline.manuscript import parse_manuscript
    headings = [(s.heading_start, s.heading_end) for s in parse_manuscript(scene.text)
                if s.heading_start is not None]
    prose = _scene_prose(scene.text, 0, len(scene.text), headings)
    scene_data = scene.model_dump()
    scene_data['text'] = prose
    result = Staging.model_validate(provider._request(ART_DIRECTION,
        json.dumps({'prose': prose, 'scene': scene_data, 'style': state.style,
            'world_prompt': scene_world_prompt(state, scene),
            'catalog': [c.model_dump() for c in state.catalogs if c.identifier in scene.identity_links],
            'aspect_ratio': f'{state.image_settings.width}:{state.image_settings.height}'}), Staging))
    scene.final_prompt = treatment_prompt(result.final_prompt, scene.render_type, scene.explanatory_focus)
    scene.negative_prompt = result.negative_prompt
    scene.prompt_dependency = dependency(state, scene); state.save(session.root)


def store_still(session, state, scene, data, *, prompt='', request=None, provider='import', job_id=None):
    from PySide6.QtGui import QImage
    image = QImage.fromData(data)
    if image.isNull() or image.width() <= 0: raise ValueError('Provider returned an unusable image')
    identifier = str(uuid4()); path = f'images/{identifier}.png'
    if not image.save(str(session.root / path), 'PNG'): raise ValueError('Could not save image')
    still = Still(identifier=identifier, scene_id=scene.identifier, source_hash=scene.source_version,
        start=scene.start, end=scene.end, excerpt=scene.text, path=path, prompt=prompt,
        request=request.model_dump() if request else {}, identities=scene.identity_links,
        references=[r.path for r in request.references] if request else [], provider=provider,
        job_id=job_id, dependency=source_hash(scene.final_prompt + dependency(state, scene)))
    state.stills.append(still); state.selected[scene.identifier] = still.identifier; state.save(session.root)
    return still


def generate_character_reference(session, state, character, provider, cancel=None, progress=lambda *_: None):
    """Generate and attach a canonical design without replacing existing references."""
    from PySide6.QtGui import QImage

    cancel = cancel or Event()
    if cancel.is_set():
        raise ValueError('Reference generation canceled')
    if character.kind != 'Character' or character not in state.catalogs:
        raise ValueError('Select a character in this project first')
    settings = state.image_settings
    prompt = (f'Canonical character reference for {character.name}.\n'
        f'Appearance: {character.description.strip() or "Choose a coherent, restrained character design."}\n'
        f'Project style: {state.style}.\n'
        + CHARACTER_REFERENCE_SUBJECTS
        + 'Show every subject in a neutral standing pose, full body from head to feet, '
        'with headroom and space around all silhouettes on a blank solid white background. '
        'Make each face, clothing, proportions and distinguishing features clear. '
        'No captions, labels, watermarks or panels.'
        + CHARACTER_REFERENCE_PEOPLE_ONLY)
    request = ImageRequest(prompt=prompt, model=settings.model, width=settings.width,
        height=settings.height, quality=settings.quality, candidates=1,
        seed=settings.seed, output_format=settings.output_format)
    provider.validate(request)
    job = {'id': str(uuid4()), 'kind': 'character_reference', 'identity_id': character.identifier,
        'status': 'Submitting', 'request': request.model_dump(), 'provider': provider.name}
    state.jobs.append(job)
    state.save(session.root)
    try:
        progress(f'Generating reference for {character.name} · Waiting for {provider.name}')
        result = provider.submit(request)
        job['provider_job_id'] = result.provider_job_id
        while result.status in {'Submitted', 'Running'}:
            job['status'] = result.status
            state.save(session.root)
            if cancel.wait(1):
                raise ValueError('Polling canceled; reference job retained for reconciliation')
            progress(f'Generating reference for {character.name} · {result.status}')
            result = provider.poll(result.provider_job_id or job['provider_job_id'])
        if result.status != 'Completed' or not result.images:
            raise ValueError(result.error or 'No completed reference image')
        image = QImage.fromData(result.images[0].data)
        if image.isNull():
            raise ValueError('Provider returned an unusable reference image')
        path = f'references/{uuid4()}.png'
        if not image.save(str(session.root / path), 'PNG'):
            raise ValueError('Could not save reference image')
        character.references.append(path)
        character.manual = True
        job.update(status='Completed', path=path)
        state.save(session.root)
        progress(f'Reference saved for {character.name}')
        return path
    except Exception as exc:
        job.update(status='Failed or uncertain', error=str(exc))
        state.save(session.root)
        raise


def generate_still(session, state, scene, provider, cancel=None, progress=lambda *_: None):
    if source_hash(read_prose(session.project.resolved_prose(session.root))) != state.prose_hash and state.prose_hash:
        raise ValueError('Source prose changed; existing images retained. Resolve anchors before generation.')
    if not scene.final_prompt.strip(): raise ValueError('Write or generate the scene prompt first')
    settings = state.image_settings
    refs = [p for c in state.catalogs if c.identifier in scene.identity_links for p in c.references]
    refs = list(dict.fromkeys(refs)) if settings.use_references else []
    caps = provider.capabilities(settings.model)
    if refs and (not caps.references or len(refs) > caps.max_references):
        raise ValueError(f'{provider.name} supports {caps.max_references} references; selected {len(refs)}. Change reference selection or disable references.')
    prompt, negative = scene.final_prompt, scene.negative_prompt
    world = scene_world_prompt(state, scene)
    if world and world not in prompt:
        prompt += '\nWorld: ' + world
    if negative and not caps.negative_prompt:
        prompt += '\nAvoid: ' + negative; negative = ''
        progress('Provider adaptation: negative prompt appended as Avoid instructions')
    request = ImageRequest(prompt=prompt, negative_prompt=negative, model=settings.model, width=settings.width,
        height=settings.height, quality=settings.quality, candidates=1, seed=settings.seed,
        output_format=settings.output_format, references=[ImageReference(asset_id=p, path=str(session.root/p), role='Continuity') for p in refs])
    # Apply only to the outgoing still request; scene prompts and reference
    # generation retain their existing behavior.
    from prose_to_video.storage.visual_style_preferences import load_visual_styles
    style_prompt = load_visual_styles().selected_prompt()
    if style_prompt:
        request = request.model_copy(update={'prompt': request.prompt + '\nStyle: ' + style_prompt})
    provider.validate(request)
    job = {'id': str(uuid4()), 'scene_id': scene.identifier, 'status': 'Submitting', 'request': request.model_dump(), 'provider': provider.name}
    state.jobs.append(job); state.save(session.root)
    try:
        progress(f'Scene {scene.sequence}: {scene.title} · Sending the image prompt to {provider.name} '
            f'({settings.width}×{settings.height}, {len(refs)} reference images); waiting for an image')
        result = provider.submit(request)
        job['provider_job_id'] = result.provider_job_id
        while result.status in {'Submitted', 'Running'}:
            job['status'] = result.status; state.save(session.root)
            progress(f'Scene {scene.sequence}: {scene.title} · {provider.name} image job is {result.status.lower()}; waiting for the result')
            if cancel and cancel.wait(1): raise ValueError('Polling canceled; job retained for reconciliation')
            result = provider.poll(result.provider_job_id or job['provider_job_id'])
        if result.status != 'Completed' or not result.images: raise ValueError(result.error or 'No completed image')
        # Select only after a replacement has been successfully saved.
        progress(f'Scene {scene.sequence}: {scene.title} · Checking and saving the generated image, then selecting it for the scene')
        asset = store_still(session, state, scene, result.images[0].data, prompt=prompt, request=request,
            provider=provider.name, job_id=result.provider_job_id or job['id'])
        job['status'] = 'Completed'; state.save(session.root); return asset
    except Exception as exc:
        job['status'] = 'Failed or uncertain'; job['error'] = str(exc); state.save(session.root); raise


def assemble_timeline(session, state, cancel=None, progress=lambda *_: None, *, timing_mode='strict'):
    """Align if needed and assemble all selected scene stills in one operation."""
    from prose_to_video.pipeline.audio_alignment import align_project
    from prose_to_video.pipeline.audio_transcription import AlignmentCanceled, TranscriptionSettings

    cancel = cancel or Event()
    def check():
        if cancel.is_set():
            raise AlignmentCanceled('Timeline assembly canceled; saved timeline preserved')

    check()
    audio = session.project.resolved_audio(session.root)
    prose = session.project.resolved_prose(session.root)
    if not audio or not audio.is_file():
        raise ValueError('Link narration audio in Start first')
    if not prose or not prose.is_file():
        raise ValueError('Import prose in Start first')
    if source_hash(read_prose(prose)) != state.prose_hash:
        raise ValueError('Prose changed; resolve scene anchors before building the timeline')
    scenes = [s for s in state.scenes if s.production_included]
    if not scenes:
        raise ValueError('Analyze the story and generate or import stills first')
    for scene in scenes:
        asset = state.current_still(scene.identifier)
        if not asset:
            raise ValueError(f'{scene.title}: generate or import a still')
        if not (session.root / asset.path).is_file():
            raise ValueError(f'{scene.title}: selected still is missing; import or generate a replacement')
    progress('Checking narration alignment')
    audio_hash = source_file_hash(audio)
    alignment = session.database.load_alignment()
    if not alignment or alignment.prose_hash != state.prose_hash or alignment.audio_hash != audio_hash:
        progress('Aligning narration automatically (first use may download local speech models)')
        alignment = align_project(prose, audio, state.scenes, session.database,
            TranscriptionSettings(allow_download=True), cancel=cancel,
            progress=lambda stage, n, total: progress(f'{stage} {n}/{total}'))
    else:
        progress('Reusing saved narration alignment')
    # Upgrade saved transcript matching without another speech recognition pass.
    from prose_to_video.pipeline.text_matching import MATCHING_VERSION
    from prose_to_video.pipeline.audio_alignment import rematch_alignment
    from prose_to_video.pipeline.manuscript import inclusion_fingerprint
    from prose_to_video.storage.manuscript import ManuscriptStore
    sections = ManuscriptStore(session.database).sections(read_prose(prose))
    if alignment.transcript and (alignment.matching_version != MATCHING_VERSION or
            alignment.inclusion_fingerprint != inclusion_fingerprint(sections)):
        alignment = rematch_alignment(alignment, read_prose(prose), state.scenes, sections)
        session.database.save_alignment(alignment)
    check()
    progress(f'Building timeline with {len(scenes)} stills')
    # Build against a copy so validation, cancellation, or save failures preserve edits.
    updated = state.model_copy(deep=True)
    build_timeline(updated, alignment, audio_hash, timing_mode=timing_mode)
    updated.timeline_mode = timing_mode
    check()
    if source_file_hash(audio) != audio_hash or source_hash(read_prose(prose)) != state.prose_hash:
        raise ValueError('Source prose or audio changed during assembly; saved timeline preserved')
    updated.save(session.root)
    state.timeline = updated.timeline
    state.timeline_audio_hash = updated.timeline_audio_hash
    state.timeline_mode = updated.timeline_mode
    return state.timeline


def build_timeline(state, alignment, audio_hash, *, timing_mode='strict'):
    if state.prose_hash != alignment.prose_hash or alignment.audio_hash != audio_hash:
        raise ValueError('Prose/audio changed. Align narration again.')
    if timing_mode not in {'nearest', 'strict'}:
        raise ValueError('Select nearest-fit or strict timing')
    if timing_mode == 'nearest':
        return build_nearest_timeline(state, alignment, audio_hash)
    extra = sum(i.kind == 'spoken_insertion' for i in alignment.issues)
    missing = sum(w.status == 'unmatched' and any(s.start <= w.source_start and w.source_end <= s.end
        for s in state.scenes if s.production_included) for w in alignment.words)
    if extra:
        raise ValueError(f'Narration needs review: {extra} unmatched transcript words and {missing} manuscript words/phrases '
            'without verified timing. Open Review Narration, listen and correct the transcript, then re-align. '
            'Extra speech may be recognition errors, insertions, or repetitions; saved timeline preserved.')
    scenes = [s for s in state.scenes if s.production_included]
    speech = []
    for scene in scenes:
        if not state.current_still(scene.identifier): raise ValueError(f'{scene.title}: generate or import a still')
        words = [w for w in alignment.words if scene.start <= w.source_start and w.source_end <= scene.end and w.status != 'excluded']
        if not words or any(w.audio_start is None for w in words):
            raise ValueError(f'{scene.title}: unmatched or omitted narration. Open Review Narration to correct the transcript and re-align before building the timeline.')
        speech.append((words[0].audio_start, words[-1].audio_end))
    if not speech: raise ValueError('No included scenes')
    previous = {t.scene_id:t for t in state.timeline if t.manual}
    result = []
    for i, (scene,(start,end)) in enumerate(zip(scenes,speech)):
        result.append(previous.get(scene.identifier) or Interval(scene_id=scene.identifier,
            start=0 if i==0 else start, end=speech[i+1][0] if i+1<len(scenes) else alignment.audio_duration,
            speech_start=start, speech_end=end))
    validate_timeline(result, alignment.audio_duration)
    state.timeline = result; state.timeline_audio_hash = audio_hash
    return result


def build_nearest_timeline(state, alignment, audio_hash):
    """Estimate display cuts, without changing transcript issues or verified word times."""
    scenes = [s for s in state.scenes if s.production_included]
    if not scenes:
        raise ValueError('No included scenes')
    for scene in scenes:
        if not state.current_still(scene.identifier):
            raise ValueError(f'{scene.title}: generate or import a still')
    duration = alignment.audio_duration
    # Word starts take precedence over the previous word's end at shared offsets.
    anchors = {scenes[0].start: 0.0, scenes[-1].end: duration}
    usable = [w for w in alignment.words if w.status == 'matched' and w.audio_start is not None and
        any(s.start <= w.source_start and w.source_end <= s.end for s in scenes)]
    for word in usable:
        anchors[word.source_start] = word.audio_start
        anchors[word.source_end] = word.audio_end
    positions = sorted(anchors)
    def estimate(position):
        i = bisect_left(positions, position)
        if i < len(positions) and positions[i] == position:
            return anchors[position]
        left, right = positions[max(0, i-1)], positions[min(i, len(positions)-1)]
        if left == right:
            return anchors[left]
        fraction = (position-left)/(right-left)
        return anchors[left] + fraction*(anchors[right]-anchors[left])
    # Entirely omitted adjacent scenes may land on the same word. Keep every still
    # editable and in order, even in that case, with positive contiguous intervals.
    minimum = min(.01, duration/(2*len(scenes)))
    cuts = [0.0]
    for i, scene in enumerate(scenes[1:], 1):
        words = [w for w in alignment.words if w.status != 'excluded' and
            scene.start <= w.source_start and w.source_end <= scene.end]
        desired = (words[0].audio_start if words and words[0].audio_start is not None
            else estimate(scene.start))
        cuts.append(max(cuts[-1]+minimum, min(desired, duration-(len(scenes)-i)*minimum)))
    cuts.append(duration)
    previous = {t.scene_id:t for t in state.timeline if t.manual}
    result = []
    for i, scene in enumerate(scenes):
        words = [w for w in alignment.words if w.status != 'excluded' and
            scene.start <= w.source_start and w.source_end <= scene.end]
        timed = [w for w in words if w.audio_start is not None]
        uncertain = bool(alignment.issues) or not words or len(timed) != len(words)
        result.append(previous.get(scene.identifier) or Interval(scene_id=scene.identifier,
            start=cuts[i], end=cuts[i+1],
            speech_start=timed[0].audio_start if timed else cuts[i],
            speech_end=timed[-1].audio_end if timed else cuts[i+1], estimated=uncertain))
    validate_timeline(result, duration)
    state.timeline = result
    state.timeline_audio_hash = audio_hash
    return result


def validate_timeline(timeline, duration):
    if duration is None or not math.isfinite(duration) or duration <= 0:
        raise ValueError('Narration duration is unavailable; relink the audio')
    pos = 0.0
    for index, t in enumerate(timeline, 1):
        if not all(math.isfinite(v) for v in (t.start, t.end)):
            raise ValueError(f'Still {index} has a non-finite timeline boundary. Correct its Start and End values; saved work was preserved.')
        if abs(t.start-pos)>1e-6:
            raise ValueError(f'Still {index} starts at {t.start:.3f}s, but the preceding still ends at {pos:.3f}s. Set both sides of the cut to the same time; saved work was preserved.')
        if t.end<=t.start:
            raise ValueError(f'Still {index} ends at {t.end:.3f}s, before its start at {t.start:.3f}s. Correct this interval; saved work was preserved.')
        if t.end>duration+1e-6:
            raise ValueError(f'Still {index} ends at {t.end:.3f}s, past the narration end at {duration:.3f}s. Correct this interval; saved work was preserved.')
        pos = t.end
    if abs(pos-duration)>1e-6:
        raise ValueError(f'Timeline ends at {pos:.3f}s, but narration ends at {duration:.3f}s. Rebuild the timeline or correct the last interval; saved work was preserved.')


def render_plan(session, state, *, approved_audio_hash=None):
    audio = session.project.resolved_audio(session.root)
    if not audio: raise ValueError('Link narration audio first')
    digest = source_file_hash(audio)
    if digest != state.timeline_audio_hash and digest != approved_audio_hash:
        raise ValueError('Audio changed; align and rebuild timeline, or choose Export anyway')
    if source_hash(read_prose(session.project.resolved_prose(session.root))) != state.prose_hash:
        raise ValueError('Prose changed; timeline is out of date')
    from prose_to_video.utils.media_info import inspect_audio
    duration = inspect_audio(audio).duration
    validate_timeline(state.timeline, duration)
    settings = state.render_settings; segments = []
    for t in state.timeline:
        asset = state.current_still(t.scene_id)
        if not asset: raise ValueError('Missing selected image')
        path = session.root/asset.path
        if not path.is_file(): raise ValueError(f'Missing image {asset.path}')
        a,b = round(t.start*settings.frame_rate), round(t.end*settings.frame_rate)
        if b<=a: raise ValueError('An image interval is shorter than one frame')
        scene = next(s for s in state.scenes if s.identifier == t.scene_id)
        camera = still_camera(scene.camera_movement, settings, scene.camera_focal_x, scene.camera_focal_y, scene.identifier)
        segments.append(PlannedSegment(scene_id=t.scene_id, shot_id=t.scene_id, asset_id=asset.identifier,
            source_path=str(path), source_fingerprint=source_file_hash(path), production_mode='Static Image' if camera.kind == 'Static' else 'Camera Motion',
            source_start=t.start, source_end=t.end, start_frame=a, end_frame=b, width=settings.width,
            height=settings.height, frame_rate=settings.frame_rate, fit_policy='crop' if camera.kind != 'Static' else settings.fit_policy,
            camera=camera, transition=settings.default_transition,
            transition_frames=round(settings.crossfade_duration*settings.frame_rate) if settings.default_transition!='Cut' else 0))
    return RenderPlan(project_id=session.project.identifier, scope='full', mode='final', source_start=0,
        source_end=duration, audio_path=str(audio), audio_fingerprint=digest, audio_duration=duration,
        settings=settings, segments=segments)
