"""
QtRenderer — PySide6/QPainter implementation of the Renderer protocol.

A modernised alternative to ``PygameRenderer``.  Both produce 526x300
RGB24 byte frames at the same cadence, so ``DashUIStream`` consumes
them interchangeably.

What this gives you over the pygame version
    - Real font hinting + sub-pixel anti-aliased text (system fonts
      via QFontDatabase).
    - Smooth gradients (QLinearGradient / QRadialGradient).
    - QPainterPath for curved roads on the Map placeholder, dashed
      strokes, rounded cards.
    - Composited alpha — soft shadows, glows, translucent overlays.

What stays identical
    - Same `Renderer` protocol (``width``, ``height``, ``fps``,
      ``render_frame() -> bytes``).
    - Same Button enum and ``inject_button(btn)`` queue semantics, so
      ``BikeLink`` wires up unchanged.
    - Same round-dash safe-area cropping (radius 142, 12 px margin).
    - Same Video screen using the existing ffmpeg-pipe approach.

Threading model
    QGuiApplication is created on the main thread (in __init__).  After
    that, QPainter + QImage off-screen rendering is safe to run on the
    encoder feed thread (no widgets, no event loop interaction).
"""

from __future__ import annotations

import collections
import math
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Deque

from dash_ui.renderer import DASH_FPS, DASH_HEIGHT, DASH_WIDTH

try:
    from PySide6.QtCore import Qt, QPointF, QRectF, QSize
    from PySide6.QtGui import (
        QBrush,
        QColor,
        QFont,
        QFontDatabase,
        QGuiApplication,
        QImage,
        QLinearGradient,
        QPainter,
        QPainterPath,
        QPen,
        QPolygonF,
        QRadialGradient,
        QTransform,
    )
    _HAS_QT = True
except ImportError:  # pragma: no cover
    _HAS_QT = False

try:
    from dash_ui.bike_link import Button
except ImportError:  # pragma: no cover
    Button = None  # type: ignore[assignment]

from dash_ui.gpx import Track, list_gpx, parse_gpx
from dash_ui.map_view import TileCache, render_basemap


MENU_ITEMS = ("Open Map", "GPS Tracks", "Settings")
MAP_ITEM = "Open Map"
GPS_ITEM = "GPS Tracks"
SETTINGS_ITEM = "Settings"

# Settings rows.  Index 0 is always "Back"; DOWN on it closes the screen.
_SETTINGS_ROWS = ("Back", "View")

# Map view modes — cycled via DOWN on the "View" row in Settings.
#   north   : top-down, N is always up (default).
#   heading : map rotates so the direction of travel is up; compass
#             needle rotates so the user can still tell where N is.
#   behind  : heading-up + a forward-looking perspective trapezoid;
#             the bike sits low on the canvas and the road ahead
#             recedes towards the horizon (no 3D buildings — just the
#             camera angle).
_VIEW_MODES = ("north", "heading", "behind")
_VIEW_LABELS = {
    "north":   "North up",
    "heading": "Heading up",
    "behind":  "Behind bike",
}


# ---------------------------------------------------------------------------
# Modern colour palette (loosely inspired by Material 3 / Apple HIG dark)
# ---------------------------------------------------------------------------
_BG_TOP        = QColor(0x0B, 0x10, 0x20) if _HAS_QT else None
_BG_BOTTOM     = QColor(0x16, 0x1B, 0x2D) if _HAS_QT else None
_CARD_BG       = QColor(0x1A, 0x21, 0x36, 220) if _HAS_QT else None
_CARD_HI       = QColor(0x27, 0x33, 0x55) if _HAS_QT else None
_ACCENT_A      = QColor(0x4E, 0xC9, 0xFF) if _HAS_QT else None    # cyan
_ACCENT_B      = QColor(0x9D, 0x6BFF if False else 0x6B, 0xFF) if _HAS_QT else None
_ACCENT_C      = QColor(0xFF, 0xC1, 0x4E) if _HAS_QT else None    # warm yellow
_ACCENT_GREEN  = QColor(0x6B, 0xE0, 0x9D) if _HAS_QT else None
_ACCENT_PINK   = QColor(0xFF, 0x6B, 0xC1) if _HAS_QT else None
_TEXT_HI       = QColor(0xF1, 0xF4, 0xFA) if _HAS_QT else None
_TEXT_LO       = QColor(0x9E, 0xA8, 0xC2) if _HAS_QT else None
_TEXT_DIM      = QColor(0x55, 0x60, 0x7E) if _HAS_QT else None
_HAIRLINE      = QColor(0xFF, 0xFF, 0xFF, 22) if _HAS_QT else None

_ITEM_PALETTE = (
    _ACCENT_GREEN,    # Open Map
    _ACCENT_A,        # GPS Tracks
    _ACCENT_B,        # Settings (purple-ish)
)

_BUTTON_COLOURS = {
    "LEFT":  _ACCENT_A,
    "RIGHT": _ACCENT_GREEN,
    "DOWN":  _ACCENT_C,
    "CLICK": _ACCENT_PINK,
}

_MENU_ICON_FILES = {
    MAP_ITEM: "map-icon.png",
    GPS_ITEM: "track-icon.png",
    SETTINGS_ITEM: "settings-icon.png",
}
_TRACK_ICON_FILES = (
    "highlands-icon.png",
    "valley-icon.png",
    "coastal-icon.png",
)


