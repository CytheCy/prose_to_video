import json
import os
from threading import Event

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
import pytest
from PySide6.QtGui import QImage
from PySide6.QtWidgets import QApplication
from prose_to_video.pipeline.character_detection import detect_characters, listed_characters, CharactersCanceled
from prose_to_video.pipeline.providers import AISettings
from prose_to_video.storage.project_files import create_project
from prose_to_video.storage.visual_library import VisualLibrary
from prose_to_video.models.visual_reference import EntityReference
from prose_to_video.gui.characters_panel import CharactersPanel


class Provider:
    def resolve_character_mentions(self, payload):
        return {'characters': []}

    def detect_characters(self, payload):
        data = json.loads(payload)
        ids = [p['id'] for p in data['paragraphs']]
        return {'characters': [
            {'name': 'Mara', 'aliases': ['Captain'], 'paragraph_ids': ids + ids,
             'appearance_description': 'Short red hair', 'clothing_accessories': 'Blue coat'},
            {'name': 'Courier', 'paragraph_ids': [ids[0]]},
        ]}


def test_distinct_paragraphs_and_preservation(tmp_path):
    library = VisualLibrary(create_project(tmp_path, 'Characters').root)
    text = 'Mara greets a courier. Mara waves.\r\nStill the same paragraph.\r\n\r\nShe leaves.'
    result = detect_characters(text, library, Provider(), AISettings())
    assert [e.name for e in result] == ['Mara']
    assert result[0].character_paragraphs == [1, 2]
    assert result[0].appearance_description == 'Short red hair'
    entity = result[0]
    entity.appearance_description = 'User red hair'
    library.save_entity(entity)
    again = detect_characters(text, library, Provider(), AISettings())
    assert again[0].identifier == entity.identifier
    assert again[0].appearance_description == 'User red hair'
    assert library.entity(entity.identifier).aliases == ['Captain']
    assert len(listed_characters(VisualLibrary(library.root))) == 1


def test_long_single_paragraph_does_not_recur(tmp_path):
    library = VisualLibrary(create_project(tmp_path, 'Long').root)
    assert detect_characters('Mara waves. ' * 300, library, Provider(), AISettings(chunk_chars=1000, overlap_chars=100)) == []


def test_alias_across_chunks_and_rollback(tmp_path):
    library = VisualLibrary(create_project(tmp_path, 'Chunks').root)
    class AliasProvider(Provider):
        calls = 0
        def detect_characters(self, payload):
            self.calls += 1
            data = json.loads(payload)
            return {'characters': [{'name': 'Mara' if self.calls == 1 else 'Captain', 'aliases': ['Captain', 'Mara'],
                                    'paragraph_ids': [p['id'] for p in data['paragraphs']]}]}
    text = 'Mara ' + 'a' * 990 + '\n\nCaptain returns.'
    result = detect_characters(text, library, AliasProvider(), AISettings(chunk_chars=1000, overlap_chars=100))
    assert len(result) == 1
    class Invalid(Provider):
        def detect_characters(self, payload):
            return {'characters': [{'name': 'Other', 'paragraph_ids': [999]}]}
    with pytest.raises(ValueError):
        detect_characters(text, library, Invalid(), AISettings())
    assert listed_characters(library)[0].identifier == result[0].identifier
    cancel = Event(); cancel.set()
    with pytest.raises(CharactersCanceled):
        detect_characters(text, library, Provider(), AISettings(), cancel=cancel)
    assert len(listed_characters(library)) == 1


