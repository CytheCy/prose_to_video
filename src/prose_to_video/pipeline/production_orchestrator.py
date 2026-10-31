"""Persistent, dependency-driven production using the existing stage APIs."""
from __future__ import annotations

import json
import os
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from threading import Event
from uuid import NAMESPACE_URL, uuid5

from prose_to_video.models.production import ProductionProfile, ProductionRun, ProductionTask, QualityIssue, fingerprint
from prose_to_video.models.scene import source_hash
from prose_to_video.pipeline.video_assembly import build_render_plan, file_hash
from prose_to_video.pipeline.video_rendering import run_render
from prose_to_video.pipeline.image_generation import asset_freshness, ImageGenerator
from prose_to_video.pipeline.video_generation import clip_freshness, VideoGenerator
from prose_to_video.storage.generated_images import ImageStore
from prose_to_video.storage.generated_videos import VideoStore
from prose_to_video.storage.image_prompts import PromptStore
from prose_to_video.storage.production import ProductionStore
from prose_to_video.storage.video_renders import output_path
from prose_to_video.storage.video_renders import VideoRenderStore
from prose_to_video.storage.visual_library import VisualLibrary
from prose_to_video.utils.prose import read_prose


CHECKPOINTS = {'scenes': 'Scene Analysis Approval', 'storyboard': 'Storyboard Approval',
               'entities': 'Visual Reference Approval', 'image_prompt': 'Image Prompt Approval',
               'image': 'Image Approval', 'motion_prompt': 'Motion Prompt Approval',
               'video': 'Animation Approval', 'render': 'Final Video Approval'}
PAID = {'scenes': 'language', 'storyboard': 'language', 'entities': 'language',
        'image_prompt': 'language', 'motion_prompt': 'language', 'image': 'image', 'video': 'video'}
DONE = {'Completed', 'Reused'}


def _manifest_safe(value):
    """Keep reproducibility metadata while excluding credential values."""
    if isinstance(value, dict):
        protected = {'api_key', 'access_token', 'refresh_token', 'auth_token',
                     'client_secret', 'password', 'secret', 'authorization',
                     'credential_value'}
        return {key: ('[redacted]' if key.casefold() in protected or
                      key.casefold().endswith(('_api_key', '_secret', '_password',
                                               '_access_token', '_auth_token', '_refresh_token'))
                      else _manifest_safe(item)) for key, item in value.items()}
    if isinstance(value, list):
        return [_manifest_safe(item) for item in value]
    if isinstance(value, str) and value.casefold().startswith('bearer '):
        return '[redacted]'
    return value


class ExternalPending(RuntimeError):
    """A submitted request may have incurred charges and must be reconciled."""


def failure_kind(exc: Exception) -> str:
    if isinstance(exc, ExternalPending): return 'Uncertain External'
    if isinstance(exc, PermissionError): return 'Budget'
    if isinstance(exc, (TimeoutError, BlockingIOError, ConnectionError)): return 'Transient'
    message = str(exc).casefold()
    if 'credential' in message or 'api key' in message: return 'Credentials'
    if 'unsupported' in message or 'cannot use' in message: return 'Unsupported'
    if isinstance(exc, ValueError): return 'Validation'
    return 'User Intervention'


def animation_score(shot) -> int:
    """Stable, explainable score from existing shot fields, without model calls."""
    text = ' '.join((shot.character_actions, shot.suggested_motion, shot.visual_description)).casefold()
    tokens = {'runs': 3, 'running': 3, 'walks': 2, 'walking': 2, 'moves': 2,
              'flies': 3, 'drives': 3, 'chases': 4, 'fights': 4, 'explodes': 4,
              'machine': 2, 'robot': 2, 'vehicle': 2, 'storm': 2, 'dancing': 3}
    return sum(weight for word, weight in tokens.items() if word in text)


def select_animations(shots, profile: ProductionProfile, cost_per_clip: float | None = None):
    if profile.animation_policy == 'None':
        return []
    ranked = sorted(((animation_score(s), s) for s in shots), key=lambda pair: (-pair[0], pair[1].scene_id, pair[1].sequence))
    eligible = [shot for score, shot in ranked if score > 0]
    if profile.animation_policy == 'All eligible':
        limit = len(eligible)
    else:
        fraction = {'Limited': .2, 'Moderate': .5, 'Extensive': .8}[profile.animation_policy]
        limit = max(1, round(len(eligible) * min(fraction, profile.animation_percent / 100))) if eligible else 0
    if profile.animation_max is not None:
        limit = min(limit, profile.animation_max)
    # Budget decisions happen when requests are scheduled. Truncating this list
    # would silently change a requested AI Animation shot into a still shot.
    return [s.identifier for s in eligible[:limit]]


