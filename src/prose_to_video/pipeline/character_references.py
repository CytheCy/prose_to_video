"""Prepare reproducible character reference prompts and reviewable image candidates."""
from __future__ import annotations

import json
import re
import tempfile
from pathlib import Path
from uuid import uuid4

from prose_to_video.models.image_prompt import fingerprint
from prose_to_video.models.image_job import ImageRequest
from prose_to_video.models.visual_reference import AppearanceVariant, EntityReference


class ReferenceStore:
    def __init__(self, library):
        self.library = library

    def records(self, entity_id=None):
        with self.library.transaction() as con:
            return [json.loads(row[0]) for row in con.execute(
                'SELECT data_json FROM character_reference_preparation WHERE (? IS NULL OR entity_id=?) ORDER BY rowid',
                (entity_id, entity_id))]

    def save(self, record):
        with self.library.transaction() as con:
            con.execute('INSERT INTO character_reference_preparation VALUES(?,?,?) '
                        'ON CONFLICT(identifier) DO UPDATE SET data_json=excluded.data_json',
                        (record['identifier'], record['entity_id'], json.dumps(record)))


def _suggest_age_variants(library, entity, evidence):
    """Use explicit age claims only; never infer age from section ordering."""
    existing = library.variants(entity.identifier)
    if existing:
        return existing
    groups = {}
    for item in evidence:
        if not item.scene_id or item.kind != 'explicit' or item.status == 'Rejected':
            continue
        ages = {int(m.group(1)) for m in re.finditer(r'\b(\d{1,3})(?:[- ]year[- ]old| years? old)\b', item.quote, re.I)}
        if len(ages) == 1 and 0 < next(iter(ages)) < 120:
            age = next(iter(ages))
            groups.setdefault(age, []).append(item)
    if len(groups) < 2:
        return []
    return [library.add_variant(AppearanceVariant(entity_id=entity.identifier,
        name=f'Age {age}', description=f'{age} years old', age_min=age, age_max=age,
        source_evidence_ids=[e.identifier for e in items],
        scene_ids=list(dict.fromkeys(e.scene_id for e in items)), status='Suggested'))
        for age, items in sorted(groups.items())]


