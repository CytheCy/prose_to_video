"""Persisted render-job execution shared by GUI and tests."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from threading import Event
import tempfile

from prose_to_video.models.video_render import RenderJob, RenderPlan
from prose_to_video.pipeline.video_assembly import file_hash
from prose_to_video.pipeline.video_assembly import build_render_plan
from prose_to_video.rendering.ffmpeg_renderer import FFmpegRenderer, RenderCanceled, RenderFailure
from prose_to_video.storage.video_renders import VideoRenderStore, export_render_manifest


def run_render(session, plan: RenderPlan, output: Path, *, cancel: Event | None = None,
               progress=None, accept_warnings=False) -> RenderJob:
    store = VideoRenderStore(session.root)
    job = RenderJob(project_id=plan.project_id, scope=plan.scope, mode=plan.mode,
                    settings=plan.settings.model_dump(mode='json'), plan_fingerprint=plan.fingerprint(),
                    output_path=str(output))
    store.save_job(job)
    try:
        job.status = 'Validating'; store.save_job(job)
        if any(i.severity == 'error' for i in plan.issues): raise RenderFailure('Timeline has blocking validation errors')
        if any(i.severity == 'warning' for i in plan.issues) and not accept_warnings:
            raise RenderFailure('Timeline warnings require explicit approval')
        def verify_dependencies():
            current, report = build_render_plan(session, plan.settings, scope=plan.scope,
                scene_id=plan.segments[0].scene_id if plan.scope == 'scene' else None,
                shot_id=plan.segments[0].shot_id if plan.scope == 'shot' else None)
            if (current is None or not report.ready or current.fingerprint() != plan.fingerprint()
                or current.issues != plan.issues):
                raise RenderFailure('Storyboard, render settings, or source dependencies changed since validation')
        verify_dependencies()
        if file_hash(Path(plan.audio_path)) != plan.audio_fingerprint:
            raise RenderFailure('Narration source changed since plan creation')
        output.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryFile(dir=output.parent):
            pass
        if output.exists(): raise FileExistsError(f'Output already exists: {output}')
        job.status = 'Rendering'; store.save_job(job)
        metadata = FFmpegRenderer(session.root).render(plan, output, cancel=cancel,
                                                        progress=progress, accept_warnings=accept_warnings)
        verify_dependencies()
        manifest = export_render_manifest(plan, output, metadata)
        job.metadata = {'video': metadata, 'manifest': str(manifest)}
        job.status = 'Completed'
    except RenderCanceled as exc:
        job.status = 'Canceled'; job.error = str(exc)
    except Exception as exc:
        job.status = 'Failed'; job.error = str(exc)
    finally:
        job.completed_at = datetime.now(timezone.utc)
        store.save_job(job)
    return job
