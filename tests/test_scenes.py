from __future__ import annotations
from prose_to_video.storage.database import SCHEMA_VERSION

import json
import re
import sqlite3
from pathlib import Path
from threading import Event

import pytest
from pydantic import ValidationError

from prose_to_video.models.scene import Scene, source_hash, validate_partition
from prose_to_video.pipeline.providers import AISettings, BoundaryProposal, OpenAIProvider, SceneDescription
from prose_to_video.pipeline.scene_detection import (AnalysisCanceled, detect_scenes,
                                                      make_chunks, merge_with_next,
                                                      paragraph_starts, split_scene)
from prose_to_video.storage.database import ProjectDatabase, ProjectDatabaseError
from prose_to_video.storage.project_files import create_project, open_project
from prose_to_video.storage.scene_export import export_json, export_markdown


TEXT = "At home, Ada waits.\n\nShe opens the door.\n\nAt the station, Ben arrives.\n\nHe boards a train."


def scenes_for(text: str = TEXT) -> list[Scene]:
    cut = text.index("At the station")
    return [Scene(sequence=1, start=0, end=cut, text=text[:cut], title="Home"),
            Scene(sequence=2, start=cut, end=len(text), text=text[cut:], title="Station")]


class MockProvider:
    def __init__(self, boundary_text: str = "At the station"):
        self.boundary_text = boundary_text
        self.calls = 0

    def propose_boundaries(self, labeled_text: str) -> BoundaryProposal:
        self.calls += 1
        ids = []
        for match in re.finditer(r"\[ID (\d+); (eligible|context only)\] ([^\n]*)", labeled_text):
            if match.group(2) == "eligible" and self.boundary_text in match.group(3):
                ids.append(int(match.group(1)))
        return BoundaryProposal(starts=ids)

    def describe_scene(self, scene_text: str) -> SceneDescription:
        return SceneDescription(title="Detected", location=None, time_of_day=None,
                                characters=["Ada"] if "Ada" in scene_text else ["Ben"],
                                point_of_view=None, narrative_type="action", summary="Events in source.",
                                environment="", important_objects=[], actions=[],
                                dialogue_participants=[], visual_atmosphere="", boundary_reason="")


def test_scene_model_and_partition_validation() -> None:
    scenes = scenes_for()
    validate_partition(TEXT, scenes)
    assert "".join(scene.text for scene in scenes) == TEXT
    with pytest.raises(ValidationError):
        Scene(sequence=1, start=0, end=3, text="hi")
    with pytest.raises(ValueError, match="gap, overlap"):
        validate_partition(TEXT, [scenes[0], scenes[1].model_copy(update={"start": scenes[1].start - 1})])
    with pytest.raises(ValueError, match="source text"):
        validate_partition(TEXT, [scenes[0].model_copy(update={"end": scenes[0].end - 1}), scenes[1]])
    with pytest.raises(ValueError, match="source text"):
        validate_partition(TEXT, [scenes[0].model_copy(update={"text": "X" + scenes[0].text[1:]}), scenes[1]])


def test_chunk_offsets_and_no_artificial_boundaries() -> None:
    text = ("A" * 800 + "\n\n") * 6 + "At the station, Ben arrives.\n\n" + ("B" * 800 + "\n\n") * 5
    chunks = make_chunks(text, 1800, 250)
    assert len(chunks) > 2
    assert {u.identifier for c in chunks for u in c.units if u.identifier in c.core_ids} == {
        u.identifier for c in chunks for u in c.units}
    assert all(text[u.start:u.end] for c in chunks for u in c.units)
    assert any(len([c for c in chunks if u in c.units]) > 1 for u in chunks[1].units)
    settings = AISettings(chunk_chars=1800, overlap_chars=250)
    scenes = detect_scenes(text, MockProvider(), settings)
    validate_partition(text, scenes)
    assert [s.start for s in scenes] == [0, text.index("At the station")]
    assert paragraph_starts(text)[0] == 0
    assert "Before\n***\nAfter".index("***") in paragraph_starts("Before\n***\nAfter")


