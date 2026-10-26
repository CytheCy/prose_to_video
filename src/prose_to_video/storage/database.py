"""Single-project SQLite database with explicit, sequential schema migrations."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

from pydantic import ValidationError

from prose_to_video.models.project import Project, VideoSettings


SCHEMA_VERSION = 1
MIGRATIONS = {
    1: """
        CREATE TABLE project (
            identifier TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            format_version INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            modified_at TEXT NOT NULL,
            prose_path TEXT,
            audio_path TEXT,
            audio_duration REAL,
            audio_codec TEXT,
            audio_sample_rate INTEGER,
            audio_channels INTEGER,
            prose_word_count INTEGER NOT NULL,
            output_width INTEGER NOT NULL,
            output_height INTEGER NOT NULL,
            frame_rate INTEGER NOT NULL
        );
    """,
}


class ProjectDatabaseError(Exception):
    """A project database is missing, invalid, or cannot be written."""


class ProjectDatabase:
    def __init__(self, path: Path):
        self.path = path

    def _connect(self) -> sqlite3.Connection:
        try:
            connection = sqlite3.connect(self.path)
            connection.row_factory = sqlite3.Row
            return connection
        except sqlite3.Error as exc:
            raise ProjectDatabaseError(f"Cannot open project database: {exc}") from exc

    def initialize(self) -> None:
        if self.path.exists():
            raise ProjectDatabaseError("Project database already exists")
        try:
            with closing(self._connect()) as connection, connection:
                for version in range(1, SCHEMA_VERSION + 1):
                    # Each schema script and its user_version update commit together.
                    connection.executescript("BEGIN;" + MIGRATIONS[version] + f"PRAGMA user_version = {version}; COMMIT;")
        except (sqlite3.Error, OSError) as exc:
            self.path.unlink(missing_ok=True)
            raise ProjectDatabaseError(f"Cannot initialize project database: {exc}") from exc

    def _check_schema(self, connection: sqlite3.Connection) -> None:
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        if version != SCHEMA_VERSION:
            raise ProjectDatabaseError(
                f"Unsupported project database version {version}; expected {SCHEMA_VERSION}"
            )

    def load(self) -> Project:
        if not self.path.is_file():
            raise ProjectDatabaseError("This folder does not contain project.sqlite")
        try:
            with closing(self._connect()) as connection, connection:
                self._check_schema(connection)
                rows = connection.execute("SELECT * FROM project").fetchall()
                if len(rows) != 1:
                    raise ProjectDatabaseError("Project database must contain exactly one project")
                row = rows[0]
                return Project(
                    identifier=row["identifier"], name=row["name"],
                    format_version=row["format_version"], created_at=row["created_at"],
                    modified_at=row["modified_at"], prose_path=row["prose_path"],
                    audio_path=row["audio_path"], audio_duration=row["audio_duration"],
                    audio_codec=row["audio_codec"], audio_sample_rate=row["audio_sample_rate"],
                    audio_channels=row["audio_channels"], prose_word_count=row["prose_word_count"],
                    settings=VideoSettings(width=row["output_width"], height=row["output_height"],
                                           frame_rate=row["frame_rate"]),
                )
        except (sqlite3.Error, ValidationError) as exc:
            raise ProjectDatabaseError(f"Invalid project database: {exc}") from exc

    def save(self, project: Project) -> None:
        if not self.path.is_file():
            raise ProjectDatabaseError("Project database is missing")
        try:
            with closing(self._connect()) as connection, connection:
                self._check_schema(connection)
                existing = connection.execute("SELECT identifier FROM project").fetchall()
                if existing and (len(existing) != 1 or existing[0][0] != project.identifier):
                    raise ProjectDatabaseError("Project identifier does not match the database")
                connection.execute(
                    """INSERT INTO project VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(identifier) DO UPDATE SET
                    name=excluded.name, format_version=excluded.format_version,
                    created_at=excluded.created_at, modified_at=excluded.modified_at,
                    prose_path=excluded.prose_path, audio_path=excluded.audio_path,
                    audio_duration=excluded.audio_duration, audio_codec=excluded.audio_codec,
                    audio_sample_rate=excluded.audio_sample_rate, audio_channels=excluded.audio_channels,
                    prose_word_count=excluded.prose_word_count, output_width=excluded.output_width,
                    output_height=excluded.output_height, frame_rate=excluded.frame_rate""",
                    (project.identifier, project.name, project.format_version,
                     project.created_at.isoformat(), project.modified_at.isoformat(),
                     project.prose_path, project.audio_path, project.audio_duration,
                     project.audio_codec, project.audio_sample_rate, project.audio_channels,
                     project.prose_word_count, project.settings.width, project.settings.height,
                     project.settings.frame_rate),
                )
        except sqlite3.Error as exc:
            raise ProjectDatabaseError(f"Cannot save project database: {exc}") from exc
