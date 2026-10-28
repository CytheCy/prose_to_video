from prose_to_video.storage.database import SCHEMA_VERSION
"""Stage Seven deterministic image generation and storage tests."""
import json
import os
import sqlite3
from pathlib import Path
from types import SimpleNamespace
import pytest
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
from PySide6.QtCore import QByteArray,QBuffer,QIODevice
from PySide6.QtGui import QImage
from PySide6.QtWidgets import QApplication
from test_image_prompts import setup,FakeProvider as PromptProvider
from prose_to_video.models.image_prompt import PromptSettings
from prose_to_video.models.image_job import ImageCapabilities,ImageRequest,ImageGenerationSettings,ProviderImage,ProviderResult
from prose_to_video.pipeline.image_prompt_generation import generate_prompt
from prose_to_video.pipeline.image_generation import ImageGenerator,eligible_shots,select_references
from prose_to_video.pipeline.image_providers.base import ImageProvider,ImageProviderError
from prose_to_video.pipeline.image_providers.openai_provider import OpenAIImageProvider
from prose_to_video.pipeline.image_providers.comfyui_provider import ComfyUIProvider
from prose_to_video.storage.image_prompts import PromptStore
from prose_to_video.storage.generated_images import ImageStore
from prose_to_video.storage.image_export import export_approved_images,visual_timeline_manifest
from prose_to_video.gui.image_gallery_panel import ImageGalleryPanel

APP=QApplication.instance() or QApplication([])


def png():
    image=QImage(64,64,QImage.Format_RGB32);image.fill(0xff336699)
    buffer=QBuffer();buffer.open(QIODevice.ReadWrite);assert image.save(buffer,'PNG')
    return bytes(buffer.data())


class FakeImageProvider(ImageProvider):
    name='Fake'
    def __init__(self):self.requests=[]
    def test_connection(self):return 'Connected'
    def capabilities(self,model):
        return ImageCapabilities(references=True,multiple_references=True,negative_prompt=True,
            seeds=True,multiple_candidates=True,exact_dimensions=True,editing=True,
            max_references=2,max_candidates=10,formats=['png'],qualities=['medium'])
    def submit(self,request):
        self.validate(request);self.requests.append(request)
        return ProviderResult(status='Completed',images=[ProviderImage(data=png(),model=request.model)
            for _ in range(request.candidates)])


def fixture(tmp_path):
    session,scene,board,library,_=setup(tmp_path)
    prompt=generate_prompt(board.shots[0],scene,board,library,session.project,PromptSettings(),PromptProvider())
    store=PromptStore(session.root);store.save(prompt);store.approve(prompt.shot_id,1,scene,board.shots[0],board,session.project,library)
    return session,scene,board,store


def test_fake_provider_generation_assets_approval_reopen_export(tmp_path):
    session,scene,board,prompts=fixture(tmp_path)
    settings=ImageGenerationSettings(provider='Fake',model='fake-v1',width=1536,height=864,candidates=2)
    store=ImageStore(session.root);store.save_settings(settings)
    provider=FakeImageProvider();generator=ImageGenerator(session,provider)
    job,request,_=generator.enqueue(scene,board.shots[0],board)
    assert job.status=='Pending' and job.prompt_id==prompts.approved(job.shot_id).identifier
    generator.run(job,request)
    assert job.status=='Completed' and len(provider.requests)==1
    assets=store.assets(job.shot_id);assert len(assets)==2 and assets[0].identifier!=assets[1].identifier
    assert all((session.root/a.path).is_file() and (session.root/a.thumbnail_path).is_file() for a in assets)
    assert all(a.width==64 and a.height==64 and a.warnings for a in assets)
    store.approve(assets[0].identifier,lock=True)
    with pytest.raises(ValueError):store.approve(assets[1].identifier)
    store.lock(job.shot_id,False);store.approve(assets[1].identifier)
    assert store.asset(assets[0].identifier).approval_status=='Historical'
    assert ImageStore(session.root).approved(job.shot_id)[0].identifier==assets[1].identifier
    destination=tmp_path/'export';manifest=export_approved_images(session,[scene],destination)
    assert json.loads(manifest.read_text())[0]['asset_id']==assets[1].identifier
    with pytest.raises(FileExistsError):export_approved_images(session,[scene],destination)
    assert visual_timeline_manifest(session,[scene])[0]['validity']=='Unresolved timing'
    gallery=ImageGalleryPanel();gallery.set_shot(board.shots[0],assets,assets[1],session.root)
    assert gallery.list_widget.count()==2


