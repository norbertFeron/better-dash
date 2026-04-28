"""
Slippy-map XYZ tile coordinate math, on-disk cache, and a polite
downloader.

We use the standard OSM tile scheme:

    https://tile.openstreetmap.org/{z}/{x}/{y}.png

Each tile is 256x256 PNG.  Tiles are cached on disk under

    <cache_dir>/<z>/<x>/<y>.png

so the runtime only needs the cache directory; it never hits the
network if the tiles are already there.

OSM usage policy reminder
    The public OSM tile servers are donation-funded and ask that bulk
    downloads be polite (User-Agent header, light request rate, only
    download what you actually need).  The CLI (`download_tiles.py`)
    pre-fetches tiles for ONE GPX track at a time with a small delay
    between requests; for heavier use, point ``base_url`` at your own
    tile server (Mapbox / MapTiler / self-hosted).
"""

from __future__ import annotations

import math
import time
import urllib.request
from pathlib import Path
from typing import Callable, Iterable

OSM_TILE_URL = "https://tile.openstreetmap.org/{z}/{x}/{y}.png"
USER_AGENT = "better-dash/0.1 (offline-tile-cache)"
TILE_SIZE = 256


# ---------------------------------------------------------------------------
# Coordinate math
# ---------------------------------------------------------------------------

def deg2num(lat: float, lon: float, zoom: int) -> tuple[int, int]:
    """(lat, lon) → integer (tile_x, tile_y) at the given zoom."""
    lat_rad = math.radians(lat)
    n = 1 << zoom
    x = int((lon + 180.0) / 360.0 * n)
    y = int((1.0 - math.asinh(math.tan(lat_rad)) / math.pi) / 2.0 * n)
    return x, y


def deg2num_float(lat: float, lon: float, zoom: int) -> tuple[float, float]:
    """(lat, lon) → fractional (tile_x, tile_y) — useful for sub-tile placement."""
    lat_rad = math.radians(lat)
    n = 1 << zoom
    x = (lon + 180.0) / 360.0 * n
    y = (1.0 - math.asinh(math.tan(lat_rad)) / math.pi) / 2.0 * n
    return x, y


def num2deg(x: float, y: float, zoom: int) -> tuple[float, float]:
    """Inverse of deg2num — returns (lat, lon) in degrees."""
    n = 1 << zoom
    lon = x / n * 360.0 - 180.0
    lat = math.degrees(math.atan(math.sinh(math.pi * (1.0 - 2.0 * y / n))))
    return lat, lon


# ---------------------------------------------------------------------------
# Cache layout
# ---------------------------------------------------------------------------

def tile_path(z: int, x: int, y: int, cache_dir: Path) -> Path:
    return cache_dir / str(z) / str(x) / f"{y}.png"


def is_tile_cached(z: int, x: int, y: int, cache_dir: Path) -> bool:
    p = tile_path(z, x, y, cache_dir)
    return p.is_file() and p.stat().st_size > 0


# ---------------------------------------------------------------------------
# Network fetch
# ---------------------------------------------------------------------------

def fetch_tile(
    z: int, x: int, y: int,
    *,
    cache_dir: Path,
    base_url: str = OSM_TILE_URL,
    user_agent: str = USER_AGENT,
    timeout: float = 15.0,
) -> Path | None:
    """Download one tile if not cached.  Returns the path, or None on failure."""
    p = tile_path(z, x, y, cache_dir)
    if is_tile_cached(z, x, y, cache_dir):
        return p
    url = base_url.format(z=z, x=x, y=y)
    req = urllib.request.Request(url, headers={"User-Agent": user_agent})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = r.read()
    except Exception:
        return None
    if not data:
        return None
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    return p


# ---------------------------------------------------------------------------
# Bulk-download planning
# ---------------------------------------------------------------------------

def tiles_along_points(
    points: Iterable[tuple[float, float]],
    zoom: int,
    *,
    padding: int = 1,
) -> set[tuple[int, int, int]]:
    """Tiles covering each (lat, lon) point + padding tiles in each direction.

    Set semantics dedupe automatically — useful when many points fall in
    the same tile (urban areas, slow segments).

    The padding is in tile-units, so its real-world width SHRINKS as the
    zoom grows.  For a stable buffer width across zooms use
    ``tiles_along_corridor`` (km-based) instead.
    """
    out: set[tuple[int, int, int]] = set()
    n = 1 << zoom
    for lat, lon in points:
        x, y = deg2num(lat, lon, zoom)
        for dx in range(-padding, padding + 1):
            for dy in range(-padding, padding + 1):
                ny = y + dy
                if 0 <= ny < n:
                    out.add((zoom, (x + dx) % n, ny))
    return out


# ---------------------------------------------------------------------------
# Region helpers — bbox, km-buffer corridor
# ---------------------------------------------------------------------------

