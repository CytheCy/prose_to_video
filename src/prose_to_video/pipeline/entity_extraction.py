"""Incremental, source-grounded visual entity extraction and reconciliation."""
from __future__ import annotations

import json
import re
from threading import Event
from typing import Protocol
from pydantic import BaseModel, ConfigDict, Field
from prose_to_video.models.scene import Scene, source_hash
from prose_to_video.models.visual_reference import ENTITY_TYPES, SourceEvidence, VisualEntity
from prose_to_video.storage.visual_library import VisualLibrary

class EntityProposal(BaseModel):
    model_config = ConfigDict(extra='forbid')
    name: str
    entity_type: str
    aliases: list[str] = Field(default_factory=list)
    description: str = ''
    appearances: list['AppearanceProposal'] = Field(default_factory=list)

class AppearanceProposal(BaseModel):
    model_config = ConfigDict(extra='forbid')
    field: str
    value: str
    exact_quote: str = ''
    kind: str = 'explicit'

class EntityProposals(BaseModel):
    model_config = ConfigDict(extra='forbid')
    entities: list[EntityProposal] = Field(default_factory=list)

class EntityProvider(Protocol):
    def extract_entities(self, payload: str) -> EntityProposals: ...

class ExtractionCanceled(Exception): pass

APPEARANCE_FIELDS = {'appearance_description', 'distinguishing_features', 'colors_materials',
                     'size_proportions', 'clothing_accessories'}

