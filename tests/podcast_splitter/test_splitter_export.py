from pathlib import Path

import shutil
import subprocess

import pytest

from podcast_splitter.core import Project, Section, fingerprint
from podcast_splitter.engine import export, probe


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="FFmpeg required")
def test_export_precise_cuts_and_no_overwrite(tmp_path):
    audio = tmp_path / "source.wav"
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "sine=frequency=440:duration=5", str(audio)], check=True)
    output = tmp_path / "output"
    p = Project(audio=str(audio), audio_fingerprint=fingerprint(str(audio)), duration=5, output=str(output),
                sections=[Section("01 Test", "", start=1.125, end=3.625, reviewed=True)])
    export(p, "wav")
    result = output / "01 Test.wav"
    assert probe(str(result)) == pytest.approx(2.5, abs=.001)
    original = result.read_bytes()
    with pytest.raises(FileExistsError):
        export(p, "wav")
    assert result.read_bytes() == original
    assert probe(str(audio)) == 5


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="FFmpeg required")
def test_source_change_rejected(tmp_path):
    p = tmp_path / "audio"
    p.write_bytes(b"changed")
    project = Project(audio=str(p), audio_fingerprint="old", duration=3, output=str(tmp_path / "out"),
                      sections=[Section("Test", "", start=0, end=2, reviewed=True)])
    with pytest.raises(ValueError, match="changed"):
        export(project, "wav")


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="FFmpeg required")
def test_cancel_during_publish_rolls_back(tmp_path, monkeypatch):
    import os
    audio = tmp_path / "source.wav"
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "sine=duration=3", str(audio)], check=True)
    output = tmp_path / "output"
    p = Project(audio=str(audio), audio_fingerprint=fingerprint(str(audio)), duration=3, output=str(output),
                sections=[Section("One", "", start=0, end=1, reviewed=True), Section("Two", "", start=1, end=2, reviewed=True)])
    original_link = os.link
    calls = []
    def interrupted_link(src, dst):
        if len(calls) == 2:
            raise KeyboardInterrupt
        calls.append(dst)
        original_link(src, dst)
    monkeypatch.setattr(os, "link", interrupted_link)
    with pytest.raises(KeyboardInterrupt):
        export(p, "wav")
    assert not list(output.iterdir())


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="FFmpeg required")
def test_widened_export_samples_and_shared_audio(tmp_path):
    import array
    import wave
    rate = 8000
    audio = tmp_path / "source.wav"
    samples = array.array("h", ((i // rate + 1) * 1000 for i in range(rate * 6)))
    with wave.open(str(audio), "wb") as f:
        f.setparams((1, 2, rate, 0, "NONE", "not compressed"))
        f.writeframes(samples.tobytes())
    p = Project(audio=str(audio), audio_fingerprint=fingerprint(str(audio)), duration=6,
                output=str(tmp_path / "out"), part_padding=1,
                sections=[Section("One", "", start=.5, end=3, reviewed=True),
                          Section("Two", "", start=3, end=5.5, reviewed=True)])
    export(p, "wav")
    for name, first, last in [("01 One.wav", 1000, 4000), ("02 Two.wav", 3000, 6000)]:
        with wave.open(str(Path(p.output) / name), "rb") as f:
            data = array.array("h", f.readframes(f.getnframes()))
            assert f.getnframes() / f.getframerate() == 4
            assert (data[0], data[-1]) == (first, last)



@pytest.mark.parametrize("extension", ["mp3", "flac", "wav"])
@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="FFmpeg required")
def test_export_matching_markdown(tmp_path, extension):
    audio = tmp_path / "source.wav"
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "sine=duration=2", str(audio)], check=True)
    output = tmp_path / "out"
    p = Project(audio=str(audio), audio_fingerprint=fingerprint(str(audio)), duration=2,
                output=str(output), sections=[
                    Section("A / title", "First **paragraph**.\n\nCafé narration.", start=0, end=1, reviewed=True),
                    Section("A / title", "Second part.", start=1, end=2, reviewed=True),
                    Section("Excluded", "Not exported.", included=False)])
    names = export(p, extension)
    assert len(names) == 2
    assert len(list(output.iterdir())) == 4
    for section, name in zip(p.sections, names):
        assert (output / name).is_file()
        assert (output / Path(name).with_suffix(".md")).read_text(encoding="utf-8") == f"# {section.title}\n\n{section.text}\n"


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="FFmpeg required")
def test_existing_markdown_prevents_export(tmp_path):
    audio = tmp_path / "source.wav"
    audio.write_bytes(b"source")
    output = tmp_path / "out"
    output.mkdir()
    prose = output / "01 Test.md"
    prose.write_text("Keep my prose", encoding="utf-8")
    p = Project(audio=str(audio), audio_fingerprint=fingerprint(str(audio)), duration=2,
                output=str(output), sections=[Section("Test", "New prose", start=0, end=1, reviewed=True)])
    with pytest.raises(FileExistsError, match="01 Test.md"):
        export(p, "wav")
    assert prose.read_text(encoding="utf-8") == "Keep my prose"
    assert list(output.iterdir()) == [prose]
