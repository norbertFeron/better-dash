"""
Local test harness — run the UI on the Mac, drive it with the keyboard.

No bike, no network, no encoder.  Just the PygameRenderer in a window
with arrow keys / enter wired to the same Button events that BikeLink
would inject from UDP/2002.

Usage::

    python -m dash_ui.local_test

Key bindings
    ←  / a       Button.LEFT
    →  / d       Button.RIGHT
    ↓  / s       Button.DOWN
    ↑  / w       Button.DOWN          (treated as DOWN; the bike has no UP)
    Enter / Space / Return            Button.CLICK
    Esc / Q                           quit
    Window close                      quit

The renderer is upscaled in a normal pygame window because 526x300 is
tiny on a Retina display; --scale controls the multiplier.
"""

from __future__ import annotations

import argparse
import sys

from dash_ui.bike_link import Button
from dash_ui.pygame_renderer import PygameRenderer

import pygame


def _parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Run the dash UI locally with keyboard input (no bike).",
    )
    p.add_argument(
        "--scale", type=int, default=2,
        help="Window scale factor (default 2 → 1052x600 window).",
    )
    p.add_argument(
        "--fps-cap", type=int, default=60,
        help=(
            "Wall-clock fps cap for the local window. The renderer still "
            "produces frames at DASH_FPS (4) internally; this just keeps "
            "the local event loop responsive."
        ),
    )
    return p.parse_args(argv)


_KEYMAP = {
    pygame.K_LEFT: Button.LEFT,
    pygame.K_a: Button.LEFT,
    pygame.K_RIGHT: Button.RIGHT,
    pygame.K_d: Button.RIGHT,
    pygame.K_DOWN: Button.DOWN,
    pygame.K_s: Button.DOWN,
    pygame.K_UP: Button.DOWN,    # bike has no UP; map to DOWN for symmetry
    pygame.K_w: Button.DOWN,
    pygame.K_RETURN: Button.CLICK,
    pygame.K_KP_ENTER: Button.CLICK,
    pygame.K_SPACE: Button.CLICK,
}


def main(argv: list[str]) -> int:
    args = _parse_args(argv)

    # Headless renderer so PygameRenderer paints onto an off-screen
    # surface; we own the visible window ourselves and blit + scale into
    # it.  Keeps the rendering pipeline byte-identical to what the dash
    # would receive.  dummy_video stays False so SDL keeps the real
    # macOS display driver and our set_mode() below actually shows a
    # window.
    renderer = PygameRenderer(headless=True, dummy_video=False)

    win_w = renderer.width * max(1, args.scale)
    win_h = renderer.height * max(1, args.scale)
    window = pygame.display.set_mode((win_w, win_h))
    pygame.display.set_caption("Pi Dash · local test (←→↓ enter)")
    clock = pygame.time.Clock()

    print(
        f"local UI {renderer.width}x{renderer.height} → window {win_w}x{win_h}\n"
        "  ← / →   navigate menu\n"
        "  ↓       toggle detail panel\n"
        "  Enter   click selected item\n"
        "  Esc     quit",
        file=sys.stderr,
    )

    running = True
    while running:
        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                running = False
                break
            if event.type == pygame.KEYDOWN:
                if event.key in (pygame.K_ESCAPE, pygame.K_q):
                    running = False
                    break
                btn = _KEYMAP.get(event.key)
                if btn is not None:
                    renderer.inject_button(btn)

        # Render an internal frame and upscale into the window.  The
        # renderer's own clock paces it to DASH_FPS, which would block
        # this loop for ~250 ms; for snappy keyboard handling we render
        # the off-screen surface directly here without going through
        # render_frame()'s internal tick.  Buttons are drained inline.
        renderer._drain_buttons()       # noqa: SLF001 — local harness, intentional
        renderer._draw()                # noqa: SLF001
        renderer._frame_count += 1      # noqa: SLF001

        if args.scale == 1:
            window.blit(renderer.surface, (0, 0))
        else:
            scaled = pygame.transform.scale(renderer.surface, (win_w, win_h))
            window.blit(scaled, (0, 0))
        pygame.display.flip()
        clock.tick(args.fps_cap)

    renderer.close()
    pygame.quit()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
