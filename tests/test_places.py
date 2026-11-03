"""Prose-first places, reusable setting descriptions, and source-based linking."""
import json
import os
from threading import Event

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
import pytest
from PySide6.QtWidgets import QApplication
from prose_to_video.gui.places_panel import PlacesPanel
from prose_to_video.models.scene import Scene
from prose_to_video.pipeline.place_detection import (detect_places, link_places, listed_places,
    PlaceProposals, PlaceProposal, PlaceDescription, PlacesCanceled)
from prose_to_video.pipeline.providers import AISettings
from prose_to_video.storage.project_files import create_project
from prose_to_video.storage.visual_library import VisualLibrary

TEXT = "Marvin's street in 2020 has brick houses.\nInside the shoe factory, machines clatter.\nIn another room, leather is stacked.\nMarvin's street in 2030 has empty lots."

class Provider:
    def __init__(self): self.events = []
    def detect_places(self, payload):
        data = json.loads(payload)
        self.events.append(('detect', data))
        assert 'scenes' not in data
        return PlaceProposals(places=[
            PlaceProposal(name="Marvin's Street 2030", description='Empty lots.', passage_ids=[4]),
            PlaceProposal(name='The Shoe Factory', description='Clattering machines; another room holds stacked leather.', passage_ids=[2, 3]),
            PlaceProposal(name="Marvin's Street 2020", description='Brick houses.', passage_ids=[1]),
        ])
    def describe_place(self, payload):
        data = json.loads(payload)
        self.events.append(('describe', data))
        return PlaceDescription(description=data['source_details'])


def setup_places(tmp_path):
    session = create_project(tmp_path, 'Places')
    return session, VisualLibrary(session.root)


def save_scenes(session):
    starts = [0, TEXT.index('Inside'), TEXT.index('In another'), TEXT.index("Marvin's street in 2030")]
    scenes = [Scene(sequence=i + 1, start=start, end=end, text=TEXT[start:end],
                    location='A deliberately unrelated scene label')
              for i, (start, end) in enumerate(zip(starts, starts[1:] + [len(TEXT)]))]
    session.database.replace_scenes(TEXT, scenes)
    return scenes


def test_prose_first_order_periods_and_later_links(tmp_path):
    session, library = setup_places(tmp_path)
    provider = Provider()
    places = detect_places(TEXT, library, provider, AISettings())
    assert [p.name for p in places] == ["Marvin's Street 2020", 'The Shoe Factory', "Marvin's Street 2030"]
    assert [event[0] for event in provider.events] == ['detect', 'describe', 'describe', 'describe']
    assert all(not library.scenes_for(p.identifier) for p in places)
    scenes = save_scenes(session)
    link_places(library)
    assert set(library.scenes_for(places[1].identifier)) == {s.identifier for s in scenes[1:3]}
    assert library.scenes_for(places[2].identifier) == [scenes[3].identifier]
    reopened = listed_places(VisualLibrary(session.root))
    assert [p.name for p in reopened] == [p.name for p in places]
    assert 'leather' in reopened[1].description


def test_reanalysis_preserves_edits_and_source_changes_remove_links(tmp_path):
    session, library = setup_places(tmp_path)
    scenes = save_scenes(session)
    places = detect_places(TEXT, library, Provider(), AISettings())
    library.save_entity(places[1].model_copy(update={'description': 'Edited setting.'}))
    detect_places(TEXT, library, Provider(), AISettings())
    assert listed_places(library)[1].description == 'Edited setting.'
    new = TEXT + ' New prose.'
    session.database.replace_scenes(new, [Scene(sequence=1, start=0, end=len(new), text=new)])
    link_places(library)
    assert all(not library.scenes_for(p.identifier) for p in places)


def test_failed_canceled_and_stale_runs_preserve_catalog(tmp_path):
    _, library = setup_places(tmp_path)
    detect_places(TEXT, library, Provider(), AISettings())
    before = [p.model_dump() for p in listed_places(library)]
    class Bad(Provider):
        def describe_place(self, payload): raise ValueError('failed')
    with pytest.raises(ValueError, match='failed'):
        detect_places(TEXT, library, Bad(), AISettings())
    cancel = Event(); cancel.set()
    with pytest.raises(PlacesCanceled):
        detect_places(TEXT, library, Provider(), AISettings(), cancel=cancel)
    class Invalid(Provider):
        def detect_places(self, payload):
            result = super().detect_places(payload)
            result.places[0].passage_ids = [999]
            return result
    with pytest.raises(ValueError, match='unknown'):
        detect_places(TEXT, library, Invalid(), AISettings())
    assert [p.model_dump() for p in listed_places(library)] == before


def test_place_editor_and_navigation(tmp_path):
    app = QApplication.instance() or QApplication([])
    session, library = setup_places(tmp_path)
    scenes = save_scenes(session)
    detect_places(TEXT, library, Provider(), AISettings())
    panel = PlacesPanel(); panel.set_library(library)
    panel.list_widget.setCurrentRow(1)
    assert panel.scene_list.count() == 2
    opened = []; panel.scene_requested.connect(opened.append)
    panel.open_button.click()
    assert opened == [scenes[1].identifier]
    panel.name_edit.setText('Shoe Works')
    panel.description_edit.setPlainText('Edited factory.')
    panel.save_button.click()
    detect_places(TEXT, library, Provider(), AISettings())
    assert listed_places(library)[1].name == 'Shoe Works'
    assert listed_places(library)[1].description == 'Edited factory.'
    panel.set_library(None)
    assert panel.list_widget.count() == panel.scene_list.count() == 0
    assert not panel.save_button.isEnabled()


def test_chunked_recurring_place_merges_details_and_covers_source(tmp_path):
    _, library = setup_places(tmp_path)
    text = ('A long description of brick factory walls. ' * 20 + '\n') * 5
    class ChunkProvider:
        def __init__(self): self.calls = 0
        def detect_places(self, payload):
            self.calls += 1
            data = json.loads(payload)
            return PlaceProposals(places=[PlaceProposal(name='The Shoe Factory',
                description=f'Detail {self.calls}', passage_ids=[p['id'] for p in data['passages']])])
        def describe_place(self, payload):
            data = json.loads(payload)
            return PlaceDescription(description=data['accumulated_description'] + '\n' + data['source_details'])
    provider = ChunkProvider()
    places = detect_places(text, library, provider, AISettings(chunk_chars=1800, overlap_chars=200))
    assert provider.calls > 1
    assert len(places) == 1
    assert all(f'Detail {n}' in places[0].description for n in range(1, provider.calls + 1))
    ranges = places[0].place_ranges
    assert ranges[0][0] == 0 and ranges[-1][1] == len(text)
    assert all(left[1] == right[0] for left, right in zip(ranges, ranges[1:]))
