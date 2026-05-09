"""
Background GPS reader for the NEO-6M (or any NMEA serial GPS).

Spawns a daemon thread that reads GPRMC/GNRMC sentences from the serial
port and maintains the latest valid fix in a thread-safe slot.

Usage::

    gps = GpsReader("/dev/ttyS0")
    gps.start()
    fix = gps.get_fix()   # GpsFix | None
    gps.stop()

The fix is considered stale after FIX_TIMEOUT seconds (default 5 s), so
``get_fix()`` returns None if the antenna loses lock.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass

try:
    import serial
    import pynmea2
    _HAS_SERIAL = True
except ImportError:
    _HAS_SERIAL = False

FIX_TIMEOUT = 5.0  # seconds before a fix is treated as stale


@dataclass(frozen=True)
class GpsFix:
    lat: float
    lon: float
    speed_kmh: float
    bearing: float    # course over ground, degrees true
    timestamp: float  # time.monotonic() when this fix arrived


class GpsReader:
    """Thread-safe NMEA GPS reader over a serial port."""

    def __init__(self, port: str = "/dev/ttyS0", baud: int = 9600) -> None:
        if not _HAS_SERIAL:
            raise ImportError(
                "pyserial and pynmea2 are required for GpsReader.\n"
                "Install with:  pip install pyserial pynmea2"
            )
        self._port = port
        self._baud = baud
        self._fix: GpsFix | None = None
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="gps-reader"
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()

    def get_fix(self) -> GpsFix | None:
        """Return the latest fix if it arrived within FIX_TIMEOUT, else None."""
        with self._lock:
            fix = self._fix
        if fix is None:
            return None
        if time.monotonic() - fix.timestamp > FIX_TIMEOUT:
            return None
        return fix

    # ------------------------------------------------------------------ private

    def _run(self) -> None:
        while not self._stop_event.is_set():
            try:
                with serial.Serial(self._port, self._baud, timeout=1) as ser:
                    while not self._stop_event.is_set():
                        raw = ser.readline()
                        line = raw.decode("ascii", errors="replace").strip()
                        self._parse(line)
            except Exception as exc:
                print(f"[GpsReader] {exc} — retrying in 2 s", flush=True)
                time.sleep(2)

    def _parse(self, line: str) -> None:
        # Accept both GP (single-system) and GN (multi-constellation) prefixes.
        if not (line.startswith("$GPRMC") or line.startswith("$GNRMC")):
            return
        try:
            msg = pynmea2.parse(line)
        except pynmea2.ParseError:
            return
        if msg.status != "A":  # A = active/valid fix
            return
        speed_kmh = float(msg.spd_over_grnd or 0) * 1.852  # knots → km/h
        bearing = float(msg.true_course or 0)
        with self._lock:
            self._fix = GpsFix(
                lat=msg.latitude,
                lon=msg.longitude,
                speed_kmh=speed_kmh,
                bearing=bearing,
                timestamp=time.monotonic(),
            )
