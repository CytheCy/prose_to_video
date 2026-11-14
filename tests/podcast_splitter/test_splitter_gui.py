"""Behavioral GUI checks, including the asynchronous worker protocol."""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication
from podcast_splitter.app import Window
from podcast_splitter.core import Project, Section


def test_edit_include_and_approval(tmp_path):
    app = QApplication.instance() or QApplication([])
    w = Window()
    w.project = Project(duration=30, sections=[Section("01 Test", "Some prose", start=2, end=10, status="Matched", confidence=1)])
    w.refresh()
    w.table.selectRow(0)
    w.start.setValue(3.125)
    assert w.project.sections[0].start == 3.125
    assert w.project.sections[0].status == "Manual edit"
    w.approve.setChecked(True)
    assert w.project.sections[0].reviewed
    w.end.setValue(11)
    assert not w.project.sections[0].reviewed
    w.table.item(0, 0).setCheckState(Qt.CheckState.Unchecked)
    assert not w.project.sections[0].included
    assert "0 selected" in w.summary.text()
    w.project_path = str(tmp_path / "saved.json")
    assert w.save_project()
    p = Project.load(w.project_path)
    assert p.sections[0].start == 3.125
    w.dirty = False
    w.close()


def test_async_export_worker(tmp_path):
    import subprocess
    import time
    from podcast_splitter.core import fingerprint
    app = QApplication.instance() or QApplication([])
    audio = tmp_path / "audio.wav"
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "sine=duration=2", str(audio)], check=True)
    w = Window()
    w.project = Project(audio=str(audio), duration=2, audio_fingerprint=fingerprint(str(audio)),
                        output=str(tmp_path / "out"), sections=[Section("01 Test", "", start=.25, end=1.5, reviewed=True)])
    errors = []
    w.error = errors.append
    w.refresh()
    w.start_worker("export", ["--format", "wav"])
    assert not w.export_button.isEnabled()
    deadline = time.monotonic() + 15
    while w.process and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(.01)
    if w.process:
        w.cancel()
    assert not errors
    assert w.process is None
    assert w.export_button.isEnabled()
    assert (tmp_path / "out" / "01 Test.wav").exists()
    w.dirty = False
    w.close()


def test_boundary_preview_uses_new_cut_samples(tmp_path):
    """Check actual rendered samples, not just where the UI draws a marker."""
    import array
    import time
    import wave
    app = QApplication.instance() or QApplication([])
    audio = tmp_path / "source.wav"
    rate = 8000
    samples = array.array("h", ((i // rate + 1) * 1000 for i in range(rate * 10)))
    with wave.open(str(audio), "wb") as f:
        f.setparams((1, 2, rate, 0, "NONE", "not compressed"))
        f.writeframes(samples.tobytes())
    w = Window()
    w.audio_output.setMuted(True)
    w.project = Project(audio=str(audio), duration=10, sections=[Section("Test", "", start=2.25, end=8, reviewed=True)])
    w.refresh()
    def preview_result(previous=None):
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            app.processEvents()
            path = w.player.source().toLocalFile()
            if not w.preview_timer.isActive() and w.preview_process is None and path != previous and path.endswith(".wav") and "preview-" in path:
                with wave.open(path, "rb") as f:
                    data = array.array("h", f.readframes(f.getnframes()))
                    return path, f.getnframes() / f.getframerate(), data[0], data[-1]
            time.sleep(.01)
        raise AssertionError(w.message.text())
    try:
        w.audition(False)
        first, duration, start, end = preview_result()
        assert (duration, start, end) == (5, 3000, 8000)
        w.set_boundary("start", 3.25)
        second, duration, start, end = preview_result(first)
        assert (duration, start, end) == (4.75, 4000, 8000)
        w.audition(True)
        third, duration, start, end = preview_result(second)
        assert (duration, start, end) == (4, 5000, 8000)
        w.set_boundary("end", 6.125)
        fourth, duration, start, end = preview_result(third)
        assert (duration, start, end) == (2.875, 4000, 7000)
        assert w.preview_offset == 3.25
        w.part_padding.setValue(.5)
        fifth, duration, start, end = preview_result(fourth)
        assert (duration, start, end) == (3.875, 3000, 7000)
        assert w.preview_offset == 2.75
        assert w.start.value() == 2.75
        assert w.end.value() == 6.625
    finally:
        w.dirty = False
        w.close()


def test_active_chapter_stop_time_tracks_edits():
    app = QApplication.instance() or QApplication([])
    w = Window()
    w.project = Project(duration=30, sections=[Section("Test", "", start=2, end=10)])
    w.refresh()
    w.playback_mode = "chapter"
    w.play_until = 10
    w.set_boundary("end", 12)
    assert w.play_until == 12
    w.set_boundary("start", 4)
    assert w.play_until == 12
    w.dirty = False
    w.close()



def test_widening_updates_all_parts_and_resets_without_drift(tmp_path):
    app = QApplication.instance() or QApplication([])
    w = Window()
    w.project = Project(duration=20, sections=[
        Section("One", "", start=.25, end=5, reviewed=True),
        Section("Two", "", start=6, end=19.75, included=False, reviewed=True)])
    w.refresh()
    try:
        w.playback_mode = "chapter"
        w.part_padding.setValue(.5)
        assert w.play_until == 5.5
        assert (w.start.value(), w.end.value()) == (0, 5.5)
        assert w.table.item(1, 1).text() == "00:05.500"
        assert w.table.item(1, 2).text() == "00:20.000"
        w.project_path = str(tmp_path / "widened.json")
        assert w.save_project()
        assert Project.load(w.project_path).part_padding == .5
        w.part_padding.setValue(1)
        w.part_padding.setValue(0)
        assert (w.start.value(), w.end.value()) == (.25, 5)
        assert w.project.sections[1].end == 19.75
        w.part_padding.setValue(.5)
        w.end.setValue(5.75)
        assert w.project.cut_points(w.project.sections[0]) == (0, 5.75)
        assert w.project.sections[0].end == 5.25
    finally:
        w.dirty = False
        w.close()
