"""Project lifecycle and source imports, independent of the GUI."""

from __future__ import annotations

import logging
import os
import shutil
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from pydantic import ValidationError

from prose_to_video.models.project import PROJECT_FORMAT_VERSION, Project, VideoSettings
from prose_to_video.storage.database import ProjectDatabase, ProjectDatabaseError
from prose_to_video.utils.media_info import AudioInspectionError, inspect_audio
from prose_to_video.utils.prose import count_prose, read_prose


LOGGER = logging.getLogger(__name__)
PROJECT_DIRS = ("source", "scenes", "alignment", "storyboard", "references", "images",
                "video_clips", "previews", "output")
PROSE_EXTENSIONS = {".md", ".txt"}
AUDIO_EXTENSIONS = {".mp3", ".wav", ".flac", ".m4a"}


class ProjectError(Exception):
    """Expected project or import error suitable for a short GUI message."""


@dataclass(frozen=True)
class ProjectSession:
    root: Path
    project: Project

    @property
    def database(self) -> ProjectDatabase:
        return ProjectDatabase(self.root / "project.sqlite")

    def file_status(self) -> tuple[bool, bool]:
        prose = self.project.resolved_prose(self.root)
        audio = self.project.resolved_audio(self.root)
        return (bool(prose and prose.is_file()), bool(audio and audio.is_file()))


def _fresh(project: Project, **changes: object) -> Project:
    return project.model_copy(update={**changes, "modified_at": datetime.now(timezone.utc)})


def create_project(parent: Path, name: str) -> ProjectSession:
    name = name.strip()
    if not name or name in {".", ".."} or "/" in name or "\\" in name or "\0" in name:
        raise ProjectError("Choose a nonempty project name without path separators")
    root = parent.expanduser() / name
    if root.exists():
        raise ProjectError(f"Project directory already exists: {root}")
    created = False
    try:
        root.mkdir(parents=False)
        created = True
        for directory in PROJECT_DIRS:
            (root / directory).mkdir()
        project = Project.create(name)
        database = ProjectDatabase(root / "project.sqlite")
        database.initialize()
        database.save(project)
        return ProjectSession(root.resolve(), project)
    except (OSError, ProjectDatabaseError, ValidationError) as exc:
        LOGGER.exception("Could not create project in %s", root)
        if created:
            shutil.rmtree(root, ignore_errors=True)
        raise ProjectError(f"Could not create project: {exc}") from exc


def open_project(root: Path) -> ProjectSession:
    root = root.expanduser().resolve()
    if not root.is_dir():
        raise ProjectError("Project directory does not exist or cannot be read")
    try:
        project = ProjectDatabase(root / "project.sqlite").load()
        if project.format_version != PROJECT_FORMAT_VERSION:
            raise ProjectError(f"Unsupported project format version {project.format_version}")
        return ProjectSession(root, project)
    except ProjectDatabaseError as exc:
        LOGGER.warning("Could not open project %s: %s", root, exc)
        raise ProjectError(str(exc)) from exc


def save_settings(session: ProjectSession, settings: VideoSettings) -> ProjectSession:
    updated = _fresh(session.project, settings=settings)
    try:
        session.database.save(updated)
    except ProjectDatabaseError as exc:
        raise ProjectError(str(exc)) from exc
    return ProjectSession(session.root, updated)


def save_project(session: ProjectSession) -> ProjectSession:
    updated = _fresh(session.project)
    try:
        session.database.save(updated)
    except ProjectDatabaseError as exc:
        raise ProjectError(str(exc)) from exc
    return ProjectSession(session.root, updated)


def _import_file(session: ProjectSession, source: Path, kind: str) -> ProjectSession:
    allowed = PROSE_EXTENSIONS if kind == "prose" else AUDIO_EXTENSIONS
    if source.suffix.lower() not in allowed:
        raise ProjectError(f"Unsupported {kind} file type. Choose: {', '.join(sorted(allowed))}")
    if not source.is_file():
        raise ProjectError(f"Selected {kind} file does not exist or cannot be read")
    source_dir = session.root / "source"
    if not source_dir.is_dir() or not source_dir.resolve().is_relative_to(session.root.resolve()):
        raise ProjectError("Project source directory is missing or invalid")
    destination = source_dir / source.name
    if destination.exists():
        raise ProjectError(f"A source file named {source.name} already exists in this project")

    temporary: Path | None = None
    committed = False
    try:
        with tempfile.NamedTemporaryFile(prefix=".import-", dir=source_dir, delete=False) as output:
            temporary = Path(output.name)
            with source.open("rb") as input_file:
                shutil.copyfileobj(input_file, output)
            output.flush()
            os.fsync(output.fileno())
        if kind == "prose":
            stats = count_prose(read_prose(temporary))
            changes: dict[str, object] = {
                "prose_path": f"source/{source.name}", "prose_word_count": stats.words,
            }
        else:
            info = inspect_audio(temporary)
            changes = {
                "audio_path": f"source/{source.name}", "audio_duration": info.duration,
                "audio_codec": info.codec, "audio_sample_rate": info.sample_rate,
                "audio_channels": info.channels,
            }
        if destination.exists():
            raise ProjectError(f"A source file named {source.name} already exists in this project")
        # Link fails if the destination appeared during inspection. It never overwrites.
        os.link(temporary, destination)
        committed = True
        updated = _fresh(session.project, **changes)
        session.database.save(updated)
        return ProjectSession(session.root, updated)
    except FileExistsError as exc:
        raise ProjectError(f"A source file named {source.name} already exists in this project") from exc
    except (OSError, ValueError, AudioInspectionError, ProjectDatabaseError) as exc:
        LOGGER.warning("Could not import %s %s: %s", kind, source, exc)
        if committed:
            destination.unlink(missing_ok=True)
        raise ProjectError(str(exc)) from exc
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def import_prose(session: ProjectSession, source: Path) -> ProjectSession:
    return _import_file(session, source, "prose")


def import_audio(session: ProjectSession, source: Path) -> ProjectSession:
    return _import_file(session, source, "audio")
