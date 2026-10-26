from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from prose_to_video.gui.main_window import MainWindow


def test_window_smoke() -> None:
    application = QApplication.instance() or QApplication([])
    window = MainWindow()
    window.show()
    application.processEvents()
    assert window.isVisible()
    assert not window.save_action.isEnabled()
    assert not window.import_prose_button.isEnabled()
    window.close()
