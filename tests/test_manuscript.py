"""Source-grounded manuscript regression checks; no live AI or media service."""
from __future__ import annotations
from prose_to_video.storage.database import SCHEMA_VERSION

import os
import sqlite3
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtWidgets import QApplication

from prose_to_video.gui.manuscript_panel import ManuscriptPanel
from prose_to_video.gui.storyboard_panel import StoryboardPanel
from prose_to_video.models.alignment import TranscriptWord
from prose_to_video.models.alignment import AlignmentRun
from prose_to_video.models.shot import ViewpointContext
from prose_to_video.models.shot import StoryboardSettings
from prose_to_video.models.scene import Scene, source_hash
from prose_to_video.models.visual_reference import AppearanceVariant, VisualEntity
from prose_to_video.pipeline.shot_planning import ShotIdea, ShotIdeas, plan_scene, target_range_for_scene
from prose_to_video.pipeline.manuscript import (analysis_report, export_report,
    included_ranges, parse_manuscript)
from prose_to_video.pipeline.text_matching import match_words, span_alignment
from prose_to_video.pipeline.text_matching import derive_scene_timings, manuscript_tokens
from prose_to_video.pipeline.scene_detection import detect_scenes
from prose_to_video.pipeline.providers import AISettings, BoundaryProposal, SceneDescription
from prose_to_video.storage.manuscript import ManuscriptStore
from prose_to_video.storage.visual_library import VisualLibrary
from prose_to_video.storage.project_files import create_project
from prose_to_video.utils.prose import read_prose


FIXTURE = Path(__file__).parent / "fixtures" / "001_40_Marvin.md"


def test_full_manuscript_structure_and_chronology():
    text = read_prose(FIXTURE)
    sections = parse_manuscript(text)
    assert len(sections) == 19
    assert sections[0].heading == "Change" and sections[0].classification == "Editorial Note"
    assert not sections[0].included
    assert sections[1].number == 1 and sections[-1].number == 18
    assert sections[1].source(text).startswith("# 01 Werliner Shoes")
    assert sections[-1].end == len(text)
    assert all(a.end == b.start for a, b in zip(sections, sections[1:]))
    assert "advertisement" in sections[1].formats
    assert "radio" in sections[3].formats and sections[3].viewpoint == "radio"
    assert "surveillance" in sections[7].formats
    assert "television" in sections[12].formats
    assert "video call" in sections[15].formats
    assert "exposition" in sections[16].formats
    assert "surveillance" in sections[-1].formats
    report = analysis_report(text, sections)
    assert any(issue["character"] == "Marvin" for issue in report["chronology_conflicts"])
    assert any(item["name"] == "Marvin" for item in report["recurring_characters"])
    assert any(item["name"] == "Werliner Shoes" for item in report["recurring_locations"])
    assert any(item["name"] == "rock" for item in report["recurring_objects"])
    assert all(text[a:b].casefold().rstrip("s") == item["name"].casefold().rstrip("s")
               for key in ("recurring_characters", "recurring_locations", "recurring_objects")
               for item in report[key] for a, b in item["source_ranges"])
    assert any(event["year"] == 2023 for event in report["chronology"])
    assert any(event["year"] == 2040 for event in report["chronology"])
    assert all(text[event["source_start"]:event["source_end"]] for event in report["chronology"]
               if event["year"] is not None)
    assert all(text[c["source_start"]:c["source_end"]] == c["quote"]
               for c in report["chronology_claims"])


def test_crlf_setext_unnumbered_offsets_and_ellipsis():
    text = "Intro\r\n=====\r\nAn ad.\r\n\r\n# Talk Radio\r\nA radio plays... ... ...\r\nCaller speaks.\r\n"
    sections = parse_manuscript(text)
    assert [s.heading for s in sections] == ["Intro", "Talk Radio"]
    assert [s.level for s in sections] == [1, 1]
    assert sections[0].source(text).endswith("\r\n\r\n")
    assert sections[1].source(text).startswith("# Talk Radio\r\n")
    assert sections[1].narration_range() == (sections[1].body_start, len(text))
    assert len(sections) == 2
    bom = "\ufeff# Chapter\r\nStory.\r\n"
    assert parse_manuscript(bom)[0].heading == "Chapter"
    assert parse_manuscript(bom)[0].source(bom) == bom


