"""Single-project SQLite database with explicit, sequential schema migrations."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

from pydantic import ValidationError

from prose_to_video.models.project import Project, VideoSettings
from prose_to_video.models.alignment import AlignmentRun, SceneTiming, TranscriptSegment
from prose_to_video.pipeline.text_matching import derive_scene_timings
from prose_to_video.models.scene import Scene, source_hash, validate_partition
from prose_to_video.models.shot import Storyboard, StoryboardSettings
from prose_to_video.pipeline.shot_planning import scene_definition_hash


SCHEMA_VERSION = 4
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
    2: """
        CREATE TABLE scene_analysis (
            singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
            source_hash TEXT NOT NULL
        );
        CREATE TABLE scene (
            identifier TEXT PRIMARY KEY,
            sequence INTEGER NOT NULL UNIQUE,
            start_offset INTEGER NOT NULL,
            end_offset INTEGER NOT NULL,
            data_json TEXT NOT NULL
        );
    """,
    3: """
        CREATE TABLE IF NOT EXISTS alignment_run (
            identifier TEXT PRIMARY KEY, prose_hash TEXT NOT NULL, audio_hash TEXT NOT NULL,
            audio_duration REAL NOT NULL, config_fingerprint TEXT NOT NULL,
            status TEXT NOT NULL, data_json TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS active_alignment (singleton INTEGER PRIMARY KEY CHECK(singleton=1),
            run_id TEXT NOT NULL REFERENCES alignment_run(identifier));
        CREATE TABLE IF NOT EXISTS alignment_word (run_id TEXT NOT NULL, identifier TEXT NOT NULL,
            source_start INTEGER NOT NULL, source_end INTEGER NOT NULL,
            audio_start REAL, audio_end REAL,
            PRIMARY KEY(run_id, identifier));
        CREATE INDEX IF NOT EXISTS alignment_word_source ON alignment_word(run_id, source_start, source_end);
        CREATE INDEX IF NOT EXISTS alignment_word_audio ON alignment_word(run_id, audio_start, audio_end);
        CREATE TABLE IF NOT EXISTS scene_timing (run_id TEXT NOT NULL, scene_id TEXT NOT NULL,
            source_start INTEGER NOT NULL, source_end INTEGER NOT NULL,
            audio_start REAL, audio_end REAL, status TEXT NOT NULL, data_json TEXT NOT NULL,
            PRIMARY KEY(run_id, scene_id));
        CREATE INDEX IF NOT EXISTS scene_timing_audio ON scene_timing(run_id, audio_start, audio_end);
        CREATE TABLE IF NOT EXISTS transcript_cache (audio_hash TEXT NOT NULL, config_fingerprint TEXT NOT NULL,
            data_json TEXT NOT NULL, PRIMARY KEY(audio_hash, config_fingerprint));
    """,
    4: """
        CREATE TABLE IF NOT EXISTS storyboard_settings (singleton INTEGER PRIMARY KEY CHECK(singleton=1), data_json TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS storyboard_run (identifier TEXT PRIMARY KEY, scene_id TEXT NOT NULL,
            prose_hash TEXT NOT NULL, audio_hash TEXT, alignment_id TEXT,
            scene_definition_hash TEXT NOT NULL, settings_fingerprint TEXT NOT NULL,
            model_name TEXT NOT NULL, status TEXT NOT NULL, data_json TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS active_storyboard (scene_id TEXT PRIMARY KEY, run_id TEXT NOT NULL
            REFERENCES storyboard_run(identifier));
        CREATE TABLE IF NOT EXISTS shot (run_id TEXT NOT NULL, identifier TEXT NOT NULL,
            scene_id TEXT NOT NULL, sequence INTEGER NOT NULL,
            source_start INTEGER NOT NULL, source_end INTEGER NOT NULL,
            audio_start REAL, audio_end REAL, review_status TEXT NOT NULL,
            manually_edited INTEGER NOT NULL, data_json TEXT NOT NULL,
            PRIMARY KEY(run_id, identifier), UNIQUE(run_id, sequence));
        CREATE INDEX IF NOT EXISTS shot_scene_order ON shot(scene_id, run_id, sequence);
        CREATE INDEX IF NOT EXISTS shot_audio_range ON shot(scene_id, audio_start, audio_end);
        CREATE INDEX IF NOT EXISTS shot_source_range ON shot(scene_id, source_start, source_end);
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
        while 1 <= version < SCHEMA_VERSION:
            next_version = version + 1
            connection.executescript("BEGIN;" + MIGRATIONS[next_version] +
                                     f"PRAGMA user_version = {next_version}; COMMIT;")
            version = next_version
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

    def load_scenes(self) -> tuple[str | None, list[Scene]]:
        try:
            with closing(self._connect()) as connection, connection:
                self._check_schema(connection)
                row = connection.execute("SELECT source_hash FROM scene_analysis WHERE singleton=1").fetchone()
                scenes = [Scene.model_validate_json(r[0]) for r in connection.execute(
                    "SELECT data_json FROM scene ORDER BY sequence")]
                if bool(row) != bool(scenes):
                    raise ProjectDatabaseError("Scene analysis metadata is incomplete")
                return (row[0] if row else None, scenes)
        except (sqlite3.Error, ValueError) as exc:
            raise ProjectDatabaseError(f"Cannot load scenes: {exc}") from exc

    def replace_scenes(self, text: str, scenes: list[Scene]) -> None:
        scenes = [Scene.model_validate(scene.model_dump()) for scene in scenes]
        validate_partition(text, scenes)
        digest = source_hash(text)
        try:
            with closing(self._connect()) as connection, connection:
                self._check_schema(connection)
                connection.execute("DELETE FROM scene")
                connection.execute("DELETE FROM scene_analysis")
                connection.execute("INSERT INTO scene_analysis VALUES (1, ?)", (digest,))
                connection.executemany(
                    "INSERT INTO scene VALUES (?, ?, ?, ?, ?)",
                    [(s.identifier, s.sequence, s.start, s.end, s.model_dump_json()) for s in scenes],
                )
                row = connection.execute("SELECT run_id FROM active_alignment WHERE singleton=1").fetchone()
                if row:
                    run = AlignmentRun.model_validate_json(connection.execute(
                        "SELECT data_json FROM alignment_run WHERE identifier=?", (row[0],)).fetchone()[0])
                    if run.prose_hash == digest:
                        old = {timing.scene_id: timing for timing in run.scenes}
                        timings = derive_scene_timings(scenes, run.words)
                        for i, timing in enumerate(timings):
                            previous = old.get(timing.scene_id)
                            if previous and previous.manual and (previous.source_start, previous.source_end) == (timing.source_start, timing.source_end):
                                timings[i] = previous
                        run.scenes = timings
                        self._write_run(connection, run, activate=True)
        except sqlite3.Error as exc:
            raise ProjectDatabaseError(f"Cannot save scene analysis: {exc}") from exc

    def update_scene(self, scene: Scene, text: str) -> None:
        digest, scenes = self.load_scenes()
        if digest != source_hash(text):
            raise ProjectDatabaseError("Scene analysis is outdated for this prose")
        updated = [scene if current.identifier == scene.identifier else current for current in scenes]
        if len(updated) == len(scenes) and all(a.identifier != scene.identifier for a in scenes):
            raise ProjectDatabaseError("Scene does not exist")
        self.replace_scenes(text, updated)

    def delete_scene(self, identifier: str, text: str) -> None:
        digest, scenes = self.load_scenes()
        if digest != source_hash(text):
            raise ProjectDatabaseError("Scene analysis is outdated for this prose")
        index = next((i for i, scene in enumerate(scenes) if scene.identifier == identifier), None)
        if index is None:
            raise ProjectDatabaseError("Scene does not exist")
        if len(scenes) == 1:
            raise ProjectDatabaseError("Cannot delete the only scene")
        # Deletion assigns the removed source range to a neighbor.
        target = index - 1 if index else 1
        neighbor = scenes[target]
        start = min(neighbor.start, scenes[index].start)
        end = max(neighbor.end, scenes[index].end)
        scenes[target] = neighbor.model_copy(update={
            "start": start, "end": end, "text": text[start:end], "summary": "",
            "actions": [], "boundary_reason": "Scene deleted; review description",
            "review_status": "Not reviewed",
        })
        scenes.pop(index)
        self.replace_scenes(text, [s.model_copy(update={"sequence": i}) for i, s in enumerate(scenes, 1)])

    def _write_run(self, connection: sqlite3.Connection, run: AlignmentRun, activate: bool) -> None:
        connection.execute("INSERT OR REPLACE INTO alignment_run VALUES (?, ?, ?, ?, ?, ?, ?)",
                           (run.identifier, run.prose_hash, run.audio_hash, run.audio_duration,
                            run.config_fingerprint, run.status, run.model_dump_json()))
        connection.execute("DELETE FROM alignment_word WHERE run_id=?", (run.identifier,))
        connection.executemany("INSERT INTO alignment_word VALUES (?, ?, ?, ?, ?, ?)",
                               [(run.identifier, w.identifier, w.source_start, w.source_end,
                                 w.audio_start, w.audio_end) for w in run.words])
        connection.execute("DELETE FROM scene_timing WHERE run_id=?", (run.identifier,))
        connection.executemany("INSERT INTO scene_timing VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                               [(run.identifier, t.scene_id, t.source_start, t.source_end,
                                 t.audio_start, t.audio_end, t.status, t.model_dump_json())
                                for t in run.scenes])
        if activate:
            connection.execute("INSERT OR REPLACE INTO active_alignment VALUES (1, ?)", (run.identifier,))

    def save_alignment(self, run: AlignmentRun) -> None:
        if run.status != "complete":
            raise ValueError("Only complete alignment runs can become active")
        try:
            with closing(self._connect()) as connection, connection:
                self._check_schema(connection)
                self._write_run(connection, run, activate=True)
        except sqlite3.Error as exc:
            raise ProjectDatabaseError(f"Cannot save alignment: {exc}") from exc

    def load_alignment(self, prose_hash: str | None = None, audio_hash: str | None = None) -> AlignmentRun | None:
        try:
            with closing(self._connect()) as connection, connection:
                self._check_schema(connection)
                row = connection.execute("SELECT r.data_json FROM alignment_run r JOIN active_alignment a "
                                         "ON r.identifier=a.run_id WHERE a.singleton=1").fetchone()
                if not row:
                    return None
                run = AlignmentRun.model_validate_json(row[0])
                if prose_hash is not None and run.prose_hash != prose_hash:
                    return None
                if audio_hash is not None and run.audio_hash != audio_hash:
                    return None
                return run
        except (sqlite3.Error, ValueError) as exc:
            raise ProjectDatabaseError(f"Cannot load alignment: {exc}") from exc

    def save_transcript_cache(self, audio_hash: str, fingerprint: str,
                              segments: list[TranscriptSegment], stage: str = "aligned",
                              alignment_model: str = "WhisperX language default") -> None:
        import json
        data = json.dumps({"stage": stage, "alignment_model": alignment_model,
                           "segments": [segment.model_dump() for segment in segments]}, ensure_ascii=False)
        try:
            with closing(self._connect()) as connection, connection:
                self._check_schema(connection)
                connection.execute("INSERT OR REPLACE INTO transcript_cache VALUES (?, ?, ?)",
                                   (audio_hash, fingerprint, data))
        except sqlite3.Error as exc:
            raise ProjectDatabaseError(f"Cannot cache transcript: {exc}") from exc

    def load_transcript_cache(self, audio_hash: str, fingerprint: str) -> tuple[list[TranscriptSegment], str, str] | None:
        import json
        with closing(self._connect()) as connection, connection:
            self._check_schema(connection)
            row = connection.execute("SELECT data_json FROM transcript_cache WHERE audio_hash=? AND config_fingerprint=?",
                                     (audio_hash, fingerprint)).fetchone()
            if not row:
                return None
            data = json.loads(row[0])
            if isinstance(data, list):
                return [TranscriptSegment.model_validate(item) for item in data], "aligned", "WhisperX language default"
            return ([TranscriptSegment.model_validate(item) for item in data["segments"]],
                    data["stage"], data.get("alignment_model", "WhisperX language default"))

    def words_for_scene(self, scene: Scene, prose_hash: str, audio_hash: str) -> list:
        run = self.load_alignment(prose_hash, audio_hash)
        return [w for w in run.words if scene.start <= w.source_start < scene.end] if run else []

    def prose_at_audio(self, time: float, prose_hash: str, audio_hash: str) -> tuple[int, int] | None:
        run = self.load_alignment(prose_hash, audio_hash)
        if not run:
            return None
        words = [w for w in run.words if w.audio_start is not None and w.audio_start <= time <= w.audio_end]
        if not words:
            return None
        word = words[0]
        sentence = next((s for s in run.sentences if s.source_start <= word.source_start < s.source_end), None)
        return (sentence.source_start, sentence.source_end) if sentence else (word.source_start, word.source_end)

    def audio_for_prose(self, start: int, end: int, prose_hash: str, audio_hash: str) -> tuple[float, float] | None:
        run = self.load_alignment(prose_hash, audio_hash)
        if not run:
            return None
        words = [w for w in run.words if start <= w.source_start and w.source_end <= end and w.audio_start is not None]
        return (words[0].audio_start, words[-1].audio_end) if words else None

    def correct_scene_time(self, scene_id: str, start: float, end: float,
                           prose_hash: str, audio_hash: str) -> SceneTiming:
        import math
        run = self.load_alignment(prose_hash, audio_hash)
        if not run:
            raise ValueError("No current alignment is available")
        if not math.isfinite(start) or not math.isfinite(end) or not 0 <= start < end <= run.audio_duration:
            raise ValueError("Scene times must be ordered and within the audio duration")
        index = next((i for i, t in enumerate(run.scenes) if t.scene_id == scene_id), None)
        if index is None:
            raise ValueError("Scene has no timing record")
        timing = run.scenes[index].model_copy(update={"audio_start": start, "audio_end": end,
                                                "manual": True, "status": "manual"})
        run.scenes[index] = timing
        self.save_alignment(run)
        return timing

    def invalidate_alignment(self) -> None:
        try:
            with closing(self._connect()) as connection, connection:
                self._check_schema(connection)
                connection.execute("DELETE FROM active_alignment")
        except sqlite3.Error as exc:
            raise ProjectDatabaseError(f"Cannot invalidate alignment: {exc}") from exc

    def storyboard_settings(self) -> StoryboardSettings:
        with closing(self._connect()) as connection, connection:
            self._check_schema(connection)
            row = connection.execute("SELECT data_json FROM storyboard_settings WHERE singleton=1").fetchone()
            return StoryboardSettings.model_validate_json(row[0]) if row else StoryboardSettings()

    def save_storyboard_settings(self, settings: StoryboardSettings) -> None:
        settings = StoryboardSettings.model_validate(settings.model_dump())
        try:
            with closing(self._connect()) as connection, connection:
                self._check_schema(connection)
                connection.execute("INSERT OR REPLACE INTO storyboard_settings VALUES (1, ?)", (settings.model_dump_json(),))
        except sqlite3.Error as exc:
            raise ProjectDatabaseError(f"Cannot save storyboard settings: {exc}") from exc

    def load_storyboard(self, scene_id: str) -> Storyboard | None:
        try:
            with closing(self._connect()) as connection, connection:
                self._check_schema(connection)
                row = connection.execute("SELECT r.data_json FROM storyboard_run r JOIN active_storyboard a "
                    "ON a.run_id=r.identifier WHERE a.scene_id=?", (scene_id,)).fetchone()
                return Storyboard.model_validate_json(row[0]) if row else None
        except (sqlite3.Error, ValueError) as exc:
            raise ProjectDatabaseError(f"Cannot load storyboard: {exc}") from exc

    def save_storyboard(self, board: Storyboard, text: str, *, replace: bool = False,
                        audio_duration: float | None = None) -> None:
        self.save_storyboards([board], text, replace=replace, audio_duration=audio_duration)

    def save_storyboards(self, boards: list[Storyboard], text: str, *, replace: bool = False,
                         audio_duration: float | None = None) -> None:
        """Replace the selected scene plans in one transaction."""
        if audio_duration is None:
            audio_duration = self.load().audio_duration
        boards = [Storyboard.model_validate(board.model_dump()) for board in boards]
        if len({board.scene_id for board in boards}) != len(boards):
            raise ValueError("Duplicate scene storyboard in replacement")
        digest, scenes = self.load_scenes()
        by_id = {scene.identifier: scene for scene in scenes}
        for board in boards:
            board.validate_against(text, audio_duration)
            scene = by_id.get(board.scene_id)
            if digest != source_hash(text) or scene is None or (scene.start, scene.end) != (board.scene_start, board.scene_end):
                raise ProjectDatabaseError("Scene or manuscript changed; regenerate the storyboard")
            if board.scene_definition_hash != scene_definition_hash(scene):
                raise ProjectDatabaseError("Scene boundaries changed; regenerate the storyboard")
        try:
            with closing(self._connect()) as connection, connection:
                self._check_schema(connection)
                for board in boards:
                    current = connection.execute("SELECT run_id FROM active_storyboard WHERE scene_id=?", (board.scene_id,)).fetchone()
                    if current and current[0] != board.identifier and not replace:
                        raise ProjectDatabaseError("Storyboard exists; explicit replacement is required")
                    self._write_storyboard(connection, board)
        except sqlite3.Error as exc:
            raise ProjectDatabaseError(f"Cannot save storyboard: {exc}") from exc

    def _write_storyboard(self, connection: sqlite3.Connection, board: Storyboard) -> None:
        connection.execute("INSERT OR REPLACE INTO storyboard_run VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (board.identifier, board.scene_id, board.prose_hash, board.audio_hash,
             board.alignment_id, board.scene_definition_hash, board.settings_fingerprint,
             board.model_name, board.status, board.model_dump_json()))
        connection.execute("DELETE FROM shot WHERE run_id=?", (board.identifier,))
        connection.executemany("INSERT INTO shot VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [(board.identifier, s.identifier, s.scene_id, s.sequence, s.source_start,
              s.source_end, s.audio_start, s.audio_end, s.review_status,
              int(s.manually_edited), s.model_dump_json()) for s in board.shots])
        connection.execute("INSERT OR REPLACE INTO active_storyboard VALUES (?, ?)",
                           (board.scene_id, board.identifier))

    def storyboard_state(self, board: Storyboard, text: str, alignment: AlignmentRun | None) -> str:
        if board.prose_hash != source_hash(text):
            return "Prose changed: source offsets are stale"
        _, scenes = self.load_scenes()
        scene = next((s for s in scenes if s.identifier == board.scene_id), None)
        if scene is None or board.scene_definition_hash != scene_definition_hash(scene):
            return "Scene boundaries changed: shot ranges need review"
        if board.alignment_id and (alignment is None or board.alignment_id != alignment.identifier or
                                    board.audio_hash != alignment.audio_hash):
            return "Audio alignment changed: shot timestamps are stale"
        return "Current" if board.status == "ready" else "Draft: timing needs review"
