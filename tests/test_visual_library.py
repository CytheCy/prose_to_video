"""Stage Five integration tests. AI calls are deterministic local fakes."""
from __future__ import annotations
import json
import os
import shutil
import sqlite3
import time
from pathlib import Path
from threading import Event
import pytest
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from PySide6.QtGui import QImage, QColor
from PySide6.QtWidgets import QApplication
from prose_to_video.models.scene import Scene, source_hash
from prose_to_video.models.shot import Shot, Storyboard, StoryboardSettings
from prose_to_video.models.visual_reference import (AppearanceVariant, EntityReference, ReferenceAsset,
    SourceEvidence, VisualEntity, VisualStyle)
from prose_to_video.pipeline.entity_extraction import (AppearanceProposal, EntityProposal, EntityProposals,
    ExtractionCanceled, extract_entities)
from prose_to_video.pipeline.shot_planning import scene_definition_hash
from prose_to_video.storage.project_files import create_project, open_project
from prose_to_video.storage.visual_library import VisualLibrary
from prose_to_video.storage.visual_export import export_json, export_markdown, export_style
from prose_to_video.gui.visual_library_panel import VisualLibraryPanel


def setup(tmp_path):
    session = create_project(tmp_path, 'Story')
    text = 'Jeff sees a white robot. Jeff waves.'
    scene = Scene(sequence=1, start=0, end=len(text), text=text, title='Meeting')
    session.database.replace_scenes(text, [scene])
    settings = StoryboardSettings()
    shot = Shot(scene_id=scene.identifier, sequence=1, title='Meeting', source_start=0,
                source_end=len(text), original_prose=text, visual_description='Jeff sees robot')
    board = Storyboard(scene_id=scene.identifier, scene_start=0, scene_end=len(text),
        scene_hash=source_hash(text), prose_hash=source_hash(text),
        scene_definition_hash=scene_definition_hash(scene), model_name='mock', settings=settings,
        settings_fingerprint=settings.fingerprint(), shots=[shot])
    session.database.save_storyboard(board, text)
    return session, VisualLibrary(session.root), scene, shot, text


def picture(path, color='white', fmt='PNG'):
    image = QImage(32, 20, QImage.Format_RGB32); image.fill(QColor(color))
    assert image.save(str(path), fmt)


def test_model_validation_and_migration(tmp_path):
    with pytest.raises(ValueError): VisualEntity(name=' ', entity_type='Character')
    with pytest.raises(ValueError): VisualEntity(name='Jeff', entity_type='Alien')
    with pytest.raises(ValueError): VisualEntity(name='Jeff', aliases=['jeff'])
    with pytest.raises(ValueError): ReferenceAsset(path='../wrong.png', sha256='a', width=1, height=1, format='PNG')
    session, library, *_ = setup(tmp_path)
    with sqlite3.connect(session.database.path) as con:
        assert con.execute('PRAGMA user_version').fetchone()[0] == 7
        assert con.execute("SELECT name FROM sqlite_master WHERE name='visual_entity'").fetchone()
    assert open_project(session.root).project.identifier == session.project.identifier
    assert library.style() == VisualStyle()


def test_alias_merge_separate_and_relationships(tmp_path):
    _, library, scene, shot, _ = setup(tmp_path)
    first = library.save_entity(VisualEntity(name='Jeffrey', entity_type='Character', aliases=['Jeff']))
    duplicate = library.save_entity(VisualEntity(name='J.', entity_type='Character'))
    library.relate_scene(first.identifier, scene.identifier)
    library.relate_shot(duplicate.identifier, shot.identifier)
    assert library.entities_for_shot(shot.identifier)[0].identifier == duplicate.identifier
    with pytest.raises(ValueError): library.merge_entities(first.identifier, duplicate.identifier)
    library.merge_entities(first.identifier, duplicate.identifier, confirmed=True)
    assert library.entity(duplicate.identifier) is None
    assert 'J.' in library.entity(first.identifier).aliases
    assert library.shots_for(first.identifier)[0]['shot_id'] == shot.identifier
    separated = library.separate(first.identifier, shot_ids=[shot.identifier], new_name='Other Jeff')
    assert library.entities_for_shot(shot.identifier)[0].identifier == separated.identifier
    assert not library.shots_for(first.identifier)
    assert [e['action'] for e in library.audit_log()][-2:] == ['entities_merged', 'entity_separated']
    with pytest.raises(ValueError): library.relate_scene(first.identifier, 'missing')
    with pytest.raises(ValueError): library.relate_shot(first.identifier, 'missing')


