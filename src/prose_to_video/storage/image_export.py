"""Provider-independent image and visual timeline manifests."""
import json
import shutil
from pathlib import Path
from prose_to_video.storage.generated_images import ImageStore
from prose_to_video.pipeline.image_generation import asset_freshness


def image_manifest(session, scenes):
    store=ImageStore(session.root)
    records=[]
    for scene in scenes:
        board=session.database.load_storyboard(scene.identifier)
        if not board: continue
        for shot in board.shots:
            approved=store.approved(shot.identifier)
            assets=store.assets(shot.identifier)
            jobs=[j for j in store.jobs() if j.shot_id==shot.identifier]
            records.append({'scene_id':scene.identifier,'shot_id':shot.identifier,
                'approved_image_path':approved[0].path if approved else None,
                'approved_image_id':approved[0].identifier if approved else None,
                'locked':approved[1] if approved else False,
                'candidates':[a.model_dump(mode='json') for a in assets],
                'jobs':[j.model_dump(mode='json') for j in jobs],
                'freshness':{a.identifier:asset_freshness(session,scene,shot,board,a) for a in assets}})
    return records


def visual_timeline_manifest(session, scenes):
    store=ImageStore(session.root); rows=[]
    for scene in scenes:
        board=session.database.load_storyboard(scene.identifier)
        if not board: continue
        for shot in board.shots:
            approved=store.approved(shot.identifier)
            asset=approved[0] if approved else None
            freshness=asset_freshness(session,scene,shot,board,asset) if asset else 'Missing'
            valid=bool(asset and (session.root/asset.path).is_file() and
                       shot.audio_start is not None and shot.audio_end is not None and shot.audio_end>shot.audio_start)
            rows.append({'scene_id':scene.identifier,'shot_id':shot.identifier,
                'audio_start':shot.audio_start,'audio_end':shot.audio_end,
                'duration':shot.duration,'approved_image':asset.model_dump(mode='json') if asset else None,
                'image_width':asset.width if asset else None,'image_height':asset.height if asset else None,
                'camera_movement':shot.camera_movement,'transition':shot.transition,
                'freshness':freshness,
                'validity':'Ready' if valid else 'Missing image' if not asset else
                    'Missing image file' if not (session.root/asset.path).is_file() else 'Unresolved timing'})
    return rows


def export_approved_images(session, scenes, destination):
    destination=Path(destination)
    destination.mkdir(parents=True,exist_ok=True)
    rows=image_manifest(session,scenes)
    planned=[]
    for index,row in enumerate(rows,1):
        if not row['approved_image_path']: continue
        src=session.root/row['approved_image_path']
        if not src.is_file(): raise ValueError(f'Missing approved image: {src}')
        target=destination/f'{index:05d}_{row["shot_id"]}{src.suffix}'
        if target.exists(): raise FileExistsError(f'Export destination already contains {target.name}')
        planned.append((src,target,row))
    manifest=destination/'approved_images.json'
    if manifest.exists(): raise FileExistsError('Export manifest already exists')
    copied=[]
    try:
        for src,target,row in planned:
            shutil.copy2(src,target);copied.append(target)
        manifest.write_text(json.dumps([{'scene_id':row['scene_id'],'shot_id':row['shot_id'],
            'asset_id':row['approved_image_id'],'file':target.name} for _,target,row in planned],indent=2),encoding='utf-8')
    except Exception:
        for path in copied: path.unlink(missing_ok=True)
        raise
    return manifest


def manifest_markdown(rows):
    lines=['# Generated image assets','']
    for row in rows:
        lines.extend([f'## Scene {row["scene_id"]} · Shot {row["shot_id"]}',
            f'Approved: {row["approved_image_path"] or "None"}',
            f'Candidates: {len(row["candidates"])}',''])
        for asset in row['candidates']:
            lines.append(f'- {asset["identifier"]}: {asset["path"]} · {asset["approval_status"]} · {asset["provider"]} {asset["model"]} · {asset["width"]}×{asset["height"]} · prompt {asset["prompt_id"]} · {row["freshness"][asset["identifier"]]}')
        for job in row['jobs']:
            lines.append(f'- Job {job["identifier"]}: {job["status"]} · prompt {job["prompt_id"]} · references {", ".join(job["reference_ids"]) or "none"} · provider {job["provider"]} {job["model"]}')
            lines.append(f'  - Settings: `{json.dumps(job["settings"],sort_keys=True)}`')
            if job['usage']:lines.append(f'  - Usage: `{json.dumps(job["usage"],sort_keys=True)}`')
            if job['actual_cost'] is not None:lines.append(f'  - Actual cost: {job["actual_cost"]}')
            if job['estimated_cost'] is not None:lines.append(f'  - Estimated cost: {job["estimated_cost"]}')
        lines.append('')
    return '\n'.join(lines)
