"""Stage Nine: workflow mapping, persistence, and synthetic video assembly."""
from __future__ import annotations
import json
import os
import shutil
import sqlite3
import subprocess
from pathlib import Path
from types import SimpleNamespace
import pytest
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
from PySide6.QtCore import QBuffer,QIODevice
from PySide6.QtGui import QImage,QColor
from PySide6.QtWidgets import QApplication
from test_video_render import project_with_timing,make_wav
from prose_to_video.models.image_prompt import ImagePrompt,PromptComponents
from prose_to_video.models.image_job import ImageJob,ProviderImage
from prose_to_video.models.generated_video import MotionPrompt
from prose_to_video.models.video_job import VideoGenerationSettings,VideoJob,VideoRequest,VideoCapabilities,VideoProviderResult,ProviderVideo
from prose_to_video.models.video_render import CameraFrame,PlannedSegment,RenderPlan,RenderSettings
from prose_to_video.pipeline.motion_prompt_generation import generate_motion_prompt,source_fingerprint
from prose_to_video.pipeline.video_generation import VideoGenerator,eligible_video_shots
from prose_to_video.pipeline.video_providers.base import VideoProvider,VideoProviderError
from prose_to_video.pipeline.video_providers.comfyui_provider import ComfyUIVideoProvider
from prose_to_video.pipeline.video_assembly import build_render_plan,file_hash
from prose_to_video.rendering.ffmpeg_renderer import FFmpegRenderer,probe
from prose_to_video.storage.database import ProjectDatabase,MIGRATIONS,SCHEMA_VERSION
from prose_to_video.storage.generated_images import ImageStore
from prose_to_video.storage.generated_videos import VideoStore,validate_video,extract_final_frame
from prose_to_video.storage.image_prompts import PromptStore
from prose_to_video.storage.video_export import video_manifest,manifest_markdown,export_approved_clips
from prose_to_video.gui.animation_panel import AnimationPanel,VideoGenerationSettingsDialog

def approved_image(session,scene,shot):
    prompt=ImagePrompt(shot_id=shot.identifier,scene_id=scene.identifier,version=1,
        positive_prompt='Starting composition',components=PromptComponents(),source_start=shot.source_start,
        source_end=shot.source_end,aspect_ratio='4:3',dependency_fingerprint='source')
    PromptStore(session.root).save(prompt)
    job=ImageJob(project_id=session.project.identifier,scene_id=scene.identifier,shot_id=shot.identifier,
        prompt_id=prompt.identifier,provider='Fake',model='fake',width=320,height=240,quality='medium',
        candidates=1,dependency_fingerprint='source')
    store=ImageStore(session.root);store.save_job(job)
    image=QImage(320,240,QImage.Format_RGB32);image.fill(QColor('red'))
    buf=QBuffer();buf.open(QIODevice.ReadWrite);assert image.save(buf,'PNG')
    asset=store.save_image(job,ProviderImage(data=bytes(buf.data()),model='fake'),1)
    store.approve(asset.identifier)
    return asset

def motion_fixture(tmp_path):
    session,scenes=project_with_timing(tmp_path)
    scene=scenes[0];shot=session.database.load_storyboard(scene.identifier).shots[0]
    image=approved_image(session,scene,shot)
    settings=VideoGenerationSettings(provider='ComfyUI',width=320,height=240,duration=.6,frame_rate=10,
        workflow_mapping={'starting_image':{'node':'start','input':'image'},'motion_prompt':{'node':'text','input':'text'},
        'width':{'node':'size','input':'width'},'height':{'node':'size','input':'height'},
        'duration':{'node':'size','input':'duration'},'frame_rate':{'node':'size','input':'fps'},
        'output_video':'output'})
    store=VideoStore(session.root);store.save_settings(settings)
    prompt=MotionPrompt(shot_id=shot.identifier,scene_id=scene.identifier,version=1,
        starting_image_id=image.identifier,positive_prompt='The view remains still.',intended_duration=.6,
        dependency_fingerprint=source_fingerprint(shot,image,None,settings))
    store.save_prompt(prompt);store.approve_prompt(prompt.identifier)
    return session,scene,shot,image,prompt,store

class FakeMotionProvider:
    def _request(self,instructions,content,schema):
        assert 'No image bytes' in instructions
        assert 'First view' in content
        return schema(positive_prompt='A vehicle rolls forward slowly. The camera remains stationary.',
            subject_movements='Vehicle rolls forward',camera_movement='Stationary')