def test_images_hash_thumbnail_primary_portability_and_integrity(tmp_path):
    session, library, *_ = setup(tmp_path)
    source = tmp_path / 'original.png'; picture(source)
    entity = library.save_entity(VisualEntity(name='Robot', entity_type='Robot'))
    asset = library.import_image(source)
    assert asset.width == 32 and asset.height == 20 and asset.path.startswith('references/')
    assert library.thumbnail_path(asset).is_file()
    assert library.import_image(source).identifier == asset.identifier
    source.unlink()
    assert (session.root / asset.path).is_file()
    library.attach_image(EntityReference(entity_id=entity.identifier, asset_id=asset.identifier,
        role='Front view', status='Approved'))
    library.set_primary(entity.identifier, asset.identifier)
    assert library.references(entity.identifier)[0].is_primary
    moved = tmp_path / 'Moved'; shutil.move(session.root, moved)
    portable = VisualLibrary(moved)
    assert not portable.asset_issues()
    (moved / asset.path).write_bytes(b'changed')
    assert portable.asset_issues()[0].code == 'changed_asset'
    (moved / asset.path).unlink()
    assert portable.asset_issues()[0].code == 'missing_asset'


def test_invalid_images_and_duplicate_names(tmp_path):
    _, library, *_ = setup(tmp_path)
    bad = tmp_path / 'bad.png'; bad.write_text('broken')
    with pytest.raises(ValueError): library.import_image(bad)
    wrong = tmp_path / 'wrong.gif'; wrong.write_bytes(b'GIF89a')
    with pytest.raises(ValueError): library.import_image(wrong)
    a = tmp_path / 'one' / 'same.png'; b = tmp_path / 'two' / 'same.png'
    a.parent.mkdir(); b.parent.mkdir(); picture(a); picture(b, 'black')
    x, y = library.import_image(a), library.import_image(b)
    assert x.path != y.path


def test_variants_style_package_and_export(tmp_path):
    session, library, scene, shot, _ = setup(tmp_path)
    entity = library.save_entity(VisualEntity(name='Robot', entity_type='Robot',
        appearance_description='White shell', review_status='Approved'))
    library.relate_scene(entity.identifier, scene.identifier)
    variant = library.add_variant(AppearanceVariant(entity_id=entity.identifier, name='Damaged',
        description='Cracked shell', scene_ids=[scene.identifier], status='Approved'))
    library.relate_shot(entity.identifier, shot.identifier, variant.identifier)
    source = tmp_path / 'robot.png'; picture(source)
    asset = library.import_image(source)
    library.attach_image(EntityReference(entity_id=entity.identifier, asset_id=asset.identifier,
        variant_id=variant.identifier, role='Detail view', status='Approved', is_primary=True))
    library.save_style(VisualStyle(style_name='Painted', lighting='Warm'), 'Concept art')
    assert library.load_style_preset('Concept art').lighting == 'Warm'
    package = library.package_for_shot(shot.identifier)
    assert package['style']['style_name'] == 'Painted'
    assert package['entities'][0]['variant']['name'] == 'Damaged'
    assert package['entities'][0]['references'][0]['asset']['path'] == asset.path
    assert 'bytes' not in json.dumps(package)
    output = tmp_path / 'guide.json'; export_json(library, output)
    assert json.loads(output.read_text())['entities'][0]['entity']['identifier'] == entity.identifier
    output = tmp_path / 'guide.md'; export_markdown(library, output)
    assert 'Robot' in output.read_text() and asset.path in output.read_text()
    output = tmp_path / 'style.json'; export_style(library, output)
    assert json.loads(output.read_text())['style_name'] == 'Painted'


