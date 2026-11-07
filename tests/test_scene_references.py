import os

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import pytest
from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtWidgets import QApplication

from prose_to_video.gui.scenes_panel import ScenesPanel
from prose_to_video.models.scene import Scene, source_hash
from prose_to_video.models.visual_reference import VisualEntity
from prose_to_video.pipeline.scene_references import scene_references
from prose_to_video.storage.project_files import create_project
from prose_to_video.storage.visual_library import VisualLibrary


@pytest.mark.parametrize('separator', ['\n\n', '\r\n\r\n', '\r\r', '\n'])
def test_catalog_references_follow_paragraph_appearances(tmp_path, separator):
    library = VisualLibrary(create_project(tmp_path, 'Story').root)
    paragraphs = ['Mara meets Jo.', 'She leaves.', 'She leaves.']
    text = separator.join(paragraphs)
    split = text.rindex('She leaves.')
    scenes = [Scene(sequence=1, start=0, end=split, text=text[:split]),
              Scene(sequence=2, start=split, end=len(text), text=text[split:])]
    library.db.replace_scenes(text, scenes)
    mara = VisualEntity(name='Mara', entity_type='Character',
                        character_paragraphs=[1, 2], source_hash=source_hash(text))
    jo = VisualEntity(name='Jo', entity_type='Character',
                      character_paragraphs=[1, 3], source_hash=source_hash(text))
    stale = VisualEntity(name='Old character', entity_type='Character',
                         character_paragraphs=[1, 3], source_hash='old')
    incidental = VisualEntity(name='Courier', entity_type='Character',
                              character_paragraphs=[3], source_hash=source_hash(text))
    place = VisualEntity(name='Station', entity_type='Location', place_order=1)
    for entity in (mara, jo, stale, incidental, place):
        library.save_entity(entity)
    library.relate_scene(place.identifier, scenes[1].identifier)
    refs = scene_references(library, text, scenes)
    assert refs[scenes[0].identifier] == ([], ['Jo', 'Mara'])
    assert refs[scenes[1].identifier] == (['Station'], ['Jo'])

    application = QApplication.instance() or QApplication([])
    panel = ScenesPanel()
    panel.set_references(refs)
    panel.set_scenes(scenes)
    assert panel.persistent_characters_label.text() == 'Jo\nMara'
    panel.list_widget.setCurrentRow(1)
    assert panel.place_label.text() == 'Station'
    assert panel.persistent_characters_label.text() == 'Jo'
    panel.fields['title'].setText('Unsaved edit')
    library.save_entity(place.model_copy(update={'name': 'Old Station'}))
    panel.set_references(scene_references(library, text, scenes))
    assert panel.place_label.text() == 'Old Station'
    assert panel.fields['title'].text() == 'Unsaved edit'
    panel.set_scenes([])
    assert panel.place_label.text() == 'No scene selected'
    assert panel.persistent_characters_label.text() == 'No scene selected'
    panel.close()
    panel.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
