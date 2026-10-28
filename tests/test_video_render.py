"""Stage Eight: absolute timing, explicit draft gaps, caching, jobs, and real FFmpeg."""
from __future__ import annotations
from prose_to_video.storage.database import SCHEMA_VERSION

import math
import os
import shutil
import sqlite3
import struct
import wave
from pathlib import Path
from threading import Event

import pytest
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from PySide6.QtGui import QImage, QColor
from PySide6.QtWidgets import QApplication

from prose_to_video.models.scene import Scene, source_hash
from prose_to_video.models.shot import Shot, Storyboard, StoryboardSettings
from prose_to_video.models.video_render import CameraFrame, PlannedSegment, RenderPlan, RenderSettings, RenderJob
from prose_to_video.pipeline.shot_planning import scene_definition_hash
from prose_to_video.pipeline.video_assembly import build_render_plan, frame_boundary, file_hash, resolve_camera, validate_render_plan
from prose_to_video.pipeline.video_rendering import run_render
from prose_to_video.rendering.ffmpeg_renderer import FFmpegRenderer, RenderCanceled, image_filter, probe, validate_output
from prose_to_video.storage.project_files import create_project, import_audio, import_prose
from prose_to_video.storage.video_renders import VideoRenderStore
from prose_to_video.gui.video_production import RenderSettingsDialog, VideoProductionPanel


def make_wav(path, seconds=2):
    with wave.open(str(path), 'wb') as out:
        out.setnchannels(1);out.setsampwidth(2);out.setframerate(16000)
        out.writeframes(b''.join(struct.pack('<h', int(4000*math.sin(2*math.pi*220*i/16000)))
                                 for i in range(int(seconds*16000))))


def project_with_timing(tmp_path):
    text='First view. Second view.'
    source=tmp_path/'story.txt';source.write_text(text)
    audio=tmp_path/'voice.wav';make_wav(audio)
    session=import_audio(import_prose(create_project(tmp_path,'Video story'),source),audio)
    cut=text.index('Second')
    scenes=[Scene(sequence=1,start=0,end=cut,text=text[:cut]),
            Scene(sequence=2,start=cut,end=len(text),text=text[cut:])]
    session.database.replace_scenes(text,scenes)
    settings=StoryboardSettings(minimum=.1,maximum=10)
    for scene,start,end in zip(scenes,[.2,1.2],[.8,1.8]):
        shot=Shot(scene_id=scene.identifier,sequence=1,title='View',source_start=scene.start,
            source_end=scene.end,original_prose=scene.text,audio_start=start,audio_end=end,
            visual_description='An illustration',timing_status='fully timed')
        board=Storyboard(scene_id=scene.identifier,scene_start=scene.start,scene_end=scene.end,
            scene_hash=source_hash(scene.text),prose_hash=source_hash(text),
            scene_definition_hash=scene_definition_hash(scene),model_name='test',settings=settings,
            settings_fingerprint=settings.fingerprint(),coverage_start=start,coverage_end=end,
            status='ready',shots=[shot])
        session.database.save_storyboard(board,text)
    return session,scenes


def synthetic_plan(tmp_path, count=2, transition='Crossfade'):
    audio=tmp_path/'voice.wav';make_wav(audio,seconds=count)
    segments=[]
    for i in range(count):
        path=tmp_path/f'Visual ünicode {i}.png'
        img=QImage(320,240,QImage.Format_RGB32);img.fill(QColor('red' if i%2==0 else 'blue'))
        assert img.save(str(path))
        segments.append(PlannedSegment(scene_id='scene',shot_id=str(i),asset_id=str(i),
            source_path=str(path),source_fingerprint=file_hash(path),source_start=i,source_end=i+1,
            start_frame=i*30,end_frame=(i+1)*30,width=320,height=240,frame_rate=30,
            fit_policy='fit',camera=CameraFrame(kind='Push in',start_zoom=1,end_zoom=1.06)
            if i==0 else CameraFrame(kind='Slow pan right',start_x=.53,end_x=.47,
                                    start_zoom=1.06,end_zoom=1.06),
            transition=transition if i else 'Cut',transition_frames=6 if i else 0))
    return RenderPlan(project_id='test',scope='full',mode='final',source_start=0,source_end=count,
        audio_path=str(audio),audio_fingerprint=file_hash(audio),audio_duration=count,
        settings=RenderSettings(width=320,height=240,frame_rate=30,preset='ultrafast'),segments=segments)


