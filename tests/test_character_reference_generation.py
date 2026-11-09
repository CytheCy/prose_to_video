"""Character reference generation uses project settings and preserves prior images."""
import os
import subprocess
import sys


def test_generate_character_reference(tmp_path):
    result = subprocess.run([sys.executable, '-c', '''
import sys
import time
from pathlib import Path
from threading import Event
from PySide6.QtCore import QBuffer, QIODevice
from PySide6.QtGui import QImage, QColor
from PySide6.QtWidgets import QApplication
from prose_to_video.gui.main_window import MainWindow
from prose_to_video.models.image_job import ProviderResult, ProviderImage
from prose_to_video.models.scene import Scene
from prose_to_video.pipeline.stills_workflow import Identity, load_workflow, generate_character_reference
from prose_to_video.pipeline import image_generation
from prose_to_video.storage.project_files import create_project

app = QApplication([])
window = MainWindow()
assert not window.generate_character_reference_button.isEnabled()
session = create_project(Path(sys.argv[1]), 'generation')
window.load(session)
character = Identity(kind='Character', name='Mira', description='green coat',
    references=['references/existing.png'])
window.state.catalogs = [character]
window.state.scenes = [Scene(sequence=i, start=i-1, end=i, text='A',
    identity_links=[character.identifier]) for i in [1, 2]]
window.state.style = 'Watercolor'
window.state.image_settings.width = 1024
window.state.image_settings.height = 1024
window.refresh()
assert window.generate_character_reference_button.text() == 'Generate Reference'
assert window.generate_character_reference_button.isEnabled()
image = QImage(50, 100, QImage.Format_RGB32)
image.fill(QColor('red'))
buffer = QBuffer()
buffer.open(QIODevice.WriteOnly)
assert image.save(buffer, 'PNG')

class Provider:
    name = 'Fake'
    requests = []
    fail = False
    def validate(self, request):
        assert request.model == window.state.image_settings.model
        assert request.width == request.height == 1024
    def submit(self, request):
        self.requests.append(request)
        return ProviderResult(status='Completed', images=[ProviderImage(
            data=b'broken' if self.fail else bytes(buffer.data()), model=request.model)])

provider = Provider()
image_generation.make_image_provider = lambda *args: provider
window.catalog_edits['Character'].setPlainText('blue coat, short hair')
window.generate_character_reference_button.click()
deadline = time.monotonic() + 10
while window.worker and time.monotonic() < deadline:
    app.processEvents()
    time.sleep(.01)
assert window.worker is None
request = provider.requests[0]
assert 'blue coat, short hair' in request.prompt and 'Watercolor' in request.prompt
assert 'full body' in request.prompt
saved = load_workflow(session)
assert saved.catalogs[0].description == 'blue coat, short hair'
assert len(saved.catalogs[0].references) == 2
assert saved.catalogs[0].references[0] == 'references/existing.png'
path = saved.catalogs[0].references[-1]
assert not QImage(str(session.root / path)).isNull()
assert window.character_reference_position.text() == '2 / 2'
assert not window.character_reference_preview.pixmap().isNull()
assert saved.jobs[-1]['status'] == 'Completed'
assert not saved.stills

provider.fail = True
try:
    generate_character_reference(session, window.state, character, provider)
except ValueError as exc:
    assert 'unusable' in str(exc)
else:
    raise AssertionError('Invalid image accepted')
assert load_workflow(session).catalogs[0].references == saved.catalogs[0].references
cancel = Event()
cancel.set()
before = len(provider.requests)
try:
    generate_character_reference(session, window.state, character, provider, cancel)
except ValueError as exc:
    assert 'canceled' in str(exc)
else:
    raise AssertionError('Canceled request submitted')
assert len(provider.requests) == before
window.close()
''', str(tmp_path)], capture_output=True, text=True, timeout=30,
        env={**os.environ, 'QT_QPA_PLATFORM': 'offscreen',
             'XDG_CONFIG_HOME': str(tmp_path / 'config')})
    assert result.returncode == 0, result.stderr
