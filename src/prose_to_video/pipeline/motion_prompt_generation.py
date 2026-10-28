"""Source-grounded motion instructions; image bytes never reach the language model."""
from __future__ import annotations
import hashlib
from pathlib import Path
from pydantic import BaseModel
from prose_to_video.models.generated_video import MotionPrompt
from prose_to_video.models.image_prompt import fingerprint
from prose_to_video.storage.generated_images import ImageStore
from prose_to_video.storage.generated_videos import VideoStore
from prose_to_video.storage.visual_library import VisualLibrary

class MotionResponse(BaseModel):
    positive_prompt: str
    negative_prompt: str = ''
    subject_movements: str = ''
    object_movements: str = ''
    environmental_movements: str = ''
    camera_movement: str = 'Stationary'
    continuity_constraints: str = ''

def workflow_fingerprint(root, settings):
    if not settings.workflow_path:return ''
    path=Path(settings.workflow_path)
    if not path.is_absolute():path=Path(root)/path
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else 'Missing'

def source_fingerprint(shot, image, ending_image, settings, reference_ids=(),workflow_hash=''):
    relevant=('provider','model','workflow_path','workflow_mapping','width','height','duration',
        'frame_rate','seed','motion_intensity','camera_preference','use_last_frame',
        'use_references','output_format')
    settings_data=settings.model_dump(mode='json')
    return fingerprint({'shot_id':shot.identifier,'source':shot.original_prose,
        'description':shot.visual_description,'character_actions':shot.character_actions,
        'objects':shot.objects,'camera':shot.camera_movement,'image':image.sha256,
        'ending_image':ending_image.sha256 if ending_image else None,
        'settings':{key:settings_data[key] for key in relevant},'workflow_hash':workflow_hash,
        'references':list(reference_ids)})

def resolve_ending_image(root, identifier):
    if not identifier:return None
    return ImageStore(root).asset(identifier) or VisualLibrary(root).asset(identifier)

def motion_references(root, shot_id, enabled):
    if not enabled:return []
    package=VisualLibrary(root).package_for_shot(shot_id)
    assets={}
    for entity in package['entities']:
        for ref in entity['references']:
            asset=ref['asset'];assets[asset['identifier']]=asset
    for asset in package['shot_references']:assets[asset['identifier']]=asset
    return [assets[key] for key in sorted(assets)]

def generate_motion_prompt(session, scene, shot, *, provider, ending_image_id=None, adjacent=(), cancel=None):
    store=VideoStore(session.root); images=ImageStore(session.root)
    selected=images.approved(shot.identifier)
    if not selected:raise ValueError('Approve a starting image first')
    image=selected[0]
    ending=resolve_ending_image(session.root,ending_image_id)
    if ending_image_id and not ending:raise ValueError('Ending image is missing')
    if ending and abs(image.width/image.height-ending.width/ending.height)>.02:
        raise ValueError('Ending image aspect ratio differs from starting image')
    settings=store.settings()
    if ending_image_id and not settings.use_last_frame:
        raise ValueError('Enable last-frame conditioning in Video Settings first')
    refs=motion_references(session.root,shot.identifier,settings.use_references)
    board=session.database.load_storyboard(scene.identifier)
    earlier=[s.visual_description for s in board.shots if s.sequence==shot.sequence-1] if board else []
    previous=[getattr(s,'visual_description',str(s)) for s in list(adjacent)[:1]] or earlier
    package=VisualLibrary(session.root).package_for_shot(shot.identifier)
    continuity=[{'name':entity['name'],'type':entity['type'],
        'design':entity['approved_design'],'variant':entity['variant'],
        'constraints':entity['constraints']} for entity in package['entities']]
    context={'prose':shot.original_prose,'shot_description':shot.visual_description,
        'character_actions':shot.character_actions,'objects':shot.objects,
        'camera':shot.camera_movement if settings.camera_preference=='Follow storyboard' else 'Stationary',
        'duration_seconds':shot.duration or settings.duration,
        'starting_image_id':image.identifier,'starting_image_dimensions':[image.width,image.height],
        'previous_shot_context':previous,'continuity':continuity,
        'visual_style':package['style'],'visual_references':shot.entity_refs}
    instructions=('Describe only physical movement that occurs in the assigned prose passage. '
        'Preserve the approved starting composition, identities, environment, and event order. '
        'Do not invent significant actions, new characters, or future events. '
        'A still subject should stay still. Prefer one short achievable action. '
        'Return specific subject, object, environment, and camera instructions. '
        'No image bytes are supplied.')
    import json
    result=provider._request(instructions,json.dumps(context,ensure_ascii=False),MotionResponse)
    if cancel and cancel.is_set():raise ValueError('Motion prompt generation canceled')
    if not isinstance(result,MotionResponse):raise ValueError('Language model returned no motion prompt')
    prompt=MotionPrompt(shot_id=shot.identifier,scene_id=scene.identifier,
        version=(store.prompts(shot.identifier)[0].version+1 if store.prompts(shot.identifier) else 1),
        starting_image_id=image.identifier,ending_image_id=ending_image_id,
        positive_prompt=result.positive_prompt,negative_prompt=result.negative_prompt,
        subject_movements=result.subject_movements,object_movements=result.object_movements,
        environmental_movements=result.environmental_movements,camera_movement=result.camera_movement,
        continuity_constraints=result.continuity_constraints,intended_duration=min(shot.duration or settings.duration,settings.duration),
        settings=settings.model_dump(mode='json'),dependency_fingerprint=source_fingerprint(shot,image,ending,settings,
            [(r['identifier'],r['sha256']) for r in refs],workflow_fingerprint(session.root,settings)))
    return store.save_prompt(prompt)
