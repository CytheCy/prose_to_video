"""Source-owned alignment records. Offsets are Python character offsets; times are seconds."""
from __future__ import annotations

import math
from uuid import uuid4
from pydantic import BaseModel, ConfigDict, Field, model_validator


class Record(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TranscriptWord(Record):
    text: str
    start: float | None = None
    end: float | None = None
    score: float | None = None

    @model_validator(mode="after")
    def valid_time(self):
        if (self.start is None) != (self.end is None):
            raise ValueError("Transcript word needs both timestamps or neither")
        if self.start is not None and (not math.isfinite(self.start) or not math.isfinite(self.end)
                                       or self.start < 0 or self.end < self.start):
            raise ValueError("Invalid transcript word time")
        return self


class TranscriptSegment(Record):
    identifier: str = Field(default_factory=lambda: str(uuid4()))
    start: float = Field(ge=0)
    end: float = Field(ge=0)
    text: str
    language: str | None = None
    words: list[TranscriptWord] = Field(default_factory=list)

    @model_validator(mode="after")
    def valid_range(self):
        if not math.isfinite(self.start) or not math.isfinite(self.end) or self.end < self.start:
            raise ValueError("Invalid transcript segment range")
        return self


class TextPassage(Record):
    identifier: str = Field(default_factory=lambda: str(uuid4()))
    source_start: int = Field(ge=0)
    source_end: int = Field(ge=0)
    transcript_start: int = Field(ge=0)
    transcript_end: int = Field(ge=0)
    matched_words: int = Field(ge=0)


class WordAlignment(Record):
    identifier: str = Field(default_factory=lambda: str(uuid4()))
    source_start: int = Field(ge=0)
    source_end: int = Field(gt=0)
    text: str
    recognized: str | None = None
    audio_start: float | None = None
    audio_end: float | None = None
    status: str = "unmatched"
    confidence: float | None = None

    @model_validator(mode="after")
    def valid(self):
        if self.source_end <= self.source_start:
            raise ValueError("Invalid word source range")
        if (self.audio_start is None) != (self.audio_end is None):
            raise ValueError("Word needs both timestamps or neither")
        if self.audio_start is not None:
            if (not math.isfinite(self.audio_start) or not math.isfinite(self.audio_end)
                    or self.audio_start < 0 or self.audio_end <= self.audio_start or self.status != "matched"):
                raise ValueError("Invalid word timing")
        elif self.status == "matched":
            raise ValueError("Matched word has no verified timing")
        return self


class SpanAlignment(Record):
    identifier: str = Field(default_factory=lambda: str(uuid4()))
    source_start: int = Field(ge=0)
    source_end: int = Field(gt=0)
    audio_start: float | None = None
    audio_end: float | None = None
    matched_words: int = Field(ge=0)
    unmatched_words: int = Field(ge=0)
    coverage: float = Field(ge=0, le=1)
    status: str = "unresolved"

    @model_validator(mode="after")
    def valid_range(self):
        if self.source_end <= self.source_start:
            raise ValueError("Invalid span source range")
        return self


class SceneTiming(SpanAlignment):
    scene_id: str
    manual: bool = False
    warnings: list[str] = Field(default_factory=list)

    @property
    def duration(self) -> float | None:
        return self.audio_end - self.audio_start if self.audio_start is not None and self.audio_end is not None else None


class AlignmentIssue(Record):
    kind: str
    message: str
    source_start: int | None = None
    source_end: int | None = None
    audio_start: float | None = None
    audio_end: float | None = None


class AlignmentRun(Record):
    identifier: str = Field(default_factory=lambda: str(uuid4()))
    prose_hash: str
    audio_hash: str
    audio_path: str
    audio_duration: float = Field(gt=0)
    processing_format: str = "mono PCM s16le"
    sample_rate: int = 16000
    transcription_model: str
    alignment_model: str = "WhisperX language default"
    device: str
    precision: str
    config_fingerprint: str
    status: str = "complete"
    transcript: list[TranscriptSegment] = Field(default_factory=list)
    passages: list[TextPassage] = Field(default_factory=list)
    words: list[WordAlignment] = Field(default_factory=list)
    sentences: list[SpanAlignment] = Field(default_factory=list)
    paragraphs: list[SpanAlignment] = Field(default_factory=list)
    scenes: list[SceneTiming] = Field(default_factory=list)
    issues: list[AlignmentIssue] = Field(default_factory=list)

    @model_validator(mode="after")
    def valid(self):
        previous = 0.0
        for word in self.words:
            if word.audio_start is None:
                continue
            if word.audio_start + 0.02 < previous or word.audio_end > self.audio_duration + 0.05:
                raise ValueError("Word timestamps are out of order or beyond audio duration")
            previous = max(previous, word.audio_end)
        for span in [*self.sentences, *self.paragraphs, *self.scenes]:
            if (span.audio_start is None) != (span.audio_end is None):
                raise ValueError("Span has only one boundary")
            if span.audio_start is not None and (span.audio_start < 0 or span.audio_end <= span.audio_start
                                                 or span.audio_end > self.audio_duration + 0.05):
                raise ValueError("Invalid span timing")
        return self
