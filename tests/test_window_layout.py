"""Short windows keep the workspace controls reachable without resizing."""
import os
import subprocess
import sys


def test_workspace_fits_screen_and_scrolls_to_bottom(tmp_path):
    result = subprocess.run([sys.executable, '-c', r'''
from PySide6.QtCore import QPoint, QSize
from PySide6.QtWidgets import QApplication, QScrollArea
from prose_to_video.gui.main_window import MainWindow

app = QApplication([])
window = MainWindow()
window.show()

def settle():
    for _ in range(5):
        app.processEvents()

settle()
available = window.screen().availableGeometry()
assert window.frameGeometry().height() <= available.height()
assert window.frameGeometry().width() <= available.width()

# Switching to a tall page must not push the window's footer offscreen.
window.resize(760, 600)
for index in range(window.workspace_tabs.count()):
    window.workspace_tabs.setCurrentIndex(index)
    settle()
    assert window.size() == QSize(760, 600)
    assert window.rect().contains(window.statusBar().geometry())
    assert window.rect().contains(window.workspace_tabs.tabBar().geometry())

window.workspace_tabs.setCurrentWidget(window.video)
window.video_message.setText('End of the video controls')
settle()
scroll = window.video.layout().itemAt(0).widget()
assert isinstance(scroll, QScrollArea)
assert scroll.verticalScrollBar().maximum() > 0

def assert_bottom_reachable():
    scroll.ensureWidgetVisible(window.video_message)
    settle()
    bottom = window.video_message.mapTo(scroll.viewport(),
        QPoint(0, window.video_message.height() - 1))
    assert scroll.viewport().rect().contains(bottom)

assert_bottom_reachable()
# Content changes must update the scroll range without fiddling with the window.
previous_maximum = scroll.verticalScrollBar().maximum()
window.video_message.setText('\n'.join(['Additional export details'] * 50))
settle()
assert scroll.verticalScrollBar().maximum() > previous_maximum
window.video_message.setText('Export complete')
settle()
assert_bottom_reachable()
window.resize(1100, 1500)
settle()
assert scroll.verticalScrollBar().maximum() == 0
window.resize(760, 600)
settle()
assert_bottom_reachable()
window.close()
'''], capture_output=True, text=True, timeout=30,
        env={**os.environ, 'QT_QPA_PLATFORM': 'offscreen',
             'XDG_CONFIG_HOME': str(tmp_path / 'config')})
    assert result.returncode == 0, result.stdout + result.stderr
