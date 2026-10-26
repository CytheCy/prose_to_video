"""Stage Three tests use a deterministic provider; no model, API, or GPU is required."""
from __future__ import annotations

import json
import os
import sqlite3
import struct
import time
import wave
from pathlib import Path
from threading import Event

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtWidgets import QApplication

from prose_to_video.gui.main_window import MainWindow
from prose_to_video.models.alignment import AlignmentRun, TranscriptSegment, TranscriptWord, WordAlignment
from prose_to_video.models.scene import Scene, source_hash
from prose_to_video.pipeline.audio_alignment import align_project
from prose_to_video.pipeline.audio_transcription import (AlignmentCanceled, TranscriptionSettings,
    decode_chunk, runtime, source_file_hash)
from prose_to_video.pipeline.text_matching import (derive_scene_timings, derive_spans, match_words,
    normalize, paragraph_ranges, sentence_ranges)
from prose_to_video.storage.project_files import create_project, import_audio, import_prose
from prose_to_video.storage.scene_export import export_json, export_markdown


TEXT = "😀 First sentence.\r\n\r\nSecond sentence.\r\n\r\nThird sentence."


def audio_file(path: Path) -> Path:
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(16000)
        # Three seconds of silence; timestamps remain on the original timeline.
        output.writeframes(b"\0\0" * 16000 * 3)
        output.writeframes(b"".join(struct.pack("<h", 5000 if i % 20 < 10 else -5000)
                                    for i in range(16000 * 3)))
    return path


class MockProvider:
    alignment_model = "mock-word-aligner"
    calls = 0

    def load(self, settings, device, precision):
        assert device == "cpu"
        assert precision == "int8"

    def process_chunk(self, path, offset, settings):
        self.calls += 1
        assert path.is_file()
        return [TranscriptSegment(start=offset + 3, end=offset + 5.5,
            text="First sentence Third sentence",
            words=[TranscriptWord(text=word, start=offset + start, end=offset + start + .2)
                   for word, start in (("First", 3.1), ("sentence", 3.4),
                                       ("Third", 4.7), ("sentence", 5.0))])]


def project_with_sources(tmp_path):
    source = tmp_path / "story.txt"
    source.write_bytes(TEXT.encode())
    audio = audio_file(tmp_path / "voice.wav")
    session = import_audio(import_prose(create_project(tmp_path, "Story"), source), audio)
    cut = TEXT.index("Third")
    scenes = [Scene(sequence=1, start=0, end=cut, text=TEXT[:cut]),
              Scene(sequence=2, start=cut, end=len(TEXT), text=TEXT[cut:])]
    session.database.replace_scenes(TEXT, scenes)
    return session, scenes


def run_mock(session, scenes):
    return align_project(session.project.resolved_prose(session.root),
        session.project.resolved_audio(session.root), scenes, session.database,
        TranscriptionSettings(), provider=MockProvider())


def test_decode_preserves_leading_silence_and_timeline(tmp_path):
    source = audio_file(tmp_path / "voice.wav")
    converted = tmp_path / "work.wav"
    decode_chunk(source, converted, 0, 6)
    with wave.open(str(converted), "rb") as stream:
        assert stream.getframerate() == 16000
        assert stream.getnchannels() == 1
        assert stream.getnframes() == 96000
        assert set(stream.readframes(16000 * 3)) == {0}
    assert source.stat().st_size > 0
    assert runtime(TranscriptionSettings(device="cpu")) == ("cpu", "int8")
    with pytest.raises(ValueError, match="float16"):
        runtime(TranscriptionSettings(device="cpu", precision="float16"))


def test_matching_repeats_skips_inserts_and_unicode_offsets():
    text = "😀 First sentence. Second sentence. Third sentence. Repeat me. Repeat me."
    speech = "Intro First sentence Third sentence Repeat me Repeat me Again".split()
    spoken = [TranscriptWord(text=w, start=i * .5, end=i * .5 + .2) for i, w in enumerate(speech)]
    words, passages, issues = match_words(text, spoken)
    assert [w.text for w in words if w.status == "unmatched"] == ["Second", "sentence"]
    repeats = [w for w in words if w.text == "Repeat"]
    assert repeats[0].audio_start < repeats[1].audio_start
    assert words[0].source_start == 2
    assert any(issue.kind == "spoken_insertion" and "Intro" in issue.message for issue in issues)
    assert any("Again" in issue.message for issue in issues)
    assert passages
    assert normalize("DON’T") == "dont"
    assert normalize("three") == "3"


def test_segmentation_offsets_and_unresolved_sentences():
    text = "# Heading\r\nDr. Ada waited... Then left!\r\n\r\n😀 Final."
    paragraphs = paragraph_ranges(text)
    assert text[slice(*paragraphs[0])] == "# Heading"
    assert text[slice(*paragraphs[1])].startswith("Dr. Ada")
    sentences = sentence_ranges(text)
    assert [text[a:b] for a, b in sentences] == ["# Heading", "Dr. Ada waited...", "Then left!", "😀 Final."]
    spoken = [TranscriptWord(text="Ada", start=1, end=1.2)]
    words, _, _ = match_words(text, spoken)
    sentence_spans, paragraph_spans = derive_spans(text, words)
    assert sentence_spans[0].status == "unresolved"
    assert sentence_spans[-1].audio_start is None
    assert paragraph_spans[1].coverage < 1
    assert ["U.S. Army arrived.", "Then left."] == [
        "U.S. Army arrived. Then left."[a:b] for a, b in sentence_ranges("U.S. Army arrived. Then left.")]


