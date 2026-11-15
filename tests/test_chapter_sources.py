"""Scene analysis uses selected exports; catalogs use the whole source story."""
import json

import pytest

from prose_to_video.pipeline.stills_workflow import Workflow, analyze
from prose_to_video.pipeline.providers import AISettings
from prose_to_video.pipeline.scene_detection import detect_scenes
from prose_to_video.models.scene import validate_partition
from prose_to_video.storage.manuscript import ManuscriptStore
from test_stills_workflow import FakeAI, session_with_text


@pytest.mark.parametrize('newline', ['\n', '\r\n', '\r'])
@pytest.mark.parametrize('mode', ['ai', 'paragraph'])
def test_catalog_reads_whole_story_but_scenes_read_only_selected_exports(tmp_path, newline, mode):
    text = '# First\nMira waits.\n\n# Second\nSECRET unselected prose.\n'.replace('\n', newline)
    session = session_with_text(tmp_path, text)
    store = ManuscriptStore(session.database)
    second = store.sections(text)[1]
    store.decide(text, second.identifier, second.classification, False, False)
    (session.root / 'output/chapters/Second.md').write_bytes(b'\xff unreadable and unselected')
    (session.root / 'output/chapters/Unrelated.md').write_bytes(b'\xff unrelated')
    state = Workflow(scene_detection=mode)
    provider = FakeAI()
    analyze(session, state, provider, AISettings())
    catalogs = json.dumps([content for schema, content in provider.calls if schema.__name__ == 'CatalogResult'])
    assert 'SECRET' in catalogs
    payloads = json.dumps([content for schema, content in provider.calls if schema.__name__ != 'CatalogResult'])
    assert 'SECRET' not in payloads
    assert '# First' not in payloads and '# Second' not in payloads
    assert 'Mira waits.' in payloads
    assert all('#' not in scene.text for scene in state.scenes)
    validate_partition(text, state.scenes)


def test_missing_or_changed_selected_export_never_falls_back(tmp_path):
    session = session_with_text(tmp_path, '# First\nMira waits.\n')
    path = session.root / 'output/chapters/First.md'
    path.unlink()
    provider = FakeAI()
    with pytest.raises(ValueError, match='Podcast Splitter'):
        analyze(session, Workflow(), provider, AISettings())
    assert not provider.calls
    path.write_text('Different story.')
    with pytest.raises(ValueError, match='Podcast Splitter'):
        analyze(session, Workflow(), provider, AISettings())
    assert not provider.calls


@pytest.mark.parametrize('heading', ['#No space', '####### Deep', '    # Indented', '#'])
def test_all_hash_lines_are_structural(heading):
    text = heading + '\nMira waits.\n# End\n'
    from test_scenes import MockProvider
    scenes = detect_scenes(text, MockProvider(), AISettings())
    assert [s.text for s in scenes] == ['Mira waits.\n']
    assert all(s.production_included for s in scenes)
    validate_partition(text, scenes)


def test_gaps_may_contain_headings_but_never_lost_prose():
    from prose_to_video.models.scene import Scene
    text = '# First\nStory.\nLost prose.\n'
    start = text.index('Story.')
    scene = Scene(sequence=1, start=start, end=start + 7, text=text[start:start+7])
    with pytest.raises(ValueError, match='complete manuscript'):
        validate_partition(text, [scene])


def test_heading_labels_do_not_supply_narrative_facts():
    from prose_to_video.pipeline.manuscript import parse_manuscript, analysis_report
    text = '# Radio plays at Mystery Station 2040\nA person waits.\n# Mystery Station 2040\nSomeone leaves.\n'
    sections = parse_manuscript(text)
    assert all(s.viewpoint == 'direct' for s in sections)
    report = analysis_report(text, sections)
    assert not report['recurring_locations']
    assert all(event['year'] is None for event in report['chronology'])


def test_legacy_scene_crossing_parts_is_rebuilt_without_headings(tmp_path):
    from prose_to_video.models.scene import Scene, source_hash
    text = '# First\nMira waits.\n# Second\nMira leaves.\n'
    session = session_with_text(tmp_path, text)
    state = Workflow(prose_hash=source_hash(text), scenes=[
        Scene(sequence=1, start=0, end=len(text), text=text)])
    analyze(session, state, FakeAI(), AISettings())
    validate_partition(text, state.scenes)
    assert len(state.scenes) == 2
    assert all('#' not in s.text for s in state.scenes)
