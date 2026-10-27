"""Approved-prompt image generation, queue processing, and recovery."""
import hashlib
import json
import time
from pathlib import Path
from prose_to_video.models.generated_image import utcnow
from prose_to_video.models.image_job import ImageGenerationSettings, ImageJob, ImageReference, ImageRequest
from prose_to_video.models.image_prompt import fingerprint
from prose_to_video.pipeline.image_providers.base import ImageProviderError
from prose_to_video.pipeline.image_providers.openai_provider import OpenAIImageProvider
from prose_to_video.pipeline.image_providers.comfyui_provider import ComfyUIProvider
from prose_to_video.storage.generated_images import ImageStore
from prose_to_video.storage.image_prompts import PromptStore
from prose_to_video.storage.visual_library import VisualLibrary


def make_image_provider(settings: ImageGenerationSettings, root=None):
    if settings.provider == 'OpenAI': return OpenAIImageProvider()
    if settings.provider == 'ComfyUI':
        path=Path(settings.workflow_path)
        if not path.is_absolute() and root: path=Path(root)/path
        if not path.is_file(): raise ValueError('Import a ComfyUI API workflow JSON file')
        workflow=json.loads(path.read_text(encoding='utf-8'))
        return ComfyUIProvider(settings.comfyui_url,workflow,settings.workflow_mapping)
    raise ValueError(f'Unknown image provider: {settings.provider}')


def select_references(references, root, max_count):
    """Pinned/primary first, then character/robot/vehicle, then other roles; stable IDs break ties."""
    rank={'character':0,'portrait':0,'full-body':0,'robot':1,'vehicle':1,'location':2,'environment':2}
    ordered=sorted(references,key=lambda r:(not r.get('primary',False),
        rank.get(str(r.get('role','')).lower(),3),r['asset_id']))
    included=ordered[:max_count]
    excluded=ordered[max_count:]
    return [ImageReference(asset_id=r['asset_id'],path=str(Path(root)/r['path']),role=r['role'],
                           primary=r.get('primary',False)) for r in included], [r['asset_id'] for r in excluded]


def openai_reference_instruction(refs, *, has_parent=False):
    lines=[]
    if has_parent:lines.append('Image 1 is the source image to refine.')
    for index,ref in enumerate(refs,2 if has_parent else 1):
        lines.append(f'Image {index} is an approved {ref.role} reference.')
    return '\n'.join(lines)


