"""Stage Ten deterministic orchestration and real final muxing."""
from __future__ import annotations
import os
import shutil
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
from PySide6.QtWidgets import QApplication

from test_video_render import project_with_timing
from test_image_generation import FakeImageProvider
from test_image_prompts import FakeProvider as PromptProvider
from prose_to_video.models.alignment import AlignmentRun
from prose_to_video.models.image_job import ImageGenerationSettings
from prose_to_video.models.image_prompt import PromptSettings
from prose_to_video.models.project import VideoSettings
from prose_to_video.models.production import ProductionProfile, QualityIssue, correction_workspace
from prose_to_video.models.visual_reference import VisualEntity
from prose_to_video.models.video_render import RenderSettings
from prose_to_video.pipeline.image_generation import ImageGenerator
from prose_to_video.pipeline.image_prompt_generation import generate_prompt
from prose_to_video.pipeline.production_orchestrator import ProductionOrchestrator, select_animations
from prose_to_video.pipeline.quality_control import QualityController
from prose_to_video.pipeline.video_assembly import file_hash
from prose_to_video.rendering.ffmpeg_renderer import probe
from prose_to_video.storage.database import MIGRATIONS, ProjectDatabase, SCHEMA_VERSION
from prose_to_video.storage.generated_images import ImageStore
from prose_to_video.storage.image_prompts import PromptStore
from prose_to_video.storage.production import ProductionStore
from prose_to_video.storage.project_files import save_settings
from prose_to_video.storage.visual_library import VisualLibrary
from prose_to_video.models.scene import source_hash

APP=QApplication.instance() or QApplication([])


def prepared(tmp_path):
    session, scenes = project_with_timing(tmp_path)
    session=save_settings(session,VideoSettings(width=320,height=240,frame_rate=30))
    audio=session.project.resolved_audio(session.root)
    session.database.save_alignment(AlignmentRun(prose_hash=source_hash('First view. Second view.'),
        audio_hash=file_hash(audio),audio_path=str(audio.relative_to(session.root)),audio_duration=2,
        transcription_model='fake',device='cpu',precision='int8',config_fingerprint='fake'))
    library=VisualLibrary(session.root)
    entity=library.save_entity(VisualEntity(name='The view',entity_type='Prop or object',review_status='Approved'))
    for scene in scenes:library.relate_scene(entity.identifier,scene.identifier)
    image_store=ImageStore(session.root)
    image_store.save_settings(ImageGenerationSettings(provider='Fake',model='fake-v1',
        width=320,height=240,candidates=1,estimated_cost_per_image=0.1))
    provider=FakeImageProvider();generator=ImageGenerator(session,provider)
    for scene in scenes:
        board=session.database.load_storyboard(scene.identifier)
        for shot in board.shots:
            prompt=generate_prompt(shot,scene,board,library,session.project,PromptSettings(),PromptProvider())
            prompts=PromptStore(session.root);prompts.save(prompt)
            prompts.approve(shot.identifier,prompt.version,scene,shot,board,session.project,library)
            job,request,_=generator.enqueue(scene,shot,board)
            generator.run(job,request)
            image_store.approve(image_store.assets(shot.identifier)[0].identifier)
    return session, scenes, provider


def profile(session, **changes):
    render=RenderSettings(width=320,height=240,frame_rate=30,preset='ultrafast',
        output_directory='output',output_filename='stage10.mp4')
    return ProductionProfile.preset('Illustrated Story', automation_mode='Fully Automatic',
        authorize_auto_approval=True, authorize_unknown_cost=True,
        quality_thresholds={'accept_render_warnings':True,'max_timeline_gap':.5},render_settings=render, **changes)


