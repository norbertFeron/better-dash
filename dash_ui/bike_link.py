"""
BikeLink — control-plane companion to DashUIStream.

DashUIStream only knows how to push H.264/RTP pixels to UDP/5000.  On its
own that is not enough: the dash will refuse to allocate its nav-decoder
surface until the phone has

    1. completed the RSA/AES authentication handshake
    2. announced a destination (0x007E "route card")
    3. latched projection on (q3c.g + q3c.w)
    4. issued q3c.z2 ("start navigation")
    5. kept a 4 Hz q3c.g + 1 Hz route-card + 1 Hz nav-info heartbeat

…and in parallel, the dash sends button events on UDP/2002 that we want
to surface to the renderer:

    09 00 0001 13   →  Right
    09 00 0001 14   →  Left
    09 00 0001 15   →  Down
    09 00 0001 18   →  Click  (followed by 05/09 segments)

`BikeLink` runs all of that machinery in background threads and exposes
two affordances:

    link = BikeLink(ssid="RE_…", on_button=my_callback)
    link.start()
    # …stream pixels via DashUIStream concurrently…
    link.stop()

Almost every K1G primitive is reused verbatim from the battle-tested
`tripper_app_like_nav.py` (auth handshake, packet builders, projection
heartbeat, route-card keepalive, nav-info loop, 1 Hz tick).  This module
just orchestrates them and adds the button-event dispatcher.
"""

from __future__ import annotations

import enum
import importlib.util
import socket
import sys
import threading
import time
from pathlib import Path
from types import ModuleType
from typing import Callable

# ---------------------------------------------------------------------------
# Pull tripper_app_like_nav.py in as a module so we can reuse all its
# tested helpers (auth, packet builders, threads…).  It lives at the repo
# root, one directory above this package; importing by spec keeps the
# package self-contained without requiring it to be installed.
# ---------------------------------------------------------------------------

