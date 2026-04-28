"""
Minimal GPX 1.0/1.1 parser + arc-length walker for the navigation
simulation.

Why no external dependency?
    `gpxpy` is the obvious pick, but for our purposes we only need
    track-point lat/lon and a way to walk along the track at a constant
    speed.  Hand-rolling the parser keeps `dash_ui` free of yet another
    runtime dep — and lets us be lenient about the namespace
    (komoot exports GPX 1.1, Strava sometimes 1.0, gpsd 0.x emits no
    namespace at all).

Coordinate system
    All math uses the WGS-84 sphere with mean radius 6_371_008.8 m.
    Cumulative arc-length is computed along straight great-circle
    segments between consecutive points; for our 20 km/h indoor
    simulation that is plenty accurate.
"""

from __future__ import annotations

import bisect
import math
from dataclasses import dataclass, field
from pathlib import Path
from xml.etree import ElementTree as ET

# Mean Earth radius (IUGG).
EARTH_R_M = 6_371_008.8


@dataclass(frozen=True)
class TrackPoint:
    lat: float
    lon: float


@dataclass
class Track:
    """A single GPX track, post-processed for arc-length lookups."""

    name: str
    points: list[TrackPoint]
    cumulative_m: list[float] = field(default_factory=list)

    def __post_init__(self) -> None:
        # Skip if the caller already supplied cumulative_m or the track
        # is too short to bother.
        if self.cumulative_m or len(self.points) < 2:
            return
        cum = [0.0]
        prev = self.points[0]
        for p in self.points[1:]:
            cum.append(cum[-1] + haversine_m(prev.lat, prev.lon, p.lat, p.lon))
            prev = p
        self.cumulative_m = cum

    @property
    def total_m(self) -> float:
        return self.cumulative_m[-1] if self.cumulative_m else 0.0

    @property
    def bbox(self) -> tuple[float, float, float, float]:
        """Return (min_lat, min_lon, max_lat, max_lon)."""
        lats = [p.lat for p in self.points]
        lons = [p.lon for p in self.points]
        return min(lats), min(lons), max(lats), max(lons)

    def position_at_meters(self, m: float) -> tuple[float, float, float]:
        """Linear-interpolate (lat, lon, bearing_deg) at arc-length m.

        Bearing is the direction from the segment start to its end —
        good enough as a heading marker for the bike avatar.
        """
        if not self.points:
            return (0.0, 0.0, 0.0)
        if len(self.points) == 1:
            p = self.points[0]
            return (p.lat, p.lon, 0.0)
        total = self.total_m
        if total <= 0.0:
            p = self.points[0]
            return (p.lat, p.lon, 0.0)

        m = max(0.0, min(m, total))
        # cumulative_m is monotonically non-decreasing; bisect_right
        # returns the index *after* m → segment index = that - 1.
        i = bisect.bisect_right(self.cumulative_m, m) - 1
        i = max(0, min(i, len(self.points) - 2))
        seg_start = self.cumulative_m[i]
        seg_end = self.cumulative_m[i + 1]
        seg_len = max(1e-6, seg_end - seg_start)
        t = (m - seg_start) / seg_len
        a = self.points[i]
        b = self.points[i + 1]
        lat = a.lat + (b.lat - a.lat) * t
        lon = a.lon + (b.lon - a.lon) * t
        bearing = initial_bearing_deg(a.lat, a.lon, b.lat, b.lon)
        return (lat, lon, bearing)

    def decimated(self, max_points: int = 600) -> list[TrackPoint]:
        """Return a thinned copy for cheap polyline rendering."""
        n = len(self.points)
        if n <= max_points:
            return list(self.points)
        step = n / max_points
        out: list[TrackPoint] = []
        i = 0.0
        while int(i) < n:
            out.append(self.points[int(i)])
            i += step
        # Always keep the last point so the polyline reaches the end.
        if out[-1] is not self.points[-1]:
            out.append(self.points[-1])
        return out


# ---------------------------------------------------------------------------
# Geodesic helpers
# ---------------------------------------------------------------------------

def haversine_m(a_lat: float, a_lon: float, b_lat: float, b_lon: float) -> float:
    p1 = math.radians(a_lat)
    p2 = math.radians(b_lat)
    dp = math.radians(b_lat - a_lat)
    dl = math.radians(b_lon - a_lon)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_R_M * math.asin(math.sqrt(h))


def initial_bearing_deg(
    a_lat: float, a_lon: float, b_lat: float, b_lon: float
) -> float:
    p1 = math.radians(a_lat)
    p2 = math.radians(b_lat)
    dl = math.radians(b_lon - a_lon)
    y = math.sin(dl) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return (math.degrees(math.atan2(y, x)) + 360.0) % 360.0


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

def _strip_ns(tag: str) -> str:
    return tag.split("}", 1)[1] if tag.startswith("{") else tag


def parse_gpx(path: Path) -> Track:
    """Parse one .gpx file and return its first non-empty track.

    Falls back to <rtept> then <wpt> if no <trkpt> is found.
    """
    tree = ET.parse(str(path))
    root = tree.getroot()

    # Track name (first <trk>/<name> if present, else <metadata>/<name>,
    # else the filename stem).
    name: str | None = None
    for el in root.iter():
        if _strip_ns(el.tag) == "name" and el.text:
            name = el.text.strip()
            break
    if not name:
        name = path.stem

    points: list[TrackPoint] = []
    # Prefer <trkpt>, fall back to <rtept>, then <wpt>.
    for tag in ("trkpt", "rtept", "wpt"):
        for el in root.iter():
            if _strip_ns(el.tag) != tag:
                continue
            try:
                lat = float(el.get("lat"))
                lon = float(el.get("lon"))
            except (TypeError, ValueError):
                continue
            points.append(TrackPoint(lat, lon))
        if points:
            break

    return Track(name=name, points=points)


def list_gpx(folder: Path) -> list[Path]:
    """Return all .gpx files in `folder`, sorted by name. Empty if missing."""
    if not folder.is_dir():
        return []
    return sorted(folder.glob("*.gpx"))
