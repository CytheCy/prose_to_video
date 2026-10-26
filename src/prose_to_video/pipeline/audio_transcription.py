"""Chunked FFmpeg decoding and replaceable local transcription provider."""
from __future__ import annotations

import hashlib
import json
import math
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from threading import Event
from typing import Callable, Protocol

from prose_to_video.models.alignment import TranscriptSegment, TranscriptWord
from prose_to_video.utils.media_info import inspect_audio


class AlignmentCanceled(Exception):
    pass


@dataclass(frozen=True)
class TranscriptionSettings:
    model: str = "tiny"
    device: str = "auto"
    precision: str = "auto"
    batch_size: int = 4
    chunk_seconds: int = 300
    language: str | None = None
    allow_download: bool = False

    def validate(self) -> None:
        if not self.model.strip() or self.device not in {"auto", "cpu", "cuda"}:
            raise ValueError("Select a model and a supported device: auto, cpu, or cuda")
        if self.precision not in {"auto", "int8", "float16", "float32"}:
            raise ValueError("Unsupported precision")
        if not 1 <= self.batch_size <= 32 or not 30 <= self.chunk_seconds <= 900:
            raise ValueError("Invalid batch size or chunk duration")

    def fingerprint(self) -> str:
        resolved_device, resolved_precision = runtime(self)
        data = {"model": self.model, "language": self.language, "chunk_seconds": self.chunk_seconds,
                "batch_size": self.batch_size, "device": resolved_device,
                "precision": resolved_precision}
        return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()


def source_file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def runtime(settings: TranscriptionSettings) -> tuple[str, str]:
    settings.validate()
    try:
        import torch
    except ImportError:
        if settings.device == "cuda":
            raise ValueError("PyTorch is required for CUDA. Install the audio optional dependencies.")
        if settings.precision == "float16":
            raise ValueError("float16 is unsupported on the CPU; choose int8 or float32")
        return "cpu", "int8" if settings.precision == "auto" else settings.precision
    cuda = torch.cuda.is_available()
    if settings.device == "cuda" and not cuda:
        raise ValueError("CUDA was selected but PyTorch reports no compatible GPU")
    device = "cuda" if cuda and settings.device == "auto" else "cpu" if settings.device == "auto" else settings.device
    precision = ("float16" if device == "cuda" else "int8") if settings.precision == "auto" else settings.precision
    if device == "cpu" and precision == "float16":
        raise ValueError("float16 is unsupported on the CPU; choose int8 or float32")
    return device, precision


def dependency_check() -> None:
    import importlib.util
    from shutil import which
    if not which("ffmpeg") or not which("ffprobe"):
        raise ValueError("FFmpeg and FFprobe are required; install Fedora ffmpeg-free")
    if not importlib.util.find_spec("torch") or not importlib.util.find_spec("whisperx"):
        raise ValueError("PyTorch and WhisperX are required. Install the audio optional dependencies; see README.")


def decode_chunk(source: Path, output: Path, start: float, duration: float) -> None:
    """Accurate input seek followed by PCM conversion; no silence trimming or time scaling."""
    command = ["ffmpeg", "-nostdin", "-v", "error", "-y", "-i", str(source),
               "-ss", f"{start:.6f}", "-t", f"{duration:.6f}", "-ac", "1", "-ar", "16000",
               "-c:a", "pcm_s16le", str(output)]
    result = subprocess.run(command, capture_output=True, text=True)
    if result.returncode or not output.is_file():
        raise ValueError(f"FFmpeg audio conversion failed: {result.stderr.strip()[:500]}")


class TranscriptionProvider(Protocol):
    alignment_model: str
    def load(self, settings: TranscriptionSettings, device: str, precision: str) -> None: ...
    def transcribe_chunk(self, path: Path, offset: float, settings: TranscriptionSettings) -> list[TranscriptSegment]: ...
    def align_chunk(self, path: Path, offset: float, raw: list[TranscriptSegment]) -> list[TranscriptSegment]: ...


