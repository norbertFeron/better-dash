"""
Renderer protocol — the contract between DashUIStream and any UI backend.

Any class that satisfies this protocol can be passed to DashUIStream.
The only hard requirement: `render_frame()` must return exactly
`width * height * 3` bytes of packed RGB24 (R G B R G B …) at the
cadence dictated by `fps`.  The caller blocks in a tight loop calling
`render_frame()` and relying on the renderer to pace itself (e.g. via a
pygame Clock or a simple time.sleep).

Why RGB24 (not RGBA or YUV)?
  ffmpeg's `-f rawvideo -pix_fmt rgb24` is the lowest-friction format —
  no alignment padding, no alpha stripping, no colourspace guessing.
  The encoder step converts to yuv420p before H.264 anyway.

Swapping renderers
  Drop in any class with `width`, `height`, `fps` attrs and a
  `render_frame() → bytes` method — no base class needed, duck-typing
  suffices.  If you need lifecycle hooks (OpenGL context setup, …) also
  implement `start()` and `close()`.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


# Dash's stock MediaFormat constants. The prototype can raise fps for experiments.
DASH_WIDTH = 526
DASH_HEIGHT = 300
DASH_FPS = 4
DASH_BITRATE = 204_800   # bps
DASH_GOP_SEC = 1         # IDR every N seconds


@runtime_checkable
class Renderer(Protocol):
    """
    Structural protocol for a frame source.

    Implementations only need to satisfy the attribute + method
    signatures; inheriting from this class is optional.
    """

    width: int   # must equal DASH_WIDTH (526)
    height: int  # must equal DASH_HEIGHT (300)
    fps: int     # stream cadence; stock dash app uses DASH_FPS (4)

    def render_frame(self) -> bytes:
        """
        Draw the next frame and return raw RGB24 bytes.

        Must return exactly `width * height * 3` bytes.
        Should block long enough to maintain the declared `fps` cadence
        (e.g. call `pygame.time.Clock.tick(fps)` internally).
        """
        ...

    def close(self) -> None:
        """Release renderer resources (window, GL context, …)."""
        ...
