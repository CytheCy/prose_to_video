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
    panel._save()
    result = detect_characters('Mara enters.\n\nShe leaves.', library, Provider(), AISettings())
    assert result[0].appearance_description == ''
    assert library.references(entity.identifier)[0].asset_id == asset.identifier
    panel.set_library(None)
    assert not panel.file_button.isEnabled()
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