class WhisperXProvider:
    """Loads models only after the GUI has explained possible downloads."""
    alignment_model = "WhisperX language default"

    def load(self, settings: TranscriptionSettings, device: str, precision: str) -> None:
        dependency_check()
        import whisperx
        self.whisperx = whisperx
        self.device = device
        self.allow_download = settings.allow_download
        if not settings.allow_download:
            import torch
            vad_cache = Path(torch.hub.get_dir()) / "snakers4_silero-vad_master"
            if not vad_cache.exists():
                raise ValueError("Silero speech detector is not cached. Allow first-time model downloads.")
        try:
            self.model = whisperx.load_model(settings.model, device, compute_type=precision,
                                             language=settings.language, vad_method="silero",
                                             local_files_only=not settings.allow_download)
        except Exception as exc:
            if "out of memory" in str(exc).lower():
                raise ValueError("Model ran out of memory. Try a smaller model or CPU mode.") from exc
            raise ValueError(f"Cannot load transcription model {settings.model}: {exc}. "
                             "Enable model downloads in the confirmation dialog if the model is absent.") from exc
        self.align_models = {}
        self.align_model_names = {}

    def transcribe_chunk(self, path: Path, offset: float, settings: TranscriptionSettings) -> list[TranscriptSegment]:
        try:
            audio = self.whisperx.load_audio(str(path))
            result = self.model.transcribe(audio, batch_size=settings.batch_size, language=settings.language)
            language = result.get("language") or settings.language
            if not language and result.get("segments"):
                raise ValueError("WhisperX did not detect a language")
            return [TranscriptSegment(start=offset + float(s["start"]),
                       end=offset + float(s["end"]), text=s.get("text", ""), language=language,
                       words=[TranscriptWord(text=w) for w in s.get("text", "").split()])
                    for s in result.get("segments", [])]
        except (MemoryError, RuntimeError) as exc:
            if "memory" in str(exc).lower() or "out of" in str(exc).lower():
                raise ValueError("Model ran out of memory. Use a smaller model, batch size, or CPU mode.") from exc
            raise

    def align_chunk(self, path: Path, offset: float, raw: list[TranscriptSegment]) -> list[TranscriptSegment]:
        if not raw:
            return []
        language = raw[0].language
        if not language:
            raise ValueError("WhisperX did not detect a language for this speech")
        try:
            audio = self.whisperx.load_audio(str(path))
            if language not in self.align_models:
                try:
                    import nltk
                    from whisperx.utils import PUNKT_LANGUAGES
                    punkt = PUNKT_LANGUAGES.get(language, "english")
                    if not self.allow_download:
                        nltk.data.find(f"tokenizers/punkt_tab/{punkt}/")
                    model_name = None
                    if not self.allow_download:
                        from whisperx.alignment import DEFAULT_ALIGN_MODELS_TORCH, TORCHAUDIO_PIPELINE_TO_HF
                        model_name = TORCHAUDIO_PIPELINE_TO_HF.get(DEFAULT_ALIGN_MODELS_TORCH.get(language))
                    if model_name is None:
                        from whisperx.alignment import DEFAULT_ALIGN_MODELS_TORCH, DEFAULT_ALIGN_MODELS_HF
                        model_name = DEFAULT_ALIGN_MODELS_TORCH.get(language) or DEFAULT_ALIGN_MODELS_HF.get(language)
                    if model_name is None:
                        raise ValueError(f"No default alignment model for language {language}")
                    self.align_models[language] = self.whisperx.load_align_model(
                        language_code=language, device=self.device, model_name=model_name,
                        model_cache_only=not self.allow_download)
                    self.align_model_names[language] = model_name
                except Exception as exc:
                    if "out of memory" in str(exc).lower():
                        raise ValueError("Alignment model ran out of memory. Try CPU mode.") from exc
                    raise ValueError(f"No available WhisperX alignment model for language {language}: {exc}") from exc
            model, metadata = self.align_models[language]
            self.alignment_model = ", ".join(sorted(set(self.align_model_names.values())))
            local = [{"start": s.start - offset, "end": s.end - offset, "text": s.text} for s in raw]
            aligned = self.whisperx.align(local, model, metadata, audio, self.device,
                                          interpolate_method="ignore", return_char_alignments=False)
        except (MemoryError, RuntimeError) as exc:
            if "memory" in str(exc).lower() or "out of" in str(exc).lower():
                raise ValueError("Model ran out of memory. Use a smaller model, batch size, or CPU mode.") from exc
            raise
        segments = []
        for raw in aligned.get("segments", []):
            words = [TranscriptWord(text=w.get("word", ""),
                                    start=offset + float(w["start"]) if "start" in w else None,
                                    end=offset + float(w["end"]) if "end" in w else None,
                                    score=w.get("score")) for w in raw.get("words", []) if w.get("word", "").strip()]
            segments.append(TranscriptSegment(start=offset + float(raw["start"]),
                                              end=offset + float(raw["end"]),
                                              text=raw.get("text", ""), language=language, words=words))
        return segments


