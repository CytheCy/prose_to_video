"""Reference preparation and paid-call recovery tested with local fake images."""
import pytest
from PySide6.QtGui import QImage
from prose_to_video.models.visual_reference import VisualEntity, SourceEvidence, VisualStyle, EntityReference
from prose_to_video.models.image_job import ImageGenerationSettings, ProviderResult, ProviderImage
from prose_to_video.pipeline.character_references import (ReferenceStore, prepare_character_references,
    generate_character_reference, reconcile_character_reference, reference_spending)
from prose_to_video.storage.project_files import create_project
from prose_to_video.storage.visual_library import VisualLibrary


def setup(tmp_path):
    session = create_project(tmp_path, 'References')
    library = VisualLibrary(session.root)
    entity = library.save_entity(VisualEntity(name='Ada', entity_type='Character'))
    for scene, age, hair in [('young-scene', 25, 'black hair'), ('old-scene', 70, 'white hair')]:
        library.add_evidence(entity.identifier, SourceEvidence(scene_id=scene, kind='explicit',
            field='appearance_description', value=f'{age} years old, {hair}', quote=f'{age} years old, {hair}'))
    return library, entity


def test_age_specific_preparation_is_repeatable_and_unapproved(tmp_path):
    library, entity = setup(tmp_path)
    library.save_style(VisualStyle(rendering_approach='Risograph'))
    records = prepare_character_references(library)
    assert len(records) == 2
    assert 'white hair' not in records[0]['prompt']
    assert 'black hair' not in records[1]['prompt']
    assert all('Risograph' in r['prompt'] and r['status'] == 'Prepared' for r in records)
    assert {v.status for v in library.variants(entity.identifier)} == {'Suggested'}
    assert [r['identifier'] for r in prepare_character_references(library)] == [r['identifier'] for r in records]
    assert len(ReferenceStore(library).records()) == 2
    assert not library.references(entity.identifier)


class FakeImages:
    def __init__(self, tmp_path):
        path = tmp_path / 'image.png'
        image = QImage(32, 32, QImage.Format_RGB32); image.fill(0xffabcdef); image.save(str(path))
        self.result = ProviderResult(status='Completed', images=[ProviderImage(data=path.read_bytes(), model='fake')])
        self.calls = 0
    def validate(self, request): assert request.candidates == 1
    def submit(self, request): self.calls += 1; return self.result
    def poll(self, identifier): return self.result


def test_generated_candidates_preserve_approval_and_block_resubmission(tmp_path):
    library, entity = setup(tmp_path)
    record = prepare_character_references(library)[0]
    provider = FakeImages(tmp_path)
    result = generate_character_reference(library, record['identifier'], ImageGenerationSettings(), provider)
    ref = library.references(entity.identifier)[0]
    assert result['status'] == 'Completed' and ref.status == 'Suggested' and not ref.is_primary
    assert ref.variant_id == record['variant_id']
    library.attach_image(ref.model_copy(update={'status': 'Approved', 'is_primary': True}))
    with pytest.raises(ValueError, match='already submitted'):
        generate_character_reference(library, record['identifier'], ImageGenerationSettings(), provider)
    assert provider.calls == 1
    assert library.references(entity.identifier)[0].status == 'Approved'


def test_changed_style_blocks_stale_preparation_and_cap_blocks_unknown_cost(tmp_path):
    library, _ = setup(tmp_path)
    record = prepare_character_references(library)[0]
    provider = FakeImages(tmp_path)
    with pytest.raises(ValueError, match='cost estimate'):
        generate_character_reference(library, record['identifier'], ImageGenerationSettings(spending_cap=1), provider)
    assert provider.calls == 0
    library.save_style(VisualStyle(style_name='Ink'))
    with pytest.raises(ValueError, match='changed'):
        generate_character_reference(library, record['identifier'], ImageGenerationSettings(), provider)
    assert provider.calls == 0


def test_timeout_is_persistent_and_never_resubmitted(tmp_path):
    library, _ = setup(tmp_path)
    record = prepare_character_references(library)[0]
    provider = FakeImages(tmp_path)
    def fail(request):
        assert ReferenceStore(library).records()[0]['status'] == 'Submitted'
        raise TimeoutError('uncertain')
    provider.submit = fail
    with pytest.raises(TimeoutError):
        generate_character_reference(library, record['identifier'], ImageGenerationSettings(), provider)
    assert ReferenceStore(library).records()[0]['status'] == 'Needs Reconciliation'
    with pytest.raises(ValueError, match='No provider job ID'):
        reconcile_character_reference(library, record['identifier'], provider)
    with pytest.raises(ValueError, match='unknown'):
        reference_spending(library)


