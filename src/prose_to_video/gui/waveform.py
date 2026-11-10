"""Cached, bounded waveform summary drawn at the current widget resolution."""
from __future__ import annotations

import json
import subprocess
from array import array
from pathlib import Path

from PySide6.QtCore import QPointF, Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPen, QPolygonF
from PySide6.QtWidgets import QWidget

from prose_to_video.models.alignment import SceneTiming
from prose_to_video.pipeline.audio_transcription import source_file_hash


def waveform_peaks(audio: Path, cache_dir: Path) -> list[float]:
    cache_dir.mkdir(parents=True, exist_ok=True)
    path = cache_dir / f"waveform-{source_file_hash(audio)}.json"
    if path.is_file():
        return json.loads(path.read_text(encoding="utf-8"))
    # 1000 samples/second, one peak per 0.5 seconds. Streaming keeps memory bounded.
    process = subprocess.Popen(["ffmpeg", "-nostdin", "-v", "error", "-i", str(audio),
                                "-ac", "1", "-ar", "1000", "-f", "s16le", "-"],
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    peaks = []
    pending = b""
    try:
        while True:
            block = process.stdout.read(100_000)
            if not block:
                break
            pending += block
            full = len(pending) // 1000 * 1000
            for offset in range(0, full, 1000):
                samples = array("h")
                samples.frombytes(pending[offset:offset + 1000])
                peaks.append(max(abs(value) for value in samples) / 32768)
            pending = pending[full:]
        if pending:
            samples = array("h")
            samples.frombytes(pending[:len(pending) - len(pending) % 2])
            if samples:
                peaks.append(max(abs(value) for value in samples) / 32768)
        error = process.stderr.read().decode(errors="replace")
        if process.wait() != 0:
            raise ValueError(f"Could not generate waveform: {error[:300]}")
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(peaks), encoding="utf-8")
    temporary.replace(path)
    return peaks


