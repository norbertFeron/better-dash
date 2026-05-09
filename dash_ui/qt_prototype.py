"""
Qt-based prototype — same wiring as ``dash_ui.prototype`` but uses
``QtRenderer`` (PySide6) instead of ``PygameRenderer``.

Usage::

    python -m dash_ui.qt_prototype --ssid RE_3NNH_240301 --fps 12

Both prototypes share the encoder + RTP packetizer + BikeLink, so any
flag tuned for one (--bitrate-kbps, --rtp-payload, …) carries over.
"""

from __future__ import annotations

import argparse
import signal
import sys
import time

from dash_ui.bike_link import BikeLink, BikeLinkConfig, Button
from dash_ui.qt_renderer import QtRenderer
from dash_ui.stream import DashUIStream


def _parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Tripper dash UI prototype — Qt renderer (modern look).",
    )
    p.add_argument("--ssid", default=None,
                   help="Dash Wi-Fi SSID (e.g. RE_3NNH_240301).")
    p.add_argument("--hostname", default="MacBook")
    p.add_argument("--bike-ip", default="192.168.1.1")
    p.add_argument("--broadcast", default="192.168.1.255")
    p.add_argument("--rtp-port", type=int, default=5000)
    p.add_argument("--fps", type=int, default=12,
                   help="UI / encoder frame rate (stock dash = 4 fps; 8-12 is responsive).")
    p.add_argument("--bitrate-kbps", type=int, default=300,
                   help="H.264 bitrate in kbps; 300-450 works well at 10-12 fps.")
    p.add_argument("--rtp-payload", type=int, default=1380)
    p.add_argument("--route-title", default="Pi Dash")
    p.add_argument("--no-auth", action="store_true")
    p.add_argument("--auth-timeout", type=float, default=8.0)
    p.add_argument("--calibration-grid", action="store_true")
    p.add_argument("--video-file", default="test_640.mp4")
    p.add_argument("--gpx-dir", default="gpx_files",
                   help="Folder scanned for .gpx files in the Map sub-menu.")
    p.add_argument("--tile-cache", default="tile_cache",
                   help="Slippy-map tile cache (use dash_ui.download_tiles to populate).")
    p.add_argument("--nav-zoom", type=int, default=14,
                   help="Zoom level for the navigation basemap (typical 13-15).")
    p.add_argument("--nav-speed-kmh", type=float, default=20.0,
                   help="Simulated bike speed along the GPX track (ignored when --gps-port is set).")
    p.add_argument("--gps-port", default=None,
                   help="Serial port for real GPS (e.g. /dev/ttyS0). Overrides the simulation.")
    p.add_argument("--fake-buttons", action="store_true")
    return p.parse_args(argv)


def main(argv: list[str]) -> int:
    args = _parse_args(argv)

    renderer = QtRenderer(
        title=f"Pi Dash → {args.bike_ip}",
        headless=True,
        calibration_grid=args.calibration_grid,
        video_file=args.video_file,
        fps=args.fps,
        gpx_dir=args.gpx_dir,
        tile_cache=args.tile_cache,
        nav_zoom=args.nav_zoom,
        nav_speed_kmh=args.nav_speed_kmh,
        gps_port=args.gps_port,
    )

    def on_button(btn: Button) -> None:
        renderer.inject_button(btn)

    link = BikeLink(
        BikeLinkConfig(
            ssid=args.ssid,
            hostname=args.hostname,
            bike_ip=args.bike_ip,
            broadcast=args.broadcast,
            route_title=args.route_title,
            no_auth=args.no_auth,
            auth_timeout=args.auth_timeout,
            projection_fps=float(args.fps),
        ),
        on_button=on_button,
    )

    stream = DashUIStream(
        renderer,
        bike_ip=args.bike_ip,
        rtp_port=args.rtp_port,
        max_rtp_payload=args.rtp_payload,
        bitrate=max(64, args.bitrate_kbps) * 1000,
    )

    interrupted = {"flag": False}

    def _sigint(_signum, _frame) -> None:
        interrupted["flag"] = True

    signal.signal(signal.SIGINT, _sigint)
    signal.signal(signal.SIGTERM, _sigint)

    try:
        link.start()
        if not link.connected and not args.no_auth:
            print(
                "[qt-prototype] handshake didn't reach a usable state; continuing.",
                file=sys.stderr,
            )
        stream.start()

        last_fake = time.monotonic()
        fake_seq = [Button.RIGHT, Button.LEFT, Button.DOWN, Button.LEFT]
        fake_idx = 0

        while not interrupted["flag"]:
            if not stream.running:
                print("[qt-prototype] stream stopped unexpectedly", file=sys.stderr)
                break
            if args.fake_buttons:
                now = time.monotonic()
                if now - last_fake >= 1.5:
                    btn = fake_seq[fake_idx % len(fake_seq)]
                    print(f"[qt-prototype] fake-button: {btn.name}", file=sys.stderr)
                    renderer.inject_button(btn)
                    fake_idx += 1
                    last_fake = now
            time.sleep(0.1)
    finally:
        try:
            stream.stop()
        finally:
            link.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
