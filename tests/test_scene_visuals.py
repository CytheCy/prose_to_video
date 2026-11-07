from threading import Event
from dataclasses import replace
import json

import pytest

from prose_to_video.models.scene import Scene, source_hash
from prose_to_video.models.visual_reference import VisualEntity
from prose_to_video.pipeline.manuscript import parse_manuscript
from prose_to_video.pipeline.providers import AISettings
from prose_to_video.pipeline.scene_detection import AnalysisCanceled, detect_scenes, merge_with_next
from prose_to_video.pipeline.scene_visuals import (
    SceneContinuity, SceneVisualDescription, enrich_scene_visuals,
)
from prose_to_video.storage.project_files import create_project, open_project
from prose_to_video.storage.scene_export import export_json, export_markdown
from prose_to_video.storage.visual_library import VisualLibrary
from test_scenes import MockProvider


class VisualProvider(MockProvider):
    def __init__(self):
        super().__init__("She sits.")
        self.context_calls = []
        self.visual_calls = []

    def summarize_scene_context(self, payload):
        data = json.loads(payload)
        self.context_calls.append(data)
        return SceneContinuity(notes=(data["previous_notes"] + " Ada wears a red coat.")[:data["notes_character_limit"]])

    def generate_scene_visual(self, payload):
        data = json.loads(payload)
        self.visual_calls.append(data)
        return SceneVisualDescription(visual_description="Ada in her red coat, foreground bench, brick background, soft side light, medium shot.")


def test_broader_context_reaches_scene_and_persists(tmp_path):
    text = "# One\nAda wears a red coat.\n" + "She waits. " * 150 + "\nShe sits.\n# Two\nBen leaves."
    provider = VisualProvider()
    scenes = detect_scenes(text, provider, AISettings(chunk_chars=1000, overlap_chars=100))
    assert len(provider.context_calls) > 2
    assert provider.context_calls[1]["previous_notes"]
    assert provider.context_calls[0]["previous_notes"] == ""
    assert provider.visual_calls[0]["earlier_paragraph_continuity"] == ""
    assert any(call["source_prose"] == "She sits." and "red coat" in call["earlier_paragraph_continuity"]
               for call in provider.visual_calls)
    assert all("# One" not in c["source_prose"] for c in provider.context_calls)
    assert all(len(c["source_prose"]) + len(c["previous_notes"]) <= 1000 for c in provider.context_calls)
    assert all(sum(len(c[k]) for k in ("source_prose", "earlier_paragraph_continuity", "visual_reference_notes")) <= 1000
               for c in provider.visual_calls)
    assert "".join(scene.text for scene in scenes) == text
    session = create_project(tmp_path, "Visual scenes")
    session.database.replace_scenes(text, scenes)
    saved = open_project(session.root).database.load_scenes()[1]
    assert saved == scenes
    assert saved[0].visual_description
    assert saved[0].summary == "Events in source."
    export_json(tmp_path / "scenes.json", text, saved)
    export_markdown(tmp_path / "scenes.md", text, saved)
    assert json.loads((tmp_path / "scenes.json").read_text())["scenes"][0]["visual_description"]
    assert "**Visual description:** Ada" in (tmp_path / "scenes.md").read_text()
    merged = merge_with_next(text, saved, saved[0].identifier)
    assert not merged[0].visual_description


def test_catalog_and_selected_style_enrich_only_matching_entities(tmp_path):
    session = create_project(tmp_path, "Catalog")
    library = VisualLibrary(session.root)
    library.save_entity(VisualEntity(name="Ada", entity_type="Character", appearance_description="red coat"))
    library.save_entity(VisualEntity(name="Ben", entity_type="Character", appearance_description="blue hat"))
    style_id = library.add_prompt_style("Drawing", "charcoal illustration")
    library.select_prompt_style(style_id, True)
    text = "Ada waits."
    scene = Scene(sequence=1, start=0, end=len(text), text=text, characters=["Ada"])
    provider = VisualProvider()
    enrich_scene_visuals(text, [scene], parse_manuscript(text), provider, AISettings(),
                        Event(), lambda *args: None, library)
    notes = provider.visual_calls[0]["visual_reference_notes"]
    assert "Ada" in notes and "red coat" in notes and "charcoal illustration" in notes
    assert "Ben" not in notes and "blue hat" not in notes


def test_excluded_sections_and_cancellation():
    text = "# One\nAda waits.\n# Two\nEditorial secret."
    sections = parse_manuscript(text)
    sections[1] = replace(sections[1], included=False)
    provider = VisualProvider()
    scenes = detect_scenes(text, provider, AISettings(), sections=sections)
    assert all("Editorial secret" not in c["source_prose"] for c in provider.context_calls + provider.visual_calls)
    assert not scenes[-1].visual_description
    cancel = Event()

    def cancel_context(payload):
        cancel.set()
        return SceneContinuity(notes="Ada")
    provider.summarize_scene_context = cancel_context
    with pytest.raises(AnalysisCanceled):
        detect_scenes("Ada waits.", provider, AISettings(), cancel)


