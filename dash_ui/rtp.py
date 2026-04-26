"""
RTP packetizer for H.264 Annex-B → UDP.

Self-contained copy of the battle-tested logic from tripper_app_like_nav.py,
adapted for use with a renderer-fed rawvideo pipe.

Key constraints enforced here (same as the stock app):
  - NO STAP-A aggregation (NAL type 24) — the dash's embedded decoder drops it.
  - Single-NAL packets for small NALs, FU-A for everything else.
  - SPS/PPS are captured live from ffmpeg (not hardcoded) so the slice-header
    bit-widths match what libx264 actually encoded (see PROTOCOL.md §SPS mismatch).
  - IDR AUs are bundled as SPS+PPS+IDR with embedded Annex-B start codes,
    exactly as the real phone does in nav_open_ok.pcap.
"""

from __future__ import annotations

import random
import os
import socket
import struct
import sys
import threading
import time
from typing import IO, Iterator


# ---------------------------------------------------------------------------
# NAL / Annex-B helpers
# ---------------------------------------------------------------------------

def _find_startcode(buf: bytes | bytearray, start: int) -> tuple[int, int]:
    """Return (pos, sc_len) of the first Annex-B start code at or after `start`.
    sc_len is 3 (00 00 01) or 4 (00 00 00 01).  Returns (-1, 0) if none found.
    """
    n = len(buf)
    i = start
    while i < n - 2:
        if buf[i] == 0 and buf[i + 1] == 0:
            if buf[i + 2] == 1:
                return i, 3
            if i + 3 < n and buf[i + 2] == 0 and buf[i + 3] == 1:
                return i, 4
        i += 1
    return -1, 0


def iter_annexb_nals(
    stream: IO[bytes],
    stop: threading.Event,
    chunk_size: int = 4096,
) -> Iterator[bytes]:
    """Yield raw NAL units (no start code) from an H.264 Annex-B byte stream."""
    buf = bytearray()
    try:
        fd = stream.fileno()
    except (AttributeError, OSError):
        fd = None
    while not stop.is_set():
        try:
            chunk = os.read(fd, chunk_size) if fd is not None else stream.read(chunk_size)
        except Exception:
            break
        if not chunk:
            break
        buf.extend(chunk)
        while True:
            sc_pos, sc_len = _find_startcode(buf, 0)
            if sc_pos < 0:
                break
            if sc_pos > 0:
                del buf[:sc_pos]
                sc_pos = 0
            next_sc_pos, _ = _find_startcode(buf, sc_len)
            if next_sc_pos < 0:
                break
            nal = bytes(buf[sc_len:next_sc_pos])
            del buf[:next_sc_pos]
            if nal:
                yield nal
    # flush trailing NAL
    sc_pos, sc_len = _find_startcode(buf, 0)
    if sc_pos == 0 and len(buf) > sc_len:
        trailing = bytes(buf[sc_len:])
        if trailing:
            yield trailing


def _bundle_sps_pps_idr(sps: bytes, pps: bytes, idr: bytes) -> bytes:
    """Combine SPS + PPS + IDR with embedded Annex-B start codes.

    Mirrors the real phone's per-IDR packing (nav_open_ok.pcap); the dash
    reassembles the surrounding FU-A chain and splits on the Annex-B codes.
    """
    return sps + b"\x00\x00\x00\x01" + pps + b"\x00\x00\x00\x01" + idr


# ---------------------------------------------------------------------------
# RTP sender
# ---------------------------------------------------------------------------