class FakeVideoProvider(VideoProvider):
    name='Fake'
    def __init__(self,content=None):self.content=content;self.requests=[]
    def test_connection(self):return 'Connected'
    def capabilities(self,model):return VideoCapabilities(duration=True,resolution=True,frame_rate=True,job_lookup=True,cancel=True,continuation=True)
    def submit(self,request):
        self.validate(request);self.requests.append(request)
        if self.content:return VideoProviderResult(status='Completed',videos=[ProviderVideo(data=self.content,model='fake')])
        return VideoProviderResult(status='Submitted',provider_job_id='remote-1')
    def poll(self,provider_job_id):return VideoProviderResult(status='Running',provider_job_id=provider_job_id)
    def cancel(self,provider_job_id):return True

def test_schema_nine_migrates_eight_without_loss(tmp_path):
    path=tmp_path/'legacy.sqlite'
    with sqlite3.connect(path) as con:
        for version in range(1,9):con.executescript('BEGIN;'+MIGRATIONS[version]+f'PRAGMA user_version={version}; COMMIT;')
        con.execute("INSERT INTO image_prompt VALUES('old','shot','scene',1,'Approved','hash','date','{}')")
    db=ProjectDatabase(path)
    with db._connect() as con:
        db._check_schema(con)
        assert con.execute('PRAGMA user_version').fetchone()[0]==SCHEMA_VERSION
        assert con.execute("SELECT identifier FROM image_prompt").fetchone()[0]=='old'
        assert con.execute("SELECT name FROM sqlite_master WHERE name='generated_video'").fetchone()

def test_comfy_workflow_mapping_and_unsupported_controls(tmp_path):
    image=tmp_path/'start.png';image.write_bytes(b'fake')
    workflow={'a':{'class_type':'LoadImage','inputs':{'image':''}},'b':{'class_type':'CLIPTextEncode','inputs':{'text':''}},
        'c':{'class_type':'Settings','inputs':{'length':8,'width':320,'height':240,'fps':10}},
        'z':{'class_type':'VideoCombine','inputs':{'images':['c',0]}}}
    mapping={'starting_image':{'node':'a','input':'image'},'motion_prompt':{'node':'b','input':'text'},
        'frame_count':{'node':'c','input':'length'},'width':{'node':'c','input':'width'},
        'height':{'node':'c','input':'height'},'frame_rate':{'node':'c','input':'fps'},'output_video':'z'}
    provider=ComfyUIVideoProvider(workflow=workflow,mapping=mapping)
    request=VideoRequest(starting_image_path=str(image),prompt='Robot lowers cargo',frame_count=8,
        width=320,height=240,frame_rate=10)
    provider.validate(request)
    with pytest.raises(VideoProviderError,match='negative prompt'):
        provider.validate(request.model_copy(update={'negative_prompt':'blur'}))
    with pytest.raises(VideoProviderError,match='Ending image'):
        provider.validate(request.model_copy(update={'ending_image_path':str(image)}))
    bad=ComfyUIVideoProvider(workflow=workflow,mapping={**mapping,'frame_count':{'node':'missing','input':'length'}})
    with pytest.raises(VideoProviderError,match='Workflow input'):
        bad.validate(request)
    calls=[]
    provider._upload=lambda path:'uploaded.png'
    provider._json=lambda route,body=None:(calls.append((route,body)) or {'prompt_id':'p1'})
    result=provider.submit(request)
    assert result.provider_job_id=='p1'
    submitted=calls[0][1]['prompt']
    assert submitted['a']['inputs']['image']=='uploaded.png'
    assert submitted['b']['inputs']['text']=='Robot lowers cargo'
    assert submitted['c']['inputs']['length']==8
    assert workflow['a']['inputs']['image']==''

def test_motion_prompt_versions_and_approval(tmp_path):
    session,scene,shot,image,first,store=motion_fixture(tmp_path)
    prompt=generate_motion_prompt(session,scene,shot,provider=FakeMotionProvider())
    assert prompt.version==2 and prompt.starting_image_id==image.identifier
    edited=store.edit_prompt(prompt.identifier,positive_prompt='A vehicle moves one meter.')
    assert edited.version==3 and store.prompts(shot.identifier)[-1].identifier==first.identifier
    assert store.approved_prompt(shot.identifier).identifier==first.identifier
    store.approve_prompt(edited.identifier)
    assert store.approved_prompt(shot.identifier).positive_prompt=='A vehicle moves one meter.'
    assert ImageStore(session.root).approved(shot.identifier)[0].identifier==image.identifier