def test_recovery_budget_and_missing_asset(tmp_path):
    session,scene,board,_=fixture(tmp_path)
    store=ImageStore(session.root)
    store.save_settings(ImageGenerationSettings(provider='Fake',model='fake-v1',spending_cap=0.1))
    generator=ImageGenerator(session,FakeImageProvider())
    job,request,_=generator.enqueue(scene,board.shots[0],board)
    with pytest.raises(ValueError,match='cap'):generator.run(job,request)
    assert job.status=='Pending'
    store.save_settings(ImageGenerationSettings(provider='Fake',model='fake-v1'))
    generator.run(job,request)
    asset=store.assets(job.shot_id)[0]
    (session.root/asset.path).unlink()
    assert store.missing_assets()[0].identifier==asset.identifier
    other,request,_=generator.enqueue(scene,board.shots[0],board)
    other.status='Submitted';store.update_job(other)
    assert store.recover()[0].status=='Needs Reconciliation'
    assert generator.reconcile(store.job(other.identifier)).status=='Needs Reconciliation'


def test_openai_request_construction_and_capabilities():
    calls=[]
    class Images:
        def generate(self,**kw):
            calls.append(('generate',kw));return SimpleNamespace(data=[SimpleNamespace(b64_json='YWJj')],model='gpt-image-2.5-flare',usage=None)
        def edit(self,**kw):
            calls.append(('edit',kw));return SimpleNamespace(data=[SimpleNamespace(b64_json='YWJj')],model=None,usage=None)
    provider=OpenAIImageProvider(client=SimpleNamespace(images=Images()))
    request=ImageRequest(prompt='a scene',model='gpt-image-2.5-flare',width=1536,height=864,
        quality='medium',candidates=2,output_format='png')
    assert provider.submit(request).images[0].model=='gpt-image-2.5-flare'
    assert calls[0][1]['n']==2 and calls[0][1]['size']=='1536x864'
    assert 'negative_prompt' not in calls[0][1]
    with pytest.raises(ImageProviderError):provider.validate(request.model_copy(update={'seed':1}))
    with pytest.raises(ImageProviderError):provider.validate(request.model_copy(update={'negative_prompt':'bad'}))
    with pytest.raises(ImageProviderError):provider.capabilities('dall-e-3')


def test_comfy_mapping_and_validation(tmp_path):
    workflow={'1':{'class_type':'CLIPTextEncode','inputs':{'text':''}},
              '2':{'class_type':'EmptyLatentImage','inputs':{'width':512,'height':512,'batch_size':1}},
              '3':{'class_type':'SaveImage','inputs':{'images':['2',0]}}}
    mapping={'positive_prompt':{'node':'1','input':'text'},'width':{'node':'2','input':'width'},
             'height':{'node':'2','input':'height'},'candidates':{'node':'2','input':'batch_size'},'outputs':['3']}
    provider=ComfyUIProvider(workflow=workflow,mapping=mapping)
    calls=[];provider._json=lambda route,body=None:(calls.append((route,body)) or {'prompt_id':'abc'})
    request=ImageRequest(prompt='a scene',model='',width=640,height=384,quality='auto',candidates=2)
    assert provider.submit(request).provider_job_id=='abc'
    assert calls[0][1]['prompt']['1']['inputs']['text']=='a scene'
    assert calls[0][1]['prompt']['2']['inputs']['width']==640
    assert workflow['2']['inputs']['width']==512
    with pytest.raises(ImageProviderError):provider.validate(request.model_copy(update={'negative_prompt':'bad'}))
    with pytest.raises(ImageProviderError):ComfyUIProvider(workflow=workflow,mapping={'outputs':['3']}).validate(request)


def test_reference_priority_and_stale_prompt(tmp_path):
    session,scene,board,_=fixture(tmp_path)
    refs=[{'asset_id':'z','path':'references/z.png','role':'location','primary':False},
          {'asset_id':'b','path':'references/b.png','role':'portrait','primary':True},
          {'asset_id':'a','path':'references/a.png','role':'character','primary':False}]
    included,excluded=select_references(refs,session.root,2)
    assert [r.asset_id for r in included]==['b','a'] and excluded==['z']
    prompt_store=PromptStore(session.root)
    changed=prompt_store.approved(board.shots[0].identifier)
    changed.dependency_fingerprint='old'
    prompt_store._update(changed)
    generator=ImageGenerator(session,FakeImageProvider())
    with pytest.raises(ValueError,match='outdated'):generator.prepare(scene,board.shots[0],board)
    assert generator.prepare(scene,board.shots[0],board,allow_outdated=True)[2]