def km_to_tile_padding(km: float, lat: float, zoom: int) -> int:
    """How many tiles wide ``km`` is at the given latitude + zoom.

    Web-Mercator metres-per-pixel at lat L: ``156543.03·cos(L) / 2^z``.
    Multiply by ``TILE_SIZE`` to get metres per tile, then divide.
    """
    if km <= 0:
        return 0
    mpp = 156543.03 * math.cos(math.radians(lat)) / (2 ** zoom)
    tile_m = mpp * TILE_SIZE
    if tile_m <= 0:
        return 0
    return max(0, int(math.ceil(km * 1000.0 / tile_m)))


def tiles_along_corridor(
    points: Iterable[tuple[float, float]],
    zoom: int,
    *,
    buffer_km: float = 1.0,
) -> set[tuple[int, int, int]]:
    """Tiles within ``buffer_km`` of any track point, in real-world km.

    Unlike ``tiles_along_points``, the corridor width stays the same as
    you change zoom — at z=14 a 2 km buffer is 1 tile; at z=17 it's
    ~6 tiles.  This is what you want for navigation: the bike can wander
    a known number of metres off-route before hitting blank tiles.
    """
    out: set[tuple[int, int, int]] = set()
    n = 1 << zoom
    for lat, lon in points:
        pad = km_to_tile_padding(buffer_km, lat, zoom)
        x, y = deg2num(lat, lon, zoom)
        for dx in range(-pad, pad + 1):
            for dy in range(-pad, pad + 1):
                ny = y + dy
                if 0 <= ny < n:
                    out.add((zoom, (x + dx) % n, ny))
    return out


def expand_bbox_km(
    min_lat: float, min_lon: float,
    max_lat: float, max_lon: float,
    km: float,
) -> tuple[float, float, float, float]:
    """Inflate a (min_lat, min_lon, max_lat, max_lon) bbox by ``km`` in
    each direction.  Uses a flat-earth approximation that's fine for the
    tile-coverage decisions we make here (tens of km errors at the poles
    don't matter when we're just rounding to tile boundaries).
    """
    if km <= 0:
        return (min_lat, min_lon, max_lat, max_lon)
    dlat = km / 111.32
    avg_lat = (min_lat + max_lat) / 2.0
    dlon = km / (111.32 * max(0.05, math.cos(math.radians(avg_lat))))
    return (min_lat - dlat, min_lon - dlon, max_lat + dlat, max_lon + dlon)


def tiles_in_bbox(
    min_lat: float, min_lon: float,
    max_lat: float, max_lon: float,
    zoom: int,
) -> set[tuple[int, int, int]]:
    """Every XYZ tile that intersects the lat/lon bounding box.

    Note OSM tile-Y is flipped: lat increasing → tile-Y decreasing
    (north is up in the map; tile-Y 0 is the top of the world).
    """
    # NW corner → smallest tile-x, smallest tile-y.
    nw_x, nw_y = deg2num(max_lat, min_lon, zoom)
    se_x, se_y = deg2num(min_lat, max_lon, zoom)
    x_lo, x_hi = min(nw_x, se_x), max(nw_x, se_x)
    y_lo, y_hi = min(nw_y, se_y), max(nw_y, se_y)
    n = 1 << zoom
    out: set[tuple[int, int, int]] = set()
    for x in range(x_lo, x_hi + 1):
        for y in range(y_lo, y_hi + 1):
            if 0 <= y < n:
                out.add((zoom, x % n, y))
    return out


def download_tiles(
    tiles: Iterable[tuple[int, int, int]],
    *,
    cache_dir: Path,
    delay_s: float = 0.05,
    base_url: str = OSM_TILE_URL,
    user_agent: str = USER_AGENT,
    on_progress: Callable[[int, int, int, int, int], None] | None = None,
) -> tuple[int, int, int]:
    """Download all listed tiles into ``cache_dir``.

    Returns ``(downloaded, already_cached, failed)``.  ``on_progress`` is
    called as ``(i, total, downloaded, cached, failed)`` after each tile.
    """
    tiles_list = list(tiles)
    total = len(tiles_list)
    downloaded = cached = failed = 0
    cache_dir.mkdir(parents=True, exist_ok=True)
    for i, (z, x, y) in enumerate(tiles_list, start=1):
        if is_tile_cached(z, x, y, cache_dir):
            cached += 1
        else:
            ok = fetch_tile(
                z, x, y,
                cache_dir=cache_dir,
                base_url=base_url,
                user_agent=user_agent,
            )
            if ok is None:
                failed += 1
            else:
                downloaded += 1
                # Be polite to the public OSM server between *real*
                # network fetches; cached hits don't sleep.
                if delay_s > 0:
                    time.sleep(delay_s)
        if on_progress is not None:
            on_progress(i, total, downloaded, cached, failed)
    return downloaded, cached, failed