def test_modes_jobs_recovery_budget_and_rollback(tmp_path):
    session,scene,shot,image,prompt,store=motion_fixture(tmp_path)
    assert store.production(shot.identifier)[0]=='Camera Motion'
    store.set_production(shot.identifier,'AI Animation')
    selected,counts=eligible_video_shots(session,[scene])
    assert counts['selected']==1 and len(selected)==1
    provider=FakeVideoProvider();gen=VideoGenerator(session,provider)
    job,request=gen.enqueue(scene,shot)
    assert store.jobs(shot.identifier)[0].status=='Pending'
    gen.run(job,request)
    assert job.status=='Submitted' and job.provider_job_id=='remote-1'
    assert store.recover()[0].status=='Needs Reconciliation'
    gen.reconcile(job);assert job.status=='Running'
    assert gen.cancel(job) and job.status=='Canceled'
    with pytest.raises(ValueError):store.update_job(job.model_copy(update={'status':'Completed'}))
    settings=store.settings().model_copy(update={'spending_cap':1,'estimated_cost_per_clip':None})
    store.save_settings(settings)
    with pytest.raises(ValueError,match='cap'):
        gen._budget(VideoJob(project_id=session.project.identifier,scene_id=scene.identifier,shot_id=shot.identifier,
            provider='Fake',model='',starting_image_id=image.identifier,prompt_id=prompt.identifier,
            requested_duration=.6,requested_width=320,requested_height=240,dependency_fingerprint='x'))
    # Foreign keys and prompt history remain intact when a related insert fails.
    bad=VideoJob(project_id=session.project.identifier,scene_id=scene.identifier,shot_id=shot.identifier,
        provider='Fake',model='',starting_image_id=image.identifier,prompt_id='missing',
        requested_duration=.6,requested_width=320,requested_height=240,dependency_fingerprint='x')
    with pytest.raises(ValueError):store.save_job(bad)
    assert len(store.jobs())==1 and store.approved_prompt(shot.identifier).identifier==prompt.identifier

def synthetic_clip(path,seconds=1,fps=12,audio=True,color='blue'):
    args=['ffmpeg','-v','error','-y','-f','lavfi','-i',f'color=c={color}:s=320x240:r={fps}:d={seconds}']
    if audio:args+=['-f','lavfi','-i',f'sine=frequency=880:sample_rate=16000:duration={seconds}']
    args+=['-c:v','libx264','-preset','ultrafast','-pix_fmt','yuv420p']
    if audio:args+=['-c:a','aac','-shortest']
    else:args+=['-an']
    args+=[str(path)]
    subprocess.run(args,check=True,capture_output=True)
    return path

@pytest.mark.skipif(shutil.which('ffmpeg') is None or shutil.which('ffprobe') is None,reason='FFmpeg unavailable')
def test_assets_candidates_lock_continuation_export_portability(tmp_path):
    session,scene,shot,image,prompt,store=motion_fixture(tmp_path)
    clip_file=synthetic_clip(tmp_path/'generated.mp4',seconds=.6)
    job=VideoJob(project_id=session.project.identifier,scene_id=scene.identifier,shot_id=shot.identifier,
        provider='Fake',model='v1',starting_image_id=image.identifier,prompt_id=prompt.identifier,
        requested_duration=.6,requested_width=320,requested_height=240,dependency_fingerprint=prompt.dependency_fingerprint)
    store.save_job(job)
    output=ProviderVideo(data=clip_file.read_bytes(),filename='sample.mp4',model='v1')
    first=store.save_video(job,output,1);second=store.save_video(job,output,2)
    third=store.save_video(job,output,3)
    assert first.path.startswith('video_clips/') and first.has_audio and first.frame_count>0
    assert (session.root/first.poster_path).is_file()
    store.approve(shot.identifier,[first.identifier],lock=True)
    with pytest.raises(ValueError):store.approve(shot.identifier,[second.identifier])
    store.lock(shot.identifier,False);store.approve(shot.identifier,[first.identifier,second.identifier])
    assert len(store.approved(shot.identifier)[0])==2
    with pytest.raises(ValueError):store.reject(first.identifier)
    store.reject(third.identifier,'Robot changed shape','Distorted anatomy')
    assert store.clip(third.identifier).review_issues==['Distorted anatomy']
    frame=extract_final_frame(session.root/first.path,session.root/'video_clips'/'final.png')
    assert frame.is_file()
    continuation,request=VideoGenerator(session,FakeVideoProvider()).prepare(scene,shot,
        parent_clip_id=first.identifier,continuation_prompt='The view continues moving slowly.')
    assert continuation.parent_clip_id==first.identifier
    assert Path(request.starting_image_path).is_file()
    assert request.prompt=='The view continues moving slowly.'
    rows=video_manifest(session)
    assert len(rows)==3 and 'motion_prompt' in rows[0] and '# Animated video clips' in manifest_markdown(rows)
    destination=tmp_path/'export';export_approved_clips(session,destination)
    assert len(list(destination.glob('*.mp4')))==2
    with pytest.raises(FileExistsError):export_approved_clips(session,destination)
    moved=tmp_path/'moved';shutil.copytree(session.root,moved)
    assert (moved/VideoStore(moved).approved(shot.identifier)[0][0].path).is_file()