def test_absolute_frames_do_not_accumulate_rounding():
    fps=30
    boundaries=[frame_boundary(i/7,fps) for i in range(701)]
    assert sum(b-a for a,b in zip(boundaries,boundaries[1:])) == frame_boundary(100,fps)
    assert frame_boundary(.5/fps,fps)==1


def test_serialized_plan_validation(tmp_path):
    plan=synthetic_plan(tmp_path)
    assert validate_render_plan(RenderPlan.model_validate_json(plan.model_dump_json())).ready
    plan.segments[1].start_frame+=1
    assert not validate_render_plan(plan).ready
    plan.segments[1].start_frame-=1
    plan.segments[1].asset_kind='placeholder';plan.segments[1].source_path=None
    assert any(i.code=='placeholder_final' for i in validate_render_plan(plan).issues)


def test_plan_missing_images_draft_and_silence(tmp_path):
    session,scenes=project_with_timing(tmp_path)
    settings=RenderSettings(width=320,height=240,mode='draft')
    plan,report=build_render_plan(session,settings)
    assert report.ready
    assert [s.asset_kind for s in plan.segments]==['placeholder','placeholder']
    assert [s.start_frame for s in plan.segments]==[0,36]
    assert [s.end_frame for s in plan.segments]==[36,60]
    assert {'visual_gap','leading_silence','trailing_silence','missing_image'} <= {i.code for i in report.issues}
    assert plan.segments[0].source_start==.2
    shot=plan.segments[0].shot_id
    partial,partial_report=build_render_plan(session,settings,scope='shot',shot_id=shot)
    assert partial_report.ready and partial.segments[0].start_frame==0
    assert partial.source_start==.2 and partial.source_end==.8
    final,blocked=build_render_plan(session,settings.model_copy(update={'mode':'final'}))
    assert not blocked.ready and all(s.asset_kind=='placeholder' for s in final.segments)
    assert sum(i.code=='missing_image' for i in blocked.issues)==2


def test_camera_mapping_and_aspect_filters():
    from prose_to_video.models.video_render import ValidationReport
    settings=RenderSettings(width=320,height=240,frame_rate=30,motion_intensity='Moderate')
    report=ValidationReport()
    c=resolve_camera('Push in',settings,(800,600),report,'s','x')
    assert c.end_zoom>c.start_zoom
    c=resolve_camera('Pull back',settings,(800,600),report,'s','x')
    assert c.end_zoom<c.start_zoom
    for label in ['Slow pan left','Slow pan right','Tilt up','Tilt down']:
        assert resolve_camera(label,settings,(800,600),report,'s','x').kind==label
    assert resolve_camera('Tracking',settings,(800,600),report,'s','x').kind=='Static'
    assert resolve_camera('Push in',settings,(100,100),report,'s','x').kind=='Static'
    segment=PlannedSegment(scene_id='s',shot_id='x',asset_kind='placeholder',source_start=0,source_end=1,
        start_frame=0,end_frame=30,width=320,height=240,frame_rate=30,fit_policy='fit',camera=CameraFrame())
    assert 'force_original_aspect_ratio=decrease' in image_filter(segment,'black')
    assert 'pad=' in image_filter(segment,'black')
    segment.fit_policy='crop'
    assert 'force_original_aspect_ratio=increase' in image_filter(segment,'black')
    assert 'crop=' in image_filter(segment,'black')


def test_settings_jobs_migration_and_recovery(tmp_path):
    session,_=project_with_timing(tmp_path)
    with sqlite3.connect(session.database.path) as con:
        assert con.execute('PRAGMA user_version').fetchone()[0]==SCHEMA_VERSION
    store=VideoRenderStore(session.root)
    assert store.settings().width==1920
    settings=RenderSettings(width=320,height=240,output_filename='review.mp4')
    store.save_settings(settings)
    assert store.settings()==settings
    job=RenderJob(project_id=session.project.identifier,scope='full',mode='draft',
        settings=settings.model_dump(),plan_fingerprint='abc',output_path='review.mp4',status='Rendering')
    store.save_job(job)
    assert store.recover()[0].status=='Interrupted'
    assert store.jobs()[0].status=='Interrupted'


