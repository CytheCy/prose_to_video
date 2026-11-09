"""Public application window for the direct prose-to-stills workflow.

The historical workspace is isolated in legacy_main_window for compatibility
regression tests; the application never constructs its workflow panels/workers.
"""
from prose_to_video.gui.stills_window import MainWindow

__all__ = ['MainWindow']