@pytest.mark.skipif(shutil.which('ffmpeg') is None or shutil.which('ffprobe') is None,reason='FFmpeg unavailable')
def test_real_mixed_ffmpeg_transitions_master_narration(tmp_path):
    audio=tmp_path/'narration.wav';make_wav(audio,seconds=5)
    video1=synthetic_clip(tmp_path/'with_audio.mp4',fps=12,audio=True,color='blue')
    video2=synthetic_clip(tmp_path/'silent.mp4',fps=24,audio=False,color='green')
    still=tmp_path/'still.png';image=QImage(320,240,QImage.Format_RGB32);image.fill(QColor('red'));assert image.save(str(still))
    segments=[]
    kinds=['still','video','video','still','video']
    sources=[still,video1,video2,still,video1]
    for i,(kind,source) in enumerate(zip(kinds,sources)):
        segments.append(PlannedSegment(scene_id='scene',shot_id=str(i),asset_id=str(i),asset_kind=kind,
            production_mode='AI Animation' if kind=='video' else 'Camera Motion',
            source_path=str(source),source_fingerprint=file_hash(source),source_start=i,source_end=i+1,
            start_frame=i*30,end_frame=(i+1)*30,width=320,height=240,frame_rate=30,fit_policy='fit',
            camera=CameraFrame(kind='Push in',start_zoom=1,end_zoom=1.05) if kind=='still' else CameraFrame(),
            transition='Crossfade' if i else 'Cut',transition_frames=5 if i else 0))
    plan=RenderPlan(project_id='x',scope='full',mode='final',source_start=0,source_end=5,
        audio_path=str(audio),audio_fingerprint=file_hash(audio),audio_duration=5,
        settings=RenderSettings(width=320,height=240,frame_rate=30,preset='ultrafast'),segments=segments)
    output=tmp_path/'mixed.mp4';meta=FFmpegRenderer(tmp_path).render(plan,output)
    assert meta['frames']==150 and abs(meta['duration']-5)<.04
    assert meta['video']['codec_name']=='h264' and meta['audio']['codec_name']=='aac'
    assert (meta['video']['width'],meta['video']['height'])==(320,240)
    assert abs(meta['audio_duration']-5)<.12
    # Narration is 220 Hz; generated clip audio is 880 Hz. The mux must keep only narration.
    raw=subprocess.run(['ffmpeg','-v','error','-ss','1.2','-i',str(output),'-map','0:a:0',
        '-t','0.5','-c:a','pcm_s16le','-f','s16le','-ac','1','-ar','16000','-'],check=True,capture_output=True).stdout
    import array,math
    samples=array.array('h');samples.frombytes(raw)
    def power(freq):
        return abs(sum(value*math.sin(2*math.pi*freq*i/16000) for i,value in enumerate(samples[:4000])))
    assert power(220)>power(880)*5

