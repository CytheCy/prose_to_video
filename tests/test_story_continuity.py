"""Story identities survive part changes; scene context belongs to each part."""
import json

from prose_to_video.pipeline.providers import AISettings
from prose_to_video.pipeline.stills_workflow import (
    CatalogResult, Identity, Staging, Workflow, WorldPromptResult,
    analyze, listed_identities, load_workflow, regenerate_prompt, scene_world_prompt)
from prose_to_video.storage.manuscript import ManuscriptStore
from prose_to_video.storage.project_files import import_world_building_files, world_building_files
from test_stills_workflow import FakeAI, session_with_text


def select(session, text, indexes):
    store = ManuscriptStore(session.database)
    for index, section in enumerate(store.sections(text)):
        store.decide(text, section.identifier, section.classification, index in indexes, False)


class ContinuityAI(FakeAI):
    def _request(self, instructions, content, schema):
        payload = json.loads(content)
        if schema is WorldPromptResult:
            self.calls.append((schema, payload))
            return schema(prompt=payload['files'][0]['text'])
        result = super()._request(instructions, content, schema)
        if schema is CatalogResult:
            prose = ''.join(p['text'] for p in payload['passages'])
            result.identities = [Identity(identifier='person', kind='Character', name='Mira',
                description='black hair')] if 'Mira' in prose else []
            if 'Harbor' in prose:
                result.identities.append(Identity(identifier='harbor', kind='Place', name='Harbor',
                    description='stone piers'))
            if 'Tower' in prose:
                result.identities.append(Identity(identifier='tower', kind='Place', name='Tower',
                    description='red tower'))
        return result


def test_switch_parts_reuses_whole_story_catalog_and_references(tmp_path):
    text = '# First\nMira visits Harbor.\n# Second\nMira leaves Harbor.\n# Third\nTower stands empty.\n'
    session = session_with_text(tmp_path, text)
    select(session, text, {0})
    # Unselected exports are unnecessary for building the complete identity catalog.
    (session.root / 'output/chapters/Third.md').unlink()
    provider = ContinuityAI()
    state = Workflow()
    analyze(session, state, provider, AISettings())
    assert {c.name for c in listed_identities(state)} == {'Mira', 'Harbor'}
    mira = next(c for c in state.catalogs if c.name == 'Mira')
    mira.references = ['references/mira.png']
    mira.description = 'User approved design'
    mira.manual = True
    state.save(session.root)
    catalog_calls = sum(schema is CatalogResult for schema, _ in provider.calls)
    first = next(s for s in state.scenes if s.production_included)
    first.final_prompt = 'User approved scene'
    state.save(session.root)
    select(session, text, {1})
    state = load_workflow(session)
    analyze(session, state, provider, AISettings())
    assert sum(schema is CatalogResult for schema, _ in provider.calls) == catalog_calls
    shared = next(c for c in state.catalogs if c.identifier == mira.identifier)
    assert shared.references == ['references/mira.png']
    assert shared.description == 'User approved design'
    assert {c.name for c in listed_identities(state)} == {'Mira', 'Harbor'}
    assert [s.text.strip() for s in state.scenes if s.production_included] == ['Mira leaves Harbor.']
    select(session, text, {0})
    analyze(session, state, provider, AISettings())
    assert next(s for s in state.scenes if s.identifier == first.identifier).final_prompt == 'User approved scene'


def test_world_files_and_prompts_stay_with_their_parts(tmp_path):
    text = '# First\nMira waits.\n# Second\nMira leaves.\n'
    session = session_with_text(tmp_path, text)
    select(session, text, {0})
    world = tmp_path / 'city.md'; world.write_text('Blue stone city')
    import_world_building_files(session, [world])
    state = Workflow(); provider = ContinuityAI()
    analyze(session, state, provider, AISettings())
    first = next(s for s in state.scenes if s.production_included)
    assert scene_world_prompt(state, first) == 'Blue stone city'
    select(session, text, {1})
    assert world_building_files(session) == []
    other = tmp_path / 'forest.md'; other.write_text('Golden forest')
    import_world_building_files(session, [other])
    analyze(session, state, provider, AISettings())
    second = next(s for s in state.scenes if s.production_included)
    assert scene_world_prompt(state, second) == 'Golden forest'
    assert scene_world_prompt(state, first) == 'Blue stone city'
    assert 'Golden forest' not in first.location
    select(session, text, {0, 1})
    analyze(session, state, provider, AISettings())
    for scene in (first, second):
        regenerate_prompt(session, state, scene, provider)
        assert provider.calls[-1][1]['world_prompt'] == scene_world_prompt(state, scene)
    assert {p.name for p in world_building_files(session)} == {'city.md', 'forest.md'}


def test_removing_shared_world_file_only_detaches_selected_parts(tmp_path):
    from prose_to_video.storage.project_files import remove_world_building_file
    text = '# First\nMira waits.\n# Second\nMira leaves.\n'
    session = session_with_text(tmp_path, text)
    world = tmp_path / 'shared.md'; world.write_text('Shared context')
    import_world_building_files(session, [world])
    select(session, text, {0})
    assert remove_world_building_file(session, world.name) == []
    assert (session.root / 'world_building' / world.name).exists()
    select(session, text, {1})
    assert [p.name for p in world_building_files(session)] == ['shared.md']
    remove_world_building_file(session, world.name)
    assert not (session.root / 'world_building' / world.name).exists()
    assert world.exists()
