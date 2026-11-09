"""The active workspace saves independent text and image model choices."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import pytest
from PySide6.QtCore import QEvent, QSettings, QTimer
from PySide6.QtWidgets import QApplication, QDialog, QDialogButtonBox

from prose_to_video.gui import ai_settings, stills_window
from prose_to_video.gui.stills_settings import StillsSettingsWindow
from prose_to_video.pipeline.stills_workflow import load_workflow
from prose_to_video.storage import ai_preferences
from prose_to_video.storage.project_files import create_project


@pytest.fixture
def window(tmp_path, monkeypatch):
    app = QApplication.instance() or QApplication([])
    factory = lambda *args: QSettings(str(tmp_path / 'preferences.ini'), QSettings.IniFormat)
    for module in (ai_settings, ai_preferences, stills_window):
        monkeypatch.setattr(module, 'QSettings', factory)
    window = stills_window.MainWindow()
    yield window
    window.close()
    window.deleteLater()
    app.sendPostedEvents(None, QEvent.DeferredDelete)
    app.processEvents()


def edit_dialog(window, callback):
    errors = []
    def edit():
        dialog = QApplication.activeModalWidget()
        try:
            assert isinstance(dialog, StillsSettingsWindow)
            callback(dialog)
        except BaseException as exc:
            errors.append(exc)
        finally:
            if dialog.isVisible():
                dialog.reject()
    QTimer.singleShot(0, edit)
    window.ai_settings()
    if errors:
        raise errors[0]


@pytest.mark.parametrize('open_project', [False, True])
def test_model_choices_save_reopen_and_cancel(window, tmp_path, open_project):
    if open_project:
        window.load(create_project(tmp_path, 'Current'))
        window.state.image_settings.width = 1024
        window.state.image_settings.height = 768
        window.state.image_settings.workflow_path = 'keep.json'
    before = window.state.image_settings.model_dump()
    def save(dialog):
        dialog.general.text_model.setCurrentText('gpt-6.1-sol')
        dialog.image_model.setCurrentText('gpt-image-2.5-sunburst')
        dialog.findChild(QDialogButtonBox).button(QDialogButtonBox.Save).click()
        assert dialog.result() == QDialog.Accepted
    edit_dialog(window, save)
    assert ai_settings.load_ai_settings().model == 'gpt-6.1-sol'
    assert ai_preferences.load_tti_defaults()['model'] == 'gpt-image-2.5-sunburst'
    assert window.state.image_settings.model == 'gpt-image-2.5-sunburst'
    assert window.state.image_settings.model_dump(exclude={'model'}) == {k:v for k,v in before.items() if k != 'model'}
    if open_project:
        assert load_workflow(window.session).image_settings == window.state.image_settings
    new_project = create_project(tmp_path, 'New')
    assert load_workflow(new_project).image_settings.model == 'gpt-image-2.5-sunburst'
    def cancel(dialog):
        assert dialog.general.text_model.currentText() == 'gpt-6.1-sol'
        assert dialog.image_model.currentText() == 'gpt-image-2.5-sunburst'
        dialog.general.text_model.setCurrentText('custom-text-id')
        dialog.image_model.setCurrentText('custom-image-id')
        dialog.reject()
    edit_dialog(window, cancel)
    assert ai_settings.load_ai_settings().model == 'gpt-6.1-sol'
    assert window.state.image_settings.model == 'gpt-image-2.5-sunburst'


def test_provider_switch_preserves_models_and_empty_openai_stays_open(window, monkeypatch):
    warnings = []
    monkeypatch.setattr(ai_settings.QMessageBox, 'warning', lambda *args: warnings.append(args))
    def edit(dialog):
        dialog.image_model.setCurrentText('custom-image-id')
        dialog.image_provider.setCurrentText('ComfyUI')
        assert dialog.image_model.currentText() == ''
        assert dialog.image_model.count() == 0
        dialog.image_model.setCurrentText('workflow-model')
        dialog.image_provider.setCurrentText('OpenAI')
        assert dialog.image_model.currentText() == 'custom-image-id'
        dialog.image_model.setCurrentText('')
        dialog._accept()
        assert warnings
        assert dialog.isVisible()
        dialog.image_provider.setCurrentText('ComfyUI')
        assert dialog.image_model.currentText() == 'workflow-model'
        dialog.image_model.setCurrentText('')
        dialog._accept()
        assert dialog.result() == QDialog.Accepted
    edit_dialog(window, edit)
    assert ai_preferences.load_tti_defaults() == {'provider': 'ComfyUI', 'model': ''}


def test_start_settings_button_and_tabs(window):
    from PySide6.QtWidgets import QPushButton
    labels = [button.text() for button in window.start.findChildren(QPushButton)]
    assert 'Settings' in labels
    assert not {'AI Settings', 'Image Settings', 'Export Settings', 'Save Style / Density'} & set(labels)
    errors = []
    def inspect():
        dialog = QApplication.activeModalWidget()
        try:
            assert isinstance(dialog, StillsSettingsWindow)
            assert [dialog.tabs.tabText(i) for i in range(dialog.tabs.count())] == [
                'AI Settings', 'Image Settings', 'Export Settings', 'Pre-Stills']
            assert not dialog.style.isEnabled()
            assert not dialog.image_json.isEnabled()
            assert not dialog.export_json.isEnabled()
        except BaseException as exc:
            errors.append(exc)
        finally:
            dialog.reject()
    QTimer.singleShot(0, inspect)
    next(button for button in window.start.findChildren(QPushButton) if button.text() == 'Settings').click()
    if errors:
        raise errors[0]


def test_project_tabs_save_cancel_and_validation(window, tmp_path, monkeypatch):
    window.load(create_project(tmp_path, 'Settings'))
    warnings = []
    monkeypatch.setattr(ai_settings.QMessageBox, 'warning', lambda *args: warnings.append(args))
    before = window.state.model_dump()
    dialog = StillsSettingsWindow(window)
    dialog.style.setPlainText('Watercolor')
    dialog.density.setCurrentText('High')
    dialog.scene_detection.setCurrentText('Scene Per Paragraph')
    assert not dialog.density.isEnabled()
    images = window.state.image_settings.model_dump(exclude={'provider', 'model'})
    images['width'] = 1024
    import json
    dialog.image_json.setPlainText(json.dumps(images))
    render = window.state.render_settings.model_dump()
    render['width'] = 1281  # H.264 needs even dimensions.
    dialog.export_json.setPlainText(json.dumps(render))
    dialog._accept()
    assert warnings
    assert dialog.result() != QDialog.Accepted
    assert window.state.model_dump() == before
    assert load_workflow(window.session).model_dump() == before
    render['width'] = 1280
    dialog.export_json.setPlainText(json.dumps(render))
    dialog._accept()
    assert dialog.result() == QDialog.Accepted
    saved = load_workflow(window.session)
    assert saved.style == 'Watercolor'
    assert saved.density == 'High'
    assert saved.scene_detection == 'paragraph'
    assert saved.image_settings.width == 1024
    assert saved.render_settings.width == 1280
    assert window.state == saved
    reopened = StillsSettingsWindow(window)
    assert reopened.style.toPlainText() == 'Watercolor'
    assert reopened.density.currentText() == 'High'
    assert reopened.scene_detection.currentText() == 'Scene Per Paragraph'
    reopened.scene_detection.setCurrentText('AI scene detection')
    assert reopened.density.isEnabled()
    reopened.style.setPlainText('Discarded')
    reopened.image_json.setPlainText('{}')
    reopened.reject()
    assert window.state == saved
    assert load_workflow(window.session) == saved


def test_settings_blocked_during_work(window, monkeypatch):
    opened = []
    monkeypatch.setattr(StillsSettingsWindow, 'exec', lambda self: opened.append(self))
    window.worker = object()
    window.settings()
    window.worker = None
    assert opened == []
    assert 'Wait for the current operation' in window.status.text()