def test_review_decision_and_filtered_alignment_preserve_source(tmp_path):
    text = "# Change\r\nRemove this.\r\n# Story\r\nThe rock shines... ... ...\r\n"
    session = create_project(tmp_path, "Manuscript")
    store = ManuscriptStore(session.database)
    sections = store.sections(text)
    original = text.encode()
    store.decide(text, sections[0].identifier, "Editorial Note", False, False)
    assert store.sections(text)[0].included is False
    spoken = [TranscriptWord(text="The", start=0, end=.2),
              TranscriptWord(text="rock", start=.3, end=.5),
              TranscriptWord(text="shines", start=.6, end=.9)]
    words, _, _ = match_words(text, spoken, included_ranges(store.sections(text)))
    assert all(w.status == "excluded" for w in words if w.source_start < sections[1].body_start)
    assert span_alignment(sections[1].start, sections[1].end, words).coverage == 1
    assert text.encode() == original
    store.decide(text, sections[1].identifier, "Narrated Story", False, True)
    assert store.sections(text)[1].included is False
    assert store.sections(text + "extra")[1].included is True


def test_dotted_acronym_and_spoken_year_keep_exact_offsets():
    text = "In twenty-thirty, A.I. speaks."
    spoken = [TranscriptWord(text=word, start=i, end=i + .4) for i, word in
              enumerate(["In", "2030", "AI", "speaks"])]
    words, _, _ = match_words(text, spoken)
    assert [w.status for w in words] == ["matched"] * 4
    assert all(text[w.source_start:w.source_end] == w.text for w in words)


def test_report_export_and_viewpoint_validation(tmp_path):
    text = read_prose(FIXTURE)
    report = analysis_report(text, parse_manuscript(text))
    json_path, md_path = tmp_path / "report.json", tmp_path / "report.md"
    export_report(json_path, report)
    export_report(md_path, report)
    assert '"chronology_conflicts"' in json_path.read_text()
    assert "## Chronology warnings" in md_path.read_text()
    with pytest.raises(ValueError, match="deliberate"):
        ViewpointContext(category="surveillance", display_framing="external")
    view = ViewpointContext(category="surveillance", physical_location="home",
        depicted_location="tavern", media_source="public camera", display_framing="screen")
    assert view.physical_location != view.depicted_location


def test_gui_structure_navigation(tmp_path):
    app = QApplication.instance() or QApplication([])
    session = create_project(tmp_path, "Structure")
    text = "# Change\nRemove this.\n# 7 Story\nA scene."
    panel = ManuscriptPanel()
    seen = []
    panel.navigate.connect(lambda a, b: seen.append((a, b)))
    panel.bind(session.database, text)
    assert panel.table.rowCount() == 2
    panel.table.selectRow(1)
    assert seen[-1] == (text.index("# 7"), len(text))
    panel.include.setChecked(False)
    panel.save.click()
    assert not ManuscriptStore(session.database).sections(text)[1].included
    panel.close()


def test_radio_dialogue_one_shot_and_period_variants():
    text = "A radio plays in the cafe. A caller speaks through the radio. The host responds."
    scene = Scene(sequence=1, start=0, end=len(text), text=text,
        narrative_formats=["radio", "dialogue"], viewpoint_category="radio", location="cafe")
    class OneShotProvider:
        def propose_shots(self, payload):
            return ShotIdeas(shots=[ShotIdea(start_id=0, title="Radio in cafe",
                visual_description="Radio on the cafe counter", visual_treatment="establishing")])
    board = plan_scene(scene, text, StoryboardSettings(), OneShotProvider())
    assert len(board.shots) == 1
    assert board.shots[0].viewpoint.category == "radio"
    assert board.shots[0].viewpoint.physical_location == "cafe"
    assert board.shots[0].visual_treatment == "establishing"
    app = QApplication.instance() or QApplication([])
    editor = StoryboardPanel()
    editor.set_board(board)
    editor.viewpoint_category.setCurrentText("radio")
    editor.physical_location.setText("cafe")
    editor.depicted_location.setText("remote caller location unknown")
    changes = editor.changes()
    assert changes["viewpoint"].physical_location == "cafe"
    assert changes["viewpoint"].depicted_location == "remote caller location unknown"
    editor.close()
    assert target_range_for_scene(scene, StoryboardSettings()) == pytest.approx((7.2, 14.4))
    assert target_range_for_scene(scene.model_copy(update={"narrative_formats": ["action"]}),
                                  StoryboardSettings()) == pytest.approx((4.5, 9.0))
    with pytest.raises(ValueError, match="Excluded"):
        plan_scene(scene.model_copy(update={"production_included": False}), text,
                   StoryboardSettings(), OneShotProvider())
    identity = VisualEntity(name="Ada", entity_type="Character")
    young = AppearanceVariant(entity_id=identity.identifier, name="Young", age_min=20, age_max=30)
    elder = AppearanceVariant(entity_id=identity.identifier, name="Elder", age_min=60, age_max=70)
    assert young.entity_id == elder.entity_id and young.identifier != elder.identifier
    with pytest.raises(ValueError, match="reversed"):
        AppearanceVariant(entity_id=identity.identifier, name="Invalid", age_min=70, age_max=20)