def test_chunk_input_budget_for_many_short_paragraphs() -> None:
    from prose_to_video.pipeline.scene_detection import _labeled
    text = ("A.\n\n" * 600) + ("Z" * 2500)
    chunks = make_chunks(text, 1200, 100)
    assert all(len(_labeled(text, chunk.units)) <= 1200 for chunk in chunks)
    assert sum(len(chunk.core_ids) for chunk in chunks) == len({u.identifier for c in chunks for u in c.units})
    validate_partition(text, detect_scenes(text, MockProvider("Never present"),
                                           AISettings(chunk_chars=1200, overlap_chars=100)))


def test_invalid_ai_response_and_cancellation() -> None:
    class BadProvider(MockProvider):
        def propose_boundaries(self, labeled_text: str) -> BoundaryProposal:
            return BoundaryProposal(starts=[99999])
    with pytest.raises(ValueError, match="invalid paragraph ID"):
        detect_scenes(TEXT, BadProvider(), AISettings(chunk_chars=1000, overlap_chars=100))
    cancel = Event()
    cancel.set()
    with pytest.raises(AnalysisCanceled):
        detect_scenes(TEXT, MockProvider(), AISettings(), cancel)


def test_provider_settings_and_structured_request(monkeypatch: pytest.MonkeyPatch) -> None:
    from unittest.mock import Mock
    monkeypatch.setenv("TEST_SCENE_KEY", "placeholder")
    provider = OpenAIProvider(AISettings(credential_env="TEST_SCENE_KEY", model="chosen-model",
                                         temperature=0.4, max_output_tokens=600))
    provider.client = Mock()
    provider.client.responses.parse.return_value.output_parsed = BoundaryProposal(starts=[2])
    assert provider.propose_boundaries("[ID 2; eligible] New place") == BoundaryProposal(starts=[2])
    arguments = provider.client.responses.parse.call_args.kwargs
    assert arguments["model"] == "chosen-model"
    assert arguments["text_format"] is BoundaryProposal
    assert arguments["store"] is False
    assert arguments["temperature"] == 0.4
    assert "api_key" not in arguments
    provider.settings = AISettings(credential_env="TEST_SCENE_KEY", context_tokens=6000,
                                   chunk_chars=1000)
    with pytest.raises(ValueError, match="model context"):
        provider.propose_boundaries("x" * 4000)
    monkeypatch.delenv("TEST_SCENE_KEY")
    with pytest.raises(ValueError, match="TEST_SCENE_KEY"):
        OpenAIProvider(AISettings(credential_env="TEST_SCENE_KEY"))


def test_split_merge_and_ordering() -> None:
    scenes = scenes_for()
    cut = TEXT.index("She opens")
    split = split_scene(TEXT, scenes, scenes[0].identifier, cut)
    assert [s.sequence for s in split] == [1, 2, 3]
    assert split[0].identifier == scenes[0].identifier
    assert split[1].identifier not in {s.identifier for s in scenes}
    assert split[0].review_status == "Not reviewed"
    assert split[0].summary == ""
    validate_partition(TEXT, split)
    merged = merge_with_next(TEXT, split, split[0].identifier)
    validate_partition(TEXT, merged)
    assert [s.start for s in merged] == [0, scenes[1].start]
    with pytest.raises(ValueError, match="paragraph boundary"):
        split_scene(TEXT, scenes, scenes[0].identifier, 4)
    with pytest.raises(ValueError, match="following"):
        merge_with_next(TEXT, scenes, scenes[-1].identifier)