def test_visual_failure_preserves_saved_analysis(tmp_path):
    session = create_project(tmp_path, "Failure")
    text = "Ada waits."
    original = Scene(sequence=1, start=0, end=len(text), text=text, visual_description="Saved manual design")
    session.database.replace_scenes(text, [original])
    provider = VisualProvider()
    provider.generate_scene_visual = lambda payload: SceneVisualDescription(visual_description="")
    with pytest.raises(ValueError, match="empty visual"):
        detect_scenes(text, provider, AISettings())
    assert session.database.load_scenes() == (source_hash(text), [original])


def test_provider_visual_instructions_and_structured_output(monkeypatch):
    from unittest.mock import Mock
    from prose_to_video.pipeline.providers import OpenAIProvider
    monkeypatch.setenv("TEST_SCENE_KEY", "placeholder")
    provider = OpenAIProvider(AISettings(credential_env="TEST_SCENE_KEY"))
    provider._request = Mock(return_value=SceneContinuity(notes="Ada wears red."))
    provider.summarize_scene_context('{"source_prose":"Ada wears red."}')
    instructions, payload, schema = provider._request.call_args.args
    assert schema is SceneContinuity
    assert "previous_notes" in instructions and "period" in instructions
    provider._request.return_value = SceneVisualDescription(visual_description="A visual scene")
    provider.generate_scene_visual('{"source_prose":"Ada waits."}')
    instructions, payload, schema = provider._request.call_args.args
    assert schema is SceneVisualDescription
    for layer in ("CHARACTERS", "FOREGROUND", "MIDGROUND", "BACKGROUND", "ATMOSPHERE", "CAMERA/FRAMING"):
        assert layer in instructions
    assert "later events" in instructions
    assert "proposed visual details" in instructions


def test_visual_description_editing_in_scene_panel():
    from PySide6.QtWidgets import QApplication
    from prose_to_video.gui.scenes_panel import ScenesPanel
    application = QApplication.instance() or QApplication([])
    panel = ScenesPanel()
    text = "Ada waits."
    scene = Scene(sequence=1, start=0, end=len(text), text=text, visual_description="Wide view")
    panel.set_scenes([scene])
    panel.set_state(True, False)
    assert panel.fields["visual_description"].toPlainText() == "Wide view"
    panel.fields["visual_description"].setPlainText("Close view in charcoal")
    panel.fields["render_type"].setText("product_showcase")
    edited = panel.edited_scene()
    assert edited.visual_description == "Close view in charcoal"
    assert edited.render_type == "product_showcase"
    assert edited.text == text and edited.summary == scene.summary
    panel.close()


def test_five_post_office_paragraphs_inherit_earlier_details_then_move():
    class PostOfficeProvider(VisualProvider):
        def summarize_scene_context(self, payload):
            data = json.loads(payload)
            self.context_calls.append(data)
            notes = data["previous_notes"]
            if "post office" in data["source_prose"]:
                notes = "Ada is in the post office with brass mailboxes, wearing a red coat."
            if "walks outside" in data["source_prose"]:
                notes = "Ada is outside in the street, wearing a red coat."
            return SceneContinuity(notes=notes)

        def describe_scene_with_context(self, source, previous):
            result = super().describe_scene(source)
            result.location = ("street" if "walks outside" in source or "street" in previous
                               else "post office" if "post office" in source + previous else None)
            result.environment = "brass mailboxes" if result.location == "post office" else "street"
            return result

        def generate_scene_visual(self, payload):
            data = json.loads(payload)
            self.visual_calls.append(data)
            return SceneVisualDescription(visual_description=data["earlier_paragraph_continuity"] or data["source_prose"])

    paragraphs = [
        "Ada enters the post office with brass mailboxes, wearing a red coat.",
        "She joins the queue.", "She waits.", "She hands over a parcel.",
        "She accepts the receipt.", "She walks outside into the street.",
        "She crosses the road.",
    ]
    text = "\n\n".join(paragraphs)
    provider = PostOfficeProvider()
    scenes = detect_scenes(text, provider, AISettings(overlap_chars=0))
    assert len(scenes) == len(paragraphs)
    assert [scene.text.strip() for scene in scenes] == paragraphs
    assert [scene.location for scene in scenes] == ["post office"] * 5 + ["street"] * 2
    assert all(scene.environment == "brass mailboxes" for scene in scenes[:5])
    assert all("red coat" in scene.visual_description for scene in scenes[:5])
    assert all("street" not in call["earlier_paragraph_continuity"]
               for call in provider.visual_calls[:6])
    assert "street" in provider.visual_calls[6]["earlier_paragraph_continuity"]
