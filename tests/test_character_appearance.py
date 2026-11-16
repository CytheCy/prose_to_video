"""Age splitting is conservative and every proposed appearance is grounded."""
from types import SimpleNamespace

from prose_to_video.pipeline.character_appearance import (
    CharacterAppearance, grounded_appearances, age_version_groups, appearance_reasons)
from prose_to_video.pipeline.stills_workflow import (
    Identity, Workflow, create_detected_age_versions, listed_identities)
from prose_to_video.models.scene import Scene


def age(part, stage, number=None, **kwargs):
    return CharacterAppearance(kind='age', part_id=part, stage=stage, age=number,
        quote=kwargs.pop('quote', f'Bob was {stage}.'), reason='A different life stage',
        description=f'Bob, {stage.replace("_", " ")}', **kwargs)


def person(observations):
    return Identity(identifier='bob', kind='Character', name='Bob', description='Combined description',
        part_ids=['p1', 'p2'], character_appearances=observations)


def test_large_age_changes_split_and_keep_same_person_scene_links(tmp_path):
    bob = person([age('p1', 'young_adult', 22), age('p2', 'older_adult', 61)])
    bob.references = ['references/bob.png']
    state = Workflow(catalog_hash='hash', selected_part_ids=['p1'], catalogs=[bob], scenes=[
        Scene(sequence=1, start=0, end=10, text='Bob waits.', characters=['Bob'], identity_links=['bob']),
        Scene(sequence=2, start=10, end=20, text='Bob waits.', characters=['Bob'], identity_links=['bob'])])
    sections = [SimpleNamespace(identifier='p1', body_start=0, end=10),
                SimpleNamespace(identifier='p2', body_start=10, end=20)]
    create_detected_age_versions(state, sections)
    assert [c.name for c in listed_identities(state, 'Character')] == ['Bob (age 22)', 'Bob (age 61)']
    young, old = state.catalogs
    assert young.character_group_id == old.character_group_id == 'bob'
    assert [c.part_ids for c in state.catalogs] == [['p1'], ['p2']]
    assert 'older adult' not in young.description
    assert 'young adult' not in old.description
    assert old.references == [] and young.references == bob.references
    assert state.scenes[1].identity_links == [old.identifier]
    assert state.scenes[1].characters == [old.name]
    before = state.model_dump()
    create_detected_age_versions(state, sections)
    assert state.model_dump() == before
    assert Workflow.model_validate_json(state.model_dump_json()).catalogs[1].character_appearances


def test_small_age_gaps_and_gradual_aging_do_not_split():
    assert not age_version_groups(person([age('p1', 'child', 17), age('p2', 'adult', 20)]))
    assert not age_version_groups(person([age('p1', 'adult', 30), age('p2', 'adult', 34)]))
    bob = person([age('p1', 'adult', 30), age('p2', 'adult', 40), age('p3', 'adult', 50)])
    bob.part_ids.append('p3')
    assert not age_version_groups(bob)


def test_clear_stages_without_numbers_but_not_adjacent_stages():
    assert len(age_version_groups(person([age('p1', 'child'), age('p2', 'adult')]))) == 2
    assert len(age_version_groups(person([age('p1', 'child', 8), age('p2', 'young_adult', 20)]))) == 2
    assert len(age_version_groups(person([age('p1', 'young_adult'), age('p2', 'older_adult')]))) == 2
    assert not age_version_groups(person([age('p1', 'adolescent'), age('p2', 'young_adult')]))
    assert not age_version_groups(person([age('p1', 'unknown'), age('p2', 'elderly')]))


def test_flashbacks_split_by_evidence_not_chapter_order_and_mixed_parts_need_review():
    bob = person([age('p1', 'elderly'), age('p2', 'child')])
    groups = age_version_groups(bob)
    assert groups[0][0] == {'p1'} and groups[1][0] == {'p2'}
    bob.character_appearances.append(age('p1', 'child'))
    assert not age_version_groups(bob)
    assert not age_version_groups(person([age('p1', 'child'), age('p3', 'elderly')]))


