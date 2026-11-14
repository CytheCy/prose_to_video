"""Behavioral checks for direct scenes/stills; providers never contact a network."""
from pathlib import Path
from types import SimpleNamespace
from threading import Event
import pytest
from prose_to_video.models.scene import Scene, source_hash, validate_partition
from prose_to_video.models.alignment import AlignmentRun, WordAlignment
from prose_to_video.pipeline.providers import AISettings
from prose_to_video.pipeline.stills_workflow import (Workflow, Still, Identity, Staging, CatalogResult,
    Beats, analyze, load_workflow, build_timeline, Interval, dependency, generate_still)
from prose_to_video.storage.project_files import create_project, import_prose


def session_with_text(tmp_path, text):
    session = create_project(tmp_path, 'story')
    path = tmp_path/'input.txt'; path.write_bytes(text.encode())
    return import_prose(session,path)


class FakeAI:
    def __init__(self): self.calls=[]
    def _request(self, instructions, content, schema):
        import json
        content=json.loads(content); self.calls.append((schema,content))
        if schema is CatalogResult:
            known=content['known']
            return schema(identities=[Identity(identifier=known[0]['identifier'] if known else 'person',
                kind='Character',name='Mira',description='short black hair; green coat')])
        if schema is Beats:
            return schema(beats=[{'first_id':1,'last_id':len(content['passages'])}])
        values={name:[] if field.annotation==list[str] else '' for name,field in Staging.model_fields.items()}
        values.update(title='The turn',render_type='cinematic',identity_links=['person'],
            foreground='doorframe',middle_ground='woman turns',background='stone wall',
            final_prompt='A woman with short black hair in a green coat turns toward the door. Doorframe foreground, stone wall behind, side lighting; illustrated story, 1536:864.')
        return schema(**values)


def test_exact_repeated_crlf_and_retry(tmp_path):
    text='Mira waits.\r\nShe turns.\r\nMira waits.\r\n'
    session=session_with_text(tmp_path,text); state=Workflow(); provider=FakeAI()
    analyze(session,state,provider,AISettings())
    validate_partition(text,state.scenes)
    assert ''.join(s.text for s in state.scenes)==text
    assert all(s.source_version==source_hash(text) for s in state.scenes)
    state.scenes[0].final_prompt='MANUAL PROMPT'; state.scenes[0].manual_fields=['final_prompt']; state.save(session.root)
    restored=load_workflow(session); ids=[s.identifier for s in restored.scenes]
    analyze(session,restored,provider,AISettings())
    assert [s.identifier for s in restored.scenes]==ids
    assert restored.scenes[0].final_prompt=='MANUAL PROMPT'
    assert len(restored.catalogs)==1
    assert restored.scenes[0].foreground=='doorframe'


def test_cancel_checkpoints_and_resume(tmp_path):
    session=session_with_text(tmp_path,'Mira waits. She turns.'); state=Workflow(); cancel=Event()
    def progress(message):
        if message.startswith('Writing'):cancel.set()
    with pytest.raises(ValueError,match='Canceled'):
        analyze(session,state,FakeAI(),AISettings(),cancel,progress)
    assert state.checkpoints
    resumed=load_workflow(session); analyze(session,resumed,FakeAI(),AISettings())
    assert resumed.scenes


@pytest.mark.parametrize('links, expected', [
    (['person'], ['person']),
    ([' Mira ', 'MIRA', 'person'], ['person']),
    (['The traveler'], ['person']),
    ([], []),
])
def test_analysis_resolves_identity_names_and_aliases(tmp_path, links, expected):
    class NamedLinksAI(FakeAI):
        def _request(self, instructions, content, schema):
            result = super()._request(instructions, content, schema)
            if schema is CatalogResult:
                result.identities[0].aliases = ['The traveler']
            if schema is Staging:
                assert 'exact identifier values' in instructions
                result.identity_links = links
            return result
    session = session_with_text(tmp_path, 'Mira waits.')
    state = Workflow()
    provider = NamedLinksAI()
    analyze(session, state, provider, AISettings())
    assert state.scenes[0].identity_links == expected
    assert load_workflow(session).scenes[0].identity_links == expected
    assert sum(schema is Staging for schema, _ in provider.calls) == 1


def test_analysis_retries_unknown_identity_links(tmp_path):
    class CorrectingAI(FakeAI):
        def _request(self, instructions, content, schema):
            result = super()._request(instructions, content, schema)
            if schema is Staging and 'identity_link_correction' not in self.calls[-1][1]:
                result.identity_links = ['invented-id']
            return result
    session = session_with_text(tmp_path, 'Mira waits.')
    state = Workflow()
    provider = CorrectingAI()
    messages = []
    analyze(session, state, provider, AISettings(), progress=messages.append)
    requests = [content for schema, content in provider.calls if schema is Staging]
    assert len(requests) == 2
    assert requests[1]['identity_link_correction']['invalid_links'] == ['invented-id']
    assert state.scenes[0].identity_links == ['person']
    assert all('invented-id' not in value.get('identity_links', []) for value in state.checkpoints.values())
    assert any('Retry 1/2' in message for message in messages)


