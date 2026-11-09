"""Persistent transcript corrections and cancellable forced re-alignment."""
from pathlib import Path
from threading import Event
from uuid import uuid4
import os
import tempfile

from pydantic import BaseModel, Field
from prose_to_video.models.alignment import AlignmentRun, TranscriptSegment
from prose_to_video.models.scene import source_hash
from prose_to_video.pipeline.audio_alignment import rematch_alignment
from prose_to_video.pipeline.audio_transcription import (
    AlignmentCanceled, TranscriptionSettings, WhisperXProvider, decode_chunk, runtime, source_file_hash)
from prose_to_video.pipeline.text_matching import MATCHING_VERSION
from prose_to_video.pipeline.manuscript import inclusion_fingerprint
from prose_to_video.storage.manuscript import ManuscriptStore
from prose_to_video.utils.prose import read_prose


class ReviewDraft(BaseModel):
    run_id: str
    prose_hash: str
    audio_hash: str
    texts: dict[str, str] = Field(default_factory=dict)
    listened: list[str] = Field(default_factory=list)

    @classmethod
    def load(cls, root, run):
        path = root / 'alignment' / 'transcript-review.json'
        if path.is_file():
            draft = cls.model_validate_json(path.read_text())
            if (draft.run_id, draft.prose_hash, draft.audio_hash) == (run.identifier, run.prose_hash, run.audio_hash):
                return draft
        return cls(run_id=run.identifier, prose_hash=run.prose_hash, audio_hash=run.audio_hash)

    def save(self, root):
        folder = root / 'alignment'; folder.mkdir(exist_ok=True)
        fd, name = tempfile.mkstemp(dir=folder, suffix='.tmp')
        try:
            with os.fdopen(fd, 'w') as f:
                f.write(self.model_dump_json(indent=2)); f.flush(); os.fsync(f.fileno())
            os.replace(name, folder / 'transcript-review.json')
        finally:
            Path(name).unlink(missing_ok=True)


def prepare_review(session, scenes):
    run = session.database.load_alignment()
    if not run or not run.transcript:
        raise ValueError('Align Narration first to create a transcript for review')
    text = read_prose(session.project.resolved_prose(session.root))
    audio = session.project.resolved_audio(session.root)
    if source_hash(text) != run.prose_hash or source_file_hash(audio) != run.audio_hash:
        raise ValueError('Sources changed; Align Narration before reviewing')
    sections = ManuscriptStore(session.database).sections(text)
    if run.matching_version != MATCHING_VERSION or run.inclusion_fingerprint != inclusion_fingerprint(sections):
        run = rematch_alignment(run, text, scenes, sections)
        session.database.save_alignment(run)
    return run, text


def segment_review(run, text):
    """Map both extra speech and untimed prose to playable transcript segments."""
    segments = run.transcript
    result = [dict(messages=[], source_start=None, source_end=None) for _ in segments]
    if not segments:
        return result
    def owner(seconds):
        return min(range(len(segments)), key=lambda i: max(segments[i].start-seconds, seconds-segments[i].end, 0))
    for issue in run.issues:
        if issue.kind == 'spoken_insertion':
            # Missing ASR timestamps still need review; assign by transcript order/text.
            t = issue.audio_start
            if t is None:
                needle = issue.message.removeprefix('Unmatched speech: ')
                indexes = [i for i,s in enumerate(segments) if any(w.text == needle and w.start is None for w in s.words)]
            else:
                indexes = [owner(t)]
            for i in indexes:
                result[i]['messages'].append(issue.message)
    for j, word in enumerate(run.words):
        if word.status == 'excluded': continue
        if word.audio_start is not None:
            i = owner(word.audio_start)
        else:
            before = next((w.audio_end for w in reversed(run.words[:j]) if w.audio_end is not None), 0)
            after = next((w.audio_start for w in run.words[j+1:] if w.audio_start is not None), run.audio_duration)
            i = owner((before+after)/2)
            result[i]['messages'].append('Missing verified timing: ' + word.text)
        row = result[i]
        row['source_start'] = min(row['source_start'] if row['source_start'] is not None else word.source_start, word.source_start)
        row['source_end'] = max(row['source_end'] or 0, word.source_end)
    # Extra-only segments get neighboring manuscript context rather than an empty pane.
    for i,row in enumerate(result):
        if row['source_start'] is None:
            before = [w for w in run.words if w.audio_end is not None and w.audio_end <= segments[i].start]
            after = [w for w in run.words if w.audio_start is not None and w.audio_start >= segments[i].end]
            row['source_start'] = before[-1].source_end if before else 0
            row['source_end'] = after[0].source_start if after else len(text)
        row['context'] = text[max(0,row['source_start']-80):min(len(text),row['source_end']+80)]
    return result