def test_profile_validation_and_animation_policy():
    with pytest.raises(ValueError,match='automatic approval'):
        ProductionProfile(automation_mode='Fully Automatic')
    class Shot:
        def __init__(self, i, action):
            self.identifier=str(i);self.scene_id='one';self.sequence=i;self.character_actions=action
            self.suggested_motion='';self.visual_description=action
    shots=[Shot(1,'quiet room'),Shot(2,'robot runs'),Shot(3,'vehicle drives'),Shot(4,'person walks')]
    value=ProductionProfile(animation_policy='All eligible',budget_video=2)
    assert select_animations(shots,value,1)==['2','3','4']
    assert select_animations(shots,ProductionProfile(animation_policy='None'))==[]


def test_schema_ten_migrates_and_store_transaction(tmp_path):
    session,_=project_with_timing(tmp_path)
    with sqlite3.connect(session.database.path) as con:
        assert con.execute('PRAGMA user_version').fetchone()[0]==SCHEMA_VERSION
        assert con.execute("SELECT name FROM sqlite_master WHERE name='production_task'").fetchone()
    store=ProductionStore(session.root)
    issue=QualityIssue(project_id=session.project.identifier,category='Timing',severity='Error',description='A gap')
    store.save_issue(issue)
    issue.status='Resolved';store.save_issue(issue)
    assert store.issues(status='Resolved')[0].identifier==issue.identifier
    assert correction_workspace(issue)=='Storyboard'
    with pytest.raises(ValueError,match='dependency graph'):
        from prose_to_video.models.production import ProductionRun,ProductionTask
        run=ProductionRun(project_id=session.project.identifier,profile=ProductionProfile())
        store.save_graph(run,[ProductionTask(project_id=run.project_id,kind='broken',fingerprint='x',dependencies=['missing'])])
    assert not store.runs()


def test_dry_run_queue_and_approval(tmp_path):
    session,scenes,provider=prepared(tmp_path)
    engine=ProductionOrchestrator(session)
    value=profile(session)
    before=len(provider.requests)
    report=engine.dry_run(value)
    assert report['ready'] and report['shots']==2 and report['scenes']==2
    assert [t.kind for t in report['will_run']]==['coverage','render','manifest']
    assert len(provider.requests)==before and not ProductionStore(session.root).runs()
    run=engine.start(value)
    assert len(engine.store.tasks(run.identifier))==len(report['tasks'])
    assert engine.step(run.identifier).current_stage=='coverage'
    assert engine.store.tasks(run.identifier)[-3].status=='Completed'
    engine.pause(run.identifier)
    assert engine.store.run(run.identifier).status=='Paused'
    engine.resume(run.identifier)
    engine.cancel(run.identifier)
    assert engine.store.run(run.identifier).status=='Canceled'
    shot=session.database.load_storyboard(scenes[0].identifier).shots[0]
    approved=ImageStore(session.root).approved(shot.identifier)
    assert approved and (session.root/approved[0].path).is_file()


def test_quality_issues_and_routes(tmp_path):
    session,scenes,_=prepared(tmp_path)
    image=ImageStore(session.root).approved(session.database.load_storyboard(scenes[0].identifier).shots[0].identifier)[0]
    (session.root/image.path).unlink()
    findings=QualityController(session).inspect()
    assert any(i.category=='Image' and i.severity=='Error' for i in findings)
    assert ProductionStore(session.root).issues(category='Image')
    assert correction_workspace(next(i for i in findings if i.category=='Image'))=='Image Gallery'


@pytest.mark.skipif(not shutil.which('ffmpeg') or not shutil.which('ffprobe'),reason='FFmpeg unavailable')
def test_real_production_render_manifest_and_recovery(tmp_path):
    session,scenes,provider=prepared(tmp_path)
    engine=ProductionOrchestrator(session)
    run=engine.start(profile(session))
    first=engine.step(run.identifier)
    assert first.status=='Running' and first.current_stage=='coverage'
    assert engine.store.tasks(run.identifier)[-3].status=='Completed'
    assert engine.store.recover()[0].status=='Interrupted'
    engine.resume(run.identifier)
    final=engine.run_until_pause(run.identifier)
    assert final.status=='Completed With Warnings',final.error
    output=session.root/final.output_path
    assert output.is_file() and Path(session.root/final.manifest_path).is_file()
    data=probe(output)
    assert {'video','audio'} <= {s['codec_type'] for s in data['streams']}
    assert len(provider.requests)==2
    assert [t.status for t in engine.store.tasks(run.identifier)][-3:]==['Completed']*3
    assert engine.store.runs()[0].identifier==run.identifier
    assert engine.store.run(run.identifier).manifest_path==final.manifest_path