def test_quotes_must_come_from_current_passages_and_part_ids_are_not_trusted():
    valid = age('invented', 'young_adult', quote='Young Bob\nwaited.')
    invented = age('p1', 'elderly', quote='Bob was eighty.')
    result = grounded_appearances([valid, invented], [{'text': 'Young Bob waited. Then he left.'}], 'p2')
    assert len(result) == 1 and result[0].part_id == 'p2'


def test_visible_changes_are_reasons_never_automatic_versions():
    changes = [CharacterAppearance(kind='visible_change', part_id='p2', quote='His hair was short now.',
        reason='Hair cut from long to short'), CharacterAppearance(kind='visible_change', part_id='p2',
        quote='A new scar crossed his cheek.', reason='New facial scar')]
    bob = person(changes)
    assert not age_version_groups(bob)
    reasons = appearance_reasons(bob, {'p2': 'Part Two'})
    assert len(reasons) == 2 and 'Part Two' in reasons[0]
    assert 'His hair was short now.' in reasons[0]


def test_analysis_collects_grounded_evidence_and_uses_age_specific_scene_catalog(tmp_path):
    import json
    from test_stills_workflow import session_with_text, FakeAI
    from prose_to_video.pipeline.stills_workflow import analyze, CatalogResult, Staging
    from prose_to_video.pipeline.providers import AISettings

    class AppearanceAI(FakeAI):
        def _request(self, instructions, content, schema):
            data = json.loads(content)
            if schema is CatalogResult:
                assert 'flashbacks' in instructions and 'few years' in instructions
                young = 'Young Bob' in str(data['passages'])
                quote = 'Young Bob waited.' if young else 'Bob, much older now, waited.'
                return CatalogResult(identities=[person([
                    age('untrusted', 'young_adult' if young else 'older_adult', quote=quote),
                    age('untrusted', 'elderly', quote='Invented evidence.')])])
            if schema is Staging:
                catalog = data['catalog']
                assert len(catalog) == 1
                result = super()._request(instructions, content, schema)
                result.identity_links = [catalog[0]['identifier']]
                result.characters = [catalog[0]['name']]
                return result
            return super()._request(instructions, content, schema)

    session = session_with_text(tmp_path, '# Part 1\n\nYoung Bob waited.\n\n# Part 2\n\nBob, much older now, waited.')
    state = Workflow()
    analyze(session, state, AppearanceAI(), AISettings())
    assert [c.name for c in state.catalogs] == ['Bob (young adult)', 'Bob (older adult)']
    assert all(len(c.character_appearances) == 2 for c in state.catalogs)
    assert {i for scene in state.scenes for i in scene.identity_links} == {c.identifier for c in state.catalogs}
    restored = Workflow.model_validate_json((session.root / 'stills-workflow.json').read_text())
    assert restored.catalogs == state.catalogs


def test_character_selection_displays_source_backed_version_reasons(tmp_path, monkeypatch):
    from PySide6.QtCore import QSettings
    from PySide6.QtWidgets import QApplication
    from prose_to_video.gui import stills_window
    from test_stills_workflow import session_with_text
    from prose_to_video.pipeline.manuscript import parse_manuscript
    from prose_to_video.utils.prose import read_prose
    monkeypatch.setattr(stills_window, 'QSettings', lambda *args: QSettings(str(tmp_path / 'ui.ini'), QSettings.IniFormat))
    app = QApplication.instance() or QApplication([])
    window = stills_window.MainWindow()
    window.session = session_with_text(tmp_path, '# Part 1\n\nBob waited.\n\n# Part 2\n\nA new scar crossed his cheek.')
    ids = [p.identifier for p in parse_manuscript(read_prose(window.session.project.resolved_prose(window.session.root)))]
    bob = person([CharacterAppearance(kind='visible_change', quote='A new scar crossed his cheek.',
        reason='New facial scar', part_id='p2')])
    bob.part_ids = ids
    bob.character_appearances[0].part_id = ids[-1]
    window.state = Workflow(catalog_hash='hash', selected_part_ids=ids, catalogs=[bob])
    window.refresh()
    window.show_identity('Character')
    assert 'New facial scar' in window.character_version_reasons.text()
    assert 'A new scar crossed his cheek.' in window.character_version_reasons.text()
    window.catalog_lists['Character'].setCurrentRow(-1)
    assert window.character_version_reasons.text() == ''
    window.close()