class ProductionOrchestrator:
    def __init__(self, session, *, language_provider=None, image_provider=None,
                 video_provider=None, alignment_provider=None):
        self.session = session
        self.store = ProductionStore(session.root)
        self.language_provider = language_provider
        self.image_provider = image_provider
        self.video_provider = video_provider
        self.alignment_provider = alignment_provider
        self.cancel_event = Event()
        self.pause_event = Event()

    def _snapshot(self):
        project = self.session.project
        prose = project.resolved_prose(self.session.root)
        audio = project.resolved_audio(self.session.root)
        prose_digest = file_hash(prose) if prose and prose.is_file() else None
        audio_digest = file_hash(audio) if audio and audio.is_file() else None
        text = read_prose(prose) if prose_digest else ''
        scene_hash, scenes = self.session.database.load_scenes()
        alignment = self.session.database.load_alignment(source_hash(text), audio_digest) if prose_digest and audio_digest else None
        return prose, audio, text, prose_digest, audio_digest, scene_hash, scenes, alignment

    def graph(self, profile: ProductionProfile, run_id: str = '') -> list[ProductionTask]:
        prose, audio, text, prose_hash, audio_hash, scene_hash, scenes, alignment = self._snapshot()
        project_id = self.session.project.identifier
        tasks = []
        def add(kind, target='', deps=(), inputs=None, reused=False, scene_id=None, shot_id=None):
            key = f'{run_id}:{kind}:{target}'
            task = ProductionTask(identifier=str(uuid5(NAMESPACE_URL, key)), run_id=run_id,
                project_id=project_id, kind=kind, scene_id=scene_id, shot_id=shot_id,
                dependencies=[d.identifier for d in deps], inputs=inputs or {},
                fingerprint=fingerprint({'kind': kind, 'target': target, 'inputs': inputs or {}}),
                status='Reused' if reused else 'Pending')
            tasks.append(task)
            return task
        source = add('source', inputs={'prose': prose_hash, 'audio': audio_hash},
                     reused=bool(prose_hash and audio_hash))
        scenes_current = bool(scenes and scene_hash == source_hash(text))
        scene_task = add('scenes', deps=[source], inputs={'prose': prose_hash,
            'settings': profile.narrative_settings}, reused=scenes_current)
        aligned = bool(alignment and alignment.status == 'complete')
        align_task = add('alignment', deps=[source, scene_task], inputs={'prose': prose_hash,
            'audio': audio_hash, 'settings': profile.alignment_settings}, reused=aligned)
        entity_tasks = []
        board_tasks = []
        prompt_tasks = []
        image_tasks = []
        video_tasks = []
        image_store = ImageStore(self.session.root)
        video_store = VideoStore(self.session.root)
        prompt_store = PromptStore(self.session.root)
        library = VisualLibrary(self.session.root)
        image_settings = profile.image_settings or image_store.settings().model_dump(mode='json')
        video_settings = profile.video_settings or video_store.settings().model_dump(mode='json')
        all_shots = [s for scene in scenes for s in (self.session.database.load_storyboard(scene.identifier).shots
                     if self.session.database.load_storyboard(scene.identifier) else [])]
        selected = set(select_animations(all_shots, profile, video_store.settings().estimated_cost_per_clip))
        for scene in scenes if scenes_current else []:
            entity_ready = any(scene.identifier in library.scenes_for(e.identifier) for e in library.entities())
            entity_task = add('entities', scene.identifier, [scene_task],
                {'scene': scene.model_dump(mode='json')}, entity_ready, scene_id=scene.identifier)
            entity_tasks.append(entity_task)
            board = self.session.database.load_storyboard(scene.identifier)
            board_current = bool(board and self.session.database.storyboard_state(board, text, alignment) == 'Current'
                and board.settings.density == profile.shot_density
                and board.settings.visual_style == profile.visual_style)
            board_task = add('storyboard', scene.identifier, [scene_task, align_task],
                {'scene': scene.model_dump(mode='json'), 'alignment': alignment.identifier if alignment else None,
                 'density': profile.shot_density, 'style': profile.visual_style,
                 'board': board.model_dump(mode='json') if board_current else None}, board_current, scene_id=scene.identifier)
            board_tasks.append(board_task)
            if not board or not board_current:
                continue
            for shot in board.shots:
                prompt = prompt_store.approved(shot.identifier)
                active_prompt = prompt_store.active(shot.identifier)
                prompt_current = bool(prompt and (not active_prompt or active_prompt.identifier == prompt.identifier)
                    and not prompt_store.validate(prompt, scene, shot, board,
                    self.session.project, library))
                prompt_task = add('image_prompt', shot.identifier, [board_task, entity_task],
                    {'shot': shot.model_dump(mode='json'), 'references': [e.identifier for e in library.entities_for_shot(shot.identifier)],
                     'settings': prompt_store.settings().model_dump(mode='json')}, prompt_current,
                    scene_id=scene.identifier, shot_id=shot.identifier)
                prompt_tasks.append(prompt_task)
                if active_prompt and (not prompt or active_prompt.identifier != prompt.identifier):
                    prompt_task.status = 'Awaiting Approval'
                approved = image_store.approved(shot.identifier)
                image_current = bool(prompt_current and approved and image_settings == image_store.settings().model_dump(mode='json')
                    and asset_freshness(self.session, scene, shot, board, approved[0]) == 'Current')
                image_task = add('image', shot.identifier, [prompt_task],
                    {'prompt': prompt.identifier if prompt else None, 'settings': image_settings,
                     'approved': (approved[0].identifier, approved[0].sha256) if approved else None},
                    image_current, scene_id=scene.identifier, shot_id=shot.identifier)
                image_tasks.append(image_task)
                current_mode = video_store.production(shot.identifier)[0]
                _, clips_locked = video_store.approved(shot.identifier)
                animate = current_mode == 'AI Animation' or (shot.identifier in selected and profile.allow_mode_changes and not clips_locked)
                if not animate:
                    continue
                motion = video_store.approved_prompt(shot.identifier)
                motion_current = bool(motion and approved and motion.starting_image_id == approved[0].identifier
                    and video_settings == video_store.settings().model_dump(mode='json'))
                if motion_current:
                    from prose_to_video.pipeline.motion_prompt_generation import (
                        source_fingerprint, resolve_ending_image, motion_references, workflow_fingerprint)
                    ending = resolve_ending_image(self.session.root, motion.ending_image_id)
                    refs = motion_references(self.session.root, shot.identifier, video_store.settings().use_references)
                    motion_current = (not motion.ending_image_id or ending is not None) and (
                        motion.dependency_fingerprint == source_fingerprint(shot, approved[0], ending,
                            video_store.settings(), [(r['identifier'], r['sha256']) for r in refs],
                            workflow_fingerprint(self.session.root, video_store.settings())))
                motion_task = add('motion_prompt', shot.identifier, [image_task],
                    {'shot': shot.model_dump(mode='json'), 'image': approved[0].identifier if approved else None},
                    motion_current, scene_id=scene.identifier, shot_id=shot.identifier)
                clips, _ = video_store.approved(shot.identifier)
                clips_current = bool(clips and all(clip_freshness(self.session, shot, c) == 'Current' for c in clips))
                video_tasks.append(add('video', shot.identifier, [motion_task],
                    {'motion': motion.identifier if motion else None, 'priority': animation_score(shot),
                     'settings': video_settings,
                     'clips': [(c.identifier, c.sha256) for c in clips]},
                    clips_current, scene_id=scene.identifier, shot_id=shot.identifier))
        coverage = add('coverage', deps=[scene_task, align_task, *board_tasks, *image_tasks, *video_tasks],
                       inputs={'shots': [s.identifier for s in all_shots],
                               'dependency_fingerprints': [t.fingerprint for t in [*board_tasks,*image_tasks,*video_tasks]]}, reused=False)
        render = add('render', deps=[coverage], inputs={'settings': profile.render_settings.model_dump(mode='json'),
            'coverage': coverage.fingerprint,
            'overrides': [VideoRenderStore(self.session.root).shot_override(s.identifier).model_dump(mode='json')
                if VideoRenderStore(self.session.root).shot_override(s.identifier) else None for s in all_shots],
            'modes': [video_store.production(s.identifier) for s in all_shots]})
        add('manifest', deps=[render], inputs={'render': render.fingerprint})
        return tasks

    def readiness(self, profile: ProductionProfile):
        tasks = self.graph(profile)
        states = Counter(t.status for t in tasks)
        missing = [t.kind + (f' {t.shot_id or t.scene_id}' if t.shot_id or t.scene_id else '')
                   for t in tasks if t.status != 'Reused']
        prose, audio, _, _, _, _, scenes, alignment = self._snapshot()
        blocked = []
        if not prose or not prose.is_file(): blocked.append('Imported prose is missing')
        if not audio or not audio.is_file(): blocked.append('Imported narration is missing')
        if self.session.project.audio_duration is None: blocked.append('Narration duration is unknown')
        if alignment and alignment.scenes:
            threshold = profile.quality_thresholds.get('min_alignment_coverage', .9)
            for timing in alignment.scenes:
                board = self.session.database.load_storyboard(timing.scene_id)
                if timing.coverage < threshold and (not board or board.status != 'ready'):
                    blocked.append(f'Scene {timing.scene_id} alignment coverage is below {threshold:.0%}; validate manual timing')
        items = []
        image_store = ImageStore(self.session.root)
        video_store = VideoStore(self.session.root)
        prompt_store = PromptStore(self.session.root)
        for task in tasks:
            state = 'Ready' if task.status == 'Reused' else 'Missing'
            if task.kind == 'source' and blocked: state = 'Blocked'
            elif task.kind == 'scenes' and scenes: state = 'Outdated'
            elif task.kind == 'alignment' and self.session.database.load_alignment(): state = 'Outdated'
            elif task.kind == 'storyboard':
                board = self.session.database.load_storyboard(task.scene_id)
                if board and task.status != 'Reused': state = 'Needs Review' if board.status == 'draft' else 'Outdated'
            elif task.kind == 'image_prompt':
                if task.status != 'Reused':
                    if task.status == 'Awaiting Approval' or prompt_store.active(task.shot_id):
                        state = 'Needs Review'
                    elif prompt_store.approved(task.shot_id):
                        state = 'Outdated'
            elif task.kind == 'image':
                if task.status != 'Reused':
                    selected = image_store.approved(task.shot_id)
                    jobs = [j for j in image_store.jobs() if j.shot_id == task.shot_id]
                    state = ('Blocked' if selected and selected[1] else 'Outdated' if selected else
                             'In Progress' if any(j.status in {'Submitted','Running','Pending','Needs Reconciliation'} for j in jobs) else
                             'Failed' if any(j.status == 'Failed' for j in jobs) else 'Needs Review' if image_store.assets(task.shot_id) else 'Missing')
            elif task.kind == 'video':
                if task.status != 'Reused':
                    clips, locked = video_store.approved(task.shot_id)
                    jobs = video_store.jobs(task.shot_id)
                    state = ('Blocked' if clips and locked else 'Outdated' if clips else
                             'In Progress' if any(j.status in {'Submitted','Running','Pending','Needs Reconciliation'} for j in jobs) else
                             'Failed' if any(j.status == 'Failed' for j in jobs) else 'Needs Review' if video_store.clips(task.shot_id) else 'Missing')
            items.append({'kind': task.kind, 'scene_id': task.scene_id, 'shot_id': task.shot_id,
                          'state': state, 'automatic': state in {'Missing', 'Outdated'} and
                          profile.automation_mode != 'Manual Production' and task.kind != 'source'})
            if state == 'Blocked' and task.kind != 'source':
                blocked.append(f'{task.kind} {task.shot_id or task.scene_id} is locked or cannot be replaced automatically')
        return {'ready': not blocked, 'blocked': blocked, 'tasks': tasks,
                'counts': dict(states), 'missing': missing, 'scenes': len(scenes),
                'items': items,
                'shots': sum(len(self.session.database.load_storyboard(s.identifier).shots)
                             for s in scenes if self.session.database.load_storyboard(s.identifier)),
                'estimated_image_cost': self._estimated_cost(tasks, 'image', profile),
                'estimated_video_cost': self._estimated_cost(tasks, 'video', profile)}

    def _estimated_cost(self, tasks, kind, profile=None):
        settings = ImageStore(self.session.root).settings() if kind == 'image' else VideoStore(self.session.root).settings()
        if profile and (profile.image_settings if kind == 'image' else profile.video_settings):
            settings = type(settings).model_validate(profile.image_settings if kind == 'image' else profile.video_settings)
        unit = settings.estimated_cost_per_image * settings.candidates if kind == 'image' and settings.estimated_cost_per_image is not None else (
            settings.estimated_cost_per_clip if kind == 'video' else None)
        count = sum(t.kind == kind and t.status != 'Reused' for t in tasks)
        return unit * count if unit is not None else None

    def dry_run(self, profile: ProductionProfile):
        report = self.readiness(profile)
        report['will_run'] = [t for t in report['tasks'] if t.status != 'Reused']
        report['will_reuse'] = [t for t in report['tasks'] if t.status == 'Reused']
        report['output'] = str(output_path(self.session.root, profile.render_settings))
        report['unknown_cost_categories'] = [kind for kind in ('image', 'video')
            if any(t.kind == kind for t in report['will_run']) and report[f'estimated_{kind}_cost'] is None]
        report['provisional_workload'] = any(t.kind in {'scenes','storyboard'} and t.status != 'Reused'
                                             for t in report['tasks'])
        report['generation_workload'] = dict(Counter(t.kind for t in report['will_run'] if t.kind in PAID))
        report['selected_animation_shots'] = [t.shot_id for t in report['tasks'] if t.kind == 'video']
        report['approved_assets'] = sum(t.kind in {'image','video'} and t.status == 'Reused'
                                        for t in report['tasks'])
        report['review_tasks'] = sum(i['state'] == 'Needs Review' for i in report['items'])
        return report

    def start(self, profile: ProductionProfile, *, paid_confirmed=False):
        report = self.dry_run(profile)
        if report['blocked']:
            raise ValueError('; '.join(report['blocked']))
        if profile.automation_mode == 'Manual Production':
            raise ValueError('Manual Production does not schedule stages automatically')
        if any(t.kind in PAID for t in report['will_run']) and not paid_confirmed:
            raise ValueError('Confirm the preview plan before submitting paid work')
        if profile.image_settings:
            from prose_to_video.models.image_job import ImageGenerationSettings
            ImageStore(self.session.root).save_settings(ImageGenerationSettings.model_validate(profile.image_settings))
        if profile.video_settings:
            from prose_to_video.models.video_job import VideoGenerationSettings
            VideoStore(self.session.root).save_settings(VideoGenerationSettings.model_validate(profile.video_settings))
        self.store.save_profile(profile)
        run = ProductionRun(project_id=self.session.project.identifier, profile=profile, status='Preparing')
        self.store.save_graph(run, self.graph(profile, run.identifier))
        return run

    def _budget(self, run, category, estimate, *, per_shot=True):
        limit = {'image': run.profile.budget_image, 'video': run.profile.budget_video,
                 'language': run.profile.budget_total}.get(category)
        if estimate is None:
            if not run.profile.authorize_unknown_cost:
                raise PermissionError(f'{category} cost is unknown; explicit category authorization is required')
            if limit is not None or run.profile.budget_total is not None or (
                    category in {'image','video'} and run.profile.budget_per_shot is not None):
                raise PermissionError(f'{category} cost is unknown; a finite budget cannot be verified')
        if estimate is not None:
            unknown = any(r['amount'] is None and not r['estimated'] for r in self.store.usage(run.identifier)
                          if r['category'] == category or run.profile.budget_total is not None)
            if unknown and (limit is not None or run.profile.budget_total is not None):
                raise PermissionError('A previous unknown charge prevents safe budget verification')
            category_spent = sum(r['amount'] for r in self.store.usage(run.identifier)
                                 if r['category'] == category and not r['estimated'] and r['amount'] is not None)
            if per_shot and run.profile.budget_per_shot is not None and category in {'image', 'video'} and estimate > run.profile.budget_per_shot:
                raise PermissionError('Per-shot budget would be exceeded')
            if limit is not None and category_spent + estimate > limit + 1e-9:
                raise PermissionError(f'{category} budget would be exceeded')
            if run.profile.budget_total is not None and run.known_cost + estimate > run.profile.budget_total + 1e-9:
                raise PermissionError('Total production budget would be exceeded')

    def _provider(self, run):
        if self.language_provider:
            return self.language_provider
        from prose_to_video.pipeline.providers import AISettings, make_provider
        return make_provider(AISettings(**run.profile.narrative_settings))

    def _needs_checkpoint(self, run, kind):
        name = CHECKPOINTS.get(kind)
        if not name: return False
        requested = run.profile.approval_requirements.get(name)
        if requested is True: return True
        if requested is False and run.profile.authorize_auto_approval: return False
        return run.profile.automation_mode == 'Review at Milestones'

    def _execute(self, run, task):
        from prose_to_video.pipeline.scene_detection import detect_scenes
        from prose_to_video.pipeline.audio_alignment import align_project
        from prose_to_video.pipeline.audio_transcription import TranscriptionSettings
        from prose_to_video.pipeline.shot_planning import plan_scene
        from prose_to_video.pipeline.entity_extraction import extract_entities
        from prose_to_video.pipeline.image_prompt_generation import generate_batch
        from prose_to_video.pipeline.motion_prompt_generation import generate_motion_prompt
        from prose_to_video.pipeline.providers import AISettings
        from prose_to_video.models.shot import StoryboardSettings
        from prose_to_video.storage.video_renders import output_path
        prose, audio, text, _, _, _, scenes, alignment = self._snapshot()
        scene = next((s for s in scenes if s.identifier == task.scene_id), None)
        board = self.session.database.load_storyboard(task.scene_id) if scene else None
        shot = next((s for s in board.shots if s.identifier == task.shot_id), None) if board else None
        if task.kind == 'source':
            if not prose or not prose.is_file() or not audio or not audio.is_file(): raise ValueError('Sources are missing')
        elif task.kind == 'scenes':
            found = detect_scenes(text, self._provider(run), AISettings(**run.profile.narrative_settings), self.cancel_event)
            self.session.database.replace_scenes(text, found)
            task.output_ids = [s.identifier for s in found]
        elif task.kind == 'alignment':
            settings = TranscriptionSettings(**run.profile.alignment_settings)
            result = align_project(prose, audio, scenes, self.session.database, settings,
                                   provider=self.alignment_provider, cancel=self.cancel_event)
            task.output_ids = [result.identifier]
        elif task.kind == 'storyboard':
            settings = self.session.database.storyboard_settings()
            settings = settings.model_copy(update={'density': run.profile.shot_density,
                                                   'visual_style': run.profile.visual_style})
            planned = plan_scene(scene, text, settings, self._provider(run), alignment,
                                 model_name=AISettings(**run.profile.narrative_settings).model,
                                 cancel=self.cancel_event)
            self.session.database.save_storyboard(planned, text, replace=True)
            task.output_ids = [planned.identifier]
        elif task.kind == 'entities':
            created = extract_entities([scene], VisualLibrary(self.session.root), self._provider(run),
                                       manuscript_hash=source_hash(text), cancel=self.cancel_event)
            task.output_ids = list(created)
        elif task.kind == 'image_prompt':
            store = PromptStore(self.session.root)
            results = generate_batch([shot], {scene.identifier: scene}, {scene.identifier: board},
                VisualLibrary(self.session.root), self.session.project, store.settings(),
                self._provider(run), store, regenerate=bool(store.latest(shot.identifier)),
                cancel=self.cancel_event)
            task.output_ids = [p.identifier for p in results]
            if run.profile.authorize_auto_approval and not self._needs_checkpoint(run, task.kind):
                if results: store.restore(shot.identifier, results[-1].version)
                prompt = store.active(shot.identifier)
                store.approve(shot.identifier, prompt.version, scene, shot, board,
                              self.session.project, VisualLibrary(self.session.root))
        elif task.kind == 'image':
            selected = ImageStore(self.session.root).approved(shot.identifier)
            if selected and selected[1]:
                raise ValueError('Approved image is locked; unlock before regenerating')
            generator = ImageGenerator(self.session, self.image_provider)
            job, request, _ = generator.prepare(scene, shot, board)
            self._budget(run, 'image', job.estimated_cost)
            generator.store.save_job(job)
            result = generator.run(job, request, cancel=self.cancel_event)
            if result.status in {'Submitted', 'Running'} and result.provider_job_id:
                result = generator.reconcile(result)
            if result.status != 'Completed':
                run.unknown_cost_count += 1
                self.store.record_usage(run.identifier, 'image', None,
                    details={'job_id': result.identifier, 'status': result.status,
                             'estimated_cost': result.estimated_cost})
            if result.status in {'Submitted', 'Running', 'Needs Reconciliation'}:
                raise ExternalPending(f'Image job {result.identifier} requires provider reconciliation')
            if result.status != 'Completed': raise RuntimeError(result.error or result.status)
            assets = generator.store.assets(shot.identifier)
            task.output_ids = [a.identifier for a in assets if a.job_id == job.identifier]
            if run.profile.authorize_auto_approval and not self._needs_checkpoint(run, task.kind) and task.output_ids:
                generator.store.approve(task.output_ids[0])
            if result.actual_cost is not None: run.known_cost += result.actual_cost
            else: run.unknown_cost_count += 1
            self.store.record_usage(run.identifier, 'image', result.actual_cost,
                details={'job_id': result.identifier, 'estimated_cost': result.estimated_cost})
        elif task.kind == 'motion_prompt':
            if VideoStore(self.session.root).production(shot.identifier)[0] != 'AI Animation':
                if not run.profile.allow_mode_changes:
                    raise ValueError('Changing shot mode requires explicit profile authorization')
                VideoStore(self.session.root).set_production(shot.identifier, 'AI Animation')
            prompt = generate_motion_prompt(self.session, scene, shot, provider=self._provider(run),
                                            cancel=self.cancel_event)
            task.output_ids = [prompt.identifier]
            if run.profile.authorize_auto_approval and not self._needs_checkpoint(run, task.kind):
                VideoStore(self.session.root).approve_prompt(prompt.identifier)
        elif task.kind == 'video':
            approved_clips, clips_locked = VideoStore(self.session.root).approved(shot.identifier)
            if approved_clips and clips_locked:
                raise ValueError('Approved video clips are locked; unlock before regenerating')
            generator = VideoGenerator(self.session, self.video_provider)
            job, request = generator.prepare(scene, shot)
            self._budget(run, 'video', job.estimated_cost)
            generator.store.save_job(job)
            result = generator.run(job, request, cancel=self.cancel_event)
            if result.status in {'Submitted', 'Running'} and result.provider_job_id:
                result = generator.reconcile(result)
            if result.status != 'Completed':
                run.unknown_cost_count += 1
                self.store.record_usage(run.identifier, 'video', None,
                    details={'job_id': result.identifier, 'status': result.status,
                             'estimated_cost': result.estimated_cost})
            if result.status in {'Submitted', 'Running', 'Needs Reconciliation'}:
                raise ExternalPending(f'Video job {result.identifier} requires provider reconciliation')
            if result.status != 'Completed': raise RuntimeError(result.error or result.status)
            clips = [c for c in generator.store.clips(shot.identifier) if c.job_id == job.identifier]
            task.output_ids = [c.identifier for c in clips]
            if run.profile.authorize_auto_approval and not self._needs_checkpoint(run, task.kind) and task.output_ids:
                generator.store.approve(shot.identifier, task.output_ids)
            if result.actual_cost is not None: run.known_cost += result.actual_cost
            else: run.unknown_cost_count += 1
            self.store.record_usage(run.identifier, 'video', result.actual_cost,
                details={'job_id': result.identifier, 'estimated_cost': result.estimated_cost})
        elif task.kind == 'coverage':
            plan, report = build_render_plan(self.session, run.profile.render_settings)
            self._record_render_issues(run, report)
            for issue in report.issues:
                if issue.code == 'visual_gap':
                    gap_seconds = float(issue.message.split('s pause', 1)[0])
                    if gap_seconds > run.profile.quality_thresholds.get('max_timeline_gap', .1):
                        raise ValueError(f'{gap_seconds:.3f}s timeline gap exceeds the quality threshold')
            unresolved = sum(i.code in {'missing_timing','missing_image','missing_video',
                                         'video_coverage','placeholder_final'} for i in report.issues)
            if unresolved > run.profile.quality_thresholds.get('max_unresolved_shots', 0):
                raise ValueError(f'{unresolved} unresolved shot dependencies exceed the quality threshold')
            critical = sum(i.severity == 'Critical' and i.status not in {'Resolved','Dismissed'}
                           for i in self.store.issues() if i.run_id == run.identifier)
            if critical > run.profile.quality_thresholds.get('max_critical_issues', 0):
                raise ValueError('Unresolved critical quality issues exceed the threshold')
            if not plan or not report.ready: raise ValueError('Final visual timeline has blocking issues')
            task.output_ids = [plan.fingerprint()]
        elif task.kind == 'render':
            plan, report = build_render_plan(self.session, run.profile.render_settings)
            if not plan or not report.ready: raise ValueError('Final render plan is not ready')
            target = output_path(self.session.root, run.profile.render_settings)
            if target.exists():
                target = target.with_name(f'{target.stem}-{run.identifier[:8]}{target.suffix}')
            job = run_render(self.session, plan, target, cancel=self.cancel_event,
                             accept_warnings=bool(run.profile.quality_thresholds.get('accept_render_warnings')))
            if job.status != 'Completed': raise RuntimeError(job.error or job.status)
            duration = job.metadata.get('video', {}).get('duration')
            expected = plan.source_end - plan.source_start
            if duration is not None and abs(duration - expected) > run.profile.quality_thresholds.get('max_duration_mismatch', .12):
                raise ValueError('Validated output duration exceeds the production quality threshold')
            run.output_path = str(target.relative_to(self.session.root)) if target.is_relative_to(self.session.root) else str(target)
            task.output_ids = [job.identifier]
            run.output_artifact_ids.append(job.identifier)
        elif task.kind == 'manifest':
            data = self.manifest(run)
            target = self.session.root / 'output' / f'production-{run.identifier}.json'
            target.parent.mkdir(exist_ok=True)
            temporary = target.with_suffix('.json.part')
            temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n')
            temporary.replace(target)
            run.manifest_path = str(target.relative_to(self.session.root))
            run.output_artifact_ids.append(run.manifest_path)
            self.store.save_manifest(run.identifier, run.manifest_path, data)
            task.output_ids = [run.manifest_path]
        else:
            raise ValueError(f'Unknown production task: {task.kind}')

    def _record_render_issues(self, run, report):
        for item in report.issues:
            issue = QualityIssue(run_id=run.identifier, project_id=run.project_id,
                scene_id=item.scene_id, shot_id=item.shot_id, category='Timing' if item.code in
                {'overlap', 'coverage', 'visual_gap', 'missing_timing', 'invalid_timing'} else 'Render',
                severity='Error' if item.severity == 'error' else 'Warning',
                description=item.message, evidence={'code': item.code},
                suggested_correction='Review the relevant storyboard shot or render settings')
            self.store.save_issue(issue)

    def _fallback_video(self, run, task, reason):
        target = run.profile.fallback_animation
        if target == 'Pause': return False
        approved = ImageStore(self.session.root).approved(task.shot_id)
        clips, locked = VideoStore(self.session.root).approved(task.shot_id)
        if not approved or locked: return False
        VideoStore(self.session.root).set_production(task.shot_id, target)
        self.store.record_fallback(run.identifier, task.identifier, 'AI Animation', target, reason)
        self.store.save_issue(QualityIssue(run_id=run.identifier, project_id=run.project_id,
            scene_id=task.scene_id, shot_id=task.shot_id, category='Animation',
            severity='Warning', description=f'AI animation fell back to {target}: {reason}',
            evidence={'fallback': target}, suggested_correction='Review this shot in AI Animation'))
        task.output_ids = [approved[0].identifier]
        return True

    def _refresh_graph(self, run):
        proposed = self.graph(run.profile, run.identifier)
        self.store.sync_graph(run.identifier, proposed)
        return self.store.tasks(run.identifier)

    def _complete(self, run, tasks):
        """Validate the artifact again before recording a successful run."""
        from prose_to_video.rendering.ffmpeg_renderer import validate_output
        path = Path(run.output_path) if run.output_path else None
        if path and not path.is_absolute(): path = self.session.root / path
        manifest = self.session.root / run.manifest_path if run.manifest_path else None
        plan, report = build_render_plan(self.session, run.profile.render_settings)
        if not path or not manifest or not manifest.is_file() or not plan or not report.ready:
            raise ValueError('Final video or production manifest is missing or outdated')
        validate_output(path, plan)
        warnings = bool(self.store.fallbacks(run.identifier) or any(
            i.run_id == run.identifier and i.status not in {'Resolved','Dismissed'} and
            i.severity in {'Warning','Suggestion'} for i in self.store.issues()))
        run.status = 'Completed With Warnings' if warnings else 'Completed'
        self.store.save_run(run)
        data = self.manifest(run)
        temporary = manifest.with_suffix(manifest.suffix + '.part')
        temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n')
        temporary.replace(manifest)
        self.store.save_manifest(run.identifier, run.manifest_path, data)
        return run

    def step(self, run_id: str):
        run = self.store.run(run_id)
        if not run: raise ValueError('Production run does not exist')
        if run.status in {'Canceled', 'Completed', 'Completed With Warnings', 'Paused'}: return run
        if self.pause_event.is_set():
            run.status = 'Paused'; self.store.save_run(run); return run
        tasks = self._refresh_graph(run)
        by_id = {t.identifier: t for t in tasks}
        if any(t.status == 'Interrupted' for t in tasks):
            run.status = 'Interrupted'; self.store.save_run(run); return run
        ready = [t for t in tasks if t.status == 'Pending' and all(by_id[d].status in DONE for d in t.dependencies)]
        if not ready:
            if all(t.status in DONE for t in tasks) and run.output_path and run.manifest_path:
                try: return self._complete(run,tasks)
                except Exception as exc:
                    run.status = 'Failed'; run.error = f'Final output validation: {exc}'
                    self.store.save_run(run)
                    return run
            run.status = ('Awaiting Approval' if any(t.status == 'Awaiting Approval' for t in tasks) else 'Blocked')
            self.store.save_run(run)
            return run
        task = (max((t for t in ready if t.kind == 'video'),
                    key=lambda t: (t.inputs.get('priority', 0), t.identifier))
                if ready[0].kind == 'video' else ready[0])
        if task.kind in PAID:
            estimate = None if PAID[task.kind] == 'language' else self._estimated_cost([task], task.kind, run.profile)
            try: self._budget(run, PAID[task.kind], estimate)
            except PermissionError as exc:
                if task.kind == 'video' and self._fallback_video(run, task, str(exc)):
                    task.status = 'Completed'; self.store.save_task(task); self.store.save_run(run)
                    return run
                task.status = 'Blocked'; task.error = str(exc); task.failure_kind='Budget'; self.store.save_task(task)
                run.status = 'Blocked'; run.error = str(exc); self.store.save_run(run)
                return run
        run.status = 'Running'; run.current_stage = task.kind; self.store.save_run(run)
        task.status = 'Running'; task.attempts += 1; self.store.save_task(task)
        try:
            self._execute(run, task)
            if PAID.get(task.kind) == 'language':
                run.unknown_cost_count += 1
                self.store.record_usage(run.identifier, 'language', None, details={'task_id': task.identifier})
            task.status = 'Awaiting Approval' if self._needs_checkpoint(run, task.kind) else 'Completed'
            if task.status == 'Awaiting Approval': run.status = 'Awaiting Approval'
            task.error = None
            if run.profile.authorize_auto_approval and task.kind in CHECKPOINTS and task.status != 'Awaiting Approval':
                self.store.decide(run_id, task.identifier, CHECKPOINTS[task.kind], 'Automatic',
                                  'Fully Automatic authorized by production profile')
        except Exception as exc:
            if task.kind == 'video' and not isinstance(exc, ExternalPending) and self._fallback_video(run, task, str(exc)):
                task.status = 'Completed'; task.error = None
                self.store.save_task(task); self.store.save_run(run)
                return run
            task.error = str(exc)
            task.failure_kind = failure_kind(exc)
            if task.failure_kind == 'Transient' and task.kind not in PAID and task.attempts <= run.profile.retry_limit:
                task.status = 'Pending'
                self.store.save_task(task); self.store.save_run(run)
                return run
            task.status = 'Interrupted' if isinstance(exc, ExternalPending) else 'Failed'
            run.status = 'Interrupted' if task.status == 'Interrupted' else 'Failed'
            run.error = f'{task.kind}: {exc}'
        self.store.save_task(task); self.store.save_run(run)
        if self.cancel_event.is_set():
            run.status = 'Canceled'; self.store.save_run(run)
        elif self.pause_event.is_set():
            run.status = 'Paused'; self.store.save_run(run)
        return run

    def _batch_step(self, run_id: str):
        """Run independent tasks of one type within the profile's explicit limit."""
        run = self.store.run(run_id)
        if not run or run.status in {'Canceled','Paused'} or self.pause_event.is_set():
            return self.step(run_id)
        tasks = self._refresh_graph(run)
        by_id = {t.identifier:t for t in tasks}
        ready = [t for t in tasks if t.status == 'Pending' and
                 all(by_id[d].status in DONE for d in t.dependencies)]
        if not ready:
            return self.step(run_id)
        kind = ready[0].kind
        category = PAID.get(kind, 'cpu')
        limit = run.profile.concurrency.get(category, 1)
        if kind == 'image':limit = min(limit,ImageStore(self.session.root).settings().max_concurrent_jobs)
        if kind == 'video':limit = min(limit,VideoStore(self.session.root).settings().max_concurrent_jobs)
        if kind == 'render': limit = run.profile.concurrency['render']
        if limit <= 1 or kind in {'source','scenes','alignment','coverage','render','manifest'} or self._needs_checkpoint(run,kind):
            return self.step(run_id)
        candidates = [t for t in ready if t.kind == kind]
        if kind == 'video':
            candidates.sort(key=lambda t: (-t.inputs.get('priority', 0), t.identifier))
        selected = candidates[:limit]
        if len(selected) < 2:
            return self.step(run_id)
        if kind in PAID:
            try:
                if category in {'image','video'}:
                    each = self._estimated_cost(selected[:1], kind, run.profile)
                    for _task in selected:self._budget(run,category,each)
                    if each is not None:self._budget(run,category,each*len(selected),per_shot=False)
                    settings = (ImageStore(self.session.root).settings() if category == 'image'
                                else VideoStore(self.session.root).settings())
                    if settings.spending_cap is not None:
                        jobs = (ImageStore(self.session.root).jobs() if category == 'image'
                                else VideoStore(self.session.root).jobs())
                        committed = [j.actual_cost if j.actual_cost is not None else j.estimated_cost
                                     for j in jobs if j.status not in {'Canceled','Failed','Pending'}]
                        if each is None or any(x is None for x in committed) or sum(committed)+each*len(selected)>settings.spending_cap:
                            raise PermissionError(f'Project {category} spending cap would be exceeded')
                else:self._budget(run,category,None)
            except PermissionError as exc:
                if kind == 'video' and run.profile.fallback_animation != 'Pause' and all(
                    ImageStore(self.session.root).approved(t.shot_id) and not
                    VideoStore(self.session.root).approved(t.shot_id)[1] for t in selected):
                    for task in selected:
                        self._fallback_video(run,task,str(exc))
                        task.status='Completed';self.store.save_task(task)
                    run.status='Running';self.store.save_run(run)
                    return run
                selected[0].status='Blocked';selected[0].error=str(exc)
                selected[0].failure_kind='Budget';self.store.save_task(selected[0])
                run.status='Blocked';run.error=str(exc);self.store.save_run(run)
                return run
        run.status='Running';run.current_stage=kind;self.store.save_run(run)
        base_known, base_unknown = run.known_cost, run.unknown_cost_count
        for task in selected:
            task.status='Running';task.attempts+=1;self.store.save_task(task)
        def process(task):
            local=ProductionRun.model_validate(run.model_dump())
            try:
                self._execute(local,task)
                return task,local,None
            except Exception as exc:
                return task,local,exc
        results=[]
        with ThreadPoolExecutor(max_workers=len(selected)) as pool:
            futures=[pool.submit(process,task) for task in selected]
            for future in as_completed(futures):results.append(future.result())
        errors=[]
        for task,local,exc in results:
            run.known_cost += local.known_cost - base_known
            # Each worker began with the same run snapshot. Count its own unknown usage once.
            run.unknown_cost_count += local.unknown_cost_count - base_unknown
            if exc is None:
                task.status='Completed';task.error=None;task.failure_kind=None
                if category == 'language':
                    run.unknown_cost_count+=1
                    self.store.record_usage(run_id,'language',None,details={'task_id':task.identifier})
                if run.profile.authorize_auto_approval and kind in CHECKPOINTS:
                    self.store.decide(run_id,task.identifier,CHECKPOINTS[kind],'Automatic',
                                      'Fully Automatic authorized by production profile')
            elif kind == 'video' and not isinstance(exc,ExternalPending) and self._fallback_video(run,task,str(exc)):
                task.status='Completed';task.error=None;task.failure_kind=None
            else:
                task.error=str(exc);task.failure_kind=failure_kind(exc)
                task.status='Interrupted' if isinstance(exc,ExternalPending) else 'Failed'
                errors.append(task)
            self.store.save_task(task)
        if errors:
            run.status='Interrupted' if any(t.status=='Interrupted' for t in errors) else 'Failed'
            run.error='; '.join(f'{t.kind} {t.shot_id or t.scene_id}: {t.error}' for t in errors)
        if self.cancel_event.is_set():run.status='Canceled'
        elif self.pause_event.is_set():run.status='Paused'
        self.store.save_run(run)
        return run

    def run_until_pause(self, run_id: str, progress=None):
        while True:
            prior = self.store.run(run_id)
            if prior.status in {'Canceled', 'Paused', 'Awaiting Approval', 'Blocked', 'Completed',
                                'Completed With Warnings', 'Failed', 'Interrupted'}: return prior
            run = self._batch_step(run_id)
            if progress:
                tasks = self.store.tasks(run_id)
                stage_tasks = [t for t in tasks if t.kind == run.current_stage]
                progress(run.current_stage, sum(t.status in DONE for t in stage_tasks), len(stage_tasks))
            if run.status in {'Canceled', 'Paused', 'Awaiting Approval', 'Blocked', 'Completed',
                              'Completed With Warnings', 'Failed', 'Interrupted'}: return run

    def approve_checkpoint(self, run_id: str, task_id: str):
        run = self.store.run(run_id)
        task = next((t for t in self.store.tasks(run_id) if t.identifier == task_id), None)
        if not run or not task or task.status != 'Awaiting Approval': raise ValueError('No pending checkpoint')
        current = {t.identifier:t for t in self.graph(run.profile,run_id)}.get(task_id)
        if task.kind in {'scenes','storyboard','image_prompt','image','motion_prompt','video'} and (
                not current or current.status != 'Reused'):
            raise ValueError('Checkpoint output is missing or outdated; review the current project state')
        if task.kind == 'image_prompt' and not PromptStore(self.session.root).approved(task.shot_id):
            raise ValueError('Approve the image prompt in Image Prompts first')
        if task.kind == 'image' and not ImageStore(self.session.root).approved(task.shot_id):
            raise ValueError('Approve an image in Image Gallery first')
        if task.kind == 'motion_prompt' and not VideoStore(self.session.root).approved_prompt(task.shot_id):
            raise ValueError('Approve the motion prompt in AI Animation first')
        if task.kind == 'video' and not VideoStore(self.session.root).approved(task.shot_id)[0]:
            raise ValueError('Approve a clip in AI Animation first')
        if task.kind == 'render':
            from prose_to_video.rendering.ffmpeg_renderer import validate_output
            plan, report = build_render_plan(self.session, run.profile.render_settings)
            path = Path(run.output_path) if run.output_path else None
            if path and not path.is_absolute(): path = self.session.root / path
            if not plan or not report.ready or not path:
                raise ValueError('Final output is no longer ready for approval')
            validate_output(path, plan)
        self.store.decide(run_id, task_id, CHECKPOINTS[task.kind], 'Manual')
        task.status = 'Completed'; self.store.save_task(task)
        run.status = 'Running'; self.store.save_run(run)

    def pause(self, run_id):
        self.pause_event.set()
        run = self.store.run(run_id)
        run.status = 'Paused'; self.store.save_run(run)
        return run

    def reconcile(self, run_id):
        """Poll known external requests once; never submit a replacement request."""
        run = self.store.run(run_id)
        if not run: raise ValueError('Production run does not exist')
        ImageStore(self.session.root).recover()
        VideoStore(self.session.root).recover()
        for task in self.store.tasks(run_id):
            if task.status != 'Interrupted': continue
            if task.kind in {'image', 'video'}:
                image_job = task.kind == 'image'
                jobs = (ImageStore(self.session.root).jobs() if image_job else VideoStore(self.session.root).jobs(task.shot_id))
                jobs = [j for j in jobs if j.shot_id == task.shot_id]
                job = jobs[-1] if jobs else None
                if not job:
                    task.status = 'Pending'
                elif job.status in {'Submitted', 'Running', 'Needs Reconciliation'}:
                    if not job.provider_job_id: continue
                    generator = (ImageGenerator(self.session, self.image_provider) if image_job else
                                 VideoGenerator(self.session, self.video_provider))
                    job = generator.reconcile(job)
                if job and job.status == 'Completed':
                    if image_job:
                        assets = [a for a in ImageStore(self.session.root).assets(task.shot_id) if a.job_id == job.identifier]
                        task.output_ids = [a.identifier for a in assets]
                        if run.profile.authorize_auto_approval and not self._needs_checkpoint(run, task.kind) and assets:
                            ImageStore(self.session.root).approve(assets[0].identifier)
                    else:
                        clips = [c for c in VideoStore(self.session.root).clips(task.shot_id) if c.job_id == job.identifier]
                        task.output_ids = [c.identifier for c in clips]
                        if run.profile.authorize_auto_approval and not self._needs_checkpoint(run, task.kind) and clips:
                            VideoStore(self.session.root).approve(task.shot_id, task.output_ids)
                    task.status = 'Awaiting Approval' if self._needs_checkpoint(run, task.kind) else 'Completed'
                    earlier = [r for r in self.store.usage(run_id) if
                               json.loads(r['data_json']).get('details', {}).get('job_id') == job.identifier]
                    if earlier and earlier[-1]['amount'] is None:
                        run.unknown_cost_count = max(0, run.unknown_cost_count - 1)
                    if job.actual_cost is not None: run.known_cost += job.actual_cost
                    else: run.unknown_cost_count += 1
                    self.store.record_usage(run_id, task.kind, job.actual_cost, details={'job_id': job.identifier})
                elif job and job.status == 'Failed':
                    task.status = 'Failed'; task.error = job.error
                elif job and job.status == 'Pending':
                    task.status = 'Pending'
            elif task.kind in {'render', 'coverage', 'manifest', 'source'}:
                task.status = 'Pending'; task.error = None
            self.store.save_task(task)
        self.store.save_run(run)
        return run

    def resume(self, run_id):
        run = self.store.run(run_id)
        if run.status not in {'Paused', 'Awaiting Approval', 'Interrupted', 'Failed', 'Blocked'}:
            raise ValueError('Production cannot resume from this state')
        if run.status == 'Interrupted':
            self.reconcile(run_id)
            run = self.store.run(run_id)
        if any(t.status == 'Failed' for t in self.store.tasks(run_id)):
            raise ValueError('Resolve and explicitly retry failed tasks before resuming')
        if any(t.status in {'Interrupted', 'Running'} for t in self.store.tasks(run_id)):
            raise ValueError('Reconcile uncertain external jobs before resuming')
        self.pause_event.clear(); self.cancel_event.clear()
        run.status = 'Running'; run.error = None; self.store.save_run(run)
        return run

    def retry_failed(self, run_id, *, task_ids=None, paid_confirmed=False):
        run = self.store.run(run_id)
        if not run or run.status not in {'Failed','Blocked'}:
            raise ValueError('Production has no retryable failed state')
        targets = [t for t in self.store.tasks(run_id) if t.status in {'Failed','Blocked'}
                   and (task_ids is None or t.identifier in task_ids)]
        if not targets:raise ValueError('No failed tasks selected')
        if any(t.kind in PAID for t in targets) and not paid_confirmed:
            raise PermissionError('Confirm another potentially paid attempt before retrying')
        for task in targets:
            if task.failure_kind == 'Uncertain External':
                raise ValueError('Reconcile uncertain external requests before retrying')
            task.status='Pending';task.error=None;task.failure_kind=None
            self.store.save_task(task)
        run.status='Paused';run.error=None;self.store.save_run(run)
        return run

    def resolve_interrupted(self, run_id, task_id, *, confirmed_no_charge=False):
        """Manual recovery after checking the provider's external job state."""
        run = self.store.run(run_id)
        task = next((t for t in self.store.tasks(run_id) if t.identifier == task_id),None)
        if not run or not task or task.status != 'Interrupted':raise ValueError('No interrupted task')
        if task.kind in PAID and not confirmed_no_charge:
            raise PermissionError('Confirm the provider did not complete or charge this request')
        task.status='Pending';task.error=None;task.failure_kind=None
        self.store.save_task(task)
        run.status='Paused';self.store.save_run(run)
        return run

    def cancel(self, run_id):
        self.cancel_event.set()
        run = self.store.run(run_id)
        run.status = 'Canceled'; self.store.save_run(run)
        for task in self.store.tasks(run_id):
            if task.status in {'Pending', 'Awaiting Approval'}:
                task.status = 'Canceled'; self.store.save_task(task)
        return run

    def manifest(self, run):
        _, _, text, prose_hash, audio_hash, _, scenes, alignment = self._snapshot()
        images = ImageStore(self.session.root); videos = VideoStore(self.session.root)
        shots = []
        for scene in scenes:
            board = self.session.database.load_storyboard(scene.identifier)
            if not board: continue
            for shot in board.shots:
                image = images.approved(shot.identifier)
                clips, _ = videos.approved(shot.identifier)
                shots.append({'scene_id': scene.identifier, 'storyboard_id': board.identifier,
                    'shot_id': shot.identifier, 'timing': [shot.audio_start, shot.audio_end],
                    'mode': videos.production(shot.identifier)[0],
                    'image_id': image[0].identifier if image else None,
                    'video_ids': [c.identifier for c in clips]})
        return _manifest_safe({'production_id': run.identifier, 'project_id': run.project_id,
            'prose_hash': prose_hash, 'audio_hash': audio_hash,
            'profile': run.profile.model_dump(mode='json'), 'alignment_id': alignment.identifier if alignment else None,
            'shots': shots, 'tasks': [t.model_dump(mode='json') for t in self.store.tasks(run.identifier)],
            'approvals': self.store.decisions(run.identifier),
            'fallbacks': self.store.fallbacks(run.identifier),
            'usage': self.store.usage(run.identifier),
            'issues': [i.model_dump(mode='json') for i in self.store.issues() if i.run_id == run.identifier],
            'known_cost': run.known_cost, 'unknown_cost_count': run.unknown_cost_count,
            'output': run.output_path})
