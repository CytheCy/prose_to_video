"""Grounded prompt generation from one shot and its approved visual references."""
from __future__ import annotations

import json
import hashlib
from threading import Event
from pydantic import BaseModel, ConfigDict, Field

from prose_to_video.models.image_prompt import (ImagePrompt, PromptComponents, PromptReference,
    PromptSettings, assemble_prompt, aspect_ratio, fingerprint)


class PromptDraft(BaseModel):
    model_config = ConfigDict(extra='forbid')
    components: PromptComponents
    negative_prompt: str = ''
    warnings: list[str] = Field(default_factory=list)


class PromptCanceled(Exception):
    pass


def context_for_shot(shot, scene, board, library, project, settings, *, legacy=False):
    if shot.scene_id != scene.identifier or shot.original_prose != scene.text[shot.source_start-scene.start:shot.source_end-scene.start]:
        raise ValueError('Shot source passage is stale')
    visual = library.package_for_shot(shot.identifier)
    style = visual['style']
    entities = visual['entities']
    references = []
    variants = {}
    missing = []
    for entity in entities:
        variant = entity['variant']
        if variant: variants[entity['id']] = variant['identifier']
        elif any(v.identifier == next((r['variant_id'] for r in library.shots_for(entity['id']) if r['shot_id'] == shot.identifier), None)
                 for v in library.variants(entity['id'])):
            missing.append(f"Unapproved appearance variant for {entity['name']}")
        for ref in entity['references']:
            asset = ref['asset']
            references.append(PromptReference(asset_id=asset['identifier'], path=asset['path'],
                role=ref['role'], entity_id=entity['id'], variant_id=variant['identifier'] if variant else None,
                primary=ref['primary'], width=asset['width'], height=asset['height'],
                sha256=asset['sha256'], description=ref['description']))
        if not entity['references']:
            missing.append(f"No approved image reference for {entity['name']}; using approved written details")
    for asset in visual['shot_references']:
        references.append(PromptReference(asset_id=asset['identifier'], path=asset['path'],
            role='Pinned shot reference', width=asset['width'], height=asset['height'],
            sha256=asset['sha256']))
    checked = set()
    for ref in references:
        if ref.asset_id in checked: continue
        checked.add(ref.asset_id)
        path = library.root / ref.path
        if not path.is_file():
            missing.append(f'Missing reference image: {ref.path}')
        elif ref.sha256:
            digest = hashlib.sha256()
            with path.open('rb') as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b''):
                    digest.update(chunk)
            if digest.hexdigest() != ref.sha256:
                missing.append(f'Reference image changed: {ref.path}')
    index = next((i for i, s in enumerate(board.shots) if s.identifier == shot.identifier), -1)
    if index < 0: raise ValueError('Shot is absent from its storyboard')
    previous = board.shots[index-1] if settings.include_adjacent_shots and index else None
    following = board.shots[index+1] if settings.include_adjacent_shots and index+1 < len(board.shots) else None
    ratio = aspect_ratio(project.settings.width, project.settings.height)
    image_shot = shot.model_dump(mode='json') if legacy else shot.model_dump(mode='json', exclude={
        'camera_movement', 'suggested_motion', 'transition', 'audio_start', 'audio_end',
        'timing_status', 'timing_note', 'review_status', 'manually_edited'})
    payload = {'shot': image_shot, 'source_prose': shot.original_prose,
        'surrounding_prose': scene.text[max(0, shot.source_start-scene.start-400):shot.source_end-scene.start],
        'scene': {'title': scene.title, 'location': scene.location,
                  'environment': scene.environment, 'atmosphere': scene.visual_atmosphere},
        'previous_shot': {'characters': previous.characters,
                          'character_appearances': previous.character_appearances,
                          'objects': previous.objects, 'environment': previous.environment,
                          'foreground': previous.foreground, 'background': previous.background,
                          'character_actions': previous.character_actions,
                          'visual_description': previous.visual_description} if previous else None,
        # Following shot is a continuity constraint only. Never depict its later action.
        'following_shot': {'environment': following.environment} if following else None,
        'entities': [{k: v for k, v in e.items() if k != 'references'} for e in entities],
        'style': style, 'aspect_ratio': ratio, 'settings': settings.model_dump()}
    deps = {'shot': image_shot, 'scene': payload['scene'],
            'previous_shot': payload['previous_shot'], 'following_shot': payload['following_shot'],
            'entities': visual['entities'], 'shot_references': visual['shot_references'],
            'style': style, 'ratio': ratio, 'settings': settings.model_dump()}
    return payload, references, variants, missing, fingerprint(deps)