def test_panel_preview_and_folder_selection(tmp_path, monkeypatch):
    app = QApplication.instance() or QApplication([])
    library = VisualLibrary(create_project(tmp_path, 'Preview').root)
    entity = detect_characters('Mara enters.\n\nShe leaves.', library, Provider(), AISettings())[0]
    folder = tmp_path / 'refs'; folder.mkdir()
    path = folder / 'portrait.png'
    image = QImage(32, 48, QImage.Format_RGB32); image.fill(0xff5533); image.save(str(path))
    (folder / 'ignore.txt').write_text('not an image')
    asset = library.import_image(path)
    library.attach_image(EntityReference(entity_id=entity.identifier, asset_id=asset.identifier))
    panel = CharactersPanel(); panel.set_library(library)
    assert panel.list_widget.count() == 1
    assert panel.preview.pixmap() and not panel.preview.pixmap().isNull()
    received = []
    panel.import_requested.connect(lambda identifier, paths: received.append((identifier, paths)))
    monkeypatch.setattr('prose_to_video.gui.characters_panel.QFileDialog.getExistingDirectory', lambda *args: str(folder))
    panel._choose_folder()
    assert received == [(entity.identifier, [path])]
    panel.fields['appearance_description'].setPlainText('')
    result = detect_characters('Mara enters.\n\nShe leaves.', library, Provider(), AISettings())
    assert result[0].appearance_description == ''
    assert library.references(entity.identifier)[0].asset_id == asset.identifier
    panel.set_library(None)
    assert not panel.file_button.isEnabled()
    panel.close()


def test_attributes_autosave_without_resetting_editor_or_changing_other_characters(tmp_path):
    from PySide6.QtGui import QTextCursor
    from PySide6.QtWidgets import QPushButton
    from prose_to_video.models.visual_reference import VisualEntity

    app = QApplication.instance() or QApplication([])
    library = VisualLibrary(create_project(tmp_path, 'Automatic saving').root)
    for name, appearance in [('Ada', 'Black hair'), ('Mara', 'Red hair')]:
        library.save_entity(VisualEntity(name=name, entity_type='Character',
            character_paragraphs=[1, 2], appearance_description=appearance))
    panel = CharactersPanel()
    audit_before = library.audit_log()
    panel.set_library(library)
    assert library.audit_log() == audit_before
    assert not any(button.text() == 'Save Visual Attributes' for button in panel.findChildren(QPushButton))
    selected = panel.current_character()
    editor = panel.fields['appearance_description']
    cursor = editor.textCursor()
    cursor.movePosition(QTextCursor.Start)
    editor.setTextCursor(cursor)
    editor.insertPlainText('Curly ')
    reopened = VisualLibrary(library.root)
    saved = reopened.entity(selected.identifier)
    assert saved.appearance_description == 'Curly Black hair'
    assert saved.character_edited_fields == ['appearance_description']
    assert editor.textCursor().position() == len('Curly ')
    assert editor.document().isUndoAvailable()
    editor.undo()
    assert reopened.entity(selected.identifier).appearance_description == 'Black hair'
    editor.redo()
    panel.list_widget.setCurrentRow(1)
    other = panel.current_character()
    assert editor.toPlainText() == 'Red hair'
    assert reopened.entity(other.identifier).character_edited_fields == []
    editor.clear()
    panel.list_widget.setCurrentRow(0)
    assert editor.toPlainText() == 'Curly Black hair'
    assert reopened.entity(other.identifier).appearance_description == ''
    audit_after = library.audit_log()
    panel.refresh()
    panel.set_library(None)
    assert library.audit_log() == audit_after
    panel.set_library(reopened)
    assert editor.toPlainText() == 'Curly Black hair'
    panel.close()


def test_remove_selected_character(tmp_path):
    from prose_to_video.models.visual_reference import VisualEntity

    app = QApplication.instance() or QApplication([])
    library = VisualLibrary(create_project(tmp_path, 'Remove character').root)
    for name in ('Ada', 'Mara'):
        library.save_entity(VisualEntity(name=name, entity_type='Character', character_paragraphs=[1, 2]))
    panel = CharactersPanel()
    assert not panel.remove_button.isEnabled()
    panel.set_library(library)
    panel.list_widget.setCurrentRow(1)
    selected = panel.current_character()
    changes = []
    panel.changed.connect(lambda: changes.append(True))
    panel.set_busy(True)
    assert not panel.remove_button.isEnabled()
    panel._remove_character()
    assert library.entity(selected.identifier) is not None
    panel.set_busy(False)
    panel.remove_button.click()
    reopened = VisualLibrary(library.root)
    assert reopened.entity(selected.identifier) is None
    assert [e.name for e in reopened.entities()] == ['Ada']
    assert panel.current_character().name == 'Ada'
    assert changes == [True]
    panel.remove_button.click()
    assert reopened.entities() == []
    assert panel.list_widget.count() == 0
    assert panel.current_character() is None
    assert not panel.remove_button.isEnabled()
    assert not panel.fields['appearance_description'].toPlainText()
    panel.close()


