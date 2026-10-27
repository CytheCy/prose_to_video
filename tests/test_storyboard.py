"""Stage Four tests: deterministic AI proposals, no network or model key."""
from __future__ import annotations
import json
import os
import re
import sqlite3
import time
import wave
from pathlib import Path
from threading import Event
import pytest
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtWidgets import QApplication
from PySide6.QtGui import QTextCursor
from prose_to_video.models.scene import Scene, source_hash
from prose_to_video.models.alignment import AlignmentRun, SceneTiming, WordAlignment
from prose_to_video.models.shot import StoryboardSettings
from prose_to_video.pipeline.shot_planning import ShotIdea, ShotIdeas, PlanningCanceled, plan_scene
from prose_to_video.pipeline.shot_editing import split_shot, merge_shots, delete_shot, update_shot, swap_visuals_with_next
from prose_to_video.storage.project_files import create_project, import_prose, import_audio
from prose_to_video.pipeline.audio_transcription import source_file_hash
from prose_to_video.storage.storyboard_export import export_storyboard_json, export_storyboard_markdown
from prose_to_video.gui.main_window import MainWindow

TEXT = "Ada waits. Ben enters. Ada smiles."

class FakeProvider:
    def __init__(self, starts=(0,1,2)):
        self.starts = starts
        self.calls = 0
    def propose_shots(self, payload):
        self.calls += 1
        valid = json.loads(payload)["valid_ids"]
        return ShotIdeas(shots=[ShotIdea(start_id=i, title=f"Moment {i}", shot_type="Medium",
            foreground="Ada", background="the room", visual_description=f"Ada and Ben at narrative moment {i}.")
            for i in self.starts if i in valid])

def fixture_data(text=TEXT, missing=False):
    scene = Scene(sequence=1, start=0, end=len(text), text=text, title="Arrival")
    words = []
    for i, match in enumerate(re.finditer(r"\b[A-Za-z]+\b", text)):
        start = float(i + 1)
        if i >= 4: start += 2
        timed = not (missing and i == 2)
        words.append(WordAlignment(source_start=match.start(), source_end=match.end(), text=match.group(),
            recognized=match.group() if timed else None, audio_start=start if timed else None,
            audio_end=start+.4 if timed else None, status="matched" if timed else "unmatched"))
    run = AlignmentRun(prose_hash=source_hash(text), audio_hash="audio", audio_path="voice.wav",
        audio_duration=20, transcription_model="mock", device="cpu", precision="int8",
        config_fingerprint="mock", words=words, scenes=[SceneTiming(scene_id=scene.identifier,
            source_start=0, source_end=len(text), audio_start=1, audio_end=words[-1].audio_end,
            matched_words=len(words)-int(missing), unmatched_words=int(missing),
            coverage=1-int(missing)/len(words), status="partial" if missing else "timed")])
    return scene, run

def test_settings_and_shot_timeline():
    assert StoryboardSettings(density="Low").target_range == (12,20)
    assert StoryboardSettings(density="High").target_range == (3,6)
    with pytest.raises(ValueError): StoryboardSettings(minimum=10, maximum=2)
    scene, run = fixture_data()
    board = plan_scene(scene, TEXT, StoryboardSettings(maximum=20, minimum=.1), FakeProvider(), run)
    assert board.status == "ready"
    assert [s.source_start for s in board.shots] == [0, TEXT.index("Ben"), TEXT.index("Ada smiles")]
    assert [s.audio_start for s in board.shots] == [1,3,7]
    assert board.shots[0].audio_end == board.shots[1].audio_start
    assert board.shots[-1].audio_end == run.scenes[0].audio_end
    assert [s.original_prose for s in board.shots] == [TEXT[s.source_start:s.source_end] for s in board.shots]
    assert board.settings_fingerprint == board.settings.fingerprint()
    broken = board.model_copy(deep=True)
    broken.shots[1].audio_start += .2
    with pytest.raises(ValueError, match="gap"): broken.validate_against(TEXT, 20)
    broken.shots[1].audio_start -= .4
    with pytest.raises(ValueError, match="overlap"): broken.validate_against(TEXT, 20)

def test_pause_policy_and_incomplete_timing():
    scene, run = fixture_data()
    previous = plan_scene(scene, TEXT, StoryboardSettings(minimum=.1, maximum=20), FakeProvider(), run)
    next_board = plan_scene(scene, TEXT, StoryboardSettings(minimum=.1, maximum=20, pause_policy="next"), FakeProvider(), run)
    assert previous.shots[2].audio_start == 7
    assert next_board.shots[2].audio_start == 4.4
    scene, partial = fixture_data(missing=True)
    draft = plan_scene(scene, TEXT, StoryboardSettings(minimum=.1, maximum=20), FakeProvider(), partial)
    assert draft.status == "draft"
    assert any(s.timing_status != "fully timed" for s in draft.shots)
    untimed = plan_scene(scene, TEXT, StoryboardSettings(), FakeProvider(), None)
    assert untimed.status == "draft" and all(s.audio_start is None for s in untimed.shots)
    short = plan_scene(scene, TEXT, StoryboardSettings(minimum=10, maximum=20), FakeProvider(), partial)
    assert short.status == "draft"

