"""
DashUIStream — wires a Renderer → H264Encoder → RTP packetizer.

This is the single object callers need; everything else in dash_ui is an
implementation detail that can be swapped independently.

Thread model
    Main thread      : creates DashUIStream, calls start() / stop().
    render-feed thr  : calls renderer.render_frame() in a tight loop,
                       writes raw RGB24 bytes to encoder.stdin.
    rtp-packetizer   : reads H.264 Annex-B from encoder.stdout,
                       fragments into RTP and sends to <bike_ip>:<rtp_port>.

    Both background threads are daemon threads so they die automatically
    when the main process exits.

Example::

    from dash_ui import DashUIStream
    from dash_ui.pygame_renderer import PygameRenderer

    stream = DashUIStream(PygameRenderer(), bike_ip="192.168.1.1")
    stream.start()
    try:
        while stream.running:
            time.sleep(1)
    finally:
        stream.stop()
"""

from __future__ import annotations

import sys
import threading
import time

from dash_ui.encoder import H264Encoder
from dash_ui.renderer import DASH_BITRATE, DASH_FPS, DASH_HEIGHT, DASH_WIDTH, Renderer
from dash_ui.rtp import packetizer_loop


class DashUIStream:
    """
    Parameters
    ----------
    renderer:
        Any object satisfying the Renderer protocol (e.g. PygameRenderer).
    bike_ip:
        IP address of the dash's Wi-Fi AP (default: 192.168.1.1).
    rtp_port:
        UDP port on the dash that accepts H.264 RTP (default: 5000).
    max_rtp_payload:
        Max RTP payload size in bytes.  The packetizer subtracts 12 B
        (RTP header) internally.  Should stay ≤ 1380 to avoid IP
        fragmentation on the 192.168.1.x link.
    bitrate:
        H.264 target bitrate in bits/sec. The stock phone stream is ~205 kbps;
        higher FPS experiments may need more bits per second to avoid blur.
    """

    def __init__(
        self,
        renderer: Renderer,
        *,
        bike_ip: str = "192.168.1.1",
        rtp_port: int = 5000,
        max_rtp_payload: int = 1380,
        bitrate: int = DASH_BITRATE,
    ) -> None:
        self._renderer = renderer
        self._bike_ip = bike_ip
        self._rtp_port = rtp_port
        self._max_rtp_payload = max_rtp_payload
        self._bitrate = bitrate

        self._encoder: H264Encoder | None = None
        self._stop = threading.Event()
        self._feed_thr: threading.Thread | None = None
        self._rtp_thr: threading.Thread | None = None

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    @property
    def running(self) -> bool:
        """True while both background threads are alive and no stop was requested."""
        if self._stop.is_set():
            return False
        feed_ok = self._feed_thr is not None and self._feed_thr.is_alive()
        rtp_ok = self._rtp_thr is not None and self._rtp_thr.is_alive()
        return feed_ok and rtp_ok

    def start(self) -> None:
        """Start the encoder and both background threads."""
        if self._encoder is not None:
            raise RuntimeError("DashUIStream already started")

        self._stop.clear()
        self._encoder = H264Encoder(
            width=self._renderer.width,
            height=self._renderer.height,
            fps=self._renderer.fps,
            bitrate=self._bitrate,
        )
        self._encoder.start()

        # Thread 1: renderer → encoder stdin.
        self._feed_thr = threading.Thread(
            target=self._feed_loop,
            name="dash-ui-feed",
            daemon=True,
        )
        # Thread 2: encoder stdout → RTP packetizer → UDP.
        self._rtp_thr = threading.Thread(
            target=packetizer_loop,
            args=(self._encoder.stdout, self._bike_ip, self._rtp_port, self._stop),
            kwargs={"max_payload": self._max_rtp_payload, "max_fps": float(self._renderer.fps) * 2},
            name="dash-ui-rtp",
            daemon=True,
        )

        self._rtp_thr.start()
        self._feed_thr.start()
        print(
            f"[dash_ui] stream started → {self._bike_ip}:{self._rtp_port} "
            f"({self._renderer.width}x{self._renderer.height}@{self._renderer.fps}fps)",
            file=sys.stderr,
        )

    def stop(self) -> None:
        """Signal threads to stop, flush the encoder, and release resources."""
        self._stop.set()
        enc = self._encoder
        if self._feed_thr is not None:
            self._feed_thr.join(timeout=1.0)
        if enc is not None:
            enc.stop()
        if self._feed_thr is not None:
            self._feed_thr.join(timeout=2.0)
        if self._rtp_thr is not None:
            self._rtp_thr.join(timeout=3.0)
        try:
            self._renderer.close()
        except Exception:
            pass
        self._encoder = None
        print("[dash_ui] stream stopped.", file=sys.stderr)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _feed_loop(self) -> None:
        """Render frames and write raw RGB24 to the encoder's stdin."""
        enc = self._encoder
        assert enc is not None
        frame_bytes = self._renderer.width * self._renderer.height * 3
        while not self._stop.is_set():
            try:
                frame = self._renderer.render_frame()
            except SystemExit:
                # Renderer window closed — treat as a clean stop.
                self._stop.set()
                break
            except Exception as exc:
                print(f"[dash_ui/feed] render_frame error: {exc}", file=sys.stderr)
                self._stop.set()
                break

            if len(frame) != frame_bytes:
                print(
                    f"[dash_ui/feed] bad frame size {len(frame)} "
                    f"(expected {frame_bytes}), skipping",
                    file=sys.stderr,
                )
                continue

            if not enc.running:
                print("[dash_ui/feed] encoder exited unexpectedly", file=sys.stderr)
                self._stop.set()
                break

            try:
                enc.stdin.write(frame)
                enc.stdin.flush()
            except (AssertionError, OSError, ValueError) as exc:
                print(f"[dash_ui/feed] encoder stdin write failed: {exc}", file=sys.stderr)
                self._stop.set()
                break

        # Close stdin so ffmpeg flushes and exits cleanly.
        try:
            enc.stdin.close()
        except (AssertionError, OSError, ValueError):
            pass
        print("[dash_ui/feed] feed loop exited", file=sys.stderr)