def test_failed_render_preserves_approved_images(tmp_path,monkeypatch):
    session,scenes,_=prepared(tmp_path)
    engine=ProductionOrchestrator(session)
    run=engine.start(profile(session))
    engine.step(run.identifier)
    import prose_to_video.pipeline.production_orchestrator as module
    monkeypatch.setattr(module,'run_render',lambda *a,**k:SimpleNamespace(status='Failed',error='test failure'))
    result=engine.run_until_pause(run.identifier)
    assert result.status=='Failed' and 'test failure' in result.error
    for scene in scenes:
        shot=session.database.load_storyboard(scene.identifier).shots[0]
        asset=ImageStore(session.root).approved(shot.identifier)[0]
        assert (session.root/asset.path).is_file()
    monkeypatch.undo()
    engine.retry_failed(run.identifier)
    engine.resume(run.identifier)
    assert engine.run_until_pause(run.identifier).status=='Completed With Warnings'

@pytest.mark.skipif(not shutil.which('ffmpeg') or not shutil.which('ffprobe'),reason='FFmpeg unavailable')
@pytest.mark.parametrize('animated',[False,True,'fallback'])
def test_full_from_imported_sources_with_fake_providers(tmp_path,animated):
    """All AI stages use deterministic adapters; the final MP4 uses real FFmpeg."""
    import json
    import subprocess
    from test_video_render import make_wav
    from prose_to_video.storage.project_files import create_project,import_prose,import_audio
    from prose_to_video.models.shot import StoryboardSettings
    from prose_to_video.models.alignment import TranscriptSegment,TranscriptWord
    from prose_to_video.pipeline.providers import BoundaryProposal,SceneDescription
    from prose_to_video.pipeline.shot_planning import ShotIdea,ShotIdeas
    from prose_to_video.pipeline.entity_extraction import EntityProposals
    from prose_to_video.pipeline.image_prompt_generation import PromptDraft
    from prose_to_video.models.image_prompt import PromptComponents
    from prose_to_video.models.video_job import VideoGenerationSettings,VideoCapabilities,VideoProviderResult,ProviderVideo
    from prose_to_video.pipeline.video_providers.base import VideoProvider
    from prose_to_video.storage.generated_videos import VideoStore
    from prose_to_video.pipeline.video_assembly import build_render_plan
    text='Ada runs.'
    source=tmp_path/'manuscript.txt';source.write_text(text)
    audio=tmp_path/'voice.wav';make_wav(audio,1)
    session=import_audio(import_prose(create_project(tmp_path,'From sources'),source),audio)
    session=save_settings(session,VideoSettings(width=320,height=240,frame_rate=30))
    session.database.save_storyboard_settings(StoryboardSettings(minimum=.1,maximum=10))
    ImageStore(session.root).save_settings(ImageGenerationSettings(provider='Fake',model='fake-v1',
        width=320,height=240,estimated_cost_per_image=.1))
    if animated:
        VideoStore(session.root).save_settings(VideoGenerationSettings(provider='ComfyUI',
            width=320,height=240,duration=1,frame_rate=10,estimated_cost_per_clip=.2))
    class Language:
        def propose_boundaries(self,payload):return BoundaryProposal(starts=[])
        def describe_scene(self,payload):
            return SceneDescription(title='Running',location=None,time_of_day=None,characters=['Ada'],
                point_of_view=None,narrative_type='action',summary='Ada runs.',environment='',
                important_objects=[],actions=['runs'],dialogue_participants=[],visual_atmosphere='',boundary_reason='')
        def propose_shots(self,payload):
            return ShotIdeas(shots=[ShotIdea(start_id=json.loads(payload)['valid_ids'][0],
                title='Ada runs',visual_description='Ada running on a path',character_actions='Ada runs')])
        def extract_entities(self,payload):return EntityProposals()
        def generate_image_prompt(self,payload):
            return PromptDraft(components=PromptComponents(shot_description='Ada runs on a path'))
        def _request(self,instructions,content,schema):
            return schema(positive_prompt='Ada runs forward',subject_movements='Ada runs')
    class Alignment:
        alignment_model='fake'
        def load(self,settings,device,precision):pass
        def process_chunk(self,path,offset,settings):
            return [TranscriptSegment(start=.1,end=.7,text='Ada runs',words=[
                TranscriptWord(text='Ada',start=.1,end=.3),TranscriptWord(text='runs',start=.4,end=.7)])]
    fake_image=FakeImageProvider()
    video_provider=None
    if animated:
        clip=tmp_path/'fake-clip.mp4'
        subprocess.run(['ffmpeg','-v','error','-f','lavfi','-i','color=c=blue:s=320x240:r=10:d=1',
            '-c:v','libx264','-pix_fmt','yuv420p','-y',str(clip)],check=True)
        class FakeVideo(VideoProvider):
            name='Fake'
            def __init__(self):self.requests=[]
            def test_connection(self):return 'Connected'
            def capabilities(self,model):return VideoCapabilities(duration=True,resolution=True,frame_rate=True)
            def submit(self,request):
                self.validate(request);self.requests.append(request)
                if animated == 'fallback':
                    return VideoProviderResult(status='Failed',error='Provider rejected the clip')
                return VideoProviderResult(status='Completed',videos=[ProviderVideo(data=clip.read_bytes(),model='fake')])
        video_provider=FakeVideo()
    engine=ProductionOrchestrator(session,language_provider=Language(),image_provider=fake_image,
                                  video_provider=video_provider,alignment_provider=Alignment())
    value=(profile(session,animation_policy='All eligible',allow_mode_changes=True,budget_video=.2,
                   fallback_animation='Camera Motion' if animated == 'fallback' else 'Pause')
           if animated else profile(session))
    assert engine.dry_run(value)['scenes']==0
    run=engine.start(value,paid_confirmed=True)
    final=engine.run_until_pause(run.identifier)
    assert final.status=='Completed With Warnings',final.error
    assert len(fake_image.requests)==1
    assert (session.root/final.output_path).is_file()
    assert len(session.database.load_scenes()[1])==1
    assert len(engine.store.tasks(run.identifier))>6
    if animated is True:
        assert len(video_provider.requests)==1
        plan,report=build_render_plan(session,value.render_settings)
        assert report.ready and [(s.asset_kind,s.start_frame,s.end_frame) for s in plan.segments]==[('video',0,30)]
        pixel=subprocess.run(['ffmpeg','-v','error','-ss','0.5','-i',str(session.root/final.output_path),
            '-frames:v','1','-vf','scale=1:1','-f','rawvideo','-pix_fmt','rgb24','-'],
            capture_output=True,check=True).stdout
        assert pixel[2]>pixel[0]+30 and pixel[2]>pixel[1]+30
    if animated == 'fallback':
        assert len(video_provider.requests)==1
        assert engine.store.fallbacks(run.identifier)[0]['to_mode']=='Camera Motion'
        plan,report=build_render_plan(session,value.render_settings)
        assert report.ready and all(s.asset_kind=='still' for s in plan.segments)

