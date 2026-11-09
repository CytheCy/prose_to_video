"""Narration reconciliation keeps source offsets, original audio, and real discrepancies."""
import os
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
from threading import Event
import wave

import pytest
from PySide6.QtWidgets import QApplication, QDialog

from prose_to_video.models.alignment import TranscriptWord, TranscriptSegment, AlignmentRun
from prose_to_video.models.scene import Scene, source_hash
from prose_to_video.pipeline.audio_alignment import align_project
from prose_to_video.pipeline.audio_transcription import AlignmentCanceled, TranscriptionSettings
from prose_to_video.pipeline.text_matching import match_words, MATCHING_VERSION
from prose_to_video.pipeline.transcript_review import ReviewDraft, prepare_review, realign_review, segment_review
from prose_to_video.storage.project_files import create_project, import_audio, import_prose


@pytest.mark.parametrize('prose,speech', [
    ('semi-truck', 'semi-truck'), ('Drug-emaciated', 'Drug-emaciated'),
    ('K-Mart', 'Kmart'), ('thirty-three', '33'), ('fifty', '50'),
    ('seventy-five-year-old', '75-year-old'), ('twenty-twenty-three', '2023'),
    ('one hundred and five', '105'), ('fifty-two-thousand', '52000'),
    ('art déco', 'art Deco'), ('de\u0301co', 'deco'), ('3.50', '3.5'), ('1,000', '1000'), ('ear plugs', 'earplugs'), ('Wallstreet', 'Wall Street'),
    ('court yard', 'courtyard'), ('A.I.', 'AI'), ('U.S.', 'U. S.'),
    ('Okay', 'OK'), ('Mr. Midnight', 'Mister Midnight'), ('call-in', 'call-in'),
    ('uni-brow round-faced', 'unibrow round-faced'), ('six story', 'six-story'),
])
def test_equivalent_speech_keeps_exact_manuscript_ranges(prose,speech):
    text='😀 '+prose+' finishes.'
    words=[TranscriptWord(text=w,start=i,end=i+.4) for i,w in enumerate((speech+' finishes.').split())]
    matched,_,issues=match_words(text,words)
    assert not issues
    assert all(w.status=='matched' for w in matched)
    assert all(text[w.source_start:w.source_end]==w.text for w in matched)
    assert matched[0].source_start==2
    # Grouped phrases use the complete returned timing; no fabricated sub-word times.
    if len(prose.split())>len(speech.split()) or ('-' in prose and '-' in speech):
        assert matched[0].audio_start==0
    AlignmentRun(prose_hash='p',audio_hash='a',audio_path='voice.wav',audio_duration=20,
        transcription_model='test',device='cpu',precision='int8',config_fingerprint='x',words=matched)


def test_one_to_many_uses_verified_phrase_interval_and_honors_scene_boundaries():
    spoken=[TranscriptWord(text='earplugs',start=1,end=1.7)]
    words,_,issues=match_words('ear plugs',spoken)
    assert not issues
    assert [(w.text,w.audio_start,w.audio_end) for w in words]==[('ear plugs',1,1.7)]
    words,_,issues=match_words('ear plugs',spoken,boundaries=[4])
    assert all(w.audio_start is None for w in words)
    assert issues
    words,_,issues=match_words('ear plugs',spoken,included=[(0,3)])
    assert words[1].status=='excluded' and issues


def test_omissions_repetitions_homophones_and_distinct_numbers_still_need_review():
    for prose,speech in [('Read this once','Read this this once'),('Read this once','Read once'),
                         ('hoarse','horse'),('one two','12'),('Roni','Ronnie'),('3.14','314')]:
        spoken=[TranscriptWord(text=w,start=i,end=i+.4) for i,w in enumerate(speech.split())]
        words,_,issues=match_words(prose,spoken)
        assert issues or any(w.status=='unmatched' for w in words)


def test_missing_and_backward_phrase_times_are_never_invented():
    for speech in [[TranscriptWord(text='ear'),TranscriptWord(text='plugs',start=1,end=2)],
                   [TranscriptWord(text='ear',start=1,end=2),TranscriptWord(text='plugs',start=.5,end=1)]]:
        words,_,_=match_words('earplugs',speech)
        assert words[0].status=='unmatched' and words[0].audio_start is None


class FakeAligner:
    alignment_model='test-forced-aligner'
    def __init__(self, fail=False): self.fail=fail; self.calls=[]
    def load_alignment(self,*args): pass
    def align_chunk(self,path,offset,raw):
        assert path.is_file()
        self.calls.append(raw[0].text)
        if self.fail: raise ValueError('aligner unavailable')
        segment=raw[0]
        return [segment.model_copy(update={'words':[
            TranscriptWord(text=w,start=segment.start+i*.3,end=segment.start+i*.3+.2)
            for i,w in enumerate(segment.text.split())]})]


