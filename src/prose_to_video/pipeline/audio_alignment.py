"""Coordinates source validation, cached speech, text matching, and atomic publication."""
from __future__ import annotations

from pathlib import Path
from threading import Event
from typing import Callable

from prose_to_video.models.alignment import AlignmentIssue, AlignmentRun
from prose_to_video.models.scene import Scene, source_hash, validate_partition
from prose_to_video.pipeline.audio_transcription import (AlignmentCanceled, TranscriptionProvider,
    TranscriptionSettings, WhisperXProvider, runtime, source_file_hash, transcribe)
from prose_to_video.pipeline.text_matching import (derive_scene_timings, derive_spans,
                                                    match_words, sentence_ranges)
from prose_to_video.storage.database import ProjectDatabase
from prose_to_video.utils.prose import read_prose


def align_project(prose_path: Path, audio_path: Path, scenes: list[Scene], db: ProjectDatabase,
                  settings: TranscriptionSettings, provider: TranscriptionProvider | None = None,
                  cancel: Event | None = None,
                  progress: Callable[[str, int, int], None] | None = None) -> AlignmentRun:
    cancel = cancel or Event()
    progress = progress or (lambda *_: None)
    text = read_prose(prose_path)
    prose_digest = source_hash(text)
    audio_digest = source_file_hash(audio_path)
    if scenes:
        validate_partition(text, scenes)
    fingerprint = settings.fingerprint()
    provider = provider or WhisperXProvider()
    cached = db.load_transcript_cache(audio_digest, fingerprint)
    if cached is None or cached[1] == "transcribed":
        raw = cached[0] if cached else None
        def checkpoint(segments, stage):
            db.save_transcript_cache(audio_digest, fingerprint, segments, stage,
                                     provider.alignment_model)
        segments, duration, device, precision = transcribe(audio_path, settings,
            provider, cancel, progress, checkpoint=checkpoint, cached_raw=raw)
    else:
        from prose_to_video.utils.media_info import inspect_audio
        duration = inspect_audio(audio_path).duration
        device, precision = runtime(settings)
        segments = cached[0]
        provider.alignment_model = cached[2]
        progress("Reusing cached transcript", 1, 1)
    if cancel.is_set(): raise AlignmentCanceled("Audio alignment canceled")
    progress("Matching transcript to prose", 0, 1)
    spoken = [word for segment in segments for word in segment.words]
    words, passages, issues = match_words(text, spoken)
    progress("Matching transcript to prose", 1, 1)
    if cancel.is_set(): raise AlignmentCanceled("Audio alignment canceled")
    progress("Calculating sentence and paragraph times", 0, 1)
    sentences, paragraphs = derive_spans(text, words)
    for sentence in sentences:
        if sentence.status == "unresolved" and any(w.source_start >= sentence.source_start and w.source_end <= sentence.source_end for w in words):
            issues.append(AlignmentIssue(kind="unmatched_sentence", message="No verified speech for manuscript sentence",
                                         source_start=sentence.source_start, source_end=sentence.source_end))
    timings = derive_scene_timings(scenes, words)
    progress("Calculating scene timestamps", 1, 1)
    if cancel.is_set(): raise AlignmentCanceled("Audio alignment canceled")
    if source_hash(read_prose(prose_path)) != prose_digest or source_file_hash(audio_path) != audio_digest:
        raise ValueError("Source prose or audio changed during alignment; prior results were preserved")
    run = AlignmentRun(prose_hash=prose_digest, audio_hash=audio_digest,
                       audio_path=str(audio_path.relative_to(db.path.parent)) if audio_path.is_relative_to(db.path.parent)
                                  else str(audio_path), audio_duration=duration,
                       transcription_model=settings.model,
                       alignment_model=provider.alignment_model,
                       device=device, precision=precision, config_fingerprint=fingerprint,
                       transcript=segments, passages=passages, words=words,
                       sentences=sentences, paragraphs=paragraphs, scenes=timings, issues=issues)
    progress("Saving results", 0, 1)
    db.save_alignment(run)
    progress("Saving results", 1, 1)
    return run
