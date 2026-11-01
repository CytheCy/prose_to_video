from __future__ import annotations

import os
import shutil
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QApplication, QDialog

from prose_to_video.gui.main_window import MainWindow
from prose_to_video.gui.project_manager import NewProjectDialog
from prose_to_video.models.scene import Scene
from prose_to_video.pipeline.providers import BoundaryProposal, SceneDescription
from prose_to_video.storage.project_files import create_project, import_prose, open_project


@pytest.fixture(autouse=True)
def isolated_app_settings(tmp_path: Path, monkeypatch) -> None:
    import prose_to_video.gui.main_window as main_window

    settings_path = tmp_path / "app-settings.ini"
    monkeypatch.setattr(main_window, "QSettings",
                        lambda *args: QSettings(str(settings_path), QSettings.Format.IniFormat))


def test_window_smoke() -> None:
    application = QApplication.instance() or QApplication([])
    window = MainWindow()
    window.show()
    application.processEvents()
    assert window.isVisible()
    assert not window.save_action.isEnabled()
    assert not window.import_prose_button.isEnabled()
    window.close()


def test_choose_source_prose_when_creating_project(tmp_path: Path, monkeypatch) -> None:
    import prose_to_video.gui.main_window as main_window
    import prose_to_video.gui.project_manager as project_manager

    application = QApplication.instance() or QApplication([])
    source = tmp_path / "chosen.md"
    source.write_text("A chosen story.\n\nIts next scene.", encoding="utf-8")
    dialog = NewProjectDialog()
    dialog.name_edit.setText("Story")
    dialog.parent_edit.setText(str(tmp_path))
    monkeypatch.setattr(project_manager.QFileDialog, "getOpenFileName",
                        lambda *args: (str(source), "Prose files (*.md *.txt)"))
    dialog._browse_prose()
    assert dialog.prose_file == source
    monkeypatch.setattr(dialog, "exec", lambda: QDialog.DialogCode.Accepted)
    monkeypatch.setattr(main_window, "NewProjectDialog", lambda parent: dialog)

    window = MainWindow()
    window._new_project()
    deadline = time.monotonic() + 5
    while (window._task is not None or not window._prose_ready) and time.monotonic() < deadline:
        application.processEvents()
        time.sleep(0.01)
    assert window._prose_ready
    assert window.session.project.prose_path == "source/chosen.md"
    assert window._source_text == source.read_text(encoding="utf-8")
    assert open_project(window.session.root).project.prose_path == "source/chosen.md"
    window.close()


def test_open_recent_persists_reopens_and_removes_missing_project(tmp_path: Path, monkeypatch) -> None:
    import prose_to_video.gui.main_window as main_window

    QApplication.instance() or QApplication([])
    settings_path = tmp_path / "settings.ini"
    monkeypatch.setattr(main_window, "QSettings",
                        lambda *args: QSettings(str(settings_path), QSettings.Format.IniFormat))
    first = create_project(tmp_path, "First")
    second = create_project(tmp_path, "Second")
    window = MainWindow()
    window._set_session(first)
    window._set_session(second)
    window._set_session(first)
    assert window._recent_projects == [str(first.root), str(second.root)]
    window.close()

    reopened = MainWindow()
    assert reopened.session.root == first.root
    assert reopened.recent_menu.isEnabled()
    assert len(reopened._recent_projects) == 2
    reopened.recent_menu.actions()[1].trigger()
    assert reopened.session.root == second.root
    assert reopened._recent_projects[0] == str(second.root)

    shutil.rmtree(first.root)
    errors = []
    monkeypatch.setattr(reopened, "_show_error", lambda title, message: errors.append((title, message)))
    reopened.recent_menu.actions()[1].trigger()
    assert errors and errors[0][0] == "Could not open project"
    assert reopened.session.root == second.root
    assert reopened._recent_projects == [str(second.root)]
    reopened.recent_menu.actions()[-1].trigger()
    assert not reopened.recent_menu.isEnabled()
    assert QSettings(str(settings_path), QSettings.Format.IniFormat).value(
        "projects/recent", [], type=list) == []
    reopened.close()


def test_startup_restores_next_available_recent_project(tmp_path: Path) -> None:
    QApplication.instance() or QApplication([])
    first = create_project(tmp_path, "First")
    settings_path = tmp_path / "app-settings.ini"
    QSettings(str(settings_path), QSettings.Format.IniFormat).setValue(
        "projects/recent", [str(tmp_path / "Missing"), str(first.root)])
    reopened = MainWindow()
    assert reopened.session.root == first.root
    assert reopened._recent_projects == [str(first.root)]
    reopened.close()


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
