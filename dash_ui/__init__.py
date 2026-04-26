"""
dash_ui — UI-to-dash RTP streaming + bike-side control plane.

Quick start (prototype)::

    python -m dash_ui.prototype --ssid RE_3NNH_240301

Programmatic use::

    from dash_ui import (
        BikeLink, BikeLinkConfig, Button,
        DashUIStream, PygameRenderer,
    )

    renderer = PygameRenderer(headless=True)
    link = BikeLink(
        BikeLinkConfig(ssid="RE_3NNH_240301"),
        on_button=lambda b: renderer.inject_button(b),
    )
    stream = DashUIStream(renderer, bike_ip="192.168.1.1")

    link.start()        # auth + nav-mode entry + heartbeats
    stream.start()      # H.264 / RTP to UDP/5000
    try:
        while stream.running:
            time.sleep(1)
    finally:
        stream.stop()
        link.stop()
"""

from dash_ui.bike_link import BikeLink, BikeLinkConfig, Button
from dash_ui.pygame_renderer import PygameRenderer
from dash_ui.stream import DashUIStream

__all__ = [
    "BikeLink",
    "BikeLinkConfig",
    "Button",
    "DashUIStream",
    "PygameRenderer",
]