def review_fixture(tmp_path):
    text='Roni waits. She leaves.'
    source=tmp_path/'story.txt'; source.write_text(text)
    audio=tmp_path/'voice.wav'
    with wave.open(str(audio),'wb') as stream:
        stream.setnchannels(1); stream.setsampwidth(2); stream.setframerate(16000); stream.writeframes(b'\0\0'*16000*4)
    session=import_audio(import_prose(create_project(tmp_path,'Story'),source),audio)
    scene=Scene(sequence=1,start=0,end=len(text),text=text)
    class ASR:
        alignment_model='asr-aligner'
        def load(self,*args): pass
        def process_chunk(self,*args):
            return [TranscriptSegment(start=.5,end=1.5,text='Ronnie waits.',language='en',words=[
                TranscriptWord(text='Ronnie',start=.5,end=.7),TranscriptWord(text='waits.',start=.9,end=1.1)]),
                TranscriptSegment(start=2,end=3,text='She leaves.',language='en',words=[
                TranscriptWord(text='She',start=2,end=2.2),TranscriptWord(text='leaves.',start=2.4,end=2.6)])]
    run=align_project(session.project.resolved_prose(session.root),session.project.resolved_audio(session.root),
        [scene],session.database,TranscriptionSettings(device='cpu'),provider=ASR())
    draft=ReviewDraft.load(session.root,run)
    draft.texts[run.transcript[0].identifier]='Roni waits.'
    draft.listened=[run.transcript[0].identifier]
    return session,[scene],run,draft,text


def test_corrected_text_is_realigned_and_original_prose_is_untouched(tmp_path):
    session,scenes,run,draft,text=review_fixture(tmp_path)
    provider=FakeAligner()
    updated=realign_review(session,scenes,run,draft,provider=provider)
    assert provider.calls==['Roni waits.','She leaves.']
    assert updated.identifier!=run.identifier and updated.matching_version==MATCHING_VERSION
    assert updated.words[0].text=='Roni' and updated.words[0].audio_start==.5
    assert not updated.issues and all(w.status=='matched' for w in updated.words)
    assert session.database.load_alignment().identifier==updated.identifier
    assert session.project.resolved_prose(session.root).read_text()==text
    assert updated.reviewed_segments==draft.listened
    assert ReviewDraft.load(session.root,run).texts==draft.texts
    assert not ReviewDraft.load(session.root,updated).texts


def test_realign_cancellation_failure_and_unchecked_edits_preserve_alignment(tmp_path):
    session,scenes,run,draft,_=review_fixture(tmp_path)
    draft.listened=[]
    with pytest.raises(ValueError,match='Listen'):
        realign_review(session,scenes,run,draft,provider=FakeAligner())
    draft.listened=[run.transcript[0].identifier]
    with pytest.raises(ValueError,match='unavailable'):
        realign_review(session,scenes,run,draft,provider=FakeAligner(fail=True))
    cancel=Event()
    with pytest.raises(AlignmentCanceled):
        realign_review(session,scenes,run,draft,provider=FakeAligner(),cancel=cancel,
            progress=lambda _:cancel.set())
    assert session.database.load_alignment().identifier==run.identifier
    assert ReviewDraft.load(session.root,run).texts==draft.texts


def test_review_refreshes_legacy_matches_and_flags_real_mismatches(tmp_path):
    session,scenes,run,draft,text=review_fixture(tmp_path)
    run.matching_version=1
    session.database.save_alignment(run)
    refreshed,_=prepare_review(session,scenes)
    assert refreshed.matching_version==MATCHING_VERSION
    rows=segment_review(refreshed,text)
    assert 'Ronnie' in str(rows[0]['messages']) and 'Roni' in str(rows[0]['messages'])
    assert not rows[1]['messages']
    assert 'Roni waits' in rows[0]['context']


def test_actual_extra_speech_and_omissions_survive_review(tmp_path):
    session,scenes,run,draft,_=review_fixture(tmp_path)
    draft.texts[run.transcript[0].identifier]='Roni Roni'
    updated=realign_review(session,scenes,run,draft,provider=FakeAligner())
    assert any(i.kind=='spoken_insertion' for i in updated.issues)
    assert any(w.text=='waits' and w.status=='unmatched' for w in updated.words)


def test_stale_review_cannot_overwrite_new_alignment(tmp_path):
    session,scenes,run,draft,_=review_fixture(tmp_path)
    newer=run.model_copy(update={'identifier':'newer'})
    session.database.save_alignment(newer)
    with pytest.raises(ValueError,match='changed'):
        realign_review(session,scenes,run,draft,provider=FakeAligner())
    assert session.database.load_alignment().identifier=='newer'