def _load_nav_module() -> ModuleType:
    name = "tripper_app_like_nav"
    if name in sys.modules:
        return sys.modules[name]
    repo_root = Path(__file__).resolve().parent.parent
    spec_path = repo_root / "tripper_app_like_nav.py"
    if not spec_path.is_file():
        raise ImportError(f"cannot find {spec_path}")
    spec = importlib.util.spec_from_file_location(name, spec_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load spec for {spec_path}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


_nav = _load_nav_module()


# ---------------------------------------------------------------------------
# Button event model
# ---------------------------------------------------------------------------

class Button(enum.Enum):
    """Bike-side button events delivered as 09 00 0001 XX on UDP/2002."""

    RIGHT = 0x13
    LEFT  = 0x14
    DOWN  = 0x15
    CLICK = 0x18

    @classmethod
    def from_byte(cls, b: int) -> "Button | None":
        try:
            return cls(b)
        except ValueError:
            return None


# K1G ack template for "06 80 0001 XX" — same shape as Q3C_R2/T2/U2/… in
# the nav script, parameterised on the trailing byte.  The dash app uses
# a substring check (StringsKt.n3) so the outer wrapper just has to be a
# valid K1G envelope.
_BUTTON_ACK_PREFIX = "0016000200000000020100054B3147200006800001"


def _button_ack_hex(button_byte: int) -> str:
    return _BUTTON_ACK_PREFIX + f"{button_byte & 0xFF:02X}"


ButtonCallback = Callable[[Button], None]


# ---------------------------------------------------------------------------
# Connection options
# ---------------------------------------------------------------------------

class BikeLinkConfig:
    """Plain-data container so the BikeLink ctor stays readable."""

    __slots__ = (
        "ssid", "hostname", "bike_ip", "broadcast",
        "udp_port", "listen_port",
        "route_title", "no_auth", "auth_timeout",
        "fixed_temp_c", "tick_battery", "tick_cell_signal",
        "k1g_seq_start", "burst_pause",
        "route_card_pre_z2", "route_card_gap", "route_card_rate",
        "pre_z2_wait", "z2_repeat",
        "nav_info_rate", "nav_maneuver",
        "nav_primary_distance", "nav_total_distance",
        "projection_fps",
    )

    def __init__(
        self,
        *,
        ssid: str | None = None,
        hostname: str = "MacBook",
        bike_ip: str = "192.168.1.1",
        broadcast: str = "192.168.1.255",
        udp_port: int = 2000,
        listen_port: int = 2002,
        route_title: str = "Pi Dash",
        no_auth: bool = False,
        auth_timeout: float = 8.0,
        fixed_temp_c: int = 1,
        tick_battery: int = 80,
        tick_cell_signal: int = 255,
        k1g_seq_start: int = 0,
        burst_pause: float = 0.02,
        route_card_pre_z2: int = 4,
        route_card_gap: float = 0.35,
        route_card_rate: float = 1.0,
        pre_z2_wait: float = 0.45,
        z2_repeat: int = 1,
        nav_info_rate: float = 1.0,
        nav_maneuver: int | None = None,
        nav_primary_distance: int = 500,
        nav_total_distance: int = 500,
        projection_fps: float = 4.0,
    ) -> None:
        self.ssid = ssid
        self.hostname = hostname
        self.bike_ip = bike_ip
        self.broadcast = broadcast
        self.udp_port = udp_port
        self.listen_port = listen_port
        self.route_title = route_title
        self.no_auth = no_auth
        self.auth_timeout = auth_timeout
        self.fixed_temp_c = fixed_temp_c
        self.tick_battery = tick_battery
        self.tick_cell_signal = tick_cell_signal
        self.k1g_seq_start = k1g_seq_start
        self.burst_pause = burst_pause
        self.route_card_pre_z2 = route_card_pre_z2
        self.route_card_gap = route_card_gap
        self.route_card_rate = route_card_rate
        self.pre_z2_wait = pre_z2_wait
        self.z2_repeat = z2_repeat
        self.nav_info_rate = nav_info_rate
        self.nav_maneuver = (
            nav_maneuver
            if nav_maneuver is not None
            else _nav.NAV_MANEUVER_CONTINUE
        )
        self.nav_primary_distance = nav_primary_distance
        self.nav_total_distance = nav_total_distance
        self.projection_fps = projection_fps


# ---------------------------------------------------------------------------
# BikeLink
# ---------------------------------------------------------------------------

class BikeLink:
    """
    Run the full K1G control plane against a Tripper dash.

    Lifecycle::

        link = BikeLink(BikeLinkConfig(ssid="RE_xxxx"), on_button=cb)
        link.start()       # blocks until handshake + nav-mode entry done
        if not link.connected:
            sys.exit("link did not establish")
        # …feed pixels via DashUIStream while link runs in the background…
        link.stop()         # joins threads, sends q3c.h + q3c.x
    """

    def __init__(
        self,
        config: BikeLinkConfig | None = None,
        *,
        on_button: ButtonCallback | None = None,
    ) -> None:
        self.config = config or BikeLinkConfig()
        self.on_button = on_button

        # Sockets / TX serializer.
        self._sock: socket.socket | None = None
        self._listen_sock: socket.socket | None = None
        self._tx: _nav.K1GTx | None = None

        # Auth state (None when --no-auth).
        self._auth: _nav.AuthState | None = None
        self._connected = False

        # Stop events for each background loop.
        self._stop_all = threading.Event()
        self._rx_stop = threading.Event()
        self._proj_stop = threading.Event()
        self._nav_info_stop = threading.Event()
        self._route_card_stop = threading.Event()
        self._tick_stop = threading.Event()

        # Threads.
        self._rx_thr: threading.Thread | None = None
        self._proj_thr: threading.Thread | None = None
        self._nav_info_thr: threading.Thread | None = None
        self._route_card_thr: threading.Thread | None = None
        self._tick_thr: threading.Thread | None = None

        # Cached for shutdown.
        self._projection_started = False

    # ------------------------------------------------------------------ public

    @property
    def connected(self) -> bool:
        """True once the handshake + nav-mode kicks have completed."""
        return self._connected

    def start(self) -> bool:
        """
        Open sockets, do the auth handshake, fire the nav-mode kick
        sequence, and start all background threads.  Returns True if
        the handshake reached a usable state (auth_ok or no_auth).
        """
        cfg = self.config

        self._sock = _nav.open_broadcast_socket(None, cfg.udp_port)
        self._listen_sock = _nav.open_listen_socket_2002(None, cfg.listen_port)
        seq = _nav.RollingSeq(cfg.k1g_seq_start)
        self._tx = _nav.K1GTx(self._sock, (cfg.broadcast, cfg.udp_port), seq)

        if not cfg.no_auth:
            if not _nav._HAS_CRYPTO:
                print(
                    "[bike_link] WARNING: 'cryptography' missing — auth disabled",
                    file=sys.stderr,
                )
            else:
                self._auth = _nav.AuthState(ssid=cfg.ssid or "")
                if not cfg.ssid:
                    print(
                        "[bike_link] WARNING: ssid not set; dash will reject auth",
                        file=sys.stderr,
                    )

        # RX listener must be up before the burst — the dash answers in
        # tens of ms and we cannot afford to miss the 07 00 / 07 03 frames.
        self._rx_thr = threading.Thread(
            target=self._rx_loop, name="bike-link-rx", daemon=True,
        )
        self._rx_thr.start()

        print(
            f"[bike_link] UDP/{cfg.udp_port} → {cfg.broadcast} "
            f"(bike {cfg.bike_ip}); listening on UDP/{cfg.listen_port}",
            file=sys.stderr,
        )

        # ----- Initial burst (includes q3c.e auth request) ----------
        _nav.send_initial_burst(
            self._tx, cfg.hostname, cfg.burst_pause, cfg.fixed_temp_c,
        )

        # ----- Auth wait --------------------------------------------
        auth_ok = False
        if self._auth is not None:
            print(
                f"[bike_link] waiting up to {cfg.auth_timeout:.1f}s for auth…",
                file=sys.stderr,
            )
            auth_ok = self._auth.authenticated.wait(timeout=cfg.auth_timeout)
            if auth_ok:
                print("[bike_link] auth OK", file=sys.stderr)
            else:
                print(
                    "[bike_link] auth timeout — continuing (commands may be ignored)",
                    file=sys.stderr,
                )

        send_nav_now = cfg.no_auth or self._auth is None or auth_ok

        # ----- Nav-mode kick sequence --------------------------------
        if send_nav_now:
            self._enter_nav_mode()
            self._connected = True

        # ----- 1Hz heartbeat tick -----------------------------------
        # Always run the tick; it keeps the home/connected state alive
        # even before nav fully comes up.
        self._tick_thr = threading.Thread(
            target=self._tick_loop, name="bike-link-tick", daemon=True,
        )
        self._tick_thr.start()

        return self._connected

    def stop(self) -> None:
        """Stop all threads, send projection-off TLVs, close sockets."""
        self._stop_all.set()
        self._rx_stop.set()
        self._proj_stop.set()
        self._nav_info_stop.set()
        self._route_card_stop.set()
        self._tick_stop.set()

        # Best-effort teardown TLVs (NavigationFragment.Y7).
        if self._projection_started and self._tx is not None:
            try:
                self._tx.send_hex(_nav.Q3C_H_PROJ_STOP)
                self._tx.send_hex(_nav.Q3C_X_PROJ_OFF)
            except Exception:
                pass

        for thr in (
            self._proj_thr,
            self._nav_info_thr,
            self._route_card_thr,
            self._tick_thr,
            self._rx_thr,
        ):
            if thr is not None:
                thr.join(timeout=1.5)

        for s in (self._listen_sock, self._sock):
            if s is not None:
                try:
                    s.close()
                except OSError:
                    pass
        self._listen_sock = None
        self._sock = None
        print("[bike_link] stopped", file=sys.stderr)

    # ------------------------------------------------------------- internals

    def _enter_nav_mode(self) -> None:
        """Mirror the post-auth ordering reconstructed in tripper_app_like_nav.main()."""
        cfg = self.config
        tx = self._tx
        assert tx is not None

        # Nav context + empty favourite lists (NavigationRootFragment.F0).
        tx.send_hex(_nav.Q3C_Q_NAV_CTX)
        tx.send_hex(_nav.Q3C_R_EMPTY_LISTS)

        # Pre-z2 route-card burst — establishes destination so the dash
        # opens the UDP/5000 decoder surface when z2 lands.
        route_pkt = _nav.build_navigation_packet(cfg.route_title, 0)
        route_pkt_proj_on = _nav.build_navigation_packet(
            cfg.route_title, 0, projection_on=True,
        )
        reps = max(1, int(cfg.route_card_pre_z2))
        gap = max(0.0, float(cfg.route_card_gap))
        for i in range(reps):
            tx.send(route_pkt)
            if i < reps - 1 and gap > 0:
                time.sleep(gap)
        print(
            f"[bike_link] sent 0x007e route card x{reps}",
            file=sys.stderr,
        )

        # q3c.g — projection-on frame; start the per-frame heartbeat.
        tx.send_hex(_nav.Q3C_G_PROJ_FRAME)
        self._proj_thr = threading.Thread(
            target=_nav.projection_heartbeat_loop,
            args=(tx, self._proj_stop, float(cfg.projection_fps)),
            name="bike-link-proj-hb", daemon=True,
        )
        self._proj_thr.start()
        self._projection_started = True

        # q3c.z2 — actual nav-start.
        for _ in range(max(1, int(cfg.z2_repeat))):
            tx.send_hex(_nav.Q3C_Z2_START_NAV)
            if cfg.z2_repeat > 1:
                time.sleep(0.1)
        print("[bike_link] sent q3c.z2 (nav start)", file=sys.stderr)

        # Post-z2 confirmation route card.
        tx.send(route_pkt)

        # Brief warm-up so the decoder surface is live before pixels arrive.
        if cfg.pre_z2_wait > 0:
            time.sleep(cfg.pre_z2_wait)

        # Nav-info loop — keeps the instruction bubble alive.
        def _nav_pkt_factory() -> bytes:
            return _nav.build_active_nav_packet(
                primary_maneuver=cfg.nav_maneuver,
                primary_distance_m=cfg.nav_primary_distance,
                primary_unit=_nav.NAV_UNIT_METERS,
                total_distance_m=cfg.nav_total_distance,
                total_distance_unit=_nav.NAV_UNIT_METERS,
                projection_on=True,
                decimal_fmt_on=False,
            )

        self._nav_info_thr = threading.Thread(
            target=_nav.nav_info_loop,
            args=(
                tx,
                self._nav_info_stop,
                _nav_pkt_factory,
                1.0 / max(0.1, cfg.nav_info_rate),
            ),
            name="bike-link-nav-info", daemon=True,
        )
        self._nav_info_thr.start()

        # Route-card keep-alive — dash watchdog tears the decoder down at
        # ~15–20 s without this.
        if cfg.route_card_rate > 0:
            tx.send(route_pkt_proj_on)
            self._route_card_thr = threading.Thread(
                target=_nav.route_card_keepalive_loop,
                args=(
                    tx,
                    self._route_card_stop,
                    route_pkt_proj_on,
                    1.0 / max(0.1, cfg.route_card_rate),
                ),
                name="bike-link-route-card", daemon=True,
            )
            self._route_card_thr.start()

    # ------------------------------------------------------------ tick / RX

    def _tick_loop(self) -> None:
        """1 Hz 0044+0030 metadata heartbeat (REForeGroundService.d/e)."""
        cfg = self.config
        tx = self._tx
        assert tx is not None
        while not self._tick_stop.wait(timeout=1.0):
            try:
                tx.send(
                    _nav.build_0044_heartbeat_d_no_cell(
                        fixed_temp_c=cfg.fixed_temp_c,
                        cell_signal_0_255=cfg.tick_cell_signal,
                        battery_pct_0_100=cfg.tick_battery,
                        gps_on=True,
                        charging=False,
                        music_ratio_0_1=0.6,
                        nav_distance_rounded=0,
                        alarm_ratio_0_1=0.5,
                        call_tail_hex="0521000132054D000132",
                    )
                )
                tx.send(
                    _nav.build_metadata_0030_e(
                        cell_signal_0_255=cfg.tick_cell_signal,
                        music_ratio_0_1=0.6,
                        nav_distance_rounded=0,
                        alarm_ratio_0_1=0.5,
                        call_tail_hex="0521000132054D000132",
                    )
                )
            except Exception as exc:  # pragma: no cover - defensive
                print(f"[bike_link] tick error: {exc}", file=sys.stderr)

    def _rx_loop(self) -> None:
        """Process inbound UDP/2002 segments: auth, frame-decoded ack, buttons."""
        sock = self._listen_sock
        tx = self._tx
        assert sock is not None and tx is not None
        sock.settimeout(0.5)
        while not self._rx_stop.is_set():
            try:
                data, _addr = sock.recvfrom(65535)
            except socket.timeout:
                continue
            except OSError:
                return
            for s in _nav.decode_ic_to_app_segments(data):
                seg_hex = str(s["seg_hex"]).upper()
                self._handle_segment(seg_hex)

    def _handle_segment(self, seg_hex: str) -> None:
        tx = self._tx
        assert tx is not None

        # 07 xx — auth.  Delegate to the nav-script handler so RSA/AES
        # behaviour stays identical to the proven path.
        if seg_hex.startswith("07") and self._auth is not None:
            try:
                _nav.handle_auth_segment(seg_hex, tx, self._auth)
            except Exception as exc:
                print(f"[bike_link] auth handler error: {exc}", file=sys.stderr)
            return

        # 09 06 0001 55 — per-IDR "frame decoded" ACK.  Always answer with
        # q3c.L2 or the dash watchdog tears the decoder down.
        if seg_hex.startswith("09060001") and seg_hex.endswith("55"):
            try:
                tx.send_hex(_nav.Q3C_L2)
            except Exception:
                pass
            return

        # 09 00 0001 XX — bike-button event.  Echo-ack and dispatch.
        if seg_hex.startswith("09000001") and len(seg_hex) >= 10:
            try:
                button_byte = int(seg_hex[8:10], 16)
            except ValueError:
                return
            self._dispatch_button(button_byte)
            return

    def _dispatch_button(self, button_byte: int) -> None:
        tx = self._tx
        if tx is None:
            return
        # Always send the 0680 0001 XX echo ack — same shape as q3c.r2/u2/…
        try:
            tx.send_hex(_button_ack_hex(button_byte))
        except Exception as exc:  # pragma: no cover - defensive
            print(f"[bike_link] button ack send failed: {exc}", file=sys.stderr)

        btn = Button.from_byte(button_byte)
        if btn is None:
            print(
                f"[bike_link] unknown button byte 0x{button_byte:02X}",
                file=sys.stderr,
            )
            return
        print(f"[bike_link] button: {btn.name}", file=sys.stderr)
        cb = self.on_button
        if cb is not None:
            try:
                cb(btn)
            except Exception as exc:
                print(f"[bike_link] on_button raised: {exc}", file=sys.stderr)
