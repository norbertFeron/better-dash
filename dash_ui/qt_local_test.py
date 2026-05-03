"""
Qt local test — a Mac window driving QtRenderer with the keyboard.

Usage::

    python -m dash_ui.qt_local_test --scale 2

Same key bindings as ``dash_ui.local_test``:

    ←  / a       Button.LEFT
    →  / d       Button.RIGHT
    ↓  / s / ↑   Button.DOWN
    Enter/Space  Button.CLICK
    Esc / q      quit
"""

from __future__ import annotations

import argparse
import signal
import sys

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QImage, QKeyEvent, QPainter
from PySide6.QtWidgets import QApplication, QWidget

from dash_ui.bike_link import Button
from dash_ui.qt_renderer import QtRenderer


_KEYMAP = {
    Qt.Key.Key_Left: Button.LEFT,
    Qt.Key.Key_A: Button.LEFT,
    Qt.Key.Key_Right: Button.RIGHT,
    Qt.Key.Key_D: Button.RIGHT,
    # The dash UI now uses only LEFT / RIGHT / DOWN, so we route every
    # "select / activate" key on the Mac to DOWN — that's the button the
    # state machine treats as "open / activate / back".
    Qt.Key.Key_Down: Button.DOWN,
    Qt.Key.Key_S: Button.DOWN,
    Qt.Key.Key_Up: Button.DOWN,
    Qt.Key.Key_W: Button.DOWN,
    Qt.Key.Key_Return: Button.DOWN,
    Qt.Key.Key_Enter: Button.DOWN,
    Qt.Key.Key_Space: Button.DOWN,
}


class _DashWindow(QWidget):
    def __init__(self, renderer: QtRenderer, scale: int, fps_cap: int) -> None:
        super().__init__()
        self._renderer = renderer
        self._scale = max(1, scale)
        self.setWindowTitle("Pi Dash · Qt local test (←→↓ enter)")
        self.resize(renderer.width * self._scale, renderer.height * self._scale)
        # Refresh on a wall-clock timer so animations (the pulsing pin
        # on the Map screen) tick smoothly even with no input.
        self._timer = QTimer(self)
        self._timer.timeout.connect(self.update)
        self._timer.start(int(1000 / max(1, fps_cap)))

    def paintEvent(self, _ev) -> None:
        img = self._renderer.render_qimage()
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        p.drawImage(self.rect(), img)
        p.end()

    def keyPressEvent(self, ev: QKeyEvent) -> None:  # noqa: N802
        key = ev.key()
        if key in (Qt.Key.Key_Escape, Qt.Key.Key_Q):
            self.close()
            return
        btn = _KEYMAP.get(Qt.Key(key))
        if btn is not None:
            self._renderer.inject_button(btn)


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(
        description="Run the Qt UI locally with keyboard input (no bike).",
    )
    p.add_argument("--scale", type=int, default=2,
                   help="Window scale factor (default 2 → 1052x600).")
    p.add_argument("--fps-cap", type=int, default=30,
                   help="Wall-clock fps for the local window.")
    p.add_argument("--calibration-grid", action="store_true")
    p.add_argument("--video-file", default="test_640.mp4")
    p.add_argument("--gpx-dir", default="gpx_files")
    p.add_argument("--tile-cache", default="tile_cache")
    p.add_argument("--nav-zoom", type=int, default=14)
    p.add_argument("--nav-speed-kmh", type=float, default=80.0)
    p.add_argument("--ui-fps", type=int, default=12,
                   help="Renderer fps (controls animation timing).")
    args = p.parse_args(argv)

    app = QApplication.instance() or QApplication(sys.argv)
    signal.signal(signal.SIGINT, lambda *_: app.quit())
    _sigint_timer = QTimer()
    _sigint_timer.start(200)
    _sigint_timer.timeout.connect(lambda: None)
    renderer = QtRenderer(
        headless=True,
        calibration_grid=args.calibration_grid,
        video_file=args.video_file,
        fps=args.ui_fps,
        gpx_dir=args.gpx_dir,
        tile_cache=args.tile_cache,
        nav_zoom=args.nav_zoom,
        nav_speed_kmh=args.nav_speed_kmh,
    )
    window = _DashWindow(renderer, args.scale, args.fps_cap)
    window.show()

    print(
        f"local UI {renderer.width}x{renderer.height} → window "
        f"{renderer.width * args.scale}x{renderer.height * args.scale}\n"
        "  ← / →     navigate (cycles through items / Back row)\n"
        "  ↓ / Enter activate the highlighted row\n"
        "  Esc       quit\n"
        "  (CLICK from the bike is intentionally ignored.)",
        file=sys.stderr,
    )
    rc = app.exec()
    renderer.close()
    return rc


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