def test_gui_settings_and_player(tmp_path):
    app=QApplication.instance() or QApplication([])
    dialog=RenderSettingsDialog(RenderSettings(width=320,height=240))
    dialog.width.setValue(640);dialog.height.setValue(480)
    assert dialog.value().width==640
    panel=VideoProductionPanel()
    assert panel.player.videoOutput()==panel.video
    panel.set_busy(True)
    assert panel.cancel_button.isEnabled()
    panel.set_busy(False)
    panel.close();dialog.close()


@pytest.mark.skipif(shutil.which('ffmpeg') is None or shutil.which('ffprobe') is None,reason='FFmpeg unavailable')
def test_real_ffmpeg_render_cache_and_validation(tmp_path):
    plan=synthetic_plan(tmp_path)
    renderer=FFmpegRenderer(tmp_path)
    out=tmp_path/'finished video ü.mp4'
    metadata=renderer.render(plan,out)
    assert out.is_file() and metadata['frames']==60
    assert metadata['video']['codec_name']=='h264' and metadata['audio']['codec_name']=='aac'
    assert (metadata['video']['width'],metadata['video']['height'])==(320,240)
    assert abs(metadata['duration']-2)<1/30
    assert len(list(renderer.cache.glob('*.mp4')))>=2
    cached={p.name:p.stat().st_mtime_ns for p in renderer.cache.glob('*.mp4')}
    renderer.render(plan,tmp_path/'second.mp4')
    assert cached=={p.name:p.stat().st_mtime_ns for p in renderer.cache.glob('*.mp4')}
    changed=plan.model_copy(deep=True)
    image=Path(changed.segments[1].source_path)
    img=QImage(320,240,QImage.Format_RGB32);img.fill(QColor('green'));img.save(str(image))
    changed.segments[1].source_fingerprint=file_hash(image)
    renderer.render(changed,tmp_path/'third.mp4')
    assert len(list(renderer.cache.glob('*.mp4')))>len(cached)
    assert cached[next(name for name in cached if name.endswith('.mp4'))] == next(p.stat().st_mtime_ns for p in renderer.cache.glob('*.mp4') if p.name==next(name for name in cached if name.endswith('.mp4')))
    renderer.clear_cache()
    assert not list(renderer.cache.glob('*.mp4'))
    assert out.exists() and image.exists()


@pytest.mark.skipif(shutil.which('ffmpeg') is None or shutil.which('ffprobe') is None,reason='FFmpeg unavailable')
def test_real_draft_project_render_and_history(tmp_path):
    session,_=project_with_timing(tmp_path)
    plan,report=build_render_plan(session,RenderSettings(width=320,height=240,preset='ultrafast',mode='draft'))
    out=session.root/'output'/'draft.mp4'
    job=run_render(session,plan,out,accept_warnings=True)
    assert job.status=='Completed',job.error
    assert out.exists() and out.with_suffix('.render.json').exists()
    assert VideoRenderStore(session.root).jobs()[0].status=='Completed'
    assert validate_output(out,plan)['frames']==60
    canceled=Event();canceled.set()
    job2=run_render(session,plan,session.root/'output'/'canceled.mp4',cancel=canceled,accept_warnings=True)
    assert job2.status=='Canceled'
    assert not (session.root/'output'/'canceled.mp4').exists()


@pytest.mark.skipif(shutil.which('ffmpeg') is None or shutil.which('ffprobe') is None,reason='FFmpeg unavailable')
def test_real_scene_preview_preserves_source_interval(tmp_path):
    session,scenes=project_with_timing(tmp_path)
    plan,report=build_render_plan(session,RenderSettings(width=320,height=240,preset='ultrafast',mode='draft'),
                                  scope='scene',scene_id=scenes[1].identifier)
    assert report.ready and plan.source_start==1.2 and plan.source_end==1.8
    assert plan.segments[0].start_frame==0
    job=run_render(session,plan,session.root/'output'/'scene.mp4',accept_warnings=True)
    assert job.status=='Completed',job.error
    assert job.metadata['video']['frames']==18
    assert abs(job.metadata['video']['audio_duration']-.6)<.1