def test_migrate_ten_to_eleven_preserves_project(tmp_path):
    session = create_project(tmp_path, "Migration")
    identifier = session.project.identifier
    with sqlite3.connect(session.database.path) as con:
        con.execute("DROP TABLE manuscript_decision")
        con.execute("PRAGMA user_version=10")
    assert session.database.load().identifier == identifier
    store = ManuscriptStore(session.database)
    text = "# Story\nA complete scene."
    assert store.sections(text)[0].included
    with sqlite3.connect(session.database.path) as con:
        assert con.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION


def test_nested_location_identity_and_cycle_guard(tmp_path):
    session = create_project(tmp_path, "Locations")
    library = VisualLibrary(session.root)
    building = library.save_entity(VisualEntity(name="Workshop", entity_type="Location"))
    roof = library.save_entity(VisualEntity(name="Roof", entity_type="Location",
        parent_entity_id=building.identifier, spatial_relationship="above sixth floor"))
    room = library.save_entity(VisualEntity(name="Roof room", entity_type="Location",
        parent_entity_id=roof.identifier, spatial_relationship="on roof"))
    assert library.entity(room.identifier).parent_entity_id == roof.identifier
    building.parent_entity_id = room.identifier
    with pytest.raises(ValueError, match="cycle"):
        library.save_entity(building)


def test_mixed_format_detection_alignment_and_storyboard():
    text = ("# Change\nRemove this line.\n# Old commercial\nThe workshop makes shoes.\n"
            "# Radio\nA radio plays in a cafe. A caller speaks through it.\n"
            "# Network\nThe network carries signals between devices.\n")
    sections = parse_manuscript(text)
    class Boundaries:
        def propose_boundaries(self, labeled):
            import re
            return BoundaryProposal(starts=[int(i) for i in re.findall(r"\[ID (\d+); eligible\] #", labeled)])
        def describe_scene(self, passage):
            return SceneDescription(title="Source scene", location="cafe" if "radio" in passage else None,
                time_of_day=None, characters=[], point_of_view=None, narrative_type=None,
                summary="", environment="", important_objects=[], actions=[],
                dialogue_participants=[], visual_atmosphere="", boundary_reason="Source heading")
    scenes = detect_scenes(text, Boundaries(), AISettings(), sections=sections)
    assert len(scenes) >= 4
    assert not scenes[0].production_included
    assert all(s.text == text[s.start:s.end] for s in scenes)
    eligible = [token for token in manuscript_tokens(text) if any(
        a <= token.start and token.end <= b for a, b in included_ranges(sections))]
    spoken = [TranscriptWord(text=token.text, start=i + 1, end=i + 1.4)
              for i, token in enumerate(eligible)]
    words, passages, issues = match_words(text, spoken, included_ranges(sections))
    assert not issues and passages
    run = AlignmentRun(prose_hash=source_hash(text),
        audio_hash="fake", audio_path="fake.wav", audio_duration=len(spoken) + 3,
        transcription_model="fake", device="cpu", precision="int8", config_fingerprint="fake",
        words=words, scenes=derive_scene_timings(scenes, words))
    class OneShot:
        def propose_shots(self, payload):
            return ShotIdeas(shots=[ShotIdea(start_id=0, title="One visual beat", visual_description="Grounded view")])
    boards = [plan_scene(scene, text, StoryboardSettings(minimum=.1, maximum=100), OneShot(), run)
              for scene in scenes if scene.production_included]
    assert len(boards) >= 3
    assert all(len(board.shots) == 1 for board in boards)
    assert all(board.shots[0].source_start >= sections[1].start for board in boards)
    assert all(text[shot.source_start:shot.source_end] == shot.original_prose
               for board in boards for shot in board.shots)
    assert all(shot.audio_start is not None and shot.audio_end is not None
               for board in boards for shot in board.shots)
    assert any(board.shots[0].viewpoint.category == "radio" for board in boards)