def test_generate_button_and_background_import(tmp_path, monkeypatch):
    import time
    from PySide6.QtCore import QSettings
    from prose_to_video.gui import main_window
    from prose_to_video.storage.project_files import import_prose
    monkeypatch.setattr(main_window, 'QSettings', lambda *args: QSettings(str(tmp_path / 'settings.ini'), QSettings.IniFormat))
    monkeypatch.setattr(main_window, 'make_provider', lambda settings: Provider())
    app = QApplication.instance() or QApplication([])
    source = tmp_path / 'story.txt'; source.write_text('Mara arrives.\n\nShe leaves.')
    session = import_prose(create_project(tmp_path, 'UI'), source)
    window = main_window.MainWindow()
    window._set_session(session, load_prose=False)
    panel = window.characters_panel
    panel.generate_button.click()
    def finish():
        deadline = time.monotonic() + 5
        while window._task is not None and time.monotonic() < deadline:
            app.processEvents(); time.sleep(.01)
        assert window._task is None
    finish()
    assert panel.list_widget.count() == 1
    assert panel.generate_button.isEnabled()
    path = tmp_path / 'reference.png'
    image = QImage(20, 30, QImage.Format_RGB32); image.fill(0xff5533); image.save(str(path))
    identifier = panel.current_character().identifier
    window._import_character_images(identifier, [path, tmp_path / 'missing.png'])
    finish()
    assert panel.gallery.count() == 1
    assert 'missing.png' in panel.status.text()
    window._import_character_images(identifier, [path])
    finish()
    assert panel.gallery.count() == 1
    chosen = tmp_path / 'chosen.png'
    image.fill(0xffabcdef); image.save(str(chosen))
    original_bytes = chosen.read_bytes()
    monkeypatch.setattr('prose_to_video.gui.characters_panel.QFileDialog.getOpenFileName',
                        lambda *args: (str(chosen), 'Images (*.png)'))
    panel.file_button.click()
    finish()
    assert panel.gallery.count() == 2
    from PySide6.QtCore import Qt
    saved_path = session.root / panel.gallery.currentItem().data(Qt.UserRole)
    assert saved_path.parent == session.root / 'references'
    assert saved_path.read_bytes() == original_bytes
    assert str(saved_path.parent) in panel.status.text()
    chosen.unlink()
    reopened = VisualLibrary(session.root)
    assert any(reopened.asset(ref.asset_id).path == str(saved_path.relative_to(session.root))
               for ref in reopened.references(identifier))
    assert saved_path.read_bytes() == original_bytes
    panel.set_library(reopened)
    panel.gallery.setCurrentRow(1)
    assert not panel.preview.pixmap().isNull()
    window.close()


def test_incomplete_output_retries_smaller_preserving_ids(tmp_path):
    from prose_to_video.pipeline.providers import AIResponseError
    library = VisualLibrary(create_project(tmp_path, 'Retry').root)
    class Truncated(Provider):
        calls = []
        def detect_characters(self, payload):
            data = json.loads(payload)
            self.calls.append(data)
            if len(data['paragraphs']) > 1:
                raise AIResponseError('token limit', retry_smaller=True)
            return {'characters': [{'name': 'Mara', 'paragraph_ids': [data['paragraphs'][0]['id']]}]}
    provider = Truncated()
    result = detect_characters('Mara waves.\n\nShe leaves.', library, provider, AISettings())
    assert result[0].character_paragraphs == [1, 2]
    assert len(provider.calls) == 3
    assert provider.calls[2]['preceding_context_only'] == 'Mara waves.'
    assert provider.calls[2]['known_characters'][0]['name'] == 'Mara'