def _chunks(text: str, limit: int):
    start = 0
    while start < len(text):
        end = min(len(text), start + limit)
        if end < len(text):
            boundary = text.rfind('\n\n', start + limit // 2, end)
            if boundary > start: end = boundary + 2
        yield start, text[start:end]
        start = end

def reconcile_shot_links(scene: Scene, library: VisualLibrary):
    """Link only unambiguous, explicit name mentions; leave pronouns for review."""
    board = library.db.load_storyboard(scene.identifier)
    if not board: return
    entities = [e for e in library.entities() if scene.identifier in library.scenes_for(e.identifier)]
    for shot in board.shots:
        matches = []
        for entity in entities:
            names = [entity.name, *entity.aliases]
            if any(re.search(r'(?<!\w)' + re.escape(name) + r'(?!\w)', shot.original_prose, re.I) for name in names):
                matches.append(entity)
        if len(matches) == 1:
            library.relate_shot(matches[0].identifier, shot.identifier, automatic=True)
        elif len(matches) > 1:
            # Multiple explicit names can be valid; skip only duplicate identity names.
            for entity in matches:
                own = {n.casefold() for n in [entity.name, *entity.aliases]}
                if all(not own.intersection({n.casefold() for n in [other.name, *other.aliases]})
                       for other in matches if other.identifier != entity.identifier):
                    library.relate_shot(entity.identifier, shot.identifier, automatic=True)

def extract_entities(scenes: list[Scene], library: VisualLibrary, provider: EntityProvider,
                     *, selected_ids: set[str] | None = None, cancel: Event | None = None,
                     progress=None, manuscript_hash: str | None = None, chunk_chars: int = 12000,
                     source_check=None):
    """One provider request per selected scene. Save each scene only after validation.

    Existing approved fields and images are never overwritten. Ambiguous identities stay suggestions.
    """
    chosen = [s for s in scenes if selected_ids is None or s.identifier in selected_ids]
    created = []
    if chunk_chars < 1000: raise ValueError('Entity extraction chunk size must be at least 1000 characters')
    for index, scene in enumerate(chosen, 1):
        if cancel and cancel.is_set(): raise ExtractionCanceled('Entity extraction canceled')
        if progress: progress(index - 1, len(chosen))
        for chunk_start, chunk_text in _chunks(scene.text, chunk_chars):
            if cancel and cancel.is_set(): raise ExtractionCanceled('Entity extraction canceled')
            known = sorted(library.entities(), key=lambda entity: entity.modified_at, reverse=True)[:80]
            payload = json.dumps({'scene_id': scene.identifier, 'source_start': scene.start + chunk_start,
                                  'source_text': chunk_text, 'scene_characters': scene.characters,
                                  'scene_location': scene.location, 'important_objects': scene.important_objects,
                                  'known_entities': [{'id': e.identifier, 'name': e.name,
                                                      'type': e.entity_type, 'aliases': e.aliases} for e in known]},
                                 ensure_ascii=False)
            proposals = provider.extract_entities(payload)
            if not isinstance(proposals, EntityProposals):
                proposals = EntityProposals.model_validate(proposals)
            if cancel and cancel.is_set(): raise ExtractionCanceled('Entity extraction canceled')
            if source_check: source_check()
            _save_chunk(proposals, scene, chunk_start, chunk_text, library, manuscript_hash, created)
        reconcile_shot_links(scene, library)
        if progress: progress(index, len(chosen))
    return created

def _save_chunk(proposals, scene, chunk_start, chunk_text, library, manuscript_hash, created):
    new_ids = []
    digest = manuscript_hash or source_hash(scene.text)
    with library.transaction() as con:
        entities = [VisualEntity.model_validate_json(row[0]) for row in con.execute('SELECT data_json FROM visual_entity')]
        aliases = {}
        for row in con.execute('SELECT entity_id, alias FROM entity_alias'):
            aliases.setdefault(row['entity_id'], set()).add(row['alias'].casefold())
        for proposal in proposals.entities:
            if not proposal.name.strip(): continue
            category = proposal.entity_type if proposal.entity_type in ENTITY_TYPES else 'Other'
            names = {proposal.name.casefold(), *(a.casefold() for a in proposal.aliases)}
            candidates = [e for e in entities if e.entity_type == category and
                          (e.name.casefold() in names or aliases.get(e.identifier, set()) & names)]
            if len(candidates) > 1: continue
            entity = candidates[0] if candidates else None
            if entity is None:
                entity = VisualEntity(name=proposal.name, entity_type=category,
                                      description=proposal.description, source_hash=digest)
                con.execute('INSERT INTO visual_entity VALUES(?,?,?,?,?,?)',
                            (entity.identifier, entity.name, entity.entity_type,
                             entity.review_status, int(entity.recurring), entity.model_dump_json()))
                library._audit(con, 'entity_created', entity_id=entity.identifier, extraction=True)
                entities.append(entity)
                new_ids.append(entity.identifier)
            con.execute('INSERT OR IGNORE INTO entity_scene VALUES(?,?)', (entity.identifier, scene.identifier))
            existing = {(e.scene_id, e.source_hash, e.field, e.value, e.kind) for e in
                        [SourceEvidence.model_validate_json(row[0]) for row in con.execute(
                            'SELECT data_json FROM entity_evidence WHERE entity_id=?', (entity.identifier,))]}
            for alias in proposal.aliases:
                if not alias.strip() or alias.casefold() == entity.name.casefold(): continue
                key = (scene.identifier, digest, 'alias', alias.strip(), 'ai_proposed')
                if key not in existing:
                    evidence = SourceEvidence(scene_id=scene.identifier, source_hash=digest,
                        field='alias', value=alias.strip(), kind='ai_proposed')
                    con.execute('INSERT INTO entity_evidence VALUES(?,?,?,?)',
                                (evidence.identifier, entity.identifier, scene.identifier, evidence.model_dump_json()))
                    existing.add(key)
            for detail in proposal.appearances:
                if detail.field not in APPEARANCE_FIELDS or not detail.value.strip(): continue
                pos = chunk_text.find(detail.exact_quote) if detail.exact_quote else -1
                kind = detail.kind if detail.kind in {'explicit', 'inferred', 'ai_proposed'} else 'ai_proposed'
                if kind == 'explicit' and pos < 0: kind = 'ai_proposed'
                key = (scene.identifier, digest, detail.field, detail.value, kind)
                if key in existing: continue
                evidence = SourceEvidence(scene_id=scene.identifier, source_hash=digest,
                    source_start=scene.start + chunk_start + pos if pos >= 0 else None,
                    source_end=scene.start + chunk_start + pos + len(detail.exact_quote) if pos >= 0 else None,
                    quote=detail.exact_quote if pos >= 0 else '', field=detail.field,
                    value=detail.value, kind=kind)
                con.execute('INSERT INTO entity_evidence VALUES(?,?,?,?)',
                            (evidence.identifier, entity.identifier, scene.identifier, evidence.model_dump_json()))
                existing.add(key)
    created.extend(new_ids)