def realign_review(session, scenes, run, draft, *, provider=None, cancel=None, progress=lambda *_: None):
    """Align edited text to audio; publish only after the entire run validates."""
    cancel = cancel or Event()
    def check():
        if cancel.is_set(): raise AlignmentCanceled('Transcript alignment canceled; previous alignment preserved')
    def sources():
        current = session.database.load_alignment()
        text = read_prose(session.project.resolved_prose(session.root))
        audio = session.project.resolved_audio(session.root)
        if not current or current.identifier != run.identifier or source_hash(text) != run.prose_hash or source_file_hash(audio) != run.audio_hash:
            raise ValueError('Alignment or sources changed; reopen transcript review')
        return text, audio
    check()
    text, audio = sources()
    if (draft.run_id,draft.prose_hash,draft.audio_hash) != (run.identifier,run.prose_hash,run.audio_hash):
        raise ValueError('This transcript draft belongs to another alignment')
    known = {s.identifier for s in run.transcript}
    if set(draft.texts)-known or set(draft.listened)-known:
        raise ValueError('Transcript draft contains unknown segments')
    edited = [s for s in run.transcript if draft.texts.get(s.identifier,s.text) != s.text]
    if any(s.identifier not in draft.listened for s in edited):
        raise ValueError('Listen to each edited segment and mark it checked before re-aligning')
    draft.save(session.root)
    settings = TranscriptionSettings(model=run.transcription_model, device=run.device,
        precision=run.precision, language=next((s.language for s in run.transcript if s.language), 'en'), allow_download=True)
    device, precision = runtime(settings)
    provider = provider or WhisperXProvider()
    progress('Loading word alignment model')
    if hasattr(provider,'load_alignment'): provider.load_alignment(settings,device,precision)
    else: provider.load(settings,device,precision)
    aligned = []
    with tempfile.TemporaryDirectory(prefix='prose-review-') as folder:
        for i, segment in enumerate(run.transcript):
            check()
            corrected = draft.texts.get(segment.identifier,segment.text).strip()
            progress(f'Aligning reviewed transcript {i+1}/{len(run.transcript)}')
            if not corrected:
                # Retain the playable segment so a deleted hallucination can be restored
                # in a later review. Missing manuscript speech still blocks the build.
                aligned.append(segment.model_copy(update={'text': '', 'words': []}))
                continue
            start = max(0,segment.start-.25); end = min(run.audio_duration,segment.end+.25)
            if end <= start: raise ValueError('Transcript segment has no playable audio range')
            path = Path(folder)/'segment.wav'
            decode_chunk(audio,path,start,end-start)
            raw = TranscriptSegment(identifier=segment.identifier,start=segment.start,end=segment.end,
                text=corrected,language=segment.language or settings.language)
            output = provider.align_chunk(path,start,[raw])
            words = [w for s in output for w in s.words]
            if not words: raise ValueError(f'No word timings returned for segment {i+1}; previous alignment preserved')
            aligned.append(raw.model_copy(update={'words':words}))
    check()
    data = run.model_dump()
    data.update(identifier=str(uuid4()), transcript=aligned, reviewed_segments=draft.listened,
        alignment_model=provider.alignment_model)
    # The old derived records are replaced below; keep the input run valid first.
    updated = AlignmentRun.model_validate(data)
    updated = rematch_alignment(updated,text,scenes,ManuscriptStore(session.database).sections(text))
    check(); sources()
    session.database.save_alignment(updated)
    return updated
