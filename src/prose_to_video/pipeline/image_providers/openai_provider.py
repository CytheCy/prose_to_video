"""OpenAI Images API adapter. Credentials are read only from the environment."""
import base64
import os
from contextlib import ExitStack
from pathlib import Path
from prose_to_video.models.image_job import ImageCapabilities, ImageRequest, ProviderImage, ProviderResult
from .base import ImageProvider, ImageProviderError


class OpenAIImageProvider(ImageProvider):
    name = 'OpenAI'
    def __init__(self, client=None, credential_env='OPENAI_API_KEY'):
        self.client = client
        self.credential_env = credential_env

    def _client(self):
        if self.client is not None: return self.client
        if not os.getenv(self.credential_env): raise ImageProviderError(f'Set {self.credential_env} before generating images')
        from openai import OpenAI
        return OpenAI(api_key=os.environ[self.credential_env], timeout=120, max_retries=0)

    def test_connection(self):
        self._client().models.list()
        return 'Connected to OpenAI'

    def capabilities(self, model):
        if model in {'gpt-image-1','dall-e-2','dall-e-3'}:
            raise ImageProviderError('Retired image model; choose a current GPT Image model')
        supported={'gpt-image-1-mini','gpt-image-1.5','gpt-image-2','gpt-image-2-2026-04-21',
            'gpt-image-2.5-sunburst','gpt-image-2.5-sunburst-2026-09-08',
            'gpt-image-2.5-flare','gpt-image-2.5-flare-2026-09-08','chatgpt-image-latest'}
        if model not in supported:
            raise ImageProviderError('Unknown image model; capability validation needs an adapter update')
        flexible = model.startswith(('gpt-image-2', 'gpt-image-2.5'))
        advanced = model.startswith('gpt-image-2.5')
        return ImageCapabilities(references=True, multiple_references=True, multiple_candidates=True,
            exact_dimensions=flexible, editing=True, max_references=16, max_candidates=10,
            formats=['png','jpeg','webp'], qualities=['auto','low','medium','high'] + (['xhigh','max'] if advanced else []))

    def validate(self, request):
        super().validate(request)
        if len(request.prompt) > 32000: raise ImageProviderError('Prompt exceeds 32,000 characters')
        if self.capabilities(request.model).exact_dimensions:
            if request.width % 16 or request.height % 16 or not (1/3 <= request.width/request.height <= 3):
                raise ImageProviderError('Image dimensions must be multiples of 16 with aspect ratio between 1:3 and 3:1')
            if request.width > 3840 or request.height > 2160:
                raise ImageProviderError('Image dimensions exceed model limits')
        elif (request.width,request.height) not in {(1024,1024),(1536,1024),(1024,1536)}:
            raise ImageProviderError('This model supports 1024×1024, 1536×1024, or 1024×1536')

    def submit(self, request: ImageRequest) -> ProviderResult:
        self.validate(request)
        kwargs = dict(model=request.model, prompt=request.prompt, n=request.candidates,
                      size=f'{request.width}x{request.height}', quality=request.quality,
                      output_format=request.output_format)
        try:
            client = self._client()
            if request.references or request.parent_path:
                paths = ([request.parent_path] if request.parent_path else []) + [r.path for r in request.references]
                with ExitStack() as stack:
                    files = [stack.enter_context(Path(p).open('rb')) for p in paths]
                    response = client.images.edit(image=files, **kwargs)
            else:
                response = client.images.generate(**kwargs)
            images = [ProviderImage(data=base64.b64decode(item.b64_json, validate=True),
                         model=getattr(response,'model',None) or request.model)
                      for item in response.data if getattr(item,'b64_json',None)]
            if not images: raise ImageProviderError('OpenAI returned no image data')
            usage = getattr(response,'usage',None)
            return ProviderResult(status='Completed', images=images,
                usage=usage.model_dump(exclude_none=True) if hasattr(usage,'model_dump') else {})
        except ImageProviderError: raise
        except Exception as exc:
            status = getattr(exc, 'status_code', None)
            recoverable = status == 429
            # A timeout/transport failure after submission is uncertain and must not be retried automatically.
            uncertain = status in {500,502,503,504} or (status is None and exc.__class__.__name__ in {'APITimeoutError','APIConnectionError'})
            raise ImageProviderError(str(exc), recoverable=recoverable, uncertain=uncertain) from exc
