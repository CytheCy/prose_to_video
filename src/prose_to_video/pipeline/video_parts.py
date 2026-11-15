"""Part-local production sources and timing, sharing the project's selected images."""
from dataclasses import dataclass

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
    from prose_to_video.pipeline.chapter_sources import chapter_source
    prose, split_text = chapter_source(session, section, source_text)
    pairs = [(prose, prose.with_suffix(ext)) for ext in ('.mp3', '.wav', '.flac')
             if prose.with_suffix(ext).is_file()]
    if len(pairs) != 1:
        raise ValueError('Export this part’s matching prose and audio in Podcast Splitter first.'
                         if not pairs else 'More than one split audio matches this part. Keep one export pair in output/chapters.')
    prose, audio = pairs[0]
    info = inspect_audio(audio)
    if not info.duration:
        raise ValueError('The split audio has no usable duration.')
    split_text = read_prose(prose)
    original_start = section.body_start + len(raw_body) - len(raw_body.lstrip())
    from prose_to_video.utils.prose import without_headings
    clean = without_headings(split_text)
    if clean != split_text:
        raise ValueError('Re-export this part in Podcast Splitter to remove headings from its Markdown file.')
    split_start = len(split_text) - len(split_text.lstrip())
    if state.prose_hash and state.prose_hash != source_hash(source_text):
        raise ValueError('Prose changed. Analyze the selected parts again before producing video.')
    scenes = []
    for scene in state.scenes:
        if not scene.production_included or scene.end <= original_start or scene.start >= original_start + len(body):
            continue
        start, end = max(scene.start, original_start), min(scene.end, original_start + len(body))
        # Whitespace at a chapter boundary is harmless; story crossing it is not.
        if without_headings(source_text[scene.start:start]).strip() or without_headings(source_text[end:scene.end]).strip():
            raise ValueError('A scene crosses this part’s boundary. Split the scene in Scenes first.')
        a = split_start + len(source_text[original_start:start].replace('\r\n', '\n').replace('\r', '\n'))
        b = split_start + len(source_text[original_start:end].replace('\r\n', '\n').replace('\r', '\n'))
        # Alignment validates this part as its own manuscript, so its scene order
        # must also be local. Keep identifiers to share selected stills and timing.
        scenes.append(scene.model_copy(update={'sequence': len(scenes) + 1,
                                               'start': a, 'end': b, 'text': split_text[a:b],
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