def test_invalid_identity_retries_are_bounded_and_resume_saved_scenes(tmp_path):
    class InvalidAI(FakeAI):
        def _request(self, instructions, content, schema):
            result = super()._request(instructions, content, schema)
            if schema is Staging and self.calls[-1][1]['prose'] == 'She turns.':
                result.identity_links = ['unknown-place']
            return result
    session = session_with_text(tmp_path, 'Mira waits.\n\nShe turns.')
    state = Workflow(scene_detection='paragraph')
    provider = InvalidAI()
    with pytest.raises(ValueError, match='after 3 attempts.*unknown-place'):
        analyze(session, state, provider, AISettings())
    assert sum(schema is Staging for schema, _ in provider.calls) == 4
    restored = load_workflow(session)
    assert len(restored.scenes) == 1
    first_id = restored.scenes[0].identifier
    assert all('unknown-place' not in value.get('identity_links', []) for value in restored.checkpoints.values())
    retry = FakeAI()
    analyze(session, restored, retry, AISettings())
    assert len(restored.scenes) == 2
    assert restored.scenes[0].identifier == first_id
    assert [schema for schema, _ in retry.calls] == [Staging]


def test_identity_resolution_does_not_guess_shared_names():
    from prose_to_video.pipeline.stills_workflow import resolve_identity_links
    catalog = [Identity(identifier='person', kind='Character', name='Mira',
                        aliases=['Traveler'], description=''),
               Identity(identifier='place', kind='Place', name='Traveler', description='')]
    assert resolve_identity_links(['Traveler', 'missing'], catalog) == ([], ['Traveler', 'missing'])
    assert resolve_identity_links(['person', 'place'], catalog) == (['person', 'place'], [])


def test_changed_prose_rebuilds_analysis_and_prompts_keeps_references(tmp_path):
    session = session_with_text(tmp_path, 'Mira waits. She turns.')
    state = Workflow(style='Ink drawing', density='High')
    analyze(session, state, FakeAI(), AISettings())
    old_scene = state.scenes[0]
    old_scene.final_prompt = 'OLD MANUAL PROMPT'
    old_scene.manual_fields = ['final_prompt']
    state.catalogs[0].description = 'OLD MANUAL DESCRIPTION'
    state.catalogs[0].manual = True
    state.catalogs[0].references = ['references/mira.png']
    state.catalogs.extend([
        Identity(identifier='old-place', kind='Place', name='Old Hall', aliases=['Museum'],
                 description='OLD PLACE', references=['references/museum.png'], manual=True),
        Identity(kind='Character', name='Removed person', description='OLD PERSON'),
        Identity(kind='Character', name='Absent with reference', description='OLD ABSENT',
                 references=['references/absent.png'])])
    for name in ['mira', 'museum', 'absent']:
        (session.root / 'references' / (name + '.png')).write_bytes(name.encode())
    image = session.root / 'images' / 'old.png'
    image.write_bytes(b'old generated image')
    state.stills = [Still(scene_id=old_scene.identifier, source_hash=state.prose_hash,
        start=old_scene.start, end=old_scene.end, excerpt=old_scene.text,
        path='images/old.png', prompt=old_scene.final_prompt)]
    state.selected = {old_scene.identifier: state.stills[0].identifier}
    state.timeline = [Interval(scene_id=old_scene.identifier, start=0, end=10,
                               speech_start=0, speech_end=10)]
    state.timeline_audio_hash = 'old-audio'
    state.jobs = [{'status': 'Completed'}]
    old_checkpoints = set(state.checkpoints)
    new_text = 'Mira visits the museum. She leaves.'
    source = tmp_path / 'input.txt'
    source.write_text(new_text, encoding='utf-8')
    session = import_prose(session, source)

    class RevisedAI(FakeAI):
        def _request(self, instructions, content, schema):
            result = super()._request(instructions, content, schema)
            if schema is CatalogResult:
                result.identities.append(Identity(identifier='new-place', kind='Place',
                    name='Museum', description='Newly analyzed museum'))
            if schema is Staging:
                result.location = 'Museum'
                result.identity_links = ['person', 'new-place']
                result.final_prompt = 'NEW museum composition and prompt'
            return result

    provider = RevisedAI()
    analyze(session, state, provider, AISettings())
    assert [schema for schema, _ in provider.calls] == [CatalogResult, Beats, Staging]
    assert provider.calls[0][1]['known'] == []
    validate_partition(new_text, state.scenes)
    assert state.prose_hash == source_hash(new_text)
    assert state.scenes[0].identifier != old_scene.identifier
    assert state.scenes[0].final_prompt == 'NEW museum composition and prompt'
    assert state.scenes[0].manual_fields == []
    assert {c.name for c in state.catalogs} == {'Mira', 'Museum'}
    assert state.catalogs[0].description != 'OLD MANUAL DESCRIPTION'
    assert state.catalogs[0].references == ['references/mira.png']
    assert state.catalogs[1].references == ['references/museum.png']
    assert state.reference_identities[0].name == 'Absent with reference'
    assert state.reference_identities[0].description == ''
    assert not (old_checkpoints & set(state.checkpoints))
    assert state.analysis_plan == [(0, len(new_text), True)]
    assert not state.stills and not state.selected and not state.jobs
    assert not state.timeline and not state.timeline_audio_hash
    assert state.style == 'Ink drawing' and state.density == 'High'
    assert image.read_bytes() == b'old generated image'
    for name in ['mira', 'museum', 'absent']:
        assert (session.root / 'references' / (name + '.png')).read_bytes() == name.encode()
    assert load_workflow(session) == state