def _send_au_rtp(
    sock: socket.socket,
    dst: tuple[str, int],
    nals: list[bytes],
    ssrc: int,
    seq_ref: list[int],
    ts: int,
    max_payload: int,
    payload_type: int,
) -> int:
    """Packetize one H.264 access unit into RTP packets and send them.

    Returns number of packets sent.  Never raises; logs sendto errors to stderr.
    """
    if not nals:
        return 0
    plan: list[bytes] = []
    for nal in nals:
        if not nal:
            continue
        if len(nal) <= max_payload:
            plan.append(nal)
        else:
            nal_hdr = nal[0]
            f_nri = nal_hdr & 0xE0
            nal_type = nal_hdr & 0x1F
            fu_indicator = f_nri | 28
            payload = nal[1:]
            chunk_size = max_payload - 2
            if chunk_size <= 0:
                continue
            offset = 0
            while offset < len(payload):
                chunk = payload[offset: offset + chunk_size]
                is_first = offset == 0
                offset += chunk_size
                is_last = offset >= len(payload)
                fu_header = (
                    (0x80 if is_first else 0)
                    | (0x40 if is_last else 0)
                    | nal_type
                )
                plan.append(bytes((fu_indicator, fu_header)) + chunk)
    if not plan:
        return 0
    last_idx = len(plan) - 1
    sent = 0
    for idx, body in enumerate(plan):
        marker = 1 if idx == last_idx else 0
        hdr = struct.pack(
            ">BBHII",
            0x80,
            (marker << 7) | (payload_type & 0x7F),
            seq_ref[0] & 0xFFFF,
            ts & 0xFFFFFFFF,
            ssrc & 0xFFFFFFFF,
        )
        try:
            sock.sendto(hdr + body, dst)
            sent += 1
        except OSError as exc:
            print(f"RTP sendto failed: {exc}", file=sys.stderr)
            return sent
        seq_ref[0] = (seq_ref[0] + 1) & 0xFFFF
    return sent


# ---------------------------------------------------------------------------
# Public packetizer loop
# ---------------------------------------------------------------------------

def packetizer_loop(
    stream: IO[bytes],
    bike_ip: str,
    rtp_port: int,
    stop: threading.Event,
    *,
    max_payload: int = 1380,
    payload_type: int = 96,
    ssrc: int | None = None,
    max_fps: float = 8.0,
) -> None:
    """
    Read H.264 Annex-B from `stream`, packetize each access unit (delimited
    by AUD NAL type 9) and send as RTP to <bike_ip>:<rtp_port>.

    Intended to run on a background thread (DashUIStream starts it).
    `max_fps` is a safety valve preventing ffmpeg from flooding the dash
    when the renderer runs faster than expected.
    """
    if ssrc is None:
        ssrc = random.getrandbits(32)
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 1 << 19)
    dst = (bike_ip, rtp_port)
    seq: list[int] = [random.randint(0, 0xFFFF)]
    ts_base = random.getrandbits(32)
    t0 = time.time()
    min_au_interval = 1.0 / max(0.1, max_fps)
    last_au_send = 0.0

    def now_ts() -> int:
        return (ts_base + int((time.time() - t0) * 90000)) & 0xFFFFFFFF

    # Live SPS/PPS captured from the ffmpeg bitstream (first IDR).
    # Using live values — not hardcoded PHONE_SPS — keeps slice-header
    # bit-widths consistent with what libx264 actually encoded.
    live_sps: bytes | None = None
    live_pps: bytes | None = None

    def _bundle_if_idr(nals: list[bytes]) -> list[bytes]:
        sps = live_sps
        pps = live_pps
        if sps is None or pps is None:
            return nals
        out: list[bytes] = []
        replaced = False
        for n in nals:
            if not replaced and (n[0] & 0x1F) == 5:
                out.append(_bundle_sps_pps_idr(sps, pps, n))
                replaced = True
            else:
                out.append(n)
        return out

    au_nals: list[bytes] = []
    total_pkts = 0
    total_aus = 0
    try:
        for nal in iter_annexb_nals(stream, stop):
            if not nal:
                continue
            nal_type = nal[0] & 0x1F
            if nal_type == 9:   # AUD — flush previous AU
                if au_nals:
                    elapsed = time.time() - last_au_send
                    if elapsed < min_au_interval:
                        if stop.wait(timeout=min_au_interval - elapsed):
                            break
                    total_pkts += _send_au_rtp(
                        sock, dst, _bundle_if_idr(au_nals), ssrc, seq,
                        now_ts(), max_payload, payload_type,
                    )
                    last_au_send = time.time()
                    total_aus += 1
                    au_nals = []
                continue
            if nal_type == 6:   # SEI — drop (x264 version string, ~686 B)
                continue
            if nal_type == 7:   # SPS — capture live, do NOT include in AU
                live_sps = nal
                continue
            if nal_type == 8:   # PPS — capture live
                live_pps = nal
                continue
            au_nals.append(nal)
        if au_nals and not stop.is_set():
            total_pkts += _send_au_rtp(
                sock, dst, _bundle_if_idr(au_nals), ssrc, seq,
                now_ts(), max_payload, payload_type,
            )
            total_aus += 1
    finally:
        sock.close()
        print(
            f"[dash_ui/rtp] packetizer done "
            f"(AUs={total_aus}, pkts={total_pkts}, SSRC=0x{ssrc:08X})",
            file=sys.stderr,
        )
