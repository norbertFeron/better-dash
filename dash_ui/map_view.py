"""
MapView — composite cached XYZ tiles into a single QImage centered on
a (lat, lon) and return a projector that maps further (lat, lon) pairs
to canvas pixel coords.

The renderer composes:
    1. A flat fallback colour (so missing tiles don't show black holes).
    2. The 4-9 tiles that intersect the canvas.
    3. (Caller draws the GPX polyline + bike avatar on top.)

In-memory tile cache
    Every loaded PNG is decoded into a QImage and kept in a small LRU
    so we don't re-decode the same tile each frame as the bike moves.
    Capped at 64 tiles by default — enough for the 9-tile working set
    plus recent neighbours.
"""

from __future__ import annotations

import collections
from pathlib import Path
from typing import Callable

from PySide6.QtCore import QPointF
from PySide6.QtGui import QColor, QImage, QPainter

from dash_ui.tiles import TILE_SIZE, deg2num_float, tile_path


class TileCache:
    """Tiny LRU keyed by (z, x, y) → QImage."""

    def __init__(self, capacity: int = 64) -> None:
        self.capacity = capacity
        self._items: "collections.OrderedDict[tuple[int, int, int], QImage]" = (
            collections.OrderedDict()
        )

    def get(self, key: tuple[int, int, int], cache_dir: Path) -> QImage | None:
        img = self._items.get(key)
        if img is not None:
            self._items.move_to_end(key)
            return img
        z, x, y = key
        path = tile_path(z, x, y, cache_dir)
        if not path.is_file():
            return None
        img = QImage(str(path))
        if img.isNull():
            return None
        if img.width() != TILE_SIZE or img.height() != TILE_SIZE:
            img = img.scaled(TILE_SIZE, TILE_SIZE)
        # Convert to RGB888 once so the per-frame composite call doesn't
        # do format conversion on every drawImage.
        if img.format() != QImage.Format.Format_RGB888:
            img = img.convertToFormat(QImage.Format.Format_RGB888)
        self._items[key] = img
        if len(self._items) > self.capacity:
            self._items.popitem(last=False)
        return img


def render_basemap(
    width: int,
    height: int,
    center_lat: float,
    center_lon: float,
    zoom: int,
    cache_dir: Path,
    *,
    tile_cache: TileCache | None = None,
    fallback: QColor | None = None,
) -> tuple[QImage, Callable[[float, float], tuple[float, float]], int]:
    """
    Composite a basemap and return ``(image, project, missing_tiles)``.

    ``project(lat, lon)`` returns ``(canvas_x, canvas_y)`` in pixels
    (top-left origin), useful for drawing overlays in the same frame.
    ``missing_tiles`` is the count of tiles that should have covered the
    canvas but were not found on disk — surface this to the user when
    it's > 0 so they know to run ``download_tiles`` first.
    """
    if tile_cache is None:
        tile_cache = TileCache()

    # World pixel coordinates of the centre point.
    cx_t, cy_t = deg2num_float(center_lat, center_lon, zoom)
    cx_w = cx_t * TILE_SIZE
    cy_w = cy_t * TILE_SIZE
    # Top-left of the canvas in world pixels.
    tl_x = cx_w - width / 2.0
    tl_y = cy_w - height / 2.0

    img = QImage(width, height, QImage.Format.Format_RGB888)
    img.fill(fallback or QColor(0x10, 0x14, 0x24))

    p = QPainter(img)
    try:
        first_tx = int(tl_x // TILE_SIZE)
        first_ty = int(tl_y // TILE_SIZE)
        last_tx = int((tl_x + width - 1) // TILE_SIZE)
        last_ty = int((tl_y + height - 1) // TILE_SIZE)
        n = 1 << zoom
        missing = 0
        for tx in range(first_tx, last_tx + 1):
            for ty in range(first_ty, last_ty + 1):
                # Wrap x around the date line; clamp y at the poles.
                wrapped_x = tx % n
                if ty < 0 or ty >= n:
                    missing += 1
                    continue
                tile = tile_cache.get((zoom, wrapped_x, ty), cache_dir)
                if tile is None:
                    missing += 1
                    continue
                dx = tx * TILE_SIZE - tl_x
                dy = ty * TILE_SIZE - tl_y
                p.drawImage(QPointF(dx, dy), tile)
    finally:
        p.end()

    def project(lat: float, lon: float) -> tuple[float, float]:
        wx, wy = deg2num_float(lat, lon, zoom)
        return (wx * TILE_SIZE - tl_x, wy * TILE_SIZE - tl_y)

    return img, project, missing