def transcribe(source: Path, settings: TranscriptionSettings, provider: TranscriptionProvider,
               cancel: Event | None = None, progress: Callable[[str, int, int], None] | None = None,
               checkpoint: Callable[[list[TranscriptSegment], str], None] | None = None,
               cached_raw: list[TranscriptSegment] | None = None) -> tuple[list[TranscriptSegment], float, str, str]:
    cancel = cancel or Event()
    progress = progress or (lambda *_: None)
    dependency_check() if isinstance(provider, WhisperXProvider) else None
    duration = inspect_audio(source).duration
    if duration is None or duration <= 0:
        raise ValueError("Audio duration is unavailable")
    device, precision = runtime(settings)
    progress("Loading transcription model", 0, 1)
    provider.load(settings, device, precision)
    progress("Loading transcription model", 1, 1)
    segments: list[TranscriptSegment] = []
    total = math.ceil(duration / settings.chunk_seconds)
    with tempfile.TemporaryDirectory(prefix="prose-audio-") as folder:
        def chunk(i: int) -> tuple[Path, float]:
            start = i * settings.chunk_seconds
            path = Path(folder) / "chunk.wav"
            progress("Preparing audio", i + 1, total)
            decode_chunk(source, path, start, min(settings.chunk_seconds, duration - start))
            return path, start

        if hasattr(provider, "transcribe_chunk") and hasattr(provider, "align_chunk"):
            raw = cached_raw
            if raw is None:
                raw = []
                for i in range(total):
                    if cancel.is_set(): raise AlignmentCanceled("Audio alignment canceled")
                    path, start = chunk(i)
                    progress("Transcribing narration", i + 1, total)
                    raw.extend(provider.transcribe_chunk(path, start, settings))
                    path.unlink(missing_ok=True)
                if checkpoint:
                    checkpoint(raw, "transcribed")
            for i in range(total):
                if cancel.is_set(): raise AlignmentCanceled("Audio alignment canceled")
                path, start = chunk(i)
                progress("Aligning words", i + 1, total)
                in_chunk = [s for s in raw if start <= s.start < start + settings.chunk_seconds]
                segments.extend(provider.align_chunk(path, start, in_chunk))
                path.unlink(missing_ok=True)
        else:
            for i in range(total):
                if cancel.is_set(): raise AlignmentCanceled("Audio alignment canceled")
                path, start = chunk(i)
                progress("Transcribing and aligning speech", i + 1, total)
                segments.extend(provider.process_chunk(path, start, settings))
                path.unlink(missing_ok=True)
    if cancel.is_set(): raise AlignmentCanceled("Audio alignment canceled")
    if checkpoint:
        checkpoint(segments, "aligned")
    return segments, duration, device, precision
