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

    def nearest_arc_m(self, lat: float, lon: float) -> float:
        """Return the arc-length (m) of the track point closest to (lat, lon)."""
        if not self.points or not self.cumulative_m:
            return 0.0
        best_i = 0
        best_d = float("inf")
        for i, p in enumerate(self.points):
            d = haversine_m(lat, lon, p.lat, p.lon)
            if d < best_d:
                best_d = d
                best_i = i
        return self.cumulative_m[best_i]

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


def next_turn(
    track: Track,
    arc_m: float,
    *,
    lookahead_m: float = 1200.0,
    min_angle_deg: float = 25.0,
    smooth_m: float = 40.0,
) -> tuple[float, float] | None:
    """Return (distance_m, turn_angle_deg) for the next significant turn ahead.

    Computes the bearing change at each candidate point using a ±smooth_m window
    to filter out GPS jitter in dense tracks.  Returns None if no turn exceeding
    min_angle_deg is found within lookahead_m.

    turn_angle_deg is signed: positive = right, negative = left, range [-180, 180].
    """
    n = len(track.points)
    if n < 3 or not track.cumulative_m:
        return None

    cum = track.cumulative_m
    pts = track.points

    start_i = bisect.bisect_right(cum, arc_m)
    start_i = max(1, min(start_i, n - 2))
    limit_m = arc_m + lookahead_m

    for j in range(start_i, n - 1):
        if cum[j] > limit_m:
            break

        # Incoming bearing: from the point ~smooth_m before j.
        k = bisect.bisect_left(cum, cum[j] - smooth_m, 0, j + 1)
        k = max(0, min(k, j - 1))

        # Outgoing bearing: to the point ~smooth_m after j.
        m_idx = bisect.bisect_left(cum, cum[j] + smooth_m, j + 1, n)
        m_idx = min(m_idx, n - 1)
        if m_idx <= j:
            m_idx = min(j + 1, n - 1)

        if k == j or m_idx == j:
            continue

        bearing_in = initial_bearing_deg(
            pts[k].lat, pts[k].lon, pts[j].lat, pts[j].lon
        )
        bearing_out = initial_bearing_deg(
            pts[j].lat, pts[j].lon, pts[m_idx].lat, pts[m_idx].lon
        )
        delta = (bearing_out - bearing_in + 180.0) % 360.0 - 180.0

        if abs(delta) >= min_angle_deg:
            return (cum[j] - arc_m, delta)

    return None