def test_extraction_reconciliation_provenance_and_preservation(tmp_path):
    _, library, scene, _, text = setup(tmp_path)
    approved = library.save_entity(VisualEntity(name='Jeff', entity_type='Character',
        appearance_description='User designed coat', review_status='Approved'))
    class Fake:
        calls = 0
        def extract_entities(self, payload):
            self.calls += 1
            assert json.loads(payload)['scene_id'] == scene.identifier
            return EntityProposals(entities=[EntityProposal(name='Jeff', entity_type='Character',
                appearances=[AppearanceProposal(field='clothing_accessories', value='Blue coat',
                                                exact_quote='blue coat', kind='explicit')]),
                EntityProposal(name='Robot', entity_type='Robot', appearances=[
                    AppearanceProposal(field='colors_materials', value='white',
                                       exact_quote='white robot', kind='explicit')])])
    fake = Fake()
    extract_entities([scene]*5, library, fake, manuscript_hash=source_hash(text))
    assert fake.calls == 5
    assert len(library.entities()) == 2
    assert library.entity(approved.identifier).appearance_description == 'User designed coat'
    assert {e.kind for e in library.evidence(approved.identifier)} == {'user', 'ai_proposed'}
    robot = next(e for e in library.entities() if e.name == 'Robot')
    evidence = library.evidence(robot.identifier)[0]
    assert evidence.kind == 'explicit' and text[evidence.source_start:evidence.source_end] == evidence.quote
    assert len(library.evidence(robot.identifier)) == 1
    stopped = Event(); stopped.set()
    with pytest.raises(ExtractionCanceled): extract_entities([scene], library, fake, cancel=stopped)
    assert fake.calls == 5


def test_transaction_rollback_and_continuity(tmp_path):
    _, library, scene, shot, _ = setup(tmp_path)
    entity = library.save_entity(VisualEntity(name='Jeff', entity_type='Character'))
    with pytest.raises(Exception):
        library.attach_image(EntityReference(entity_id=entity.identifier, asset_id='missing'))
    assert library.references(entity.identifier) == []
    evidence = SourceEvidence(scene_id=scene.identifier, field='colors_materials', value='white', kind='explicit')
    library.add_evidence(entity.identifier, evidence)
    library.add_evidence(entity.identifier, SourceEvidence(scene_id=scene.identifier,
        field='colors_materials', value='black', kind='explicit'))
    duplicate = library.save_entity(VisualEntity(name='Jeff', entity_type='Character'))
    library.relate_shot(entity.identifier, shot.identifier)
    issues = library.inspect_continuity()
    assert {'duplicate_identity', 'description_conflict'} <= {i.code for i in issues}
    assert duplicate.identifier != entity.identifier


def test_gui_selection_and_preview(tmp_path):
    app = QApplication.instance() or QApplication([])
    _, library, scene, _, _ = setup(tmp_path)
    entity = library.save_entity(VisualEntity(name='Robot', entity_type='Robot'))
    library.relate_scene(entity.identifier, scene.identifier)
    source = tmp_path / 'robot.png'; picture(source)
    asset = library.import_image(source)
    library.attach_image(EntityReference(entity_id=entity.identifier, asset_id=asset.identifier, status='Approved'))
    panel = VisualLibraryPanel(); panel.set_library(library); panel.show(); app.processEvents()
    assert panel.entities.count() == 1
    assert panel.current_id == entity.identifier
    assert panel.gallery.count() == 1
    assert panel.preview.pixmap() and not panel.preview.pixmap().isNull()
    panel.search.setText('missing'); app.processEvents(); assert panel.entities.count() == 0
    panel.close()


