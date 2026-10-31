"""Deterministic technical checks and optional, explicitly invoked semantic review."""
from __future__ import annotations

from pathlib import Path
from uuid import NAMESPACE_URL, uuid5
import subprocess
import tempfile
import shutil

from prose_to_video.models.production import QualityIssue
from prose_to_video.pipeline.video_assembly import build_render_plan
from prose_to_video.pipeline.image_generation import asset_freshness
from prose_to_video.pipeline.video_generation import clip_freshness
from prose_to_video.rendering.ffmpeg_renderer import validate_output
from prose_to_video.storage.generated_images import ImageStore
from prose_to_video.storage.generated_videos import VideoStore
from prose_to_video.storage.production import ProductionStore
from prose_to_video.storage.visual_library import VisualLibrary


class QualityController:
    def __init__(self, session):
        self.session = session
        self.store = ProductionStore(session.root)

    def inspect(self, run=None, *, persist=True):
        project = self.session.project
        issues = []
        def add(category, severity, description, scene_id=None, shot_id=None, asset_id=None, code=''):
            identity = ':'.join([project.identifier, run.identifier if run else '', category,
                                 scene_id or '', shot_id or '', asset_id or '', code, description])
            issues.append(QualityIssue(identifier=str(uuid5(NAMESPACE_URL, identity)),
                run_id=run.identifier if run else None,
                project_id=project.identifier, category=category, severity=severity,
                description=description, scene_id=scene_id, shot_id=shot_id, asset_id=asset_id,
                evidence={'code': code}, suggested_correction={
                    'Timing': 'Open Storyboard', 'Visual Continuity': 'Open Visual Library',
                    'Image': 'Open Image Gallery', 'Animation': 'Open AI Animation',
                    'Render': 'Open Video Production'}.get(category, 'Review source material')))
        for warning in VisualLibrary(self.session.root).inspect_continuity():
            add('Visual Continuity', 'Error' if warning.severity == 'confirmed' else 'Warning',
                warning.message, shot_id=warning.shot_ids[0] if warning.shot_ids else None,
                code=warning.code)
        _, scenes = self.session.database.load_scenes()
        alignment = self.session.database.load_alignment()
        if not alignment:
            add('Timing','Error','Narration alignment is missing',code='missing_alignment')
        else:
            for timing in alignment.scenes:
                if timing.audio_start is None or timing.audio_end is None:
                    add('Timing','Error','Scene has unresolved narration timing',timing.scene_id,
                        code='unresolved_scene_timing')
        images = ImageStore(self.session.root)
        videos = VideoStore(self.session.root)
        for scene in scenes:
            board = self.session.database.load_storyboard(scene.identifier)
            if not board:
                add('Narrative', 'Error', 'Scene has no storyboard', scene.identifier, code='missing_storyboard')
                continue
            for shot in board.shots:
                if not shot.original_prose.strip() or not shot.visual_description.strip():
                    add('Narrative', 'Error', 'Shot has missing prose or visual description', scene.identifier,
                        shot.identifier, code='missing_narrative')
                if shot.audio_start is None or shot.audio_end is None:
                    add('Timing', 'Error', 'Shot has unresolved narration timing', scene.identifier,
                        shot.identifier, code='missing_timing')
                selected = images.approved(shot.identifier)
                if not selected:
                    add('Image', 'Error', 'Shot has no approved image', scene.identifier, shot.identifier,
                        code='missing_image')
                elif asset_freshness(self.session, scene, shot, board, selected[0]) != 'Current':
                    add('Image', 'Error', 'Approved image is missing or outdated', scene.identifier,
                        shot.identifier, selected[0].identifier, code='outdated_image')
                if videos.production(shot.identifier)[0] == 'AI Animation':
                    clips, _ = videos.approved(shot.identifier)
                    if not clips:
                        add('Animation', 'Error', 'AI Animation shot has no approved clip',
                            scene.identifier, shot.identifier, code='missing_video')
                    for clip in clips:
                        if clip_freshness(self.session, shot, clip) != 'Current':
                            add('Animation', 'Error', 'Approved clip is missing or outdated',
                                scene.identifier, shot.identifier, clip.identifier, 'outdated_video')
        settings = run.profile.render_settings if run else None
        if settings:
            try:
                plan, report = build_render_plan(self.session, settings)
            except Exception as exc:
                plan, report = None, None
                add('Render','Error',f'Could not build a render plan: {exc}',code='render_plan_failure')
            if report:
                for item in report.issues:
                    add('Timing' if item.code in {'overlap', 'coverage', 'visual_gap', 'missing_timing', 'invalid_timing'}
                        else 'Render', 'Error' if item.severity == 'error' else 'Warning', item.message,
                        item.scene_id, item.shot_id, code=item.code)
            if run.output_path:
                path = Path(run.output_path)
                if not path.is_absolute(): path = self.session.root / path
                if not plan:
                    add('Render', 'Error', 'Output cannot be checked without a valid render plan', code='no_plan')
                else:
                    try:
                        validate_output(path, plan)
                        if shutil.which('ffmpeg'):
                            checked = subprocess.run(['ffmpeg','-hide_banner','-i',str(path),'-an',
                                '-vf','blackdetect=d=0.2:pix_th=0.1','-f','null','-'],
                                capture_output=True,text=True,timeout=300)
                            if checked.returncode == 0 and 'black_start:' in checked.stderr:
                                add('Render','Warning','Potential black frame interval detected; review intentional fades',
                                    code='possible_black_frames')
                    except Exception as exc: add('Render', 'Error', str(exc), code='invalid_output')
        if persist:
            for issue in issues: self.store.save_issue(issue)
        return issues

    def _review_budget(self, run, reviewer):
        if not run:return
        estimate=getattr(reviewer,'estimated_cost',None)
        if estimate is None and not run.profile.authorize_unknown_cost:
            raise PermissionError('Review cost is unknown; authorize unknown review costs first')
        if estimate is None and (run.profile.budget_review is not None or run.profile.budget_total is not None):
            raise PermissionError('Review cost is unknown; a finite budget cannot be verified')
        if estimate is not None and (run.profile.budget_review is not None or run.profile.budget_total is not None):
            if any(r['amount'] is None and not r['estimated'] for r in self.store.usage(run.identifier)
                   if r['category']=='review' or run.profile.budget_total is not None):
                raise PermissionError('A previous unknown charge prevents safe review budget verification')
        known=sum(r['amount'] for r in self.store.usage(run.identifier) if r['category']=='review'
                  and not r['estimated'] and r['amount'] is not None)
        if estimate is not None and run.profile.budget_review is not None and known+estimate>run.profile.budget_review:
            raise PermissionError('Review budget would be exceeded')
        if estimate is not None and run.profile.budget_total is not None and run.known_cost+estimate>run.profile.budget_total:
            raise PermissionError('Total production budget would be exceeded')

    def _record_review_usage(self, run, reviewer):
        if not run:return
        actual=getattr(reviewer,'actual_cost',None)
        self.store.record_usage(run.identifier,'review',actual,
            details={'reviewer':type(reviewer).__name__})
        if actual is None:run.unknown_cost_count+=1
        else:run.known_cost+=actual
        self.store.save_run(run)

    def semantic_image_review(self, shot, image_path: Path, reviewer, *, authorized=False, run=None):
        """Adapter contract: reviewer.review_image(path, context) -> finding dictionaries."""
        if not authorized:
            raise PermissionError('External visual review requires explicit image sharing authorization')
        if not image_path.is_file(): raise ValueError('Approved image file is missing')
        self._review_budget(run,reviewer)
        visual = VisualLibrary(self.session.root).package_for_shot(shot.identifier)
        references = [str(self.session.root / ref['asset']['path'])
                      for entity in visual['entities'] for ref in entity['references']]
        findings = reviewer.review_image(image_path, {'prose': shot.original_prose,
            'description': shot.visual_description, 'characters': shot.characters,
            'objects': shot.objects, 'entities': [{k:v for k,v in e.items() if k != 'references'}
                                              for e in visual['entities']],
            'reference_images': references, 'style': visual['style']})
        results = []
        for item in findings:
            issue = QualityIssue(project_id=self.session.project.identifier, scene_id=shot.scene_id,
                shot_id=shot.identifier, category='Image', severity=item.get('severity', 'Suggestion'),
                description=item['description'], evidence=item.get('evidence', {}),
                detection_method='AI Suggestion', status='Needs Review')
            self.store.save_issue(issue); results.append(issue)
        self._record_review_usage(run,reviewer)
        return results

    def semantic_animation_review(self, shot, clip_path: Path, starting_image: Path,
                                  motion_prompt: str, reviewer, *, authorized=False, run=None):
        """Share three representative frames only when explicitly authorized."""
        if not authorized:
            raise PermissionError('External animation review requires explicit frame sharing authorization')
        if not clip_path.is_file() or not starting_image.is_file():
            raise ValueError('Clip or starting image is missing')
        self._review_budget(run,reviewer)
        from prose_to_video.rendering.ffmpeg_renderer import probe
        metadata = probe(clip_path)
        duration = float(metadata['format']['duration'])
        with tempfile.TemporaryDirectory() as directory:
            frames = []
            for index, fraction in enumerate((.1, .5, .9)):
                path = Path(directory) / f'frame-{index}.png'
                result = subprocess.run(['ffmpeg','-v','error','-ss',f'{duration*fraction:.6f}',
                    '-i',str(clip_path),'-frames:v','1','-y',str(path)],
                    capture_output=True, text=True, timeout=60)
                if result.returncode or not path.is_file():
                    raise ValueError('Could not extract representative animation frames')
                frames.append(path)
            findings = reviewer.review_frames(frames, {'starting_image': starting_image,
                'prose': shot.original_prose, 'shot_description': shot.visual_description,
                'motion_prompt': motion_prompt,
                'style': VisualLibrary(self.session.root).package_for_shot(shot.identifier)['style']})
        results = []
        for item in findings:
            issue = QualityIssue(project_id=self.session.project.identifier, scene_id=shot.scene_id,
                shot_id=shot.identifier, category='Animation', severity=item.get('severity','Suggestion'),
                description=item['description'], evidence=item.get('evidence',{}),
                detection_method='AI Suggestion', status='Needs Review')
            self.store.save_issue(issue); results.append(issue)
        self._record_review_usage(run,reviewer)
        return results
