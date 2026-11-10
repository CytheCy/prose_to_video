"""Styles are exclusive preferences applied only to outgoing still requests."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import pytest
from PySide6.QtCore import QSettings, QTimer
from PySide6.QtWidgets import QApplication, QDialog

from prose_to_video.gui.stills_settings import StillsSettingsWindow
from prose_to_video.gui.visual_style_settings import AddVisualStyleDialog
from prose_to_video.storage import visual_style_preferences as preferences
from prose_to_video.storage.visual_style_preferences import VisualStyle, VisualStylePreferences
from test_generation_ai_settings import window


@pytest.fixture(autouse=True)
def isolated_styles(tmp_path, monkeypatch):
    monkeypatch.setattr(preferences, 'QSettings',
        lambda *args: QSettings(str(tmp_path / 'styles.ini'), QSettings.IniFormat))


def select_style(prompt):
    style = VisualStyle(name=prompt, prompt=prompt)
    preferences.save_visual_styles(VisualStylePreferences(styles=[style], selected_id=style.identifier))


def test_add_select_save_reopen_and_cancel(window, monkeypatch):
    dialog = StillsSettingsWindow(window)
    assert dialog.tabs.tabText(dialog.tabs.count() - 1) == 'Visual Style'
    widget = dialog.visual_style
    assert [b.text() for b in widget.group.buttons()] == ['No visual style']
    for name, prompt in [('Ink', 'Fine ink lines'), ('Paint', 'Soft brush strokes')]:
        def fill(name=name, prompt=prompt):
            popup = QApplication.activeModalWidget()
            assert isinstance(popup, AddVisualStyleDialog)
            popup.name.setText(name)
            popup.prompt.setPlainText(prompt)
            popup._save()
        QTimer.singleShot(0, fill)
        widget.add_button.click()
    assert [b.text() for b in widget.group.buttons()] == ['Ink', 'Paint']
    assert [s.name for s in widget.values().styles] == ['Ink', 'Paint']
    assert widget.values().selected_prompt() == 'Soft brush strokes'
    assert sum(b.isChecked() for b in widget.group.buttons()) == 1
    ink = widget.group.buttons()[0]
    ink.click()
    assert widget.values().selected_prompt() == 'Fine ink lines'
    assert sum(b.isChecked() for b in widget.group.buttons()) == 1
    # Preferences are committed by Settings Save, not by the add popup.
    assert preferences.load_visual_styles().styles == []
    dialog._accept()
    assert dialog.result() == QDialog.Accepted
    reopened = StillsSettingsWindow(window)
    assert reopened.visual_style.values().selected_prompt() == 'Fine ink lines'
    assert [b.text() for b in reopened.visual_style.group.buttons()] == ['Ink', 'Paint']
    reopened.visual_style.group.buttons()[1].click()
    reopened.reject()
    assert preferences.load_visual_styles().selected_prompt() == 'Fine ink lines'
    reopened = StillsSettingsWindow(window)
    reopened.visual_style.group.buttons()[1].click()
    reopened._accept()
    assert preferences.load_visual_styles().selected_prompt() == 'Soft brush strokes'
    assert len(preferences.load_visual_styles().styles) == 2


def test_saved_styles_without_selection_choose_first(window):
    style = VisualStyle(name='Ink', prompt='Fine ink lines')
    preferences.save_visual_styles(VisualStylePreferences(styles=[style]))
    dialog = StillsSettingsWindow(window)
    widget = dialog.visual_style
    assert [b.text() for b in widget.group.buttons()] == ['Ink']
    assert widget.values().selected_prompt() == 'Fine ink lines'


def test_add_requires_name_and_prompt(window, monkeypatch):
    popup = AddVisualStyleDialog(window)
    warnings = []
    monkeypatch.setattr('prose_to_video.gui.visual_style_settings.QMessageBox.warning',
                        lambda *args: warnings.append(args))
    popup.name.setText('Ink')
    popup.prompt.setPlainText('   ')
    popup._save()
    assert warnings and popup.result() != QDialog.Accepted


def test_direct_stills_switch_styles_without_changing_scene_or_references(tmp_path):
    from test_stills_workflow import session_with_text
    from test_image_generation import png
    from prose_to_video.models.scene import Scene
    from prose_to_video.pipeline.stills_workflow import Workflow, Identity
    from prose_to_video.models.image_job import ImageCapabilities, ProviderResult, ProviderImage
    from prose_to_video.pipeline.stills_workflow import generate_still, generate_character_reference

    session = session_with_text(tmp_path, 'Mira turns.')
    state = Workflow()
    scene = Scene(sequence=1, start=0, end=11, text='Mira turns.', final_prompt='Mira turns',
                  negative_prompt='watermark')
    state.scenes = [scene]
    character = Identity(kind='Character', name='Mira', description='Green coat')
    state.catalogs = [character]
    original_scene = scene.model_dump()
    requests = []
    class Provider:
        name = 'mock'
        def capabilities(self, model): return ImageCapabilities()
        def validate(self, request): pass
        def submit(self, request):
            requests.append(request)
            return ProviderResult(status='Completed', images=[ProviderImage(data=png(), model='mock')])
    for style in ['Fine ink lines', 'Soft brush strokes', '']:
        select_style(style)
        generate_still(session, state, scene, Provider())
        expected = 'Mira turns\nAvoid: watermark' + ('\nStyle: ' + style if style else '')
        assert requests[-1].prompt == expected
        assert scene.model_dump() == original_scene
    select_style('THIS IS ONLY FOR STILLS')
    generate_character_reference(session, state, character, Provider())
    assert 'THIS IS ONLY FOR STILLS' not in requests[-1].prompt
    assert 'Canonical character reference for Mira' in requests[-1].prompt


def test_queued_and_rebuilt_requests_resolve_style_at_submission(tmp_path):
    from test_image_generation import fixture, FakeImageProvider
    from prose_to_video.models.image_job import ImageGenerationSettings
    from prose_to_video.pipeline.image_generation import ImageGenerator
    from prose_to_video.storage.generated_images import ImageStore

    session, scene, board, prompts = fixture(tmp_path)
    ImageStore(session.root).save_settings(ImageGenerationSettings(provider='Fake', model='fake-v1'))
    provider = FakeImageProvider()
    generator = ImageGenerator(session, provider)
    original = prompts.approved(board.shots[0].identifier).model_dump()
    select_style('Old style')
    job, request, _ = generator.enqueue(scene, board.shots[0], board)
    assert 'Style: Old style' not in request.prompt
    select_style('New style')
    generator.run(job, request)
    assert provider.requests[-1].prompt == request.prompt + '\nStyle: New style'
    assert 'Style: New style' not in request.prompt
    job, _, _ = generator.enqueue(scene, board.shots[0], board)
    rebuilt = generator.request_for_job(job)
    select_style('Third style')
    generator.run(job, rebuilt)
    assert provider.requests[-1].prompt == rebuilt.prompt + '\nStyle: Third style'
    assert prompts.approved(board.shots[0].identifier).model_dump() == original