def test_style_overrides_and_manual_shot_exclusion(tmp_path):
    _, library, scene, shot, _ = setup(tmp_path)
    library.save_style(VisualStyle(style_name='Grounded', lighting='Soft', camera_style='Static'))
    library.save_style_override('scene', scene.identifier, VisualStyle(lighting='Night'))
    library.save_style_override('shot', shot.identifier, VisualStyle(camera_style='Handheld'))
    style = library.style_for_shot(shot.identifier)
    assert (style.style_name, style.lighting, style.camera_style) == ('Grounded', 'Night', 'Handheld')
    entity = library.save_entity(VisualEntity(name='Jeff', entity_type='Character'))
    library.relate_scene(entity.identifier, scene.identifier)
    from prose_to_video.pipeline.entity_extraction import reconcile_shot_links
    reconcile_shot_links(scene, library)
    assert library.entities_for_shot(shot.identifier)
    library.relate_shot(entity.identifier, shot.identifier, present=False)
    reconcile_shot_links(scene, library)
    assert library.entities_for_shot(shot.identifier) == []
    library.relate_shot(entity.identifier, shot.identifier)
    assert library.entities_for_shot(shot.identifier)


def test_long_scene_chunking_and_ambiguous_names(tmp_path):
    session = create_project(tmp_path, 'Long')
    text = ('Jeff walks through the station. ' * 100)
    scene = Scene(sequence=1, start=0, end=len(text), text=text)
    session.database.replace_scenes(text, [scene])
    library = VisualLibrary(session.root)
    class Fake:
        calls = 0
        def extract_entities(self, payload):
            self.calls += 1
            return EntityProposals(entities=[EntityProposal(name='Jeff', entity_type='Character')])
    fake = Fake()
    extract_entities([scene], library, fake, chunk_chars=1000)
    assert fake.calls > 1 and len(library.entities()) == 1
    library.save_entity(VisualEntity(name='Jeff', entity_type='Character'))
    before = len(library.entities())
    extract_entities([scene], library, fake, chunk_chars=1000)
    assert len(library.entities()) == before


def test_merge_preserves_variant_and_separation_moves_evidence(tmp_path):
    _, library, scene, shot, _ = setup(tmp_path)
    a = library.save_entity(VisualEntity(name='Jeffrey', entity_type='Character'))
    b = library.save_entity(VisualEntity(name='Jeff', entity_type='Character',
                                         appearance_description='Blue coat', review_status='Approved'))
    evidence = library.add_evidence(b.identifier, SourceEvidence(scene_id=scene.identifier,
        field='clothing_accessories', value='Blue coat', kind='user', status='Approved'))
    variant = library.add_variant(AppearanceVariant(entity_id=b.identifier, name='Formal', status='Approved'))
    library.relate_shot(b.identifier, shot.identifier, variant.identifier)
    library.merge_entities(a.identifier, b.identifier, confirmed=True)
    assert library.entity(a.identifier).appearance_description == 'Blue coat'
    assert library.variants(a.identifier)[0].entity_id == a.identifier
    separated = library.separate(a.identifier, evidence_ids=[evidence.identifier], variant_ids=[variant.identifier],
                                 new_name='Another Jeff')
    assert library.evidence(separated.identifier)[0].identifier == evidence.identifier
    assert library.variants(separated.identifier)[0].identifier == variant.identifier
    assert library.audit_log()[-2]['action'] == 'entities_merged'