class ImageGenerator:
    def __init__(self, session, provider=None):
        self.session=session
        self.store=ImageStore(session.root)
        self.prompts=PromptStore(session.root)
        self.provider=provider or make_image_provider(self.store.settings(),session.root)

    def prepare(self, scene, shot, board, *, settings=None, allow_outdated=False, parent_id=None,
                revision_instruction=''):
        settings=settings or self.store.settings()
        prompt=self.prompts.approved(shot.identifier)
        if not prompt: raise ValueError(f'Shot {shot.sequence} has no approved image prompt')
        issues=self.prompts.validate(prompt,scene,shot,board,self.session.project,VisualLibrary(self.session.root))
        fatal=[issue for issue in issues if issue in {'Missing positive prompt','Invalid scene or shot association'}]
        if fatal:raise ValueError('Approved prompt is invalid: '+'; '.join(fatal))
        if issues and not allow_outdated: raise ValueError('Approved prompt is outdated: '+'; '.join(issues))
        # Preserve the approved prompt and its saved reference package even under override.
        refs=prompt.references if settings.use_references else []
        caps=self.provider.capabilities(settings.model)
        if refs and not caps.references: raise ImageProviderError('Selected provider cannot use approved references')
        limit=caps.max_references-(1 if parent_id and settings.provider=='OpenAI' else 0)
        included,excluded=select_references([r.model_dump() for r in refs],self.session.root,max(0,limit))
        for ref in included:
            saved=next(r for r in refs if r.asset_id==ref.asset_id)
            path=Path(ref.path)
            if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest()!=saved.sha256:
                raise ValueError(f'Approved reference changed or is missing: {ref.asset_id}')
        parent=self.store.asset(parent_id) if parent_id else None
        if parent_id and (not parent or parent.shot_id != shot.identifier): raise ValueError('Parent image must belong to this shot')
        translated=prompt.positive_prompt
        negative=prompt.negative_prompt
        transformations=[]
        if negative and not caps.negative_prompt:
            translated+='\nAvoid: '+negative
            negative=''
            transformations.append('Negative prompt appended as Avoid instruction; provider has no negative-prompt field')
        if revision_instruction.strip():
            if not parent_id:raise ValueError('Revision instructions require a parent image')
            translated+='\nRevision request: '+revision_instruction.strip()
            transformations.append('Revision instruction appended to approved prompt for this derived candidate')
        if settings.provider=='OpenAI' and (included or parent_id):
            translated+='\n'+openai_reference_instruction(included,has_parent=bool(parent_id))
            transformations.append('Reference roles described in input image order')
        request=ImageRequest(prompt=translated,negative_prompt=negative,
            model=settings.model,width=settings.width,height=settings.height,quality=settings.quality,
            candidates=settings.candidates,output_format=settings.output_format,seed=settings.seed,
            references=included,parent_path=str(self.session.root/parent.path) if parent else None)
        self.provider.validate(request)
        if settings.provider=='ComfyUI' and settings.candidates>1 and 'candidates' not in self.provider.mapping:
            raise ImageProviderError('Map candidates to a workflow batch input or request one candidate')
        if prompt.aspect_ratio:
            a,b=(int(x) for x in prompt.aspect_ratio.split(':'))
            if abs(settings.width/settings.height-a/b)>0.03:
                raise ValueError('Generation dimensions differ from approved prompt aspect ratio')
        dependency=fingerprint({'prompt':prompt.identifier,'prompt_dependencies':prompt.dependency_fingerprint,
            'provider':settings.provider,'model':settings.model,'settings':settings.model_dump(mode='json'),
            'references':[(r.asset_id,next(p.sha256 for p in refs if p.asset_id==r.asset_id)) for r in included],
            'shot':shot.model_dump(mode='json'),'parent':parent_id,
            'revision_instruction':revision_instruction.strip()})
        job=ImageJob(project_id=self.session.project.identifier,scene_id=scene.identifier,shot_id=shot.identifier,
            prompt_id=prompt.identifier,provider=settings.provider,model=settings.model,
            width=settings.width,height=settings.height,quality=settings.quality,candidates=settings.candidates,
            reference_ids=[r.asset_id for r in included],settings=settings.model_dump(mode='json'),
            dependency_fingerprint=dependency,estimated_cost=(settings.estimated_cost_per_image*settings.candidates
                if settings.estimated_cost_per_image is not None else None),
            included_references=[r.asset_id for r in included],excluded_references=excluded,parent_id=parent_id,
            transformations=transformations,revision_instruction=revision_instruction.strip())
        return job,request,issues

    def enqueue(self, scene, shot, board, **kwargs):
        job,request,issues=self.prepare(scene,shot,board,**kwargs)
        self.store.save_job(job)
        return job,request,issues

    def request_for_job(self, job):
        """Rebuild an unsubmitted request from its immutable approved prompt version."""
        prompt=next((p for p in self.prompts.versions(job.shot_id) if p.identifier==job.prompt_id),None)
        if not prompt:raise ValueError('Saved prompt version is missing')
        settings=ImageGenerationSettings.model_validate(job.settings)
        refs=[]
        for asset_id in job.reference_ids:
            ref=next((r for r in prompt.references if r.asset_id==asset_id),None)
            if not ref:raise ValueError(f'Saved reference {asset_id} is missing from prompt')
            path=self.session.root/ref.path
            if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest()!=ref.sha256:
                raise ValueError(f'Saved reference {asset_id} changed or is missing')
            refs.append(ImageReference(asset_id=ref.asset_id,path=str(path),role=ref.role,primary=ref.primary))
        parent=self.store.asset(job.parent_id) if job.parent_id else None
        if job.parent_id and not parent:raise ValueError('Parent image is missing')
        translated=prompt.positive_prompt
        negative=prompt.negative_prompt
        if job.transformations:
            if any(t.startswith('Negative prompt') for t in job.transformations):
                translated+='\nAvoid: '+negative
                negative=''
        if job.revision_instruction:translated+='\nRevision request: '+job.revision_instruction
        if job.provider=='OpenAI' and (refs or job.parent_id):
            translated+='\n'+openai_reference_instruction(refs,has_parent=bool(job.parent_id))
        request=ImageRequest(prompt=translated,negative_prompt=negative,
            model=job.model,width=job.width,height=job.height,quality=job.quality,candidates=job.candidates,
            output_format=settings.output_format,seed=settings.seed,references=refs,
            parent_path=str(self.session.root/parent.path) if parent else None)
        self.provider.validate(request)
        return request

    def _budget_ok(self, job):
        cap=self.store.settings().spending_cap
        if cap is None: return
        jobs=self.store.jobs()
        total=0.0
        for item in jobs:
            if item.identifier==job.identifier or item.status in {'Canceled','Failed'}: continue
            amount=item.actual_cost if item.actual_cost is not None else item.estimated_cost
            if amount is None: raise ValueError('Spending cap cannot be evaluated without a reliable estimate')
            total+=amount
        if job.estimated_cost is None or total+job.estimated_cost>cap:
            raise ValueError('Project image spending cap reached or cannot be safely evaluated')

    def run(self, job, request, *, cancel=None, progress=None):
        if job.status!='Pending': raise ValueError('Only pending jobs can be submitted')
        if cancel and cancel.is_set():
            job.status='Canceled';self.store.update_job(job);return job
        self._budget_ok(job)
        job.submitted_at=utcnow()
        # Commit before a potentially paid call. An interruption now requires reconciliation.
        job.status='Submitted';self.store.update_job(job)
        try:
            retries=ImageGenerationSettings.model_validate(job.settings).retry_limit
            for attempt in range(retries+1):
                try:
                    result=self.provider.submit(request)
                    break
                except ImageProviderError as exc:
                    if not exc.recoverable or exc.uncertain or attempt>=retries:raise
                    time.sleep(min(2**attempt,8))
            if result.provider_job_id: job.provider_job_id=result.provider_job_id
            if result.status=='Completed': self._complete(job,result)
            elif result.status in {'Submitted','Running'}:
                job.status=result.status;self.store.update_job(job)
            else: raise ImageProviderError(result.error or 'Image generation failed')
            return job
        except ImageProviderError as exc:
            job.error=str(exc)
            job.status='Needs Reconciliation' if exc.uncertain else 'Failed'
            self.store.update_job(job)
            return job
        except Exception as exc:
            job.error=str(exc);job.status='Needs Reconciliation';self.store.update_job(job)
            return job

    def _complete(self,job,result):
        if not result.images:raise ValueError('Provider reported completion without image data')
        existing=[a for a in self.store.assets(job.shot_id) if a.job_id==job.identifier]
        by_number={a.candidate_number:a for a in existing}
        for n,image in enumerate(result.images,1):
            digest=hashlib.sha256(image.data).hexdigest()
            if n in by_number:
                if by_number[n].sha256!=digest:raise ValueError('Provider returned different data for an existing candidate')
                continue
            self.store.save_image(job,image,n,parent_id=job.parent_id)
        job.status='Completed';job.completed_at=utcnow();job.usage=result.usage;job.actual_cost=result.actual_cost
        self.store.update_job(job)

    def reconcile(self,job):
        if job.status not in {'Needs Reconciliation','Submitted','Running'}:
            raise ValueError('Job is not unfinished')
        if not job.provider_job_id:
            job.status='Needs Reconciliation';self.store.update_job(job);return job
        try:
            result=self.provider.poll(job.provider_job_id)
            if result.status=='Completed': self._complete(job,result)
            elif result.status=='Failed': job.status='Failed';job.error=result.error;self.store.update_job(job)
            else: job.status=result.status;self.store.update_job(job)
        except ImageProviderError as exc:
            job.error=str(exc);job.status='Needs Reconciliation';self.store.update_job(job)
        return job

    def cancel(self,job):
        if job.status=='Pending':
            job.status='Canceled';self.store.update_job(job);return True
        if job.status in {'Submitted','Running','Needs Reconciliation'} and job.provider_job_id:
            if self.provider.cancel(job.provider_job_id):
                job.status='Canceled';self.store.update_job(job);return True
        return False