def test_changed_prose_failed_analysis_resumes_without_losing_references(tmp_path):
    session = session_with_text(tmp_path, 'Mira waits.')
    state = Workflow()
    analyze(session, state, FakeAI(), AISettings())
    state.catalogs[0].references = ['references/mira.png']
    source = tmp_path / 'input.txt'
    source.write_text('Mira leaves.', encoding='utf-8')
    session = import_prose(session, source)
    class FailingAI(FakeAI):
        def _request(self, instructions, content, schema):
            if schema is Beats:
                raise RuntimeError('Temporary failure')
            return super()._request(instructions, content, schema)
    with pytest.raises(RuntimeError, match='Temporary failure'):
        analyze(session, state, FailingAI(), AISettings())
    assert not state.scenes
    restored = load_workflow(session)
    provider = FakeAI()
    analyze(session, restored, provider, AISettings())
    assert all(schema is not CatalogResult for schema, _ in provider.calls)
    assert restored.scenes[0].text == 'Mira leaves.'
    assert restored.catalogs[0].references == ['references/mira.png']


def timeline_fixture():
    scenes=[Scene(identifier='a',sequence=1,start=0,end=2,text='A '),Scene(identifier='b',sequence=2,start=2,end=3,text='B')]
    state=Workflow(prose_hash='hash',scenes=scenes)
    for scene in scenes:
        still=Still(scene_id=scene.identifier,source_hash='hash',start=scene.start,end=scene.end,excerpt=scene.text,path='images/a.png',prompt='')
        state.stills.append(still);state.selected[scene.identifier]=still.identifier
    run=AlignmentRun(prose_hash='hash',audio_hash='audio',audio_path='source/a.wav',audio_duration=10,
        transcription_model='mock',device='cpu',precision='float32',config_fingerprint='',words=[
            WordAlignment(source_start=0,source_end=1,text='A',audio_start=2,audio_end=3,status='matched'),
            WordAlignment(source_start=2,source_end=3,text='B',audio_start=6,audio_end=7,status='matched')])
    return state,run


def test_timeline_holds_pauses_leading_trailing_and_manual():
    state,run=timeline_fixture(); result=build_timeline(state,run,'audio')
    assert [(t.start,t.end) for t in result]==[(0,6),(6,10)]
    assert [(t.speech_start,t.speech_end) for t in result]==[(2,3),(6,7)]
    result[0].end=5; result[0].manual=True; result[1].start=5; result[1].manual=True
    assert [(t.start,t.end) for t in build_timeline(state,run,'audio')]==[(0,5),(5,10)]
    with pytest.raises(ValueError,match='changed'):build_timeline(state,run,'different')
    assert state.timeline[0].end==5


def test_timeline_validation_identifies_bad_cut():
    from prose_to_video.pipeline.stills_workflow import validate_timeline
    timeline = [Interval(scene_id='a', start=0, end=5, speech_start=1, speech_end=2),
                Interval(scene_id='b', start=5.1, end=10, speech_start=6, speech_end=7)]
    with pytest.raises(ValueError, match=r'Still 2 starts at 5\.100s.*5\.000s'):
        validate_timeline(timeline, 10)
    timeline[1].start = 5
    timeline[1].end = 10.1
    with pytest.raises(ValueError, match=r'Still 2 ends at 10\.100s.*10\.000s'):
        validate_timeline(timeline, 10)


def test_render_plan_uses_current_audio_duration(tmp_path):
    import wave
    from prose_to_video.storage.project_files import import_audio
    from prose_to_video.pipeline.audio_transcription import source_file_hash
    from prose_to_video.pipeline.stills_workflow import render_plan
    session = session_with_text(tmp_path, 'A B')
    source = tmp_path / 'narration.wav'
    with wave.open(str(source), 'wb') as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(16000)
        audio.writeframes(b'\0\0' * 16000)
    session = import_audio(session, source)
    state, run = timeline_fixture()
    state.prose_hash = source_hash('A B')
    state.timeline_audio_hash = source_file_hash(session.project.resolved_audio(session.root))
    state.timeline = [Interval(scene_id='a', start=0, end=.5, speech_start=.1, speech_end=.3),
                      Interval(scene_id='b', start=.5, end=1, speech_start=.6, speech_end=.9)]
    (session.root / 'images').mkdir(exist_ok=True)
    (session.root / 'images/a.png').write_bytes(b'image')
    stale_session = type(session)(session.root, session.project.model_copy(update={'audio_duration': .9}))
    plan = render_plan(stale_session, state)
    assert plan.audio_duration == plan.source_end == 1
    state.timeline_audio_hash = 'previous-audio'
    with pytest.raises(ValueError, match='Audio changed'):
        render_plan(stale_session, state)
    with pytest.raises(ValueError, match='Audio changed'):
        render_plan(stale_session, state, approved_audio_hash='another-audio')
    approved = render_plan(stale_session, state, approved_audio_hash=plan.audio_fingerprint)
    assert approved.audio_fingerprint == plan.audio_fingerprint
    assert [(t.start, t.end) for t in state.timeline] == [(0, .5), (.5, 1)]


def test_unmatched_narration_blocks_precise_timeline():
    state,run=timeline_fixture();run.words[1]=WordAlignment(source_start=2,source_end=3,text='B')
    with pytest.raises(ValueError,match='unmatched'):build_timeline(state,run,'audio')
    assert not state.timeline