def test_gui_animation_controls_and_missing_clip_validation(tmp_path):
    session,scenes=project_with_timing(tmp_path)
    shot=session.database.load_storyboard(scenes[0].identifier).shots[0]
    store=VideoStore(session.root);store.set_production(shot.identifier,'AI Animation')
    plan,report=build_render_plan(session,RenderSettings(width=320,height=240,mode='final'))
    assert any(issue.code=='missing_video' for issue in report.issues)
    script='''
import sys
from pathlib import Path
from PySide6.QtWidgets import QApplication
from prose_to_video.gui.animation_panel import AnimationPanel,VideoGenerationSettingsDialog
from prose_to_video.storage.project_files import open_project
from prose_to_video.storage.generated_videos import VideoStore
app=QApplication([])
session=open_project(Path(sys.argv[1]))
scene=session.database.load_scenes()[1][0]
shot=session.database.load_storyboard(scene.identifier).shots[0]
panel=AnimationPanel();panel.bind(session,scene,shot)
assert panel.player.videoOutput()==panel.video and panel.audio.isMuted()
dialog=VideoGenerationSettingsDialog(VideoStore(session.root).settings())
assert dialog.width.value()==832
dialog.close();panel.close()
'''
    import sys
    result=subprocess.run([sys.executable,'-c',script,str(session.root)],
        env={**os.environ,'QT_QPA_PLATFORM':'offscreen'},capture_output=True,text=True)
    assert result.returncode==0,result.stderr

@pytest.mark.skipif(shutil.which('ffmpeg') is None or shutil.which('ffprobe') is None,reason='FFmpeg unavailable')
def test_planned_ai_shot_and_explicit_tail_policy(tmp_path):
    session,scene,shot,image,prompt,store=motion_fixture(tmp_path)
    source=synthetic_clip(tmp_path/'generated.mp4',seconds=.4,fps=10,audio=True)
    job=VideoJob(project_id=session.project.identifier,scene_id=scene.identifier,shot_id=shot.identifier,
        provider='Fake',model='v1',starting_image_id=image.identifier,prompt_id=prompt.identifier,
        requested_duration=.6,requested_width=320,requested_height=240,dependency_fingerprint=prompt.dependency_fingerprint)
    store.save_job(job)
    clip=store.save_video(job,ProviderVideo(data=source.read_bytes(),filename='generated.mp4'),1)
    store.approve(shot.identifier,[clip.identifier]);store.set_production(shot.identifier,'AI Animation')
    settings=RenderSettings(width=320,height=240,frame_rate=30,preset='ultrafast',mode='final')
    plan,report=build_render_plan(session,settings,scope='shot',shot_id=shot.identifier)
    assert any(i.code=='video_coverage' for i in report.issues)
    store.set_production(shot.identifier,'AI Animation',{'strategy':'still_tail','tail_image_id':image.identifier})
    plan,report=build_render_plan(session,settings,scope='shot',shot_id=shot.identifier)
    assert report.ready,[i.model_dump() for i in report.issues]
    assert [s.asset_kind for s in plan.segments]==['video','still']
    assert plan.segments[-1].end_frame==18
    output=tmp_path/'shot.mp4'
    meta=FFmpegRenderer(session.root).render(plan,output,accept_warnings=True)
    assert meta['frames']==18 and abs(meta['audio_duration']-.6)<.12

@pytest.mark.skipif(shutil.which('ffmpeg') is None or shutil.which('ffprobe') is None,reason='FFmpeg unavailable')
def test_approved_sequence_covers_one_shot_without_timing_change(tmp_path):
    session,scene,shot,image,prompt,store=motion_fixture(tmp_path)
    source=synthetic_clip(tmp_path/'short.mp4',seconds=.3,fps=10,audio=False)
    job=VideoJob(project_id=session.project.identifier,scene_id=scene.identifier,shot_id=shot.identifier,
        provider='Fake',model='v1',starting_image_id=image.identifier,prompt_id=prompt.identifier,
        requested_duration=.3,requested_width=320,requested_height=240,dependency_fingerprint=prompt.dependency_fingerprint)
    store.save_job(job)
    output=ProviderVideo(data=source.read_bytes(),filename='short.mp4')
    a=store.save_video(job,output,1);b=store.save_video(job,output,2)
    store.approve(shot.identifier,[a.identifier,b.identifier]);store.set_production(shot.identifier,'AI Animation',{'strategy':'sequence'})
    plan,report=build_render_plan(session,RenderSettings(width=320,height=240,frame_rate=30,mode='final'),
        scope='shot',shot_id=shot.identifier)
    assert report.ready,[i.model_dump() for i in report.issues]
    assert [s.asset_kind for s in plan.segments]==['video','video']
    assert [s.frames for s in plan.segments]==[9,9]
    assert (plan.source_start,plan.source_end)==(.2,.8)


