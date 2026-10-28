"""Project-local render settings, persistent jobs, and export manifests."""
from __future__ import annotations

import json
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from prose_to_video.models.video_render import RenderJob, RenderPlan, RenderSettings, ShotRenderOverride
from prose_to_video.storage.database import ProjectDatabase


class VideoRenderStore:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.db = ProjectDatabase(self.root / 'project.sqlite')

    def settings(self) -> RenderSettings:
        with closing(self.db._connect()) as con:
            self.db._check_schema(con)
            row = con.execute('SELECT data_json FROM video_render_settings WHERE singleton=1').fetchone()
            if row: return RenderSettings.model_validate_json(row[0])
        project = self.db.load()
        return RenderSettings(width=project.settings.width, height=project.settings.height,
                              frame_rate=project.settings.frame_rate,
                              output_directory='output')

    def save_settings(self, settings: RenderSettings):
        with closing(self.db._connect()) as con, con:
            self.db._check_schema(con)
            con.execute('INSERT OR REPLACE INTO video_render_settings VALUES(1,?)',
                        (settings.model_dump_json(),))

    def shot_override(self, shot_id: str) -> ShotRenderOverride | None:
        with closing(self.db._connect()) as con:
            self.db._check_schema(con)
            row = con.execute('SELECT data_json FROM render_shot_override WHERE shot_id=?', (shot_id,)).fetchone()
            return ShotRenderOverride.model_validate_json(row[0]) if row else None

    def save_shot_override(self, shot_id: str, override: ShotRenderOverride):
        with closing(self.db._connect()) as con, con:
            self.db._check_schema(con)
            con.execute('INSERT OR REPLACE INTO render_shot_override VALUES(?,?)',
                        (shot_id, override.model_dump_json()))

    def save_job(self, job: RenderJob):
        with closing(self.db._connect()) as con, con:
            self.db._check_schema(con)
            con.execute('INSERT OR REPLACE INTO video_render_job VALUES(?,?,?,?,?,?,?,?,?,?)',
                (job.identifier, job.project_id, job.scope, job.mode, job.status, job.output_path,
                 job.started_at.isoformat(), job.completed_at.isoformat() if job.completed_at else None,
                 job.plan_fingerprint, job.model_dump_json()))

    def jobs(self) -> list[RenderJob]:
        with closing(self.db._connect()) as con:
            self.db._check_schema(con)
            return [RenderJob.model_validate_json(row[0]) for row in con.execute(
                'SELECT data_json FROM video_render_job ORDER BY started_at DESC')]

    def recover(self) -> list[RenderJob]:
        recovered = []
        for temporary in (self.root / 'video_clips' / 'render_cache').glob('*.part'):
            temporary.unlink(missing_ok=True)
        for job in self.jobs():
            if job.status in {'Pending', 'Validating', 'Rendering'}:
                job.status = 'Interrupted'
                job.completed_at = datetime.now(timezone.utc)
                job.error = 'Application exited before render completed; retry is safe'
                self.save_job(job)
                target = Path(job.output_path)
                for suffix in ('.part.mp4', '.visual.mp4'):
                    target.with_name('.' + target.name + suffix).unlink(missing_ok=True)
                recovered.append(job)
        return recovered


def output_path(root: Path, settings: RenderSettings) -> Path:
    folder = Path(settings.output_directory).expanduser()
    if not folder.is_absolute(): folder = root / folder
    return folder / settings.output_filename


def export_render_manifest(plan: RenderPlan, output: Path, metadata: dict) -> Path:
    manifest = output.with_suffix('.render.json')
    if manifest.exists(): raise FileExistsError(f'Render manifest already exists: {manifest}')
    data = {'render_plan': plan.model_dump(mode='json'), 'plan_fingerprint': plan.fingerprint(),
            'output': str(output), 'output_metadata': metadata}
    temporary = manifest.with_name('.' + manifest.name + '.part')
    try:
        temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        temporary.replace(manifest)
    finally: temporary.unlink(missing_ok=True)
    return manifest
