"""Prose-first AI place catalog, followed by source-range scene associations."""
from __future__ import annotations

import json
from threading import Event
from pydantic import BaseModel, ConfigDict, Field

from prose_to_video.models.scene import source_hash
from prose_to_video.models.visual_reference import SourceEvidence, VisualEntity
from prose_to_video.pipeline.scene_detection import make_chunks


class PlaceProposal(BaseModel):
    model_config = ConfigDict(extra='forbid')
    name: str = Field(min_length=1)
    description: str = Field(min_length=1)
    passage_ids: list[int]


class PlaceProposals(BaseModel):
    model_config = ConfigDict(extra='forbid')
    places: list[PlaceProposal]


class PlaceDescription(BaseModel):
    model_config = ConfigDict(extra='forbid')
    description: str = Field(min_length=1)


class PlacesCanceled(Exception):
    pass


def _key(name):
    return ' '.join(name.split()).casefold()


def listed_places(library):
    return sorted((p for p in library.entities('Location') if p.place_order is not None),
                  key=lambda p: p.place_order)


def detect_places(text, library, provider, settings, *, cancel=None, progress=None, source_check=None):
    """Read original prose in order. Finish the catalog before linking any scenes.

    All AI work is staged in memory; cancellation/failure preserves the saved catalog.
    Each place has a separate description request, allowing the full output budget.
    """
    if not text.strip():
        raise ValueError('Import nonempty prose before detecting places')
    settings.validate()
    cancel = cancel or Event()
    progress = progress or (lambda message: None)
    def check():
        if cancel.is_set():
            raise PlacesCanceled('Place detection canceled; saved places were preserved')
        if source_check:
            source_check()
    catalog = {}
    chunks = make_chunks(text, settings.chunk_chars, settings.overlap_chars)
    existing = library.entities('Location')
    for number, chunk in enumerate(chunks, 1):
        check()
        progress(f'Reading original prose for places: {number}/{len(chunks)}')
        units = {u.identifier: u for u in chunk.units}
        payload = {'passages': [{'id': u.identifier, 'prose': text[u.start:u.end]} for u in chunk.units],
                   'known_places': [r['name'] for r in catalog.values()],
                   'saved_places': [{'name': p.name, 'aliases': p.aliases} for p in existing]}
        result = provider.detect_places(json.dumps(payload, ensure_ascii=False))
        result = PlaceProposals.model_validate(result)
        for proposal in result.places:
            if not proposal.name.strip() or not proposal.description.strip() or not proposal.passage_ids:
                raise ValueError('AI returned a place without a name, description, or source passages')
            if any(i not in units for i in proposal.passage_ids):
                raise ValueError('AI returned an unknown place passage ID')
            ranges = [(units[i].start, units[i].end) for i in proposal.passage_ids if i in chunk.core_ids]
            if not ranges:
                continue
            name = ' '.join(proposal.name.split())
            identities = [p for p in existing if _key(name) in {_key(n) for n in [p.name, *p.aliases]}]
            if len(identities) > 1:
                raise ValueError(f'Ambiguous place identity: {name}. Merge duplicates in Visual Library.')
            if identities:
                name = identities[0].name
            key = _key(name)
            record = catalog.setdefault(key, {'name': name, 'details': [], 'ranges': []})
            record['details'].append(proposal.description)
            record['ranges'].extend(ranges)
    records = sorted(catalog.values(), key=lambda r: min(start for start, end in r['ranges']))
    for number, record in enumerate(records, 1):
        check()
        progress(f'Describing places: {number}/{len(records)} — {record["name"]}')
        # Progressive synthesis keeps long recurring-place descriptions within input limits.
        description = ''
        for detail in dict.fromkeys(record['details']):
            check()
            response = provider.describe_place(json.dumps({'name': record['name'],
                'accumulated_description': description, 'source_details': detail}, ensure_ascii=False))
            description = PlaceDescription.model_validate(response).description
            if not description.strip():
                raise ValueError('AI returned an empty place description')
        record['description'] = description
    check()
    digest = source_hash(text)
    with library.transaction() as con:
        # Retire the old catalog without deleting reusable entities or user edits.
        for old in existing:
            if old.place_order is not None:
                old.place_order = None
                con.execute('UPDATE visual_entity SET data_json=? WHERE identifier=?',
                            (old.model_dump_json(), old.identifier))
        for number, record in enumerate(records, 1):
            matches = [p for p in existing if _key(record['name']) in {_key(n) for n in [p.name, *p.aliases]}]
            if len(matches) > 1:
                raise ValueError(f'Ambiguous place identity: {record["name"]}. Merge duplicates in Visual Library.')
            place = matches[0].model_copy(deep=True) if matches else VisualEntity(name=record['name'], entity_type='Location')
            evidence = [SourceEvidence.model_validate_json(r[0]) for r in con.execute(
                'SELECT data_json FROM entity_evidence WHERE entity_id=?', (place.identifier,))]
            if not place.place_description_edited and not any(e.field == 'description' and e.kind == 'user' for e in evidence):
                place.description = record['description']
            place.place_order = number
            place.place_ranges = sorted(set(record['ranges']))
            place.source_hash = digest
            con.execute('INSERT INTO visual_entity VALUES(?,?,?,?,?,?) ON CONFLICT(identifier) DO UPDATE SET '
                        'name=excluded.name, entity_type=excluded.entity_type, status=excluded.status, '
                        'recurring=excluded.recurring, data_json=excluded.data_json',
                (place.identifier, place.name, place.entity_type, place.review_status,
                 int(place.recurring), place.model_dump_json()))
            library._audit(con, 'place_detected', entity_id=place.identifier)
    progress('Places listed and described; linking places to scenes…')
    link_places(library)
    return listed_places(library)


def link_places(library):
    """Link a finished catalog to current scenes by AI-identified source passages."""
    digest, scenes = library.db.load_scenes()
    places = listed_places(library)
    desired = {(p.identifier, s.identifier) for p in places if p.source_hash == digest
               for s in scenes if s.production_included
               and any(start < s.end and end > s.start for start, end in p.place_ranges)}
    with library.transaction() as con:
        tracked = {}
        for row in con.execute('SELECT identifier, entity_id, data_json FROM entity_evidence'):
            evidence = SourceEvidence.model_validate_json(row['data_json'])
            if evidence.field == 'detected_place':
                tracked[(row['entity_id'], evidence.scene_id)] = row['identifier']
        for pair, evidence_id in tracked.items():
            if pair not in desired:
                con.execute('DELETE FROM entity_scene WHERE entity_id=? AND scene_id=?', pair)
                con.execute('DELETE FROM entity_evidence WHERE identifier=?', (evidence_id,))
        for pair in desired:
            con.execute('INSERT OR IGNORE INTO entity_scene VALUES(?,?)', pair)
            if pair not in tracked:
                evidence = SourceEvidence(scene_id=pair[1], source_hash=digest,
                    field='detected_place', value='Prose setting', kind='inferred')
                con.execute('INSERT INTO entity_evidence VALUES(?,?,?,?)',
                    (evidence.identifier, pair[0], pair[1], evidence.model_dump_json()))
