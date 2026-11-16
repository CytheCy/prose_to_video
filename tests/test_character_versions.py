"""Character versions share a person while owning separate part appearances."""
from types import SimpleNamespace

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QDialog

from prose_to_video.gui.character_versions_dialog import CharacterVersionsDialog
from prose_to_video.models.scene import Scene
from prose_to_video.pipeline.stills_workflow import (
    Identity, Workflow, assign_character_versions, listed_identities, load_workflow, dependency)
from prose_to_video.storage.project_files import create_project


def story():
    bob = Identity(identifier='bob', kind='Character', name='Bob', description='brown coat',
                   part_ids=['p1', 'p2', 'p3', 'p4'], references=['references/bob.png'])
    state = Workflow(catalog_hash='hash', selected_part_ids=['p1', 'p2', 'p3', 'p4'], catalogs=[bob])
    sections = [SimpleNamespace(identifier=f'p{i+1}', body_start=i*10, end=(i+1)*10) for i in range(5)]
    state.scenes = [Scene(sequence=i+1, start=i*10, end=(i+1)*10, text='Bob waits.',
                          identity_links=['bob'], characters=['Bob']) for i in range(4)]
    for scene in state.scenes:
        scene.prompt_dependency = dependency(state, scene)
    return state, bob, sections


def test_dialog_assigns_only_original_parts_and_keeps_edits_on_switch():
    app = QApplication.instance() or QApplication([])
    state, bob, _ = story()
    dialog = CharacterVersionsDialog(bob, state.catalogs, [(f'p{i}', f'Part {i}') for i in range(1, 6)])
    assert dialog.part_list.count() == 4
    dialog.name_edit.selectAll()
    from PySide6.QtTest import QTest
    QTest.keyClicks(dialog.name_edit, 'Bob young')
    dialog.add_button.click()
    QTest.keyClicks(dialog.name_edit, 'Bob old')
    assert dialog.versions[1].references == []
    for i in (2, 3):
        dialog.part_list.item(i).setCheckState(Qt.Checked)
    dialog.version_list.setCurrentRow(0)
    assert dialog.name_edit.text() == 'Bob young'
    assert [dialog.part_list.item(i).checkState() for i in range(4)] == [Qt.Checked]*2 + [Qt.Unchecked]*2
    dialog.add_button.click()
    QTest.keyClicks(dialog.name_edit, 'Bob ancient')
    dialog.part_list.item(3).setCheckState(Qt.Checked)
    assert [c.part_ids for c in dialog.versions] == [['p1', 'p2'], ['p3'], ['p4']]
    dialog.accept()
    assert dialog.result() == QDialog.Accepted
    # Canceling edits has never mutated the original workflow.
    assert bob.name == 'Bob' and bob.part_ids == ['p1', 'p2', 'p3', 'p4']
    dialog.close()


def test_versions_persist_relink_scenes_and_reopen_from_any_member(tmp_path):
    app = QApplication.instance() or QApplication([])
    state, bob, sections = story()
    young = bob.model_copy(update={'name': 'Bob young', 'part_ids': ['p1', 'p2']})
    old = bob.model_copy(update={'identifier': 'old', 'name': 'Bob old', 'part_ids': ['p3', 'p4'], 'references': []})
    updated = assign_character_versions(state, 'bob', [young, old], sections)
    assert [s.identity_links for s in updated.scenes] == [['bob'], ['bob'], ['old'], ['old']]
    assert [s.characters for s in updated.scenes] == [['Bob young']]*2 + [['Bob old']]*2
    assert all(s.prompt_dependency != dependency(updated, s) for s in updated.scenes)
    assert state.scenes[2].identity_links == ['bob']
    session = create_project(tmp_path, 'Versions')
    updated.save(session.root)
    restored = load_workflow(session)
    restored.selected_part_ids = ['p1']
    assert [c.name for c in listed_identities(restored, 'Character')] == ['Bob young', 'Bob old']
    old = restored.catalogs[1]
    dialog = CharacterVersionsDialog(old, restored.catalogs, [(f'p{i}', f'Part {i}') for i in range(1, 6)])
    assert len(dialog.versions) == 2 and dialog.part_list.count() == 4
    assert dialog.name_edit.text() == 'Bob old'
    assert restored.catalogs[0].references == ['references/bob.png']
    assert old.references == [] and old.aliases == ['Bob']
    dialog.reject()


def test_invalid_assignments_do_not_mutate_workflow():
    state, bob, sections = story()
    before = state.model_dump()
    for parts in (['p1'], ['p1', 'p2', 'p3', 'p4', 'p5']):
        with pytest.raises(ValueError, match='exactly one'):
            assign_character_versions(state, bob.identifier, [bob.model_copy(update={'part_ids': parts})], sections)
    assert state.model_dump() == before


def test_character_tab_button_saves_and_refreshes_all_versions(tmp_path, monkeypatch):
    from PySide6.QtCore import QSettings
    from prose_to_video.gui import stills_window
    from prose_to_video.pipeline.manuscript import parse_manuscript
    from prose_to_video.storage.project_files import import_prose
    monkeypatch.setattr(stills_window, 'QSettings', lambda *args: QSettings(str(tmp_path / 'ui.ini'), QSettings.IniFormat))
    app = QApplication.instance() or QApplication([])
    session = create_project(tmp_path, 'Tab versions')
    text = '\n\n'.join(f'# Part {i}\n\nBob waits.' for i in range(1, 6))
    source = tmp_path / 'story.md'
    source.write_text(text)
    session = import_prose(session, source)
    # Use imported prose offsets/IDs, including any normalization at import.
    from prose_to_video.utils.prose import read_prose
    text = read_prose(session.project.resolved_prose(session.root))
    parts = parse_manuscript(text)
    ids = [p.identifier for p in parts]
    bob = Identity(identifier='bob', kind='Character', name='Bob', description='', part_ids=ids[:4])
    state = Workflow(catalog_hash='hash', selected_part_ids=ids, catalogs=[bob])
    state.save(session.root)
    window = stills_window.MainWindow()
    assert window.add_character_version_button.text() == 'Add Version'
    assert not window.add_character_version_button.isEnabled()
    window.session = session
    window.state = state
    window.refresh()
    assert window.add_character_version_button.isEnabled()
    def edit(dialog):
        assert dialog.part_list.count() == 4
        dialog.rename_version('Bob young')
        dialog.add_variant()
        dialog.rename_version('Bob old')
        for i in (2, 3):
            dialog.part_list.item(i).setCheckState(Qt.Checked)
        dialog.accept()
        return dialog.result()
    monkeypatch.setattr(CharacterVersionsDialog, 'exec', edit)
    window.add_character_version_button.click()
    assert [window.catalog_lists['Character'].item(i).text() for i in range(2)] == ['Bob young', 'Bob old']
    restored = load_workflow(session)
    assert [c.part_ids for c in restored.catalogs] == [ids[:2], ids[2:4]]
    window.close()