@pytest.mark.skipif(not shutil.which('ffmpeg') or not shutil.which('ffprobe'),reason='FFmpeg unavailable')
def test_provider_reconciliation_does_not_resubmit_paid_image(tmp_path):
    from test_image_generation import png
    from prose_to_video.models.image_job import ProviderResult,ProviderImage
    session,scenes,_=prepared(tmp_path)
    shot=session.database.load_storyboard(scenes[0].identifier).shots[0]
    previous=ImageStore(session.root).approved(shot.identifier)[0]
    (session.root/previous.path).unlink()
    class AsyncImages(FakeImageProvider):
        def __init__(self):super().__init__();self.poll_count=0
        def submit(self,request):
            self.requests.append(request)
            return ProviderResult(status='Submitted',provider_job_id='remote-1')
        def poll(self,provider_job_id):
            self.poll_count+=1
            if self.poll_count==1:
                return ProviderResult(status='Running',provider_job_id=provider_job_id)
            return ProviderResult(status='Completed',provider_job_id=provider_job_id,
                images=[ProviderImage(data=png(),model='fake')],actual_cost=.1)
    provider=AsyncImages();engine=ProductionOrchestrator(session,image_provider=provider)
    run=engine.start(profile(session),paid_confirmed=True)
    interrupted=engine.run_until_pause(run.identifier)
    assert interrupted.status=='Interrupted'
    assert len(provider.requests)==1
    engine.resume(run.identifier)
    finished=engine.run_until_pause(run.identifier)
    assert finished.status=='Completed With Warnings',finished.error
    assert len(provider.requests)==1 and provider.poll_count==2
    assert ImageStore(session.root).approved(shot.identifier)[0].identifier!=previous.identifier
    assert finished.known_cost==pytest.approx(.1)
    assert finished.unknown_cost_count>=0

