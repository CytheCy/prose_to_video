"""Persistent image-to-video submission, recovery, and continuation preparation."""
from __future__ import annotations
import json
import time
from pathlib import Path
from threading import Lock
from prose_to_video.models.video_job import VideoRequest, VideoJob, VideoGenerationSettings
from prose_to_video.models.generated_video import now
from prose_to_video.pipeline.motion_prompt_generation import source_fingerprint, resolve_ending_image, motion_references, workflow_fingerprint
from prose_to_video.pipeline.video_providers.base import VideoProviderError
from prose_to_video.pipeline.video_providers.comfyui_provider import ComfyUIVideoProvider
from prose_to_video.storage.generated_images import ImageStore
from prose_to_video.storage.generated_videos import VideoStore, extract_final_frame
from prose_to_video.storage.generated_videos import hash_file

def make_video_provider(settings, root):
    if settings.provider!='ComfyUI':raise ValueError(f'Unsupported video provider: {settings.provider}')
    path=Path(settings.workflow_path)
    if not path.is_absolute():path=Path(root)/path
    if not path.is_file():raise ValueError('Import a ComfyUI API-format video workflow')
    return ComfyUIVideoProvider(settings.comfyui_url,json.loads(path.read_text()),settings.workflow_mapping)

class VideoGenerator:
    _budget_lock=Lock()
    def __init__(self,session,provider=None):
        self.session=session;self.store=VideoStore(session.root);self.images=ImageStore(session.root)
        self.provider=provider or make_video_provider(self.store.settings(),session.root)

    def prepare(self,scene,shot,*,parent_clip_id=None,candidate_number=1,continuation_prompt=''):
        settings=self.store.settings();prompt=self.store.approved_prompt(shot.identifier)
        if not prompt:raise ValueError('Approve a motion prompt first')
        selected=self.images.approved(shot.identifier)
        if not selected or selected[0].identifier!=prompt.starting_image_id:
            raise ValueError('Approved starting image changed; review motion prompt')
        image=selected[0];ending=resolve_ending_image(self.session.root,prompt.ending_image_id)
        if prompt.ending_image_id and not ending:raise ValueError('Ending image is missing')
        if prompt.ending_image_id and not settings.use_last_frame:raise ValueError('Last-frame conditioning is disabled')
        if ending and abs(image.width/image.height-ending.width/ending.height)>.02:
            raise ValueError('Ending image aspect ratio differs')
        refs=motion_references(self.session.root,shot.identifier,settings.use_references)
        if len(refs)>self.provider.capabilities(settings.model).references:
            raise VideoProviderError('Workflow cannot use all approved reference images')
        for asset in [image,*([ending] if ending else [])]:
            if not (self.session.root/asset.path).is_file() or hash_file(self.session.root/asset.path)!=asset.sha256:
                raise ValueError('Starting or ending image is missing or changed')
        for ref in refs:
            if not (self.session.root/ref['path']).is_file() or hash_file(self.session.root/ref['path'])!=ref['sha256']:
                raise ValueError('Reference image is missing or changed')
        current=source_fingerprint(shot,image,ending,settings,[(r['identifier'],r['sha256']) for r in refs],
            workflow_fingerprint(self.session.root,settings))
        if current!=prompt.dependency_fingerprint:raise ValueError('Motion prompt dependencies changed')
        start_path=self.session.root/image.path
        if parent_clip_id:
            parent=self.store.clip(parent_clip_id)
            if not parent or parent.shot_id!=shot.identifier:raise ValueError('Continuation parent is invalid')
            if not self.provider.capabilities(settings.model).continuation:raise VideoProviderError('Provider cannot continue a clip')
            frame=self.session.root/'video_clips'/f'{parent.identifier}.final.png'
            if not frame.exists():extract_final_frame(self.session.root/parent.path,frame)
            start_path=frame
            if not continuation_prompt.strip():
                raise ValueError('Write a continuation prompt describing the next short movement')
        elif continuation_prompt.strip():raise ValueError('Continuation prompt needs a parent clip')
        mapping=getattr(self.provider,'mapping',None)
        if mapping is not None:
            duration=settings.duration if 'duration' in mapping else None
            frame_count=round(settings.duration*settings.frame_rate) if 'frame_count' in mapping else None
            if duration is None and frame_count is None and mapping.get('fixed_duration')!=settings.duration:
                raise VideoProviderError('Workflow cannot control requested duration; configure fixed_duration')
            if 'frame_rate' not in mapping and mapping.get('fixed_frame_rate')!=settings.frame_rate:
                raise VideoProviderError('Workflow cannot control requested frame rate; configure fixed_frame_rate')
            mapped_resolution='width' in mapping and 'height' in mapping
            if not mapped_resolution and (mapping.get('fixed_width'),mapping.get('fixed_height'))!=(settings.width,settings.height):
                raise VideoProviderError('Workflow cannot control requested resolution; configure fixed_width and fixed_height')
            if 'output_format' not in mapping and mapping.get('fixed_output_format','mp4')!=settings.output_format:
                raise VideoProviderError('Workflow cannot control requested output format')
            fps=settings.frame_rate if 'frame_rate' in mapping else None
        else:
            caps=self.provider.capabilities(settings.model)
            duration=settings.duration if caps.duration else None
            frame_count=round(settings.duration*settings.frame_rate) if caps.frame_count else None
            mapped_resolution=caps.resolution
            fps=settings.frame_rate if caps.frame_rate else None
        request=VideoRequest(model=settings.model,starting_image_path=str(start_path),
            ending_image_path=str(self.session.root/ending.path) if ending else None,
            reference_paths=[str(self.session.root/r['path']) for r in refs],
            prompt=continuation_prompt.strip() if parent_clip_id else prompt.positive_prompt,
            width=settings.width if mapped_resolution else None,height=settings.height if mapped_resolution else None,
            duration=duration,frame_count=frame_count,
            frame_rate=fps,seed=settings.seed,
            motion_intensity=settings.motion_intensity,output_format=settings.output_format)
        self.provider.validate(request)
        job=VideoJob(project_id=self.session.project.identifier,scene_id=scene.identifier,shot_id=shot.identifier,
            provider=settings.provider,model=settings.model,starting_image_id=image.identifier,
            ending_image_id=prompt.ending_image_id,prompt_id=prompt.identifier,requested_duration=settings.duration,
            requested_width=settings.width,requested_height=settings.height,
            settings={**settings.model_dump(mode='json'),'continuation_prompt':continuation_prompt.strip()},
            dependency_fingerprint=current,parent_clip_id=parent_clip_id,candidate_number=candidate_number,
            estimated_cost=settings.estimated_cost_per_clip)
        return job,request

    def enqueue(self,scene,shot,**kwargs):
        job,request=self.prepare(scene,shot,**kwargs);self.store.save_job(job);return job,request

    def request_for_job(self,job):
        if job.status!='Pending':raise ValueError('Only never-submitted jobs can be rebuilt')
        scene=next((s for s in self.session.database.load_scenes()[1] if s.identifier==job.scene_id),None)
        board=self.session.database.load_storyboard(job.scene_id)
        shot=next((s for s in board.shots if s.identifier==job.shot_id),None) if board else None
        if not scene or not shot:raise ValueError('Saved storyboard shot is missing')
        continuation=job.settings.get('continuation_prompt','')
        check,request=self.prepare(scene,shot,parent_clip_id=job.parent_clip_id,
            candidate_number=job.candidate_number,continuation_prompt=continuation)
        if (check.prompt_id,check.starting_image_id,check.ending_image_id,check.dependency_fingerprint,
                check.provider,check.model)!=(job.prompt_id,job.starting_image_id,job.ending_image_id,
                job.dependency_fingerprint,job.provider,job.model):
            raise ValueError('Saved video job dependencies changed; do not resubmit')
        return request

    def _budget(self,job):
        cap=self.store.settings().spending_cap
        if cap is None:return
        total=0
        for item in self.store.jobs():
            if item.identifier==job.identifier or item.status in {'Pending','Canceled','Failed'}:continue
            value=item.actual_cost if item.actual_cost is not None else item.estimated_cost
            if value is None:raise ValueError('Video spending cap cannot be evaluated with unknown cost')
            total+=value
        if job.estimated_cost is None or total+job.estimated_cost>cap:
            raise ValueError('Project video spending cap reached or estimate unavailable')

    def run(self,job,request,*,cancel=None):
        if job.status!='Pending':raise ValueError('Only pending video jobs can be submitted')
        if cancel and cancel.is_set():job.status='Canceled';self.store.update_job(job);return job
        with self._budget_lock:
            self._budget(job)
            job.status='Submitted';job.submitted_at=now();self.store.update_job(job)
        received=False
        try:
            retries=VideoGenerationSettings.model_validate(job.settings).retry_limit
            for attempt in range(retries+1):
                try:result=self.provider.submit(request);break
                except VideoProviderError as exc:
                    if not exc.recoverable or exc.uncertain or attempt>=retries:raise
                    time.sleep(min(2**attempt,8))
            received=True;job.provider_job_id=result.provider_job_id
            self._apply(job,result)
        except VideoProviderError as exc:
            job.status='Needs Reconciliation' if exc.uncertain else 'Failed';job.error=str(exc);self.store.update_job(job)
        except Exception as exc:
            job.status='Failed' if received else 'Needs Reconciliation';job.error=str(exc);self.store.update_job(job)
        return job

    def _apply(self,job,result):
        if result.status=='Completed':
            if not result.videos:raise ValueError('Completed video job returned no clips')
            for n,output in enumerate(result.videos,job.candidate_number):
                try:
                    if not any(c.job_id==job.identifier and c.candidate_number==n for c in self.store.clips(job.shot_id)):
                        self.store.save_video(job,output,n)
                finally:
                    if output.path and output.metadata.get('temporary'):
                        Path(output.path).unlink(missing_ok=True)
            job.status='Completed';job.completed_at=now();job.usage=result.usage;job.actual_cost=result.actual_cost
        elif result.status in {'Submitted','Running','Failed','Needs Reconciliation'}:
            job.status=result.status;job.error=result.error
        else:raise ValueError('Invalid provider result')
        self.store.update_job(job)

    def reconcile(self,job):
        if job.status not in {'Submitted','Running','Needs Reconciliation'}:raise ValueError('Job is not unfinished')
        if not job.provider_job_id:
            job.status='Needs Reconciliation';self.store.update_job(job);return job
        try:self._apply(job,self.provider.poll(job.provider_job_id))
        except Exception as exc:
            job.status='Needs Reconciliation';job.error=str(exc);self.store.update_job(job)
        return job

    def cancel(self,job):
        if job.status=='Pending':job.status='Canceled';self.store.update_job(job);return True
        if job.provider_job_id and self.provider.cancel(job.provider_job_id):
            job.status='Canceled';self.store.update_job(job);return True
        return False

