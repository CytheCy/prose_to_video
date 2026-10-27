"""Shared image provider contract and strict capability validation."""
from abc import ABC, abstractmethod
from pathlib import Path
from prose_to_video.models.image_job import ImageCapabilities, ImageRequest, ProviderResult


class ImageProviderError(RuntimeError):
    def __init__(self, message: str, *, recoverable: bool = False, uncertain: bool = False):
        super().__init__(message)
        self.recoverable = recoverable
        self.uncertain = uncertain


class ImageProvider(ABC):
    name: str

    @abstractmethod
    def test_connection(self) -> str: ...

    @abstractmethod
    def capabilities(self, model: str) -> ImageCapabilities: ...

    def validate(self, request: ImageRequest):
        c = self.capabilities(request.model)
        if not c.text_to_image: raise ImageProviderError('Text-to-image is unsupported')
        if request.references and not c.references: raise ImageProviderError('This provider cannot use reference images')
        if len(request.references) > c.max_references: raise ImageProviderError(f'Provider accepts at most {c.max_references} references')
        if request.negative_prompt and not c.negative_prompt: raise ImageProviderError('Negative prompts are unsupported')
        if request.seed is not None and not c.seeds: raise ImageProviderError('Seeds are unsupported')
        if request.parent_path and not c.editing: raise ImageProviderError('Image editing is unsupported')
        if request.candidates > c.max_candidates: raise ImageProviderError(f'Provider accepts at most {c.max_candidates} candidates')
        if request.output_format not in c.formats: raise ImageProviderError('Unsupported output format')
        if c.qualities and request.quality not in c.qualities: raise ImageProviderError('Unsupported quality')
        if request.width <= 0 or request.height <= 0: raise ImageProviderError('Invalid dimensions')
        if request.parent_path and not Path(request.parent_path).is_file(): raise ImageProviderError('Parent image is missing')
        for ref in request.references:
            if not Path(ref.path).is_file(): raise ImageProviderError(f'Reference image is missing: {ref.asset_id}')

    @abstractmethod
    def submit(self, request: ImageRequest) -> ProviderResult: ...

    def poll(self, provider_job_id: str) -> ProviderResult:
        raise ImageProviderError('Job lookup is unsupported')

    def cancel(self, provider_job_id: str) -> bool:
        return False