def test_migration_reopen_edits_and_source_hash(tmp_path: Path) -> None:
    session = create_project(tmp_path, "Story")
    db = session.database
    with sqlite3.connect(db.path) as connection:
        connection.execute("DROP TABLE scene")
        connection.execute("DROP TABLE scene_analysis")
        connection.execute("PRAGMA user_version = 1")
    assert open_project(session.root).project.identifier == session.project.identifier
    with sqlite3.connect(db.path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    db.replace_scenes(TEXT, scenes_for())
    digest, loaded = ProjectDatabase(db.path).load_scenes()
    assert digest == source_hash(TEXT)
    edit = loaded[0].model_copy(update={"summary": "Ada waits for the door.", "review_status": "Manually edited"})
    db.update_scene(edit, TEXT)
    assert open_project(session.root).database.load_scenes()[1][0].summary == edit.summary
    with pytest.raises(ProjectDatabaseError, match="outdated"):
        db.update_scene(edit, TEXT + "changed")
    db.delete_scene(loaded[1].identifier, TEXT)
    _, remaining = db.load_scenes()
    validate_partition(TEXT, remaining)
    assert len(remaining) == 1


def test_split_and_merge_persist_after_reopen(tmp_path: Path) -> None:
    session = create_project(tmp_path, "Story")
    original = scenes_for()
    session.database.replace_scenes(TEXT, original)
    split = split_scene(TEXT, original, original[0].identifier, TEXT.index("She opens"))
    session.database.replace_scenes(TEXT, split)
    reopened = open_project(session.root).database.load_scenes()[1]
    assert len(reopened) == 3
    assert reopened[1].start == TEXT.index("She opens")
    merged = merge_with_next(TEXT, reopened, reopened[0].identifier)
    session.database.replace_scenes(TEXT, merged)
    final = open_project(session.root).database.load_scenes()[1]
    validate_partition(TEXT, final)
    assert len(final) == 2


def test_reanalysis_replacement_and_transaction_rollback(tmp_path: Path) -> None:
    db = create_project(tmp_path, "Story").database
    old = scenes_for()
    db.replace_scenes(TEXT, old)
    replacement = [Scene(sequence=1, start=0, end=len(TEXT), text=TEXT, title="One scene")]
    db.replace_scenes(TEXT, replacement)
    assert db.load_scenes()[1][0].title == "One scene"
    with sqlite3.connect(db.path) as connection:
        connection.execute("CREATE TRIGGER reject_scene BEFORE INSERT ON scene BEGIN SELECT RAISE(ABORT, 'write failed'); END")
    with pytest.raises(ProjectDatabaseError, match="write failed"):
        db.replace_scenes(TEXT, old)
    assert db.load_scenes()[1][0].title == "One scene"


def test_failed_or_canceled_analysis_preserves_saved_scenes(tmp_path: Path) -> None:
    db = create_project(tmp_path, "Story").database
    db.replace_scenes(TEXT, scenes_for())
    class FailingProvider(MockProvider):
        def describe_scene(self, scene_text: str) -> SceneDescription:
            raise ValueError("Invalid AI response")
    with pytest.raises(ValueError, match="Invalid AI response"):
        detect_scenes(TEXT, FailingProvider(), AISettings())
    cancel = Event()
    cancel.set()
    with pytest.raises(AnalysisCanceled):
        detect_scenes(TEXT, MockProvider(), AISettings(), cancel)
    assert [s.title for s in db.load_scenes()[1]] == ["Home", "Station"]


def test_exports(tmp_path: Path) -> None:
    scenes = scenes_for()
    json_path, markdown_path = tmp_path / "scenes.json", tmp_path / "scenes.md"
    export_json(json_path, TEXT, scenes)
    export_markdown(markdown_path, TEXT, scenes)
    data = json.loads(json_path.read_text(encoding="utf-8"))
    assert data["source_sha256"] == source_hash(TEXT)
    assert data["scenes"][0]["text"] == scenes[0].text
    assert "## Scene 2: Station" in markdown_path.read_text(encoding="utf-8")
    assert TEXT[scenes[1].start:scenes[1].end] in markdown_path.read_text(encoding="utf-8")
