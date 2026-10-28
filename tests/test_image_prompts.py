from prose_to_video.storage.database import SCHEMA_VERSION
"""Stage Six tests use a deterministic provider and no external request."""
import json
import os
import sqlite3
from threading import Event

import pytest
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from PySide6.QtWidgets import QApplication
from prose_to_video.models.scene import Scene, source_hash
from prose_to_video.models.shot import Shot, Storyboard, StoryboardSettings
from prose_to_video.models.visual_reference import VisualEntity, VisualStyle
from prose_to_video.models.image_prompt import PromptComponents, PromptReference, PromptSettings, assemble_prompt, aspect_ratio
from prose_to_video.pipeline.shot_planning import scene_definition_hash
from prose_to_video.pipeline.image_prompt_generation import PromptDraft, PromptCanceled, generate_prompt, generate_batch
from prose_to_video.storage.project_files import create_project
from prose_to_video.storage.visual_library import VisualLibrary
from prose_to_video.storage.image_prompts import PromptStore
from prose_to_video.storage.image_prompt_export import export_prompt_json, export_prompt_markdown
from prose_to_video.gui.image_prompt_panel import ImagePromptPanel

APP = QApplication.instance() or QApplication([])


class FakeProvider:
    def __init__(self): self.payloads = []
    def generate_image_prompt(self, payload):
        data = json.loads(payload); self.payloads.append(data)
        return PromptDraft(components=PromptComponents(
            shot_description='Jeff sees the robot in the room',
            composition='Jeff stands left of the robot',
            foreground='A low platform', middle_ground='Jeff and the robot',
            background='The rear wall', environment='An underground room',
            character_appearance='Jeff wears a blue coat',
            lighting='Warm ceiling lights'), negative_prompt='No text overlays')


def setup(tmp_path):
    session = create_project(tmp_path, 'Prompts')
    text = 'Jeff sees the robot. Jeff waves.'
    scene = Scene(sequence=1, start=0, end=len(text), text=text, title='Meeting')
    session.database.replace_scenes(text, [scene])
    settings = StoryboardSettings()
    cut = text.index(' Jeff waves')
    shots = [Shot(scene_id=scene.identifier, sequence=i+1, title=f'Shot {i+1}',
        source_start=start, source_end=end, original_prose=text[start:end],
        visual_description='Jeff and robot in a room', camera_framing='Medium shot')
        for i,(start,end) in enumerate([(0,cut),(cut,len(text))])]
    board = Storyboard(scene_id=scene.identifier, scene_start=0, scene_end=len(text),
        scene_hash=source_hash(text), prose_hash=source_hash(text),
        scene_definition_hash=scene_definition_hash(scene), model_name='fake',
        settings=settings, settings_fingerprint=settings.fingerprint(), shots=shots)
    session.database.save_storyboard(board, text)
    library = VisualLibrary(session.root)
    entity = library.save_entity(VisualEntity(name='Jeff', entity_type='Character',
        appearance_description='blue coat', review_status='Approved'))
    library.relate_shot(entity.identifier, shots[0].identifier)
    return session, scene, board, library, entity


def test_model_assembly_settings_and_ratio():
    assert assemble_prompt(PromptComponents(shot_description='Jeff waits.', foreground='A platform')) == 'Jeff waits. A platform.'
    assert aspect_ratio(1920,1080) == '16:9'
    assert aspect_ratio(1000,1000) == '1:1'
    with pytest.raises(ValueError): PromptSettings(detail_level='Max')


def test_generation_grounding_refs_style_and_continuity(tmp_path):
    session, scene, board, library, entity = setup(tmp_path)
    library.save_style(VisualStyle(general_aesthetic='cell shaded'))
    fake = FakeProvider()
    prompt = generate_prompt(board.shots[0], scene, board, library, session.project, PromptSettings(), fake)
    assert '16:9' in prompt.positive_prompt and 'cell shaded' in prompt.positive_prompt
    assert prompt.entity_ids == [entity.identifier]
    assert prompt.negative_prompt == 'No text overlays'
    assert fake.payloads[0]['source_prose'] == board.shots[0].original_prose
    assert fake.payloads[0]['following_shot'] == {'environment': ''}
    assert fake.payloads[0]['entities'][0]['approved_design']['appearance_description'] == 'blue coat'
    assert generate_prompt(board.shots[0],scene,board,library,session.project,
        PromptSettings(generate_negative=False, include_adjacent_shots=False),fake).negative_prompt == ''
    assert fake.payloads[-1]['following_shot'] is None


