"""Part-local production sources and timing, sharing the project's selected images."""
from dataclasses import dataclass
import re

from pydantic import Field

from prose_to_video.models.scene import source_hash
from prose_to_video.pipeline.stills_workflow import Workflow
from prose_to_video.storage.database import ProjectDatabase
from prose_to_video.storage.project_files import ProjectSession
from prose_to_video.utils.media_info import inspect_audio
from prose_to_video.utils.prose import read_prose


class PartWorkflow(Workflow):
    storage_path: str = Field(exclude=True)

    def save(self, root):
        directory = root / self.storage_path
        directory.mkdir(parents=True, exist_ok=True)
        super().save(directory)


@dataclass(frozen=True)
class PartSession(ProjectSession):
    storage_path: str

    @property
    def database(self):
        return ProjectDatabase(self.root / self.storage_path / 'alignment.sqlite')


def load_video_part(session, state, section, source_text):
    """Match exported content, not mutable export numbering or splitter checkboxes."""
    raw_body = source_text[section.body_start:section.end]
    body = raw_body.strip()
    normalized_body = body.replace('\r\n', '\n').replace('\r', '\n')
    heading_line = source_text[section.heading_start:section.heading_end].splitlines()[0] if section.heading_start is not None else ''
    match = re.match(r'^ {0,3}#{1,6}\s+(.+?)\s*#*\s*$', heading_line.lstrip('\ufeff'))
    title = match[1] if match else section.heading
    expected = f'# {title}\n\n{normalized_body}\n'
    output = session.root / 'output' / 'chapters'
    pairs = []
    for prose in sorted(output.glob('*.md')):
        if read_prose(prose) != expected:
            continue
        for extension in ('.mp3', '.wav', '.flac'):
            audio = prose.with_suffix(extension)
            if audio.is_file():
                pairs.append((prose, audio))
    if len(pairs) != 1:
        raise ValueError('Export this part’s matching prose and audio in Podcast Splitter first.'
                         if not pairs else 'More than one split audio matches this part. Keep one export pair in output/chapters.')
    prose, audio = pairs[0]
    info = inspect_audio(audio)
    if not info.duration:
        raise ValueError('The split audio has no usable duration.')
    split_text = read_prose(prose)
    original_start = section.body_start + len(raw_body) - len(raw_body.lstrip())
    split_start = len(f'# {title}\n\n')
    if state.prose_hash and state.prose_hash != source_hash(source_text):
        raise ValueError('Prose changed. Analyze the selected parts again before producing video.')
    scenes = []
    for scene in state.scenes:
        if not scene.production_included or scene.end <= original_start or scene.start >= original_start + len(body):
            continue
        start, end = max(scene.start, original_start), min(scene.end, original_start + len(body))
        # Whitespace at a chapter boundary is harmless; story crossing it is not.
        if source_text[scene.start:start].strip() or source_text[end:scene.end].strip():
            raise ValueError('A scene crosses this part’s boundary. Split the scene in Scenes first.')
        a = split_start + len(source_text[original_start:start].replace('\r\n', '\n').replace('\r', '\n'))
        b = split_start + len(source_text[original_start:end].replace('\r\n', '\n').replace('\r', '\n'))
        scenes.append(scene.model_copy(update={'start': a, 'end': b, 'text': split_text[a:b],
                                               'source_version': source_hash(split_text)}))
    storage = f'output/production-parts/{section.identifier}'
    directory = session.root / storage
    directory.mkdir(parents=True, exist_ok=True)
    # This in-memory view points at generated files; it never replaces the imported sources.
    project = session.project.model_copy(update={
        'prose_path': prose.relative_to(session.root).as_posix(),
        'audio_path': audio.relative_to(session.root).as_posix(),
        'audio_duration': info.duration, 'audio_codec': info.codec,
        'audio_sample_rate': info.sample_rate, 'audio_channels': info.channels})
    part_session = PartSession(session.root, project, storage)
    if not part_session.database.path.exists():
        part_session.database.initialize()
    saved_path = directory / 'stills-workflow.json'
    saved = Workflow.model_validate_json(saved_path.read_text()) if saved_path.exists() else None
    data = state.model_dump()
    data.update(prose_hash=source_hash(split_text), scenes=scenes, timeline=[], timeline_audio_hash='',
                storage_path=storage)
    part = PartWorkflow(**data)
    anchors = lambda values: [(s.identifier, s.start, s.end) for s in values]
    if saved and saved.prose_hash == part.prose_hash and anchors(saved.scenes) == anchors(scenes):
        part.timeline, part.timeline_audio_hash, part.timeline_mode = saved.timeline, saved.timeline_audio_hash, saved.timeline_mode
        old = {s.identifier: s for s in saved.scenes}
        for scene in part.scenes:
            for name in ('camera_movement', 'camera_focal_x', 'camera_focal_y'):
                setattr(scene, name, getattr(old[scene.identifier], name))
    return part_session, part