def eligible_shots(session, scenes, *, scene_ids=None, include_approved=False):
    store=ImageStore(session.root); prompts=PromptStore(session.root)
    selected=[];counts={'selected':0,'approved':0,'missing':0,'outdated':0,'skipped':0}
    for scene in scenes:
        if scene_ids and scene.identifier not in scene_ids: continue
        board=session.database.load_storyboard(scene.identifier)
        if not board: continue
        for shot in board.shots:
            counts['selected']+=1
            approved=store.approved(shot.identifier)
            if approved: counts['approved']+=1
            if not store.assets(shot.identifier): counts['missing']+=1
            if approved and not include_approved: counts['skipped']+=1;continue
            prompt=prompts.approved(shot.identifier)
            if not prompt: counts['skipped']+=1;continue
            if prompts.validate(prompt,scene,shot,board,session.project,VisualLibrary(session.root)):
                counts['outdated']+=1;counts['skipped']+=1;continue
            selected.append((scene,shot,board))
    return selected,counts


def asset_freshness(session, scene, shot, board, asset):
    store=ImageStore(session.root)
    if not (session.root/asset.path).is_file():return 'Missing'
    job=store.job(asset.job_id)
    prompt=PromptStore(session.root).approved(shot.identifier)
    if not job or not prompt or prompt.identifier!=asset.prompt_id:return 'Outdated'
    if PromptStore(session.root).validate(prompt,scene,shot,board,session.project,VisualLibrary(session.root)):
        return 'Outdated'
    settings=store.settings()
    if settings.model_dump(mode='json')!=job.settings:return 'Outdated'
    refs=[]
    for asset_id in job.reference_ids:
        ref=next((r for r in prompt.references if r.asset_id==asset_id),None)
        if not ref or not (session.root/ref.path).is_file():return 'Outdated'
        digest=hashlib.sha256((session.root/ref.path).read_bytes()).hexdigest()
        if digest!=ref.sha256:return 'Outdated'
        refs.append((asset_id,ref.sha256))
    current=fingerprint({'prompt':prompt.identifier,'prompt_dependencies':prompt.dependency_fingerprint,
        'provider':settings.provider,'model':settings.model,'settings':settings.model_dump(mode='json'),
        'references':refs,'shot':shot.model_dump(mode='json'),'parent':asset.parent_id,
        'revision_instruction':job.revision_instruction})
    return 'Current' if current==asset.dependency_fingerprint else 'Outdated'