def test_versions_approval_invalidation_exports_and_package(tmp_path):
    session, scene, board, library, entity = setup(tmp_path)
    store = PromptStore(session.root); fake = FakeProvider(); settings = PromptSettings()
    prompts = generate_batch(board.shots, {scene.identifier:scene}, {scene.identifier:board}, library,
        session.project, settings, fake, store)
    assert len(prompts) == 2 and len(fake.payloads) == 2
    assert not generate_batch(board.shots,{scene.identifier:scene},{scene.identifier:board},library,
        session.project,settings,fake,store)
    first = store.active(board.shots[0].identifier)
    assert not store.validate(first, scene, board.shots[0], board, session.project, library)
    approved = store.approve(first.shot_id, first.version, scene, board.shots[0], board, session.project, library)
    assert store.approved(first.shot_id).identifier == approved.identifier
    edited = store.edit(first.shot_id, positive_prompt=first.positive_prompt+' A glass door.',
        negative_prompt='', components=first.components)
    assert edited.version == 2 and edited.manually_edited
    assert store.approved(first.shot_id).identifier == approved.identifier
    store.restore(first.shot_id,1)
    package = store.generation_package(first.shot_id, scene, board.shots[0], board, session.project, library)
    assert package['version'] == 1 and package['aspect_ratio'] == '16:9'
    assert len(json.loads(export_prompt_json([scene],{scene.identifier:board},store))['prompts']) == 2
    assert '## Meeting' in export_prompt_markdown([scene],{scene.identifier:board},store)
    changed = library.entity(entity.identifier); changed.appearance_description = 'red coat'; library.save_entity(changed)
    assert store.state(approved, scene, board.shots[0], board, session.project, library) == 'Outdated'
    assert store.state(store.active(board.shots[1].identifier), scene, board.shots[1], board,
                       session.project, library) != 'Outdated'
    with pytest.raises(ValueError): store.generation_package(first.shot_id, scene, board.shots[0], board,
                                                                session.project, library)


def test_cancellation_and_transaction_preserves_version(tmp_path):
    session, scene, board, library, _ = setup(tmp_path)
    store = PromptStore(session.root); fake = FakeProvider(); cancel = Event(); cancel.set()
    with pytest.raises(PromptCanceled):
        generate_batch(board.shots,{scene.identifier:scene},{scene.identifier:board},library,
                       session.project,PromptSettings(),fake,store,cancel=cancel)
    assert store.latest(board.shots[0].identifier) is None
    prompt = generate_prompt(board.shots[0],scene,board,library,session.project,PromptSettings(),fake)
    store.save(prompt)
    duplicate = PromptReference(asset_id='asset', path='references/ref.png', role='Character')
    next_prompt = prompt.model_copy(update={'identifier':'version-two', 'version':2,
                                            'references':[duplicate,duplicate]})
    with pytest.raises(sqlite3.IntegrityError): store.save(next_prompt)
    assert len(store.versions(prompt.shot_id)) == 1


def test_gui_editor_selection_and_save(tmp_path):
    app = QApplication.instance() or QApplication([])
    session, scene, board, library, _ = setup(tmp_path)
    store = PromptStore(session.root)
    prompt = generate_prompt(board.shots[0],scene,board,library,session.project,PromptSettings(),FakeProvider())
    store.save(prompt)
    editor = ImagePromptPanel(); editor.set_prompt(prompt,store.versions(prompt.shot_id),session.root)
    assert editor.fields['positive_prompt'].toPlainText() == prompt.positive_prompt
    editor.fields['foreground'].setPlainText('A steel platform')
    positive, negative, components = editor.changes()
    saved = store.edit(prompt.shot_id,positive_prompt=positive,negative_prompt=negative,components=components)
    assert store.active(prompt.shot_id).components.foreground == 'A steel platform'
    assert saved.version == 2
    editor.close(); editor.deleteLater(); app.processEvents()