def test_repeated_truncation_is_bounded_and_preserves_catalog(tmp_path):
    from prose_to_video.pipeline.providers import AIResponseError
    library = VisualLibrary(create_project(tmp_path, 'Fail').root)
    old = detect_characters('Mara waves.\n\nShe leaves.', library, Provider(), AISettings())[0]
    class AlwaysTruncated(Provider):
        calls = 0
        def detect_characters(self, payload):
            self.calls += 1
            raise AIResponseError('token limit', retry_smaller=True)
    provider = AlwaysTruncated()
    with pytest.raises(AIResponseError):
        detect_characters('Mara ' * 3000, library, provider, AISettings())
    assert provider.calls == 4
    assert listed_characters(library)[0].identifier == old.identifier


def test_split_single_paragraph_still_counts_once(tmp_path):
    from prose_to_video.pipeline.providers import AIResponseError
    library = VisualLibrary(create_project(tmp_path, 'Single').root)
    class Truncated(Provider):
        calls = 0
        def detect_characters(self, payload):
            self.calls += 1
            if self.calls == 1:
                raise AIResponseError('token limit', retry_smaller=True)
            data = json.loads(payload)
            return {'characters': [{'name': 'Mara', 'paragraph_ids': [data['paragraphs'][0]['id']]}]}
    assert detect_characters('Mara ' * 300, library, Truncated(), AISettings()) == []


def test_line_separated_prose_and_soft_wrapped_paragraphs():
    from prose_to_video.pipeline.character_detection import character_paragraphs
    assert character_paragraphs('Mara enters.\r\nShe sits.\r\nHer hands shake.') == [
        'Mara enters.', 'She sits.', 'Her hands shake.']
    assert character_paragraphs('Mara enters and\nlooks around.\n\nShe sits.') == [
        'Mara enters and\nlooks around.', 'She sits.']


@pytest.mark.parametrize('separator', ['\n', '\n\n'])
def test_pronoun_pass_recovers_character_named_only_once(tmp_path, separator):
    library = VisualLibrary(create_project(tmp_path, 'Pronouns').root)
    class PronounProvider(Provider):
        def detect_characters(self, payload):
            # Reproduce the failure: extraction sees the named paragraph only.
            return {'characters': [
                {'name': 'Mara', 'paragraph_ids': [1], 'aliases': ['she'], 'appearance_description': 'Red hair'},
                {'name': 'Courier', 'paragraph_ids': [1]},
            ]}
        def resolve_character_mentions(self, payload):
            data = json.loads(payload)
            assert [p['id'] for p in data['paragraphs']] == [1, 2, 3]
            assert data['known_characters'][0]['name'] == 'Mara'
            assert data['known_characters'][0]['aliases'] == []
            assert data['known_characters'][0]['anchor_excerpt'].startswith('Mara')
            return {'characters': [{'name': 'Mara', 'paragraph_ids': [1, 2, 3]}]}
    result = detect_characters(separator.join(['Mara greets a courier.', 'She sits.', 'Her hands shake.']),
                               library, PronounProvider(), AISettings())
    assert [e.name for e in result] == ['Mara']
    assert result[0].character_paragraphs == [1, 2, 3]
    assert result[0].appearance_description == 'Red hair'


def test_pronoun_resolution_crosses_chunks_and_does_not_merge_people(tmp_path):
    library = VisualLibrary(create_project(tmp_path, 'Continuity').root)
    text = 'Mara walks. ' + 'a' * 980 + '\n\nShe sits.\n\nNora arrives.\n\nShe waves.'
    class PronounProvider(Provider):
        resolve_calls = []
        def detect_characters(self, payload):
            data = json.loads(payload)
            ids = {p['id'] for p in data['paragraphs']}
            return {'characters': [record for record in [
                {'name': 'Mara', 'aliases': ['she'], 'paragraph_ids': [1]},
                {'name': 'Nora', 'aliases': ['she'], 'paragraph_ids': [3]}]
                if record['paragraph_ids'][0] in ids]}
        def resolve_character_mentions(self, payload):
            data = json.loads(payload); self.resolve_calls.append(data)
            ids = {p['id'] for p in data['paragraphs']}
            if 2 in ids:
                mara = next(c for c in data['known_characters'] if c['name'] == 'Mara')
                assert mara['anchor_paragraph_id'] == 1
                assert 'Mara walks' in mara['anchor_excerpt']
            return {'characters': [{'name': name, 'paragraph_ids': sorted(ids & appearances)}
                    for name, appearances in [('Mara', {1, 2}), ('Nora', {3, 4})]]}
    provider = PronounProvider()
    result = detect_characters(text, library, provider, AISettings(chunk_chars=1000, overlap_chars=0))
    assert {e.name: e.character_paragraphs for e in result} == {'Mara': [1, 2], 'Nora': [3, 4]}
    assert len(provider.resolve_calls) == 2


