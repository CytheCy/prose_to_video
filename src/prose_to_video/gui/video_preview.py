"""Lightweight preview of a still timeline at the audio playhead."""

from math import cos, pi
from pathlib import Path

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QImage, QPainter
from PySide6.QtWidgets import QFrame


class VideoPreview(QFrame):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self.setMinimumHeight(240)
        self._path = None
        self._image = QImage()
        self._camera = None
        self._progress = 0.0
        self._settings = None
        self._previous_path = None
        self._previous_image = QImage()
        self._previous_camera = None
        self._transition = 'Cut'
        self._transition_progress = 1.0

    def show_frame(self, path: Path | None, camera, progress, settings, *,
                   previous_path=None, previous_camera=None, transition='Cut', transition_progress=1.0):
        if path != self._path:
            self._path = path
            self._image = QImage(str(path)) if path else QImage()
        if previous_path != self._previous_path:
            self._previous_path = previous_path
            self._previous_image = QImage(str(previous_path)) if previous_path else QImage()
        self._camera = camera
        self._progress = max(0.0, min(1.0, progress))
        self._settings = settings
        self._previous_camera = previous_camera
        self._transition = transition
        self._transition_progress = max(0.0, min(1.0, transition_progress))
        self.update()

    def paintEvent(self, event):
        super().paintEvent(event)
        painter = QPainter(self)
        painter.fillRect(self.contentsRect(), Qt.GlobalColor.black)
        if self._settings is None or self._image.isNull():
            painter.setPen(Qt.GlobalColor.white)
            message = 'Build a timeline to preview the video' if self._settings is None else 'Selected still unavailable'
            painter.drawText(self.contentsRect(), Qt.AlignmentFlag.AlignCenter, message)
            return

        settings = self._settings
        bounds = QRectF(self.contentsRect())
        aspect = settings.width / settings.height
        width = min(bounds.width(), bounds.height() * aspect)
        height = width / aspect
        target = QRectF(bounds.center().x() - width / 2, bounds.center().y() - height / 2, width, height)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        if not self._previous_image.isNull() and self._transition_progress < 1:
            if self._transition == 'Fade through black':
                self._paint_layer(painter, target, self._previous_image, self._previous_camera,
                                  1, settings, max(0, 1 - 2 * self._transition_progress))
                opacity = max(0, 2 * self._transition_progress - 1)
            else:
                self._draw_frame(painter, target, self._previous_image, self._previous_camera, 1, settings)
                opacity = self._transition_progress
            self._paint_layer(painter, target, self._image, self._camera, self._progress, settings, opacity)
        else:
            self._draw_frame(painter, target, self._image, self._camera, self._progress, settings)

    @classmethod
    def _paint_layer(cls, painter, target, image, camera, progress, settings, opacity):
        if opacity <= 0:
            return
        if opacity >= 1:
            cls._draw_frame(painter, target, image, camera, progress, settings)
            return
        layer = QImage(int(target.width()), int(target.height()), QImage.Format.Format_ARGB32_Premultiplied)
        layer.fill(Qt.GlobalColor.black)
        layer_painter = QPainter(layer)
        layer_painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        cls._draw_frame(layer_painter, QRectF(layer.rect()), image, camera, progress, settings)
        layer_painter.end()
        painter.setOpacity(opacity)
        painter.drawImage(target, layer)
        painter.setOpacity(1)

    @staticmethod
    def _draw_frame(painter, target, image, camera, progress, settings):
        painter.fillRect(target, QColor(settings.background_color))
        iw, ih = image.width(), image.height()
        width, height = target.width(), target.height()
        aspect = settings.width / settings.height
        if camera.kind == 'Static' and settings.fit_policy == 'fit':
            fit = min(width / iw, height / ih)
            image_target = QRectF(target.center().x() - iw * fit / 2,
                                  target.center().y() - ih * fit / 2, iw * fit, ih * fit)
            painter.drawImage(image_target, image)
            return

        # Match the export's aspect-ratio crop, followed by its zoompan viewport.
        crop_width = min(iw, ih * aspect)
        crop_height = crop_width / aspect
        anchor_x = anchor_y = .5
        if camera.kind.startswith('zoom_in_') and '_pan_' not in camera.kind or camera.kind == 'zoom_out_from_focal_point':
            anchor_x, anchor_y = camera.start_x, camera.start_y
        base_x = (iw - crop_width) * anchor_x
        base_y = (ih - crop_height) * anchor_y
        t = progress
        zoom = (1 + (camera.end_zoom - 1) * (1 - cos(2 * pi * t)) / 2
                if camera.kind == 'breathe' else
                camera.start_zoom + (camera.end_zoom - camera.start_zoom) * t)
        zoom = max(1, zoom)
        x = max(0, min(1, camera.start_x + (camera.end_x - camera.start_x) * t))
        y = max(0, min(1, camera.start_y + (camera.end_y - camera.start_y) * t))
        view_width, view_height = crop_width / zoom, crop_height / zoom
        source = QRectF(base_x + (crop_width - view_width) * x,
                        base_y + (crop_height - view_height) * y,
                        view_width, view_height)
        painter.drawImage(target, image, source)