def test_variant_pinned_image_missing_file_and_thumbnail(tmp_path):
    from PySide6.QtGui import QImage, QColor
    from prose_to_video.models.visual_reference import AppearanceVariant, EntityReference
    app = QApplication.instance() or QApplication([])
    session, scene, board, library, entity = setup(tmp_path)
    variant = library.add_variant(AppearanceVariant(entity_id=entity.identifier, name='Rain',
        description='yellow raincoat', status='Approved'))
    library.relate_shot(entity.identifier, board.shots[0].identifier, variant.identifier)
    image_path = tmp_path / 'ref.png'
    image = QImage(16,16,QImage.Format_RGB32); image.fill(QColor('yellow'))
    assert image.save(str(image_path))
    asset = library.import_image(image_path)
    library.attach_image(EntityReference(entity_id=entity.identifier, asset_id=asset.identifier,
        variant_id=variant.identifier, status='Approved', role='Character front', is_primary=True))
    library.relate_shot_asset(board.shots[0].identifier, asset.identifier)
    prompt = generate_prompt(board.shots[0],scene,board,library,session.project,PromptSettings(),FakeProvider())
    assert prompt.variant_ids[entity.identifier] == variant.identifier
    assert 'yellow raincoat' in prompt.positive_prompt
    assert {r.role for r in prompt.references} == {'Character front','Pinned shot reference'}
    editor = ImagePromptPanel(); editor.set_prompt(prompt,[prompt],session.root)
    assert editor.references.count() == 2
    assert not editor.references.item(0).icon().isNull()
    editor.close(); editor.deleteLater(); app.processEvents()
    (session.root / asset.path).unlink()
    assert PromptStore(session.root).validate(prompt,scene,board.shots[0],board,session.project,library)


def test_style_inheritance_and_aspect_conflict(tmp_path):
    session, scene, board, library, _ = setup(tmp_path)
    library.save_style(VisualStyle(general_aesthetic='illustration', color_palette='blue'))
    library.save_style_override('scene',scene.identifier,VisualStyle(color_palette='amber'))
    library.save_style_override('shot',board.shots[0].identifier,VisualStyle(lighting='dim'))
    prompt = generate_prompt(board.shots[0],scene,board,library,session.project,PromptSettings(),FakeProvider())
    assert 'illustration' in prompt.positive_prompt and 'amber' in prompt.positive_prompt and 'dim' in prompt.positive_prompt
    assert 'blue' not in prompt.components.visual_style
    prompt.positive_prompt += ' Compose for 4:3.'
    assert 'Contradictory aspect-ratio instruction' in PromptStore(session.root).validate(
        prompt,scene,board.shots[0],board,session.project,library)


def test_main_window_prompt_selection_edit_and_save(tmp_path):
    from prose_to_video.gui.main_window import MainWindow
    app = QApplication.instance() or QApplication([])
    session, scene, board, library, _ = setup(tmp_path)
    store = PromptStore(session.root)
    prompt = generate_prompt(board.shots[0],scene,board,library,session.project,PromptSettings(),FakeProvider())
    store.save(prompt)
    window = MainWindow()
    window._set_session(session, load_prose=False)
    window._source_text = scene.text
    window.scenes_panel.set_scenes([scene])
    window._load_selected_storyboard()
    window.storyboard_panel.select_id(board.shots[0].identifier)
    assert window.image_prompt_panel.prompt.identifier == prompt.identifier
    window.image_prompt_panel.fields['foreground'].setPlainText('A cargo platform')
    window._save_prompt()
    assert store.active(prompt.shot_id).components.foreground == 'A cargo platform'
    assert window.image_prompt_panel.version.count() == 2
    window.close()


def test_version_five_migration_keeps_visual_data(tmp_path):
    session, scene, board, library, entity = setup(tmp_path)
    with sqlite3.connect(session.database.path) as con:
        for table in ('prompt_reference','prompt_approved','prompt_active','image_prompt','prompt_settings'):
            con.execute(f'DROP TABLE {table}')
        con.execute('PRAGMA user_version=5')
    store = PromptStore(session.root)
    assert store.settings().detail_level == 'Detailed'
    assert library.entity(entity.identifier).name == 'Jeff'
    with sqlite3.connect(session.database.path) as con:
        assert con.execute('PRAGMA user_version').fetchone()[0] == SCHEMA_VERSION
        assert con.execute("SELECT name FROM sqlite_master WHERE name='image_prompt'").fetchone()