def test_job_transition_and_cancellation(tmp_path):
    session,scene,board,_=fixture(tmp_path)
    store=ImageStore(session.root);store.save_settings(ImageGenerationSettings(provider='Fake',model='fake-v1'))
    generator=ImageGenerator(session,FakeImageProvider())
    job,request,_=generator.enqueue(scene,board.shots[0],board)
    assert generator.cancel(job)
    assert store.job(job.identifier).status=='Canceled'
    job.status='Running'
    with pytest.raises(ValueError,match='transition'):store.update_job(job)


def test_dependency_freshness_and_portability(tmp_path):
    from prose_to_video.pipeline.image_generation import asset_freshness
    from prose_to_video.storage.project_files import open_project
    from prose_to_video.storage.visual_library import VisualLibrary
    import shutil
    session,scene,board,_=fixture(tmp_path)
    store=ImageStore(session.root);store.save_settings(ImageGenerationSettings(provider='Fake',model='fake-v1'))
    generator=ImageGenerator(session,FakeImageProvider())
    job,request,_=generator.enqueue(scene,board.shots[0],board);generator.run(job,request)
    asset=store.assets(job.shot_id)[0]
    assert asset_freshness(session,scene,board.shots[0],board,asset)=='Current'
    moved=tmp_path/'Moved';shutil.move(session.root,moved)
    reopened=open_project(moved)
    assert (reopened.root/asset.path).is_file()
    entity=VisualLibrary(reopened.root).entities()[0]
    entity.appearance_description='changed';VisualLibrary(reopened.root).save_entity(entity)
    assert asset_freshness(reopened,scene,board.shots[0],board,asset)=='Outdated'


def test_rate_limit_retry_and_uncertain_request(tmp_path):
    session,scene,board,_=fixture(tmp_path)
    store=ImageStore(session.root);store.save_settings(ImageGenerationSettings(provider='Fake',model='fake-v1',retry_limit=1))
    class Throttle(FakeImageProvider):
        def submit(self,request):
            self.requests.append(request)
            if len(self.requests)==1:raise ImageProviderError('rate limit',recoverable=True)
            return ProviderResult(status='Completed',images=[ProviderImage(data=png(),model=request.model)])
    provider=Throttle();generator=ImageGenerator(session,provider)
    job,request,_=generator.enqueue(scene,board.shots[0],board);generator.run(job,request)
    assert job.status=='Completed' and len(provider.requests)==2
    class Timeout(FakeImageProvider):
        def submit(self,request):raise ImageProviderError('timeout',uncertain=True)
    generator=ImageGenerator(session,Timeout())
    other,request,_=generator.enqueue(scene,board.shots[0],board);generator.run(other,request)
    assert other.status=='Needs Reconciliation'
    assert store.job(other.identifier).status=='Needs Reconciliation'


def test_approval_transaction_rolls_back(tmp_path):
    session,scene,board,_=fixture(tmp_path)
    store=ImageStore(session.root);store.save_settings(ImageGenerationSettings(provider='Fake',model='fake-v1',candidates=2))
    generator=ImageGenerator(session,FakeImageProvider())
    job,request,_=generator.enqueue(scene,board.shots[0],board);generator.run(job,request)
    assets=store.assets(job.shot_id);store.approve(assets[0].identifier)
    with sqlite3.connect(store.db.path) as con:
        con.execute('CREATE TRIGGER fail_approval BEFORE INSERT ON approved_image BEGIN SELECT RAISE(ABORT,"blocked"); END')
    with pytest.raises(sqlite3.IntegrityError):store.approve(assets[1].identifier)
    assert store.approved(job.shot_id)[0].identifier==assets[0].identifier
    assert store.asset(assets[0].identifier).approval_status=='Approved'