def eligible_video_shots(session,scenes,scene_ids=None,include_approved=False):
    store=VideoStore(session.root);selected=[]
    counts={'selected':0,'approved':0,'static':0,'missing_prompt':0,'skipped':0}
    for scene in scenes:
        if scene_ids and scene.identifier not in scene_ids:continue
        board=session.database.load_storyboard(scene.identifier)
        if not board:continue
        for shot in board.shots:
            counts['selected']+=1
            mode,_,_=store.production(shot.identifier)
            if mode!='AI Animation':counts['static']+=1;counts['skipped']+=1;continue
            clips,locked=store.approved(shot.identifier)
            if clips:counts['approved']+=1
            if clips and (locked or not include_approved):counts['skipped']+=1;continue
            if not store.approved_prompt(shot.identifier):counts['missing_prompt']+=1;counts['skipped']+=1;continue
            selected.append((scene,shot))
    return selected,counts

def animation_status(session, shot):
    store=VideoStore(session.root);mode,_,_=store.production(shot.identifier)
    if mode=='Static Image':return 'Static'
    if mode=='Camera Motion':return 'Camera Motion'
    clips,_=store.approved(shot.identifier)
    if clips:
        if any(clip_freshness(session,shot,c)!='Current' for c in clips):
            return 'AI Animation Outdated'
        return 'AI Animation Ready'
    jobs=store.jobs(shot.identifier)
    if any(j.status in {'Submitted','Running'} for j in jobs):return 'AI Animation Generating'
    if any(j.status=='Pending' for j in jobs):return 'AI Animation Pending'
    if any(j.status=='Failed' for j in jobs):return 'AI Animation Failed'
    return 'AI Animation Pending'

