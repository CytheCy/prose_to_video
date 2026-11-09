"""Approximate display cuts never overwrite transcript diagnostics or word timing."""
import pytest
from prose_to_video.models.alignment import AlignmentIssue, WordAlignment
from prose_to_video.models.scene import Scene
from prose_to_video.pipeline.stills_workflow import Workflow, Still, build_timeline, assemble_timeline, load_workflow
from test_stills_workflow import timeline_fixture, assembly_fixture


def test_nearest_ignores_flags_without_clearing_or_retiming_them():
    state,run=timeline_fixture()
    run.issues=[AlignmentIssue(kind='spoken_insertion',message='Repeated speech',audio_start=4,audio_end=5)]
    before=run.model_dump()
    result=build_timeline(state,run,'audio',timing_mode='nearest')
    assert [(t.start,t.end) for t in result]==[(0,6),(6,10)]
    assert all(t.estimated for t in result)
    assert run.model_dump()==before
    with pytest.raises(ValueError,match='review'): build_timeline(state,run,'audio')


def test_missing_boundary_uses_neighboring_source_audio_anchors():
    state,run=timeline_fixture()
    run.words[1]=WordAlignment(source_start=2,source_end=3,text='B')
    result=build_timeline(state,run,'audio',timing_mode='nearest')
    # Source offset 2 lies halfway between verified word-end (1,3s) and end (3,10s).
    assert result[0].end==pytest.approx(6.5)
    assert result[1].start==pytest.approx(6.5) and result[1].estimated


def test_no_matches_uses_prose_proportion_and_covers_entire_audio():
    state,run=timeline_fixture();run.words=[]
    result=build_timeline(state,run,'audio',timing_mode='nearest')
    assert [(t.start,t.end) for t in result]==[(0,pytest.approx(20/3)),(pytest.approx(20/3),10)]
    assert all(t.estimated for t in result)


def test_nearest_keeps_manual_intervals_and_validates_sources():
    state,run=timeline_fixture()
    first=build_timeline(state,run,'audio',timing_mode='nearest')
    first[0].end=5;first[1].start=5
    for t in first:t.manual=True
    assert [(t.start,t.end) for t in build_timeline(state,run,'audio',timing_mode='nearest')]==[(0,5),(5,10)]
    with pytest.raises(ValueError,match='changed'):build_timeline(state,run,'other',timing_mode='nearest')


def test_nearest_handles_collapsed_matches_without_zero_length_stills():
    state,run=timeline_fixture()
    run.words[1].audio_start=0;run.words[1].audio_end=.001
    result=build_timeline(state,run,'audio',timing_mode='nearest')
    assert all(t.end>t.start for t in result)
    assert result[0].start==0 and result[-1].end==run.audio_duration


def test_assembly_persists_approximate_timeline_and_flags(tmp_path):
    session,state,run=assembly_fixture(tmp_path)
    run.words[1]=WordAlignment(source_start=2,source_end=3,text='B')
    run.issues=[AlignmentIssue(kind='spoken_insertion',message='ASR mismatch')]
    session.database.save_alignment(run)
    intervals=assemble_timeline(session,state,timing_mode='nearest')
    restored=load_workflow(session)
    assert restored.timeline==intervals and restored.timeline_mode=='nearest'
    assert all(t.estimated for t in intervals)
    assert session.database.load_alignment().issues==run.issues