def test_version_seven_migration_keeps_prompt(tmp_path):
    from prose_to_video.storage.database import MIGRATIONS,ProjectDatabase
    database=tmp_path/'legacy.sqlite'
    with sqlite3.connect(database) as con:
        for version in range(1,7):
            con.executescript('BEGIN;'+MIGRATIONS[version]+f'PRAGMA user_version={version}; COMMIT;')
        con.execute('INSERT INTO image_prompt VALUES(?,?,?,?,?,?,?,?)',
            ('prompt','shot','scene',1,'Approved','fingerprint','2026-01-01','{}'))
    db=ProjectDatabase(database)
    with db._connect() as con:
        db._check_schema(con)
        assert con.execute('PRAGMA user_version').fetchone()[0]==SCHEMA_VERSION
        assert con.execute('SELECT identifier FROM image_prompt').fetchone()[0]=='prompt'
        assert con.execute('SELECT count(*) FROM generated_image').fetchone()[0]==0


def test_comfy_reconciliation_and_gallery_signals(tmp_path):
    session,scene,board,_=fixture(tmp_path)
    workflow={'1':{'class_type':'CLIPTextEncode','inputs':{'text':''}},
              '2':{'class_type':'EmptyLatentImage','inputs':{'width':1,'height':1}},
              '3':{'class_type':'SaveImage','inputs':{'images':['2',0]}}}
    mapping={'positive_prompt':{'node':'1','input':'text'},'width':{'node':'2','input':'width'},
             'height':{'node':'2','input':'height'},'outputs':['3']}
    provider=ComfyUIProvider(workflow=workflow,mapping=mapping)
    provider._json=lambda route,body=None: {'id':{'status':{'completed':True},'outputs':{'3':{'images':[]}}}} if route.startswith('/history/') else {'queue_pending':[],'queue_running':[]}
    assert provider.poll('id').status=='Failed'
    gallery=ImageGalleryPanel();approved=[];gallery.approve_requested.connect(approved.append)
    gallery.set_shot(board.shots[0],[],None,session.root)
    assert gallery.status.text().endswith('Missing image')
    gallery.buttons['Approve Image'].click()
    assert approved==['']


def test_gui_batch_progress_and_approval(tmp_path,monkeypatch):
    import time
    import prose_to_video.pipeline.image_generation as pipeline
    from prose_to_video.gui.main_window import MainWindow
    from PySide6.QtWidgets import QMessageBox
    session,scene,board,_=fixture(tmp_path)
    ImageStore(session.root).save_settings(ImageGenerationSettings(provider='Fake',model='fake-v1'))
    monkeypatch.setattr(pipeline,'make_image_provider',lambda settings,root=None:FakeImageProvider())
    monkeypatch.setattr(QMessageBox,'question',lambda *args,**kwargs:QMessageBox.Yes)
    window=MainWindow();window._set_session(session,load_prose=False)
    window.scenes_panel.set_scenes([scene]);window.storyboard_panel.set_board(board)
    window._generate_images('shot')
    task=window._image_task
    deadline=time.monotonic()+5
    while window._image_task and time.monotonic()<deadline:
        APP.processEvents();time.sleep(.01)
    APP.processEvents()
    task.wait()
    assert window._image_task is None
    assert window.image_gallery_panel.list_widget.count()==1
    asset=ImageStore(session.root).assets(board.shots[0].identifier)[0]
    window._approve_image(asset.identifier)
    assert ImageStore(session.root).approved(asset.shot_id)[0].identifier==asset.identifier
    window.close()


def test_refinement_preserves_parent_and_prompt(tmp_path):
    session,scene,board,prompts=fixture(tmp_path)
    store=ImageStore(session.root);store.save_settings(ImageGenerationSettings(provider='Fake',model='fake-v1'))
    generator=ImageGenerator(session,FakeImageProvider())
    original,request,_=generator.enqueue(scene,board.shots[0],board);generator.run(original,request)
    parent=store.assets(original.shot_id)[0]
    child,request,_=generator.enqueue(scene,board.shots[0],board,parent_id=parent.identifier,
        revision_instruction='Make the lighting warmer')
    assert 'Revision request: Make the lighting warmer' in request.prompt
    assert request.parent_path==str(session.root/parent.path)
    generator.run(child,request)
    derived=next(a for a in store.assets(original.shot_id) if a.job_id==child.identifier)
    assert derived.parent_id==parent.identifier
    assert prompts.approved(original.shot_id).positive_prompt not in {'','Make the lighting warmer'}