def test_run_persistence_scene_pauses_manual_correction_and_exports(tmp_path):
    session, scenes = project_with_sources(tmp_path)
    run = run_mock(session, scenes)
    assert len(run.words) == 6
    assert [w.text for w in run.words if w.status == "unmatched"] == ["Second", "sentence"]
    assert any(issue.kind == "unmatched_sentence" for issue in run.issues)
    assert run.scenes[0].status == "partial"
    assert run.scenes[0].audio_start == pytest.approx(3.1)
    assert run.scenes[1].audio_start == pytest.approx(4.7)
    assert run.scenes[1].audio_start > run.scenes[0].audio_end
    db = session.database
    loaded = db.load_alignment(source_hash(TEXT), run.audio_hash)
    assert loaded.identifier == run.identifier
    assert len(db.words_for_scene(scenes[1], run.prose_hash, run.audio_hash)) == 2
    assert db.prose_at_audio(3.15, run.prose_hash, run.audio_hash) == (0, 17)
    assert db.audio_for_prose(scenes[1].start, scenes[1].end, run.prose_hash, run.audio_hash) == pytest.approx((4.7, 5.2))
    with pytest.raises(ValueError):
        db.correct_scene_time(scenes[1].identifier, 5, 4, run.prose_hash, run.audio_hash)
    corrected = db.correct_scene_time(scenes[1].identifier, 4.5, 5.5, run.prose_hash, run.audio_hash)
    assert corrected.manual and corrected.status == "manual"
    db.replace_scenes(TEXT, scenes)
    assert db.load_alignment().scenes[1].manual
    json_path, md_path = tmp_path / "export.json", tmp_path / "export.md"
    current = db.load_alignment()
    export_json(json_path, TEXT, scenes, current, detailed=True)
    export_markdown(md_path, TEXT, scenes, current)
    data = json.loads(json_path.read_text())
    assert data["scenes"][1]["audio_start"] == 4.5
    assert data["scenes"][1]["duration"] == 1.0
    assert len(data["words"]) == 6
    assert "00:00:04" in md_path.read_text()
    assert db.load_alignment(source_hash(TEXT + "changed"), run.audio_hash) is None
    assert db.load_alignment(run.prose_hash, "changed") is None
    middle = TEXT.index("Second")
    revised = [scenes[0].model_copy(update={"end": middle, "text": TEXT[:middle]}),
               Scene(sequence=2, start=middle, end=scenes[1].start,
                     text=TEXT[middle:scenes[1].start]),
               scenes[1].model_copy(update={"sequence": 3})]
    db.replace_scenes(TEXT, revised)
    retimed = db.load_alignment().scenes
    assert retimed[0].audio_end == pytest.approx(3.6)
    assert retimed[1].status == "unresolved" and retimed[1].audio_start is None
    assert retimed[2].manual


def test_unmatched_scene_and_validation():
    text = "Read this.\n\nSkipped entirely."
    words, _, _ = match_words(text, [TranscriptWord(text=w, start=i, end=i + .2)
                                     for i, w in enumerate(["Read", "this"])])
    cut = text.index("Skipped")
    scenes = [Scene(sequence=1, start=0, end=cut, text=text[:cut]),
              Scene(sequence=2, start=cut, end=len(text), text=text[cut:])]
    timings = derive_scene_timings(scenes, words)
    assert timings[1].status == "unresolved" and timings[1].audio_start is None
    with pytest.raises(ValueError, match="out of order"):
        AlignmentRun(prose_hash="x", audio_hash="y", audio_path="voice.wav", audio_duration=2,
                     transcription_model="mock", device="cpu", precision="int8",
                     config_fingerprint="x", words=[
                         WordAlignment(source_start=0, source_end=1, text="a", recognized="a",
                                       audio_start=1, audio_end=1.2, status="matched"),
                         WordAlignment(source_start=2, source_end=3, text="b", recognized="b",
                                       audio_start=.5, audio_end=.6, status="matched")])


def test_failed_run_cancellation_and_transaction_rollback(tmp_path, monkeypatch):
    session, scenes = project_with_sources(tmp_path)
    old = run_mock(session, scenes)
    canceled = Event(); canceled.set()
    with pytest.raises(AlignmentCanceled):
        align_project(session.project.resolved_prose(session.root),
                      session.project.resolved_audio(session.root), scenes, session.database,
                      TranscriptionSettings(model="base"), provider=MockProvider(), cancel=canceled)
    assert session.database.load_alignment().identifier == old.identifier
    db = session.database
    original_write = db._write_run
    def fail_after_write(connection, run, activate):
        original_write(connection, run, activate)
        raise sqlite3.IntegrityError("simulated failure")
    monkeypatch.setattr(db, "_write_run", fail_after_write)
    with pytest.raises(Exception):
        db.save_alignment(old.model_copy(update={"identifier": "replacement"}))
    assert session.database.load_alignment().identifier == old.identifier
    with sqlite3.connect(session.database.path) as connection:
        assert not connection.execute("SELECT 1 FROM alignment_run WHERE identifier='replacement'").fetchone()