@pytest.mark.skipif(shutil.which('ffmpeg') is None or shutil.which('ffprobe') is None,reason='FFmpeg unavailable')
@pytest.mark.parametrize('effect',['Cut','Fade through black','Fade in','Fade out'])
def test_real_transition_variants(tmp_path,effect):
    plan=synthetic_plan(tmp_path,transition='Cut')
    if effect=='Fade in': plan.segments[0].transition='Fade in';plan.segments[0].transition_frames=6
    elif effect=='Fade out': plan.segments[-1].transition='Fade out';plan.segments[-1].transition_frames=6
    else: plan.segments[1].transition=effect;plan.segments[1].transition_frames=6 if effect!='Cut' else 0
    metadata=FFmpegRenderer(tmp_path).render(plan,tmp_path/'effect.mp4')
    assert metadata['frames']==60
    assert abs(metadata['duration']-2)<1/30


@pytest.mark.skipif(shutil.which('ffmpeg') is None or shutil.which('ffprobe') is None,reason='FFmpeg unavailable')
def test_real_partial_shot_audio(tmp_path):
    plan=synthetic_plan(tmp_path,transition='Cut')
    plan.scope='shot';plan.source_start=1;plan.source_end=2
    plan.segments=[plan.segments[1]]
    plan.segments[0].start_frame=0;plan.segments[0].end_frame=30
    plan.segments[0].transition='Cut';plan.segments[0].transition_frames=0
    metadata=FFmpegRenderer(tmp_path).render(plan,tmp_path/'shot.mp4')
    assert metadata['frames']==30
    assert abs(metadata['audio_duration']-1)<.1


@pytest.mark.skipif(shutil.which('ffmpeg') is None or shutil.which('ffprobe') is None,reason='FFmpeg unavailable')
def test_many_short_segments_no_timing_drift(tmp_path):
    count=16;fps=30;duration=3.2
    audio=tmp_path/'longer.wav';make_wav(audio,duration)
    segments=[]
    for i in range(count):
        path=tmp_path/f'short {i}.png'
        img=QImage(160,120,QImage.Format_RGB32);img.fill(QColor.fromHsv((i*23)%360,220,200));img.save(str(path))
        segments.append(PlannedSegment(scene_id='s',shot_id=str(i),asset_id=str(i),
            source_path=str(path),source_fingerprint=file_hash(path),source_start=i*.2,source_end=(i+1)*.2,
            start_frame=frame_boundary(i*.2,fps),end_frame=frame_boundary((i+1)*.2,fps),
            width=160,height=120,frame_rate=fps,fit_policy='fit',camera=CameraFrame()))
    plan=RenderPlan(project_id='p',scope='full',mode='final',source_start=0,source_end=duration,
        audio_path=str(audio),audio_fingerprint=file_hash(audio),audio_duration=duration,
        settings=RenderSettings(width=160,height=120,frame_rate=fps,preset='ultrafast'),segments=segments)
    metadata=FFmpegRenderer(tmp_path).render(plan,tmp_path/'many.mp4')
    assert metadata['frames']==96
    assert abs(metadata['duration']-duration)<1/fps


def test_render_only_shot_override_and_pause_policy(tmp_path):
    from prose_to_video.models.video_render import ShotRenderOverride
    session,scenes=project_with_timing(tmp_path)
    shot=session.database.load_storyboard(scenes[1].identifier).shots[0]
    original=shot.model_dump()
    store=VideoRenderStore(session.root)
    override=ShotRenderOverride(camera_movement='Combined pan and zoom',
        transition='Crossfade',transition_duration=.2)
    store.save_shot_override(shot.identifier,override)
    settings=RenderSettings(width=320,height=240,mode='draft',pause_policy='next')
    plan,report=build_render_plan(session,settings)
    assert report.ready
    assert plan.segments[1].start_frame==24  # preceding shot ends at .8 s
    assert plan.segments[1].camera.kind=='Combined pan and zoom'
    assert plan.segments[1].camera.start_x!=plan.segments[1].camera.end_x
    assert plan.segments[1].camera.start_zoom!=plan.segments[1].camera.end_zoom
    assert plan.segments[1].transition_frames==6
    assert session.database.load_storyboard(scenes[1].identifier).shots[0].model_dump()==original
    custom=ShotRenderOverride(camera_movement='Custom',camera_parameters={
        'start_x':0,'start_y':0,'end_x':1,'end_y':1,'start_zoom':1.1,'end_zoom':1.2})
    store.save_shot_override(shot.identifier,custom)
    changed,_=build_render_plan(session,settings)
    assert changed.segments[1].camera.end_zoom==1.2
    with pytest.raises(ValueError): ShotRenderOverride(camera_parameters={'start_x':.5})