def assembly_fixture(tmp_path):
    import wave
    from prose_to_video.storage.project_files import import_audio
    from prose_to_video.pipeline.audio_transcription import source_file_hash
    session = session_with_text(tmp_path, 'A B')
    audio = tmp_path / 'voice.wav'
    with wave.open(str(audio), 'wb') as stream:
        stream.setnchannels(1); stream.setsampwidth(2); stream.setframerate(16000)
        stream.writeframes(b'\0\0' * 160000)
    session = import_audio(session, audio)
    state, alignment = timeline_fixture()
    state.prose_hash = alignment.prose_hash = source_hash('A B')
    alignment.audio_hash = source_file_hash(session.project.resolved_audio(session.root))
    (session.root / 'images/a.png').write_bytes(b'still')
    state.save(session.root)
    return session, state, alignment


@pytest.mark.parametrize('cached', [False, True, 'stale'])
def test_one_click_assembly_aligns_when_needed_and_persists(tmp_path, monkeypatch, cached):
    import prose_to_video.pipeline.audio_alignment as audio_alignment
    from prose_to_video.pipeline.stills_workflow import assemble_timeline
    session, state, alignment = assembly_fixture(tmp_path)
    if cached:
        saved = alignment.model_copy(update={'audio_hash': 'old'}) if cached == 'stale' else alignment
        session.database.save_alignment(saved)
    calls = []
    def align(*args, **kwargs):
        calls.append((args, kwargs))
        session.database.save_alignment(alignment)
        return alignment
    monkeypatch.setattr(audio_alignment, 'align_project', align)
    messages = []
    intervals = assemble_timeline(session, state, progress=messages.append)
    assert len(calls) == (0 if cached is True else 1)
    if calls:
        assert calls[0][0][4].allow_download
        assert calls[0][1]['cancel'] is not None
    assert [(t.scene_id, t.start, t.end) for t in intervals] == [('a', 0, 6), ('b', 6, 10)]
    restored = load_workflow(session)
    assert restored.timeline == intervals
    assert restored.timeline_audio_hash == alignment.audio_hash
    assert messages


def test_one_click_assembly_preserves_manual_edits(tmp_path, monkeypatch):
    from prose_to_video.pipeline.stills_workflow import assemble_timeline
    session, state, alignment = assembly_fixture(tmp_path)
    build_timeline(state, alignment, alignment.audio_hash)
    state.timeline[0].end = state.timeline[1].start = 5
    for interval in state.timeline: interval.manual = True
    session.database.save_alignment(alignment)
    assert [(t.start, t.end) for t in assemble_timeline(session, state)] == [(0, 5), (5, 10)]


@pytest.mark.parametrize('failure', ['missing_still', 'missing_audio', 'unmatched', 'cancel'])
def test_one_click_assembly_failure_keeps_saved_timeline(tmp_path, monkeypatch, failure):
    import prose_to_video.pipeline.audio_alignment as audio_alignment
    from prose_to_video.pipeline.audio_transcription import AlignmentCanceled
    from prose_to_video.pipeline.stills_workflow import assemble_timeline
    session, state, alignment = assembly_fixture(tmp_path)
    build_timeline(state, alignment, alignment.audio_hash)
    state.save(session.root)
    before = (session.root / 'stills-workflow.json').read_bytes()
    original = state.timeline.copy()
    cancel = Event()
    if failure == 'missing_still': (session.root / 'images/a.png').unlink()
    if failure == 'missing_audio': session.project.resolved_audio(session.root).unlink()
    if failure == 'unmatched': alignment.words[1].audio_start = None
    def align(*args, **kwargs):
        if failure == 'cancel': cancel.set()
        return alignment
    monkeypatch.setattr(audio_alignment, 'align_project', align)
    with pytest.raises((ValueError, AlignmentCanceled)):
        assemble_timeline(session, state, cancel)
    assert state.timeline == original
    assert (session.root / 'stills-workflow.json').read_bytes() == before


def test_catalog_change_marks_prompt_stale():
    state=Workflow(catalogs=[Identity(identifier='person',kind='Character',name='Mira',description='green coat')])
    scene=Scene(sequence=1,start=0,end=1,text='A',identity_links=['person'])
    before=dependency(state,scene);state.catalogs[0].description='blue coat'
    assert dependency(state,scene)!=before


def test_no_approval_gate_and_failed_replacement_keeps_selection(tmp_path):
    session=session_with_text(tmp_path,'A');state=Workflow();scene=Scene(sequence=1,start=0,end=1,text='A',final_prompt='A green tree')
    state.selected[scene.identifier]='existing'
    class Provider:
        name='mock'
        def capabilities(self,model):return SimpleNamespace(references=False,max_references=0,negative_prompt=False)
        def validate(self,request):pass
        def submit(self,request):raise RuntimeError('provider failed')
    with pytest.raises(RuntimeError):generate_still(session,state,scene,Provider())
    assert state.selected[scene.identifier]=='existing'
    assert state.jobs[-1]['request']['prompt']=='A green tree'
    assert state.jobs[-1]['status']=='Failed or uncertain'


def test_empty_project_migration_is_idempotent(tmp_path):
    session=create_project(tmp_path,'legacy'); first=load_workflow(session)
    assert load_workflow(session)==first
    assert session.database.load_scenes()==(None,[])