def test_ambiguous_pronouns_do_not_count_as_appearances(tmp_path):
    library = VisualLibrary(create_project(tmp_path, 'Ambiguous').root)
    class Ambiguous(Provider):
        def detect_characters(self, payload):
            return {'characters': [{'name': name, 'paragraph_ids': [1]} for name in ['Mara', 'Nora']]}
        def resolve_character_mentions(self, payload):
            # No unambiguous antecedent: neither identity acquires paragraph 2.
            return {'characters': []}
    assert detect_characters('Mara and Nora stand together.\n\nShe leaves.', library, Ambiguous(), AISettings()) == []


def test_pronoun_resolution_failure_preserves_saved_catalog(tmp_path):
    library = VisualLibrary(create_project(tmp_path, 'Preserve').root)
    original = detect_characters('Mara arrives.\n\nShe leaves.', library, Provider(), AISettings())[0]
    class InvalidMentions(Provider):
        def resolve_character_mentions(self, payload):
            return {'characters': [{'name': 'Mara', 'paragraph_ids': [999]}]}
    with pytest.raises(ValueError, match='unknown character or paragraph'):
        detect_characters('Mara arrives.\n\nShe leaves.', library, InvalidMentions(), AISettings())
    assert listed_characters(library)[0].identifier == original.identifier


def test_pronoun_resolution_retries_keep_original_ids(tmp_path):
    from prose_to_video.pipeline.providers import AIResponseError
    library = VisualLibrary(create_project(tmp_path, 'RetryPronouns').root)
    class Retry(Provider):
        calls = 0
        def detect_characters(self, payload):
            return {'characters': [{'name': 'Mara', 'paragraph_ids': [1]}]}
        def resolve_character_mentions(self, payload):
            self.calls += 1
            data = json.loads(payload)
            if self.calls == 1:
                raise AIResponseError('token limit', retry_smaller=True)
            return {'characters': [{'name': 'Mara', 'paragraph_ids': [p['id'] for p in data['paragraphs']]}]}
    provider = Retry()
    result = detect_characters('Mara enters.\n\nShe sits.', library, provider, AISettings())
    assert result[0].character_paragraphs == [1, 2]
    assert provider.calls == 3


