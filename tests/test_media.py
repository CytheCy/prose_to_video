from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import Mock

import pytest

from prose_to_video.storage.project_files import ProjectError, create_project, import_audio
from prose_to_video.utils.media_info import (AudioInspectionError, format_duration,
                                             inspect_audio, parse_ffprobe)


PROBE = {
    "streams": [{"codec_type": "video", "codec_name": "cover"},
                {"codec_type": "audio", "codec_name": "flac", "sample_rate": "48000",
                 "channels": 2, "duration": "62.5"}],
    "format": {"duration": "63.0"},
}


def test_parse_audio_metadata_and_duration() -> None:
    info = parse_ffprobe(PROBE)
    assert info.duration == 62.5
    assert info.codec == "flac"
    assert info.sample_rate == 48000
    assert info.channels == 2
    assert format_duration(info.duration) == "00:01:02"
    assert format_duration(3661) == "01:01:01"
    assert format_duration(None) == "Unknown"
    assert parse_ffprobe({"streams": [{"codec_type": "audio"}], "format": {"duration": "4"}}).duration == 4
    with pytest.raises(AudioInspectionError, match="no audio stream"):
        parse_ffprobe({"streams": []})


def test_audio_import_with_mock_ffprobe(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from prose_to_video.utils import media_info

    monkeypatch.setattr(media_info.shutil, "which", lambda command: "/usr/bin/ffprobe")
    monkeypatch.setattr(media_info.subprocess, "run", lambda *args, **kwargs: Mock(returncode=0, stdout=json.dumps(PROBE), stderr=""))
    source = tmp_path / "narration.flac"
    source.write_bytes(b"sample audio bytes")
    session = import_audio(create_project(tmp_path, "Story"), source)
    reopened = session.database.load()
    assert reopened.audio_path == "source/narration.flac"
    assert reopened.audio_duration == 62.5
    assert reopened.audio_codec == "flac"
    assert reopened.audio_sample_rate == 48000
    assert reopened.audio_channels == 2
    assert session.file_status() == (False, True)


def test_missing_ffprobe_and_invalid_audio(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from prose_to_video.utils import media_info

    source = tmp_path / "narration.mp3"
    source.write_bytes(b"not audio")
    session = create_project(tmp_path, "Story")
    monkeypatch.setattr(media_info.shutil, "which", lambda command: None)
    with pytest.raises(ProjectError, match="FFprobe is missing"):
        import_audio(session, source)
    assert list((session.root / "source").iterdir()) == []
    monkeypatch.setattr(media_info.shutil, "which", lambda command: "/usr/bin/ffprobe")
    monkeypatch.setattr(media_info.subprocess, "run", lambda *args, **kwargs: Mock(returncode=1, stdout="", stderr="Invalid data"))
    with pytest.raises(ProjectError, match="Invalid or unsupported"):
        import_audio(session, source)
    assert session.database.load().audio_path is None


def test_inspection_rejects_bad_json(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from prose_to_video.utils import media_info

    monkeypatch.setattr(media_info.shutil, "which", lambda command: "/usr/bin/ffprobe")
    monkeypatch.setattr(media_info.subprocess, "run", lambda *args, **kwargs: Mock(returncode=0, stdout="bad", stderr=""))
    with pytest.raises(AudioInspectionError, match="invalid metadata"):
        inspect_audio(tmp_path / "audio.wav")
