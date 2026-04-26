"""
Prototype entry point — wires BikeLink + DashUIStream + PygameRenderer.

Usage::

    python -m dash_ui.prototype --ssid RE_3NNH_240301

What it does
    1. Opens UDP/2000 + UDP/2002 and runs the K1G handshake against the
       dash (RSA/AES auth, route card, q3c.z2, projection heartbeat,
       nav-info, route-card keep-alive, 1 Hz status tick).
    2. Spins up the H.264 encoder + custom RTP packetizer, with the
       interactive PygameRenderer as the pixel source.
    3. Listens for bike-button events on UDP/2002 and forwards them into
       the renderer's queue, so LEFT/RIGHT/DOWN/CLICK on the dash drive
       the on-screen menu.
    4. On Ctrl+C / SIGINT, sends q3c.h + q3c.x and joins all threads.

Run on the Mac that's joined to the dash's Wi-Fi AP (SSID matches the
sticker on the dash).  See AGENTS.md / PROTOCOL.md for protocol notes.
"""

from __future__ import annotations

import argparse
import signal
import sys
import time

from dash_ui.bike_link import BikeLink, BikeLinkConfig, Button
from dash_ui.pygame_renderer import PygameRenderer
from dash_ui.stream import DashUIStream


def _parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Tripper dash UI prototype (stream + button input)",
    )
    p.add_argument("--ssid", default=None,
                   help="Dash Wi-Fi SSID (e.g. RE_3NNH_240301). Required for auth.")
    p.add_argument("--hostname", default="MacBook")
    p.add_argument("--bike-ip", default="192.168.1.1")
    p.add_argument("--broadcast", default="192.168.1.255")
    p.add_argument("--rtp-port", type=int, default=5000)
    p.add_argument("--fps", type=int, default=8,
                   help="UI stream frame rate. Use --fps 4 to return to the stock dash cadence.")
    p.add_argument("--bitrate-kbps", type=int, default=205,
                   help="H.264 bitrate in kbps. Try 300-450 at 10-12fps if the dash stays stable.")
    p.add_argument("--rtp-payload", type=int, default=1380,
                   help="Max RTP payload bytes. Lower to 1000/1200 if Wi-Fi loss appears.")
    p.add_argument("--route-title", default="Pi Dash")
    p.add_argument("--no-auth", action="store_true",
                   help="Skip RSA handshake (for protocol experiments only).")
    p.add_argument("--auth-timeout", type=float, default=8.0)
    p.add_argument("--windowed", action="store_true",
                   help="Show the renderer in a Mac window in addition to streaming.")
    p.add_argument("--calibration-grid", action="store_true",
                   help="Stream a grid with concentric circles for photographing the round dash crop.")
    p.add_argument("--video-file", default="test_640.mp4",
                   help="Video file opened by the Video menu item when pressing DOWN.")
    p.add_argument("--fake-buttons", action="store_true",
                   help="Inject LEFT/RIGHT/DOWN/CLICK events on a timer so you can verify the UI without the bike.")
    return p.parse_args(argv)


def main(argv: list[str]) -> int:
    args = _parse_args(argv)

    # Renderer is created on the main thread; pygame display init must
    # happen here on macOS even when SDL_VIDEODRIVER=dummy.
    renderer = PygameRenderer(
        title=f"Pi Dash → {args.bike_ip}",
        headless=not args.windowed,
        calibration_grid=args.calibration_grid,
        video_file=args.video_file,
        fps=args.fps,
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
                "[prototype] handshake didn't reach a usable state; "
                "video may be ignored by the dash. Continuing anyway.",
                file=sys.stderr,
            )
        stream.start()

        # Optional self-test: rotate through buttons every 1.5s.
        last_fake = time.monotonic()
        fake_seq = [Button.RIGHT, Button.DOWN, Button.DOWN, Button.LEFT]
        fake_idx = 0

        while not interrupted["flag"]:
            if not stream.running:
                print("[prototype] stream stopped unexpectedly", file=sys.stderr)
                break
            if args.fake_buttons:
                now = time.monotonic()
                if now - last_fake >= 1.5:
                    btn = fake_seq[fake_idx % len(fake_seq)]
                    print(f"[prototype] fake-button: {btn.name}", file=sys.stderr)
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