def test_png_jpeg_webp_and_approved_reference_reanalysis(tmp_path):
    _, library, scene, _, text = setup(tmp_path)
    source = tmp_path / 'portrait.png'; picture(source)
    jeff = library.save_entity(VisualEntity(name='Jeff', entity_type='Character',
        appearance_description='Manually approved look', review_status='Approved'))
    asset = library.import_image(source)
    library.attach_image(EntityReference(entity_id=jeff.identifier, asset_id=asset.identifier,
        role='Primary portrait', status='Approved', is_primary=True))
    class Fake:
        def extract_entities(self, payload):
            return EntityProposals(entities=[EntityProposal(name='Jeff', entity_type='Character',
                appearances=[AppearanceProposal(field='appearance_description', value='AI suggestion',
                    kind='ai_proposed')])])
    for _ in range(2): extract_entities([scene], library, Fake(), manuscript_hash=source_hash(text))
    assert library.entity(jeff.identifier).appearance_description == 'Manually approved look'
    assert library.references(jeff.identifier)[0].asset_id == asset.identifier
    assert library.references(jeff.identifier)[0].status == 'Approved'
    assert len([e for e in library.evidence(jeff.identifier) if e.kind == 'ai_proposed']) == 1
    for fmt, extension in [('JPEG', 'jpg'), ('WEBP', 'webp')]:
        path = tmp_path / f'view.{extension}'
        picture(path, 'red' if fmt == 'JPEG' else 'blue', fmt)
        imported = library.import_image(path)
        assert imported.format == fmt
        assert library.thumbnail_path(imported).is_file()


def test_real_version_four_migration(tmp_path):
    from prose_to_video.storage.database import MIGRATIONS, ProjectDatabase
    db_path = tmp_path / 'project.sqlite'
    with sqlite3.connect(db_path) as con:
        for version in range(1, 5):
            con.executescript('BEGIN;' + MIGRATIONS[version] + f'PRAGMA user_version={version}; COMMIT;')
    db = ProjectDatabase(db_path)
    # Loading the schema through a normal operation upgrades without replacing earlier tables.
    digest, scenes = db.load_scenes()
    assert digest is None and scenes == []
    with sqlite3.connect(db_path) as con:
        assert con.execute('PRAGMA user_version').fetchone()[0] == 7
        assert con.execute("SELECT name FROM sqlite_master WHERE name='visual_entity'").fetchone()
        assert con.execute("SELECT name FROM sqlite_master WHERE name='shot'").fetchone()


def test_approved_reference_cannot_be_downgraded_by_duplicate_import(tmp_path):
    _, library, *_ = setup(tmp_path)
    entity = library.save_entity(VisualEntity(name='Robot', entity_type='Robot'))
    source = tmp_path / 'robot.png'; picture(source)
    asset = library.import_image(source)
    library.attach_image(EntityReference(entity_id=entity.identifier, asset_id=asset.identifier,
        status='Approved', is_primary=True))
    with pytest.raises(ValueError, match='Approved reference'):
        library.attach_image(EntityReference(entity_id=entity.identifier, asset_id=asset.identifier))
    assert library.references(entity.identifier)[0].status == 'Approved'
    assert library.references(entity.identifier)[0].is_primary


def test_gui_mock_extraction_and_storyboard_reference(tmp_path, monkeypatch):
    import prose_to_video.gui.main_window as main_window
    from prose_to_video.gui.main_window import MainWindow
    from prose_to_video.storage.project_files import import_prose
    app = QApplication.instance() or QApplication([])
    source = tmp_path / 'story.txt'; source.write_text('Jeff sees a white robot. Jeff waves.')
    session = import_prose(create_project(tmp_path, 'GUI'), source)
    text = source.read_text()
    scene = Scene(sequence=1, start=0, end=len(text), text=text)
    session.database.replace_scenes(text, [scene])
    class Fake:
        def extract_entities(self, payload):
            return EntityProposals(entities=[EntityProposal(name='Jeff', entity_type='Character')])
    monkeypatch.setattr(main_window, 'make_provider', lambda settings: Fake())
    window = MainWindow(); window._set_session(session)
    deadline = time.monotonic() + 5
    while (window._task is not None or not window._prose_ready) and time.monotonic() < deadline:
        app.processEvents(); time.sleep(.01)
    assert window._prose_ready
    window._extract_entities('selected')
    deadline = time.monotonic() + 5
    while window._entity_task is not None and time.monotonic() < deadline:
        app.processEvents(); time.sleep(.01)
    assert window._entity_task is None
    assert window.visual_library_panel.entities.count() == 1
    assert window.workspace_tabs.tabText(2) == 'Visual Library'
    window.close()


