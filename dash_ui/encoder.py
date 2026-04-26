"""
H264Encoder — feeds raw RGB24 frames into an ffmpeg subprocess and exposes
the Annex-B output as a readable pipe for the RTP packetizer.

Frame contract
    Caller writes `width * height * 3` bytes of packed RGB24 per frame to
    `encoder.stdin`.  ffmpeg reads rawvideo from stdin, converts to yuv420p,
    and encodes with the exact same flags the stock Tripper app uses
    (baseline 4.1, 526x300@4fps, 204kbps, GOP=4, one slice per picture).
    For the interactive prototype we additionally disable x264 lookahead so
    button-driven UI changes are emitted as soon as each frame is encoded.

Why a dedicated encoder module?
    The renderer (pygame / any future backend) should not need to know
    anything about ffmpeg.  H264Encoder is the narrow bridge between
    "pixels" and "Annex-B bytes".
"""

from __future__ import annotations

import shlex
import shutil
import subprocess
import sys
from typing import IO

from dash_ui.renderer import DASH_BITRATE, DASH_FPS, DASH_GOP_SEC, DASH_HEIGHT, DASH_WIDTH


def _ffmpeg_cmd(
    width: int,
    height: int,
    fps: int,
    bitrate: int,
    gop_sec: int,
    extra_args: list[str],
) -> list[str]:
    ff = shutil.which("ffmpeg")
    if ff is None:
        raise RuntimeError(
            "ffmpeg not found in PATH. Install with: brew install ffmpeg"
        )
    gop_frames = max(1, fps * gop_sec)
    return [
        ff, "-hide_banner", "-loglevel", "error",
        # Input: raw RGB24 frames from stdin, paced by the renderer's clock.
        "-analyzeduration", "0",
        "-probesize", "32",
        "-f", "rawvideo",
        "-pixel_format", "rgb24",
        "-video_size", f"{width}x{height}",
        "-framerate", str(fps),
        "-i", "pipe:0",
        # No audio.
        "-an",
        # H.264 encoding — identical to the stock app's MediaFormat:
        #   codec  = "video/avc" (H.264)
        #   profile / level: plain Baseline (not Constrained) @ Level 4.1
        #   one slice per picture, no B-frames, no CABAC, no scene-cut
        "-c:v", "libx264",
        "-preset", "ultrafast",
        "-threads", "1",      # avoid frame-thread buffering on interactive updates
        "-pix_fmt", "yuv420p",
        "-level", "4.1",
        "-x264-params",
            (
                "aud=1:"            # AUD NALs — used as AU delimiters by our packetizer
                "repeat-headers=1:" # SPS+PPS in-band on every IDR
                "scenecut=0:"       # fixed GOP cadence
                "ref=1:"
                "bframes=0:"
                "cabac=0:"
                "rc-lookahead=0:"   # no encoder frame queue for interactive UI updates
                "sync-lookahead=0:"
                "mbtree=0:"
                "slices=1:"         # one slice per picture (dash decoder expects this)
                "slice-max-size=0:"
                "sliced-threads=0:"
                "annexb=1:"
                "force-cfr=1:"
                "keyint={0}:min-keyint={0}".format(gop_frames)
            ),
        "-b:v", str(bitrate),
        "-maxrate", str(bitrate),
        "-bufsize", str(bitrate),
        "-r", str(fps),
        "-g", str(gop_frames),
        "-bf", "0",
        *extra_args,
        "-flush_packets", "1",
        "-f", "h264", "pipe:1",    # raw Annex-B on stdout
    ]


class H264Encoder:
    """
    Wraps an ffmpeg process that reads rawvideo from stdin and writes
    H.264 Annex-B to stdout.

    Typical usage::

        encoder = H264Encoder()
        encoder.start()
        # feed frames:
        encoder.stdin.write(rgb24_frame_bytes)
        # read Annex-B output (packetizer reads from encoder.stdout):
        data = encoder.stdout.read(...)
        encoder.stop()

    Attributes
    ----------
    stdin:
        Writable pipe; write one `width * height * 3`-byte RGB24 frame per call.
    stdout:
        Readable pipe; pass to `dash_ui.rtp.packetizer_loop` as `stream`.
    """

    def __init__(
        self,
        width: int = DASH_WIDTH,
        height: int = DASH_HEIGHT,
        fps: int = DASH_FPS,
        bitrate: int = DASH_BITRATE,
        gop_sec: int = DASH_GOP_SEC,
        extra_args: str | list[str] = "",
    ) -> None:
        self.width = width
        self.height = height
        self.fps = fps
        self.bitrate = bitrate
        self.gop_sec = gop_sec
        self._extra: list[str] = (
            shlex.split(extra_args) if isinstance(extra_args, str) else list(extra_args)
        )
        self._proc: subprocess.Popen[bytes] | None = None

    @property
    def stdin(self) -> IO[bytes]:
        assert self._proc is not None and self._proc.stdin is not None
        return self._proc.stdin

    @property
    def stdout(self) -> IO[bytes]:
        assert self._proc is not None and self._proc.stdout is not None
        return self._proc.stdout

    @property
    def running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def start(self) -> None:
        if self._proc is not None:
            raise RuntimeError("H264Encoder already started")
        cmd = _ffmpeg_cmd(
            self.width, self.height, self.fps,
            self.bitrate, self.gop_sec, self._extra,
        )
        print(
            f"[dash_ui/encoder] starting ffmpeg "
            f"({self.width}x{self.height}@{self.fps}fps "
            f"{self.bitrate // 1000}kbps baseline)",
            file=sys.stderr,
        )
        self._proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            bufsize=0,
        )

    def stop(self) -> None:
        if self._proc is None:
            return
        try:
            if self._proc.stdin:
                self._proc.stdin.close()
        except OSError:
            pass
        try:
            self._proc.wait(timeout=3.0)
        except subprocess.TimeoutExpired:
            self._proc.kill()
        self._proc = None
