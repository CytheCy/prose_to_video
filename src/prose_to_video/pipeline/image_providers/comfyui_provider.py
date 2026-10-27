"""ComfyUI API workflow adapter with configurable node/input mapping."""
import copy
import json
import mimetypes
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from uuid import uuid4
from prose_to_video.models.image_job import ImageCapabilities, ImageRequest, ProviderImage, ProviderResult
from .base import ImageProvider, ImageProviderError


class ComfyUIProvider(ImageProvider):
    name = 'ComfyUI'
    def __init__(self, url='http://127.0.0.1:8188', workflow=None, mapping=None, timeout=30):
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme not in {'http','https'} or not parsed.netloc or parsed.username or parsed.password:
            raise ImageProviderError('Invalid ComfyUI server URL')
        self.url = url.rstrip('/')
        self.workflow = workflow or {}
        self.mapping = mapping or {}
        self.timeout = timeout
        self.client_id = str(uuid4())

    def _json(self, route, body=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.url + route, data=data,
                                     headers={'Content-Type':'application/json'} if data else {})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as response:
                return json.load(response)
        except urllib.error.HTTPError as exc:
            detail=exc.read(4096).decode('utf-8','replace')
            raise ImageProviderError(f'ComfyUI HTTP {exc.code}: {detail}',
                                     uncertain=body is not None and exc.code>=500) from exc
        except (urllib.error.URLError,TimeoutError,OSError) as exc:
            raise ImageProviderError(f'ComfyUI connection failed: {exc}', uncertain=body is not None) from exc

    def test_connection(self):
        self._json('/system_stats')
        return 'Connected to ComfyUI'

    def capabilities(self, model):
        m = self.mapping
        return ImageCapabilities(references=bool(m.get('references')), multiple_references=len(m.get('references',[]))>1,
            negative_prompt='negative_prompt' in m, seeds='seed' in m, multiple_candidates='candidates' in m,
            exact_dimensions='width' in m and 'height' in m, editing=bool(m.get('parent_image')),
            asynchronous=True, progress=True, cancellation=True,
            max_references=len(m.get('references',[])), max_candidates=10 if 'candidates' in m else 1,
            formats=['png','jpeg','webp'] if 'output_format' in m else ['png'],
            qualities=['auto','low','medium','high'] if 'quality' in m else ['auto'])

    def _set(self, workflow, target, value):
        if not isinstance(target, dict) or 'node' not in target or 'input' not in target:
            raise ImageProviderError('Workflow mapping needs node and input keys')
        node = workflow.get(str(target['node']))
        if not isinstance(node,dict) or target['input'] not in node.get('inputs',{}):
            raise ImageProviderError(f"Workflow input is missing: {target['node']}.{target['input']}")
        node['inputs'][target['input']] = value

    def validate(self, request):
        super().validate(request)
        if not self.workflow or not isinstance(self.workflow,dict): raise ImageProviderError('Import an API-format ComfyUI workflow')
        if 'positive_prompt' not in self.mapping or 'outputs' not in self.mapping:
            raise ImageProviderError('Map positive_prompt and output nodes')
        if 'width' not in self.mapping or 'height' not in self.mapping:
            raise ImageProviderError('Map width and height so requested dimensions are applied')
        if request.model and 'model' not in self.mapping:
            raise ImageProviderError('Map model selection or leave the model field empty to use the workflow model')
        if request.candidates>1 and 'candidates' not in self.mapping:
            raise ImageProviderError('Map candidates to a workflow batch input')
        for node_id,node in self.workflow.items():
            if not isinstance(node,dict) or 'class_type' not in node or not isinstance(node.get('inputs'),dict):
                raise ImageProviderError(f'Invalid API workflow node {node_id}')
        for key in ('positive_prompt','negative_prompt','width','height','seed','model','steps','cfg','sampler','scheduler','parent_image','candidates','quality','output_format'):
            if key in self.mapping: self._set(copy.deepcopy(self.workflow),self.mapping[key], '')
        for target in self.mapping.get('references',[]): self._set(copy.deepcopy(self.workflow),target,'')
        self._reference_assignments(request.references)
        for node in self.mapping['outputs']:
            if str(node) not in self.workflow: raise ImageProviderError(f'Output node {node} is missing')

    def _reference_assignments(self,references):
        remaining=list(references);pairs=[]
        for target in self.mapping.get('references',[]):
            if not remaining:break
            role=str(target.get('role','')).casefold()
            index=next((i for i,ref in enumerate(remaining) if ref.role.casefold()==role),None) if role else 0
            if index is None:continue
            pairs.append((remaining.pop(index),target))
        if remaining:raise ImageProviderError('Workflow has no compatible input for reference roles: '+
            ', '.join(ref.role for ref in remaining))
        return pairs

    def _upload(self, path):
        boundary = '----prose' + uuid4().hex
        p = Path(path)
        content = p.read_bytes()
        if len(content) > 50_000_000: raise ImageProviderError('Reference image exceeds 50 MB')
        body = (f'--{boundary}\r\nContent-Disposition: form-data; name="image"; filename="{p.name}"\r\n'
                f'Content-Type: {mimetypes.guess_type(p.name)[0] or "application/octet-stream"}\r\n\r\n').encode() + content + f'\r\n--{boundary}--\r\n'.encode()
        req = urllib.request.Request(self.url+'/upload/image',data=body,
            headers={'Content-Type':f'multipart/form-data; boundary={boundary}'})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as response: return json.load(response)['name']
        except (urllib.error.URLError,TimeoutError,OSError,KeyError) as exc:
            raise ImageProviderError(f'ComfyUI image upload failed: {exc}') from exc

    def submit(self, request):
        self.validate(request)
        workflow = copy.deepcopy(self.workflow)
        values = {'positive_prompt':request.prompt,'negative_prompt':request.negative_prompt,
                  'width':request.width,'height':request.height,'seed':request.seed,'model':request.model,
                  'candidates':request.candidates,'quality':request.quality,
                  'output_format':request.output_format}
        values.update(request.options)
        for key,value in values.items():
            if key in self.mapping and value is not None: self._set(workflow,self.mapping[key],value)
        for ref,target in self._reference_assignments(request.references):
            self._set(workflow,target,self._upload(ref.path))
        if request.parent_path: self._set(workflow,self.mapping['parent_image'],self._upload(request.parent_path))
        result = self._json('/prompt',{'prompt':workflow,'client_id':self.client_id})
        if 'error' in result: raise ImageProviderError(str(result['error']))
        if not result.get('prompt_id'): raise ImageProviderError('ComfyUI did not return a prompt ID',uncertain=True)
        return ProviderResult(status='Submitted',provider_job_id=result['prompt_id'])

    def poll(self, provider_job_id):
        data = self._json('/history/'+urllib.parse.quote(provider_job_id,safe=''))
        item = data.get(provider_job_id)
        if not item:
            queue=self._json('/queue')
            queued=queue.get('queue_running',[])+queue.get('queue_pending',[])
            if any(provider_job_id in entry for entry in queued):
                return ProviderResult(status='Running',provider_job_id=provider_job_id)
            return ProviderResult(status='Needs Reconciliation',provider_job_id=provider_job_id,
                error='Job is absent from ComfyUI queue and history')
        status = item.get('status',{})
        if status.get('status_str') == 'error':
            return ProviderResult(status='Failed',provider_job_id=provider_job_id,error='ComfyUI workflow failed')
        if not status.get('completed', False):
            return ProviderResult(status='Running',provider_job_id=provider_job_id)
        images=[]
        for node_id in self.mapping.get('outputs',[]):
            for entry in item.get('outputs',{}).get(str(node_id),{}).get('images',[]):
                query=urllib.parse.urlencode({k:entry[k] for k in ('filename','subfolder','type') if k in entry})
                try:
                    with urllib.request.urlopen(self.url+'/view?'+query,timeout=self.timeout) as response:
                        images.append(ProviderImage(data=response.read(),model='ComfyUI',metadata={'node':str(node_id)}))
                except (urllib.error.URLError,TimeoutError,OSError) as exc:
                    raise ImageProviderError(f'ComfyUI image retrieval failed: {exc}',recoverable=True) from exc
        return ProviderResult(status='Completed' if images else 'Failed',provider_job_id=provider_job_id,
            images=images,error='' if images else 'Workflow produced no mapped output images')

    def cancel(self, provider_job_id):
        queue=self._json('/queue')
        if any(provider_job_id in entry for entry in queue.get('queue_pending',[])):
            self._json('/queue',{'delete':[provider_job_id]})
            return True
        return False