def test_repeated_passages_chunking_and_invalid_ai():
    text = "Repeat now. Repeat now. Repeat now."
    scene, run = fixture_data(text)
    board = plan_scene(scene, text, StoryboardSettings(minimum=.1, maximum=20), FakeProvider(), run)
    assert board.shots[1].source_start == text.index("Repeat", 1)
    long_text = "One sentence. " * 200
    long_scene = Scene(sequence=1, start=0, end=len(long_text), text=long_text)
    provider = FakeProvider((0,199))
    long_board = plan_scene(long_scene, long_text, StoryboardSettings(), provider, chunk_chars=1000)
    assert provider.calls > 1
    assert len(long_board.shots) == 2
    assert long_board.shots[0].source_end == long_board.shots[1].source_start
    class BadProvider:
        def propose_shots(self, payload):
            return ShotIdeas(shots=[ShotIdea(start_id=999, title="Invalid", visual_description="Invalid anchor")])
    with pytest.raises(ValueError, match="anchor"):
        plan_scene(scene, text, StoryboardSettings(), BadProvider())

def test_editing_coverage_and_identifiers():
    scene, run = fixture_data()
    board = plan_scene(scene, TEXT, StoryboardSettings(minimum=.1, maximum=20), FakeProvider((0,)), run)
    original = board.shots[0].identifier
    split = split_shot(board, original, TEXT.index("Ben"), TEXT, run)
    assert split.status == "ready" and split.shots[0].identifier == original
    assert split.shots[0].audio_end == split.shots[1].audio_start
    merged = merge_shots(split, original, TEXT)
    assert len(merged.shots) == 1 and merged.shots[0].original_prose == TEXT
    deleted = delete_shot(split, original, TEXT, "next")
    assert len(deleted.shots) == 1 and deleted.shots[0].source_start == 0
    with pytest.raises(ValueError, match="Choose"): delete_shot(split, original, TEXT, "gap")
    edited = update_shot(split, original, {"title":"New title", "review_status":"Reviewed"}, TEXT)
    assert edited.shots[0].title == "New title" and edited.shots[0].manually_edited
    assert edited.shots[0].review_status == "Manually edited"
    swapped = swap_visuals_with_next(split, original, TEXT)
    assert swapped.shots[0].title == split.shots[1].title
    assert swapped.shots[0].source_start == split.shots[0].source_start