def test_corrupt_image_and_changed_dependency_report(tmp_path,monkeypatch):
    from types import SimpleNamespace
    import prose_to_video.pipeline.video_assembly as assembly
    session,scenes=project_with_timing(tmp_path)
    shot=session.database.load_storyboard(scenes[0].identifier).shots[0]
    img=session.root/'images'/'bad.png';img.write_bytes(b'not an image')
    record=SimpleNamespace(identifier='asset',path='images/bad.png',sha256=file_hash(img),
                           width=64,height=64,dependency_fingerprint='dep')
    original_approved=assembly.ImageStore.approved
    monkeypatch.setattr(assembly.ImageStore,'approved',lambda self,shot_id:(record,False) if shot_id==shot.identifier else None)
    monkeypatch.setattr(assembly,'visual_timeline_manifest',lambda session,scenes:[
        {'shot_id':shot.identifier,'freshness':'Current'},
        {'shot_id':session.database.load_storyboard(scenes[1].identifier).shots[0].identifier,'freshness':'Missing'}])
    _,final=build_render_plan(session,RenderSettings(width=320,height=240))
    assert any(i.code=='corrupt_image' and i.severity=='error' for i in final.issues)
    draft_plan,draft=build_render_plan(session,RenderSettings(width=320,height=240,mode='draft'))
    assert any(i.code=='corrupt_image' and i.severity=='warning' for i in draft.issues)
    assert draft_plan.segments[0].asset_kind=='placeholder'
    valid=QImage(64,64,QImage.Format_RGB32);valid.fill(QColor('red'));valid.save(str(img))
    record.sha256=file_hash(img)
    monkeypatch.setattr(assembly,'visual_timeline_manifest',lambda session,scenes:[
        {'shot_id':shot.identifier,'freshness':'Outdated'},
        {'shot_id':session.database.load_storyboard(scenes[1].identifier).shots[0].identifier,'freshness':'Missing'}])
    _,stale=build_render_plan(session,RenderSettings(width=320,height=240))
    assert any(i.code=='outdated_image' and i.severity=='error' for i in stale.issues)


def test_version_eight_migrates_existing_seven(tmp_path):
    from prose_to_video.storage.database import ProjectDatabase,MIGRATIONS
    path=tmp_path/'legacy.sqlite'
    with sqlite3.connect(path) as con:
        for version in range(1,8):
            con.executescript('BEGIN;'+MIGRATIONS[version]+f'PRAGMA user_version={version}; COMMIT;')
        con.execute('INSERT INTO image_prompt VALUES(?,?,?,?,?,?,?,?)',
            ('prompt','shot','scene',1,'Approved','hash','2026-01-01','{}'))
    db=ProjectDatabase(path)
    with db._connect() as con:
        db._check_schema(con)
        assert con.execute('PRAGMA user_version').fetchone()[0]==SCHEMA_VERSION
        assert con.execute('SELECT identifier FROM image_prompt').fetchone()[0]=='prompt'
        assert con.execute("SELECT name FROM sqlite_master WHERE name='video_render_job'").fetchone()
        assert con.execute("SELECT name FROM sqlite_master WHERE name='render_shot_override'").fetchone()


def test_changed_render_dependency_blocks_stale_plan(tmp_path):
    from prose_to_video.models.video_render import ShotRenderOverride
    session,scenes=project_with_timing(tmp_path)
    plan,report=build_render_plan(session,RenderSettings(width=320,height=240,mode='draft'))
    assert report.ready
    VideoRenderStore(session.root).save_shot_override(plan.segments[1].shot_id,
        ShotRenderOverride(camera_movement='Push in'))
    job=run_render(session,plan,session.root/'output'/'stale.mp4',accept_warnings=True)
    assert job.status=='Failed'
    assert 'changed since validation' in job.error
    assert not (session.root/'output'/'stale.mp4').exists()