def test_unknown_language_cost_blocks_before_submission(tmp_path):
    from test_video_render import make_wav
    from prose_to_video.storage.project_files import create_project,import_prose,import_audio
    manuscript=tmp_path/'text.txt';manuscript.write_text('A quiet scene.')
    narration=tmp_path/'voice.wav';make_wav(narration,1)
    session=import_audio(import_prose(create_project(tmp_path,'Unknown cost'),manuscript),narration)
    class NoRequests:
        def propose_boundaries(self,payload):raise AssertionError('Paid request was submitted')
    engine=ProductionOrchestrator(session,language_provider=NoRequests())
    value=profile(session).model_copy(update={'authorize_unknown_cost':False})
    run=engine.start(value,paid_confirmed=True)
    result=engine.run_until_pause(run.identifier)
    assert result.status=='Blocked'
    blocked=[t for t in engine.store.tasks(run.identifier) if t.status=='Blocked']
    assert blocked[0].kind=='scenes' and blocked[0].failure_kind=='Budget'


def test_review_milestone_and_selective_camera_update(tmp_path):
    session,scenes,provider=prepared(tmp_path)
    board=session.database.load_storyboard(scenes[0].identifier)
    board.shots[0].camera_movement='Push in'
    session.database.save_storyboard(board,'First view. Second view.',replace=True)
    engine=ProductionOrchestrator(session)
    value=profile(session).model_copy(update={'automation_mode':'Review at Milestones',
                                               'authorize_auto_approval':False})
    report=engine.dry_run(value)
    assert all(t.status=='Reused' for t in report['tasks'] if t.kind in {'image_prompt','image'})
    assert not provider.requests or len(provider.requests)==2
    run=engine.start(value)
    paused=engine.run_until_pause(run.identifier)
    assert paused.status=='Awaiting Approval' and paused.output_path and not paused.manifest_path
    checkpoint=next(t for t in engine.store.tasks(run.identifier) if t.status=='Awaiting Approval')
    assert checkpoint.kind=='render'
    engine.approve_checkpoint(run.identifier,checkpoint.identifier)
    final=engine.run_until_pause(run.identifier)
    assert final.status=='Completed With Warnings'
    assert engine.store.decisions(run.identifier)[0]['origin']=='Manual'