def test_migration_replacement_staleness_exports(tmp_path):
    source = tmp_path / "story.txt"; source.write_text(TEXT)
    session = import_prose(create_project(tmp_path, "Project"), source)
    scene, run = fixture_data()
    session.database.replace_scenes(TEXT, [scene])
    board = plan_scene(scene, TEXT, StoryboardSettings(minimum=.1, maximum=20), FakeProvider(), run)
    db = session.database
    db.save_storyboard(board, TEXT)
    assert db.load_storyboard(scene.identifier).identifier == board.identifier
    with sqlite3.connect(db.path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 6
        assert connection.execute("SELECT COUNT(*) FROM shot").fetchone()[0] == 3
    with pytest.raises(Exception):
        db.save_storyboard(board.model_copy(update={"identifier":"bad", "scene_definition_hash":"bad"}), TEXT, replace=True)
    assert db.load_storyboard(scene.identifier).identifier == board.identifier
    edited = update_shot(board, board.shots[0].identifier, {"title":"Edited"}, TEXT)
    db.save_storyboard(edited, TEXT)
    assert db.load_storyboard(scene.identifier).shots[0].title == "Edited"
    assert db.storyboard_state(board, TEXT+"x", run).startswith("Prose changed")
    changed = run.model_copy(update={"identifier":"different"})
    assert db.storyboard_state(board, TEXT, changed).startswith("Audio alignment changed")
    j,m = tmp_path/"board.json",tmp_path/"board.md"
    export_storyboard_json(j, TEXT, [scene], [edited]); export_storyboard_markdown(m,TEXT,[scene],[edited])
    assert json.loads(j.read_text())["storyboards"][0]["shots"][0]["identifier"] == board.shots[0].identifier
    assert json.loads(j.read_text())["storyboards"][0]["shots"][0]["duration"] == board.shots[0].duration
    assert "Original prose" in m.read_text() and "Foreground" in m.read_text()

def test_cancellation_and_gui_selection(tmp_path):
    scene, run = fixture_data()
    canceled = Event(); canceled.set()
    with pytest.raises(PlanningCanceled):
        plan_scene(scene, TEXT, StoryboardSettings(), FakeProvider(), run, cancel=canceled)
    app = QApplication.instance() or QApplication([])
    source = tmp_path / "story.txt"; source.write_text(TEXT)
    session = import_prose(create_project(tmp_path, "Project"), source)
    audio = tmp_path / "voice.wav"
    with wave.open(str(audio), "wb") as stream:
        stream.setnchannels(1); stream.setsampwidth(2); stream.setframerate(16000)
        stream.writeframes(b"\0\0" * 16000 * 20)
    session = import_audio(session, audio)
    run.audio_hash = source_file_hash(session.project.resolved_audio(session.root))
    session.database.replace_scenes(TEXT,[scene])
    session.database.save_alignment(run)
    board = plan_scene(scene,TEXT,StoryboardSettings(minimum=.1,maximum=20),FakeProvider(),run)
    session.database.save_storyboard(board,TEXT)
    window = MainWindow(); window._set_session(session)
    deadline = time.monotonic()+5
    while (window._task or window._waveform_task or not window._prose_ready or not window._alignment) and time.monotonic()<deadline:
        app.processEvents(); time.sleep(.01)
    window.storyboard_panel.select_id(board.shots[1].identifier)
    app.processEvents()
    assert window.prose_viewer._layers["shot"] == (board.shots[1].source_start,board.shots[1].source_end)
    played=[]; window.audio_player.play_range=lambda a,b: played.append((a,b))
    window._play_shot(); assert played == [(board.shots[1].audio_start,board.shots[1].audio_end)]
    window.storyboard_panel.playback_position(board.shots[2].audio_start+.1)
    assert window.storyboard_panel.current_shot().identifier == board.shots[2].identifier
    cursor = window.prose_viewer.textCursor()
    cursor.setPosition(board.shots[1].source_start)
    cursor.setPosition(board.shots[1].source_start+3,QTextCursor.KeepAnchor)
    window.prose_viewer.setTextCursor(cursor)
    app.processEvents()
    assert window.storyboard_panel.current_shot().identifier == board.shots[1].identifier
    window.close()

def test_atomic_multi_scene_replacement(tmp_path, monkeypatch):
    text = "Ada waits. Ben enters."
    source = tmp_path/"story.txt"; source.write_text(text)
    session = import_prose(create_project(tmp_path,"Project"),source)
    cut = text.index("Ben")
    scenes = [Scene(sequence=1,start=0,end=cut,text=text[:cut]),
              Scene(sequence=2,start=cut,end=len(text),text=text[cut:])]
    db = session.database; db.replace_scenes(text,scenes)
    settings = StoryboardSettings()
    originals = [plan_scene(s,text,settings,FakeProvider((0,))) for s in scenes]
    db.save_storyboards(originals,text)
    replacements = [plan_scene(s,text,settings,FakeProvider((0,))) for s in scenes]
    original_write = db._write_storyboard
    calls = 0
    def fail_second(connection, board):
        nonlocal calls
        calls += 1
        original_write(connection,board)
        if calls == 2: raise sqlite3.IntegrityError("simulated failure")
    monkeypatch.setattr(db,"_write_storyboard",fail_second)
    with pytest.raises(Exception,match="simulated failure"):
        db.save_storyboards(replacements,text,replace=True)
    assert [db.load_storyboard(s.identifier).identifier for s in scenes] == [b.identifier for b in originals]

def test_gui_fake_generation_without_live_key(tmp_path, monkeypatch):
    import prose_to_video.gui.main_window as gui
    app = QApplication.instance() or QApplication([])
    source = tmp_path/"story.txt"; source.write_text(TEXT)
    session = import_prose(create_project(tmp_path,"Project"),source)
    scene = Scene(sequence=1,start=0,end=len(TEXT),text=TEXT)
    session.database.replace_scenes(TEXT,[scene])
    provider = FakeProvider()
    monkeypatch.setattr(gui,"make_provider",lambda settings: provider)
    window = MainWindow(); window._set_session(session)
    deadline = time.monotonic()+5
    while (window._task or not window._prose_ready) and time.monotonic()<deadline:
        app.processEvents(); time.sleep(.01)
    window._generate_storyboards()
    deadline = time.monotonic()+5
    while window._storyboard_task and time.monotonic()<deadline:
        app.processEvents(); time.sleep(.01)
    assert provider.calls == 1
    assert len(window.storyboard_panel.board.shots) == 3
    assert session.database.load_storyboard(scene.identifier).status == "draft"
    window.close()

def test_shot_validation_duplicate_ids_and_audio_bounds():
    scene, run = fixture_data()
    board = plan_scene(scene,TEXT,StoryboardSettings(minimum=.1,maximum=20),FakeProvider(),run)
    duplicate = board.model_copy(deep=True)
    duplicate.shots[1].identifier = duplicate.shots[0].identifier
    with pytest.raises(ValueError,match="unique"): duplicate.validate_against(TEXT,20)
    out_of_range = board.model_copy(deep=True)
    out_of_range.shots[-1].audio_end = 21
    with pytest.raises(ValueError,match="coverage|audio duration"): out_of_range.validate_against(TEXT,20)
    with pytest.raises(ValueError,match="exact source"):
        board.shots[0].model_copy(update={"original_prose":"wrong"}).model_validate(
            board.shots[0].model_copy(update={"original_prose":"wrong"}).model_dump())

def test_version_three_migration_preserves_alignment_and_scenes(tmp_path):
    source = tmp_path/"story.txt"; source.write_text(TEXT)
    session = import_prose(create_project(tmp_path,"Project"),source)
    scene, run = fixture_data()
    session.database.replace_scenes(TEXT,[scene]); session.database.save_alignment(run)
    with sqlite3.connect(session.database.path) as connection:
        for table in ("shot","active_storyboard","storyboard_run","storyboard_settings"):
            connection.execute(f"DROP TABLE {table}")
        connection.execute("PRAGMA user_version=3")
    assert session.database.load_alignment().identifier == run.identifier
    assert session.database.load_scenes()[1][0].identifier == scene.identifier
    with sqlite3.connect(session.database.path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 6

def test_inter_scene_pause_visual_coverage():
    text = "Ada waits. Ben enters."
    cut = text.index("Ben")
    scenes = [Scene(sequence=1,start=0,end=cut,text=text[:cut]),
              Scene(sequence=2,start=cut,end=len(text),text=text[cut:])]
    words = [WordAlignment(source_start=m.start(),source_end=m.end(),text=m.group(),recognized=m.group(),
        audio_start=start,audio_end=start+.4,status="matched")
        for m,start in zip(re.finditer(r"\b[A-Za-z]+\b",text),(1.,2.,5.,6.))]
    run = AlignmentRun(prose_hash=source_hash(text),audio_hash="audio",audio_path="voice.wav",audio_duration=10,
        transcription_model="mock",device="cpu",precision="int8",config_fingerprint="mock",words=words,
        scenes=[SceneTiming(scene_id=scenes[0].identifier,source_start=0,source_end=cut,audio_start=1,audio_end=2.4,
                            matched_words=2,unmatched_words=0,coverage=1,status="timed"),
                SceneTiming(scene_id=scenes[1].identifier,source_start=cut,source_end=len(text),audio_start=5,audio_end=6.4,
                            matched_words=2,unmatched_words=0,coverage=1,status="timed")])
    settings = StoryboardSettings(minimum=.1,maximum=20,pause_policy="previous")
    left,right = [plan_scene(scene,text,settings,FakeProvider((0,)),run) for scene in scenes]
    assert left.coverage_end == right.coverage_start == 5
    settings = settings.model_copy(update={"pause_policy":"next"})
    left,right = [plan_scene(scene,text,settings,FakeProvider((0,)),run) for scene in scenes]
    assert left.coverage_end == right.coverage_start == 2.4

def test_openai_adapter_uses_structured_shot_schema_without_network():
    from prose_to_video.pipeline.providers import OpenAIProvider
    provider = object.__new__(OpenAIProvider)
    seen = {}
    def fake_request(instructions, content, schema):
        seen.update(instructions=instructions, content=content, schema=schema)
        return ShotIdeas(shots=[])
    provider._request = fake_request
    assert provider.propose_shots("labeled source").shots == []
    assert seen["schema"] is ShotIdeas
    assert "timestamps" in seen["instructions"]
    with pytest.raises(ValueError):
        ShotIdea(start_id=0,title="Invalid time",visual_description="Source moment",audio_start=1.0)

def test_character_aliases_and_appearance_continuity():
    text = "Jeffrey waits. Jeffery speaks."
    scene = Scene(sequence=1,start=0,end=len(text),text=text,characters=["Jeffrey"])
    class Provider:
        def propose_shots(self,payload):
            return ShotIdeas(shots=[
                ShotIdea(start_id=0,title="Wait",visual_description="Jeffrey waits by the doorway.",
                         characters=["Jeffrey"],character_appearances={"Jeffrey":"dark coat"},objects=["key"]),
                ShotIdea(start_id=1,title="Reply",visual_description="Jeffery speaks by the doorway.",
                         characters=["Jeffery"],character_appearances={"Jeffery":"white coat"},objects=["key"])])
    board = plan_scene(scene,text,StoryboardSettings(),Provider())
    assert board.shots[1].characters == ["Jeffrey"]
    assert "Review appearance continuity" in board.shots[1].ai_notes
    assert "character:jeffrey" in board.shots[1].entity_refs
    assert "prop:key" in board.shots[1].entity_refs