def prepare_character_references(library, entity_ids=None, *, current_attributes=False):
    """Local preparation is automatic and free; it never approves designs or images."""
    store = ReferenceStore(library)
    prepared = []
    manuscript_hash, _ = library.db.load_scenes()
    for entity in library.entities('Character'):
        if entity.name.casefold() in {'he', 'she', 'they', 'it', 'him', 'her', 'them'}: continue
        if entity.review_status == 'Rejected' or (entity_ids is not None and entity.identifier not in entity_ids):
            continue
        evidence = [e for e in library.evidence(entity.identifier) if e.status != 'Rejected'
                    and (not manuscript_hash or not e.source_hash or e.source_hash == manuscript_hash)]
        # User-edited canonical fields supersede their historical evidence values.
        evidence = [e for e in evidence if not (e.kind == 'user' and not e.scene_id
                    and hasattr(entity, e.field) and getattr(entity, e.field) != e.value)]
        variants = [] if current_attributes else _suggest_age_variants(library, entity, evidence)
        variants = [v for v in variants if v.status != 'Rejected']
        targets = variants or [None]
        for variant in targets:
            scope = set(variant.scene_ids) if variant else set()
            selected = [e for e in evidence if e.field not in {'alias', 'description'} and
                        (e.kind == 'explicit' or e.status == 'Approved') and
                        (not variant or not e.scene_id or e.scene_id in scope or e.identifier in variant.source_evidence_ids)]
            # Different scene descriptions stay separate when no variant covers them.
            # They remain suggestions, never promoted to canonical appearance.
            details = list(dict.fromkeys(f'{e.field}: {e.value}' for e in selected))
            if current_attributes:
                from prose_to_video.pipeline.entity_extraction import APPEARANCE_FIELDS
                selected = []
                details = [f'{field}: {getattr(entity, field)}' for field in sorted(APPEARANCE_FIELDS)
                           if getattr(entity, field)]
            warnings = []
            if not details: warnings.append('No grounded appearance details; review the proposed design before use.')
            if variant and variant.status != 'Approved': warnings.append('Appearance variant needs approval before use in storyboard shots.')
            if any(e.status != 'Approved' for e in selected): warnings.append('Includes source evidence awaiting review.')
            if variant:
                details += [f'Appearance variant: {variant.name}. {variant.description}',
                            f'Period: {variant.period}' if variant.period else '',
                            f'Clothing: {variant.clothing}' if variant.clothing else '']
                if variant.age_min is not None or variant.age_max is not None:
                    details.append(f'Age range: {variant.age_min if variant.age_min is not None else "unspecified"} to {variant.age_max if variant.age_max is not None else "unspecified"}')
            style = library.prompt_style_values(library.style())
            prompt = (f'Character identity reference for {entity.name}. One person only, full body standing, '
                      'centered and facing the camera, face clearly visible, neutral expression and relaxed pose, '
                      'plain solid white blank background, even readable lighting. '
                      'No scenery, environment, background objects, patterns, textures, gradients or horizon line. '
                      'Pull the camera back: show the entire figure from the top of the hair or headwear to the soles of both feet. '
                      'Keep at least 10 percent of the image height as empty headroom above the highest point of the head, '
                      'hair or headwear, and leave clear space below the feet and beside the hands. '
                      'The figure must fit entirely within the central 80 percent of the canvas height. '
                      'Never crop the head, hair, headwear, hands or feet; no close-up or tight portrait crop. '
                      'Preserve the supplied identity and the selected age and clothing; do not blend life stages. '
                      'No story action, extra people, captions or collage. Unknown appearance is an unapproved visual proposal. '
                      + ' '.join(d for d in details if d) + '\nVisual style: '
                      + ('; '.join(v for v in style.values() if v) or 'Neutral illustration')
                      + '\nApply the visual style to the character only; keep the background blank and solid white.')
            inputs = {'entity': entity.model_dump(mode='json'), 'variant': variant.model_dump(mode='json') if variant else None,
                      'evidence': [e.model_dump(mode='json') for e in selected], 'style': style, 'prompt': prompt,
                      'current_attributes': current_attributes}
            digest = fingerprint(inputs)
            old = next((r for r in store.records(entity.identifier) if r['fingerprint'] == digest), None)
            record = old or {'identifier': str(uuid4()), 'entity_id': entity.identifier,
                'variant_id': variant.identifier if variant else None, 'current_attributes': current_attributes,
                'label': f'{entity.name} — {variant.name if variant else "Default appearance"}',
                'prompt': prompt, 'fingerprint': digest, 'warnings': warnings,
                'evidence_ids': [e.identifier for e in selected], 'status': 'Prepared', 'asset_ids': []}
            if old is None: store.save(record)
            prepared.append(record)
    return prepared


def generate_selected_character_reference(library, entity_id, settings, provider):
    """Generate one current-appearance reference; block unresolved requests from resubmission."""
    records = prepare_character_references(library, {entity_id}, current_attributes=True)
    if not records:
        raise ValueError('Selected character is missing or rejected')
    record = records[0]
    store = ReferenceStore(library)
    matching = [r for r in store.records(entity_id) if r.get('current_attributes')]
    pending = next((r for r in matching if r['status'] in {'Submitted', 'Needs Reconciliation'}), None)
    if pending:
        raise ValueError('A reference request for this character is unresolved. Reconcile it in the Visual Library before generating again.')
    if record['status'] != 'Prepared':
        record = {**record, 'identifier': str(uuid4()), 'status': 'Prepared', 'asset_ids': []}
        for key in ('settings', 'estimated_cost', 'actual_cost', 'usage', 'provider_job_id', 'error'):
            record.pop(key, None)
        store.save(record)
    # Reference framing is independent of the storyboard's landscape output settings.
    settings = settings.model_copy(update={'width': 1024, 'height': 1536, 'candidates': 1})
    return generate_character_reference(library, record['identifier'], settings, provider)