def generate_prompt(shot, scene, board, library, project, settings: PromptSettings, provider,
                    *, version=1, cancel: Event | None = None, source_check=None):
    if cancel and cancel.is_set(): raise PromptCanceled('Prompt generation canceled')
    payload, references, variants, warnings, digest = context_for_shot(shot, scene, board, library, project, settings)
    response = provider.generate_image_prompt(json.dumps(payload, ensure_ascii=False))
    draft = response if isinstance(response, PromptDraft) else PromptDraft.model_validate(response)
    if cancel and cancel.is_set(): raise PromptCanceled('Prompt generation canceled')
    if source_check: source_check()
    components = draft.components
    if not components.shot_description.strip(): raise ValueError('AI returned no shot description')
    for name in ('foreground', 'background', 'environment', 'lighting'):
        if not getattr(components, name).strip() and getattr(shot, name).strip():
            setattr(components, name, getattr(shot, name))
    if shot.character_actions and not components.character_positions_actions:
        components.character_positions_actions = shot.character_actions
    if shot.objects and not components.objects:
        components.objects = ', '.join(shot.objects)
    # Canonical approved style and camera facts are authoritative over suggestions.
    components.camera_framing = shot.camera_framing or components.camera_framing or shot.shot_type
    components.camera_angle = shot.camera_angle or components.camera_angle
    components.camera_position = shot.camera_position or components.camera_position
    components.composition = f"Compose for a {payload['aspect_ratio']} frame. {components.composition}".strip()
    components.visual_style = '; '.join(str(v) for v in payload['style'].values() if v)
    if not components.visual_style: components.visual_style = board.settings.visual_style
    for entity in payload['entities']:
        details = list(entity.get('approved_design', {}).values())
        details += [f"{e['field']}: {e['value']}" for e in entity.get('approved_details', [])]
        variant = entity.get('variant')
        if variant and variant.get('description'): details.append(variant['description'])
        if not details: continue
        board_appearance = shot.character_appearances.get(entity['name'], '')
        approved_appearance = entity.get('appearance', '')
        if board_appearance and approved_appearance and approved_appearance.casefold() not in board_appearance.casefold():
            warnings.append(f"Storyboard appearance for {entity['name']} may conflict with the approved Visual Library design")
        target = 'environment' if entity['type'] in {'Location', 'Architectural structure'} else 'objects' if entity['type'] in {'Prop or object', 'Vehicle'} else 'character_appearance'
        existing = getattr(components, target)
        approved = f"{entity['name']}: " + '; '.join(dict.fromkeys(details))
        if any(value and value.casefold() not in existing.casefold() for value in details):
            warnings.append(f"Review generated {entity['name']} description against approved appearance")
        setattr(components, target, f'{existing} {approved}'.strip())
    positive = assemble_prompt(components)
    warnings += draft.warnings
    return ImagePrompt(shot_id=shot.identifier, scene_id=scene.identifier, version=version,
        positive_prompt=positive, negative_prompt=draft.negative_prompt if settings.generate_negative else '',
        components=components, entity_ids=[e['id'] for e in payload['entities']], references=references,
        variant_ids=variants, source_start=shot.source_start, source_end=shot.source_end,
        aspect_ratio=payload['aspect_ratio'], dependency_fingerprint=digest,
        review_status='Needs Review' if warnings else 'Generated', warnings=warnings,
        model_name=settings.model or getattr(getattr(provider, 'settings', None), 'model', ''),
        provider=settings.provider, settings=settings)


def generate_batch(shots, scene_by_id, board_by_scene, library, project, settings, provider,
                   store, *, regenerate=False, cancel=None, progress=None, source_check=None):
    results = []
    for index, shot in enumerate(shots):
        if cancel and cancel.is_set(): raise PromptCanceled('Prompt generation canceled')
        if progress: progress(index, len(shots))
        existing = store.latest(shot.identifier)
        if existing and not regenerate:
            continue
        prompt = generate_prompt(shot, scene_by_id[shot.scene_id], board_by_scene[shot.scene_id],
            library, project, settings, provider, version=existing.version+1 if existing else 1,
            cancel=cancel, source_check=source_check)
        current_board = store.db.load_storyboard(shot.scene_id)
        current_shot = next((s for s in current_board.shots if s.identifier == shot.identifier), None) if current_board else None
        if current_shot is None or current_shot.model_dump() != shot.model_dump():
            raise ValueError('Storyboard shot changed during prompt generation')
        if store.db.load().settings != project.settings:
            raise ValueError('Project output dimensions changed during prompt generation')
        if store.settings() != settings:
            raise ValueError('Prompt settings changed during prompt generation')
        current_digest = context_for_shot(shot, scene_by_id[shot.scene_id], current_board,
                                          library, project, settings)[4]
        if current_digest != prompt.dependency_fingerprint:
            raise ValueError('Visual references or style changed during prompt generation')
        store.save(prompt, activate=not bool(store.approved(shot.identifier)))
        results.append(prompt)
        if settings.pause_for_approval: break
    if progress: progress(len(shots), len(shots))
    return results
