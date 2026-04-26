"""
PygameRenderer — pygame-backed implementation of the Renderer protocol,
now driven by bike-side button events from BikeLink.

The rendering thread is the same as before (DashUIStream's feed thread):
on each tick it pulls any queued button events, mutates UI state, draws
the current scene at DASH_WIDTH x DASH_HEIGHT, and returns RGB24 bytes
for the H.264 encoder.

Buttons are injected from another thread via `inject_button(Button)`.
The queue is bounded; older events fall off if rendering stalls.

Layout (526 x 300):

    +-------------------------------------------------+
    |  Pi Dash                            [⏎]         |
    |                                                 |
    |    Map                                          |            |
    |    Settings                                     |
    |                                                 |
    |  ◀ LEFT  /  RIGHT ▶  /  ▼ DOWN  /  ● CLICK      |
    +-------------------------------------------------+

Thread safety
    render_frame() and inject_button() are safe to call from different
    threads (a Lock guards the shared state).  pygame display creation
    must happen on the thread that calls __init__ — for the prototype
    that's the main thread.  In headless mode no display is created so
    the constraint is relaxed.
"""

from __future__ import annotations

import collections
import math
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Deque

from dash_ui.renderer import DASH_FPS, DASH_HEIGHT, DASH_WIDTH

try:
    import pygame
    _HAS_PYGAME = True
except ImportError:
    _HAS_PYGAME = False

# pygame.font has a known circular-import bug on Python 3.14 / pygame
# 2.6.1 — every call to SysFont raises and the UI renders as empty
# rectangles.  pygame.freetype is a parallel module with its own init
# path that *does* work on 3.14, so we prefer it and fall back to the
# legacy module only if freetype is unavailable.
_freetype = None
if _HAS_PYGAME:
    try:
        import pygame.freetype as _freetype  # type: ignore
        _freetype.init()
    except Exception:
        _freetype = None

try:
    from dash_ui.bike_link import Button
except ImportError:  # pragma: no cover — bike_link uses pygame_renderer indirectly
    Button = None  # type: ignore[assignment]


# Menu items shown on the dash; tweak freely while iterating on the UI.
MENU_ITEMS = ("Map", "Video", "Settings")
VIDEO_ITEM = "Video"


