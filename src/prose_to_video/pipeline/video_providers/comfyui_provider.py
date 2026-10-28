"""Configurable API-format ComfyUI video workflows."""
import copy
import hashlib
import urllib.parse
import urllib.request
import shutil
import tempfile
from pathlib import Path
from prose_to_video.models.video_job import VideoCapabilities, VideoRequest, VideoProviderResult, ProviderVideo
from prose_to_video.pipeline.image_providers.comfyui_provider import ComfyUIProvider as ImageComfyUI
from prose_to_video.pipeline.image_providers.base import ImageProviderError
from .base import VideoProvider, VideoProviderError

class ComfyUIVideoProvider(ImageComfyUI, VideoProvider):
    name='ComfyUI'
    def capabilities(self, model):
        m=self.mapping
        return VideoCapabilities(last_frame='ending_image' in m,references=len(m.get('references',[])),
            negative_prompt='negative_prompt' in m,duration='duration' in m,
            frame_count='frame_count' in m,resolution='width' in m and 'height' in m,
            frame_rate='frame_rate' in m,seed='seed' in m,motion_intensity='motion_intensity' in m,
            continuation=True,cancel=True,job_lookup=True,
            max_duration=m.get('max_duration'),supported_frame_rates=m.get('supported_frame_rates',[]),
            supported_resolutions=m.get('supported_resolutions',[]),formats=m.get('formats',['mp4']))

    def validate(self, request: VideoRequest):
        VideoProvider.validate(self,request)
        if not isinstance(self.workflow,dict) or not self.workflow:
            raise VideoProviderError('Import an API-format video workflow')
        if not {'starting_image','motion_prompt','output_video'} <= self.mapping.keys():
            raise VideoProviderError('Map starting_image, motion_prompt, and output_video')
        if request.model and 'model' not in self.mapping:
            raise VideoProviderError('Map model selection or use the workflow model')
        for node_id,node in self.workflow.items():
            if not isinstance(node,dict) or not isinstance(node.get('inputs'),dict) or 'class_type' not in node:
                raise VideoProviderError(f'Invalid API workflow node {node_id}')
        for key in ('starting_image','ending_image','motion_prompt','negative_prompt','duration',
                    'frame_count','width','height','frame_rate','seed','motion_intensity','model','output_format'):
            if key in self.mapping:
                try:self._set(copy.deepcopy(self.workflow),self.mapping[key],'')
                except ImageProviderError as exc:raise VideoProviderError(str(exc)) from exc
        for target in [self.mapping['starting_image'],*([self.mapping['ending_image']] if 'ending_image' in self.mapping else []),
                       *self.mapping.get('references',[])]:
            if target.get('mode','upload') not in {'upload','reference'}:
                raise VideoProviderError('Image mapping mode must be upload or reference')
            if target.get('mode')=='reference' and not target.get('filename'):
                raise VideoProviderError('Referenced server image needs a filename')
            if target.get('mode')=='reference':
                name=Path(target['filename'])
                if name.is_absolute() or '..' in name.parts or len(name.parts)!=1:
                    raise VideoProviderError('Referenced server image filename must be a basename')
        for target in self.mapping.get('references',[]):
            try:self._set(copy.deepcopy(self.workflow),target,'')
            except ImageProviderError as exc:raise VideoProviderError(str(exc)) from exc
        if str(self.mapping['output_video']) not in self.workflow:
            raise VideoProviderError('Mapped output video node is missing')
        if request.reference_paths and len(request.reference_paths)>len(self.mapping.get('references',[])):
            raise VideoProviderError('Too many reference images for workflow')
        if request.frame_count and request.duration and request.frame_rate and abs(request.frame_count-request.duration*request.frame_rate)>1:
            raise VideoProviderError('Frame count conflicts with duration and frame rate')

    def submit(self, request: VideoRequest):
        self.validate(request)
        workflow=copy.deepcopy(self.workflow)
        values={'motion_prompt':request.prompt,'negative_prompt':request.negative_prompt,
            'duration':request.duration,'frame_count':request.frame_count,'width':request.width,
            'height':request.height,'frame_rate':request.frame_rate,'seed':request.seed,
            'motion_intensity':request.motion_intensity,'model':request.model,'output_format':request.output_format}
        try:
            for key,value in values.items():
                if key in self.mapping and value is not None:self._set(workflow,self.mapping[key],value)
            self._set(workflow,self.mapping['starting_image'],self._image_value(self.mapping['starting_image'],request.starting_image_path))
            if request.ending_image_path:self._set(workflow,self.mapping['ending_image'],
                self._image_value(self.mapping['ending_image'],request.ending_image_path))
            for path,target in zip(request.reference_paths,self.mapping.get('references',[])):
                self._set(workflow,target,self._image_value(target,path))
            result=self._json('/prompt',{'prompt':workflow,'client_id':self.client_id})
        except ImageProviderError as exc:
            raise VideoProviderError(str(exc),uncertain=exc.uncertain,
                recoverable=exc.recoverable or 'HTTP 429' in str(exc)) from exc
        if result.get('error'):raise VideoProviderError(str(result['error']))
        if not result.get('prompt_id'):raise VideoProviderError('ComfyUI returned no prompt ID',uncertain=True)
        return VideoProviderResult(status='Submitted',provider_job_id=result['prompt_id'])

    def _image_value(self,target,path):
        if target.get('mode','upload')=='upload':return self._upload(path)
        query=urllib.parse.urlencode({'filename':target['filename'],
            'subfolder':target.get('subfolder',''),'type':target.get('type','input')})
        remote=hashlib.sha256()
        try:
            with urllib.request.urlopen(self.url+'/view?'+query,timeout=self.timeout) as response:
                while block:=response.read(1024*1024):remote.update(block)
        except (OSError,TimeoutError) as exc:
            raise VideoProviderError(f'Cannot verify referenced ComfyUI image: {exc}',recoverable=True) from exc
        local=hashlib.sha256()
        with Path(path).open('rb') as source:
            while block:=source.read(1024*1024):local.update(block)
        if remote.hexdigest()!=local.hexdigest():
            raise VideoProviderError('Referenced ComfyUI image differs from approved local image')
        return target['filename']

    def poll(self, provider_job_id):
        try:
            data=self._json('/history/'+urllib.parse.quote(provider_job_id,safe=''))
            item=data.get(provider_job_id)
            if not item:
                queue=self._json('/queue')
                if any(provider_job_id in entry for entry in queue.get('queue_pending',[])+queue.get('queue_running',[])):
                    return VideoProviderResult(status='Running',provider_job_id=provider_job_id)
                return VideoProviderResult(status='Needs Reconciliation',provider_job_id=provider_job_id,error='Job absent from queue and history')
            status=item.get('status',{})
            if status.get('status_str')=='error':return VideoProviderResult(status='Failed',provider_job_id=provider_job_id,error='ComfyUI workflow failed')
            if not status.get('completed',False):return VideoProviderResult(status='Running',provider_job_id=provider_job_id)
            output=item.get('outputs',{}).get(str(self.mapping['output_video']),{})
            entries=output.get('gifs',[])+output.get('videos',[])
            if not entries:return VideoProviderResult(status='Failed',provider_job_id=provider_job_id,error='Mapped node produced no video')
            videos=[]
            for entry in entries:
                query=urllib.parse.urlencode({k:entry[k] for k in ('filename','subfolder','type') if k in entry})
                with tempfile.NamedTemporaryFile(prefix='prose-comfy-video-',suffix=Path(entry['filename']).suffix,
                                                 delete=False) as stream:
                    temporary=Path(stream.name)
                    try:
                        with urllib.request.urlopen(self.url+'/view?'+query,timeout=self.timeout) as response:
                            shutil.copyfileobj(response,stream,length=1024*1024)
                    except Exception:
                        temporary.unlink(missing_ok=True);raise
                videos.append(ProviderVideo(path=str(temporary),filename=entry['filename'],model='ComfyUI',
                    metadata={'node':str(self.mapping['output_video']),'temporary':True}))
            return VideoProviderResult(status='Completed',provider_job_id=provider_job_id,videos=videos)
        except (ImageProviderError,OSError,KeyError) as exc:
            raise VideoProviderError(f'ComfyUI video retrieval failed: {exc}',recoverable=True) from exc

    def cancel(self, provider_job_id):
        try:return super().cancel(provider_job_id)
        except ImageProviderError as exc:raise VideoProviderError(str(exc)) from exc