class QtRenderer:
    """PySide6 implementation of the Renderer protocol."""

    width: int = DASH_WIDTH
    height: int = DASH_HEIGHT
    fps: int = DASH_FPS

    _SAFE_RADIUS = 142
    _SAFE_MARGIN = 12

    # Slippy-map zoom range exposed via LEFT / RIGHT in the nav screen.
    _NAV_ZOOM_MIN = 10
    _NAV_ZOOM_MAX = 18

    def __init__(
        self,
        title: str = "Tripper UI",
        *,
        headless: bool = True,
        dummy_video: bool = False,
        calibration_grid: bool = False,
        video_file: str | Path = "test_640.mp4",
        fps: int = DASH_FPS,
        gpx_dir: str | Path = "gpx_files",
        tile_cache: str | Path = "tile_cache",
        nav_zoom: int = 14,
        nav_speed_kmh: float = 20.0,
    ) -> None:
        if not _HAS_QT:
            raise ImportError(
                "PySide6 is required for QtRenderer.\n"
                "Install with:  pip install PySide6"
            )
        if dummy_video and "QT_QPA_PLATFORM" not in os.environ:
            os.environ["QT_QPA_PLATFORM"] = "offscreen"

        # QGuiApplication must exist before any QImage / QFont call;
        # singleton, safe to create here when nothing else has yet.
        app = QGuiApplication.instance()
        if app is None:
            app = QGuiApplication(sys.argv[:1])
        self._app = app  # keep a reference

        self.fps = max(1, int(fps))
        self._headless = headless
        self._calibration_grid = calibration_grid
        self._video_path = Path(video_file).expanduser()
        self._title = title
        self._icons_dir = Path(__file__).resolve().parent.parent / "icons"
        self._icons = self._load_icons()

        # Off-screen render target — Format_RGB888 = packed R,G,B, but
        # scanlines are still 32-bit aligned (we strip per-row in
        # _image_to_bytes()).
        self._image = QImage(self.width, self.height, QImage.Format.Format_RGB888)
        self._image.fill(_BG_TOP)

        # Fonts — system sans (San Francisco / Segoe UI / DejaVu Sans).
        sysf = QFontDatabase.systemFont(QFontDatabase.SystemFont.GeneralFont)
        self._font_brand = QFont(sysf)
        self._font_brand.setPixelSize(13)
        self._font_brand.setWeight(QFont.Weight.Black)
        self._font_brand.setLetterSpacing(QFont.SpacingType.PercentageSpacing, 130)

        self._font_item = QFont(sysf)
        self._font_item.setPixelSize(15)
        self._font_item.setWeight(QFont.Weight.Medium)

        self._font_meta = QFont(sysf)
        self._font_meta.setPixelSize(10)
        self._font_meta.setWeight(QFont.Weight.Medium)

        self._font_big = QFont(sysf)
        self._font_big.setPixelSize(20)
        self._font_big.setWeight(QFont.Weight.Bold)

        self._font_tag = QFont(sysf)
        self._font_tag.setPixelSize(9)
        self._font_tag.setWeight(QFont.Weight.Bold)
        self._font_tag.setLetterSpacing(QFont.SpacingType.PercentageSpacing, 140)

        # Button queue + UI state.
        self._lock = threading.Lock()
        self._pending: Deque["Button"] = collections.deque(maxlen=16)
        self._selected = 0
        self._click_counts = [0] * len(MENU_ITEMS)
        self._map_open = False
        self._video_open = False
        self._settings_open = False
        self._last_button_name: str | None = None
        self._last_button_at: float = 0.0
        self._frame_count = 0
        self._start_time = time.monotonic()
        self._last_frame_at = 0.0

        # Settings state.
        self._view_mode = "north"

        # Video subprocess plumbing (parity with pygame_renderer).
        self._video_proc: subprocess.Popen[bytes] | None = None
        self._video_thr: threading.Thread | None = None
        self._video_stop = threading.Event()
        self._video_lock = threading.Lock()
        self._last_video_frame: bytes | None = None
        self._video_error: str | None = None

        # ----- Map / GPX picker / navigation simulation --------------
        self._gpx_dir = Path(gpx_dir).expanduser()
        self._tile_cache_dir = Path(tile_cache).expanduser()
        self._nav_zoom = int(nav_zoom)
        self._nav_speed_mps = max(0.0, float(nav_speed_kmh)) * 1000.0 / 3600.0
        self._tile_cache_obj = TileCache(capacity=64)

        # Picker state — index 0 is the synthetic "Back" row, files
        # follow at indices 1..N.  Default cursor on the first file
        # when files exist, else on Back (the only available choice).
        self._gpx_files: list[Path] = []
        self._picker_index = 0

        # Settings cursor — also includes a "Back" row at index 0.
        self._settings_index = 1  # cursor on first real setting

        # Live nav-simulation state.
        self._nav_open = False
        self._nav_track: Track | None = None
        self._nav_track_path: Path | None = None
        self._nav_file_index = 0
        self._nav_decimated: list[tuple[float, float]] = []
        self._nav_arc_m = 0.0
        self._nav_last_tick: float | None = None
        self._nav_missing_tiles = 0

    # =========================================================== Renderer API

    def inject_button(self, button: "Button") -> None:
        with self._lock:
            self._pending.append(button)

    def render_frame(self) -> bytes:
        self._drain_buttons()
        p = QPainter(self._image)
        p.setRenderHints(
            QPainter.RenderHint.Antialiasing
            | QPainter.RenderHint.SmoothPixmapTransform
            | QPainter.RenderHint.TextAntialiasing,
        )
        try:
            if self._calibration_grid:
                self._draw_calibration(p)
            elif self._video_open:
                self._draw_video(p)
            elif self._map_open:
                self._draw_map_screen(p)
            elif self._settings_open:
                self._draw_settings_screen(p)
            else:
                self._draw_menu(p)

            self._draw_button_badge(p)
            self._draw_dash_safe_overlay(p)
        finally:
            p.end()
        self._pace_frame()
        self._frame_count += 1
        return self._image_to_bytes()

    def close(self) -> None:
        self._stop_video()
        # Don't quit the QGuiApplication — other QtRenderer instances
        # (or a local-test window) may still need it.

    def _load_icons(self) -> dict[str, QImage]:
        icons: dict[str, QImage] = {}
        filenames = set(_MENU_ICON_FILES.values()) | set(_TRACK_ICON_FILES)
        for name in filenames:
            img = QImage(str(self._icons_dir / name))
            if img.isNull():
                continue
            icons[name] = img
        return icons

    def _pace_frame(self) -> None:
        """Match pygame's frame pacing so ffmpeg/RTP never queues stale UI frames."""
        period = 1.0 / max(1, self.fps)
        now = time.monotonic()
        if self._last_frame_at > 0.0:
            due = self._last_frame_at + period
            if now < due:
                time.sleep(due - now)
                now = time.monotonic()
        self._last_frame_at = now

    # =================================================== Button state machine

    def _drain_buttons(self) -> None:
        if Button is None:
            return
        with self._lock:
            events = list(self._pending)
            self._pending.clear()
        for ev in events:
            self._apply(ev)

    def _apply(self, button: "Button") -> None:
        if Button is None:
            return
        self._last_button_name = button.name
        self._last_button_at = time.monotonic()

        # CLICK is intentionally a no-op for this UI — the bike-link
        # layer still acks the dash so the protocol watchdog stays
        # happy; we just don't react to it.  Every screen below uses
        # only LEFT, RIGHT, and DOWN.
        if button is Button.CLICK:
            return

        # ----------------------------------------------------- Video
        if self._video_open:
            # Player screen: DOWN exits.  LEFT/RIGHT are no-ops so
            # rocking the joystick while watching doesn't yank the
            # player out from under you.
            if button is Button.DOWN:
                self._video_open = False
                self._stop_video()
            return

        # ----------------------------------------------------- Map sub-flow
        if self._map_open:
            if self._nav_open:
                # ---- Navigation simulation --------------------------
                # LEFT  = zoom out, RIGHT = zoom in.  Clamped to the
                # OSM range that's actually useful for navigation:
                #   - z=10 (~150 m / px @ equator) → wide overview
                #   - z=18 (~0.6 m / px) → street level
                # Tiles outside the pre-downloaded zooms will trigger
                # the "missing tiles" warning chip.
                if button is Button.DOWN:
                    self._picker_index = self._nav_file_index + 1  # +1 for Back row
                    self._close_nav()
                elif button is Button.LEFT:
                    self._nav_zoom = max(self._NAV_ZOOM_MIN, self._nav_zoom - 1)
                elif button is Button.RIGHT:
                    self._nav_zoom = min(self._NAV_ZOOM_MAX, self._nav_zoom + 1)
                return

            # ---- GPX file picker (index 0 = "Back" row) -------------
            n_items = 1 + len(self._gpx_files)
            if button is Button.LEFT:
                self._picker_index = (self._picker_index - 1) % n_items
            elif button is Button.RIGHT:
                self._picker_index = (self._picker_index + 1) % n_items
            elif button is Button.DOWN:
                if self._picker_index == 0:
                    # Back row → close picker.
                    self._map_open = False
                else:
                    file_idx = self._picker_index - 1
                    self._open_nav(self._gpx_files[file_idx])
            return

        # ----------------------------------------------------- Settings
        if self._settings_open:
            n = len(_SETTINGS_ROWS)
            if button is Button.LEFT:
                self._settings_index = (self._settings_index - 1) % n
            elif button is Button.RIGHT:
                self._settings_index = (self._settings_index + 1) % n
            elif button is Button.DOWN:
                self._activate_setting(self._settings_index)
            return

        # ----------------------------------------------------- Top menu
        if button is Button.LEFT:
            self._selected = (self._selected - 1) % len(MENU_ITEMS)
        elif button is Button.RIGHT:
            self._selected = (self._selected + 1) % len(MENU_ITEMS)
        elif button is Button.DOWN:
            label = MENU_ITEMS[self._selected]
            if label in (MAP_ITEM, GPS_ITEM):
                # Re-scan the gpx folder so dropping a new file in
                # there shows up without restarting.
                self._gpx_files = list_gpx(self._gpx_dir)
                # Park the cursor on the first track when files exist;
                # otherwise it stays on the Back row.
                self._picker_index = 1 if self._gpx_files else 0
                self._map_open = True
            elif label == SETTINGS_ITEM:
                self._settings_index = 1  # cursor on first real setting
                self._settings_open = True

    def _activate_setting(self, idx: int) -> None:
        if idx == 0:
            # Back row.
            self._settings_open = False
        elif _SETTINGS_ROWS[idx] == "View":
            cur = _VIEW_MODES.index(self._view_mode)
            self._view_mode = _VIEW_MODES[(cur + 1) % len(_VIEW_MODES)]

    # ============================================================ Drawing

    def _draw_background(self, p: QPainter) -> None:
        g = QLinearGradient(0, 0, 0, self.height)
        g.setColorAt(0.0, _BG_TOP)
        g.setColorAt(1.0, _BG_BOTTOM)
        p.fillRect(0, 0, self.width, self.height, QBrush(g))

        # Subtle radial glow towards the centre of the round dash.
        rg = QRadialGradient(
            QPointF(self.width / 2, self.height / 2),
            self._SAFE_RADIUS,
        )
        rg.setColorAt(0.0, QColor(60, 90, 140, 50))
        rg.setColorAt(1.0, QColor(0, 0, 0, 0))
        p.fillRect(0, 0, self.width, self.height, QBrush(rg))

        # Low-poly glass facets keep the background alive under the bike
        # overlay without competing with the real dashboard chrome.
        facets = (
            (QColor(0x2D, 0x77, 0xC8, 32),
             (QPointF(0, 70), QPointF(118, 24), QPointF(190, 182), QPointF(40, 210))),
            (QColor(0x25, 0xB7, 0xD3, 26),
             (QPointF(336, 32), QPointF(526, 0), QPointF(526, 132), QPointF(438, 164))),
            (QColor(0x68, 0x5B, 0xFF, 24),
             (QPointF(202, 0), QPointF(354, 0), QPointF(294, 92), QPointF(170, 74))),
            (QColor(0x00, 0xD7, 0xFF, 18),
             (QPointF(114, 214), QPointF(282, 164), QPointF(526, 216), QPointF(526, 300), QPointF(0, 300))),
        )
        p.setPen(Qt.PenStyle.NoPen)
        for colour, points in facets:
            p.setBrush(QBrush(colour))
            p.drawPolygon(QPolygonF(points))

        horizon = QLinearGradient(0, 78, 0, 186)
        horizon.setColorAt(0.0, QColor(255, 255, 255, 0))
        horizon.setColorAt(0.55, QColor(0x21, 0x9D, 0xFF, 28))
        horizon.setColorAt(1.0, QColor(255, 255, 255, 0))
        p.fillRect(0, 70, self.width, 128, QBrush(horizon))

    def _draw_dashboard_title(
        self,
        p: QPainter,
        title: str,
        *,
        subtitle: str | None = None,
    ) -> None:
        p.setFont(self._font_big)
        p.setPen(QPen(_TEXT_HI))
        fm = p.fontMetrics()
        tw = fm.horizontalAdvance(title)
        p.drawText(int(self.width / 2 - tw / 2), 30, title)

        glow = QLinearGradient(self.width / 2 - 90, 40, self.width / 2 + 90, 40)
        glow.setColorAt(0.0, QColor(255, 255, 255, 0))
        glow.setColorAt(0.5, QColor(_ACCENT_A.red(), _ACCENT_A.green(), _ACCENT_A.blue(), 150))
        glow.setColorAt(1.0, QColor(255, 255, 255, 0))
        p.setPen(QPen(QBrush(glow), 1))
        p.drawLine(int(self.width / 2 - 90), 42, int(self.width / 2 + 90), 42)

        if subtitle:
            p.setFont(self._font_meta)
            p.setPen(QPen(_TEXT_LO))
            fm = p.fontMetrics()
            tw = fm.horizontalAdvance(subtitle)
            p.drawText(int(self.width / 2 - tw / 2), 54, subtitle)

    def _draw_top_bar(self, p: QPainter) -> None:
        cx = self.width // 2
        x, w = self._safe_band(20, 50, extra_margin=4)

        # Brand pill: rounded rect with a small accent dot + wordmark.
        rect = QRectF(x, 22, w, 28)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(QColor(255, 255, 255, 14)))
        p.drawRoundedRect(rect, 14, 14)

        dot_r = 4
        dot_x = rect.left() + 14
        dot_y = rect.center().y()
        rg = QRadialGradient(QPointF(dot_x, dot_y), dot_r * 2.4)
        rg.setColorAt(0.0, _ACCENT_A)
        rg.setColorAt(1.0, QColor(0, 0, 0, 0))
        p.setBrush(QBrush(rg))
        p.drawEllipse(QPointF(dot_x, dot_y), dot_r * 2.2, dot_r * 2.2)
        p.setBrush(QBrush(_ACCENT_A))
        p.drawEllipse(QPointF(dot_x, dot_y), dot_r, dot_r)

        # Brand text (centered).
        p.setFont(self._font_brand)
        p.setPen(QPen(_TEXT_HI))
        fm = p.fontMetrics()
        text = "PI DASH"
        tw = fm.horizontalAdvance(text)
        p.drawText(int(cx - tw / 2), int(rect.center().y() + fm.ascent() / 2 - 1), text)

        # Right side: tiny FPS readout.
        p.setFont(self._font_meta)
        p.setPen(QPen(_TEXT_LO))
        fm = p.fontMetrics()
        elapsed = time.monotonic() - self._start_time
        readout = f"{self.fps} FPS · {elapsed:4.0f}s"
        tw = fm.horizontalAdvance(readout)
        p.drawText(
            int(rect.right() - tw - 12),
            int(rect.center().y() + fm.ascent() / 2 - 1),
            readout,
        )

    def _draw_menu(self, p: QPainter) -> None:
        self._draw_background(p)
        self._draw_dashboard_title(p, "MENU")

        # Carousel launcher: the selected item is always on the display
        # centreline, with previous/next options shown as smaller previews.
        centre_x = self.width / 2
        selected_idx = self._selected
        prev_idx = (selected_idx - 1) % len(MENU_ITEMS)
        next_idx = (selected_idx + 1) % len(MENU_ITEMS)

        side_w, side_h = 124, 112
        side_y = 82
        side_offset = 146
        for idx, cx in ((prev_idx, centre_x - side_offset),
                        (next_idx, centre_x + side_offset)):
            label = MENU_ITEMS[idx]
            self._draw_launcher_tile(
                p, label, idx,
                int(cx - side_w / 2), side_y, side_w, side_h,
                _ITEM_PALETTE[idx % len(_ITEM_PALETTE)], False,
            )

        main_w, main_h = 148, 144
        label = MENU_ITEMS[selected_idx]
        self._draw_launcher_tile(
            p, label, selected_idx,
            int(centre_x - main_w / 2), 64, main_w, main_h,
            _ITEM_PALETTE[selected_idx % len(_ITEM_PALETTE)], True,
        )
        self._draw_control_strip(p, active="SELECT")

    def _draw_launcher_tile(
        self,
        p: QPainter,
        label: str,
        idx: int,
        x: int,
        y: int,
        w: int,
        h: int,
        accent: QColor,
        selected: bool,
    ) -> None:
        rect = QRectF(x, y, w, h)
        has_png_icon = self._icons.get(_MENU_ICON_FILES.get(label, "")) is not None

        if selected and not has_png_icon:
            for grow, alpha in ((14, 30), (8, 54), (3, 100)):
                p.setBrush(QBrush(QColor(accent.red(), accent.green(), accent.blue(), alpha)))
                p.setPen(Qt.PenStyle.NoPen)
                p.drawRoundedRect(rect.adjusted(-grow, -grow, grow, grow), 18, 18)

        if not has_png_icon:
            glass = QLinearGradient(rect.topLeft(), rect.bottomRight())
            glass.setColorAt(0.0, QColor(255, 255, 255, 54 if selected else 28))
            glass.setColorAt(0.42, QColor(0x1C, 0x2B, 0x47, 210))
            glass.setColorAt(1.0, QColor(0x08, 0x0D, 0x1C, 232))
            p.setBrush(QBrush(glass))
            p.setPen(QPen(QColor(accent.red(), accent.green(), accent.blue(), 180 if selected else 70), 1.2))
            p.drawRoundedRect(rect, 16, 16)

            # Bright top edge and warm selection underline.
            edge = QLinearGradient(rect.left(), rect.top(), rect.right(), rect.top())
            edge.setColorAt(0.0, QColor(255, 255, 255, 0))
            edge.setColorAt(0.5, QColor(255, 255, 255, 115 if selected else 55))
            edge.setColorAt(1.0, QColor(255, 255, 255, 0))
            p.setPen(QPen(QBrush(edge), 1))
            p.drawLine(int(rect.left() + 12), int(rect.top() + 7),
                       int(rect.right() - 12), int(rect.top() + 7))

        icon_y = rect.top() + (56 if selected else 44)
        self._draw_icon_plinth(p, rect.center().x(), icon_y, accent, label, selected=selected)

        p.setFont(self._font_tag)
        p.setPen(QPen(_TEXT_HI if selected else _TEXT_LO))
        fm = p.fontMetrics()
        title = self._launcher_title(label)
        tw = fm.horizontalAdvance(title)
        p.drawText(
            int(rect.center().x() - tw / 2),
            int(rect.bottom() - (28 if selected else 14)),
            title,
        )

        if selected and has_png_icon:
            underline = QLinearGradient(rect.left() + 16, rect.bottom() - 16,
                                        rect.right() - 16, rect.bottom() - 16)
            underline.setColorAt(0.0, QColor(255, 255, 255, 0))
            underline.setColorAt(0.5, _ACCENT_C)
            underline.setColorAt(1.0, QColor(255, 255, 255, 0))
            p.setPen(QPen(QBrush(underline), 2))
            p.drawLine(int(rect.left() + 20), int(rect.bottom() - 16),
                       int(rect.right() - 20), int(rect.bottom() - 16))

        if selected:
            p.setFont(self._font_meta)
            p.setPen(QPen(_ACCENT_C))
            hint = "SELECT"
            tw = p.fontMetrics().horizontalAdvance(hint)
            p.drawText(int(rect.center().x() - tw / 2), int(rect.bottom() - 4), hint)

    def _draw_icon_plinth(
        self,
        p: QPainter,
        cx: float,
        cy: float,
        accent: QColor,
        label: str,
        *,
        selected: bool = False,
    ) -> None:
        icon_name = _MENU_ICON_FILES.get(label)
        icon = self._icons.get(icon_name or "")
        if icon is not None:
            size = 142 if selected else 108
            self._draw_icon_image(
                p,
                icon,
                QRectF(cx - size / 2, cy - size / 2, size, size),
                glow=accent,
            )
            return

        shadow = QRadialGradient(QPointF(cx, cy + 15), 45)
        shadow.setColorAt(0.0, QColor(accent.red(), accent.green(), accent.blue(), 78))
        shadow.setColorAt(1.0, QColor(0, 0, 0, 0))
        p.setBrush(QBrush(shadow))
        p.setPen(Qt.PenStyle.NoPen)
        p.drawEllipse(QPointF(cx, cy + 15), 50, 22)

        base = QRectF(cx - 34, cy + 4, 68, 20)
        plinth = QLinearGradient(base.topLeft(), base.bottomRight())
        plinth.setColorAt(0.0, QColor(255, 255, 255, 105))
        plinth.setColorAt(0.45, QColor(0x99, 0x7A, 0x55, 165))
        plinth.setColorAt(1.0, QColor(0x17, 0x1A, 0x27, 220))
        p.setBrush(QBrush(plinth))
        p.setPen(QPen(QColor(255, 230, 180, 125), 1))
        p.drawRoundedRect(base, 10, 10)

        icon = QRectF(cx - 23, cy - 26, 46, 46)
        face = QRadialGradient(icon.center(), 34)
        face.setColorAt(0.0, QColor(255, 255, 255, 235))
        face.setColorAt(0.48, QColor(accent.red(), accent.green(), accent.blue(), 185))
        face.setColorAt(1.0, QColor(0x10, 0x18, 0x2A, 240))
        p.setBrush(QBrush(face))
        p.setPen(QPen(QColor(255, 255, 255, 130), 1))
        p.drawEllipse(icon)

        glyph_rect = QRectF(cx - 14, cy - 17, 28, 28)
        self._draw_menu_glyph(p, label, glyph_rect, QColor(0xF6, 0xFA, 0xFF))

    def _draw_icon_image(
        self,
        p: QPainter,
        icon: QImage,
        rect: QRectF,
        *,
        glow: QColor | None = None,
    ) -> None:
        if glow is not None:
            shadow = QRadialGradient(rect.center(), max(rect.width(), rect.height()) * 0.58)
            shadow.setColorAt(0.0, QColor(glow.red(), glow.green(), glow.blue(), 80))
            shadow.setColorAt(1.0, QColor(0, 0, 0, 0))
            p.setBrush(QBrush(shadow))
            p.setPen(Qt.PenStyle.NoPen)
            p.drawEllipse(rect.center(), rect.width() * 0.58, rect.height() * 0.48)

        iw = max(1, icon.width())
        ih = max(1, icon.height())
        scale = min(rect.width() / iw, rect.height() / ih)
        w = iw * scale
        h = ih * scale
        target = QRectF(
            rect.center().x() - w / 2,
            rect.center().y() - h / 2,
            w,
            h,
        )
        p.drawImage(target, icon)

    def _track_icon_for_index(self, idx: int) -> QImage | None:
        if not _TRACK_ICON_FILES:
            return None
        name = _TRACK_ICON_FILES[idx % len(_TRACK_ICON_FILES)]
        return self._icons.get(name) or self._icons.get("track-icon.png")

    def _draw_settings_launcher(
        self,
        p: QPainter,
        idx: int,
        x: int,
        y: int,
        w: int,
        h: int,
        selected: bool,
    ) -> None:
        rect = QRectF(x, y, w, h)
        accent = _ACCENT_B
        if selected:
            p.setBrush(QBrush(QColor(accent.red(), accent.green(), accent.blue(), 66)))
            p.setPen(Qt.PenStyle.NoPen)
            p.drawRoundedRect(rect.adjusted(-5, -5, 5, 5), 16, 16)

        p.setBrush(QBrush(QColor(255, 255, 255, 26 if selected else 16)))
        p.setPen(QPen(QColor(accent.red(), accent.green(), accent.blue(), 150 if selected else 64), 1))
        p.drawRoundedRect(rect, 14, 14)
        self._draw_menu_glyph(
            p, SETTINGS_ITEM, QRectF(rect.left() + 9, rect.top() + 5, 18, 18),
            accent if selected else _TEXT_LO,
        )
        p.setFont(self._font_meta)
        p.setPen(QPen(_TEXT_HI if selected else _TEXT_LO))
        text = "SET" if rect.width() < 110 else "DASH SETTINGS"
        p.drawText(int(rect.left() + 34), int(rect.top() + 18), text)

        if selected:
            p.setBrush(QBrush(_ACCENT_C))
            p.setPen(Qt.PenStyle.NoPen)
            p.drawEllipse(QPointF(rect.right() - 15, rect.center().y()), 3, 3)

    def _draw_control_strip(self, p: QPainter, *, active: str, y: int | None = None) -> None:
        if y is None:
            y = self.height - 42
        labels = (("↩", "LEFT"), ("↪", "RIGHT"), ("↓", active))
        slot = 74
        x0 = self.width / 2 - slot
        for i, (icon, label) in enumerate(labels):
            cx = x0 + i * slot
            colour = _ACCENT_C if label == active else _TEXT_LO
            p.setFont(self._font_item)
            p.setPen(QPen(colour))
            fm = p.fontMetrics()
            p.drawText(int(cx - fm.horizontalAdvance(icon) / 2), y + 7, icon)
            p.setFont(self._font_tag)
            p.setPen(QPen(_TEXT_LO))
            fm = p.fontMetrics()
            p.drawText(int(cx - fm.horizontalAdvance(label) / 2), y + 18, label)

    def _draw_menu_card(
        self,
        p: QPainter,
        label: str,
        idx: int,
        x: int,
        y: int,
        w: int,
        h: int,
        selected: bool,
    ) -> None:
        if w <= 0:
            return
        rect = QRectF(x, y, w, h)

        if selected:
            # Soft glow behind the selected card.
            for j, alpha in enumerate((22, 38, 70)):
                grow = (3 - j) * 4
                glow = rect.adjusted(-grow, -grow, grow, grow)
                p.setBrush(QBrush(QColor(_ACCENT_A.red(), _ACCENT_A.green(), _ACCENT_A.blue(), alpha)))
                p.setPen(Qt.PenStyle.NoPen)
                p.drawRoundedRect(glow, 14 + grow / 2, 14 + grow / 2)

            # Card body — gradient.
            grad = QLinearGradient(rect.topLeft(), rect.bottomRight())
            grad.setColorAt(0.0, QColor(0x2A, 0x35, 0x57))
            grad.setColorAt(1.0, QColor(0x18, 0x20, 0x3A))
            p.setBrush(QBrush(grad))
        else:
            p.setBrush(QBrush(_CARD_BG))

        p.setPen(QPen(_HAIRLINE, 1))
        p.drawRoundedRect(rect, 12, 12)

        # Left-side icon chip.
        icon_size = 24
        icon_rect = QRectF(rect.left() + 10, rect.center().y() - icon_size / 2,
                           icon_size, icon_size)
        item_colour = _ITEM_PALETTE[idx % len(_ITEM_PALETTE)]
        p.setBrush(QBrush(QColor(item_colour.red(), item_colour.green(), item_colour.blue(), 40)))
        p.setPen(Qt.PenStyle.NoPen)
        p.drawRoundedRect(icon_rect, 8, 8)
        self._draw_menu_glyph(p, label, icon_rect, item_colour)

        # Label.
        p.setFont(self._font_item)
        p.setPen(QPen(_TEXT_HI if selected else _TEXT_LO))
        fm = p.fontMetrics()
        p.drawText(
            int(icon_rect.right() + 12),
            int(rect.center().y() + fm.ascent() / 2 - 2),
            label,
        )

        # Right-side hint.
        p.setFont(self._font_meta)
        p.setPen(QPen(_TEXT_DIM))
        hint = self._item_hint(label)
        if hint:
            fm = p.fontMetrics()
            tw = fm.horizontalAdvance(hint)
            p.drawText(
                int(rect.right() - tw - 14),
                int(rect.center().y() + fm.ascent() / 2 - 2),
                hint,
            )

        # Click pip(s) — stacked dots above the icon for feedback.
        clicks = self._click_counts[idx]
        for k in range(min(clicks, 6)):
            cx = icon_rect.left() + icon_rect.width() / 2 - 12 + k * 5
            cy = rect.bottom() + 4 + (k % 2) * 0  # subtle row at the very bottom
            del cy
        # Click dot row inside the card.
        for k in range(min(clicks, 8)):
            cx = rect.right() - 14 - k * 7
            cy = rect.top() + 8
            p.setBrush(QBrush(item_colour))
            p.setPen(Qt.PenStyle.NoPen)
            p.drawEllipse(QPointF(cx, cy), 2, 2)

        # Selection chevron.
        if selected:
            cx = icon_rect.left() - 12
            cy = rect.center().y()
            poly = QPolygonF([
                QPointF(cx - 3, cy - 5),
                QPointF(cx + 3, cy),
                QPointF(cx - 3, cy + 5),
            ])
            p.setBrush(QBrush(_ACCENT_C))
            p.setPen(Qt.PenStyle.NoPen)
            p.drawPolygon(poly)

    def _draw_menu_glyph(
        self,
        p: QPainter,
        label: str,
        rect: QRectF,
        colour: QColor,
    ) -> None:
        cx = rect.center().x()
        cy = rect.center().y()
        pen = QPen(colour, 1.6)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        p.setPen(pen)
        p.setBrush(Qt.BrushStyle.NoBrush)

        if label == MAP_ITEM:
            # Pin glyph.
            path = QPainterPath()
            path.moveTo(cx, cy - 7)
            path.cubicTo(cx + 6, cy - 7, cx + 6, cy + 1, cx, cy + 7)
            path.cubicTo(cx - 6, cy + 1, cx - 6, cy - 7, cx, cy - 7)
            p.drawPath(path)
            p.setBrush(QBrush(colour))
            p.drawEllipse(QPointF(cx, cy - 2), 1.8, 1.8)
        elif label == GPS_ITEM:
            # Stacked route cards.
            for off, alpha in ((4, 95), (0, 180), (-4, 235)):
                card = QRectF(cx - 8 + off, cy - 7 + off / 2, 16, 11)
                p.setBrush(QBrush(QColor(colour.red(), colour.green(), colour.blue(), alpha)))
                p.setPen(QPen(QColor(255, 255, 255, alpha), 0.8))
                p.drawRoundedRect(card, 3, 3)
            p.setPen(QPen(_BG_TOP, 1.2,
                          Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap,
                          Qt.PenJoinStyle.RoundJoin))
            route = QPainterPath()
            route.moveTo(cx - 6, cy + 2)
            route.cubicTo(cx - 2, cy - 6, cx + 4, cy + 6, cx + 8, cy - 3)
            p.drawPath(route)
        elif label == SETTINGS_ITEM:
            # Gear approximated as 8 little spokes around a centre ring.
            for i in range(8):
                a = i * math.pi / 4
                x1 = cx + math.cos(a) * 3.5
                y1 = cy + math.sin(a) * 3.5
                x2 = cx + math.cos(a) * 7.5
                y2 = cy + math.sin(a) * 7.5
                p.drawLine(QPointF(x1, y1), QPointF(x2, y2))
            p.setBrush(QBrush(colour))
            p.setPen(Qt.PenStyle.NoPen)
            p.drawEllipse(QPointF(cx, cy), 3, 3)

    # ============================================================ Map sub-flow

    def _draw_map_screen(self, p: QPainter) -> None:
        """Dispatch: GPX picker first, navigation simulation when a track is open."""
        if self._nav_open and self._nav_track is not None:
            self._draw_nav_screen(p)
        else:
            self._draw_gpx_picker(p)

    # ------------------------------------------------------------ Picker

    def _draw_gpx_picker(self, p: QPainter) -> None:
        self._draw_background(p)
        self._draw_dashboard_title(p, "GPS TRACKS")

        # Combined list: Back row at index 0, files at 1..N.  When no
        # files exist, the picker shows an empty-state card *plus* the
        # Back row so DOWN still has somewhere obvious to land.
        n_items = 1 + len(self._gpx_files)
        visible = 4
        half = visible // 2
        first = (
            max(0, min(self._picker_index - half, n_items - visible))
            if n_items > visible else 0
        )
        end = min(n_items, first + visible)

        list_y = 54
        row_h = 40
        gap = 5
        x = 164
        w = 330
        cascade_dx = 12
        for slot, idx in enumerate(range(first, end)):
            top = list_y + slot * (row_h + gap)
            row_x = x + slot * cascade_dx
            row_w = w
            selected = idx == self._picker_index
            if idx == 0:
                self._draw_track_picker_row(
                    p, "Back", "Return to launcher", idx, row_x, top, row_w, row_h,
                    selected, is_back=True,
                )
            else:
                file_idx = idx - 1
                path = self._gpx_files[file_idx]
                name = self._elided_text(p, path.stem, 185, self._font_item)
                subtitle = f"{file_idx + 1} OF {len(self._gpx_files)}   READY"
                self._draw_track_picker_row(
                    p, name, subtitle, file_idx, row_x, top, row_w, row_h,
                    selected, is_back=False,
                )

        # Empty-state hint when the picker has only the Back row.
        if not self._gpx_files:
            self._draw_picker_empty(p)

        # Scroll bar (right side) — useful once you have more than 2 tracks.
        if n_items > visible:
            track_top = list_y
            track_h = visible * row_h + (visible - 1) * gap
            track_x = x + (visible - 1) * cascade_dx + w + 7
            p.setBrush(QBrush(QColor(255, 255, 255, 26)))
            p.setPen(Qt.PenStyle.NoPen)
            p.drawRoundedRect(QRectF(track_x, track_top, 4, track_h), 2, 2)
            thumb_h = max(20, track_h * visible / n_items)
            thumb_y = track_top + (track_h - thumb_h) * (
                self._picker_index / max(1, n_items - 1)
            )
            p.setBrush(QBrush(_ACCENT_C))
            p.drawRoundedRect(QRectF(track_x, thumb_y, 4, thumb_h), 2, 2)

        self._draw_control_strip(p, active="SELECT")

    def _draw_track_picker_row(
        self,
        p: QPainter,
        title: str,
        subtitle: str,
        idx: int,
        x: int,
        y: int,
        w: int,
        h: int,
        selected: bool,
        *,
        is_back: bool,
    ) -> None:
        rect = QRectF(x, y, w, h)
        accent = QColor(255, 255, 255) if is_back else _ACCENT_GREEN
        if selected:
            for grow, alpha in ((12, 28), (7, 54), (3, 105)):
                p.setBrush(QBrush(QColor(accent.red(), accent.green(), accent.blue(), alpha)))
                p.setPen(Qt.PenStyle.NoPen)
                p.drawRoundedRect(rect.adjusted(-grow, -grow, grow, grow), 16, 16)

        body = QLinearGradient(rect.topLeft(), rect.bottomRight())
        if selected:
            body.setColorAt(0.0, QColor(0x3A, 0x42, 0x55) if is_back else QColor(0x2F, 0x4C, 0x46))
            body.setColorAt(1.0, QColor(0x16, 0x1D, 0x2C) if is_back else QColor(0x0D, 0x25, 0x22))
        else:
            body.setColorAt(0.0, QColor(255, 255, 255, 24))
            body.setColorAt(1.0, QColor(0x0C, 0x13, 0x25, 205))
        p.setBrush(QBrush(body))
        p.setPen(QPen(QColor(accent.red(), accent.green(), accent.blue(), 155 if selected else 45), 1))
        p.drawRoundedRect(rect, 14, 14)

        chip_size = 34 if is_back else 44
        chip = QRectF(
            rect.left() + 12,
            rect.center().y() - chip_size / 2,
            chip_size,
            chip_size,
        )
        track_icon = None if is_back else self._track_icon_for_index(idx)
        if is_back or track_icon is None:
            p.setBrush(QBrush(QColor(accent.red(), accent.green(), accent.blue(), 48 if selected else 28)))
            p.setPen(QPen(QColor(255, 255, 255, 54), 1))
            p.drawRoundedRect(chip, 12, 12)

        if is_back:
            cx = chip.center().x()
            cy = chip.center().y()
            p.setPen(QPen(_TEXT_HI, 1.9,
                          Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap,
                          Qt.PenJoinStyle.RoundJoin))
            p.drawLine(QPointF(cx + 5, cy - 5), QPointF(cx - 4, cy))
            p.drawLine(QPointF(cx - 4, cy), QPointF(cx + 5, cy + 5))
        else:
            if track_icon is not None:
                self._draw_icon_image(p, track_icon, chip.adjusted(-5, -7, 5, 4))
            else:
                self._draw_menu_glyph(p, GPS_ITEM, chip, _TEXT_HI)

        p.setFont(self._font_item)
        p.setPen(QPen(_TEXT_HI if selected else _TEXT_LO))
        fm = p.fontMetrics()
        p.drawText(int(chip.right() + 12), int(rect.top() + 18), title.upper())

        p.setFont(self._font_meta)
        p.setPen(QPen(_ACCENT_C if selected else _TEXT_DIM))
        p.drawText(int(chip.right() + 12), int(rect.bottom() - 8), subtitle)

        if not is_back:
            p.setFont(self._font_tag)
            p.setPen(QPen(_TEXT_HI if selected else _TEXT_DIM))
            tag = "SELECT"
            tw = p.fontMetrics().horizontalAdvance(tag)
            p.drawText(int(rect.right() - tw - 12), int(rect.center().y() + 4), tag)

        if selected:
            p.setBrush(QBrush(_ACCENT_C))
            p.setPen(Qt.PenStyle.NoPen)
            p.drawRoundedRect(QRectF(rect.left() - 5, rect.top() + 9, 3, rect.height() - 18), 2, 2)

    def _draw_back_card(
        self,
        p: QPainter,
        x: int, y: int, w: int, h: int,
        selected: bool,
        *,
        subtitle: str = "",
    ) -> None:
        """A grey "Back" row used at the top of every list-style screen."""
        if w <= 0:
            return
        rect = QRectF(x, y, w, h)

        if selected:
            for j, alpha in enumerate((20, 32, 60)):
                grow = (3 - j) * 4
                glow = rect.adjusted(-grow, -grow, grow, grow)
                p.setBrush(QBrush(QColor(255, 255, 255, alpha)))
                p.setPen(Qt.PenStyle.NoPen)
                p.drawRoundedRect(glow, 14 + grow / 2, 14 + grow / 2)
            grad = QLinearGradient(rect.topLeft(), rect.bottomRight())
            grad.setColorAt(0.0, QColor(0x2C, 0x33, 0x45))
            grad.setColorAt(1.0, QColor(0x18, 0x1D, 0x2C))
            p.setBrush(QBrush(grad))
        else:
            p.setBrush(QBrush(_CARD_BG))
        p.setPen(QPen(_HAIRLINE, 1))
        p.drawRoundedRect(rect, 12, 12)

        # Left chip with a back-arrow glyph.
        chip = QRectF(rect.left() + 10, rect.center().y() - 12, 24, 24)
        p.setBrush(QBrush(QColor(255, 255, 255, 30)))
        p.setPen(Qt.PenStyle.NoPen)
        p.drawRoundedRect(chip, 8, 8)
        cx = chip.center().x()
        cy = chip.center().y()
        pen = QPen(_TEXT_HI, 1.8,
                   Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap,
                   Qt.PenJoinStyle.RoundJoin)
        p.setPen(pen)
        p.drawLine(QPointF(cx + 4, cy - 5), QPointF(cx - 4, cy))
        p.drawLine(QPointF(cx - 4, cy), QPointF(cx + 4, cy + 5))

        p.setFont(self._font_item)
        p.setPen(QPen(_TEXT_HI if selected else _TEXT_LO))
        fm = p.fontMetrics()
        label = "Back"
        p.drawText(
            int(chip.right() + 12),
            int(rect.center().y() + fm.ascent() / 2 - 4),
            label,
        )
        if subtitle:
            p.setFont(self._font_meta)
            p.setPen(QPen(_TEXT_DIM))
            p.drawText(int(chip.right() + 12),
                       int(rect.bottom() - 8), subtitle)

        if selected:
            cx = chip.left() - 12
            cy = rect.center().y()
            poly = QPolygonF([
                QPointF(cx - 3, cy - 5),
                QPointF(cx + 3, cy),
                QPointF(cx - 3, cy + 5),
            ])
            p.setBrush(QBrush(_ACCENT_C))
            p.setPen(Qt.PenStyle.NoPen)
            p.drawPolygon(poly)

    def _draw_track_card(
        self,
        p: QPainter,
        path: Path,
        idx: int,
        x: int, y: int, w: int, h: int,
        selected: bool,
    ) -> None:
        if w <= 0:
            return
        rect = QRectF(x, y, w, h)

        if selected:
            for j, alpha in enumerate((22, 38, 70)):
                grow = (3 - j) * 4
                glow = rect.adjusted(-grow, -grow, grow, grow)
                p.setBrush(QBrush(QColor(_ACCENT_GREEN.red(),
                                          _ACCENT_GREEN.green(),
                                          _ACCENT_GREEN.blue(), alpha)))
                p.setPen(Qt.PenStyle.NoPen)
                p.drawRoundedRect(glow, 14 + grow / 2, 14 + grow / 2)
            grad = QLinearGradient(rect.topLeft(), rect.bottomRight())
            grad.setColorAt(0.0, QColor(0x24, 0x38, 0x33))
            grad.setColorAt(1.0, QColor(0x14, 0x22, 0x1F))
            p.setBrush(QBrush(grad))
        else:
            p.setBrush(QBrush(_CARD_BG))
        p.setPen(QPen(_HAIRLINE, 1))
        p.drawRoundedRect(rect, 12, 12)

        # Left chip with a tiny "track" glyph (zigzag).
        chip = QRectF(rect.left() + 10, rect.center().y() - 12, 24, 24)
        p.setBrush(QBrush(QColor(_ACCENT_GREEN.red(),
                                  _ACCENT_GREEN.green(),
                                  _ACCENT_GREEN.blue(), 45)))
        p.setPen(Qt.PenStyle.NoPen)
        p.drawRoundedRect(chip, 8, 8)
        pen = QPen(_ACCENT_GREEN, 1.6,
                   Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap,
                   Qt.PenJoinStyle.RoundJoin)
        p.setPen(pen)
        p.setBrush(Qt.BrushStyle.NoBrush)
        cx = chip.center().x()
        cy = chip.center().y()
        path_g = QPainterPath()
        path_g.moveTo(cx - 7, cy + 4)
        path_g.lineTo(cx - 2, cy - 3)
        path_g.lineTo(cx + 3, cy + 1)
        path_g.lineTo(cx + 7, cy - 4)
        p.drawPath(path_g)

        # Friendly track name (first 28 chars; full filename stem fallback).
        name = path.stem
        if len(name) > 28:
            name = name[:27] + "…"

        p.setFont(self._font_item)
        p.setPen(QPen(_TEXT_HI if selected else _TEXT_LO))
        fm = p.fontMetrics()
        p.drawText(
            int(chip.right() + 12),
            int(rect.center().y() + fm.ascent() / 2 - 4),
            name,
        )

        # Sub-line: file index / count.
        p.setFont(self._font_meta)
        p.setPen(QPen(_TEXT_DIM))
        sub = f"{idx + 1} / {len(self._gpx_files)}"
        p.drawText(int(chip.right() + 12), int(rect.bottom() - 8), sub)

        if selected:
            cx = chip.left() - 12
            cy = rect.center().y()
            poly = QPolygonF([
                QPointF(cx - 3, cy - 5),
                QPointF(cx + 3, cy),
                QPointF(cx - 3, cy + 5),
            ])
            p.setBrush(QBrush(_ACCENT_C))
            p.setPen(Qt.PenStyle.NoPen)
            p.drawPolygon(poly)

    def _draw_picker_empty(self, p: QPainter) -> None:
        # Sits below the Back row (which lives at y=64..108).
        x, w = self._safe_band(130, 230, extra_margin=10)
        rect = QRectF(x, 130, w, 90)
        p.setBrush(QBrush(_CARD_BG))
        p.setPen(QPen(_HAIRLINE, 1))
        p.drawRoundedRect(rect, 12, 12)

        p.setFont(self._font_item)
        p.setPen(QPen(_TEXT_HI))
        msg = "No GPX tracks found"
        fm = p.fontMetrics()
        tw = fm.horizontalAdvance(msg)
        p.drawText(int(rect.center().x() - tw / 2),
                   int(rect.top() + 30), msg)

        p.setFont(self._font_meta)
        p.setPen(QPen(_TEXT_LO))
        sub_lines = [
            "Drop .gpx files into",
            str(self._gpx_dir),
        ]
        for i, line in enumerate(sub_lines):
            fm = p.fontMetrics()
            tw = fm.horizontalAdvance(line)
            p.drawText(
                int(rect.center().x() - tw / 2),
                int(rect.top() + 50 + i * 14),
                line,
            )

    # ------------------------------------------------------------ Nav

    def _open_nav(self, gpx_path: Path) -> None:
        """Load a GPX file and start the at-20km/h simulation."""
        try:
            track = parse_gpx(gpx_path)
        except Exception as exc:  # pragma: no cover - defensive
            print(f"[qt-renderer] failed to parse {gpx_path.name}: {exc}",
                  file=sys.stderr)
            self._nav_track = None
            self._nav_open = False
            return
        if not track.points:
            print(f"[qt-renderer] {gpx_path.name} has no track points",
                  file=sys.stderr)
            self._nav_track = None
            self._nav_open = False
            return
        self._nav_track = track
        self._nav_track_path = gpx_path
        # Remember which slot in the picker this came from so LEFT /
        # RIGHT in the nav screen can cycle through tracks without
        # making the user back out first.
        try:
            self._nav_file_index = self._gpx_files.index(gpx_path)
        except ValueError:
            self._nav_file_index = 0
        self._nav_decimated = [(p.lat, p.lon)
                                for p in track.decimated(max_points=600)]
        self._nav_arc_m = 0.0
        self._nav_last_tick = None
        self._nav_open = True

    def _close_nav(self) -> None:
        self._nav_open = False
        self._nav_track = None
        self._nav_decimated = []
        self._nav_arc_m = 0.0
        self._nav_last_tick = None
        self._nav_missing_tiles = 0

    def _advance_simulation(self) -> None:
        """Advance the simulated bike along the track at the configured speed."""
        track = self._nav_track
        if track is None or track.total_m <= 0:
            return
        now = time.monotonic()
        if self._nav_last_tick is None:
            self._nav_last_tick = now
            return
        dt = now - self._nav_last_tick
        self._nav_last_tick = now
        # Loop the simulation when we hit the end so the screen stays
        # interesting even if the rider doesn't reset it.
        self._nav_arc_m = (self._nav_arc_m + self._nav_speed_mps * dt) % track.total_m

    def _draw_nav_screen(self, p: QPainter) -> None:
        track = self._nav_track
        assert track is not None

        self._advance_simulation()
        lat, lon, bearing = track.position_at_meters(self._nav_arc_m)

        cx = self.width / 2
        cy = self.height / 2

        # Solid base colour first so the round-dash bezel never reveals
        # raw pixels from a previous frame after rotation/perspective.
        p.fillRect(0, 0, self.width, self.height, QBrush(_BG_TOP))

        # ----- Build basemap (size depends on view mode) ------------
        # north-up: render exactly the canvas size, fastest path.
        # heading/behind: render to an oversized square so rotation
        # and the perspective trapezoid never expose black corners.
        if self._view_mode == "north":
            buf_w, buf_h = self.width, self.height
        else:
            buf_w = buf_h = 720

        basemap, project, missing = render_basemap(
            buf_w, buf_h, lat, lon, self._nav_zoom,
            self._tile_cache_dir, tile_cache=self._tile_cache_obj,
        )
        self._nav_missing_tiles = missing

        # Track polyline is drawn ONTO the basemap so it inherits the
        # same transform as the tiles below it.
        if self._nav_decimated:
            bp = QPainter(basemap)
            bp.setRenderHints(
                QPainter.RenderHint.Antialiasing
                | QPainter.RenderHint.SmoothPixmapTransform,
            )
            poly = QPainterPath()
            first = True
            for plat, plon in self._nav_decimated:
                px, py = project(plat, plon)
                if first:
                    poly.moveTo(px, py)
                    first = False
                else:
                    poly.lineTo(px, py)
            bp.setPen(QPen(QColor(0x10, 0x18, 0x2A, 200), 6,
                           Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap,
                           Qt.PenJoinStyle.RoundJoin))
            bp.drawPath(poly)
            bp.setPen(QPen(_ACCENT_A, 3,
                           Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap,
                           Qt.PenJoinStyle.RoundJoin))
            bp.drawPath(poly)
            bp.end()

        # Bike position on the canvas + how the avatar's heading arrow
        # should be rotated.  The 3D mode parks the bike in the lower
        # third of the screen so most of the canvas is "ahead".
        bike_x, bike_y = cx, cy
        arrow_rot = bearing
        if self._view_mode == "behind":
            bike_y = self.height * 0.72
            arrow_rot = 0.0
        elif self._view_mode == "heading":
            arrow_rot = 0.0

        # Let the map fill the full projected video surface. The bike's
        # physical display/bezel performs the actual round crop.
        if self._view_mode == "north":
            p.drawImage(0, 0, basemap)
        elif self._view_mode == "heading":
            self._blit_heading_up(p, basemap, buf_w, buf_h, cx, cy, bearing)
        else:  # "behind"
            self._blit_behind_view(p, basemap, buf_w, buf_h, cx, bike_y, bearing)

        # Subtle dim overlay so the HUD sits cleanly on top of the map.
        p.fillRect(0, 0, self.width, self.height, QBrush(QColor(0, 0, 0, 50)))

        # Bike avatar drawn AFTER the basemap blit (no transform), so it
        # is always crisp regardless of view mode.
        self._draw_bike_avatar(p, bike_x, bike_y, arrow_rot)

        # ----- HUD --------------------------------------------------
        # Top-left: MAP tag + zoom badge.
        self._draw_tag(p, "MAP", 14, 12, _ACCENT_GREEN)
        zoom_label = f"Z {self._nav_zoom}"
        if self._nav_zoom == self._NAV_ZOOM_MIN:
            zoom_label += " ·MIN"
        elif self._nav_zoom == self._NAV_ZOOM_MAX:
            zoom_label += " ·MAX"
        self._draw_tag(p, zoom_label, 14, 36, _ACCENT_A)

        # Compass — needle counter-rotates so N still points north.
        compass_rot = 0.0 if self._view_mode == "north" else -bearing
        self._draw_compass(p, self.width - 30, 24, rot_deg=compass_rot)

        # Bottom-left scale bar — auto-recomputed from zoom + latitude.
        self._draw_scale_bar(p, 16, self.height - 28, lat=lat)

        # Top-centre status pill: track name + speed.
        speed_kmh = self._nav_speed_mps * 3600 / 1000
        name = (self._nav_track_path.stem if self._nav_track_path else track.name)
        if len(name) > 22:
            name = name[:21] + "…"
        self._draw_status_pill(
            p, f"{name}   {speed_kmh:4.0f} km/h   {_VIEW_LABELS[self._view_mode]}",
        )

        # Bottom progress bar.
        progress = self._nav_arc_m / track.total_m if track.total_m > 0 else 0.0
        self._draw_progress_bar(p, progress, track.total_m)

        if missing > 0:
            self._draw_missing_tiles_warning(p, missing)

        self._draw_back_hint(p, "LEFT zoom out   RIGHT zoom in   DOWN back")

    # ----- View-mode blit helpers -----------------------------------

    def _blit_heading_up(
        self,
        p: QPainter,
        basemap: "QImage",
        buf_w: int, buf_h: int,
        cx: float, cy: float,
        bearing: float,
    ) -> None:
        """Heading-up: rotate basemap by -bearing around the bike pos."""
        xform = QTransform()
        xform.translate(cx, cy)
        xform.rotate(-bearing)
        xform.translate(-buf_w / 2, -buf_h / 2)
        p.save()
        p.setTransform(xform, True)
        p.drawImage(0, 0, basemap)
        p.restore()

    def _blit_behind_view(
        self,
        p: QPainter,
        basemap: "QImage",
        buf_w: int, buf_h: int,
        cx: float, bike_y: float,
        bearing: float,
    ) -> None:
        """Behind-bike: heading-up + a forward-looking trapezoid.

        Two-stage so the rotation and the perspective compose cleanly:
        1) Rotate the (oversized) basemap into a temporary buffer so its
           "up" direction matches the bike's heading.
        2) Map a forward-looking source rectangle from that buffer onto
           a trapezoid on the canvas (narrow at top = far away, wide at
           bottom = close to the rider).
        """
        # Stage 1 — rotate.
        rotated = QImage(buf_w, buf_h, QImage.Format.Format_RGB888)
        rotated.fill(_BG_TOP)
        rp = QPainter(rotated)
        rp.setRenderHints(
            QPainter.RenderHint.Antialiasing
            | QPainter.RenderHint.SmoothPixmapTransform,
        )
        rp.translate(buf_w / 2, buf_h / 2)
        rp.rotate(-bearing)
        rp.translate(-buf_w / 2, -buf_h / 2)
        rp.drawImage(0, 0, basemap)
        rp.end()

        # Stage 2 — perspective trapezoid.
        ahead_px = 320      # how far in front of the bike to sample
        behind_px = 60      # show a sliver of road behind the bike too
        src_half_w = 220
        src = QPolygonF([
            QPointF(buf_w / 2 - src_half_w, buf_h / 2 - ahead_px),  # top-left
            QPointF(buf_w / 2 + src_half_w, buf_h / 2 - ahead_px),  # top-right
            QPointF(buf_w / 2 + src_half_w, buf_h / 2 + behind_px),  # bot-right
            QPointF(buf_w / 2 - src_half_w, buf_h / 2 + behind_px),  # bot-left
        ])
        top_hw = 70         # narrow at the horizon
        bot_hw = 360        # wide near the bike
        top_y = 28
        bot_y = self.height + 6
        dst = QPolygonF([
            QPointF(cx - top_hw, top_y),
            QPointF(cx + top_hw, top_y),
            QPointF(cx + bot_hw, bot_y),
            QPointF(cx - bot_hw, bot_y),
        ])
        # PySide6's quadToQuad signature varies between versions —
        # handle both the (bool, QTransform) tuple form and the form
        # that returns just QTransform.
        result = QTransform.quadToQuad(src, dst)
        if isinstance(result, tuple):
            ok, xform = result
        else:
            xform = result
            ok = xform is not None
        if ok and xform is not None:
            p.save()
            p.setTransform(xform, True)
            p.drawImage(0, 0, rotated)
            p.restore()
        else:  # pragma: no cover — extremely unlikely with our quad geometry
            p.drawImage(
                int(cx - buf_w / 2),
                int(bike_y - buf_h / 2),
                rotated,
            )

    def _draw_bike_avatar(self, p: QPainter, cx: float, cy: float,
                          bearing_deg: float) -> None:
        # Soft outer pulse.
        pulse = (math.sin(time.monotonic() * 3.0) + 1) / 2
        outer = 12 + pulse * 4
        p.setBrush(QBrush(QColor(_ACCENT_C.red(),
                                  _ACCENT_C.green(),
                                  _ACCENT_C.blue(),
                                  int(50 + pulse * 60))))
        p.setPen(Qt.PenStyle.NoPen)
        p.drawEllipse(QPointF(cx, cy), outer, outer)

        # White ring + accent fill.
        p.setBrush(QBrush(QColor(255, 255, 255)))
        p.drawEllipse(QPointF(cx, cy), 9, 9)
        p.setBrush(QBrush(_ACCENT_C))
        p.drawEllipse(QPointF(cx, cy), 7, 7)

        # Heading arrow rotated by bearing (0° = north → -y).
        p.save()
        p.translate(cx, cy)
        p.rotate(bearing_deg)
        arrow = QPolygonF([
            QPointF(0, -16),
            QPointF(-5, -7),
            QPointF(0, -10),
            QPointF(5, -7),
        ])
        p.setBrush(QBrush(QColor(255, 255, 255)))
        p.setPen(QPen(_BG_TOP, 1))
        p.drawPolygon(arrow)
        p.restore()

    def _draw_status_pill(self, p: QPainter, text: str) -> None:
        p.setFont(self._font_meta)
        fm = p.fontMetrics()
        tw = fm.horizontalAdvance(text)
        w = tw + 22
        h = 18
        x = self.width / 2 - w / 2
        y = 50
        rect = QRectF(x, y, w, h)
        p.setBrush(QBrush(QColor(0, 0, 0, 140)))
        p.setPen(QPen(_HAIRLINE, 1))
        p.drawRoundedRect(rect, 9, 9)
        p.setPen(QPen(_TEXT_HI))
        p.drawText(
            int(rect.left() + 11),
            int(rect.center().y() + fm.ascent() / 2 - 1),
            text,
        )

    def _draw_progress_bar(self, p: QPainter, ratio: float,
                            total_m: float) -> None:
        ratio = max(0.0, min(1.0, ratio))
        y = self.height - 44
        x, w = self._safe_band(y, y + 8, extra_margin=8)
        p.setBrush(QBrush(QColor(255, 255, 255, 24)))
        p.setPen(Qt.PenStyle.NoPen)
        p.drawRoundedRect(QRectF(x, y, w, 4), 2, 2)
        p.setBrush(QBrush(_ACCENT_GREEN))
        p.drawRoundedRect(QRectF(x, y, w * ratio, 4), 2, 2)

        p.setFont(self._font_meta)
        p.setPen(QPen(_TEXT_LO))
        done_km = (ratio * total_m) / 1000.0
        total_km = total_m / 1000.0
        readout = f"{done_km:5.1f} / {total_km:5.1f} km"
        p.drawText(int(x), int(y - 3), readout)

    def _draw_missing_tiles_warning(self, p: QPainter, missing: int) -> None:
        text = f"{missing} tiles missing — run dash_ui.download_tiles"
        p.setFont(self._font_meta)
        fm = p.fontMetrics()
        tw = fm.horizontalAdvance(text)
        h = 16
        x = self.width / 2 - tw / 2 - 10
        y = 74
        rect = QRectF(x, y, tw + 20, h)
        p.setBrush(QBrush(QColor(_ACCENT_C.red(),
                                  _ACCENT_C.green(),
                                  _ACCENT_C.blue(), 50)))
        p.setPen(QPen(_ACCENT_C, 1))
        p.drawRoundedRect(rect, 8, 8)
        p.setPen(QPen(_ACCENT_C))
        p.drawText(int(rect.left() + 10),
                   int(rect.center().y() + fm.ascent() / 2 - 1), text)

    def _draw_compass(
        self, p: QPainter, cx: float, cy: float,
        *, rot_deg: float = 0.0,
    ) -> None:
        """Compass face anchored at (cx, cy).

        ``rot_deg`` rotates the needle (and the "N" label) counter to
        the map: when the map is rotated by -bearing in heading-up /
        behind modes, pass ``rot_deg=-bearing`` so the N still points
        towards true north on screen.
        """
        # Static dial (background pill stays put — the world rotates,
        # not the bezel).
        p.setBrush(QBrush(QColor(255, 255, 255, 18)))
        p.setPen(Qt.PenStyle.NoPen)
        p.drawEllipse(QPointF(cx, cy), 14, 14)

        # Rotated needle + label.
        p.save()
        p.translate(cx, cy)
        p.rotate(rot_deg)
        p.setPen(QPen(_TEXT_LO, 1))
        p.setBrush(QBrush(_ACCENT_PINK))
        north = QPolygonF([
            QPointF(0, -10), QPointF(-4, 1), QPointF(4, 1),
        ])
        p.drawPolygon(north)
        p.setBrush(QBrush(_TEXT_LO))
        south = QPolygonF([
            QPointF(0, 10), QPointF(-4, -1), QPointF(4, -1),
        ])
        p.drawPolygon(south)
        # "N" label — counter-rotate the *text* so it stays upright
        # while the needle is rotated relative to the screen.
        p.setFont(self._font_tag)
        p.setPen(QPen(_TEXT_HI))
        fm = p.fontMetrics()
        tw = fm.horizontalAdvance("N")
        # Position the text just above the north arrow head, in the
        # rotated frame, then rotate the painter back so the glyph
        # itself stays upright.
        p.save()
        p.translate(0, -13)
        p.rotate(-rot_deg)
        p.drawText(int(-tw / 2), 0, "N")
        p.restore()
        p.restore()

    def _draw_scale_bar(self, p: QPainter, x: int, y: int,
                         *, lat: float = 0.0) -> None:
        """Auto-scaled bar — picks a "nice" round distance for the current
        zoom + latitude.  Web-Mercator m/px = 156543.03·cos(lat) / 2^z.
        """
        mpp = 156543.03 * math.cos(math.radians(lat)) / (2 ** self._nav_zoom)
        if mpp <= 0:
            return
        target_px = 80
        target_m = target_px * mpp
        nice = (10, 20, 50, 100, 200, 500, 1000, 2000, 5000,
                10_000, 20_000, 50_000, 100_000)
        chosen_m = min(nice, key=lambda v: abs(v - target_m))
        bar_px = max(20, int(chosen_m / mpp))

        p.setPen(QPen(_TEXT_LO, 1.5, Qt.PenStyle.SolidLine, Qt.PenCapStyle.SquareCap))
        p.drawLine(x, y, x + bar_px, y)
        # Four ticks (start + 3 quarters + end).
        for i in range(5):
            tx = x + int(i * bar_px / 4)
            p.drawLine(tx, y - 3, tx, y + 3)

        if chosen_m < 1000:
            label = f"{chosen_m} m"
        else:
            km = chosen_m / 1000
            label = f"{km:g} km"
        p.setFont(self._font_meta)
        p.setPen(QPen(_TEXT_LO))
        p.drawText(x + bar_px + 6, y + 4, label)

    def _draw_tag(self, p: QPainter, text: str, x: int, y: int, colour: QColor) -> None:
        p.setFont(self._font_tag)
        fm = p.fontMetrics()
        tw = fm.horizontalAdvance(text)
        rect = QRectF(x, y, tw + 16, 18)
        p.setBrush(QBrush(QColor(colour.red(), colour.green(), colour.blue(), 40)))
        p.setPen(QPen(QColor(colour.red(), colour.green(), colour.blue(), 120), 1))
        p.drawRoundedRect(rect, 9, 9)
        p.setPen(QPen(colour))
        p.drawText(int(rect.left() + 8),
                   int(rect.center().y() + fm.ascent() / 2 - 1), text)

    # ----------------------------------------------------------- Settings

    def _draw_settings_screen(self, p: QPainter) -> None:
        self._draw_background(p)
        self._draw_top_bar(p)
        self._draw_tag(p, "SETTINGS", 14, 12, _ACCENT_B)

        # Per-setting display value + 0..1 ratio for the inline bar.
        view_pos = _VIEW_MODES.index(self._view_mode)
        values = {
            "View": (
                _VIEW_LABELS[self._view_mode],
                # Spread the dot across the bar — 0 / 0.5 / 1 for the 3 modes.
                view_pos / max(1, len(_VIEW_MODES) - 1),
            ),
        }

        y0 = 60
        row_h = 36
        gap = 4
        for i, label in enumerate(_SETTINGS_ROWS):
            top = y0 + i * (row_h + gap)
            x, w = self._safe_band(top, top + row_h, extra_margin=2)
            selected = i == self._settings_index
            if i == 0:
                self._draw_back_card(p, x, top, w, row_h, selected,
                                     subtitle="Close settings")
                continue
            self._draw_settings_card(
                p, label, *values[label], x, top, w, row_h, selected,
            )

        self._draw_back_hint(p, "LEFT/RIGHT navigate   DOWN selects")

    def _draw_settings_card(
        self,
        p: QPainter,
        label: str,
        value_str: str,
        ratio: float,
        x: int, y: int, w: int, h: int,
        selected: bool,
    ) -> None:
        if w <= 0:
            return
        rect = QRectF(x, y, w, h)

        if selected:
            for j, alpha in enumerate((22, 38, 70)):
                grow = (3 - j) * 4
                glow = rect.adjusted(-grow, -grow, grow, grow)
                p.setBrush(QBrush(QColor(_ACCENT_B.red(),
                                          _ACCENT_B.green(),
                                          _ACCENT_B.blue(), alpha)))
                p.setPen(Qt.PenStyle.NoPen)
                p.drawRoundedRect(glow, 14 + grow / 2, 14 + grow / 2)
            grad = QLinearGradient(rect.topLeft(), rect.bottomRight())
            grad.setColorAt(0.0, QColor(0x2A, 0x26, 0x42))
            grad.setColorAt(1.0, QColor(0x18, 0x16, 0x2C))
            p.setBrush(QBrush(grad))
        else:
            p.setBrush(QBrush(_CARD_BG))
        p.setPen(QPen(_HAIRLINE, 1))
        p.drawRoundedRect(rect, 10, 10)

        p.setFont(self._font_item)
        p.setPen(QPen(_TEXT_HI if selected else _TEXT_LO))
        fm = p.fontMetrics()
        p.drawText(int(rect.left() + 14),
                   int(rect.center().y() + fm.ascent() / 2 - 8), label)

        p.setFont(self._font_meta)
        p.setPen(QPen(_TEXT_DIM))
        p.drawText(int(rect.left() + 14), int(rect.bottom() - 8), value_str)

        # Mini bar on the right.
        bar_x = rect.right() - 90
        bar_y = rect.center().y() - 2
        p.setBrush(QBrush(QColor(255, 255, 255, 22)))
        p.setPen(Qt.PenStyle.NoPen)
        p.drawRoundedRect(QRectF(bar_x, bar_y, 80, 6), 3, 3)
        p.setBrush(QBrush(_ACCENT_B if selected else _ACCENT_A))
        p.drawRoundedRect(
            QRectF(bar_x, bar_y, 80 * max(0.0, min(1.0, ratio)), 6), 3, 3,
        )

        if selected:
            cx = rect.left()
            cy = rect.center().y()
            poly = QPolygonF([
                QPointF(cx - 7, cy - 5),
                QPointF(cx - 1, cy),
                QPointF(cx - 7, cy + 5),
            ])
            p.setBrush(QBrush(_ACCENT_C))
            p.setPen(Qt.PenStyle.NoPen)
            p.drawPolygon(poly)

    # ----------------------------------------------------------- Video

    def _video_rect(self) -> tuple[int, int, int, int]:
        return (0, 0, self.width, self.height)

    def _start_video(self) -> None:
        if self._video_proc is not None or self._video_error is not None:
            return
        path = self._video_path
        if not path.is_absolute():
            path = Path.cwd() / path
        if not path.is_file():
            self._video_error = f"missing {self._video_path}"
            return
        ff = shutil.which("ffmpeg")
        if ff is None:
            self._video_error = "ffmpeg not found"
            return
        x, y, w, h = self._video_rect()
        del x, y
        self._video_stop.clear()
        cmd = [
            ff, "-hide_banner", "-loglevel", "error",
            "-re", "-stream_loop", "-1", "-i", str(path),
            "-an",
            "-vf",
            f"scale={w}:{h}:force_original_aspect_ratio=increase,"
            f"crop={w}:{h},fps={self.fps}",
            "-pix_fmt", "rgb24",
            "-f", "rawvideo", "pipe:1",
        ]
        try:
            self._video_proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0,
            )
        except OSError as exc:
            self._video_error = f"video error: {exc}"
            return
        self._video_thr = threading.Thread(
            target=self._video_reader_loop, args=(self._video_proc, w, h),
            name="dash-ui-qt-video", daemon=True,
        )
        self._video_thr.start()

    def _stop_video(self) -> None:
        self._video_stop.set()
        proc = self._video_proc
        self._video_proc = None
        self._last_video_frame = None
        if proc is None:
            return
        try:
            proc.terminate()
            proc.wait(timeout=0.5)
        except subprocess.TimeoutExpired:
            proc.kill()
        except OSError:
            pass
        thr = self._video_thr
        self._video_thr = None
        if thr is not None and thr is not threading.current_thread():
            thr.join(timeout=0.5)

    def _video_reader_loop(self, proc, w: int, h: int) -> None:
        stdout = proc.stdout
        if stdout is None:
            self._video_error = "video stdout missing"
            return
        frame_len = w * h * 3
        buf = bytearray()
        try:
            fd = stdout.fileno()
            while not self._video_stop.is_set():
                chunk = os.read(fd, frame_len - len(buf))
                if not chunk:
                    break
                buf.extend(chunk)
                if len(buf) == frame_len:
                    with self._video_lock:
                        self._last_video_frame = bytes(buf)
                    buf.clear()
        except OSError as exc:
            if not self._video_stop.is_set():
                self._video_error = f"video read: {exc}"
        finally:
            if not self._video_stop.is_set():
                stderr = b""
                try:
                    proc.wait(timeout=0.2)
                    if proc.stderr is not None:
                        stderr = proc.stderr.read(240)
                except (OSError, subprocess.TimeoutExpired):
                    pass
                msg = stderr.decode(errors="replace").strip().splitlines()
                if msg:
                    self._video_error = msg[-1][:80]
                elif self._last_video_frame is None:
                    self._video_error = "video stopped"

    def _draw_video(self, p: QPainter) -> None:
        p.fillRect(0, 0, self.width, self.height, QBrush(QColor(0, 0, 0)))
        x, y, w, h = self._video_rect()
        self._start_video()
        with self._video_lock:
            frame = self._last_video_frame
        if frame is not None:
            # Build a QImage view over the frame bytes (RGB888, no padding
            # because ffmpeg writes width*3 per row).
            img = QImage(frame, w, h, w * 3, QImage.Format.Format_RGB888)
            p.drawImage(x, y, img)
        else:
            p.setBrush(QBrush(_CARD_BG))
            p.setPen(QPen(_ACCENT_A, 2))
            p.drawRoundedRect(QRectF(x + 40, y + 40, w - 80, h - 80), 12, 12)
            p.setFont(self._font_meta)
            p.setPen(QPen(_TEXT_HI))
            fm = p.fontMetrics()
            msg = self._video_error or "opening video..."
            tw = fm.horizontalAdvance(msg)
            p.drawText(int(self.width / 2 - tw / 2),
                       int(self.height / 2 + fm.ascent() / 2), msg)

        self._draw_tag(p, "VIDEO", 14, 12, _ACCENT_A)
        self._draw_back_hint(p, "DOWN closes")

    # ----------------------------------------------------------- Misc HUD

    def _draw_hint_bar(self, p: QPainter) -> None:
        # Top-menu legend — only shows the three buttons this UI uses.
        y = self.height - 24
        x, w = self._safe_band(y, y + 18, extra_margin=4)
        names = ("LEFT", "RIGHT", "DOWN")
        slot = w / len(names)
        for i, name in enumerate(names):
            colour = _BUTTON_COLOURS.get(name, _ACCENT_A)
            cx = x + slot * (i + 0.5)
            p.setBrush(QBrush(QColor(colour.red(), colour.green(), colour.blue(), 32)))
            p.setPen(QPen(QColor(colour.red(), colour.green(), colour.blue(), 110), 1))
            p.drawRoundedRect(QRectF(cx - 24, y, 48, 16), 8, 8)
            p.setFont(self._font_tag)
            p.setPen(QPen(colour))
            fm = p.fontMetrics()
            tw = fm.horizontalAdvance(name)
            p.drawText(int(cx - tw / 2), int(y + fm.ascent() + 2), name)

    def _draw_back_hint(self, p: QPainter, text: str) -> None:
        y = self.height - 22
        p.setFont(self._font_meta)
        p.setPen(QPen(_TEXT_LO))
        fm = p.fontMetrics()
        tw = fm.horizontalAdvance(text)
        x, w = self._safe_band(y, y + 14, extra_margin=4)
        p.drawText(int(x + (w - tw) / 2), int(y + fm.ascent()), text)

    def _draw_button_badge(self, p: QPainter) -> None:
        if self._last_button_name is None:
            return
        age = time.monotonic() - self._last_button_at
        if age >= 1.2:
            return
        fade = max(0.0, 1.0 - age / 1.2)
        colour = _BUTTON_COLOURS.get(self._last_button_name, _ACCENT_A)
        c = QColor(colour.red(), colour.green(), colour.blue(), int(255 * fade))
        cx = self.width - 30
        cy = self.height - 56
        for grow, alpha in ((10, 30), (6, 60), (3, 110)):
            p.setBrush(QBrush(QColor(colour.red(), colour.green(), colour.blue(),
                                      int(alpha * fade))))
            p.setPen(Qt.PenStyle.NoPen)
            p.drawEllipse(QPointF(cx, cy), 12 + grow, 12 + grow)
        p.setBrush(QBrush(c))
        p.drawEllipse(QPointF(cx, cy), 12, 12)
        # Glyph in the centre.
        p.setPen(QPen(_BG_TOP, 1.6, Qt.PenStyle.SolidLine,
                      Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
        p.setBrush(Qt.BrushStyle.NoBrush)
        n = self._last_button_name
        if n == "LEFT":
            p.drawLine(QPointF(cx + 3, cy - 5), QPointF(cx - 4, cy))
            p.drawLine(QPointF(cx - 4, cy), QPointF(cx + 3, cy + 5))
        elif n == "RIGHT":
            p.drawLine(QPointF(cx - 3, cy - 5), QPointF(cx + 4, cy))
            p.drawLine(QPointF(cx + 4, cy), QPointF(cx - 3, cy + 5))
        elif n == "DOWN":
            p.drawLine(QPointF(cx - 5, cy - 3), QPointF(cx, cy + 4))
            p.drawLine(QPointF(cx, cy + 4), QPointF(cx + 5, cy - 3))
        elif n == "CLICK":
            p.setBrush(QBrush(_BG_TOP))
            p.drawEllipse(QPointF(cx, cy), 4, 4)

    def _draw_dash_safe_overlay(self, p: QPainter) -> None:
        """Faint ring marking the round-dash safe area (visible when windowed)."""
        if not self._headless:
            return  # don't clutter the local-test window
        # No-op on the streamed surface — the dash itself is the bezel.

    def _draw_calibration(self, p: QPainter) -> None:
        p.fillRect(0, 0, self.width, self.height, QBrush(QColor(0, 0, 0)))
        cx = self.width / 2
        cy = self.height / 2
        max_r = min(self.width, self.height) // 2

        p.setPen(QPen(QColor(0x16, 0x20, 0x30), 1))
        for x in range(0, self.width + 1, 10):
            p.drawLine(x, 0, x, self.height)
        for y in range(0, self.height + 1, 10):
            p.drawLine(0, y, self.width, y)
        p.setPen(QPen(QColor(0x2C, 0x40, 0x5C), 2))
        for x in range(0, self.width + 1, 50):
            p.drawLine(x, 0, x, self.height)
        for y in range(0, self.height + 1, 50):
            p.drawLine(0, y, self.width, y)

        for r in (25, 50, 75, 100, 125, 150):
            p.setPen(QPen(_ACCENT_A, 2 if r % 50 == 0 else 1))
            p.drawEllipse(QPointF(cx, cy), r, r)
        for deg in range(0, 360, 30):
            rad = math.radians(deg)
            x = cx + math.cos(rad) * max_r
            y = cy + math.sin(rad) * max_r
            p.setPen(QPen(QColor(0x22, 0x33, 0x4F), 1))
            p.drawLine(QPointF(cx, cy), QPointF(x, y))

        p.setPen(QPen(_ACCENT_C, 2))
        p.drawLine(int(cx), 0, int(cx), self.height)
        p.drawLine(0, int(cy), self.width, int(cy))
        p.setBrush(QBrush(_ACCENT_C))
        p.setPen(Qt.PenStyle.NoPen)
        p.drawEllipse(QPointF(cx, cy), 4, 4)

        self._draw_tag(p, "CALIBRATION", 14, 12, _ACCENT_C)
        p.setFont(self._font_meta)
        p.setPen(QPen(_TEXT_LO))
        p.drawText(14, self.height - 10, f"center {int(cx)},{int(cy)}  max r{max_r}")

    # =========================================================== Geometry

    def _safe_band(
        self, top: int, bottom: int, *, extra_margin: int = 0,
    ) -> tuple[int, int]:
        cx = self.width // 2
        cy = self.height // 2
        sample_ys = (top, (top + bottom) // 2, bottom)
        lefts: list[int] = []
        rights: list[int] = []
        for y in sample_ys:
            dy = abs(y - cy)
            if dy >= self._SAFE_RADIUS:
                half = 0
            else:
                half = int(math.sqrt(self._SAFE_RADIUS ** 2 - dy ** 2))
            half = max(0, half - self._SAFE_MARGIN - extra_margin)
            lefts.append(cx - half)
            rights.append(cx + half)
        left = max(lefts)
        right = min(rights)
        return left, max(0, right - left)

    def _item_hint(self, label: str) -> str:
        if label == MAP_ITEM:
            return "DOWN"
        if label == GPS_ITEM:
            return "DOWN"
        if label == SETTINGS_ITEM:
            return "DOWN"
        return ""

    def _launcher_title(self, label: str) -> str:
        if label == MAP_ITEM:
            return "OPEN MAP"
        if label == GPS_ITEM:
            return "GPS TRACKS"
        if label == SETTINGS_ITEM:
            return "SETTINGS"
        return label.upper()

    def _elided_text(self, p: QPainter, text: str, max_px: int, font: QFont) -> str:
        p.setFont(font)
        fm = p.fontMetrics()
        if fm.horizontalAdvance(text) <= max_px:
            return text
        ellipsis = "…"
        while text and fm.horizontalAdvance(text + ellipsis) > max_px:
            text = text[:-1]
        return text + ellipsis if text else ellipsis

    # =========================================================== Bytes out

    def _image_to_bytes(self) -> bytes:
        img = self._image
        bpl = img.bytesPerLine()
        row = self.width * 3
        # PySide6 returns memoryview-compatible from constBits; cast to bytes.
        raw = bytes(img.constBits())
        if bpl == row:
            return raw[:row * self.height]
        out = bytearray(row * self.height)
        for y in range(self.height):
            src = y * bpl
            dst = y * row
            out[dst:dst + row] = raw[src:src + row]
        return bytes(out)

    # =========================================================== Local-test

    def render_qimage(self) -> "QImage":
        """Render a frame and return the QImage (zero-copy view).

        Used by qt_local_test so it can ``drawImage`` the surface
        without a round-trip through bytes → QImage.
        """
        self.render_frame()
        return self._image