def test_eight_tab_navigation(monkeypatch, tmp_path):
    # Qt multimedia owns process-global resources. Exercise an independent app
    # lifetime so historical widget tests cannot retain its player at teardown.
    import subprocess
    import sys
    monkeypatch.setenv('QT_QPA_PLATFORM', 'offscreen')
    monkeypatch.setenv('XDG_CONFIG_HOME', str(tmp_path / 'config'))
    result = subprocess.run([sys.executable, '-c', """
from PySide6.QtWidgets import QApplication
from PySide6.QtCore import QCoreApplication, QEvent
from prose_to_video.gui.main_window import MainWindow
app = QApplication([])
window = MainWindow()
assert [window.workspace_tabs.tabText(i) for i in range(8)] == ['Start','Podcast Splitter','Analyze','Place','Character','Scenes','Stills','Video Production']
from tempfile import TemporaryDirectory
from pathlib import Path
from PySide6.QtCore import QEventLoop, QTimer
from prose_to_video.storage.project_files import create_project
from prose_to_video.pipeline.stills_workflow import Interval
import prose_to_video.gui.stills_window as gui
calls = []
def assemble(session, state, cancel, progress, *, timing_mode):
    assert timing_mode == 'nearest'
    calls.append(session)
    progress('Building timeline')
    state.timeline = [Interval(scene_id='a', start=0, end=10, speech_start=1, speech_end=9)]
    return state.timeline
gui.assemble_timeline = assemble
with TemporaryDirectory() as folder:
    window.load(create_project(Path(folder), 'Click test'))
    button = window.video_actions[0]
    assert button.text() == 'Build Timeline from Stills + Audio'
    assert window.timeline_mode.currentData() == 'nearest'
    button.click()
    assert window.worker is not None and not button.isEnabled()
    assert window.video_actions[-1].isEnabled()
    loop = QEventLoop()
    window.worker.finished.connect(loop.quit)
    QTimer.singleShot(5000, loop.quit)
    loop.exec()
    assert window.worker is None and button.isEnabled()
    assert len(calls) == 1 and window.timeline.rowCount() == 1
    assert 'Timeline ready: 1 stills' in window.video_message.text()
window.close()
window.deleteLater()
QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
app.processEvents()
"""], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr


def test_successful_generation_snapshots_references_and_selects_immediately(tmp_path):
    import os
    os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
    from PySide6.QtGui import QImage
    from PySide6.QtCore import QByteArray, QBuffer, QIODevice
    from prose_to_video.models.image_job import ImageCapabilities, ProviderResult, ProviderImage
    session=session_with_text(tmp_path,'Mira turns.')
    state=Workflow(prose_hash=source_hash('Mira turns.'))
    scene=Scene(sequence=1,start=0,end=11,text='Mira turns.',source_version=state.prose_hash,
        final_prompt='Mira with short black hair turns in a green coat',negative_prompt='watermark',identity_links=['person'])
    state.scenes=[scene]
    image=QImage(64,64,QImage.Format_RGB32); image.fill(0xff123456)
    ref=session.root/'references'/'mira.png'; assert image.save(str(ref))
    state.catalogs=[Identity(identifier='person',kind='Character',name='Mira',description='green coat',references=['references/mira.png'])]
    data=QByteArray(); buffer=QBuffer(data);buffer.open(QIODevice.WriteOnly);image.save(buffer,'PNG')
    class Provider:
        name='mock'
        def capabilities(self,model):return ImageCapabilities(references=True,max_references=1)
        def validate(self,request):assert len(request.references)==1
        def submit(self,request):
            assert request.prompt.endswith('Avoid: watermark')
            return ProviderResult(status='Completed',provider_job_id='provider-42',images=[ProviderImage(data=bytes(data),model='mock')])
    asset=generate_still(session,state,scene,Provider())
    assert state.current_still(scene.identifier)==asset
    restored=load_workflow(session)
    assert restored.current_still(scene.identifier).excerpt=='Mira turns.'
    assert asset.job_id=='provider-42' and asset.identities==['person']
    assert asset.references==[str(ref)]
    assert (session.root/asset.path).is_file()
    assert state.jobs[-1]['status']=='Completed'


def test_legacy_multiple_shots_and_timing_survive(tmp_path):
    import wave
    from prose_to_video.storage.project_files import import_audio
    from prose_to_video.models.shot import Shot, Storyboard, StoryboardSettings
    from prose_to_video.pipeline.shot_planning import scene_definition_hash
    text='First. Second.'; session=session_with_text(tmp_path,text)
    audio=tmp_path/'narration.wav'
    with wave.open(str(audio),'wb') as stream:
        stream.setnchannels(1);stream.setsampwidth(2);stream.setframerate(16000);stream.writeframes(b'\0\0'*32000)
    session=import_audio(session,audio)
    scene=Scene(sequence=1,start=0,end=len(text),text=text)
    session.database.replace_scenes(text,[scene]); cut=text.index('Second')
    shots=[Shot(scene_id=scene.identifier,sequence=i+1,title=f'View {i}',source_start=a,source_end=b,
        original_prose=text[a:b],visual_description='A still',audio_start=start,audio_end=end,timing_status='fully timed')
        for i,(a,b,start,end) in enumerate([(0,cut,.1,1),(cut,len(text),1,1.9)])]
    settings=StoryboardSettings(minimum=.1)
    board=Storyboard(scene_id=scene.identifier,scene_start=0,scene_end=len(text),scene_hash=source_hash(text),
        prose_hash=source_hash(text),scene_definition_hash=scene_definition_hash(scene),model_name='mock',settings=settings,
        settings_fingerprint=settings.fingerprint(),coverage_start=.1,coverage_end=1.9,status='ready',shots=shots)
    session.database.save_storyboard(board,text)
    state=load_workflow(session)
    assert [s.identifier for s in state.scenes]==[s.identifier for s in shots]
    validate_partition(text,state.scenes)
    assert [(t.start,t.end,t.manual) for t in state.timeline]==[(0,1,True),(1,2,True)]
    assert session.database.load_storyboard(scene.identifier).shots==shots


def test_direct_workflow_exports_real_video(tmp_path):
    import shutil
    import wave
    if not shutil.which('ffmpeg'):pytest.skip('FFmpeg unavailable')
    from PySide6.QtGui import QImage
    from prose_to_video.storage.project_files import import_audio
    from prose_to_video.pipeline.stills_workflow import store_still, render_plan
    from prose_to_video.rendering.ffmpeg_renderer import FFmpegRenderer, probe
    session=session_with_text(tmp_path,'A B'); audio=tmp_path/'narration.wav'
    with wave.open(str(audio),'wb') as stream:
        stream.setnchannels(1);stream.setsampwidth(2);stream.setframerate(16000);stream.writeframes(b'\0\0'*16000)
    session=import_audio(session,audio)
    state,run=timeline_fixture();state.prose_hash=source_hash('A B');run.prose_hash=state.prose_hash
    run.audio_hash=__import__('prose_to_video.pipeline.audio_transcription',fromlist=['source_file_hash']).source_file_hash(session.project.resolved_audio(session.root))
    run.audio_duration=1;run.words[0].audio_start=.1;run.words[0].audio_end=.3;run.words[1].audio_start=.6;run.words[1].audio_end=.9
    state.stills=[];state.selected={}
    image=QImage(64,64,QImage.Format_RGB32);image.fill(0xff123456);path=tmp_path/'still.png';image.save(str(path))
    for scene, movement in zip(state.scenes, ['zoom_in', 'pan_left']):
        scene.camera_movement = movement
        store_still(session,state,scene,path.read_bytes())
    build_timeline(state,run,run.audio_hash)
    state.render_settings=state.render_settings.model_copy(update={'width':64,'height':64,'frame_rate':10,'preset':'ultrafast'})
    plan=render_plan(session,state);out=session.root/'output'/'test.mp4'
    assert [s.camera.kind for s in plan.segments] == ['zoom_in', 'pan_left']
    assert all(s.production_mode == 'Camera Motion' and s.fit_policy == 'crop' for s in plan.segments)
    FFmpegRenderer(session.root).render(plan,out)
    info=probe(out)
    assert any(s['codec_type']=='audio' for s in info['streams'])
    assert any(s['codec_type']=='video' for s in info['streams'])
    assert abs(float(info['format']['duration'])-1)<.2


def test_chunked_analysis_reuses_canonical_identity(tmp_path):
    text=('Mira waits beside the stone gate. She turns toward the road. '*60)
    session=session_with_text(tmp_path,text);state=Workflow();provider=FakeAI()
    analyze(session,state,provider,AISettings(chunk_chars=1000,overlap_chars=100))
    catalogs=[content for schema,content in provider.calls if schema is CatalogResult]
    assert len(catalogs)>1
    assert all(c['known'][0]['identifier']=='person' for c in catalogs[1:])
    assert len(state.catalogs)==1
    assert all(s.identity_links==['person'] for s in state.scenes if s.production_included)
    validate_partition(text,state.scenes)


@pytest.mark.parametrize('newline', ['\n', '\r\n', '\r'])
def test_paragraph_scenes_preserve_exact_text_and_skip_ai_beats(tmp_path, newline):
    text = ('  ' + newline * 2 + 'Mira waits.' + newline + 'She turns.' + newline + '  '
            + newline + 'Mira waits again.' + newline * 3 + 'She leaves.' + newline * 2)
    session = session_with_text(tmp_path, text)
    state = Workflow(scene_detection='paragraph')
    provider = FakeAI()
    analyze(session, state, provider, AISettings(chunk_chars=200))
    assert len(state.scenes) == 3
    assert [s.text.strip() for s in state.scenes] == [
        'Mira waits.' + newline + 'She turns.', 'Mira waits again.', 'She leaves.']
    validate_partition(text, state.scenes)
    assert ''.join(s.text for s in state.scenes) == text
    assert not any(schema is Beats for schema, _ in provider.calls)
    assert len([schema for schema, _ in provider.calls if schema is Staging]) == 3
    restored = load_workflow(session)
    assert restored.scene_detection == 'paragraph'
    ids = [s.identifier for s in restored.scenes]
    analyze(session, restored, provider, AISettings())
    assert [s.identifier for s in restored.scenes] == ids


def test_scene_mode_change_rebuilds_existing_partition(tmp_path):
    session = session_with_text(tmp_path, 'Mira waits.\n\nShe turns.')
    state = Workflow()
    analyze(session, state, FakeAI(), AISettings())
    assert len(state.scenes) == 1
    old_scene = state.scenes[0]
    still = Still(scene_id=old_scene.identifier, source_hash=state.prose_hash,
                  start=old_scene.start, end=old_scene.end, excerpt=old_scene.text,
                  path='images/keep.png', prompt='Keep this image')
    state.stills.append(still)
    state.selected[old_scene.identifier] = still.identifier
    state.timeline = [Interval(scene_id=old_scene.identifier, start=0, end=10, speech_start=0, speech_end=10)]
    state.scene_detection = 'paragraph'
    analyze(session, state, FakeAI(), AISettings())
    assert len(state.scenes) == 2
    assert state.stills == [still]
    assert not state.selected
    assert not state.timeline
    state.scene_detection = 'ai'
    analyze(session, state, FakeAI(), AISettings())
    assert len(state.scenes) == 1
    assert state.analysis_scene_detection == 'ai'


def test_paragraph_analysis_resumes_frozen_plan(tmp_path):
    session = session_with_text(tmp_path, 'Mira waits.\n\nShe turns.')
    state = Workflow(scene_detection='paragraph')
    cancel = Event()
    def progress(message):
        if message.startswith('Writing scene 2/2'):
            cancel.set()
    with pytest.raises(ValueError, match='Canceled'):
        analyze(session, state, FakeAI(), AISettings(), cancel, progress)
    restored = load_workflow(session)
    first_id = restored.scenes[0].identifier
    analyze(session, restored, FakeAI(), AISettings())
    assert len(restored.scenes) == 2
    assert restored.scenes[0].identifier == first_id
    assert not any(not scene.text.strip() for scene in restored.scenes)


def test_character_listing_counts_distinct_included_scenes():
    from prose_to_video.pipeline.stills_workflow import listed_identities
    mira = Identity(identifier='mira', kind='Character', name='Mira',
                    aliases=['The traveler', 'she'], description='')
    courier = Identity(identifier='courier', kind='Character', name='Courier', description='')
    place = Identity(identifier='gate', kind='Place', name='Gate', description='')
    state = Workflow(catalogs=[courier, mira, place], scenes=[
        Scene(sequence=1, start=0, end=10, text='Mira waits',
              identity_links=['mira', 'courier', 'courier'], characters=['Mira', 'Courier']),
        Scene(sequence=2, start=10, end=20, text='She turns.', characters=['The traveler']),
        Scene(sequence=3, start=20, end=30, text='Appendix. ',
              production_included=False, identity_links=['courier']),
    ])
    assert listed_identities(state, 'Character') == [mira]
    assert listed_identities(state) == [mira, place]
    assert state.catalogs[0] is courier  # Keep one-offs for staging and references.
    state.scenes[1].characters = ['she']  # Pronouns must be resolved by context, never aliases.
    assert listed_identities(state, 'Character') == []


def test_analyze_lists_recurring_character_from_pronoun_scene(tmp_path):
    from prose_to_video.pipeline.stills_workflow import listed_identities

    class PronounAI(FakeAI):
        def _request(self, instructions, content, schema):
            result = super()._request(instructions, content, schema)
            if schema is CatalogResult:
                result.identities[0].aliases = ['she', 'The traveler']
                result.identities.extend([
                    Identity(identifier='courier', kind='Character', name='Courier', description='red hat'),
                    Identity(identifier='pronoun', kind='Character', name='She', description=''),
                ])
            if schema is Staging:
                assert 'unambiguous pronouns or aliases' in instructions
                assert all('she' not in c['aliases'] for c in self.calls[-1][1]['catalog'])
                if 'courier' in self.calls[-1][1]['prose']:
                    result.identity_links = ['person', 'courier']
                    result.characters = ['Mira', 'Courier']
                else:
                    assert 'Mira' in self.calls[-1][1]['earlier_context']
                    result.characters = ['Mira']  # AI resolves "She" from the preceding scene.
            return result

    session = session_with_text(tmp_path, 'Mira greets a courier.\n\nShe walks home.')
    state = Workflow(scene_detection='paragraph')
    provider = PronounAI()
    analyze(session, state, provider, AISettings())
    assert [c.name for c in listed_identities(state, 'Character')] == ['Mira']
    assert [c.name for c in state.catalogs] == ['Mira', 'Courier']
    restored = load_workflow(session)
    analyze(session, restored, provider, AISettings())
    assert [c.name for c in listed_identities(restored, 'Character')] == ['Mira']
    # A merge leaves one scene; repeated references within it are not recurrence.
    restored.scenes = [restored.scenes[0]]
    assert listed_identities(restored, 'Character') == []


@pytest.mark.parametrize('mode', ['ai', 'paragraph'])
@pytest.mark.parametrize('newline', ['\n', '\r\n', '\r'])
@pytest.mark.parametrize('blank_line', [False, True])
def test_headings_never_become_scenes_or_still_entries(tmp_path, mode, newline, blank_line):
    separator = newline * (2 if blank_line else 1)
    text = separator.join(['# Part One', 'Mira waits.', '# Part Two',
                           'She turns.', '# End']) + newline
    session = session_with_text(tmp_path, text)
    state = Workflow(scene_detection=mode)
    provider = FakeAI()
    analyze(session, state, provider, AISettings())
    validate_partition(text, state.scenes)
    assert len(state.scenes) == 2
    assert all(s.production_included and s.final_prompt for s in state.scenes)
    staging = [content for schema, content in provider.calls if schema is Staging]
    assert [c['prose'] for c in staging] == ['Mira waits.', 'She turns.']
    assert all('#' not in c['earlier_context'] for c in staging)
    ids = [s.identifier for s in state.scenes]
    restored = load_workflow(session)
    analyze(session, restored, provider, AISettings())
    assert [s.identifier for s in restored.scenes] == ids


def test_saved_heading_rows_are_removed_without_replacing_scene_edits(tmp_path):
    text = '# Part One\n\nMira waits.\n\n# End\n'
    session = session_with_text(tmp_path, text)
    start = text.index('Mira')
    end = text.index('# End')
    state = Workflow(prose_hash=source_hash(text), scenes=[
        Scene(sequence=1, start=0, end=start, text=text[:start], production_included=False),
        Scene(sequence=2, start=start, end=end, text=text[start:end], final_prompt='My edited prompt'),
        Scene(sequence=3, start=end, end=len(text), text=text[end:], production_included=False),
    ])
    identifier = state.scenes[1].identifier
    analyze(session, state, FakeAI(), AISettings())
    validate_partition(text, state.scenes)
    assert len(state.scenes) == 1
    assert state.scenes[0].identifier == identifier
    assert state.scenes[0].final_prompt == 'My edited prompt'


@pytest.mark.parametrize('mode', ['ai', 'paragraph'])
def test_heading_only_manuscript_has_no_scenes(tmp_path, mode):
    session = session_with_text(tmp_path, '# Part One\n\n# Part Two\n')
    state = Workflow(scene_detection=mode)
    with pytest.raises(ValueError, match='no prose paragraphs'):
        analyze(session, state, FakeAI(), AISettings())
    assert not state.scenes


def test_prompt_regeneration_omits_attached_headings(tmp_path):
    from prose_to_video.pipeline.stills_workflow import regenerate_prompt
    text = '# Part One\n\nMira waits.\n\n# End\n'
    session = session_with_text(tmp_path, text)
    state = Workflow()
    provider = FakeAI()
    analyze(session, state, provider, AISettings())
    scene = state.scenes[0]
    regenerate_prompt(session, state, scene, provider)
    schema, content = provider.calls[-1]
    assert schema is Staging
    assert content['prose'] == 'Mira waits.'
    assert content['scene']['text'] == 'Mira waits.'
    assert scene.text == text


@pytest.mark.parametrize('treatment', [
    'concept_map', 'blueprint', 'cad_visualization', 'product_showcase',
])
def test_technical_scene_focus_survives_analysis_and_prompt_regeneration(tmp_path, treatment):
    from prose_to_video.models.visual_treatment import TECHNICAL_TREATMENTS
    from prose_to_video.pipeline.stills_workflow import regenerate_prompt
    focus = 'The needle relays its signal through nearby devices.'

    class TechnicalAI(FakeAI):
        def _request(self, instructions, content, schema):
            result = super()._request(instructions, content, schema)
            if schema is Staging:
                assert 'Technology vocabulary alone never requires' in instructions
                assert 'render_type and explanatory_focus are binding' in instructions
                assert TECHNICAL_TREATMENTS[treatment] in instructions
                if 'scene' not in self.calls[-1][1]:
                    result.render_type = treatment
                    result.explanatory_focus = focus
                    result.final_prompt = 'An illustrated device explanation.'
                else:
                    # Provider suggestions must not replace the saved user selection.
                    result.render_type = 'cinematic'
                    result.explanatory_focus = 'A different idea'
                    result.final_prompt = 'A revised illustrated device explanation.'
            return result

    session = session_with_text(tmp_path, 'The needle relays its signal through nearby devices.')
    state = Workflow()
    provider = TechnicalAI()
    analyze(session, state, provider, AISettings())
    scene = load_workflow(session).scenes[0]
    assert scene.render_type == treatment
    assert scene.explanatory_focus == focus
    assert TECHNICAL_TREATMENTS[treatment] in scene.final_prompt
    assert focus in scene.final_prompt
    # Regeneration must respect an edited explanatory purpose, too.
    scene.explanatory_focus = 'The source of the relayed signal remains concealed.'
    state.scenes = [scene]
    regenerate_prompt(session, state, scene, provider)
    assert provider.calls[-1][1]['scene']['explanatory_focus'] == scene.explanatory_focus
    restored = load_workflow(session).scenes[0]
    assert restored.render_type == treatment
    assert restored.explanatory_focus == scene.explanatory_focus
    assert TECHNICAL_TREATMENTS[treatment] in restored.final_prompt
    assert scene.explanatory_focus in restored.final_prompt
    assert 'A revised illustrated device explanation.' in restored.final_prompt
    assert restored.prompt_dependency == dependency(state, restored)
    scene.explanatory_focus = 'A changed focus'
    assert scene.prompt_dependency != dependency(state, scene)


def test_narrative_action_keeps_cinematic_prompt_and_legacy_scene_loads(tmp_path):
    session = session_with_text(tmp_path, 'Mira hides the wireless device inside her coat.')
    state = Workflow()
    analyze(session, state, FakeAI(), AISettings())
    scene = state.scenes[0]
    assert scene.render_type == 'cinematic'
    assert scene.explanatory_focus == ''
    assert scene.final_prompt.startswith('A woman with short black hair')
    legacy = scene.model_dump(exclude={'explanatory_focus'})
    assert Scene.model_validate(legacy).explanatory_focus == ''


def test_analysis_saves_camera_movement(tmp_path):
    class CameraAI(FakeAI):
        def _request(self, instructions, content, schema):
            result = super()._request(instructions, content, schema)
            if schema is Staging:
                assert 'Choose camera_movement' in instructions
                result.camera_movement = 'zoom_out_pan_right'
            return result
    session = session_with_text(tmp_path, 'Mira waits.')
    state = Workflow()
    analyze(session, state, CameraAI(), AISettings())
    assert state.scenes[0].camera_movement == 'zoom_out_pan_right'
    assert load_workflow(session).scenes[0].camera_movement == 'zoom_out_pan_right'
