from __future__ import annotations

import shutil
import sqlite3
from pathlib import Path

import pytest
from pydantic import ValidationError

from prose_to_video.models.project import Project, VideoSettings
from prose_to_video.storage.database import ProjectDatabase, ProjectDatabaseError, SCHEMA_VERSION
from prose_to_video.storage.project_files import (PROJECT_DIRS, ProjectError, create_project,
                                                  import_prose, open_project, save_project,
                                                  save_settings)
from prose_to_video.utils.prose import count_prose


def test_create_reopen_and_schema(tmp_path: Path) -> None:
    session = create_project(tmp_path, "My Story")
    assert session.root.name == "My Story"
    assert all((session.root / folder).is_dir() for folder in PROJECT_DIRS)
    with sqlite3.connect(session.root / "project.sqlite") as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        assert connection.execute("SELECT count(*) FROM project").fetchone()[0] == 1
    reopened = open_project(session.root)
    assert reopened.project.identifier == session.project.identifier
    assert reopened.project.created_at == session.project.created_at
    assert reopened.project.settings == VideoSettings()


def test_reopen_after_moving_directory(tmp_path: Path) -> None:
    source = tmp_path / "story.md"
    source.write_text("First paragraph.\n\nSecond paragraph.", encoding="utf-8")
    session = import_prose(create_project(tmp_path, "Story"), source)
    moved_parent = tmp_path / "moved"
    moved_parent.mkdir()
    moved = moved_parent / "Story"
    shutil.move(session.root, moved)
    reopened = open_project(moved)
    assert reopened.project.prose_path == "source/story.md"
    assert reopened.project.resolved_prose(reopened.root).read_text(encoding="utf-8") == source.read_text(encoding="utf-8")
    assert reopened.file_status() == (True, False)


def test_prose_import_and_statistics(tmp_path: Path) -> None:
    source = tmp_path / "text.txt"
    content = "Hello, world!\n\nIt's a second paragraph.\n"
    source.write_text(content, encoding="utf-8")
    session = import_prose(create_project(tmp_path, "Story"), source)
    assert session.project.prose_word_count == 6
    assert count_prose(content).characters == len(content)
    assert count_prose(content).paragraphs == 2
    assert (session.root / "source/text.txt").read_text(encoding="utf-8") == content
    assert source.read_text(encoding="utf-8") == content
    assert open_project(session.root).project.prose_word_count == 6


def test_duplicate_filename_preserves_project(tmp_path: Path) -> None:
    source = tmp_path / "story.md"
    source.write_text("Original", encoding="utf-8")
    session = import_prose(create_project(tmp_path, "Story"), source)
    source.write_text("Changed", encoding="utf-8")
    with pytest.raises(ProjectError, match="already exists"):
        import_prose(session, source)
    assert (session.root / "source/story.md").read_text(encoding="utf-8") == "Original"
    assert open_project(session.root).project.prose_word_count == 1


@pytest.mark.parametrize("filename,content,error", [
    ("story.doc", b"Hello", "Unsupported"),
    ("bad.txt", b"\xff\xfe", "UTF-8"),
])
def test_invalid_prose_preserves_metadata(tmp_path: Path, filename: str, content: bytes, error: str) -> None:
    session = create_project(tmp_path, "Story")
    source = tmp_path / filename
    source.write_bytes(content)
    with pytest.raises(ProjectError, match=error):
        import_prose(session, source)
    assert open_project(session.root).project.prose_path is None
    assert list((session.root / "source").iterdir()) == []


def test_missing_files_and_invalid_projects(tmp_path: Path) -> None:
    session = create_project(tmp_path, "Story")
    source = tmp_path / "story.md"
    source.write_text("Hello", encoding="utf-8")
    session = import_prose(session, source)
    (session.root / "source/story.md").unlink()
    assert open_project(session.root).file_status() == (False, False)
    with pytest.raises(ProjectError, match="does not exist"):
        import_prose(session, tmp_path / "absent.md")
    with pytest.raises(ProjectError, match="project.sqlite"):
        open_project(tmp_path)


def test_settings_persist_and_validate(tmp_path: Path) -> None:
    session = create_project(tmp_path, "Story")
    updated = save_settings(session, VideoSettings(width=1280, height=720, frame_rate=24))
    assert open_project(updated.root).project.settings == VideoSettings(width=1280, height=720, frame_rate=24)
    assert save_project(updated).project.modified_at >= updated.project.modified_at
    with pytest.raises(ValidationError):
        VideoSettings(width=0)
    with pytest.raises(ValidationError):
        VideoSettings(frame_rate=241)
    with pytest.raises(ValidationError):
        Project.create("   ")
    with pytest.raises(ProjectError, match="already exists"):
        create_project(tmp_path, "Story")
    with pytest.raises(ProjectError, match="separators"):
        create_project(tmp_path, "../other")


def test_invalid_database_version_and_corruption(tmp_path: Path) -> None:
    session = create_project(tmp_path, "Story")
    database = ProjectDatabase(session.root / "project.sqlite")
    with sqlite3.connect(database.path) as connection:
        connection.execute("PRAGMA user_version = 999")
    with pytest.raises(ProjectError, match="version 999"):
        open_project(session.root)
    database.path.write_bytes(b"not a SQLite database")
    with pytest.raises(ProjectError, match="Invalid project database"):
        open_project(session.root)


def test_database_rejects_changed_identifier(tmp_path: Path) -> None:
    session = create_project(tmp_path, "Story")
    other = Project.create("Other")
    with pytest.raises(ProjectDatabaseError, match="identifier"):
        session.database.save(other)
    assert open_project(session.root).project.identifier == session.project.identifier


def test_failed_import_rolls_back_new_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    session = create_project(tmp_path, "Story")
    source = tmp_path / "story.txt"
    source.write_text("Hello", encoding="utf-8")

    def fail_save(self: ProjectDatabase, project: Project) -> None:
        raise ProjectDatabaseError("Disk write failed")

    monkeypatch.setattr(ProjectDatabase, "save", fail_save)
    with pytest.raises(ProjectError, match="Disk write failed"):
        import_prose(session, source)
    assert list((session.root / "source").iterdir()) == []