def clip_freshness(session,shot,clip):
    store=VideoStore(session.root);prompt=store.approved_prompt(shot.identifier)
    image=ImageStore(session.root).asset(prompt.starting_image_id) if prompt else None
    ending=resolve_ending_image(session.root,prompt.ending_image_id) if prompt else None
    if not prompt or not image or (prompt.ending_image_id and not ending):return 'Outdated'
    approved=ImageStore(session.root).approved(shot.identifier)
    if not approved or approved[0].identifier!=image.identifier:return 'Outdated'
    for asset in [image,*([ending] if ending else [])]:
        if not (session.root/asset.path).is_file() or hash_file(session.root/asset.path)!=asset.sha256:
            return 'Missing'
    refs=motion_references(session.root,shot.identifier,store.settings().use_references)
    for ref in refs:
        if not (session.root/ref['path']).is_file() or hash_file(session.root/ref['path'])!=ref['sha256']:
            return 'Missing'
    current=source_fingerprint(shot,image,ending,store.settings(),
        [(r['identifier'],r['sha256']) for r in refs],workflow_fingerprint(session.root,store.settings()))
    if clip.prompt_id!=prompt.identifier or clip.dependency_fingerprint!=current:return 'Outdated'
    path=session.root/clip.path
    if not path.is_file() or hash_file(path)!=clip.sha256:return 'Missing'
    return 'Current'