def test_review_dialog_play_edit_save_and_reopen(tmp_path,monkeypatch):
    from prose_to_video.gui.transcript_review import TranscriptReviewDialog
    app=QApplication.instance() or QApplication([])
    session,scenes,run,draft,text=review_fixture(tmp_path)
    dialog=TranscriptReviewDialog(session,run,text)
    assert dialog.listing.count()==1
    assert dialog.transcript.toPlainText()=='Ronnie waits.'
    played=[]; dialog.audio.play_range=lambda a,b:played.append((a,b))
    dialog.play.click(); assert played==[(0,2)]
    dialog.transcript.setPlainText('Roni waits.')
    dialog.realign.click()
    assert dialog.result()!=QDialog.Accepted
    dialog.listened.setChecked(True); dialog.save.click()
    dialog.only_problems.setChecked(False)
    dialog.listing.setCurrentRow(1)
    dialog.reject()
    reopened=TranscriptReviewDialog(session,run,text)
    assert reopened.transcript.toPlainText()=='Roni waits.'
    assert reopened.listened.isChecked()
    reopened.realign.click(); assert reopened.result()==QDialog.Accepted
    reopened.close()


def test_reviewed_alignment_builds_timeline_with_existing_stills(tmp_path):
    from prose_to_video.pipeline.stills_workflow import Workflow, Still, assemble_timeline
    session,scenes,run,draft,text=review_fixture(tmp_path)
    state=Workflow(prose_hash=source_hash(text),scenes=scenes)
    still=Still(scene_id=scenes[0].identifier,source_hash=state.prose_hash,start=0,end=len(text),
        excerpt=text,path='images/existing.png',prompt='existing image')
    (session.root/still.path).write_bytes(b'preserved image')
    state.stills.append(still); state.selected[scenes[0].identifier]=still.identifier
    realign_review(session,scenes,run,draft,provider=FakeAligner())
    timeline=assemble_timeline(session,state)
    assert len(timeline)==1 and timeline[0].start==0 and timeline[0].end==4
    assert (session.root/still.path).read_bytes()==b'preserved image'
    assert session.project.resolved_prose(session.root).read_text()==text


def test_deleted_transcript_segment_stays_playable_for_later_review(tmp_path):
    session,scenes,run,draft,text=review_fixture(tmp_path)
    draft.texts[run.transcript[0].identifier]=''
    updated=realign_review(session,scenes,run,draft,provider=FakeAligner())
    assert len(updated.transcript)==2
    assert updated.transcript[0].text=='' and not updated.transcript[0].words
    assert updated.transcript[0].start==run.transcript[0].start
    current,_=prepare_review(session,scenes)
    assert segment_review(current,text)[0]['messages']


@pytest.mark.parametrize('cached',[True,False])
def test_offline_forced_alignment_checks_real_torchaudio_cache(tmp_path,monkeypatch,cached):
    import sys
    from types import SimpleNamespace
    from prose_to_video.pipeline.audio_transcription import WhisperXProvider
    folder=tmp_path/'checkpoints'; folder.mkdir()
    if cached: (folder/'weights.pth').write_bytes(b'cached')
    modules={
        'nltk':SimpleNamespace(data=SimpleNamespace(find=lambda _:True)),
        'whisperx.utils':SimpleNamespace(PUNKT_LANGUAGES={'en':'english'}),
        'whisperx.alignment':SimpleNamespace(DEFAULT_ALIGN_MODELS_TORCH={'en':'TEST'},DEFAULT_ALIGN_MODELS_HF={}),
        'torch':SimpleNamespace(hub=SimpleNamespace(get_dir=lambda:str(tmp_path))),
        'torchaudio':SimpleNamespace(pipelines=SimpleNamespace(TEST=SimpleNamespace(_path='weights.pth'))),
    }
    for name,module in modules.items(): monkeypatch.setitem(sys.modules,name,module)
    calls=[]
    def load(**kwargs): calls.append(kwargs); return object(),{}
    provider=WhisperXProvider(); provider.allow_download=False; provider.device='cpu'
    provider.align_models={}; provider.align_model_names={}
    provider.whisperx=SimpleNamespace(load_audio=lambda _:[],load_align_model=load,
        align=lambda *a,**k:{'segments':[{'start':0,'end':1,'text':'Read','words':[{'word':'Read','start':0,'end':1}]}]})
    raw=[TranscriptSegment(start=0,end=1,text='Read',language='en')]
    if cached:
        assert provider.align_chunk(tmp_path/'sample.wav',0,raw)[0].words[0].start==0
        assert calls[0]['model_cache_only'] is True
    else:
        with pytest.raises(ValueError,match='not cached'): provider.align_chunk(tmp_path/'sample.wav',0,raw)
        assert not calls
