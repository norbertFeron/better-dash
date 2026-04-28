"""
Pre-download OSM tiles for offline use on the bike.

Three planning modes:

  --mode corridor   (default)
        Tiles within ``--buffer-km`` of every track point.
        Tightest set, but the dash will show "tiles missing" the
        moment the rider deviates more than --buffer-km off-route.

  --mode bbox
        Every tile inside the lat/lon bounding box of the GPX (or the
        ``--region`` argument), inflated by ``--buffer-km``.
        Survives detours, fuel stops, and getting lost.  Bigger
        download.

  --mode region   (implicit when --region is given without GPX)
        Same as bbox but the bounding box comes only from --region —
        no GPX file needed.  Use this for "I'll wander around this
        area for a few days".

Examples::

    # Tight corridor along one route, 1 km on each side, zooms 13+14:
    python -m dash_ui.download_tiles "gpx_files/Leh-Manali.gpx"

    # Whole bounding box of the route + 5 km, more forgiving:
    python -m dash_ui.download_tiles "gpx_files/Leh-Manali.gpx" \
        --mode bbox --buffer-km 5

    # Pre-cache an explicit region (no GPX), great before a holiday:
    python -m dash_ui.download_tiles \
        --region 32.4,76.9,34.2,78.0 --zoom 13 --zoom 14

    # Multiple GPX files at once, anything in tile_cache/ already is skipped:
    python -m dash_ui.download_tiles gpx_files/  --mode bbox --buffer-km 3

Why local caching: the dash has no internet, and OSM's public tile
servers ask that bulk users either run their own tile server or use a
commercial provider.  Pre-caching only what we need keeps us inside
the politeness envelope and keeps the bike usable on any network.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from dash_ui.gpx import parse_gpx
from dash_ui.tiles import (
    OSM_TILE_URL,
    download_tiles,
    expand_bbox_km,
    is_tile_cached,
    tiles_along_corridor,
    tiles_along_points,
    tiles_in_bbox,
)


def _parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Download OSM tiles for offline navigation.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument(
        "gpx", nargs="*",
        help="One or more .gpx files (or directories — recursed).",
    )
    p.add_argument(
        "--mode", choices=("corridor", "bbox"), default="corridor",
        help=(
            "How to compute the tile set.  corridor: tiles within "
            "--buffer-km of each track point (tight). "
            "bbox: every tile inside the GPX bounding box (loose). "
            "Default: corridor."
        ),
    )
    p.add_argument(
        "--buffer-km", type=float, default=1.0,
        help="Real-world buffer in km (default: 1).",
    )
    p.add_argument(
        "--region", default=None, metavar="LAT1,LON1,LAT2,LON2",
        help=(
            "Explicit lat/lon bounding box.  Forces --mode bbox if no "
            "GPX is given.  Combined with GPX files when both are "
            "passed (union of tile sets)."
        ),
    )
    p.add_argument(
        "--zoom", type=int, action="append",
        help="Zoom level(s).  Repeat for multiple.  Default: 13 14.",
    )
    p.add_argument(
        "--padding", type=int, default=None,
        help=(
            "(Legacy) Tile-units padding around each track point in "
            "corridor mode.  When set, overrides --buffer-km."
        ),
    )
    p.add_argument(
        "--cache-dir", default="tile_cache",
        help="Where to put the tile pyramid.  Default: ./tile_cache",
    )
    p.add_argument(
        "--delay-ms", type=int, default=80,
        help="Sleep between *uncached* fetches (ms).  Default: 80.",
    )
    p.add_argument(
        "--base-url", default=OSM_TILE_URL,
        help="Tile URL template ({z}/{x}/{y}).",
    )
    p.add_argument(
        "--max-tiles", type=int, default=10_000,
        help=(
            "Refuse to fetch more than this many *new* tiles in one "
            "run (safety net against accidental world-downloads). "
            "Set to 0 to disable.  Default: 10000."
        ),
    )
    p.add_argument(
        "--dry-run", action="store_true",
        help="Plan only — print the tile count, don't download.",
    )
    return p.parse_args(argv)


def _expand_inputs(inputs: list[str]) -> list[Path]:
    out: list[Path] = []
    for s in inputs:
        path = Path(s)
        if path.is_dir():
            out.extend(sorted(path.rglob("*.gpx")))
        elif path.is_file():
            out.append(path)
        else:
            print(f"WARN: skipping missing path {path}", file=sys.stderr)
    return out


def _parse_region(s: str) -> tuple[float, float, float, float]:
    parts = [float(x.strip()) for x in s.split(",")]
    if len(parts) != 4:
        raise ValueError("expected 4 comma-separated floats")
    a, b, c, d = parts
    return (min(a, c), min(b, d), max(a, c), max(b, d))


def main(argv: list[str]) -> int:
    args = _parse_args(argv)
    zooms = sorted(set(args.zoom)) if args.zoom else [13, 14]
    cache = Path(args.cache_dir)

    region_bbox: tuple[float, float, float, float] | None = None
    if args.region:
        try:
            region_bbox = _parse_region(args.region)
        except ValueError as exc:
            print(
                f"ERROR: --region must be 'lat1,lon1,lat2,lon2' ({exc})",
                file=sys.stderr,
            )
            return 2

    gpx_files = _expand_inputs(args.gpx)
    if not gpx_files and region_bbox is None:
        print("ERROR: pass either GPX files or --region.", file=sys.stderr)
        return 2

    # If only --region was given, force bbox mode.
    mode = args.mode
    if region_bbox is not None and not gpx_files:
        mode = "bbox"

    print(
        f"Mode: {mode}  (buffer {args.buffer_km:g} km, zooms {zooms})",
        file=sys.stderr,
    )

    all_tiles: set[tuple[int, int, int]] = set()

    # ---- Plan tiles for each GPX file ------------------------------
    for gpx_path in gpx_files:
        try:
            track = parse_gpx(gpx_path)
        except Exception as exc:
            print(f"ERROR parsing {gpx_path}: {exc}", file=sys.stderr)
            continue
        if not track.points:
            print(f"WARN: {gpx_path.name} has no track points", file=sys.stderr)
            continue
        per_track: set[tuple[int, int, int]] = set()
        if mode == "corridor":
            coords = [(pt.lat, pt.lon) for pt in track.points]
            for z in zooms:
                if args.padding is not None:
                    per_track |= tiles_along_points(coords, z, padding=args.padding)
                else:
                    per_track |= tiles_along_corridor(
                        coords, z, buffer_km=args.buffer_km,
                    )
        else:  # bbox
            min_lat, min_lon, max_lat, max_lon = track.bbox
            min_lat, min_lon, max_lat, max_lon = expand_bbox_km(
                min_lat, min_lon, max_lat, max_lon, args.buffer_km,
            )
            for z in zooms:
                per_track |= tiles_in_bbox(min_lat, min_lon, max_lat, max_lon, z)
            print(
                f"  bbox after +{args.buffer_km:g} km buffer: "
                f"{(max_lat - min_lat) * 111.32:.0f} km × "
                f"{(max_lon - min_lon) * 111.32:.0f} km",
                file=sys.stderr,
            )
        all_tiles |= per_track
        print(
            f"  {gpx_path.name}: {len(track.points)} pts, "
            f"{track.total_m / 1000:.1f} km, +{len(per_track)} tiles",
            file=sys.stderr,
        )

    # ---- Plan tiles for an explicit --region -----------------------
    if region_bbox is not None:
        min_lat, min_lon, max_lat, max_lon = expand_bbox_km(
            *region_bbox, args.buffer_km,
        )
        region_tiles: set[tuple[int, int, int]] = set()
        for z in zooms:
            region_tiles |= tiles_in_bbox(min_lat, min_lon, max_lat, max_lon, z)
        all_tiles |= region_tiles
        print(
            f"  region: {(max_lat - min_lat) * 111.32:.0f} km × "
            f"{(max_lon - min_lon) * 111.32:.0f} km, "
            f"+{len(region_tiles)} tiles",
            file=sys.stderr,
        )

    if not all_tiles:
        print("Nothing to plan — empty tile set.", file=sys.stderr)
        return 1

    # ---- Cache hit accounting + safety net -------------------------
    cache.mkdir(parents=True, exist_ok=True)
    to_fetch = [t for t in all_tiles if not is_tile_cached(*t, cache_dir=cache)]
    cached_count = len(all_tiles) - len(to_fetch)

    bytes_est_mb = len(to_fetch) * 25 / 1024  # OSM tiles ~25 KB each
    minutes_est = len(to_fetch) * args.delay_ms / 1000 / 60
    print(
        f"Plan: {len(all_tiles)} tiles total — "
        f"{len(to_fetch)} to download, {cached_count} already cached.",
        file=sys.stderr,
    )
    if to_fetch:
        print(
            f"  Estimated: {bytes_est_mb:.1f} MB, ~{minutes_est:.1f} min "
            f"at {args.delay_ms} ms/tile",
            file=sys.stderr,
        )

    if args.max_tiles > 0 and len(to_fetch) > args.max_tiles:
        print(
            f"REFUSING: {len(to_fetch)} new tiles exceeds "
            f"--max-tiles {args.max_tiles}.\n"
            f"Either lower --buffer-km, drop a zoom level, or pass a "
            f"higher --max-tiles.",
            file=sys.stderr,
        )
        return 1

    if args.dry_run:
        print("--dry-run: no tiles fetched.", file=sys.stderr)
        return 0

    if not to_fetch:
        print("All tiles already cached. Nothing to do.", file=sys.stderr)
        return 0

    # ---- Actually download -----------------------------------------
    def on_progress(i: int, n: int, dl: int, c: int, f: int) -> None:
        if i == n or i % 25 == 0:
            print(
                f"  [{i:5d}/{n}] downloaded={dl} cached={c} failed={f}",
                file=sys.stderr,
                flush=True,
            )

    dl, cached, failed = download_tiles(
        sorted(all_tiles),
        cache_dir=cache,
        delay_s=args.delay_ms / 1000.0,
        base_url=args.base_url,
        on_progress=on_progress,
    )
    print(
        f"Done. downloaded={dl}, already-cached={cached}, failed={failed}\n"
        f"Cache: {cache.resolve()}",
        file=sys.stderr,
    )
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
