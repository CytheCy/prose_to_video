"""Credential file selection and persistence."""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QApplication, QDialogButtonBox

from prose_to_video.gui import ai_settings
from prose_to_video.models.image_job import ImageGenerationSettings
from prose_to_video.pipeline import image_generation
from prose_to_video.pipeline.providers import AISettings, OpenAIProvider, read_api_key


def test_key_file_takes_precedence_and_missing_file_is_reported(tmp_path, monkeypatch):
    key_file = tmp_path / "key.txt"
    key_file.write_text("  file-key\n", encoding="utf-8")
    monkeypatch.setenv("TEST_API_KEY", "environment-key")

    assert read_api_key("TEST_API_KEY", str(key_file)) == "file-key"
    assert read_api_key("TEST_API_KEY") == "environment-key"
    key_file.write_text("\n", encoding="utf-8")
    with pytest.raises(ValueError, match="empty"):
        read_api_key("TEST_API_KEY", str(key_file))
    key_file.unlink()
    with pytest.raises(ValueError, match="Could not read"):
        read_api_key("TEST_API_KEY", str(key_file))


def test_dialog_browses_and_saves_only_key_path(tmp_path, monkeypatch):
    QApplication.instance() or QApplication([])
    settings_path = tmp_path / "settings.ini"
    monkeypatch.setattr(ai_settings, "QSettings",
                        lambda *args: QSettings(str(settings_path), QSettings.Format.IniFormat))
    key_file = tmp_path / "key.txt"
    key_file.write_text("secret-for-test\n", encoding="utf-8")
    monkeypatch.setattr(ai_settings.QFileDialog, "getOpenFileName",
                        lambda *args: (str(key_file), "Text files (*.txt)"))

    dialog = ai_settings.AISettingsDialog(AISettings())
    dialog._browse_credential_file()
    dialog.credential_env.setText("sk-accidentally-pasted-here")
    assert dialog.credential_file.text() == str(key_file)
    dialog.findChild(QDialogButtonBox).button(QDialogButtonBox.StandardButton.Save).click()
    assert dialog.result() == dialog.DialogCode.Accepted
    assert ai_settings.load_ai_settings().credential_file == str(key_file)
    assert ai_settings.load_ai_settings().credential_env == "OPENAI_API_KEY"
    assert "secret-for-test" not in settings_path.read_text(encoding="utf-8")
    assert "sk-accidentally-pasted-here" not in settings_path.read_text(encoding="utf-8")


def test_scene_and_image_providers_use_saved_key_file(tmp_path, monkeypatch):
    import openai

    key_file = tmp_path / "key.txt"
    key_file.write_text("first-key\n", encoding="utf-8")
    settings_path = tmp_path / "settings.ini"
    settings_factory = lambda *args: QSettings(str(settings_path), QSettings.Format.IniFormat)
    monkeypatch.setattr(ai_settings, "QSettings", settings_factory)
    monkeypatch.setattr(image_generation, "QSettings", settings_factory)
    ai_settings.save_ai_settings(AISettings(credential_env="", credential_file=str(key_file)))

    keys = []
    monkeypatch.setattr(openai, "OpenAI", lambda **kwargs: keys.append(kwargs["api_key"]) or object())
    OpenAIProvider(ai_settings.load_ai_settings())
    key_file.write_text("second-key\n", encoding="utf-8")
    image_provider = image_generation.make_image_provider(ImageGenerationSettings(provider="OpenAI"))
    image_provider._client()
    assert keys == ["first-key", "second-key"]