def test_generate_selected_image_button_saves_edits_and_refreshes_gallery(tmp_path, monkeypatch):
    import time
    from PySide6.QtCore import QSettings
    from prose_to_video.gui import main_window
    from prose_to_video.models.image_job import ProviderResult, ProviderImage
    from prose_to_video.models.visual_reference import VisualEntity
    monkeypatch.setattr(main_window, 'QSettings', lambda *args: QSettings(str(tmp_path / 'ui.ini'), QSettings.IniFormat))
    app = QApplication.instance() or QApplication([])
    session = create_project(tmp_path, 'Generate reference')
    library = VisualLibrary(session.root)
    for name in ('Ada', 'Mara'):
        library.save_entity(VisualEntity(name=name, entity_type='Character', character_paragraphs=[1, 2]))
    path = tmp_path / 'generated.png'
    image = QImage(32, 48, QImage.Format_RGB32); image.fill(0xff5533); image.save(str(path))
    requests = []
    class Images:
        def validate(self, request): pass
        def submit(self, request):
            requests.append(request)
            return ProviderResult(status='Completed', images=[ProviderImage(data=path.read_bytes(), model='fake')])
    monkeypatch.setattr(main_window, 'make_image_provider', lambda *args: Images())
    errors = []
    window = main_window.MainWindow()
    monkeypatch.setattr(window, '_show_error', lambda *args: errors.append(args))
    window._set_session(session, load_prose=False)
    panel = window.characters_panel
    panel.list_widget.setCurrentRow(1)
    selected = panel.current_character()
    panel.fields['clothing_accessories'].setPlainText('Tall blue hat')
    panel.generate_image_button.click()
    assert not panel.generate_image_button.isEnabled()
    deadline = time.monotonic() + 5
    while window._task is not None and time.monotonic() < deadline:
        app.processEvents(); time.sleep(.01)
    assert window._task is None and not errors
    assert len(requests) == 1 and selected.name in requests[0].prompt
    assert 'Tall blue hat' in requests[0].prompt
    assert library.entity(selected.identifier).clothing_accessories == 'Tall blue hat'
    assert len(library.references(selected.identifier)) == 1
    other = next(e for e in library.entities('Character') if e.identifier != selected.identifier)
    assert not library.references(other.identifier)
    assert panel.gallery.count() == 1 and not panel.preview.pixmap().isNull()
    assert 'generated' in panel.status.text() and panel.generate_image_button.isEnabled()
    panel.set_library(None)
    assert not panel.generate_image_button.isEnabled()
    window.close()


def test_delete_displayed_reference_preserves_other_images_and_edits(tmp_path):
    from PySide6.QtCore import Qt
    from prose_to_video.models.visual_reference import VisualEntity

    app = QApplication.instance() or QApplication([])
    library = VisualLibrary(create_project(tmp_path, 'Delete reference').root)
    character = library.save_entity(VisualEntity(
        name='Mara', entity_type='Character', character_paragraphs=[1, 2]))
    other = library.save_entity(VisualEntity(
        name='Ada', entity_type='Character', character_paragraphs=[1, 2]))
    assets = []
    for index, color in enumerate((0xff112233, 0xff445566, 0xff778899)):
        path = tmp_path / f'reference-{index}.png'
        image = QImage(32, 48, QImage.Format_RGB32)
        image.fill(color)
        image.save(str(path))
        asset = library.import_image(path)
        assets.append(asset)
        library.attach_image(EntityReference(entity_id=character.identifier, asset_id=asset.identifier))
    panel = CharactersPanel()
    panel.set_library(library)
    panel.list_widget.setCurrentRow(next(i for i, e in enumerate(panel.characters) if e.identifier == character.identifier))
    panel.gallery.setCurrentRow(1)
    deleted_id = panel.gallery.currentItem().data(Qt.UserRole + 1)
    library.attach_image(EntityReference(entity_id=other.identifier, asset_id=deleted_id))
    panel.fields['appearance_description'].setPlainText('Unsaved details')
    panel.set_busy(True)
    assert not panel.delete_reference.isEnabled()
    panel._delete_reference()
    assert len(library.references(character.identifier)) == 3
    panel.set_busy(False)
    panel.delete_reference.click()
    reopened = VisualLibrary(library.root)
    assert {r.asset_id for r in reopened.references(character.identifier)} == {a.identifier for a in assets} - {deleted_id}
    assert reopened.references(other.identifier)[0].asset_id == deleted_id
    assert (library.root / library.asset(deleted_id).path).exists()
    assert panel.fields['appearance_description'].toPlainText() == 'Unsaved details'
    assert panel.gallery.count() == 2
    assert not panel.preview.pixmap().isNull()
    assert not panel.reference_navigation.isHidden()
    panel.gallery.setCurrentRow(1)
    panel.delete_reference.click()
    assert panel.gallery.count() == 1 and panel.gallery.currentRow() == 0
    assert panel.reference_navigation.isHidden()
    assert not panel.delete_reference.isEnabled()
    panel._delete_reference()
    assert len(reopened.references(character.identifier)) == 1
    panel.close()