def test_project_batch_and_partial_cancel(tmp_path):
    session = create_project(tmp_path,'Batch')
    text = 'Jeff waits. The robot enters.'
    cut = text.index(' The robot')
    scenes = [Scene(sequence=1,start=0,end=cut,text=text[:cut],title='Wait'),
              Scene(sequence=2,start=cut,end=len(text),text=text[cut:],title='Entry')]
    session.database.replace_scenes(text,scenes)
    boards = {}
    for scene in scenes:
        shot = Shot(scene_id=scene.identifier,sequence=1,title=scene.title,
            source_start=scene.start,source_end=scene.end,original_prose=scene.text,
            visual_description=scene.text)
        settings = StoryboardSettings()
        board = Storyboard(scene_id=scene.identifier,scene_start=scene.start,scene_end=scene.end,
            scene_hash=source_hash(scene.text),prose_hash=source_hash(text),
            scene_definition_hash=scene_definition_hash(scene),model_name='fake',settings=settings,
            settings_fingerprint=settings.fingerprint(),shots=[shot])
        session.database.save_storyboard(board,text)
        boards[scene.identifier] = board
    shots = [b.shots[0] for b in boards.values()]
    library = VisualLibrary(session.root); store = PromptStore(session.root); fake = FakeProvider()
    cancel = Event()
    def progress(index,total):
        if index == 1: cancel.set()
    with pytest.raises(PromptCanceled):
        generate_batch(shots,{s.identifier:s for s in scenes},boards,library,session.project,
                       PromptSettings(),fake,store,cancel=cancel,progress=progress)
    assert store.latest(shots[0].identifier) is not None
    assert store.latest(shots[1].identifier) is None
    cancel.clear()
    result = generate_batch(shots,{s.identifier:s for s in scenes},boards,library,session.project,
                            PromptSettings(),fake,store)
    assert len(result) == 1 and store.latest(shots[1].identifier) is not None


def test_failed_regeneration_keeps_approved_and_settings_persist(tmp_path):
    session, scene, board, library, _ = setup(tmp_path)
    store = PromptStore(session.root)
    settings = PromptSettings(detail_level='Concise', include_adjacent_shots=False)
    store.save_settings(settings)
    assert PromptStore(session.root).settings() == settings
    first = generate_prompt(board.shots[0],scene,board,library,session.project,settings,FakeProvider())
    store.save(first)
    store.approve(first.shot_id,first.version,scene,board.shots[0],board,session.project,library)
    class Broken:
        def generate_image_prompt(self,payload): return {'components': {'shot_description': ''}}
    with pytest.raises(ValueError):
        generate_prompt(board.shots[0],scene,board,library,session.project,settings,Broken(),version=2)
    assert store.approved(first.shot_id).identifier == first.identifier
    assert len(store.versions(first.shot_id)) == 1


def test_shot_change_invalidates_only_its_prompt(tmp_path):
    session, scene, board, library, _ = setup(tmp_path)
    store = PromptStore(session.root); fake = FakeProvider()
    prompts = generate_batch(board.shots,{scene.identifier:scene},{scene.identifier:board},library,
        session.project,PromptSettings(),fake,store)
    changed_board = board.model_copy(deep=True)
    changed_board.shots[0].camera_angle = 'Low angle'
    session.database.save_storyboard(changed_board,scene.text,replace=True)
    assert store.state(prompts[0],scene,changed_board.shots[0],changed_board,session.project,library) == 'Outdated'
    assert store.state(prompts[1],scene,changed_board.shots[1],changed_board,session.project,library) != 'Outdated'
    assert store.versions(prompts[0].shot_id)[0].identifier == prompts[0].identifier
    store.save_settings(PromptSettings(detail_level='Highly detailed'))
    assert 'Prompt generation settings changed' in store.validate(
        prompts[1],scene,changed_board.shots[1],changed_board,session.project,library)
