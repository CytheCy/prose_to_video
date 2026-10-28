"""Animation manifests and stable ordered exports."""
import json
import shutil
from pathlib import Path
from prose_to_video.storage.generated_videos import VideoStore
from prose_to_video.pipeline.video_generation import clip_freshness

def video_manifest(session):
    store=VideoStore(session.root);rows=[]
    for scene in session.database.load_scenes()[1]:
        board=session.database.load_storyboard(scene.identifier)
        if not board:continue
        for shot in board.shots:
            clips,locked=store.approved(shot.identifier)
            mode,coverage,_=store.production(shot.identifier)
            prompt=store.approved_prompt(shot.identifier)
            for clip in store.clips(shot.identifier):
                job=store.job(clip.job_id)
                rows.append({'scene':scene.sequence,'shot':shot.sequence,'scene_id':scene.identifier,
                    'shot_id':shot.identifier,'production_mode':mode,'starting_image':clip.starting_image_id,
                    'ending_image':clip.ending_image_id,'motion_prompt':prompt.positive_prompt if prompt else None,
                    'prompt_id':clip.prompt_id,'provider':clip.provider,'model':clip.model,'clip_path':clip.path,
                    'actual_duration':clip.duration,'requested_duration':job.requested_duration if job else None,
                    'approval_status':clip.approval_status,'selected':clip.identifier in [c.identifier for c in clips],
                    'review_issues':clip.review_issues,'review_notes':clip.review_notes,
                    'locked':locked,'dependency_status':clip_freshness(session,shot,clip),
                    'generation_settings':job.settings if job else None,'job_status':job.status if job else None,
                    'usage':job.usage if job else None,'estimated_cost':job.estimated_cost if job else None,
                    'actual_cost':job.actual_cost if job else None,
                    'coverage':coverage,'parent_id':clip.parent_id})
    return rows

def manifest_markdown(rows):
    lines=['# Animated video clips','']
    for row in rows:
        lines.extend([f"## Scene {row['scene']} · Shot {row['shot']}",
            f"- Clip: `{row['clip_path']}`",f"- Duration: {row['actual_duration']:.3f}s",
            f"- Provider: {row['provider']} / {row['model']}",
            f"- Approval: {row['approval_status']}",f"- Dependency: {row['dependency_status']}",
            f"- Motion: {row['motion_prompt'] or ''}",''])
    return '\n'.join(lines)

def export_approved_clips(session,folder):
    folder=Path(folder);folder.mkdir(parents=True,exist_ok=True)
    store=VideoStore(session.root);copies=[];manifest=[]
    for scene in session.database.load_scenes()[1]:
        board=session.database.load_storyboard(scene.identifier)
        if not board:continue
        for shot in board.shots:
            clips,_=store.approved(shot.identifier)
            for index,clip in enumerate(clips,1):
                name=f'scene_{scene.sequence:03d}_shot_{shot.sequence:03d}_clip_{index:02d}.{clip.format}'
                source=session.root/clip.path
                if not source.is_file():raise ValueError(f'Approved clip is missing: {clip.path}')
                copies.append((source,folder/name))
                manifest.append({'file':name,'scene_id':scene.identifier,'shot_id':shot.identifier,
                    'clip_id':clip.identifier,'duration':clip.duration,'source_path':clip.path})
    destination=folder/'approved_clips.json'
    if destination.exists() or any(target.exists() for _,target in copies):
        raise FileExistsError('Export destination contains an approved clip or manifest')
    written=[]
    try:
        for source,target in copies:shutil.copy2(source,target);written.append(target)
        destination.write_text(json.dumps(manifest,indent=2),encoding='utf-8')
    except Exception:
        for target in written:target.unlink(missing_ok=True)
        raise
    return destination