class Waveform(QWidget):
    seek_requested = Signal(float)
    scene_selected = Signal(str)
    boundary_moved = Signal(int, float)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumHeight(90)
        self.peaks: list[float] = []
        self.duration = 0.0
        self.position = 0.0
        self.timings: list[SceneTiming] = []
        self.boundaries: list[float] = []
        self.boundaries_editable = True
        self._drag_boundary: int | None = None
        self._drag_start_x = 0.0
        self._drag_start_seconds = 0.0
        self.setMouseTracking(True)

    def set_boundaries(self, boundaries: list[float]) -> None:
        """Display interval endpoints; only interior endpoints can be moved."""
        self.boundaries = list(boundaries)
        self._drag_boundary = None
        self.update()

    def set_boundaries_editable(self, editable: bool) -> None:
        self.boundaries_editable = editable
        self._drag_boundary = None
        self.unsetCursor()

    def _boundary_at(self, x: float) -> int | None:
        if self.duration <= 0 or not self.boundaries_editable:
            return None
        candidates = range(1, len(self.boundaries) - 1)
        nearest = min(candidates, key=lambda i: abs(self.boundaries[i] / self.duration * self.width() - x), default=None)
        if nearest is not None and abs(self.boundaries[nearest] / self.duration * self.width() - x) <= 8:
            return nearest
        return None

    def _drag_to(self, x: float) -> None:
        i = self._drag_boundary
        if i is None:
            return
        # Keep both neighboring stills nonempty and all other cuts fixed.
        lower, upper = self.boundaries[i - 1] + .001, self.boundaries[i + 1] - .001
        if lower > upper:
            return
        if x == self._drag_start_x:
            self.boundaries[i] = self._drag_start_seconds
            self.update()
            return
        seconds = round(self._drag_start_seconds + (x - self._drag_start_x) / max(1, self.width()) * self.duration, 3)
        self.boundaries[i] = max(lower, min(upper, seconds))
        self.update()

    def set_data(self, peaks: list[float], duration: float) -> None:
        self.peaks, self.duration = peaks, duration
        self.update()

    def set_timings(self, timings: list[SceneTiming]) -> None:
        self.timings = timings
        self.update()

    def set_position(self, seconds: float) -> None:
        self.position = seconds
        self.update()

    def mousePressEvent(self, event) -> None:
        if self.duration <= 0 or event.button() != Qt.LeftButton:
            return
        self._drag_boundary = self._boundary_at(event.position().x())
        if self._drag_boundary is not None:
            self._drag_start_x = event.position().x()
            self._drag_start_seconds = self.boundaries[self._drag_boundary]
            self.setCursor(Qt.SizeHorCursor)
            return
        seconds = max(0, min(self.duration, event.position().x() / max(1, self.width()) * self.duration))
        self.seek_requested.emit(seconds)
        for timing in self.timings:
            if timing.audio_start is not None and timing.audio_start <= seconds <= timing.audio_end:
                self.scene_selected.emit(timing.scene_id)
                break

    def mouseMoveEvent(self, event) -> None:
        if self._drag_boundary is not None:
            self._drag_to(event.position().x())
            return
        i = self._boundary_at(event.position().x())
        self.setCursor(Qt.SizeHorCursor if i is not None else Qt.ArrowCursor)
        self.setToolTip(f'Still {i} → {i + 1}: {self.boundaries[i]:.3f}s · Drag to adjust' if i is not None else '')

    def mouseReleaseEvent(self, event) -> None:
        if event.button() == Qt.LeftButton and self._drag_boundary is not None:
            self._drag_to(event.position().x())
            i = self._drag_boundary
            self._drag_boundary = None
            self.boundary_moved.emit(i, self.boundaries[i])
            self.unsetCursor()

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor("#222b38"))
        width, height = self.width(), self.height()
        center = height // 2
        if self.peaks:
            painter.setPen(QPen(QColor("#7cc6db"), 1))
            for x in range(width):
                first = int(x * len(self.peaks) / width)
                last = max(first + 1, int((x + 1) * len(self.peaks) / width))
                peak = max(self.peaks[first:last], default=0)
                amplitude = int(peak * (height - 28) / 2)
                painter.drawLine(x, center - amplitude, x, center + amplitude)
        if self.duration > 0:
            painter.setPen(QColor("#d9e2ec"))
            step = 3600 if self.duration / max(1, width) > 12 else 600 if self.duration / max(1, width) > 2 else 60 if self.duration > 1800 else 10
            for second in range(0, int(self.duration) + 1, step):
                x = int(second / self.duration * width)
                painter.drawLine(x, height - 17, x, height - 12)
                painter.drawText(x + 2, height - 2, f"{second // 60}:{second % 60:02d}")
            for timing in self.timings:
                if timing.audio_start is None: continue
                for second, color in ((timing.audio_start, "#f5ae59"),
                                      (timing.audio_end, "#e7d17f")):
                    x = int(second / self.duration * width)
                    painter.setPen(QPen(QColor(color), 2))
                    painter.drawLine(x, 0, x, height - 20)
            for i, second in enumerate(self.boundaries):
                x = int(second / self.duration * width)
                painter.setPen(QPen(QColor('#f5ae59'), 2))
                painter.drawLine(x, 0, x, height - 20)
                if 0 < i < len(self.boundaries) - 1:
                    painter.setBrush(QColor('#ffffff' if i == self._drag_boundary else '#f5ae59'))
                    painter.drawPolygon(QPolygonF([QPointF(x - 6, 0), QPointF(x + 6, 0), QPointF(x + 6, 9), QPointF(x, 15), QPointF(x - 6, 9)]))
                    painter.drawText(x + 8, 13, f'{i} → {i + 1}')
                    if i == self._drag_boundary:
                        painter.drawText(max(2, min(x + 8, width - 90)), 30, f'{second:.3f}s')
            painter.setPen(QPen(QColor("#ff6b6b"), 2))
            x = int(self.position / self.duration * width)
            painter.drawLine(x, 0, x, height)
