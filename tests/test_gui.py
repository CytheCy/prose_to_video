from __future__ import annotations

import os
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from prose_to_video.gui.main_window import MainWindow
from prose_to_video.models.scene import Scene
from prose_to_video.pipeline.providers import BoundaryProposal, SceneDescription
from prose_to_video.storage.project_files import create_project, import_prose, open_project


def test_window_smoke() -> None:
    application = QApplication.instance() or QApplication([])
    window = MainWindow()
    window.show()
    application.processEvents()
    assert window.isVisible()
    assert not window.save_action.isEnabled()
    assert not window.import_prose_button.isEnabled()
    window.close()


def test_mock_detection_gui_workflow(tmp_path: Path, monkeypatch) -> None:
    import prose_to_video.gui.main_window as main_window
    application = QApplication.instance() or QApplication([])
    source = tmp_path / "story.txt"
    source.write_text("Ada waits.\n\nAt the station, Ben arrives.", encoding="utf-8")
    session = import_prose(create_project(tmp_path, "Story"), source)

    class Provider:
        def propose_boundaries(self, labeled_text: str) -> BoundaryProposal:
            import re
            match = re.search(r"\[ID (\d+); eligible\] At the station", labeled_text)
            return BoundaryProposal(starts=[int(match.group(1))] if match else [])

        def describe_scene(self, scene_text: str) -> SceneDescription:
            return SceneDescription(title="Detected", location=None, time_of_day=None,
                                    characters=[], point_of_view=None, narrative_type=None,
                                    summary="Grounded summary", environment="", important_objects=[],
                                    actions=[], dialogue_participants=[], visual_atmosphere="",
                                    boundary_reason="New location")

    monkeypatch.setattr(main_window, "make_provider", lambda settings: Provider())
    window = MainWindow()
    window._set_session(open_project(session.root))
    deadline = time.monotonic() + 5
    while (window._task is not None or not window._prose_ready) and time.monotonic() < deadline:
        application.processEvents()
        time.sleep(0.01)
    window._detect_scenes()
    deadline = time.monotonic() + 5
    while window._scene_task is not None and time.monotonic() < deadline:
        application.processEvents()
        time.sleep(0.01)
    assert len(window.scenes_panel.scenes) == 2
    window.scenes_panel.fields["summary"].setPlainText("Reviewed summary")
    window._save_scene()
    assert window.scenes_panel.scenes[0].review_status == "Manually edited"
    assert open_project(session.root).database.load_scenes()[1][0].summary == "Reviewed summary"
    window.close()


def test_scene_selection_highlights_exact_source_range(tmp_path: Path) -> None:
    application = QApplication.instance() or QApplication([])
    text = "😀 First line\r\n\r\nRepeated.\r\n\r\nRepeated."
    source = tmp_path / "story.txt"
    source.write_bytes(text.encode("utf-8"))
    session = import_prose(create_project(tmp_path, "Story"), source)
    cut = text.rindex("Repeated.")
    scenes = [Scene(sequence=1, start=0, end=cut, text=text[:cut]),
              Scene(sequence=2, start=cut, end=len(text), text=text[cut:])]
    session.database.replace_scenes(text, scenes)
    window = MainWindow()
    window._set_session(session)
    deadline = time.monotonic() + 5
    while (window._task is not None or not window._prose_ready) and time.monotonic() < deadline:
        application.processEvents()
        time.sleep(0.01)
    assert window._prose_ready
    window.scenes_panel.list_widget.setCurrentRow(1)
    application.processEvents()
    assert window.scenes_panel.current_scene().identifier == scenes[1].identifier
    selections = window.prose_viewer.extraSelections()
    selection = selections[0].cursor
    expected = len(text[:cut].replace("\r\n", "\n").encode("utf-16-le")) // 2
    assert selection.selectionStart() == expected
    assert selection.selectedText() == "Repeated."
    assert window.prose_viewer.isReadOnly()
    (session.root / "source" / "story.txt").write_text("Changed prose", encoding="utf-8")
    window.scenes_panel.list_widget.setCurrentRow(0)
    application.processEvents()
    assert window.scenes_panel.outdated
    window.close()