def test_stage_ten_offscreen_gui_setup_dashboard_and_issues(tmp_path):
    import subprocess
    import sys
    session,scenes,_=prepared(tmp_path)
    issue=QualityIssue(project_id=session.project.identifier,category='Image',severity='Warning',
                       description='Check image')
    ProductionStore(session.root).save_issue(issue)
    script='''
from pathlib import Path
from PySide6.QtWidgets import QApplication
from prose_to_video.storage.project_files import open_project
from prose_to_video.gui.main_window import MainWindow
from prose_to_video.gui.production_dashboard import ProductionSetupDialog,ProductionDashboard,QualityControlPanel
app=QApplication([])
session=open_project(Path(__import__('sys').argv[1]))
window=MainWindow()
assert window.production_action.text()=='Generate Complete Video'
assert 'Production Dashboard' in [window.workspace_tabs.tabText(i) for i in range(window.workspace_tabs.count())]
dialog=ProductionSetupDialog(session)
dialog.width.setValue(640)
assert dialog.value().render_settings.width==640
dashboard=ProductionDashboard();dashboard.bind(session)
assert dashboard.show_plan()['ready'] and dashboard.plan.count()>0
quality=QualityControlPanel();quality.bind(session)
assert quality.issues.count()==1
quality.severity.setCurrentText('Error')
assert quality.issues.count()==0
quality.close();dashboard.close();dialog.close();window.close()
app.processEvents()
print('offscreen production GUI passed')
'''
    result=subprocess.run([sys.executable,'-c',script,str(session.root)],
        capture_output=True,text=True,env={**os.environ,'QT_QPA_PLATFORM':'offscreen'},timeout=30)
    assert result.returncode==0,result.stdout+result.stderr
    assert 'offscreen production GUI passed' in result.stdout

def test_optional_visual_review_is_authorized_and_suggestions_persist(tmp_path):
    session,scenes,_=prepared(tmp_path)
    shot=session.database.load_storyboard(scenes[0].identifier).shots[0]
    image=ImageStore(session.root).approved(shot.identifier)[0]
    class Reviewer:
        estimated_cost=.01
        actual_cost=.008
        def review_image(self,path,context):
            assert context['prose']==shot.original_prose and 'style' in context
            return [{'severity':'Suggestion','description':'Check subject placement'}]
    qc=QualityController(session)
    with pytest.raises(PermissionError):
        qc.semantic_image_review(shot,session.root/image.path,Reviewer())
    engine=ProductionOrchestrator(session)
    run=engine.start(profile(session,budget_review=.015))
    findings=qc.semantic_image_review(shot,session.root/image.path,Reviewer(),authorized=True,run=run)
    assert findings[0].detection_method=='AI Suggestion'
    assert ProductionStore(session.root).issues(category='Image')[0].status=='Needs Review'
    assert engine.store.run(run.identifier).known_cost==pytest.approx(.008)
    with pytest.raises(PermissionError,match='budget'):
        qc.semantic_image_review(shot,session.root/image.path,Reviewer(),authorized=True,run=engine.store.run(run.identifier))

@pytest.mark.skipif(not shutil.which('ffmpeg') or not shutil.which('ffprobe'),reason='FFmpeg unavailable')
def test_bounded_parallel_image_tasks_respect_dependencies(tmp_path):
    from threading import Barrier
    from test_image_generation import png
    from prose_to_video.models.image_job import ProviderResult,ProviderImage
    session,scenes,_=prepared(tmp_path)
    ImageStore(session.root).save_settings(ImageStore(session.root).settings().model_copy(update={'max_concurrent_jobs':2}))
    for scene in scenes:
        shot=session.database.load_storyboard(scene.identifier).shots[0]
        asset=ImageStore(session.root).approved(shot.identifier)[0]
        (session.root/asset.path).unlink()
    gate=Barrier(2)
    class ParallelImages(FakeImageProvider):
        def submit(self,request):
            self.requests.append(request)
            gate.wait(timeout=5)
            return ProviderResult(status='Completed',images=[ProviderImage(data=png(),model='fake')],
                                  actual_cost=.1)
    provider=ParallelImages();engine=ProductionOrchestrator(session,image_provider=provider)
    base=profile(session)
    value=base.model_copy(update={'concurrency':{**base.concurrency,'image':2}})
    run=engine.start(value,paid_confirmed=True)
    final=engine.run_until_pause(run.identifier)
    assert final.status=='Completed With Warnings',final.error
    assert len(provider.requests)==2 and final.known_cost==pytest.approx(.2)

