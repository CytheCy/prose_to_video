"""AI preferences save independently and reach text/image generation settings."""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QEvent, QSettings, QTimer
from PySide6.QtWidgets import QApplication, QDialog

from prose_to_video.gui import ai_settings, legacy_main_window as main_window
from prose_to_video.gui.settings_window import SettingsWindow
from prose_to_video.storage import ai_preferences
from prose_to_video.storage.generated_images import ImageStore
from prose_to_video.storage.project_files import create_project, open_project


@pytest.fixture(scope="session")
def application():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def window(tmp_path, monkeypatch, application):
    factory = lambda *args: QSettings(str(tmp_path / 'preferences.ini'), QSettings.IniFormat)
    for module in (ai_settings, ai_preferences, main_window):
        monkeypatch.setattr(module, 'QSettings', factory)
    window = main_window.MainWindow()
    yield window
    window.close()
    window.deleteLater()
    application.sendPostedEvents(None, QEvent.DeferredDelete)
    application.processEvents()


def test_file_settings_opens_ai_tab(window):
    seen = []
    def inspect():
        dialog = QApplication.activeModalWidget()
        seen.append(isinstance(dialog, SettingsWindow) and dialog.tabs.tabText(0) == 'AI')
        dialog.reject()
    QTimer.singleShot(0, inspect)
    window.preferences_action.trigger()
    assert seen == [True]
    file_menu = window.menuBar().actions()[0].menu()
    assert window.ai_settings_action not in file_menu.actions()


def test_independent_general_and_tti_defaults(window, tmp_path):
    dialog = SettingsWindow(window)
    dialog.general.model.setText('text-model-test')
    dialog.tti_model.setText('image-model-test')
    dialog._save()
    assert dialog.result() == QDialog.Accepted
    assert ai_settings.load_ai_settings().model == 'text-model-test'
    project = create_project(tmp_path, 'New project')
    assert ImageStore(project.root).settings().model == 'image-model-test'
    reopened = SettingsWindow(window)
    assert reopened.general.model.text() == 'text-model-test'
    assert reopened.tti_model.text() == 'image-model-test'
    reopened.general.model.setText('discarded-text')
    reopened.tti_model.setText('discarded-image')
    reopened.reject()
    assert ai_settings.load_ai_settings().model == 'text-model-test'
    assert ai_preferences.load_tti_defaults()['model'] == 'image-model-test'


def test_current_project_tti_preserves_other_settings(window, tmp_path):
    project = create_project(tmp_path, 'Existing project')
    store = ImageStore(project.root)
    initial = store.settings().model_copy(update={'width': 1024, 'height': 768, 'retry_limit': 3})
    store.save_settings(initial)
    window._set_session(project)
    dialog = SettingsWindow(window)
    dialog.tti_provider.setCurrentText('ComfyUI')
    assert dialog.tti_model.text() == ''
    dialog._save()
    assert dialog.result() == QDialog.Accepted
    saved = store.settings()
    assert saved.provider == 'ComfyUI'
    assert saved.model == ''
    assert (saved.width, saved.height, saved.retry_limit) == (1024, 768, 3)
    assert saved.quality == 'auto'
    assert ImageStore(create_project(tmp_path, 'Next').root).settings().provider == 'ComfyUI'


def test_video_settings_save_and_cancel(window, tmp_path):
    project = create_project(tmp_path, 'Video project')
    window._set_session(project)
    dialog = SettingsWindow(window)
    dialog.video.width.setValue(1280)
    dialog.video.height.setValue(720)
    dialog.video.frame_rate.setValue(24)
    dialog._save()
    assert dialog.result() == QDialog.Accepted
    saved = open_project(project.root).project.settings
    assert (saved.width, saved.height, saved.frame_rate) == (1280, 720, 24)
    assert window.session.project.settings == saved
    assert '1280' in window.settings_label.text()

    reopened = SettingsWindow(window)
    assert reopened.video.values() == saved
    reopened.video.width.setValue(1920)
    reopened.reject()
    assert open_project(project.root).project.settings == saved
    assert window.session.project.settings == saved


def test_video_settings_disabled_without_project(window):
    dialog = SettingsWindow(window)
    assert not dialog.video.isEnabled()
    assert not dialog.storyboard.isEnabled()


def test_invalid_video_settings_do_not_save(window, tmp_path, monkeypatch):
    project = create_project(tmp_path, 'Invalid video settings')
    window._set_session(project)
    dialog = SettingsWindow(window)
    dialog.video.width.setValue(1)
    warnings = []
    monkeypatch.setattr('prose_to_video.gui.settings_window.QMessageBox.warning',
                        lambda *args: warnings.append(args))
    dialog._save()
    assert warnings
    assert dialog.result() != QDialog.Accepted
    assert open_project(project.root).project.settings == project.project.settings


def test_storyboard_settings_save_and_cancel(window, tmp_path):
    project = create_project(tmp_path, 'Storyboard preferences')
    window._set_session(project)
    dialog = SettingsWindow(window)
    assert [dialog.tabs.tabText(i) for i in range(dialog.tabs.count())] == ['AI', 'Video', 'Storyboard']
    dialog.storyboard.density.setCurrentText('Custom')
    dialog.storyboard.target.setValue(12)
    dialog.storyboard.style.setText('Watercolor')
    dialog.storyboard.establishing.setChecked(False)
    dialog.storyboard.pause.setCurrentIndex(1)
    dialog._save()
    assert dialog.result() == QDialog.Accepted
    saved = open_project(project.root).database.storyboard_settings()
    assert saved.density == 'Custom'
    assert saved.custom_target == 12
    assert saved.visual_style == 'Watercolor'
    assert not saved.encourage_establishing
    assert saved.pause_policy == 'next'

    reopened = SettingsWindow(window)
    assert reopened.storyboard.values() == saved
    reopened.storyboard.style.setText('Discarded')
    reopened.reject()
    assert project.database.storyboard_settings() == saved


def test_invalid_storyboard_settings_do_not_save(window, tmp_path, monkeypatch):
    project = create_project(tmp_path, 'Invalid storyboard preferences')
    window._set_session(project)
    original = project.database.storyboard_settings()
    dialog = SettingsWindow(window)
    dialog.storyboard.minimum.setValue(60)
    dialog.storyboard.maximum.setValue(10)
    dialog.video.width.setValue(1280)
    warnings = []
    monkeypatch.setattr('prose_to_video.gui.settings_window.QMessageBox.warning',
                        lambda *args: warnings.append(args))
    dialog._save()
    assert warnings
    assert dialog.result() != QDialog.Accepted
    assert project.database.storyboard_settings() == original
    assert open_project(project.root).project.settings == project.project.settings
