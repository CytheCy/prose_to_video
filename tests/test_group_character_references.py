"""Group character references preserve subject counts in provider prompts."""
import pytest
from PySide6.QtGui import QImage

from prose_to_video.models.image_job import ProviderImage, ProviderResult
from prose_to_video.models.visual_reference import VisualEntity
from prose_to_video.pipeline.character_references import prepare_character_references
from prose_to_video.pipeline.stills_workflow import Identity, Workflow, generate_character_reference
from prose_to_video.storage.project_files import create_project
from prose_to_video.storage.visual_library import VisualLibrary


CHARACTERS = [
    ('Three Imagrants', 'Worn coats and travel bags'),
    ('3 Immigrants', 'Worn coats and travel bags'),
    ('The travelers', 'A group of three people in worn coats'),
    ('Mira', 'One woman in a blue coat'),
    ('Three travelers', 'Three people in wool coats carrying luggage beside a cart'),
    ('Mira', 'One woman in a blue coat holding a lantern beside a chair'),
]


def assert_subject_composition(prompt, name, description):
    assert name in prompt and description in prompt
    assert 'exactly that many people' in prompt
    assert 'words or digits' in prompt
    assert 'every face and full body' in prompt
    assert 'For an individual character, show only that individual' in prompt
    assert 'One person only' not in prompt
    assert 'Show one character' not in prompt
    assert 'additional characters' not in prompt
    assert 'include only the people and their worn clothing' in prompt
    assert 'Keep all hands empty' in prompt
    assert 'Do not include props, handheld or carried objects, bags, luggage' in prompt
    assert 'blank solid white background' in prompt
    # The restriction follows supplied descriptions so scene props cannot override it.
    assert prompt.index('Reference content restriction:') > prompt.index(description)
    assert 'Ignore any such objects or scene actions' in prompt


@pytest.mark.parametrize('name, description', CHARACTERS)
def test_generated_reference_keeps_group_size_in_provider_request(tmp_path, name, description):
    session = create_project(tmp_path, 'Groups')
    character = Identity(kind='Character', name=name, description=description)
    state = Workflow(catalogs=[character])
    image_path = tmp_path / 'fake.png'
    image = QImage(32, 32, QImage.Format_RGB32)
    image.fill(0xffabcdef)
    assert image.save(str(image_path))
    requests = []

    class Provider:
        name = 'Fake'

        def validate(self, request):
            assert request.candidates == 1  # One image can contain multiple people.

        def submit(self, request):
            requests.append(request)
            return ProviderResult(status='Completed', images=[
                ProviderImage(data=image_path.read_bytes(), model='fake')])

    result = generate_character_reference(session, state, character, Provider())
    assert character.references == [result]
    assert len(state.catalogs) == 1
    assert_subject_composition(requests[0].prompt, name, description)


@pytest.mark.parametrize('name, description', CHARACTERS)
@pytest.mark.parametrize('current_attributes', [False, True])
def test_library_reference_keeps_group_size(tmp_path, name, description, current_attributes):
    library = VisualLibrary(create_project(tmp_path, 'Groups').root)
    library.save_entity(VisualEntity(name=name, entity_type='Character',
                                     appearance_description=description))
    records = prepare_character_references(library, current_attributes=current_attributes)
    assert len(records) == 1
    assert_subject_composition(records[0]['prompt'], name, description)