def test_async_reconciliation_uses_saved_job_without_submission(tmp_path):
    library, _ = setup(tmp_path)
    record = prepare_character_references(library)[0]
    provider = FakeImages(tmp_path)
    completed = provider.result
    provider.result = ProviderResult(status='Running', provider_job_id='reference-job')
    settings = ImageGenerationSettings(estimated_cost_per_image=.1, spending_cap=1)
    result = generate_character_reference(library, record['identifier'], settings, provider)
    assert result['provider_job_id'] == 'reference-job'
    provider.result = completed
    assert reconcile_character_reference(library, record['identifier'], provider)['status'] == 'Completed'
    assert provider.calls == 1 and reference_spending(library) == .1


def test_preparation_ignores_rejected_and_superseded_evidence(tmp_path):
    library, entity = setup(tmp_path)
    entity.clothing_accessories = 'green coat'
    library.save_entity(entity)
    entity.clothing_accessories = 'blue coat'
    library.save_entity(entity)
    library.add_evidence(entity.identifier, SourceEvidence(field='appearance_description',
        value='purple skin', kind='explicit', status='Rejected'))
    for record in prepare_character_references(library):
        assert 'blue coat' in record['prompt']
        assert 'green coat' not in record['prompt'] and 'purple skin' not in record['prompt']


def test_reference_preparations_survive_reopening_and_gui_selection(tmp_path):
    import os
    os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
    from PySide6.QtWidgets import QApplication
    from prose_to_video.gui.characters_panel import CharactersPanel
    library, entity = setup(tmp_path)
    prepare_character_references(library)
    app = QApplication.instance() or QApplication([])
    entity.character_paragraphs = [0, 1]
    library.save_entity(entity)
    panel = CharactersPanel()
    panel.set_library(VisualLibrary(library.root))
    assert panel.current_character().identifier == entity.identifier
    assert panel.reference_prompts.count() == 2
    received = []
    panel.generate_reference_requested.connect(received.append)
    panel.generate_reference_button.click()
    assert received and received[0] in {r['identifier'] for r in ReferenceStore(library).records()}
    app.processEvents(); panel.close()


def test_start_styles_in_character_reference_prompts(tmp_path):
    library, _ = setup(tmp_path)
    identifier = library.add_prompt_style('Ink', 'Fine ink lines on cream paper')
    original = prepare_character_references(library)
    library.select_prompt_style(identifier, True)
    styled = prepare_character_references(library)
    assert all('Fine ink lines on cream paper' in record['prompt'] for record in styled)
    assert {r['fingerprint'] for r in original}.isdisjoint(r['fingerprint'] for r in styled)


def test_selected_reference_uses_current_attributes_and_portrait_headroom(tmp_path):
    from prose_to_video.pipeline.character_references import generate_selected_character_reference
    library, entity = setup(tmp_path)
    entity.appearance_description = 'Curly auburn hair'
    entity.clothing_accessories = 'Tall green hat'
    library.save_entity(entity)
    provider = FakeImages(tmp_path)
    requests = []
    original = provider.submit
    def submit(request):
        requests.append(request)
        return original(request)
    provider.submit = submit
    settings = ImageGenerationSettings(width=1536, height=864)
    for _ in range(2):
        result = generate_selected_character_reference(library, entity.identifier, settings, provider)
        assert result['status'] == 'Completed'
    assert provider.calls == 2
    assert len(ReferenceStore(library).records()) == 2
    request = requests[0]
    assert (request.width, request.height) == (1024, 1536)
    assert (settings.width, settings.height) == (1536, 864)
    assert 'Curly auburn hair' in request.prompt and 'Tall green hat' in request.prompt
    assert 'white hair' not in request.prompt and 'black hair' not in request.prompt
    assert '10 percent' in request.prompt and 'headroom' in request.prompt
    assert 'Never crop the head' in request.prompt
    assert library.references(entity.identifier)[0].variant_id is None


def test_selected_reference_blocks_uncertain_request_even_after_edits(tmp_path):
    from prose_to_video.pipeline.character_references import generate_selected_character_reference
    library, entity = setup(tmp_path)
    provider = FakeImages(tmp_path)
    provider.result = ProviderResult(status='Running', provider_job_id='pending')
    generate_selected_character_reference(library, entity.identifier, ImageGenerationSettings(), provider)
    entity.clothing_accessories = 'New hat'
    library.save_entity(entity)
    with pytest.raises(ValueError, match='unresolved'):
        generate_selected_character_reference(library, entity.identifier, ImageGenerationSettings(), provider)
    assert provider.calls == 1
