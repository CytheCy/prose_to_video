"""Inspect an audio file with FFprobe; no Qt dependency."""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class AudioInspectionError(Exception):
    """Audio cannot be inspected or contains no usable audio stream."""


@dataclass(frozen=True)
class AudioInfo:
    duration: float | None
    codec: str | None
    sample_rate: int | None
    channels: int | None


def _positive_float(value: Any) -> float | None:
    try:
        number = float(value)
        return number if 0 <= number < float("inf") else None
    except (ValueError, TypeError):
        return None


def _positive_int(value: Any) -> int | None:
    try:
        number = int(value)
        return number if number > 0 else None
    except (ValueError, TypeError):
        return None


def parse_ffprobe(data: dict[str, Any]) -> AudioInfo:
    if not isinstance(data, dict):
        raise AudioInspectionError("FFprobe returned invalid metadata")
    streams = data.get("streams", [])
    if not isinstance(streams, list):
        raise AudioInspectionError("FFprobe returned invalid stream information")
    audio = next((stream for stream in streams if isinstance(stream, dict) and stream.get("codec_type") == "audio"), None)
    if audio is None:
        raise AudioInspectionError("The selected file contains no audio stream")
    container = data.get("format", {})
    container_duration = container.get("duration") if isinstance(container, dict) else None
    return AudioInfo(
        duration=_positive_float(audio.get("duration")) or _positive_float(container_duration),
        codec=audio.get("codec_name") if isinstance(audio.get("codec_name"), str) else None,
        sample_rate=_positive_int(audio.get("sample_rate")),
        channels=_positive_int(audio.get("channels")),
    )


def inspect_audio(path: Path) -> AudioInfo:
    if shutil.which("ffprobe") is None:
        raise AudioInspectionError("FFprobe is missing. Install the Fedora ffmpeg-free package.")
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)],
            capture_output=True, text=True, timeout=30, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise AudioInspectionError(f"FFprobe could not inspect the audio file: {exc}") from exc
    if result.returncode != 0:
        detail = result.stderr.strip().splitlines()[-1] if result.stderr.strip() else "unknown format or damaged file"
        raise AudioInspectionError(f"Invalid or unsupported audio file: {detail}")
    try:
        return parse_ffprobe(json.loads(result.stdout))
    except (ValueError, TypeError) as exc:
        raise AudioInspectionError("FFprobe returned invalid metadata") from exc


def format_duration(seconds: float | None) -> str:
    if seconds is None:
        return "Unknown"
    total = max(0, int(seconds))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours:02}:{minutes:02}:{secs:02}"
