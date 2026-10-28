"""Strict provider-neutral video generation contract."""
from abc import ABC, abstractmethod
from pathlib import Path
from prose_to_video.models.video_job import VideoCapabilities, VideoRequest, VideoProviderResult

class VideoProviderError(RuntimeError):
    def __init__(self, message, *, recoverable=False, uncertain=False):
        super().__init__(message)
        self.recoverable, self.uncertain = recoverable, uncertain

class VideoProvider(ABC):
    name: str
    @abstractmethod
    def test_connection(self) -> str: ...
    @abstractmethod
    def capabilities(self, model: str) -> VideoCapabilities: ...
    def validate(self, request: VideoRequest):
        c=self.capabilities(request.model)
        if not c.first_frame or not Path(request.starting_image_path).is_file():
            raise VideoProviderError('A readable starting image is required')
        if request.ending_image_path and (not c.last_frame or not Path(request.ending_image_path).is_file()):
            raise VideoProviderError('Ending image is unsupported or missing')
        if len(request.reference_paths)>c.references or any(not Path(p).is_file() for p in request.reference_paths):
            raise VideoProviderError('Reference images are unsupported or missing')
        for value, supported, label in ((request.negative_prompt,c.negative_prompt,'negative prompt'),
            (request.duration,c.duration,'duration'),(request.frame_count,c.frame_count,'frame count'),
            (request.width or request.height,c.resolution,'resolution'),
            (request.frame_rate,c.frame_rate,'frame rate'),(request.seed,c.seed,'seed'),
            (request.motion_intensity,c.motion_intensity,'motion intensity')):
            if value and not supported: raise VideoProviderError(f'Workflow cannot control {label}')
        if (request.width is None) != (request.height is None): raise VideoProviderError('Width and height must be paired')
        if c.max_duration and request.duration and request.duration>c.max_duration:
            raise VideoProviderError('Requested duration exceeds workflow limit')
        if c.supported_frame_rates and request.frame_rate and request.frame_rate not in c.supported_frame_rates:
            raise VideoProviderError('Requested frame rate is unsupported')
        if c.supported_resolutions and request.width and (request.width,request.height) not in c.supported_resolutions:
            raise VideoProviderError('Requested resolution is unsupported')
        if request.output_format not in c.formats: raise VideoProviderError('Output format is unsupported')
        if not request.prompt.strip(): raise VideoProviderError('Approved motion prompt is empty')
    @abstractmethod
    def submit(self, request: VideoRequest) -> VideoProviderResult: ...
    def poll(self, provider_job_id: str) -> VideoProviderResult:
        raise VideoProviderError('Job lookup is unsupported')
    def cancel(self, provider_job_id: str) -> bool: return False