class PygameRenderer:
    """Interactive prototype renderer driven by BikeLink button events."""

    width: int = DASH_WIDTH
    height: int = DASH_HEIGHT
    fps: int = DASH_FPS

    def __init__(
        self,
        title: str = "Pi Dash",
        headless: bool = True,
        dummy_video: bool = False,
        calibration_grid: bool = False,
        video_file: str | Path = "test_640.mp4",
        fps: int = DASH_FPS,
    ) -> None:
        """
        Parameters
        ----------
        headless:
            If True, render to an off-screen `pygame.Surface` (the dash
            stream consumes the bytes regardless of any visible window).
            We do NOT force SDL_VIDEODRIVER=dummy here — that is a
            process-wide flag that would prevent any *other* code in the
            same process (e.g. local_test.py opening its own window)
            from getting a real display.
        dummy_video:
            Force SDL into headless dummy mode.  Use this on CI or any
            box without a window server.
        """
        if not _HAS_PYGAME:
            raise ImportError(
                "pygame is required for PygameRenderer.\n"
                "Install with:  pip install pygame-ce"
            )
        if dummy_video:
            import os
            os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
            os.environ.setdefault("SDL_AUDIODRIVER", "dummy")

        pygame.init()
        self.fps = max(1, int(fps))
        if headless:
            self.surface = pygame.Surface((self.width, self.height))
        else:
            self.surface = pygame.display.set_mode((self.width, self.height))
            pygame.display.set_caption(title)

        self._clock = pygame.time.Clock()
        self._headless = headless
        self._calibration_grid = calibration_grid
        self._video_path = Path(video_file).expanduser()
        self._frame_count = 0
        self._start_time = time.monotonic()

        # Button queue + UI state (guarded by _lock).
        self._lock = threading.Lock()
        self._pending: Deque["Button"] = collections.deque(maxlen=16)
        self._selected = 0
        self._click_counts = [0] * len(MENU_ITEMS)
        self._detail_open = False
        self._video_open = False
        self._last_button_name: str | None = None
        self._last_button_at: float = 0.0
        self._video_proc: subprocess.Popen[bytes] | None = None
        self._video_thr: threading.Thread | None = None
        self._video_stop = threading.Event()
        self._video_lock = threading.Lock()
        self._last_video_frame: bytes | None = None
        self._video_error: str | None = None

        # Pre-built fonts.  pygame.font on Python 3.14 occasionally fails
        # to initialise (cannot import name 'Font' from partially
        # initialised module 'pygame.font'); fall back to None and skip
        # text rendering instead of crashing the stream.
        self._font_title = self._make_font(22, bold=True)
        self._font_item = self._make_font(20)
        self._font_small = self._make_font(14)
        self._font_status = self._make_font(14, bold=True)

    @staticmethod
    def _make_font(size: int, *, bold: bool = False):
        # Prefer pygame.freetype (works on Python 3.14); fall back to
        # the legacy pygame.font; finally give up and let _render_text
        # draw geometric placeholders.
        if _freetype is not None:
            try:
                ft = _freetype.SysFont("monospace", size, bold=bold)
                # Tag the type so _render_text knows which API to use.
                return ("ft", ft)
            except Exception:
                pass
        try:
            return ("legacy", pygame.font.SysFont("monospace", size, bold=bold))
        except Exception:
            try:
                return ("legacy", pygame.font.Font(None, size))
            except Exception:
                return None

    @staticmethod
    def _render_text(font, text: str, colour):
        if font is None:
            return None
        kind, obj = font
        try:
            if kind == "ft":
                # freetype.render returns (surface, rect); we only need surface.
                surf, _rect = obj.render(text, fgcolor=colour)
                return surf
            return obj.render(text, True, colour)
        except Exception:
            return None

    # ------------------------------------------------------------------ public

    def inject_button(self, button: "Button") -> None:
        """Queue a button event — safe to call from any thread."""
        with self._lock:
            self._pending.append(button)

    # ----------------------------------------------------- Renderer protocol

    def render_frame(self) -> bytes:
        if not self._headless:
            for event in pygame.event.get():
                if event.type == pygame.QUIT:
                    raise SystemExit(0)

        self._drain_buttons()
        self._draw()
        self._clock.tick(self.fps)
        self._frame_count += 1

        if not self._headless:
            pygame.display.flip()
        return pygame.image.tostring(self.surface, "RGB")

    def close(self) -> None:
        self._stop_video()
        pygame.quit()

    # ------------------------------------------------------------- internals

    def _drain_buttons(self) -> None:
        """Apply any queued button events to UI state."""
        if Button is None:
            return
        with self._lock:
            events = list(self._pending)
            self._pending.clear()
        for ev in events:
            self._apply(ev)

    def _apply(self, button: "Button") -> None:
        # Single source of truth for button → UI semantics.
        if Button is None:
            return
        self._last_button_name = button.name
        self._last_button_at = time.monotonic()

        if self._video_open:
            if button is Button.DOWN:
                self._video_open = False
                self._stop_video()
            elif button is Button.LEFT:
                self._video_open = False
                self._stop_video()
                self._selected = (self._selected - 1) % len(MENU_ITEMS)
            elif button is Button.RIGHT:
                self._video_open = False
                self._stop_video()
                self._selected = (self._selected + 1) % len(MENU_ITEMS)
            elif button is Button.CLICK:
                self._click_counts[self._selected] += 1
            return

        if button is Button.LEFT:
            self._selected = (self._selected - 1) % len(MENU_ITEMS)
            self._detail_open = False
        elif button is Button.RIGHT:
            self._selected = (self._selected + 1) % len(MENU_ITEMS)
            self._detail_open = False
        elif button is Button.DOWN:
            if MENU_ITEMS[self._selected] == VIDEO_ITEM:
                self._detail_open = False
                self._video_error = None
                self._video_open = True
            else:
                self._detail_open = not self._detail_open
        elif button is Button.CLICK:
            self._click_counts[self._selected] += 1

    # ----------------------------------------------------------------- draw

    _BG       = (10, 14, 28)
    _PANEL    = (24, 30, 50)
    _ACCENT   = (0, 200, 255)
    _TEXT     = (220, 224, 232)
    _MUTED    = (130, 138, 160)
    _HILIGHT  = (255, 200, 60)
    _DIM      = (60, 70, 95)
    _SAFE_RADIUS = 142
    _SAFE_MARGIN = 12

    # Per-button colour for the press-badge (visible even with no fonts).
    _BUTTON_COLOURS = {
        "LEFT":  (0, 200, 255),
        "RIGHT": (90, 230, 120),
        "DOWN":  (255, 160, 60),
        "CLICK": (255, 90, 200),
    }

    # Item palettes — used by the no-font fallback so each row has a
    # stable identity even without a label.
    _ITEM_COLOURS = (
        (140, 220, 90),   # Map     – green
        (0, 200, 255),    # Video   – cyan
        (200, 140, 240),  # Settings– purple
    )

    def _draw(self) -> None:
        if self._calibration_grid:
            self._draw_calibration_grid()
            return
        if self._video_open:
            self._draw_video()
            return

        s = self.surface
        s.fill(self._BG)

        # ----- top bar -------------------------------------------------
        panel_x, panel_w = self._safe_rect_for_band(38, 66, extra_margin=6)
        pygame.draw.rect(s, self._PANEL, (panel_x, 38, panel_w, 28), border_radius=10)
        title = self._render_text(self._font_title, "PI DASH", self._ACCENT)
        if title is not None:
            s.blit(title, (self.width // 2 - title.get_width() // 2, 42))
        else:
            # Solid accent bar across the top so it's clear something's alive.
            pygame.draw.rect(s, self._ACCENT, (self.width // 2 - 50, 48, 100, 8))
        elapsed = time.monotonic() - self._start_time
        clock_lbl = self._render_text(
            self._font_small,
            f"{elapsed:5.1f}s",
            self._MUTED,
        )

        # ----- menu list ----------------------------------------------
        list_y = 78
        line_h = 30
        for i, label in enumerate(MENU_ITEMS):
            y = list_y + i * line_h
            list_x, row_w = self._safe_rect_for_band(y - 2, y + line_h - 4)
            is_sel = i == self._selected
            colour = self._HILIGHT if is_sel else self._TEXT
            item_colour = self._ITEM_COLOURS[i % len(self._ITEM_COLOURS)]

            # Selection background (always drawn so the highlight is
            # obvious even without text).
            if is_sel:
                pygame.draw.rect(
                    s, (50, 60, 90),
                    (list_x, y - 2, row_w, line_h - 4),
                    border_radius=8,
                )
                pygame.draw.rect(s, self._HILIGHT, (list_x, y - 2, 5, line_h - 4))

            # Coloured square = item identity (Speed/Map/Music/Settings).
            pygame.draw.rect(s, item_colour, (list_x + 10, y + 5, 14, 14))

            marker = ">" if is_sel else " "
            line = self._render_text(self._font_item, f"{marker} {label}", colour)
            if line is not None:
                s.blit(line, (list_x + 32, y))
            else:
                # No fonts: draw a horizontal "label bar" with width
                # proportional to the label length so each row is
                # distinguishable at a glance.
                bar_len = 60 + 14 * len(label)
                pygame.draw.rect(
                    s, colour if is_sel else self._DIM,
                    (list_x + 32, y + 9, min(bar_len, max(24, row_w - 96)), 6),
                )

            # Right-side info / click-count.
            info = self._item_info(i)
            info_surf = self._render_text(self._font_item, info, self._MUTED) if info else None
            if info_surf is not None and row_w >= 210:
                s.blit(info_surf, (list_x + row_w - info_surf.get_width() - 8, y))

            # Click counter as dots (always drawn — useful in both modes).
            clicks = self._click_counts[i]
            for k in range(min(clicks, 12)):
                cx = list_x + row_w - 12 - k * 8
                pygame.draw.circle(s, item_colour, (cx, y + 22), 3)
            if clicks > 12:
                pygame.draw.rect(
                    s, item_colour,
                    (list_x + row_w - 12 - 12 * 8 - 12, y + 19, 8, 6),
                )

        # ----- detail panel (toggled via DOWN) ------------------------
        if self._detail_open:
            label = MENU_ITEMS[self._selected]
            clicks = self._click_counts[self._selected]
            box_y = list_y + len(MENU_ITEMS) * line_h + 8
            box_x, box_w = self._safe_rect_for_band(box_y, box_y + 56)
            box_h = 56
            pygame.draw.rect(s, self._PANEL, (box_x, box_y, box_w, box_h), border_radius=10)
            pygame.draw.rect(s, self._ACCENT, (box_x, box_y, box_w, box_h), 2, border_radius=10)
            head = self._render_text(self._font_small, f"detail . {label}", self._ACCENT)
            body = self._render_text(
                self._font_small, f"clicks: {clicks}  DOWN closes", self._TEXT,
            )
            if head is not None:
                s.blit(head, (box_x + 8, box_y + 6))
            if body is not None:
                s.blit(body, (box_x + 8, box_y + 28))
            if head is None and body is None:
                # No-font: visible "detail open" indicator + click bars.
                pygame.draw.rect(
                    s, self._ITEM_COLOURS[self._selected % len(self._ITEM_COLOURS)],
                    (box_x + 8, box_y + 10, 14, 14),
                )
                for k in range(min(clicks, 30)):
                    pygame.draw.rect(
                        s, self._ACCENT,
                        (box_x + 30 + k * 10, box_y + 32, 6, 12),
                    )

        # ----- bottom hint bar ----------------------------------------
        hint_surf = self._render_text(
            self._font_small,
            "LEFT  RIGHT  DOWN  CLICK",
            self._MUTED,
        )
        if hint_surf is not None:
            hint_y = 265
            hint_x, hint_w = self._safe_rect_for_band(hint_y, hint_y + 18, extra_margin=3)
            s.blit(hint_surf, (hint_x + (hint_w - hint_surf.get_width()) // 2, hint_y))
        else:
            # Coloured legend strip so the user knows the scheme.
            base_x = self.width // 2 - 94
            for i, name in enumerate(("LEFT", "RIGHT", "DOWN", "CLICK")):
                pygame.draw.rect(
                    s, self._BUTTON_COLOURS[name],
                    (base_x + i * 48, 270, 34, 8),
                )

        # ----- debug / heartbeat --------------------------------------
        debug_y = 244
        debug_x, debug_w = self._safe_rect_for_band(debug_y, debug_y + 16, extra_margin=3)
        if clock_lbl is not None:
            s.blit(clock_lbl, (debug_x + debug_w - clock_lbl.get_width(), debug_y))
        else:
            # Pulsing dot in the bottom-right corner: visible heartbeat.
            phase = (self._frame_count % 8) / 8.0
            r = 4 + int(2 * abs(0.5 - phase) * 2)
            pygame.draw.circle(s, self._ACCENT, (debug_x + debug_w - 8, debug_y + 8), r)

        # ----- transient last-button badge ----------------------------
        if self._last_button_name is not None:
            age = time.monotonic() - self._last_button_at
            if age < 1.2:
                colour = self._BUTTON_COLOURS.get(
                    self._last_button_name, self._HILIGHT,
                )
                # Big coloured square in the bottom-right with a glyph.
                badge_x = self.width // 2 + 58
                badge_y = 222
                pygame.draw.rect(s, colour, (badge_x, badge_y, 44, 32))
                pygame.draw.rect(s, self._BG, (badge_x, badge_y, 44, 32), 2)
                self._draw_button_glyph(
                    s, self._last_button_name,
                    badge_x + 22, badge_y + 16, self._BG,
                )
                lbl = self._render_text(
                    self._font_status, self._last_button_name, colour,
                )
                if lbl is not None:
                    s.blit(lbl, (badge_x - lbl.get_width() - 6, badge_y + 8))

    @staticmethod
    def _draw_button_glyph(surf, name: str, cx: int, cy: int, colour) -> None:
        """Draw an arrow / dot glyph for the button, no font required."""
        if name == "LEFT":
            pts = [(cx + 8, cy - 8), (cx - 8, cy), (cx + 8, cy + 8)]
            pygame.draw.polygon(surf, colour, pts)
        elif name == "RIGHT":
            pts = [(cx - 8, cy - 8), (cx + 8, cy), (cx - 8, cy + 8)]
            pygame.draw.polygon(surf, colour, pts)
        elif name == "DOWN":
            pts = [(cx - 8, cy - 6), (cx + 8, cy - 6), (cx, cy + 8)]
            pygame.draw.polygon(surf, colour, pts)
        elif name == "CLICK":
            pygame.draw.circle(surf, colour, (cx, cy), 8)
            pygame.draw.circle(surf, colour, (cx, cy), 12, 2)

    def _safe_rect_for_band(
        self,
        top: int,
        bottom: int,
        *,
        extra_margin: int = 0,
    ) -> tuple[int, int]:
        """Return the widest centered rect that fits a vertical slice of the round dash."""
        cx = self.width // 2
        cy = self.height // 2
        sample_ys = (top, (top + bottom) // 2, bottom)
        lefts = []
        rights = []
        for y in sample_ys:
            dy = abs(y - cy)
            if dy >= self._SAFE_RADIUS:
                half = 0
            else:
                half = int(math.sqrt(self._SAFE_RADIUS ** 2 - dy ** 2))
            half = max(0, half - self._SAFE_MARGIN - extra_margin)
            lefts.append(cx - half)
            rights.append(cx + half)
        left = max(lefts)
        right = min(rights)
        return left, max(0, right - left)

    def _video_rect(self) -> tuple[int, int, int, int]:
        """Full stream rectangle; the round dash lens crops video mode."""
        return (0, 0, self.width, self.height)

    def _start_video(self) -> None:
        if self._video_proc is not None or self._video_error is not None:
            return
        video_path = self._video_path
        if not video_path.is_absolute():
            video_path = Path.cwd() / video_path
        if not video_path.is_file():
            self._video_error = f"missing {self._video_path}"
            return
        ff = shutil.which("ffmpeg")
        if ff is None:
            self._video_error = "ffmpeg not found"
            return
        x, y, w, h = self._video_rect()
        del x, y
        self._video_stop.clear()
        cmd = [
            ff, "-hide_banner", "-loglevel", "error",
            "-re",
            "-stream_loop", "-1",
            "-i", str(video_path),
            "-an",
            "-vf",
            (
                f"scale={w}:{h}:force_original_aspect_ratio=increase,"
                f"crop={w}:{h},"
                f"fps={self.fps}"
            ),
            "-pix_fmt", "rgb24",
            "-f", "rawvideo",
            "pipe:1",
        ]
        try:
            self._video_proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=0,
            )
        except OSError as exc:
            self._video_error = f"video error: {exc}"
            return

        self._video_thr = threading.Thread(
            target=self._video_reader_loop,
            args=(self._video_proc, w, h),
            name="dash-ui-video-reader",
            daemon=True,
        )
        self._video_thr.start()

    def _stop_video(self) -> None:
        self._video_stop.set()
        proc = self._video_proc
        self._video_proc = None
        self._last_video_frame = None
        if proc is None:
            return
        try:
            proc.terminate()
            proc.wait(timeout=0.5)
        except subprocess.TimeoutExpired:
            proc.kill()
        except OSError:
            pass
        thr = self._video_thr
        self._video_thr = None
        if thr is not None and thr is not threading.current_thread():
            thr.join(timeout=0.5)

    def _video_reader_loop(self, proc: subprocess.Popen[bytes], w: int, h: int) -> None:
        stdout = proc.stdout
        if stdout is None:
            self._video_error = "video stdout missing"
            return
        frame_len = w * h * 3
        buf = bytearray()
        try:
            fd = stdout.fileno()
            while not self._video_stop.is_set():
                chunk = os.read(fd, frame_len - len(buf))
                if not chunk:
                    break
                buf.extend(chunk)
                if len(buf) == frame_len:
                    with self._video_lock:
                        self._last_video_frame = bytes(buf)
                    buf.clear()
        except OSError as exc:
            if not self._video_stop.is_set():
                self._video_error = f"video read: {exc}"
        finally:
            if not self._video_stop.is_set():
                stderr = b""
                try:
                    proc.wait(timeout=0.2)
                    if proc.stderr is not None:
                        stderr = proc.stderr.read(240)
                except (OSError, subprocess.TimeoutExpired):
                    pass
                msg = stderr.decode(errors="replace").strip().splitlines()
                if msg:
                    self._video_error = msg[-1][:80]
                elif self._last_video_frame is None:
                    self._video_error = "video stopped"
                self._video_proc = None

    def _read_video_frame(self) -> bytes | None:
        self._start_video()
        with self._video_lock:
            return self._last_video_frame

    def _draw_video(self) -> None:
        s = self.surface
        s.fill((0, 0, 0))
        x, y, w, h = self._video_rect()
        frame = self._read_video_frame()
        if frame is not None:
            video_surf = pygame.image.frombuffer(frame, (w, h), "RGB")
            s.blit(video_surf, (x, y))
        else:
            pygame.draw.rect(s, self._PANEL, (x, y, w, h), border_radius=10)
            pygame.draw.rect(s, self._ACCENT, (x, y, w, h), 2, border_radius=10)
            msg = self._video_error or "opening video..."
            line = self._render_text(self._font_small, msg, self._TEXT)
            if line is not None:
                s.blit(line, (self.width // 2 - line.get_width() // 2, y + h // 2 - 8))

        title = self._render_text(self._font_status, "VIDEO", self._ACCENT)
        if title is not None:
            s.blit(title, (self.width // 2 - title.get_width() // 2, 18))
        hint = self._render_text(self._font_small, "DOWN closes", self._MUTED)
        if hint is not None:
            s.blit(hint, (self.width // 2 - hint.get_width() // 2, self.height - 24))

    def _draw_calibration_grid(self) -> None:
        """Draw a stable target for measuring the dash's circular crop."""
        s = self.surface
        s.fill((0, 0, 0))

        minor = (22, 32, 48)
        major = (46, 64, 92)
        axis = self._HILIGHT
        circle = self._ACCENT
        text = self._TEXT
        muted = self._MUTED
        cx = self.width // 2
        cy = self.height // 2
        max_r = min(self.width, self.height) // 2

        for x in range(0, self.width + 1, 10):
            colour = major if x % 50 == 0 else minor
            pygame.draw.line(s, colour, (x, 0), (x, self.height), 2 if x % 50 == 0 else 1)
        for y in range(0, self.height + 1, 10):
            colour = major if y % 50 == 0 else minor
            pygame.draw.line(s, colour, (0, y), (self.width, y), 2 if y % 50 == 0 else 1)

        # Radial spokes make rotation and off-centre cropping visible in photos.
        for deg in range(0, 360, 30):
            rad = math.radians(deg)
            x = int(cx + math.cos(rad) * max_r)
            y = int(cy + math.sin(rad) * max_r)
            pygame.draw.line(s, (34, 48, 72), (cx, cy), (x, y), 1)

        for r in (25, 50, 75, 100, 125, 150):
            width = 2 if r % 50 == 0 else 1
            pygame.draw.circle(s, circle, (cx, cy), r, width)
            label = self._render_text(self._font_small, f"r{r}", muted)
            if label is not None:
                s.blit(label, (cx + 6, cy - r + 2))

        pygame.draw.line(s, axis, (cx, 0), (cx, self.height), 2)
        pygame.draw.line(s, axis, (0, cy), (self.width, cy), 2)
        pygame.draw.circle(s, axis, (cx, cy), 5)
        pygame.draw.circle(s, axis, (cx, cy), 10, 1)
        pygame.draw.rect(s, (180, 180, 180), (0, 0, self.width - 1, self.height - 1), 1)

        title = self._render_text(self._font_status, "CALIBRATION GRID", text)
        if title is not None:
            s.blit(title, (12, 8))
        centre = self._render_text(self._font_small, f"center {cx},{cy}  max r{max_r}", text)
        if centre is not None:
            s.blit(centre, (12, self.height - 22))

    def _item_info(self, i: int) -> str:
        """Right-aligned per-item info string (placeholder data for now)."""
        label = MENU_ITEMS[i]
        if label == "Map":
            return ""
        if label == "Video":
            return "DOWN"
        if label == "Settings":
            return ""
        return ""
