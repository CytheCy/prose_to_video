"""Resolve storyboard coverage into absolute frame boundaries without moving narration."""
from __future__ import annotations

import hashlib
import math
from pathlib import Path
from PySide6.QtGui import QImageReader

from prose_to_video.models.video_render import (CameraFrame, PlannedSegment, RenderPlan,
    RenderSettings, ValidationReport)
from prose_to_video.storage.generated_images import ImageStore
from prose_to_video.storage.image_export import visual_timeline_manifest
from prose_to_video.storage.video_renders import VideoRenderStore
from prose_to_video.storage.generated_videos import VideoStore, hash_file, validate_video
from prose_to_video.pipeline.motion_prompt_generation import source_fingerprint, resolve_ending_image, motion_references, workflow_fingerprint
from prose_to_video.utils.prose import read_prose


def file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as source:
        for block in iter(lambda: source.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def frame_boundary(seconds: float, fps: int) -> int:
    """Round absolute times to nearest frame, with exact half frames rounded up."""
    return math.floor(seconds * fps + 0.5 + 1e-9)


def resolve_camera(label: str, settings: RenderSettings, image_size: tuple[int, int] | None,
                   report: ValidationReport, scene_id: str, shot_id: str,
                   approved: dict[str, float] | None = None) -> CameraFrame:
    if approved:
        if image_size:
            iw, ih = image_size
            base = max(settings.width / iw, settings.height / ih)
            if base * max(approved['start_zoom'], approved['end_zoom']) > 2:
                report.add('warning', 'motion_resolution', 'Approved movement exceeds safe image resolution; using static framing', scene_id, shot_id)
                return CameraFrame(fallback='Insufficient image resolution')
        return CameraFrame(kind='Custom', **approved)
    direction = {'Static': (0, 0, 0), 'Slow pan left': (-1, 0, 0),
        'Slow pan right': (1, 0, 0), 'Tilt up': (0, -1, 0), 'Tilt down': (0, 1, 0),
        'Pan up': (0, -1, 0), 'Pan down': (0, 1, 0),
        'Push in': (0, 0, 1), 'Pull back': (0, 0, -1),
        'Slow lateral': (1, 0, 0), 'Diagonal pan': (1, 1, 0),
        'Combined pan and zoom': (1, 1, 1)}
    if label not in direction:
        report.add('warning', 'motion_fallback', f'{label} cannot be reproduced from a flat still; using static framing', scene_id, shot_id)
        return CameraFrame(fallback=label)
    dx, dy, dz = direction[label]
    amount = {'None': 0, 'Subtle': 0.06, 'Moderate': 0.12, 'Strong': 0.20,
              'Custom': settings.custom_motion}[settings.motion_intensity]
    if label == 'Static' or amount == 0:
        return CameraFrame()
    # A cropped source needs spare pixels around the output. Limit upscaling to 2x.
    if image_size:
        iw, ih = image_size
        base = max(settings.width / iw, settings.height / ih) if settings.fit_policy == 'crop' else min(settings.width / iw, settings.height / ih)
        if base > 2 or base * (1 + amount) > 2:
            report.add('warning', 'motion_resolution', 'Image resolution is too low for safe movement; using static framing', scene_id, shot_id)
            return CameraFrame(fallback='Insufficient image resolution')
    if dx or dy:
        # A moving viewport needs overscan. The renderer scales to this zoom first.
        zoom = 1 + amount
        return CameraFrame(kind=label, start_x=.5 - dx * .5, end_x=.5 + dx * .5,
            start_y=.5 - dy * .5, end_y=.5 + dy * .5,
            start_zoom=zoom, end_zoom=zoom + amount/2 if dz else zoom)
    if dz > 0:
        return CameraFrame(kind=label, start_zoom=1, end_zoom=1 + amount)
    return CameraFrame(kind=label, start_zoom=1 + amount, end_zoom=1)


def build_render_plan(session, settings: RenderSettings, *, scope: str = 'full',
                      scene_id: str | None = None, shot_id: str | None = None) -> tuple[RenderPlan | None, ValidationReport]:
    report = ValidationReport()
    project = session.project
    audio = project.resolved_audio(session.root)
    if not audio or not audio.is_file():
        report.add('error', 'missing_audio', 'Imported narration file is missing')
        return None, report
    if not project.audio_duration or not math.isfinite(project.audio_duration):
        report.add('error', 'audio_duration', 'Narration duration is unavailable')
        return None, report
    scenes = session.database.load_scenes()[1]
    if not scenes:
        report.add('error', 'missing_scenes', 'No narrative scenes exist')
        return None, report
    if scope not in {'full', 'scene', 'shot'} or (scope == 'scene' and not scene_id) or (scope == 'shot' and not shot_id):
        report.add('error', 'scope', 'Select a valid shot or scene')
        return None, report
    boards = {scene.identifier: session.database.load_storyboard(scene.identifier) for scene in scenes}
    if scope == 'full':
        for scene in scenes:
            if boards[scene.identifier] is None:
                report.add('error', 'missing_storyboard', 'Scene has no storyboard', scene.identifier)
    selected = [(scene, shot, boards[scene.identifier]) for scene in scenes if boards[scene.identifier]
                for shot in boards[scene.identifier].shots
                if scope == 'full' or (scope == 'scene' and scene.identifier == scene_id) or
                (scope == 'shot' and shot.identifier == shot_id)]
    if not selected:
        report.add('error', 'empty_selection', 'No storyboard shots in selection')
        return None, report
    selected.sort(key=lambda item: (item[0].sequence, item[1].sequence))
    try:
        prose_path = project.resolved_prose(session.root)
        prose = read_prose(prose_path) if prose_path else ''
        if not prose: raise ValueError('Imported prose is missing or empty')
    except (OSError, ValueError) as exc:
        report.add('error', 'missing_prose', f'Cannot validate source prose: {exc}')
        prose = ''
    alignment = session.database.load_alignment()
    if prose:
        for scene in scenes:
            board = boards[scene.identifier]
            if board and any(item[0].identifier == scene.identifier for item in selected):
                try:
                    board.validate_against(prose, project.audio_duration)
                except ValueError as exc:
                    report.add('error', 'storyboard_invalid', str(exc), scene.identifier)
    store = ImageStore(session.root)
    render_store = VideoRenderStore(session.root)
    video_store = VideoStore(session.root)
    manifest = {row['shot_id']: row for row in visual_timeline_manifest(session, scenes)}
    entries = []
    for scene, shot, board in selected:
        sid, tid = scene.identifier, shot.identifier
        if board.status != 'ready':
            report.add('error', 'storyboard_draft', 'Storyboard timing has not been marked ready', sid, tid)
        if prose:
            state = session.database.storyboard_state(board, prose, alignment)
            if state not in {'Current', 'Draft: timing needs review'}:
                report.add('error', 'storyboard_stale', state, sid, tid)
        start, end = shot.audio_start, shot.audio_end
        if start is None or end is None or shot.timing_status != 'fully timed':
            report.add('error', 'missing_timing', 'Shot needs verified start and end times', sid, tid)
            continue
        if not 0 <= start < end <= project.audio_duration + .001:
            report.add('error', 'invalid_timing', 'Shot interval is invalid or outside narration', sid, tid)
            continue
        row = manifest[tid]
        production_mode, coverage, draft_fallback = video_store.production(tid)
        asset = store.approved(tid)
        path, source_hash, dimensions = None, None, None
        if asset:
            record = asset[0]
            candidate = (session.root / record.path).resolve()
            if not candidate.is_relative_to((session.root / 'images').resolve()):
                report.add('error', 'image_path', 'Approved image path leaves project images folder', sid, tid)
            elif not candidate.is_file():
                report.add('error' if settings.mode == 'final' else 'warning', 'missing_file', 'Approved image file is missing', sid, tid)
            else:
                reader = QImageReader(str(candidate))
                image = reader.read() if reader.canRead() else None
                if image is None or image.isNull():
                    report.add('error' if settings.mode == 'final' else 'warning', 'corrupt_image', 'Approved image cannot be decoded', sid, tid)
                elif (image.width(), image.height()) != (record.width, record.height):
                    report.add('error' if settings.mode == 'final' else 'warning', 'image_dimensions', 'Approved image dimensions differ from saved metadata', sid, tid)
                else:
                    digest = file_hash(candidate)
                    if digest != record.sha256:
                        report.add('error' if settings.mode == 'final' else 'warning', 'image_changed', 'Approved image content changed', sid, tid)
                    else:
                        path, source_hash, dimensions = str(candidate), digest, (record.width, record.height)
                        if row['freshness'] != 'Current' and production_mode != 'AI Animation':
                            report.add('error' if settings.mode == 'final' else 'warning', 'outdated_image', 'Approved image dependencies are outdated', sid, tid)
                            if settings.mode == 'draft': path = None
        else:
            report.add('error' if settings.mode == 'final' else 'warning', 'missing_image', 'No approved image', sid, tid)
        video_clips = []
        if production_mode == 'AI Animation':
            video_clips, _ = video_store.approved(tid)
            if not video_clips:
                severity = 'warning' if settings.mode == 'draft' and draft_fallback else 'error'
                report.add(severity,'missing_video','AI animation needs an approved clip or sequence',sid,tid)
            else:
                prompt = video_store.approved_prompt(tid)
                starting = store.asset(prompt.starting_image_id) if prompt else None
                ending = resolve_ending_image(session.root,prompt.ending_image_id) if prompt else None
                if not asset or not starting or asset[0].identifier!=starting.identifier:
                    report.add('error' if settings.mode=='final' else 'warning','video_start_image',
                        'Approved starting image differs from the clip source',sid,tid)
                refs=motion_references(session.root,tid,video_store.settings().use_references)
                for dependency_asset in [starting,*([ending] if ending else [])]:
                    if dependency_asset and (not (session.root/dependency_asset.path).is_file() or
                            hash_file(session.root/dependency_asset.path)!=dependency_asset.sha256):
                        report.add('error','video_source_image','Video source image changed or is missing',sid,tid)
                for ref in refs:
                    if not (session.root/ref['path']).is_file() or hash_file(session.root/ref['path'])!=ref['sha256']:
                        report.add('error','video_reference','Video reference image changed or is missing',sid,tid)
                current = source_fingerprint(shot,starting,ending,video_store.settings(),
                    [(r['identifier'],r['sha256']) for r in refs],workflow_fingerprint(session.root,video_store.settings())) if starting else None
                for clip in video_clips:
                    candidate = (session.root/clip.path).resolve()
                    if not candidate.is_relative_to((session.root/'video_clips').resolve()) or not candidate.is_file():
                        report.add('error','missing_video_file','Approved video clip is missing',sid,tid)
                    elif hash_file(candidate)!=clip.sha256:
                        report.add('error','video_changed','Approved video clip content changed',sid,tid)
                    else:
                        try:
                            meta=validate_video(candidate)
                            if (meta['width'],meta['height'],meta['frame_count'])!=(clip.width,clip.height,clip.frame_count):
                                report.add('error','video_metadata','Clip metadata differs from saved values',sid,tid)
                        except Exception as exc:report.add('error','corrupt_video',str(exc),sid,tid)
                    if not prompt or clip.prompt_id!=prompt.identifier or clip.dependency_fingerprint!=current:
                        report.add('error' if settings.mode=='final' else 'warning','outdated_video','Approved clip source dependencies changed',sid,tid)
        entries.append((scene, shot, start, end, path, source_hash, dimensions,
                        asset[0].identifier if asset and path else None,
                        asset[0].dependency_fingerprint if asset and path else None,
                        production_mode, coverage, video_clips))
    if not entries:
        return None, report
    # Visual coverage may extend across silence. This never changes spoken-word timestamps.
    starts = [e[2] for e in entries]
    ends = [e[3] for e in entries]
    for i in range(len(entries) - 1):
        if starts[i + 1] < ends[i] - .001:
            report.add('error', 'overlap', 'Shot intervals overlap', entries[i + 1][0].identifier, entries[i + 1][1].identifier)
        elif starts[i + 1] > ends[i] + .001:
            policy = selected[i][2].settings.pause_policy if settings.pause_policy == 'storyboard' else settings.pause_policy
            report.add('warning', 'visual_gap', f'{starts[i + 1]-ends[i]:.3f}s pause assigned to {policy} shot', entries[i + 1][0].identifier, entries[i + 1][1].identifier)
            if policy == 'next': starts[i + 1] = ends[i]
            else: ends[i] = starts[i + 1]
    if scope == 'full':
        if starts[0] > .001:
            report.add('warning', 'leading_silence', 'First image covers leading narration silence', entries[0][0].identifier, entries[0][1].identifier)
        if ends[-1] < project.audio_duration - .001:
            report.add('warning', 'trailing_silence', 'Last image covers trailing narration silence', entries[-1][0].identifier, entries[-1][1].identifier)
        starts[0], ends[-1] = 0, project.audio_duration
    source_start, source_end = starts[0], ends[-1]
    base_frame = frame_boundary(source_start, settings.frame_rate)
    # Full coverage uses ceiling for the final frame, so narration is never shortened.
    final_frame = math.ceil(source_end * settings.frame_rate - 1e-9)
    segments = []
    for i, (scene, shot, raw_start, raw_end, path, digest, dimensions, asset_id, dependency,
            production_mode, coverage, video_clips) in enumerate(entries):
        begin = frame_boundary(starts[i], settings.frame_rate) - base_frame
        finish = (final_frame if i == len(entries)-1 else frame_boundary(ends[i], settings.frame_rate)) - base_frame
        if finish <= begin:
            report.add('error', 'subframe_shot', 'Shot is shorter than one output frame', scene.identifier, shot.identifier)
            continue
        override = render_store.shot_override(shot.identifier)
        movement = override.camera_movement if override and override.camera_movement else shot.camera_movement
        camera = (CameraFrame() if production_mode in {'Static Image','AI Animation'} else
            resolve_camera(movement, settings, dimensions, report, scene.identifier, shot.identifier,
                override.camera_parameters if override else None))
        transition = (override.transition if override and override.transition else
            shot.transition if shot.transition in {'Crossfade','Fade through black','Fade in','Fade out'} else settings.default_transition)
        if i == 0 and transition in {'Crossfade', 'Fade through black'}:
            if (override and override.transition) or shot.transition in {'Crossfade', 'Fade through black'}:
                report.add('warning', 'first_transition', 'Opening shot cannot blend with a preceding shot; using cut', scene.identifier, shot.identifier)
            transition = 'Cut'
        transition_seconds = (override.transition_duration if override and override.transition_duration is not None
                              else settings.crossfade_duration)
        transition_frames = frame_boundary(transition_seconds, settings.frame_rate) if transition != 'Cut' else 0
        if transition != 'Cut' and transition_frames <= 0:
            report.add('error', 'transition_duration', 'Transition needs a positive duration', scene.identifier, shot.identifier)
        def add_piece(piece_begin,piece_end,kind,piece_path,piece_hash,piece_id,piece_dependency,
                      piece_camera,piece_transition,piece_transition_frames):
            segments.append(PlannedSegment(scene_id=scene.identifier,shot_id=shot.identifier,
                asset_id=piece_id,asset_kind=kind,source_path=piece_path,
                source_fingerprint=piece_hash,dependency_fingerprint=piece_dependency,
                production_mode=production_mode,source_start=raw_start,source_end=raw_end,
                start_frame=piece_begin,end_frame=piece_end,width=settings.width,height=settings.height,
                frame_rate=settings.frame_rate,fit_policy=settings.fit_policy,camera=piece_camera,
                transition=piece_transition,transition_frames=piece_transition_frames))
        if production_mode=='AI Animation' and video_clips:
            cursor=begin
            strategy=coverage.get('strategy','exact')
            for j,clip in enumerate(video_clips):
                remaining=finish-cursor
                if remaining<=0:
                    report.add('error','video_overflow','Approved clips exceed shot interval',scene.identifier,shot.identifier)
                    break
                frames=min(remaining,round(clip.duration*settings.frame_rate))
                if frames<=0:report.add('error','video_duration','Clip has no usable frames',scene.identifier,shot.identifier);break
                add_piece(cursor,cursor+frames,'video',str((session.root/clip.path).resolve()),clip.sha256,
                    clip.identifier,clip.dependency_fingerprint,CameraFrame(),transition if j==0 else 'Cut',
                    transition_frames if j==0 else 0)
                cursor+=frames
            if cursor<finish:
                if strategy in {'still_tail','camera_tail'} and path and coverage.get('tail_image_id')==asset_id:
                    tail_camera=CameraFrame() if strategy=='still_tail' else resolve_camera(movement,settings,dimensions,report,scene.identifier,shot.identifier)
                    add_piece(cursor,finish,'still',path,digest,asset_id,dependency,tail_camera,'Cut',0)
                else:report.add('error','video_coverage','Approved clips do not cover the shot; select a duration strategy',scene.identifier,shot.identifier)
        else:
            add_piece(begin,finish,'still' if path else 'placeholder',path,digest,asset_id,dependency,
                camera,transition,transition_frames)
    for i, segment in enumerate(segments):
        if i and segment.start_frame != segments[i-1].end_frame:
            report.add('error', 'frame_gap', 'Frame allocation has a gap or overlap', segment.scene_id, segment.shot_id)
        if segment.transition_frames and segment.transition != 'Cut':
            limit = (min(segment.frames, segments[i-1].frames) if i and segment.transition in
                     {'Crossfade', 'Fade through black'} else segment.frames)
            if segment.transition_frames >= limit:
                report.add('error', 'transition_too_long', 'Transition exceeds neighboring shot material', segment.scene_id, segment.shot_id)
        if i and i + 1 < len(segments):
            incoming = segment.transition_frames if segment.transition in {'Crossfade','Fade through black'} else 0
            outgoing = segments[i+1].transition_frames if segments[i+1].transition in {'Crossfade','Fade through black'} else 0
            if incoming + outgoing >= segment.frames:
                report.add('error', 'transition_overlap', 'Adjacent transitions consume this entire shot', segment.scene_id, segment.shot_id)
    if segments and segments[-1].end_frame != final_frame-base_frame:
        report.add('error', 'coverage', 'Rendered frames do not cover selected audio interval')
    plan = RenderPlan(project_id=project.identifier, scope=scope, mode=settings.mode,
        source_start=source_start, source_end=source_end, audio_path=str(audio.resolve()),
        audio_fingerprint=file_hash(audio), audio_duration=project.audio_duration,
        storyboard_ids=list(dict.fromkeys(board.identifier for _, _, board in selected)),
        settings=settings, segments=segments, issues=report.issues)
    report.issues.extend(validate_render_plan(plan).issues)
    plan.issues = report.issues
    return plan, report


def validate_render_plan(plan: RenderPlan) -> ValidationReport:
    """Check a serialized or externally supplied plan before any FFmpeg process starts."""
    report = ValidationReport()
    if not plan.segments:
        report.add('error', 'empty_plan', 'Render plan contains no visual segments')
        return report
    if plan.segments[0].start_frame != 0:
        report.add('error', 'start_frame', 'Visual frames must begin at zero')
    previous = None
    for segment in plan.segments:
        sid, tid = segment.scene_id, segment.shot_id
        if segment.frames <= 0:
            report.add('error', 'duration', 'Segment must contain at least one frame', sid, tid)
        if previous and segment.start_frame != previous.end_frame:
            report.add('error', 'coverage', 'Segment frames have a gap or overlap', sid, tid)
        if (segment.width, segment.height, segment.frame_rate) != (plan.settings.width, plan.settings.height, plan.settings.frame_rate):
            report.add('error', 'format', 'Segment output format differs from render settings', sid, tid)
        if segment.asset_kind == 'placeholder' and plan.mode == 'final':
            report.add('error', 'placeholder_final', 'Final render cannot contain placeholders', sid, tid)
        if segment.asset_kind == 'still' and (not segment.source_path or not segment.source_fingerprint):
            report.add('error', 'source', 'Still segment has no verified source image', sid, tid)
        if segment.asset_kind == 'video' and (not segment.source_path or not segment.source_fingerprint):
            report.add('error','video_source','Video segment has no verified clip',sid,tid)
        if segment.transition_frames < 0 or (segment.transition != 'Cut' and segment.transition_frames >= segment.frames):
            report.add('error', 'transition_duration', 'Transition duration is invalid for this segment', sid, tid)
        if previous and segment.transition in {'Crossfade','Fade through black'} and segment.transition_frames >= previous.frames:
            report.add('error', 'transition_duration', 'Transition exceeds preceding segment', sid, tid)
        previous = segment
    duration = plan.source_end - plan.source_start
    expected = plan.segments[-1].end_frame / plan.settings.frame_rate
    if duration <= 0 or abs(expected-duration) > 1/plan.settings.frame_rate + 1e-6:
        report.add('error', 'audio_coverage', 'Planned visual duration differs from selected narration interval')
    if plan.scope == 'full' and (abs(plan.source_start) > .001 or abs(plan.source_end-plan.audio_duration) > .001):
        report.add('error', 'full_coverage', 'Full render must include the complete narration')
    return report
