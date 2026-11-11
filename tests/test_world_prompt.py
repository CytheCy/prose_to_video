"""World building references produce one reusable setting prompt for every scene."""
import json
import os
from zipfile import ZipFile
import pytest

from prose_to_video.pipeline.providers import AISettings
from prose_to_video.models.scene import Scene
from prose_to_video.pipeline.stills_workflow import (
    Workflow, WorldPromptResult, analyze, load_workflow, world_building_content,
    set_world_prompt, WORLD_PROMPT_LIMIT)
from prose_to_video.storage.project_files import create_project, import_prose, import_world_building_files
from test_stills_workflow import FakeAI


class WorldAI(FakeAI):
    def _request(self, instructions, content, schema):
        if schema is WorldPromptResult:
            files = json.loads(content)['files']
            self.calls.append((schema, files))
            assert files[0]['name'] == 'world.md'
            return schema(prompt='Rain washed stone city, amber windows and muted blue dusk.')
        return super()._request(instructions, content, schema)


def test_world_prompt_saved_shown_in_locations_and_updated_with_files(tmp_path):
    session = create_project(tmp_path, 'story')
    prose = tmp_path / 'story.txt'; prose.write_text('Mira enters the station.')
    session = import_prose(session, prose)
    world = tmp_path / 'world.md'; world.write_text('A rain washed stone city at dusk.')
    import_world_building_files(session, [world])
    state = Workflow(); provider = WorldAI()
    analyze(session, state, provider, AISettings())
    assert state.world_prompt == 'Rain washed stone city, amber windows and muted blue dusk.'
    assert state.scenes[0].location.startswith(state.world_prompt)
    staging_payload = next(payload for schema, payload in provider.calls if schema.__name__ == 'Staging')
    assert staging_payload['world_prompt'] == state.world_prompt
    assert load_workflow(session).world_prompt == state.world_prompt

    # Reopening analysis keeps the generated scene and does not repeat the request.
    restored = load_workflow(session); again = WorldAI()
    analyze(session, restored, again, AISettings())
    assert not any(schema is WorldPromptResult for schema, _ in again.calls)
    assert restored.scenes[0].location.count(restored.world_prompt) == 1
    (session.root / 'world_building' / 'world.md').write_text('An altered city.')
    changed = WorldAI()
    analyze(session, restored, changed, AISettings())
    assert len([1 for schema, _ in changed.calls if schema is WorldPromptResult]) == 1
    assert restored.scenes[0].location.count(restored.world_prompt) == 1


def test_world_prompt_is_first_place_row_and_editable(tmp_path):
    os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
    from PySide6.QtWidgets import QApplication
    from prose_to_video.gui.stills_window import MainWindow
    session = create_project(tmp_path, 'story')
    state = Workflow(world_prompt='Old stone city')
    state.scenes = [Scene(sequence=1, start=0, end=1, text='A',
                          location='Old stone city\nHarbor gate')]
    state.save(session.root)
    app = QApplication.instance() or QApplication([])
    window = MainWindow(); window.load(session)
    listing = window.catalog_lists['Place']
    assert listing.item(0).text() == 'World Building Prompt'
    assert window.catalog_edits['Place'].toPlainText() == 'Old stone city'
    window.catalog_edits['Place'].setPlainText('Blue harbor at dusk')
    saved = load_workflow(session)
    assert saved.world_prompt == 'Blue harbor at dusk'
    assert saved.scenes[0].location == 'Blue harbor at dusk\nHarbor gate'
    window.catalog_edits['Place'].setPlainText('x' * (WORLD_PROMPT_LIMIT + 50))
    assert len(window.catalog_edits['Place'].toPlainText()) == WORLD_PROMPT_LIMIT
    assert len(load_workflow(session).world_prompt) == WORLD_PROMPT_LIMIT
    assert window.world_prompt_count.text().endswith('750/750 characters')
    window.close()


def test_docx_world_building_text_is_sent_as_content(tmp_path):
    session = create_project(tmp_path, 'story')
    document = tmp_path / 'notes.docx'
    with ZipFile(document, 'w') as archive:
        archive.writestr('word/document.xml',
            '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
            '<w:body><w:p><w:r><w:t>Rust red towers</w:t></w:r></w:p></w:body></w:document>')
    import_world_building_files(session, [document])
    assert world_building_content(session) == [{'name': 'notes.docx', 'text': 'Rust red towers'}]


def test_world_prompt_limit_rejects_direct_updates_and_retries_ai(tmp_path):
    with pytest.raises(ValueError, match='750 characters'):
        set_world_prompt(Workflow(), 'x' * 751)

    class VerboseAI(WorldAI):
        def _request(self, instructions, content, schema):
            if schema is WorldPromptResult and not any(call[0] is WorldPromptResult for call in self.calls):
                self.calls.append((schema, json.loads(content)['files']))
                return schema(prompt='x' * 751)
            return super()._request(instructions, content, schema)

    session = create_project(tmp_path, 'story')
    prose = tmp_path / 'story.txt'; prose.write_text('Mira enters the station.')
    session = import_prose(session, prose)
    world = tmp_path / 'world.md'; world.write_text('A rain washed stone city at dusk.')
    import_world_building_files(session, [world])
    state = Workflow(); provider = VerboseAI()
    analyze(session, state, provider, AISettings())
    assert len([call for call in provider.calls if call[0] is WorldPromptResult]) == 2
    assert len(state.world_prompt) <= WORLD_PROMPT_LIMIT