@pytest.mark.skipif(not shutil.which('ffmpeg') or not shutil.which('ffprobe'),reason='FFmpeg unavailable')
def test_transient_local_task_retries_once(tmp_path,monkeypatch):
    session,_,_=prepared(tmp_path)
    engine=ProductionOrchestrator(session)
    run=engine.start(profile(session))
    original=engine._execute
    calls=[]
    def flaky(current,task):
        if task.kind=='coverage' and not calls:
            calls.append(task.identifier)
            raise TimeoutError('temporary local timeout')
        return original(current,task)
    monkeypatch.setattr(engine,'_execute',flaky)
    final=engine.run_until_pause(run.identifier)
    assert final.status=='Completed With Warnings',final.error
    coverage=next(t for t in engine.store.tasks(run.identifier) if t.kind=='coverage')
    assert coverage.attempts==2

def test_migration_nine_to_ten_preserves_project_and_scenes(tmp_path):
    session,scenes,_=prepared(tmp_path)
    with sqlite3.connect(session.database.path) as con:
        for table in ('production_manifest','production_fallback','production_usage','production_issue',
                      'production_checkpoint','production_dependency','production_task','production_run',
                      'production_profile'):
            con.execute(f'DROP TABLE {table}')
        con.execute('PRAGMA user_version=9')
    digest,loaded=session.database.load_scenes()
    assert [s.identifier for s in loaded]==[s.identifier for s in scenes]
    with sqlite3.connect(session.database.path) as con:
        assert con.execute('PRAGMA user_version').fetchone()[0]==10
        assert con.execute("SELECT name FROM sqlite_master WHERE name='production_manifest'").fetchone()

def test_known_image_budget_blocks_before_provider_submission(tmp_path):
    session,scenes,_=prepared(tmp_path)
    shot=session.database.load_storyboard(scenes[0].identifier).shots[0]
    asset=ImageStore(session.root).approved(shot.identifier)[0]
    (session.root/asset.path).unlink()
    provider=FakeImageProvider();engine=ProductionOrchestrator(session,image_provider=provider)
    run=engine.start(profile(session,budget_image=.05),paid_confirmed=True)
    result=engine.run_until_pause(run.identifier)
    assert result.status=='Blocked' and not provider.requests
    assert not any(j.status=='Submitted' for j in ImageStore(session.root).jobs())

def test_manual_prompt_edit_pauses_and_invalidates_only_its_shot(tmp_path):
    session,scenes,_=prepared(tmp_path)
    first=session.database.load_storyboard(scenes[0].identifier).shots[0]
    second=session.database.load_storyboard(scenes[1].identifier).shots[0]
    store=PromptStore(session.root)
    approved=store.approved(first.identifier)
    edited=store.edit(first.identifier,positive_prompt=approved.positive_prompt+' A new lantern.',
                      negative_prompt=approved.negative_prompt,components=approved.components)
    assert edited.identifier!=approved.identifier
    engine=ProductionOrchestrator(session)
    report=engine.dry_run(profile(session))
    states={(t.kind,t.shot_id):t.status for t in report['tasks']}
    assert states['image_prompt',first.identifier]=='Awaiting Approval'
    assert states['image',first.identifier]=='Pending'
    assert states['image',second.identifier]=='Reused'
    assert next(t for t in report['tasks'] if t.kind=='alignment').status=='Reused'
    run=engine.start(profile(session),paid_confirmed=True)
    result=engine.run_until_pause(run.identifier)
    assert result.status=='Awaiting Approval'
    scene=scenes[0];board=session.database.load_storyboard(scene.identifier)
    store.approve(first.identifier,edited.version,scene,first,board,session.project,VisualLibrary(session.root))
    checkpoint=next(t for t in engine.store.tasks(run.identifier) if t.status=='Awaiting Approval')
    engine.approve_checkpoint(run.identifier,checkpoint.identifier)
    assert engine.store.tasks(run.identifier)