def test_dependency_fingerprint_ignores_budget_and_narration_timing(tmp_path):
    session,scene,shot,image,prompt,store=motion_fixture(tmp_path)
    before=source_fingerprint(shot,image,None,store.settings())
    changed=store.settings().model_copy(update={'spending_cap':20,'retry_limit':4,'candidates':3})
    assert source_fingerprint(shot,image,None,changed)==before
    moved=shot.model_copy(update={'audio_start':.3,'audio_end':.9})
    assert source_fingerprint(moved,image,None,store.settings())==before
    changed=store.settings().model_copy(update={'seed':17})
    assert source_fingerprint(shot,image,None,changed)!=before


def test_comfy_poll_normalizes_streamed_video(tmp_path,monkeypatch):
    source=synthetic_clip(tmp_path/'input.mp4') if shutil.which('ffmpeg') else None
    if not source:pytest.skip('FFmpeg unavailable')
    workflow={'out':{'class_type':'SaveVideo','inputs':{}}}
    provider=ComfyUIVideoProvider(workflow=workflow,mapping={'output_video':'out'})
    provider._json=lambda route,body=None:{'job':{'status':{'completed':True},
        'outputs':{'out':{'gifs':[{'filename':'output.mp4','subfolder':'','type':'output'}]}}}}
    class Response:
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def read(self,n=-1):return self.stream.read(n)
        def __init__(self):self.stream=source.open('rb')
    import urllib.request
    monkeypatch.setattr(urllib.request,'urlopen',lambda *args,**kwargs:Response())
    result=provider.poll('job')
    assert result.status=='Completed' and len(result.videos)==1
    assert Path(result.videos[0].path).read_bytes()==source.read_bytes()
    Path(result.videos[0].path).unlink()

def test_ending_image_import_and_provider_capability(tmp_path):
    from prose_to_video.storage.visual_library import VisualLibrary
    session,scene,shot,image,_,store=motion_fixture(tmp_path)
    ending_file=tmp_path/'ending.png'
    ending=QImage(320,240,QImage.Format_RGB32);ending.fill(QColor('green'));assert ending.save(str(ending_file))
    imported=VisualLibrary(session.root).import_image(ending_file)
    store.save_settings(store.settings().model_copy(update={'use_last_frame':True}))
    prompt=generate_motion_prompt(session,scene,shot,provider=FakeMotionProvider(),ending_image_id=imported.identifier)
    store.approve_prompt(prompt.identifier)
    with pytest.raises(VideoProviderError,match='Ending image'):
        VideoGenerator(session,FakeVideoProvider()).prepare(scene,shot)
    class LastFrameProvider(FakeVideoProvider):
        def capabilities(self,model):
            return super().capabilities(model).model_copy(update={'last_frame':True})
    job,request=VideoGenerator(session,LastFrameProvider()).prepare(scene,shot)
    assert job.ending_image_id==imported.identifier and request.ending_image_path==str(session.root/imported.path)
    wrong=QImage(400,240,QImage.Format_RGB32);wrong.fill(QColor('yellow'))
    other=tmp_path/'wrong.png';assert wrong.save(str(other))
    bad=VisualLibrary(session.root).import_image(other)
    with pytest.raises(ValueError,match='aspect ratio'):
        generate_motion_prompt(session,scene,shot,provider=FakeMotionProvider(),ending_image_id=bad.identifier)


def test_generation_retry_and_known_cost(tmp_path):
    session,scene,shot,image,prompt,store=motion_fixture(tmp_path)
    store.save_settings(store.settings().model_copy(update={'retry_limit':1,'estimated_cost_per_clip':.5,'spending_cap':1}))
    # New cost settings do not invalidate an approved prompt.
    class Limited(FakeVideoProvider):
        def submit(self,request):
            self.requests.append(request)
            if len(self.requests)==1:raise VideoProviderError('Rate limited',recoverable=True)
            return VideoProviderResult(status='Submitted',provider_job_id='remote-2')
    provider=Limited();generator=VideoGenerator(session,provider)
    job,request=generator.enqueue(scene,shot)
    generator.run(job,request)
    assert job.status=='Submitted' and len(provider.requests)==2 and job.estimated_cost==.5
    another,request2=generator.enqueue(scene,shot,candidate_number=2)
    assert generator._budget(another) is None
    third,request3=generator.enqueue(scene,shot,candidate_number=3)
    # Pending items are not charged until submission; the completed/submitted work is counted.
    assert generator._budget(third) is None