def test_pinned_shot_image_and_safe_cleanup(tmp_path):
    _, library, _, shot, _ = setup(tmp_path)
    entity = library.save_entity(VisualEntity(name='Robot', entity_type='Robot', review_status='Approved'))
    library.relate_shot(entity.identifier, shot.identifier)
    source = tmp_path / 'robot.png'; picture(source)
    asset = library.import_image(source)
    library.attach_image(EntityReference(entity_id=entity.identifier, asset_id=asset.identifier, status='Approved'))
    library.relate_shot_asset(shot.identifier, asset.identifier)
    assert library.package_for_shot(shot.identifier)['shot_references'][0]['identifier'] == asset.identifier
    library.remove_reference(entity.identifier, asset.identifier, confirmed=True)
    assert library.cleanup_unreferenced_assets(confirmed=True) == 0
    library.relate_shot_asset(shot.identifier, asset.identifier, False)
    with pytest.raises(ValueError): library.cleanup_unreferenced_assets()
    assert library.cleanup_unreferenced_assets(confirmed=True) == 1
    assert not (library.root / asset.path).exists()


def test_extraction_chunk_rolls_back_on_write_failure(tmp_path, monkeypatch):
    _, library, scene, _, _ = setup(tmp_path)
    class Fake:
        def extract_entities(self, payload):
            return EntityProposals(entities=[EntityProposal(name='Jeff', entity_type='Character'),
                                             EntityProposal(name='Robot', entity_type='Robot')])
    original = library._audit
    calls = 0
    def failing(con, action, **details):
        nonlocal calls
        calls += 1
        if calls == 2: raise RuntimeError('write failed')
        return original(con, action, **details)
    monkeypatch.setattr(library, '_audit', failing)
    with pytest.raises(RuntimeError, match='write failed'):
        extract_entities([scene], library, Fake())
    assert library.entities() == []


def test_ai_alias_requires_review_before_becoming_identity(tmp_path):
    _, library, scene, _, _ = setup(tmp_path)
    class Fake:
        def extract_entities(self, payload):
            return EntityProposals(entities=[EntityProposal(name='Jeffrey', entity_type='Character', aliases=['Jeff'])])
    extract_entities([scene], library, Fake())
    entity = library.entities()[0]
    assert entity.aliases == []
    suggestion = next(e for e in library.evidence(entity.identifier) if e.field == 'alias')
    assert suggestion.kind == 'ai_proposed' and suggestion.status == 'Suggested'
    library.set_evidence_status(suggestion.identifier, 'Approved')
    assert library.entity(entity.identifier).aliases == ['Jeff']


def test_variant_context_warning_and_approved_only_assignment(tmp_path):
    _, library, scene, shot, _ = setup(tmp_path)
    entity = library.save_entity(VisualEntity(name='Robot', entity_type='Robot'))
    draft = library.add_variant(AppearanceVariant(entity_id=entity.identifier, name='Draft'))
    with pytest.raises(ValueError, match='approved'):
        library.relate_shot(entity.identifier, shot.identifier, draft.identifier)
    approved = library.add_variant(AppearanceVariant(entity_id=entity.identifier, name='Damaged',
        scene_ids=['another-scene'], status='Approved'))
    library.relate_shot(entity.identifier, shot.identifier, approved.identifier)
    assert any(w.code == 'variant_context' for w in library.inspect_continuity())