def reference_spending(library):
    total = 0.0
    for record in ReferenceStore(library).records():
        if record['status'] == 'Prepared': continue
        amount = record.get('actual_cost')
        if amount is None: amount = record.get('estimated_cost')
        if amount is None: raise ValueError('Character reference spending is unknown; a finite cap cannot be evaluated')
        total += amount
    return total


def generate_character_reference(library, identifier, settings, provider, *, prompt=None):
    """Submit once; persist intent before calling the provider. Never automatically retry."""
    from prose_to_video.storage.generated_images import ImageStore
    store = ReferenceStore(library)
    record = next((r for r in store.records() if r['identifier'] == identifier), None)
    if not record or record['status'] != 'Prepared':
        raise ValueError('Reference is already submitted or generated; inspect its saved status before another request')
    current = prepare_character_references(library, {record['entity_id']},
                                           current_attributes=record.get('current_attributes', False))
    if not any(r['fingerprint'] == record['fingerprint'] for r in current):
        raise ValueError('Character details or style changed; prepare references again')
    if prompt is not None:
        if not prompt.strip(): raise ValueError('Reference prompt is empty')
        record['prompt'] = prompt.strip()
    request = ImageRequest(prompt=record['prompt'], model=settings.model, width=settings.width,
        height=settings.height, quality=settings.quality, candidates=1, output_format=settings.output_format,
        seed=settings.seed)
    provider.validate(request)
    estimate = settings.estimated_cost_per_image
    if settings.spending_cap is not None:
        total = reference_spending(library)
        for job in ImageStore(library.root).jobs():
            if job.status in {'Canceled', 'Failed'}: continue
            amount = job.actual_cost if job.actual_cost is not None else job.estimated_cost
            if amount is None: raise ValueError('Image spending is unknown; a finite cap cannot be evaluated')
            total += amount
        if estimate is None or total + estimate > settings.spending_cap:
            raise ValueError('Image spending cap reached or reference cost estimate is unknown')
    record.update(status='Submitted', settings=settings.model_dump(mode='json'), estimated_cost=estimate)
    # Atomically claim this preparation, including across application processes.
    with library.transaction() as con:
        con.execute('BEGIN IMMEDIATE')
        current = json.loads(con.execute('SELECT data_json FROM character_reference_preparation WHERE identifier=?',
                                        (identifier,)).fetchone()[0])
        if current['status'] != 'Prepared': raise ValueError('Reference already submitted')
        con.execute('UPDATE character_reference_preparation SET data_json=? WHERE identifier=?', (json.dumps(record), identifier))
    try:
        result = provider.submit(request)
        record.update(provider_job_id=result.provider_job_id, usage=result.usage, actual_cost=result.actual_cost)
        store.save(record)
        return _finish_reference(library, record, result)
    except Exception as exc:
        record.update(status='Needs Reconciliation', error=str(exc)); store.save(record)
        raise


def _finish_reference(library, record, result):
    store = ReferenceStore(library)
    record.update(provider_job_id=result.provider_job_id or record.get('provider_job_id'),
                  usage=result.usage, actual_cost=result.actual_cost)
    if result.status != 'Completed':
        record.update(status='Needs Reconciliation', error=result.error)
        store.save(record)
        return record
    if not result.images: raise ValueError('Provider returned no reference image')
    for image in result.images:
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / ('reference.' + record['settings']['output_format'])
            path.write_bytes(image.data)
            asset = library.import_image(path)
        if not any(r.asset_id == asset.identifier for r in library.references(record['entity_id'])):
            library.attach_image(EntityReference(entity_id=record['entity_id'], asset_id=asset.identifier,
                variant_id=record['variant_id'], role='Full-body view', status='Suggested',
                description=record['label']))
        if asset.identifier not in record['asset_ids']: record['asset_ids'].append(asset.identifier)
    record['status'] = 'Completed'; store.save(record)
    return record


def reconcile_character_reference(library, identifier, provider):
    record = next(r for r in ReferenceStore(library).records() if r['identifier'] == identifier)
    if record['status'] not in {'Submitted', 'Needs Reconciliation'} or not record.get('provider_job_id'):
        raise ValueError('No provider job ID is available. Inspect the provider account; this request will not be resubmitted.')
    return _finish_reference(library, record, provider.poll(record['provider_job_id']))