def test_video_workflow_profile_is_portable(tmp_path):
    session,scene,shot,image,prompt,store=motion_fixture(tmp_path)
    external=tmp_path/'api-workflow.json'
    external.write_text(json.dumps({'node-91':{'class_type':'Test','inputs':{'image':''}}}))
    before=external.read_bytes()
    settings=store.save_settings(store.settings().model_copy(update={'workflow_path':str(external)}))
    assert settings.workflow_path.startswith('storyboard/video_workflows/')
    assert (session.root/settings.workflow_path).read_bytes()==before==external.read_bytes()
    moved=tmp_path/'relocated';shutil.copytree(session.root,moved)
    assert (moved/VideoStore(moved).settings().workflow_path).read_bytes()==before


def test_gui_prompt_version_selection(tmp_path):
    session,scene,shot,image,first,store=motion_fixture(tmp_path)
    second=store.edit_prompt(first.identifier,positive_prompt='A distinct motion description.')
    script='''
import sys
from pathlib import Path
from PySide6.QtWidgets import QApplication
from prose_to_video.gui.animation_panel import AnimationPanel
from prose_to_video.storage.project_files import open_project
app=QApplication([])
project=open_project(Path(sys.argv[1]))
scene=project.database.load_scenes()[1][0]
shot=project.database.load_storyboard(scene.identifier).shots[0]
panel=AnimationPanel();panel.bind(project,scene,shot)
assert panel.version.count()==2
assert panel.prompt.toPlainText()=='A distinct motion description.'
panel.version.setCurrentIndex(1)
assert panel.prompt.toPlainText()=='The view remains still.'
panel.close()
'''
    import sys
    result=subprocess.run([sys.executable,'-c',script,str(session.root)],
        env={**os.environ,'QT_QPA_PLATFORM':'offscreen'},capture_output=True,text=True)
    assert result.returncode==0,result.stderr

def test_comfy_server_reference_verifies_approved_image(tmp_path,monkeypatch):
    import io,urllib.request
    source=tmp_path/'approved.png';source.write_bytes(b'approved image content')
    workflow={'loader':{'class_type':'LoadImage','inputs':{'image':''}},
        'text':{'class_type':'Text','inputs':{'text':''}},
        'output':{'class_type':'Video','inputs':{}}}
    mapping={'starting_image':{'node':'loader','input':'image','mode':'reference','filename':'approved.png'},
        'motion_prompt':{'node':'text','input':'text'},'output_video':'output'}
    provider=ComfyUIVideoProvider(workflow=workflow,mapping=mapping)
    request=VideoRequest(starting_image_path=str(source),prompt='Move slowly')
    provider._json=lambda route,body=None:{'prompt_id':'job'}
    monkeypatch.setattr(urllib.request,'urlopen',lambda *args,**kwargs:io.BytesIO(source.read_bytes()))
    assert provider.submit(request).provider_job_id=='job'
    monkeypatch.setattr(urllib.request,'urlopen',lambda *args,**kwargs:io.BytesIO(b'wrong image'))
    with pytest.raises(VideoProviderError,match='differs'):
        provider.submit(request)

def test_concurrent_budget_reservation_blocks_extra_submission(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    session,scene,shot,image,prompt,store=motion_fixture(tmp_path)
    store.save_settings(store.settings().model_copy(update={'max_concurrent_jobs':2,
        'spending_cap':.5,'estimated_cost_per_clip':.5}))
    provider=FakeVideoProvider();generator=VideoGenerator(session,provider)
    first,request1=generator.enqueue(scene,shot,candidate_number=1)
    second,request2=generator.enqueue(scene,shot,candidate_number=2)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures=[pool.submit(generator.run,*pair) for pair in ((first,request1),(second,request2))]
        outcomes=[]
        for future in futures:
            try:outcomes.append(future.result().status)
            except ValueError:outcomes.append('Budget blocked')
    assert sorted(outcomes)==['Budget blocked','Submitted']
    assert len(provider.requests)==1