def test_completed_transcript_survives_forced_alignment_failure(tmp_path):
    session, scenes = project_with_sources(tmp_path)
    class TwoPhase:
        alignment_model = "mock-aligner"
        transcripts = 0
        fail = True
        def load(self, settings, device, precision):
            pass
        def transcribe_chunk(self, path, offset, settings):
            self.transcripts += 1
            return [TranscriptSegment(start=3, end=5.5, text="First sentence Third sentence",
                language="en", words=[TranscriptWord(text=w) for w in
                                      ("First", "sentence", "Third", "sentence")])]
        def align_chunk(self, path, offset, raw):
            if self.fail:
                raise ValueError("alignment model unavailable")
            return [TranscriptSegment(start=3, end=5.5, text=raw[0].text, language="en",
                words=[TranscriptWord(text=w, start=t, end=t + .2) for w, t in
                       (("First", 3.1), ("sentence", 3.4), ("Third", 4.7), ("sentence", 5.0))])]
    provider = TwoPhase()
    settings = TranscriptionSettings(model="base")
    args = (session.project.resolved_prose(session.root),
            session.project.resolved_audio(session.root), scenes, session.database, settings)
    with pytest.raises(ValueError, match="unavailable"):
        align_project(*args, provider=provider)
    cache = session.database.load_transcript_cache(source_file_hash(args[1]), settings.fingerprint())
    assert cache[1] == "transcribed"
    assert session.database.load_alignment() is None
    provider.fail = False
    run = align_project(*args, provider=provider)
    assert provider.transcripts == 1
    assert run.scenes[1].audio_start == pytest.approx(4.7)


def test_migrate_v2_without_losing_scenes(tmp_path):
    session, scenes = project_with_sources(tmp_path)
    with sqlite3.connect(session.database.path) as connection:
        for table in ("active_alignment", "alignment_word", "scene_timing", "transcript_cache", "alignment_run"):
            connection.execute(f"DROP TABLE {table}")
        connection.execute("PRAGMA user_version=2")
    assert len(session.database.load_scenes()[1]) == 2
    with sqlite3.connect(session.database.path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 4


def test_gui_scene_seek_and_prose_highlight(tmp_path):
    app = QApplication.instance() or QApplication([])
    session, scenes = project_with_sources(tmp_path)
    run_mock(session, scenes)
    window = MainWindow()
    window._set_session(session)
    deadline = time.monotonic() + 8
    while (window._task or window._waveform_task or not window._prose_ready) and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(.01)
    assert window._alignment is not None
    assert window.scenes_panel.play_scene_button.isEnabled()
    played = []
    window.audio_player.play_range = lambda start, end: played.append((start, end))
    window._play_scene()
    assert played[0] == pytest.approx((3.1, 3.6))
    window._audio_position_changed(3.2)
    selections = window.prose_viewer.extraSelections()
    assert selections and selections[0].cursor.selectedText().startswith("😀 First")
    window.scenes_panel.list_widget.setCurrentRow(1)
    app.processEvents()
    window._play_scene()
    assert played[1] == pytest.approx((4.7, 5.2))
    window.close()


def test_gui_align_button_uses_background_worker(tmp_path, monkeypatch):
    import prose_to_video.gui.main_window as gui
    app = QApplication.instance() or QApplication([])
    session, scenes = project_with_sources(tmp_path)
    window = MainWindow()
    window._set_session(session)
    deadline = time.monotonic() + 8
    while (window._task or not window._prose_ready) and time.monotonic() < deadline:
        app.processEvents(); time.sleep(.01)

    class SettingsDialog:
        Accepted = 1
        def __init__(self, parent): pass
        def exec(self): return self.Accepted
        def values(self, allow_download): return TranscriptionSettings()

    actual = align_project
    monkeypatch.setattr(gui, "dependency_check", lambda: None)
    monkeypatch.setattr(gui, "AlignmentSettingsDialog", SettingsDialog)
    monkeypatch.setattr(gui.QMessageBox, "question", lambda *args: gui.QMessageBox.No)
    monkeypatch.setattr(gui, "align_project", lambda *args, **kwargs: actual(*args, provider=MockProvider(), **kwargs))
    window.scenes_panel.align_button.click()
    assert window._alignment_task is not None
    deadline = time.monotonic() + 8
    while window._alignment_task and time.monotonic() < deadline:
        app.processEvents(); time.sleep(.01)
    assert window._alignment_task is None
    assert session.database.load_alignment() is not None
    assert window.scenes_panel.play_scene_button.isEnabled()
    window.close()