@pytest.mark.skipif(not shutil.which('ffmpeg') or not shutil.which('ffprobe'),reason='FFmpeg unavailable')
def test_animation_review_uses_three_frames_and_marks_suggestion(tmp_path):
    import subprocess
    session,scenes,_=prepared(tmp_path)
    shot=session.database.load_storyboard(scenes[0].identifier).shots[0]
    image=ImageStore(session.root).approved(shot.identifier)[0]
    clip=tmp_path/'sample.mp4'
    subprocess.run(['ffmpeg','-v','error','-f','lavfi','-i','color=c=blue:s=320x240:r=10:d=1',
        '-c:v','libx264','-pix_fmt','yuv420p','-y',str(clip)],check=True)
    class Reviewer:
        def review_frames(self,frames,context):
            assert len(frames)==3 and all(p.is_file() for p in frames)
            assert context['motion_prompt']=='Ada moves'
            return [{'description':'Possible background drift'}]
    qc=QualityController(session)
    with pytest.raises(PermissionError):
        qc.semantic_animation_review(shot,clip,session.root/image.path,'Ada moves',Reviewer())
    findings=qc.semantic_animation_review(shot,clip,session.root/image.path,'Ada moves',Reviewer(),authorized=True)
    assert findings[0].category=='Animation' and findings[0].detection_method=='AI Suggestion'
    assert ProductionStore(session.root).issues(category='Animation')[0].status=='Needs Review'


def test_unknown_review_cost_with_finite_budget_never_submits(tmp_path):
    session,scenes,_=prepared(tmp_path)
    shot=session.database.load_storyboard(scenes[0].identifier).shots[0]
    image=ImageStore(session.root).approved(shot.identifier)[0]
    run=ProductionOrchestrator(session).start(profile(session,budget_review=.1))
    class Reviewer:
        def review_image(self,path,context):
            raise AssertionError('Review request was submitted despite unknown cost')
    with pytest.raises(PermissionError,match='finite budget'):
        QualityController(session).semantic_image_review(shot,session.root/image.path,Reviewer(),
            authorized=True,run=run)


@pytest.mark.skipif(not shutil.which('ffmpeg') or not shutil.which('ffprobe'),reason='FFmpeg unavailable')
def test_final_output_must_still_validate_at_completion(tmp_path):
    session,_,_=prepared(tmp_path)
    engine=ProductionOrchestrator(session)
    run=engine.start(profile(session))
    for _ in range(20):
        run=engine.step(run.identifier)
        if run.current_stage=='manifest' and any(
                t.kind=='manifest' and t.status=='Completed' for t in engine.store.tasks(run.identifier)):
            break
    else:
        pytest.fail('Production did not reach manifest generation')
    assert run.output_path and run.manifest_path
    (session.root/run.output_path).unlink()
    final=engine.step(run.identifier)
    assert final.status=='Failed' and 'validation' in final.error.lower()


def test_timeline_gap_quality_threshold_blocks_render(tmp_path):
    session,_,_=prepared(tmp_path)
    engine=ProductionOrchestrator(session)
    value=profile(session).model_copy(update={'quality_thresholds':{
        'accept_render_warnings':True,'max_timeline_gap':.1}})
    run=engine.start(value)
    final=engine.run_until_pause(run.identifier)
    assert final.status=='Failed' and 'timeline gap' in final.error
    assert final.output_path is None


def test_manifest_excludes_nested_credentials(tmp_path):
    import json
    session,_,_=prepared(tmp_path)
    engine=ProductionOrchestrator(session)
    run=engine.start(profile(session,narrative_settings={'provider':'Fake',
        'model':'fake-v1','api_key':'top-secret'}))
    manifest=json.dumps(engine.manifest(run))
    assert 'top-secret' not in manifest
    assert 'fake-v1' in manifest and '[redacted]' in manifest
