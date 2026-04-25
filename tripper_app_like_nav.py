#!/usr/bin/env python3
"""
Script to send nav mode kicks to the dash, and stream RTP similar to the stock app.
"""

from __future__ import annotations

import argparse
import binascii
import os
import random
import shlex
import shutil
import socket
import struct
import subprocess
import sys
import threading
import time
from collections import Counter
from dataclasses import dataclass
from typing import IO, Iterator

try:
    # Used for the dash ↔ app authentication handshake (RSA/PKCS1v1.5 + AES-CBC).
    # `cryptography` is a very common Python dependency; if missing, install with:
    #     pip install cryptography
    from cryptography.hazmat.backends import default_backend
    from cryptography.hazmat.primitives.asymmetric import padding as _rsa_padding
    from cryptography.hazmat.primitives.asymmetric.rsa import RSAPublicNumbers
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    from cryptography.hazmat.primitives.padding import PKCS7

    _HAS_CRYPTO = True
except ImportError:  # pragma: no cover - only exercised when dep is missing
    _HAS_CRYPTO = False


INITIAL_BURST_HEX: list[str | None] = [
    # q3c.e = "request auth / send me your RSA public key"
    # Dash replies on UDP/2002 with 07 00 <modulus> and 07 03 <exponent>, then
    # AuthState below builds the real q3c.d packet dynamically. The previous
    # version of this file had a captured/stale RSA ciphertext hardcoded at
    # slot [7]; that blob could not decrypt against the dash's private key
    # and left the pairing stuck in "Connected to <phone>" forever.
    "0016000200000000020100054b314720000804000101",
    None,  # hostname (bluconnect announce)
    "0018000200000000020100054b31472002060600030e3334",
    "0016000200000000020100054b314720030557000155",
    "0016000200000000020100054b3147200405560001aa",
    "0016000200000000020100054b3147200506050001aa",
    "0016000200000000020100054b3147200605170001aa",
    "001d000200000000020100054b314720080a020008aa55000000000000",
    "0044000a00000000020100054b3147200906080001ff060300015506040001a2060f0001aa0601000101054c000113052d00020000051b0001190521000132054d000132",
]


_NAV_FULL = binascii.unhexlify(
    "007e001100000000020100054b31472025050100145461696c6c65206465204d617320647520477200"
    "050200013c050300013405050002000a05060001300507000130050800043033303305540001300509"
    "0002004f0546000110050a000155050c000104050b0006303031303030055500012006050001aa060d0001aa"
)


_HB_73 = binascii.unhexlify(
    "0049000b00000000020100054b3147200006080001050610000139060300015506040001a2060f0001aa"
    "0601000101054c000113052d00020000051b0001190521000132054d000132"
)

#
# Minimal subset of q3c constants used by REForeGroundService 1Hz tick.
# (See jadx output: bluconnect/q3c.java)
#
Q3C_A = "55"
Q3C_B = "AA"
Q3C_Q2 = "052D0002"
# q3c.java: music stream buckets (mute -> n1, then o1..x1)
Q3C_N1 = "054C000110"
Q3C_O1 = "054C000111"
Q3C_P1 = "054C000112"
Q3C_Q1 = "054C000113"
Q3C_R1 = "054C000114"
Q3C_S1 = "054C000115"
Q3C_T1 = "054C000116"
Q3C_U1 = "054C000117"
Q3C_V1 = "054C000118"
Q3C_W1 = "054C000119"
Q3C_X1 = "054C00011A"
# alarm stream buckets (mute -> y1, then z1..I1)
Q3C_Y1 = "051B000110"
Q3C_Z1 = "051B000111"
Q3C_A1 = "051B000112"
Q3C_B1 = "051B000113"
Q3C_C1 = "051B000114"
Q3C_D1 = "051B000115"
Q3C_E1 = "051B000116"
Q3C_F1 = "051B000117"
Q3C_G1 = "051B000118"
Q3C_H1 = "051B000119"
Q3C_I1 = "051B00011A"
# GPS / battery / charging prefixes (q3c.java)
Q3C_V = "06030001"  # GPS enabled TLV prefix
Q3C_U = "06040001"  # battery capacity encoding prefix (value is k()+100 as one byte in app)
Q3C_T = "060F0001"  # charging TLV prefix

# Navigation-related constants observed in NavigationRootFragment joystick handlers.
Q3C_Q_NAV_CTX = "0016000200000000020100054B31472000052E00011E"  # q3c.q
Q3C_R_EMPTY_LISTS = "002A000600000000020100054B31472000052F0001000530000100053100010005320001000533000100"  # q3c.r
Q3C_Z2_START_NAV = "0016000200000000020100054B31472000068000010B"  # q3c.z2

# Projection lifecycle TLVs. The dash gates the nav-video surface on these
# regardless of whether RTP packets are arriving on UDP/5000 — if it doesn't
# see q3c.g frame-announces at ~frame rate plus a latched q3c.w "projection
# on" flag, it treats UDP/5000 as noise and keeps the home widgets visible.
#
#   q3c.g : inner TLV 0556 55  -> "new map bitmap was rendered this tick"
#           NavigationFragment.l9(Bitmap) calls n9(q3c.g) for every bitmap
#           pushed to gbf.c(bitmap), so once per encoded frame (~4 Hz).
#   q3c.w : inner TLV 0605 55  -> "projection video is live"
#           Sent by s6() / v7() alongside q3c.g when the projection engine
#           flips from idle to running. We latch it once at start.
#   q3c.h : inner TLV 0556 AA  -> "no more bitmaps coming" (stop-frames)
#   q3c.x : inner TLV 0605 AA  -> "projection video stopped"
#
# Sent in pairs: (g, w) on start and (h, x) on stop (see NavigationFragment
# .Y7 for the stop pair).
Q3C_G_PROJ_FRAME = "0016000200000000020100054B314720000556000155"  # q3c.g
Q3C_W_PROJ_ON    = "0016000200000000020100054B314720000605000155"  # q3c.w
Q3C_H_PROJ_STOP  = "0016000200000000020100054B3147200005560001AA"  # q3c.h
Q3C_X_PROJ_OFF   = "0016000200000000020100054B3147200006050001AA"  # q3c.x

# ----- Authentication (RSA + AES) -----------------------------------------
#
# q3c.java:
#   public static final String d = "0095000200000000020100054B3147200008000080"
#   public static final String e = "0016000200000000020100054B314720000804000101"
#
# q3c.e  == outbound "request auth / give me your RSA pubkey" K1G packet.
# q3c.d  == outbound K1G header for the RSA-encrypted session key.
#           Its total wire length is already baked in as 0x95 = 149 bytes,
#           assuming a 1024-bit RSA key (128-byte ciphertext). All Tripper
#           firmwares seen in the wild use RSA-1024.
#
# clk.X() parses inbound 07 segments (all hex, uppercase):
#   type=07 sub=00  → modulus  (big-endian, variable length, typically 128 B)
#   type=07 sub=03  → exponent (typically 0x010001)
#   type=07 sub=01  → auth status   01 = OK, anything else = failure
#
# NavigationRootFragment.R0(mod, exp):
#   payload  = ssid_bytes ‖ aes_key_bytes           (oof.j)
#   ct       = RSA-PKCS1v1.5-encrypt(payload, pubkey)
#   packet   = bytes.fromhex(q3c.d + uppercase_hex(ct))
#
Q3C_E_REQUEST_AUTH = "0016000200000000020100054B314720000804000101"
Q3C_D_PREFIX_HEX = "0095000200000000020100054B3147200008000080"


class AuthState:
    """
    Tracks the RSA handshake with the dash.
    Thread-safe: the RX thread writes modulus/exponent/status; the main
    thread waits on `authenticated`.
    """

    __slots__ = (
        "ssid",
        "modulus_hex",
        "exponent_hex",
        "aes_key",
        "authenticated",
        "_lock",
        "_session_key_sent",
        "retry_count",
    )

    def __init__(self, ssid: str) -> None:
        self.ssid = ssid
        self.modulus_hex: str | None = None
        self.exponent_hex: str | None = None
        self.aes_key: bytes | None = None
        self.authenticated = threading.Event()
        self._lock = threading.Lock()
        self._session_key_sent = False
        self.retry_count = 0

    def ingest(self, modulus_hex: str | None, exponent_hex: str | None) -> bool:
        """
        Returns True the first time both modulus and exponent are known,
        i.e. the moment the session-key message must be sent.
        """
        with self._lock:
            if modulus_hex is not None:
                self.modulus_hex = modulus_hex
            if exponent_hex is not None:
                self.exponent_hex = exponent_hex
            if (
                not self._session_key_sent
                and self.modulus_hex
                and self.exponent_hex
            ):
                if self.aes_key is None:
                    self.aes_key = os.urandom(32)
                self._session_key_sent = True
                return True
            return False

    def reset_for_retry(self) -> None:
        """
        Clear enough state to let the dash re-offer its pubkey after a 07 01 xx
        (xx != 01) failure, mirroring NavigationRootFragment.V0() + W8().
        """
        with self._lock:
            self.modulus_hex = None
            self.exponent_hex = None
            self._session_key_sent = False
            self.retry_count += 1


def _rsa_encrypt_session_key(
    ssid: str, modulus_hex: str, exponent_hex: str, aes_key: bytes
) -> bytes:
    """
    Reproduces `oof.j(ssid, mod, exp)` + `oof.h(...)`:
      payload = ssid.getBytes() ‖ aesKey.getEncoded()
      RSA/ECB/PKCS1Padding encrypt with pubkey(mod, exp)
    """
    if not _HAS_CRYPTO:
        raise RuntimeError(
            "The 'cryptography' package is required for the dash auth handshake.\n"
            "Install it with:  pip install cryptography"
        )
    n = int(modulus_hex, 16)
    e = int(exponent_hex, 16)
    pub_key = RSAPublicNumbers(e=e, n=n).public_key(default_backend())
    payload = ssid.encode("utf-8") + aes_key
    return pub_key.encrypt(payload, _rsa_padding.PKCS1v15())


def build_q3c_d_packet(ciphertext: bytes) -> bytes:
    """
    Build the wire-format q3c.d packet (K1G header + one 08 00 segment).
    Seq byte is left as 0x00; `K1GTx.send()` rewrites it on transmit.
    """
    if len(ciphertext) != 128:
        # q3c.d hardcodes outer_len=0x95 and seg_len=0x80 for 1024-bit RSA.
        # Any deviation means the dash's pubkey is unexpected; raise loudly.
        raise ValueError(
            f"unexpected RSA ciphertext size {len(ciphertext)}B (q3c.d assumes 128)"
        )
    return bytes.fromhex(Q3C_D_PREFIX_HEX) + ciphertext


def aes_decrypt_cbc(iv_then_ciphertext: bytes, aes_key: bytes) -> bytes:
    """
    Mirror `oof.d(byte[])`: first 16 bytes of input are the IV, rest is the
    AES-256/CBC/PKCS7 ciphertext encrypted under the negotiated session key.
    Used (later) to decrypt incoming type=0F vehicle-secure segments.
    """
    if not _HAS_CRYPTO:
        raise RuntimeError("'cryptography' package required for AES decrypt")
    iv = iv_then_ciphertext[:16]
    ct = iv_then_ciphertext[16:]
    cipher = Cipher(algorithms.AES(aes_key), modes.CBC(iv), backend=default_backend())
    decryptor = cipher.decryptor()
    padded = decryptor.update(ct) + decryptor.finalize()
    unpadder = PKCS7(128).unpadder()
    return unpadder.update(padded) + unpadder.finalize()


class RollingSeq:
    __slots__ = ("v",)

    def __init__(self, start: int = 0) -> None:
        self.v = start & 0xFF

    def consume(self) -> int:
        x = self.v
        self.v = (self.v + 1) & 0xFF
        return x


class K1GTx:
    """
    Serialize all outbound K1G sends to match the app's single queue semantics.
    This avoids races between the 1Hz tick thread and the 2002 RX responder thread.
    """

    __slots__ = ("sock", "dest", "seq", "_lock")

    def __init__(self, sock: socket.socket, dest: tuple[str, int], seq: RollingSeq) -> None:
        self.sock = sock
        self.dest = dest
        self.seq = seq
        self._lock = threading.Lock()

    def send(self, pkt: bytes) -> None:
        with self._lock:
            self.sock.sendto(patch_k1g_seq(pkt, self.seq.consume()), self.dest)

    def send_hex(self, hex_str: str) -> None:
        self.send(binascii.unhexlify(hex_str))


def build_hostname_announce(hostname: str) -> bytes:
    raw = hostname.encode("utf-8")[:200]
    body = bytearray(bytes.fromhex("0021000200000000020100054b314720"))
    lfield = len(raw) + 1
    body.extend(bytes([0x01, 0x06, 0x0B, 0x00, lfield]))
    body.extend(raw)
    body.append(0x00)
    struct.pack_into(">H", body, 0, len(body))
    return bytes(body)


def _parse_nav_template(nav: bytes) -> tuple[bytes, bytes]:
    magic = nav.find(b"K1G ")
    if magic == -1:
        raise ValueError("K1G marker not in nav template")
    seq_off = magic + 4
    if nav[seq_off + 1 : seq_off + 3] != b"\x05\x01":
        raise ValueError("unexpected bytes after nav seq")
    rlen = struct.unpack(">H", nav[seq_off + 3 : seq_off + 5])[0]
    str0 = seq_off + 5
    suffix = nav[str0 + rlen :]
    prefix_before_seq = nav[:seq_off]
    return prefix_before_seq, suffix


_NAV_PREFIX, _NAV_SUFFIX = _parse_nav_template(_NAV_FULL)


def build_navigation_packet(route_title: str, seq: int, *, projection_on: bool = False) -> bytes:
    rt = route_title.encode("utf-8")[:60]
    rt_null = rt + b"\x00"
    inner = bytearray()
    inner.extend(_NAV_PREFIX)
    inner.append(seq & 0xFF)
    inner.extend(bytes([0x05, 0x01]))
    inner.extend(struct.pack(">H", len(rt_null)))
    inner.extend(rt_null)
    inner.extend(_NAV_SUFFIX)
    marker = b"\x06\x05\x00\x01"
    idx = inner.rfind(marker)
    if idx >= 0:
        inner[idx + len(marker)] = 0x55 if projection_on else 0xAA
    struct.pack_into(">H", inner, 0, len(inner))
    return bytes(inner)


def patch_k1g_seq(pkt: bytes, seq: int) -> bytes:
    b = bytearray(pkt)
    k = b.find(b"K1G ")
    if k == -1:
        raise ValueError("K1G not found")
    b[k + 4] = seq & 0xFF
    struct.pack_into(">H", b, 0, len(b))
    return bytes(b)

def patch_single_byte_field(pkt: bytes, marker: bytes, value: int) -> bytes:
    """
    Replace the single byte *after* marker (first occurrence).
    Used to patch TLV values like: 06 10 00 01 <value>.
    """
    i = pkt.find(marker)
    if i == -1:
        raise ValueError(f"marker not found: {marker.hex().upper()}")
    j = i + len(marker)
    if j >= len(pkt):
        raise ValueError("marker at end of packet")
    b = bytearray(pkt)
    b[j] = value & 0xFF
    struct.pack_into(">H", b, 0, len(b))
    return bytes(b)


def build_hb_0049_fixed_temp(temp_c: int) -> bytes:
    # App d.run() does: "06100001" + g(q0 + 40, 2)
    # So on-wire byte = (temp_c + 40).
    return patch_single_byte_field(_HB_73, b"\x06\x10\x00\x01", int(temp_c) + 40)


def send_nav_mode_kick(sock: socket.socket, dest: tuple[str, int], seq: RollingSeq) -> None:
    """
    Try to reproduce the app's "enter navigation mode" messages.
    From NavigationRootFragment joystick handlers:
      - F0(): sends q3c.q then q3c.r (when lists empty)
      - w0(): sends q3c.z2 (start navigation command)
    """
    for hex_str in (Q3C_Z2_START_NAV, Q3C_Q_NAV_CTX, Q3C_R_EMPTY_LISTS):
        pkt = binascii.unhexlify(hex_str)
        sock.sendto(patch_k1g_seq(pkt, seq.consume()), dest)


def send_nav_mode_kick_tx(tx: K1GTx) -> None:
    for hex_str in (Q3C_Z2_START_NAV, Q3C_Q_NAV_CTX, Q3C_R_EMPTY_LISTS):
        tx.send_hex(hex_str)


def hex_pad(value: int, width_nibbles: int) -> str:
    # Same as l4d.d(i, width): lower nibbles, big-endian hex chars.
    return f"{value & ((1 << (4 * width_nibbles)) - 1):0{width_nibbles}X}"


def _pack_total_len_be16(pkt: bytes) -> bytes:
    b = bytearray(pkt)
    if len(b) < 2:
        raise ValueError("packet too short")
    struct.pack_into(">H", b, 0, len(b))
    return bytes(b)


def _music_tlv_from_ratio(r: float) -> str:
    """
    Approximate `REForeGroundService.d/e` music volume bucket selection.
    The Android code compares stream volume against deciles of max volume; we collapse that to a 0..1 ratio.
    """
    x = float(r)
    if x <= 0.0:
        return Q3C_N1
    idx = int(x * 10.0)
    if idx < 0:
        idx = 0
    if idx > 9:
        idx = 9
    return [Q3C_O1, Q3C_P1, Q3C_Q1, Q3C_R1, Q3C_S1, Q3C_T1, Q3C_U1, Q3C_V1, Q3C_W1, Q3C_X1][idx]


def _alarm_tlv_from_ratio(r: float) -> str:
    """Approximate alarm stream bucket selection (stream 0 in the app)."""
    x = float(r)
    if x <= 0.0:
        return Q3C_Y1
    idx = int(x * 10.0)
    if idx < 0:
        idx = 0
    if idx > 9:
        idx = 9
    return [Q3C_Z1, Q3C_A1, Q3C_B1, Q3C_C1, Q3C_D1, Q3C_E1, Q3C_F1, Q3C_G1, Q3C_H1, Q3C_I1][idx]


def build_metadata_0030_e(
    *,
    cell_signal_0_255: int,
    music_ratio_0_1: float,
    nav_distance_rounded: int,
    alarm_ratio_0_1: float,
    call_tail_hex: str,
) -> bytes:
    """
    `REForeGroundService.e.run()`:
      sb = z12.a("0030000600000000020100054B31472000", strConcat, str, strConcat2, str2);
      sb.append(str3);
    """
    str_concat = "06080001" + hex_pad(int(cell_signal_0_255), 2)
    str_music = _music_tlv_from_ratio(music_ratio_0_1)
    str_concat2 = Q3C_Q2 + hex_pad(int(nav_distance_rounded), 4)
    str2 = _alarm_tlv_from_ratio(alarm_ratio_0_1)
    inner = "0030000600000000020100054B31472000" + str_concat + str_music + str_concat2 + str2 + call_tail_hex
    return _pack_total_len_be16(binascii.unhexlify(inner))


def build_0044_heartbeat_d_no_cell(
    *,
    fixed_temp_c: int,
    cell_signal_0_255: int,
    battery_pct_0_100: int,
    gps_on: bool,
    charging: bool,
    music_ratio_0_1: float,
    nav_distance_rounded: int,
    alarm_ratio_0_1: float,
    call_tail_hex: str,
) -> bytes:
    """
    `REForeGroundService.d.run()` when `strA5 == null` and `blf.y().q0() != 200`:

      z12.a("0044000A00000000020100054B31472000", strConcat, "06100001"+g(q0+40,2), strA, strA3)
      wf9.a(sbA, strA4, str, strConcat2, str4)
      sbA.append(str5)
    """
    str_concat = "06080001" + hex_pad(int(cell_signal_0_255), 2)
    temp_byte = (int(fixed_temp_c) + 40) & 0xFF
    temp = "06100001" + hex_pad(temp_byte, 2)
    str_gps = Q3C_V + (Q3C_A if gps_on else Q3C_B)
    bat_byte = (int(battery_pct_0_100) + 100) & 0xFF
    str_bat = Q3C_U + hex_pad(bat_byte, 2)
    str_chg = Q3C_T + (Q3C_A if charging else Q3C_B)
    str_music = _music_tlv_from_ratio(music_ratio_0_1)
    str_concat2 = Q3C_Q2 + hex_pad(int(nav_distance_rounded), 4)
    str_alarm = _alarm_tlv_from_ratio(alarm_ratio_0_1)
    inner = (
        "0044000A00000000020100054B31472000"
        + str_concat
        + temp
        + str_gps
        + str_bat
        + str_chg
        + str_music
        + str_concat2
        + str_alarm
        + call_tail_hex
    )
    return _pack_total_len_be16(binascii.unhexlify(inner))


def decode_ic_to_app_segments(data: bytes) -> list[dict[str, object]]:
    # Same slicing model as clk.T()/clk.U(): binary segments starting at offset 8.
    out: list[dict[str, object]] = []
    if len(data) < 8:
        return out
    outer_len = (data[0] << 8) | data[1]
    seg_count = (data[2] << 8) | data[3]
    off = 8
    for idx in range(seg_count):
        if off + 4 > len(data):
            break
        t = data[off]
        sub = data[off + 1]
        seg_len = (data[off + 2] << 8) | data[off + 3]
        off += 4
        if off + seg_len > len(data):
            seg_payload = data[off:]
            off = len(data)
        else:
            seg_payload = data[off : off + seg_len]
            off += seg_len
        seg_hex = bytes([t, sub, (seg_len >> 8) & 0xFF, seg_len & 0xFF]).hex().upper() + seg_payload.hex().upper()
        out.append(
            {
                "outer_len": outer_len,
                "seg_count": seg_count,
                "idx": idx,
                "type": f"{t:02X}",
                "sub": f"{sub:02X}",
                "len": seg_len,
                "payload_hex_preview": seg_payload.hex().upper()[:48],
                "seg_hex": seg_hex,
            }
        )
    return out


Q3C_L2 = "0016000200000000020100054B314720000611000155"
Q3C_K2 = "0016000200000000020100054B314720000612000155"
Q3C_R2 = "0016000200000000020100054B314720000680000105"
Q3C_T2 = "0016000200000000020100054B31472000068000010A"
Q3C_U2 = "0016000200000000020100054B314720000680000106"
Q3C_V2 = "0016000200000000020100054B314720000680000107"
Q3C_S2 = "0016000200000000020100054B314720000680000109"
Q3C_J2 = "0016000200000000020100054B314720000680000122"


def handle_auth_segment(seg_hex: str, tx: K1GTx, auth: AuthState) -> bool:
    """
    Mirror `bluconnect/clk.X(String)`:
      segment hex layout (uppercase): TT SS LLLL PP...
      substring(2,4)  = sub byte     ("00" = modulus, "03" = exponent, "01" = status)
      substring(8,..) = payload hex

    Returns True if this segment was an auth segment (even if it didn't
    trigger a send), so the main dispatcher can skip other handlers.
    """
    if not seg_hex.startswith("07"):
        return False
    sub = seg_hex[2:4]
    payload_hex = seg_hex[8:]
    triggered = False

    if sub == "00":
        print(
            f"  [AUTH] dash → modulus ({len(payload_hex)//2} B): {payload_hex[:32]}…",
            file=sys.stderr,
        )
        triggered = auth.ingest(modulus_hex=payload_hex, exponent_hex=None)
    elif sub == "03":
        print(f"  [AUTH] dash → exponent: {payload_hex}", file=sys.stderr)
        triggered = auth.ingest(modulus_hex=None, exponent_hex=payload_hex)
    elif sub == "01":
        status = payload_hex[:2].upper()
        if status == "01":
            print("  [AUTH] *** authentication OK (07 01 01) ***", file=sys.stderr)
            auth.authenticated.set()
        else:
            auth.reset_for_retry()
            print(
                f"  [AUTH] auth status=0x{status} (not success) — retry #{auth.retry_count}; resending q3c.e",
                file=sys.stderr,
            )
            # NavigationRootFragment.V0(): up to 5 retries of q3c.e.
            if auth.retry_count <= 5:
                tx.send_hex(Q3C_E_REQUEST_AUTH)
        return True
    else:
        # Unknown 07 sub-type: log but swallow so we don't fall through to
        # the 09-ACK dispatcher.
        print(f"  [AUTH] unknown 07 sub=0x{sub} payload={payload_hex[:32]}…", file=sys.stderr)
        return True

    if triggered:
        try:
            assert auth.modulus_hex is not None
            assert auth.exponent_hex is not None
            assert auth.aes_key is not None
            ct = _rsa_encrypt_session_key(
                auth.ssid, auth.modulus_hex, auth.exponent_hex, auth.aes_key
            )
        except Exception as exc:
            print(f"  [AUTH] RSA encrypt failed: {exc}", file=sys.stderr)
            return True
        pkt = build_q3c_d_packet(ct)
        print(
            f"  [AUTH] → TX q3c.d (RSA({len(ct)}B) of ssid='{auth.ssid}' ‖ aes_key[32B])",
            file=sys.stderr,
        )
        tx.send(pkt)
    return True


def handle_dash_segment_and_respond(seg_hex: str, tx: K1GTx) -> None:
    """
    Best-effort port of clk.b0()/Q()/Z()/a0()/d0() response behavior.
    We only implement branches that trigger outbound K1G sends.
    """
    # 09 06 -> Z(): if payload byte == 55 -> send q3c.L2
    if seg_hex.startswith("09060001") and seg_hex.endswith("55"):
        print("  -> TX K1G q3c.L2 (ack 0906)", file=sys.stderr)
        tx.send_hex(Q3C_L2)
        return
    # 09 04 -> a0(): if payload byte == 55 -> send q3c.K2
    if seg_hex.startswith("09040001") and seg_hex.endswith("55"):
        print("  -> TX K1G q3c.K2 (ack 0904)", file=sys.stderr)
        tx.send_hex(Q3C_K2)
        return
    # 09 0A -> d0(): if payload byte == 55 -> send q3c.t2
    if seg_hex.startswith("090A0001") and seg_hex.endswith("55"):
        print("  -> TX K1G q3c.t2 (ack 090A)", file=sys.stderr)
        tx.send_hex(Q3C_T2)
        return

    #
    # App responses for "09 00" command family (see clk.D/E/F/c0/d0/u):
    # - D(): if message contains q3c.e1 (0900000106) -> send q3c.u2
    # - E(): if message contains q3c.f1 (0900000107) -> send q3c.v2
    # - F(): if message contains q3c.c1 (0900000109) -> send q3c.s2
    # - c0(): if message contains q3c.b1 (0900000105) -> send q3c.r2
    # - d0(): if message contains q3c.d1 (090000010A) -> send q3c.t2
    # - u():  if message contains q3c.p2 (0900000122) -> send q3c.J2
    #
    # Note: the app uses substring checks (StringsKt.n3), not strict equality, so we do the same.
    if "0900000106" in seg_hex:
        print("  -> TX K1G q3c.u2 (ack 0900000106)", file=sys.stderr)
        tx.send_hex(Q3C_U2)
        return
    if "0900000107" in seg_hex:
        print("  -> TX K1G q3c.v2 (ack 0900000107)", file=sys.stderr)
        tx.send_hex(Q3C_V2)
        return
    if "0900000109" in seg_hex:
        print("  -> TX K1G q3c.s2 (ack 0900000109)", file=sys.stderr)
        tx.send_hex(Q3C_S2)
        return
    if "0900000105" in seg_hex:
        print("  -> TX K1G q3c.r2 (ack 0900000105)", file=sys.stderr)
        tx.send_hex(Q3C_R2)
        return
    if "090000010A" in seg_hex:
        print("  -> TX K1G q3c.t2 (ack 090000010A)", file=sys.stderr)
        tx.send_hex(Q3C_T2)
        return
    if "0900000122" in seg_hex:
        print("  -> TX K1G q3c.J2 (ack 0900000122)", file=sys.stderr)
        tx.send_hex(Q3C_J2)
        return


def listen_2002(
    sock: socket.socket,
    stop: threading.Event,
    tx: K1GTx,
    respond: bool,
    auth: AuthState | None,
) -> None:
    sock.settimeout(0.5)
    seg_counts: Counter[str] = Counter()
    last_report = time.time()
    while not stop.is_set():
        try:
            data, addr = sock.recvfrom(65535)
        except socket.timeout:
            now = time.time()
            if now - last_report >= 10.0:
                if seg_counts:
                    top = ", ".join(f"{k}={v}" for k, v in seg_counts.most_common(8))
                    print(f"RX summary (last ~10s): {top}", file=sys.stderr)
                seg_counts.clear()
                last_report = now
            continue
        except OSError:
            return
        # User request: log anytime bike sends us a message.
        b8 = data[8] if len(data) > 8 else None
        print(
            f"RX 2002 from {addr[0]}:{addr[1]} len={len(data)} outer=0x{((data[0]<<8)|data[1]):04x}"
            + (f" [8]=0x{b8:02x}" if b8 is not None else ""),
            file=sys.stderr,
        )
        segs = decode_ic_to_app_segments(data)
        for s in segs[:20]:
            seg_counts[f"{s['type']}.{s['sub']}"] += 1
            print(
                f"  seg[{s['idx']}] type={s['type']} sub={s['sub']} len={s['len']}B hex={s['seg_hex']}",
                file=sys.stderr,
            )
            seg_hex = str(s["seg_hex"])
            # Auth segments (07 xx) must be processed even when --respond-2002
            # is off, otherwise the dash never leaves "waiting for phone" and
            # nothing else will ever come through.
            if auth is not None and seg_hex.startswith("07"):
                try:
                    handle_auth_segment(seg_hex, tx, auth)
                except Exception as exc:
                    print(f"  [AUTH] handler error: {exc}", file=sys.stderr)
                continue
            # 09 06 55 → q3c.L2 is the dash's per-IDR "frame decoded" ACK.
            # The dash sends this after successfully decoding an IDR frame and
            # expects q3c.L2 (0611 0001 55) in return before advancing its
            # decoder state. Without this ACK the decoder resets on the
            # watchdog timer and we get the 1 Hz loading-dots ↔ nav-icon
            # flash loop (observed in capture12: 09 06 55 sent twice, never
            # answered, and the flash persists at exactly the 1 Hz IDR rate).
            # Treat this the same as auth segments: always respond, regardless
            # of --respond-2002.
            if seg_hex.upper().startswith("09060001") and seg_hex.upper().endswith("55"):
                print("  -> TX K1G q3c.L2 (mandatory: ack 0906 frame-decoded)", file=sys.stderr)
                try:
                    tx.send_hex(Q3C_L2)
                except Exception as exc:
                    print(f"  [ACK] q3c.L2 send error: {exc}", file=sys.stderr)
                continue
            if respond:
                try:
                    handle_dash_segment_and_respond(seg_hex, tx)
                except Exception:
                    pass


def open_broadcast_socket(bind_ip: str | None, port: int) -> socket.socket:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    except OSError:
        pass
    try:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    except OSError:
        pass
    s.bind(((bind_ip or ""), port))
    return s

def open_listen_socket_2002(bind_ip: str | None, port: int = 2002) -> socket.socket:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    except OSError:
        pass
    s.bind(((bind_ip or ""), port))
    return s


def send_initial_burst(
    tx: K1GTx,
    hostname: str,
    pause_s: float,
    fixed_temp_c: int,
) -> None:
    for h in INITIAL_BURST_HEX:
        if h is None:
            payload = build_hostname_announce(hostname)
        else:
            payload = binascii.unhexlify(h)
            # Patch the initial 0044 status packet's temp field too, so it matches the 1Hz heartbeat.
            if payload.startswith(b"\x00\x44"):
                try:
                    payload = patch_single_byte_field(payload, b"\x06\x10\x00\x01", int(fixed_temp_c) + 40)
                except ValueError:
                    pass
        tx.send(payload)
        if pause_s > 0:
            time.sleep(pause_s)


def tick_loop(tx: K1GTx, stop: threading.Event, args: argparse.Namespace) -> None:
    # REForeGroundService uses multiple 1Hz TimerTasks:
    # - d.run(): sends 0044/0049 (battery/gps/call state + volumes + distance)
    # - e.run(): sends 0030 (signal + volumes + distance + mode flag)
    #
    # We replicate the *shape* and cadence with fixed placeholders on macOS.
    call_tail = str(args.tick_call_tail_hex).strip().upper()
    while not stop.wait(timeout=1.0):
        try:
            if str(args.tick_heartbeat).upper() == "0049":
                tx.send(build_hb_0049_fixed_temp(int(args.fixed_temp_c)))
            else:
                tx.send(
                    build_0044_heartbeat_d_no_cell(
                        fixed_temp_c=int(args.fixed_temp_c),
                        cell_signal_0_255=int(args.tick_cell_signal),
                        battery_pct_0_100=int(args.tick_battery),
                        gps_on=not bool(args.tick_gps_off),
                        charging=bool(args.tick_charging),
                        music_ratio_0_1=float(args.tick_music_level),
                        nav_distance_rounded=int(args.tick_nav_distance),
                        alarm_ratio_0_1=float(args.tick_alarm_level),
                        call_tail_hex=call_tail,
                    )
                )
            tx.send(
                build_metadata_0030_e(
                    cell_signal_0_255=int(args.tick_cell_signal),
                    music_ratio_0_1=float(args.tick_music_level),
                    nav_distance_rounded=int(args.tick_nav_distance),
                    alarm_ratio_0_1=float(args.tick_alarm_level),
                    call_tail_hex=call_tail,
                )
            )
        except OSError:
            pass


def route_card_keepalive_loop(
    tx: "K1GTx",
    stop: threading.Event,
    route_pkt: bytes,
    period_s: float = 1.0,
) -> None:
    """
    Resend the 0x007E route card at ~1 Hz while streaming. The real phone
    does this constantly (verified in nav_open_ok.pcap: 007E at t=18.824,
    19.830, 20.830, 21.850, 22.819, 23.860, 24.813 — ~1 s cadence).

    Without this, the dash accepts the initial route card, allocates the
    nav decoder surface and starts consuming our RTP, but its "destination
    still valid" watchdog fires after ~15–20 s of no 007E refresh and
    tears the decoder back down → the user sees loading dots → timeout
    even though UDP/5000 was open the whole time (exact symptom captured
    in capture_bike_to_dash2.pcapng).
    """
    while not stop.wait(timeout=period_s):
        try:
            tx.send(route_pkt)
        except Exception as exc:  # pragma: no cover - defensive
            print(f"route card keepalive failed: {exc}", file=sys.stderr)


def projection_heartbeat_loop(
    tx: "K1GTx",
    stop: threading.Event,
    frame_rate_hz: float = 4.0,
) -> None:
    """
    Emit q3c.g (0556 0001 55) at the encoder frame rate for as long as
    we're "projecting". NavigationFragment.l9(Bitmap) calls n9(q3c.g)
    once per rendered bitmap and that heartbeat is what actually gates
    the dash's nav surface — without it, the RTP stream on UDP/5000 is
    ignored no matter how well-formed it is.
    """
    period = 1.0 / max(1.0, frame_rate_hz)
    while not stop.wait(timeout=period):
        try:
            tx.send_hex(Q3C_G_PROJ_FRAME)
        except Exception as exc:  # pragma: no cover - defensive
            print(f"projection heartbeat send failed: {exc}", file=sys.stderr)
            # keep looping; transient socket errors shouldn't kill nav


# ----- Active-navigation "next-turn" packet --------------------------------
#
# Once the dash has opened the nav screen (from q3c.z2) AND is getting pixels
# (q3c.g/q3c.w + RTP/5000), it still sits on a "three dots loading" placeholder
# and eventually times out unless the phone starts streaming a per-route-step
# TLV packet that populates the instruction bubble (maneuver icon, distance
# to next turn, road name, ETA, total distance remaining, …).
#
# In the stock app this packet is built by `t3c.c()` and sent from
# `NavigationFragment.v7()` → `s3c.c()` every time Google Navigation fires an
# onRouteProgressChanged callback (≈ 1 Hz while moving).
#
# Wire format (t3c.w() + t3c.u() + t3c.v()):
#
#   <outer_len:2>                            ← t3c.v(): total packet length
#   <segment_count:2>                        ← t3c.u(): number of TLVs + 1
#   00 00 00 00 02 01 00 05                  ← fixed K1G header bytes
#   4B 31 47 20                              ← "K1G " magic
#   00                                       ← rolling seq (patched by K1GTx)
#   <TLVs...>
#
# Each TLV is `05 <type> <len:2> <value>`, with these fields (ordered as in
# t3c.w, most skippable when zero/null):
#
#   type 0x01  m()  road name            — ASCII + 0x00 terminator
#   type 0x02  g()  primary maneuver     — 1-byte enum (0x0B ≈ "continue")
#   type 0x03  n()  secondary maneuver   — 2 bytes
#   type 0x04  h()  primary distance     — 2-byte uint, meters
#   type 0x05  o()  secondary distance   — 2 bytes
#   type 0x06  j()  primary unit         — 1 byte on wire (decimal-int
#                                           appended, so pass 10 = 0x10, etc.)
#   type 0x07  p()  secondary unit       — 1 byte same encoding
#   type 0x08  e()  ETA HH:MM            — 4 bytes, two ASCII pairs
#   type 0x54  f()  ETA format           — 1 byte
#   type 0x09  q()  total distance       — 2 bytes
#   type 0x46  r()  total distance unit  — 1 byte
#   type 0x0A  d()  decimal separator    — 1 byte, q3c.A=0x55 ("."),
#                                                   q3c.B=0xAA (",")
#   type 0x0C  i()  extra counter        — 1 byte
#   q3c.S2=050B0006 + DDHHMM ASCII       — remaining time (skip when unused)
#   q3c.T2=05550001 + unit               — remaining time unit
#   type 0x05 + q3c.y/z (0605 0001 55|AA)  projection-on flag
#   type 0x06 + q3c.i/j (060D 0001 55|AA)  decimal-notation flag
#
# For our case we don't have live routing data, so we synthesise a plausible
# static "continue straight 500m" packet and refresh it at 1 Hz. That's enough
# to stop the dash's watchdog from timing out the nav screen.

# q3c.c: static bytes between the segment-count field and the first TLV.
_NAV_K1G_HDR = "00000000020100054B31472000"

# Maneuver enum bytes seen in the decomp. 0x0B shows up as the generic
# "continue / straight ahead" glyph in the dash resource tables, so it's a
# safe placeholder when we don't have a real route step.
NAV_MANEUVER_CONTINUE = 0x0B

# Distance unit bytes. `t3c.j/p/r` serialize the unit via `sb.append(int)`,
# which emits the decimal digits of the int as ASCII characters. The whole
# K1G payload is then hex→bytes decoded, so the decimal STRING "30" becomes
# the literal byte 0x30 on the wire. Cross-checked against nav_open_ok.pcap
# which shows `05 06 00 01 30` for a "500 m" next-turn instruction.
#
# From bluconnect.kvc.d() the four legal codes are:
#   10 → tenths of a kilometre  (distance field carries km × 10, e.g. 23 → "2.3 km")
#   20 → tenths of a mile       (imperial, distance field carries mi × 10)
#   30 → plain metres
#   50 → plain feet
NAV_UNIT_KM_TENTHS = 10
NAV_UNIT_MILES_TENTHS = 20
NAV_UNIT_METERS = 30
NAV_UNIT_FEET = 50

# Back-compat aliases used by callers that haven't updated yet.
NAV_UNIT_KM = NAV_UNIT_KM_TENTHS
NAV_UNIT_MILES = NAV_UNIT_MILES_TENTHS


def _nav_tlv_primary_maneuver(code: int) -> str:
    """05 02 0001 <code:1>  — t3c.g()"""
    return f"050200010{code:X}" if code < 0x10 else f"05020001{code:02X}"


def _nav_tlv_primary_distance(meters: int) -> str:
    """05 04 0002 <meters:2>  — t3c.h()"""
    return f"05040002{meters & 0xFFFF:04X}"


def _nav_tlv_primary_unit(unit_decimal: int) -> str:
    """05 06 0001 <unit>  — t3c.j(): the int is appended as decimal ASCII,
    so a value like 10 becomes the two-char literal "10" (wire byte 0x10)."""
    return f"05060001{unit_decimal:d}"


def _nav_tlv_total_distance(meters: int) -> str:
    """05 09 0002 <meters:2>  — t3c.q()"""
    return f"05090002{meters & 0xFFFF:04X}"


def _nav_tlv_total_distance_unit(unit_decimal: int) -> str:
    """05 46 0001 <unit>  — t3c.r() (type 70 = 0x46)"""
    return f"05460001{unit_decimal:d}"


def _nav_tlv_decimal_separator(use_comma: bool) -> str:
    """05 0A 0001 <55|AA>  — t3c.d() with q3c.A/B"""
    return "050A0001" + ("AA" if use_comma else "55")


def _nav_tlv_projection_flag(on: bool) -> str:
    """06 05 0001 <55|AA>  — t3c.s() with q3c.y/z"""
    return "06050001" + ("55" if on else "AA")


def _nav_tlv_decimal_flag(on: bool) -> str:
    """06 0D 0001 <55|AA>  — t3c.t() with q3c.i/j"""
    return "060D0001" + ("55" if on else "AA")


def build_active_nav_packet(
    *,
    primary_maneuver: int = NAV_MANEUVER_CONTINUE,
    primary_distance_m: int = 500,
    primary_unit: int = NAV_UNIT_METERS,
    total_distance_m: int = 500,
    total_distance_unit: int = NAV_UNIT_METERS,
    use_comma_decimal: bool = False,
    projection_on: bool = True,
    decimal_fmt_on: bool = False,
) -> bytes:
    """
    Build a minimal but valid t3c-style active-navigation K1G packet. All
    "optional" TLVs (ETA, road name, remaining-time, secondary maneuver …)
    are left out; the dash is fine with just a primary turn + totals as long
    as *something* arrives at ~1 Hz.

    `decimal_fmt_on` now defaults to **False** to mirror the real phone.
    The working pcap shows `06 0D 0001 AA` (=OFF) so that whole-metre values
    like "500 m" render as integers instead of being reformatted with a
    fractional separator.
    """
    tlvs: list[str] = []
    tlvs.append(_nav_tlv_primary_maneuver(primary_maneuver))
    tlvs.append(_nav_tlv_primary_distance(primary_distance_m))
    tlvs.append(_nav_tlv_primary_unit(primary_unit))
    tlvs.append(_nav_tlv_total_distance(total_distance_m))
    tlvs.append(_nav_tlv_total_distance_unit(total_distance_unit))
    tlvs.append(_nav_tlv_decimal_separator(use_comma_decimal))
    tlvs.append(_nav_tlv_projection_flag(projection_on))
    tlvs.append(_nav_tlv_decimal_flag(decimal_fmt_on))

    payload_hex = "".join(tlvs)
    seg_count = len(tlvs) + 1  # t3c.u(): d+1
    inner_hex = f"{seg_count:04X}" + _NAV_K1G_HDR + payload_hex
    inner_bytes = len(inner_hex) // 2
    outer_len = inner_bytes + 2  # t3c.v(): bytes + 2 for the length field
    full_hex = f"{outer_len:04X}" + inner_hex
    return bytes.fromhex(full_hex)


def nav_info_loop(
    tx: "K1GTx",
    stop: threading.Event,
    pkt_factory,
    period_s: float = 1.0,
) -> None:
    """
    Send an active-navigation TLV packet every `period_s` seconds. Mirrors
    what the real app does from `NavigationFragment.j.a(mvc)` →
    `NavigationFragment.v7()` → `s3c.c()` on every onRouteProgressChanged
    callback (which Google Navigation fires at ~1 Hz while guiding).

    `pkt_factory` is a zero-arg callable returning the current packet bytes,
    so the caller can swap in dynamic route data later.
    """
    # Emit one immediately so the dash doesn't wait a whole period for its
    # first instruction bubble update.
    try:
        tx.send(pkt_factory())
    except Exception as exc:  # pragma: no cover - defensive
        print(f"initial nav info send failed: {exc}", file=sys.stderr)
    while not stop.wait(timeout=period_s):
        try:
            tx.send(pkt_factory())
        except Exception as exc:  # pragma: no cover - defensive
            print(f"nav info send failed: {exc}", file=sys.stderr)


#
# Video format expected by the dash, reverse-engineered from the decompiled app:
#   - NavigationFragment.E7() builds a bvb/dvb MediaFormat with:
#       * codec   = tak.h            = "video/avc" (H.264)
#       * width   = tak.b            = 526
#       * height  = 300              (literal in E7)
#       * iFrameInterval = 1 second  (aVar.s(1))
#       * framerate = avbVar.getFramerate()   (default 4, from avb.<init>)
#       * bitrate   = avbVar.getBitrate()     (default 204800, from avb.<init>)
#   - Frames are rendered from the MapView, scaled to 526x300 (see ibf.java
#     `Bitmap.createScaledBitmap(..., tak.b, 300, true)`), and fed to a
#     surface-input MediaCodec.
#   - The encoded NALs leave the phone as RFC 6184 H.264 RTP on UDP/5000.
#
# The dash's embedded decoder is tuned to *exactly* this shape (baseline
# profile, tiny frame, low bitrate). Anything much bigger/faster (e.g. 1080p
# @ 30 fps that a laptop camera or stock MP4 would produce) is silently
# dropped and the nav screen stays blank even though q3c.z2 is accepted.
#
DASH_VIDEO_WIDTH = 526
DASH_VIDEO_HEIGHT = 300
DASH_VIDEO_FPS = 4
DASH_VIDEO_BITRATE_BPS = 204_800
DASH_VIDEO_GOP_SEC = 1


# Phone's exact SPS and PPS bytes, captured by FU-A reassembly of
# nav_open_ok.pcap (see claude_conversation/ and PROTOCOL.md §9).
# Rewriting ffmpeg's SPS/PPS with these makes the parameter sets
# byte-for-byte identical to the working real-phone stream.
PHONE_SPS = bytes.fromhex("674200298d8d404213f5735050105078442350")  # 19 B
PHONE_PPS = bytes.fromhex("68ca43c8")                                 # 4 B


def _normalize_sps_constraints_for_dash(sps: bytes) -> bytes:
    """Keep x264's parse-critical SPS fields, but match the phone's constraint byte.

    libx264 emits Baseline as 67 42 c0 29..., while the Tripper phone stream
    advertises 67 42 00 29.... The constraint byte does not change slice
    header parsing, but the dash appears to whitelist the phone shape before
    letting the decoder leave the loading-logo state.
    """
    if len(sps) >= 4 and (sps[0] & 0x1F) == 7 and sps[1] == 0x42 and sps[3] == 0x29:
        return sps[:2] + b"\x00" + sps[3:]
    return sps


def _bundle_sps_pps_idr(sps: bytes, pps: bytes, idr: bytes) -> bytes:
    """Pack SPS + PPS + IDR into one compound payload using embedded
    Annex-B start codes, exactly like nav_open_ok.pcap shows the real
    phone doing (see PROTOCOL.md §4.7b). The dash reassembles the
    surrounding FU-A chain into a single "NAL" whose first byte is the
    SPS header (0x67), then Annex-B-splits the result internally to
    recover SPS / PPS / IDR. This is non-standard per RFC 6184 but is
    what the Tripper firmware expects."""
    return sps + b"\x00\x00\x00\x01" + pps + b"\x00\x00\x00\x01" + idr


def _find_startcode(buf: bytes | bytearray, start: int) -> tuple[int, int]:
    """Return (pos, sc_len) of the first Annex-B start code at or after `start`.

    Returns (-1, 0) if none found. sc_len is 3 (00 00 01) or 4 (00 00 00 01).
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
    chunk_size: int = 65536,
) -> Iterator[bytes]:
    """
    Generator that yields raw NAL units (no start code) parsed from an
    H.264 Annex-B byte stream on `stream` until EOF or `stop` is set.

    We hand-roll this rather than use av/pyav so the same code works with
    ffmpeg-piped output on any platform and we stay single-dependency.
    """
    buf = bytearray()
    while not stop.is_set():
        try:
            chunk = stream.read(chunk_size)
        except Exception:
            break
        if not chunk:
            break
        buf.extend(chunk)

        # Emit every NAL that is now fully bracketed by two start codes.
        while True:
            sc_pos, sc_len = _find_startcode(buf, 0)
            if sc_pos < 0:
                # No start code yet — keep accumulating.
                break
            if sc_pos > 0:
                # Drop any garbage before the first start code.
                del buf[:sc_pos]
                sc_pos = 0
            next_sc_pos, _ = _find_startcode(buf, sc_len)
            if next_sc_pos < 0:
                # Need more bytes to know where this NAL ends.
                break
            nal = bytes(buf[sc_len:next_sc_pos])
            del buf[:next_sc_pos]
            if nal:
                yield nal

    # EOF: flush any trailing NAL still in the buffer.
    sc_pos, sc_len = _find_startcode(buf, 0)
    if sc_pos == 0 and len(buf) > sc_len:
        trailing = bytes(buf[sc_len:])
        if trailing:
            yield trailing


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
    """
    Packetize ONE access unit (list of NAL units) into RTP packets and send.

    Each NAL is either a single-NAL RTP packet (size <= fu_force_threshold)
    or is split into FU-A fragments (nal_type 28). NO STAP-A aggregation —
    the Tripper dash's embedded H.264 decoder chokes on STAP-A indicators
    (NAL type 24), which is what ffmpeg's RTP muxer emits by default in
    8.x for small adjacent NALs.

    The real phone's pattern (captured in nav_open_ok.pcap):
      * ~99 % of packets are FU-A fragments (starting with 0x5C/0x7C),
        because real video frames are ~70 kB each.
      * < 1 % are single-NAL packets for tiny non-IDR slices.
      * It NEVER emits an FU-A with S=1 AND E=1 (that would be a single-
        fragment FU-A, which some decoders reject as malformed).

    So: if the NAL fits in one packet, send it as single-NAL. Otherwise,
    FU-A fragment across ≥ 2 packets.

    Marker bit is set on the *very last* RTP packet of the AU.
    Returns the number of packets sent.
    """
    if not nals:
        return 0

    sent = 0
    # Precompute packet plan so we know which packet is last (→ marker=1).
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
            fu_indicator = f_nri | 28  # FU-A type
            payload = nal[1:]
            chunk_size = max_payload - 2  # minus FU indicator + FU header
            if chunk_size <= 0:
                continue
            offset = 0
            while offset < len(payload):
                chunk = payload[offset : offset + chunk_size]
                is_first = offset == 0
                offset += chunk_size
                is_last = offset >= len(payload)
                fu_header = (0x80 if is_first else 0) | (0x40 if is_last else 0) | nal_type
                plan.append(bytes((fu_indicator, fu_header)) + chunk)

    if not plan:
        return 0

    last_idx = len(plan) - 1
    for idx, body in enumerate(plan):
        marker = 1 if idx == last_idx else 0
        hdr = struct.pack(
            ">BBHII",
            0x80,                          # V=2, P=0, X=0, CC=0
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


def rtp_packetizer_loop(
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
    by AUD NAL type 9) and send as RTP to <bike_ip>:<rtp_port>. Intended
    to run on a background thread.

    Timestamps are wall-clock-derived (90 kHz RTP clock) so they stay
    monotonic even if ffmpeg's `-re` pacing drifts.

    `max_fps` is a *safety valve*: if ffmpeg ever runs unpaced (e.g.
    forgot `-re` on a lavfi source), we hold the packetizer thread back
    so we never exceed this AU rate. This applies backpressure through
    ffmpeg's stdout pipe and prevents the kind of 6,000-pkt/s flood that
    makes the dash drop the Wi-Fi association entirely (observed in
    capture_bike_to_dash6.pcapng).
    """
    if ssrc is None:
        ssrc = random.getrandbits(32)
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 1 << 19)
    dst = (bike_ip, rtp_port)

    seq = [random.randint(0, 0xFFFF)]
    ts_base = random.getrandbits(32)
    t0 = time.time()
    min_au_interval = 1.0 / max(0.1, max_fps)
    last_au_send = 0.0

    def now_ts() -> int:
        return (ts_base + int((time.time() - t0) * 90000)) & 0xFFFFFFFF

    # Buffer NALs until we hit the next AUD (type 9), which delimits the
    # next access unit in libx264 output with `aud=1`.
    au_nals: list[bytes] = []
    total_pkts = 0
    total_aus = 0

    # Live SPS/PPS captured from the ffmpeg bitstream (first IDR AU).
    # We use these — not PHONE_SPS/PHONE_PPS — so that the slice-header
    # fields the decoder parses (log2_max_frame_num, pic_order_cnt_type,
    # log2_max_pic_order_cnt_lsb, …) match what libx264 actually encoded.
    #
    # PHONE_SPS has log2_max_frame_num=16 and pic_order_cnt_type=0, but
    # libx264 with bframes=0 produces log2_max_frame_num=4 and
    # pic_order_cnt_type=2 (no POC fields in slice headers).  Using
    # PHONE_SPS as the in-band parameter set causes the dash's decoder to
    # read 12 extra garbage bits for every frame_num and then 16 bits for
    # a pic_order_cnt_lsb that is not present — misaligning every
    # subsequent field in every slice header → every frame fails to decode
    # → the dash retries on each IDR (once per second) → 1 Hz flashing.
    live_sps: bytes | None = None
    live_pps: bytes | None = None

    def _bundle_if_idr(nals: list[bytes]) -> list[bytes]:
        """If any NAL in this AU is an IDR slice (type 5), replace the
        first IDR with a compound SPS+PPS+IDR blob using embedded
        Annex-B start codes (see _bundle_sps_pps_idr). Mirrors the
        real phone's per-IDR packing in nav_open_ok.pcap.

        Uses the live SPS/PPS captured from ffmpeg so that the slice
        header bit-widths (log2_max_frame_num, poc_type, …) are
        consistent with the encoded payload."""
        sps = live_sps if live_sps is not None else PHONE_SPS
        pps = live_pps if live_pps is not None else PHONE_PPS
        out: list[bytes] = []
        replaced = False
        for n in nals:
            if not replaced and (n[0] & 0x1F) == 5:
                out.append(_bundle_sps_pps_idr(sps, pps, n))
                replaced = True
            else:
                out.append(n)
        return out

    try:
        for nal in iter_annexb_nals(stream, stop):
            if not nal:
                continue
            nal_type = nal[0] & 0x1F
            if nal_type == 9:
                # AUD delimits the START of a new AU — flush whatever we
                # accumulated so far (the *previous* AU).
                if au_nals:
                    # Enforce max_fps safety valve: sleep if we're about
                    # to flush AUs faster than the cap allows.
                    elapsed_since_last = time.time() - last_au_send
                    if elapsed_since_last < min_au_interval:
                        remaining = min_au_interval - elapsed_since_last
                        if stop.wait(timeout=remaining):
                            break
                    total_pkts += _send_au_rtp(
                        sock, dst, _bundle_if_idr(au_nals), ssrc, seq,
                        now_ts(), max_payload, payload_type,
                    )
                    last_au_send = time.time()
                    total_aus += 1
                    au_nals = []
                # Drop the AUD itself (real phone doesn't send it on the
                # wire; keeps our RTP pattern identical to the stock app).
                continue
            if nal_type == 6:
                # Drop SEI NALs (user-data SEI carrying x264's version
                # string, 686 B at stream start). Real phone doesn't send
                # them; some embedded decoders warn or reset on unknown
                # SEI payloads. Harmless to strip.
                continue
            if nal_type == 7:
                # Capture the live SPS from ffmpeg (repeat-headers=1
                # ensures it appears before every IDR). We do NOT drop
                # it here — it goes into live_sps for use by
                # _bundle_if_idr so the slice headers and the in-band
                # SPS stay consistent. Only the non-parse-critical
                # constraint byte is normalized to match the phone capture.
                live_sps = _normalize_sps_constraints_for_dash(nal)
                continue
            if nal_type == 8:
                # Same for PPS.
                live_pps = nal
                continue
            au_nals.append(nal)

        # EOF — flush the last AU if we have one buffered.
        if au_nals and not stop.is_set():
            total_pkts += _send_au_rtp(
                sock, dst, _bundle_if_idr(au_nals), ssrc, seq, now_ts(),
                max_payload, payload_type,
            )
            total_aus += 1
    finally:
        sock.close()
        print(
            f"RTP packetizer exited (AUs={total_aus}, packets={total_pkts}, "
            f"SSRC=0x{ssrc:08X})",
            file=sys.stderr,
        )


@dataclass
class VideoStream:
    """Handle for the ffmpeg encoder + Python RTP packetizer pair."""
    proc: subprocess.Popen[bytes]
    thread: threading.Thread
    stop: threading.Event


def start_ffmpeg_rtp(
    video: str,
    bike_ip: str,
    rtp_port: int,
    pkt_size: int,
    extra_args: list[str],
    loop: bool,
    *,
    width: int = DASH_VIDEO_WIDTH,
    height: int = DASH_VIDEO_HEIGHT,
    fps: int = DASH_VIDEO_FPS,
    bitrate: int = DASH_VIDEO_BITRATE_BPS,
    gop_sec: int = DASH_VIDEO_GOP_SEC,
) -> VideoStream:
    """
    Start the ffmpeg encoder (H.264 Annex-B → stdout) plus a background
    thread that reads each NAL unit and packetizes it into RTP *manually*.

    Why we don't use ffmpeg's own `-f rtp rtp://…` muxer:
      ffmpeg 8.x always aggregates small adjacent NAL units (AUD + SPS +
      PPS + IDR) into a single STAP-A RTP packet (NAL type 24). The
      Tripper dash's embedded H.264 decoder does not parse STAP-A and
      silently drops the IDR, which produces the splash/loading-dots
      blink loop (captured in capture_bike_to_dash3.pcapng). The real
      phone emits single-NAL packets + FU-A fragments only, so we do the
      same ourselves.

    Encoding flags stay identical to what MediaFormat asks for in the
    stock app (526x300@4fps, 204 kbps, baseline 3.0, GOP = 1 s).
    """
    ff = shutil.which("ffmpeg")
    if ff is None:
        raise RuntimeError("ffmpeg not found in PATH (install via 'brew install ffmpeg')")
    gop_frames = max(1, fps * gop_sec)
    cmd: list[str] = [ff, "-hide_banner", "-loglevel", "warning"]

    # Two input modes:
    #   - if `video` starts with "color=" or "lavfi:", we treat it as a
    #     lavfi source spec (used by --static-image).
    #   - otherwise it's a real file.
    # In BOTH cases we use `-re` for wall-clock pacing: lavfi's `r=N`
    # option only sets the output frame *rate*, it does NOT throttle the
    # source to real time. Without -re, lavfi renders thousands of fps
    # and we blast ~7.5 Mbps of RTP at the dash (observed in
    # capture_bike_to_dash6.pcapng: 159k pkts in 25 s, after which the
    # dash dropped the Wi-Fi association entirely — ENETUNREACH in our
    # sendto loop).
    is_lavfi = video.startswith("color=") or video.startswith("lavfi:")
    if is_lavfi:
        spec = video[len("lavfi:"):] if video.startswith("lavfi:") else video
        cmd += ["-re", "-f", "lavfi", "-i", spec]
    else:
        if loop:
            cmd += ["-stream_loop", "-1"]
        cmd += ["-re", "-i", video]

    cmd += [
        "-an",                        # no audio
    ]
    if not is_lavfi:
        cmd += ["-vf", (
            f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
            f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:black,"
            f"fps={fps}"
        )]
    cmd += [
        "-c:v", "libx264",
        "-preset", "ultrafast",
        # NOTE: we intentionally do NOT use `-tune zerolatency` here.
        # `zerolatency` silently forces `--slice-max-size=1500` inside
        # libx264, which chops every frame into 3–5 H.264 slices (observed
        # in capture_bike_to_dash4.pcapng). The Tripper dash's baseline
        # decoder expects *one* slice per picture.
        "-pix_fmt", "yuv420p",
        # Match the phone's SPS as closely as possible:
        #   avc1.420029  →  profile_idc=0x42 (plain Baseline, NOT
        #   Constrained Baseline), constraint_set_flags = 0x00, level_idc
        #   = 0x29 (4.1). We were emitting avc1.42c01e (Constrained
        #   Baseline + Level 3.0), which some embedded baseline decoders
        #   reject via strict (profile, constraints, level) whitelists.
        #
        # To get constraint_set_flags=0x00 with libx264 we must *not* pass
        # `-profile:v baseline` (which sets cset0+cset1). Instead we use
        # `no-scenecut=1 bframes=0 cabac=0` ourselves and rely on x264
        # emitting a plain baseline SPS.
        "-level", "4.1",
        "-x264-params",
            # aud=1 → our packetizer uses AUDs as AU delimiters (then
            # drops them before sending). repeat-headers=1 guarantees
            # SPS+PPS are in-band on every IDR.
            # slices=1 + slice-max-size=0 + sliced-threads=0 force a
            # single slice per picture (matches the real phone).
            # force-cfr=1 + no scenecut keep us honest on GOP cadence so
            # the dash sees an IDR every `gop_frames` frames exactly.
            # NOTE: we deliberately do NOT pass `no-info=1` (not a valid
            # x264-params key; on libx264 builds that error out on
            # unknown keys it poisoned the whole param string). SEI NALs
            # from x264 are instead dropped in the Python packetizer.
            "aud=1:repeat-headers=1:scenecut=0:ref=1:bframes=0:cabac=0:"
            "slices=1:slice-max-size=0:sliced-threads=0:"
            "annexb=1:force-cfr=1:"
            "keyint={0}:min-keyint={0}".format(gop_frames),
        "-b:v", str(bitrate),
        "-maxrate", str(bitrate),
        "-bufsize", str(bitrate),
        "-r", str(fps),
        "-g", str(gop_frames),
        "-bf", "0",
        *extra_args,
        "-f", "h264", "pipe:1",       # raw Annex-B on stdout
    ]
    # Clamp max_payload: leave 42 bytes of headroom for 14-byte Ethernet,
    # 20-byte IP, 8-byte UDP headers relative to `pkt_size` (default 1400).
    max_payload = max(200, int(pkt_size) - 20)
    print(
        f"Starting ffmpeg H.264 encoder → RTP packetizer → {bike_ip}:{rtp_port} "
        f"({width}x{height}@{fps}fps, {bitrate//1000}kbps, GOP={gop_frames}, "
        f"baseline, max_rtp_payload={max_payload})",
        file=sys.stderr,
    )
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        # Leave stderr inherited so ffmpeg warnings/errors are visible.
        bufsize=0,
    )
    assert proc.stdout is not None
    stop = threading.Event()
    thr = threading.Thread(
        target=rtp_packetizer_loop,
        args=(proc.stdout, bike_ip, rtp_port, stop),
        kwargs={"max_payload": max_payload, "payload_type": 96},
        daemon=True,
        name="rtp-packetizer",
    )
    thr.start()
    return VideoStream(proc=proc, thread=thr, stop=stop)


def parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="App-like Tripper K1G sender (nav-only)")
    p.add_argument("--hostname", default="MacBook", help="Device name shown on dash")
    p.add_argument("--route-title", default="Navigation", help="Nav title for 0x007e")
    p.add_argument("--bike-ip", default="192.168.1.1", help="Bike/AP IP (used only for info)")
    p.add_argument("--broadcast", default="192.168.1.255", help="Broadcast address")
    p.add_argument("--udp-port", type=int, default=2000, help="Bind+send UDP port (2000)")
    p.add_argument("--bind-ip", default=None, help="Optional local IP to bind")
    p.add_argument("--k1g-seq-start", type=lambda x: int(x, 0), default=0, help="Starting rolling seq byte")
    p.add_argument("--burst-pause", type=float, default=0.02, help="Seconds between burst packets")
    p.add_argument("--send-nav-once", action="store_true", help="Send one 0x007e nav packet immediately after burst")
    p.add_argument(
        "--nav-mode-kick",
        action="store_true",
        help="Send nav-mode kick packets (q3c.z2, q3c.q, q3c.r) before 0x007e",
    )
    # ---- RTP video (the actual trigger for the dash's nav screen) -------
    # NavigationRootFragment.w0() sends q3c.z2 *and* NavigationFragment is
    # simultaneously spinning up the H.264 encoder (NavigationFragment.E7()
    # → bluconnect.gbf → bluconnect.xub) that pushes RTP packets to
    # UDP/5000 using MediaFormat { video/avc, 526x300, baseline, 4 fps,
    # 200 kbps, 1s I-frame }. The dash keeps the nav surface hidden until
    # it actually decodes a frame, so without a matching RTP stream z2 is
    # accepted silently and the home screen stays on the battery widget.
    p.add_argument(
        "--video",
        default=None,
        help=(
            "Path to a video file to stream as H.264 RTP to the dash on "
            "UDP/<rtp-port>. Without this, the dash has no pixels to show "
            "on the nav screen and will stay on the phone-battery home view."
        ),
    )
    p.add_argument("--rtp-port", type=int, default=5000, help="RTP destination port on the dash")
    p.add_argument(
        "--pkt-size",
        type=int,
        default=1380,
        help=(
            "Max total UDP-payload size for each RTP packet (our Python "
            "packetizer subtracts 20 B to get the RTP payload budget). The "
            "real phone's full RTP UDP payloads are 1380 B in nav_open_ok.pcap, "
            "so the default keeps FU-A boundaries aligned with the stock app."
        ),
    )
    p.add_argument(
        "--video-loop",
        action="store_true",
        help="Loop the input file forever (ffmpeg -stream_loop -1)",
    )
    p.add_argument(
        "--static-image",
        nargs="?",
        const="blue",
        default=None,
        metavar="COLOR",
        help=(
            "Diagnostic: stream a solid-color still frame instead of a real "
            "video file. Encoding path + RTP packetization are identical to "
            "--video, but the source is ffmpeg's `color=` lavfi filter so "
            "every frame is byte-identical. Lets you isolate whether the "
            "blink loop is content-driven. Default color is 'blue'; any "
            "ffmpeg color name/hex works (e.g. 'red', '0x00ff00')."
        ),
    )
    p.add_argument(
        "--ffmpeg-extra",
        default="",
        help="Extra raw ffmpeg args (shlex-split), e.g. '-vf scale=1280:720 -b:v 2M'",
    )
    p.add_argument(
        "--pre-z2-wait",
        type=float,
        default=0.45,
        help=(
            "Seconds to wait between q3c.z2 and the first RTP packet, giving "
            "the dash time to allocate its nav-decoder surface. Captured from "
            "nav_open_ok.pcap: the real phone waits ~450 ms. Despite the "
            "legacy name, this is a POST-z2 delay now, not a pre-z2 one."
        ),
    )
    p.add_argument(
        "--z2-repeat",
        type=int,
        default=1,
        help=(
            "How many times to send q3c.z2 back-to-back. Real phone sends it "
            "ONCE (confirmed in nav_open_ok.pcap); bump this for diagnostics."
        ),
    )
    p.add_argument(
        "--route-card-pre-z2",
        type=int,
        default=4,
        help=(
            "Number of 0x007E route-card packets to send BEFORE q3c.z2. The "
            "real phone sends 4 copies over ~1.3 s before nav-start (frames "
            "1414/1422/1446/1468 in nav_open_ok.pcap). Without this burst "
            "the dash enters nav mode but never allocates the UDP/5000 "
            "decoder surface (observed as continuous port-unreachable ICMPs)."
        ),
    )
    p.add_argument(
        "--route-card-gap",
        type=float,
        default=0.35,
        help=(
            "Seconds between successive pre-z2 route cards. Phone spacing "
            "was 0.1/0.63/0.50 s; 0.35 s averages those and is safe."
        ),
    )
    p.add_argument(
        "--route-card-rate",
        type=float,
        default=1.0,
        help=(
            "Hz to resend the 0x007E route card while streaming. Real phone "
            "sends it at ~1 Hz (nav_open_ok.pcap: t=18.824, 19.830, 20.830, "
            "21.850, 22.819 — 1 s cadence). Without this refresh the dash's "
            "'destination valid' watchdog fires after ~15–20 s and tears the "
            "decoder down. Set to 0 to disable (not recommended)."
        ),
    )
    p.add_argument(
        "--nav-info",
        dest="nav_info",
        action="store_true",
        default=True,
        help=(
            "Stream a synthetic active-navigation TLV packet at 1 Hz while "
            "projection is live (t3c-style: primary maneuver, distance, "
            "totals, decimal flag). Required to keep the nav screen from "
            "timing out on the '3 dots loading' placeholder."
        ),
    )
    p.add_argument(
        "--no-nav-info",
        dest="nav_info",
        action="store_false",
        help="Disable the synthetic nav-info 1 Hz stream.",
    )
    p.add_argument(
        "--nav-info-rate",
        type=float,
        default=1.0,
        help="Hz for the synthetic nav-info packet (stock app = 1 Hz).",
    )
    p.add_argument(
        "--nav-maneuver",
        type=lambda x: int(x, 0),
        default=NAV_MANEUVER_CONTINUE,
        help="Primary maneuver byte (0x0B = continue straight).",
    )
    p.add_argument(
        "--nav-primary-distance",
        type=int,
        default=500,
        help="Placeholder distance-to-next-turn, in the chosen unit.",
    )
    p.add_argument(
        "--nav-total-distance",
        type=int,
        default=500,
        help="Placeholder total route distance remaining, in the chosen unit.",
    )
    p.add_argument(
        "--nav-unit",
        choices=("m", "km", "ft", "mi"),
        default="m",
        help="Distance unit for both the primary and total fields.",
    )
    p.add_argument(
        "--nav-comma-decimal",
        action="store_true",
        help="Use comma (',') as decimal separator instead of period ('.').",
    )
    p.add_argument(
        "--nav-decimal-fmt",
        action="store_true",
        default=False,
        help=(
            "Enable the dash's fractional-distance formatter (q3c.i). The "
            "real phone leaves this OFF (sends 0x06 0D 0001 AA); turning it "
            "on rewrites whole-metre values with a decimal separator, which "
            "is why '500 m' would render as '5.0 km' on some firmwares."
        ),
    )
    p.add_argument("--listen-2002", action="store_true", help="(legacy, kept for compat) UDP/2002 is always bound and logged now")
    p.add_argument("--respond-2002", action="store_true", help="Send app-like K1G responses to dash 2002 commands")
    p.add_argument(
        "--ssid",
        default=None,
        help=(
            "Wi-Fi SSID of the dash AP (the network your Mac is joined to). "
            "Required for authentication; the dash validates it inside the "
            "RSA-encrypted session-key payload (oof.j). Example: 'TRIPPER_XXXX'."
        ),
    )
    p.add_argument(
        "--no-auth",
        action="store_true",
        help=(
            "Disable the RSA/AES auth handshake. Only useful for protocol "
            "experiments; the dash will stay in 'Connected to <phone>' with a "
            "blinking sun icon and will ignore all nav commands."
        ),
    )
    p.add_argument(
        "--auth-timeout",
        type=float,
        default=8.0,
        help="Seconds to wait for 07 01 01 (auth OK) before giving up and continuing anyway",
    )
    p.add_argument("--fixed-temp-c", type=int, default=1, help="Fixed temperature (°C) to advertise (sent as temp+40)")
    p.add_argument(
        "--tick-heartbeat",
        choices=("0044", "0049"),
        default="0044",
        help="1Hz heartbeat layout: 0044 matches REForeGroundService.d when no cell TLV; 0049 is the alternate branch",
    )
    p.add_argument("--tick-cell-signal", type=int, default=255, help="0..255 for 06080001xx (cellular signal placeholder on macOS)")
    p.add_argument("--tick-battery", type=int, default=80, help="0..100 battery percent for 06040001xx (app sends k()+100)")
    p.add_argument("--tick-gps-off", action="store_true", help="Advertise GPS off (06030001AA) instead of on (0603000155)")
    p.add_argument("--tick-charging", action="store_true", help="Advertise charging on (060F000155) instead of off (060F0001AA)")
    p.add_argument("--tick-music-level", type=float, default=0.6, help="0..1 music volume bucket approximation for 054C.... TLVs")
    p.add_argument("--tick-alarm-level", type=float, default=0.5, help="0..1 alarm stream volume bucket approximation for 051B.... TLVs")
    p.add_argument("--tick-nav-distance", type=int, default=0, help="Rounded navigation distance used in 052D0002 + 4-hex field")
    p.add_argument(
        "--tick-call-tail-hex",
        default="0521000132054D000132",
        help="Trailing call-state TLV chain (hex, no spaces). Default matches idle branch in REForeGroundService",
    )
    return p.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    dest = (args.broadcast, args.udp_port)
    sock = open_broadcast_socket(args.bind_ip, args.udp_port)
    loc = sock.getsockname()
    print(
        f"UDP {loc[0]}:{loc[1]} → {dest[0]}:{dest[1]} (bike {args.bike_ip})",
        file=sys.stderr,
    )

    seq = RollingSeq(args.k1g_seq_start)
    tx = K1GTx(sock, dest, seq)

    # ----- Auth state + RX listener set up BEFORE the initial burst ------
    # The dash emits its RSA pubkey (07 00 modulus + 07 03 exponent) on
    # UDP/2002 very soon after it sees our first UDP/2000 packet. If we
    # start listening only after the burst, we routinely miss it and the
    # whole handshake deadlocks.
    #
    # Port 2002 is ALWAYS bound, even when auth is disabled or the
    # 'cryptography' package is absent.  Without a bound socket the OS
    # returns ICMP port-unreachable for every dash→Mac segment on 2002
    # (Roughtime probes, 07 xx auth frames, 09 xx ack frames …). Those
    # ICMPs confuse the dash's protocol state machine and prevent it from
    # transitioning into navigation mode regardless of what we send on
    # UDP/2000. Observed in capture11: 147 consecutive ICMPs for port 2002
    # while port 5000 had zero — confirming the dash WAS receiving our RTP
    # but refusing to open the nav surface.
    auth: AuthState | None = None
    if not args.no_auth:
        if args.ssid is None:
            print(
                "WARNING: --ssid not provided. The dash will reject authentication "
                "because it embeds and validates the SSID inside the RSA payload "
                "(see oof.j). Set --ssid to the Wi-Fi network name your Mac is "
                "joined to (e.g. 'TRIPPER_XXXX'). Auth will be attempted with "
                "an empty SSID and will almost certainly fail.",
                file=sys.stderr,
            )
        if not _HAS_CRYPTO:
            print(
                "WARNING: 'cryptography' is not installed. The auth handshake "
                "will not run. Install with: pip install cryptography",
                file=sys.stderr,
            )
        else:
            auth = AuthState(ssid=args.ssid or "")

    stop = threading.Event()
    rx_stop = threading.Event()
    rx_thr: threading.Thread | None = None
    rx_sock: socket.socket | None = None
    # Always bind UDP/2002 to absorb dash→Mac packets and prevent ICMP
    # port-unreachable (see note above). The listener is started even if
    # auth is None so it can still log 09-ACK and other segments.
    rx_sock = open_listen_socket_2002(args.bind_ip, 2002)
    rx_thr = threading.Thread(
        target=listen_2002,
        args=(rx_sock, rx_stop, tx, args.respond_2002, auth),
        daemon=True,
    )
    rx_thr.start()
    print("Listening UDP/2002 (logging all bike→Mac packets)", file=sys.stderr)

    # ----- Initial burst ------------------------------------------------
    # Includes q3c.e ("request auth"). The dash will then emit 07 00 /
    # 07 03 on UDP/2002 and our RX thread will craft + send the real q3c.d.
    print(f"Initial burst (rolling seq start 0x{args.k1g_seq_start & 0xFF:02x})", file=sys.stderr)
    send_initial_burst(tx, args.hostname, args.burst_pause, args.fixed_temp_c)

    # ----- Wait for auth, then send nav kicks ---------------------------
    auth_ok = False
    if auth is not None:
        print(
            f"Waiting up to {args.auth_timeout:.1f}s for dash auth (07 01 01)…",
            file=sys.stderr,
        )
        auth_ok = auth.authenticated.wait(timeout=args.auth_timeout)
        if auth_ok:
            print("Authentication complete.", file=sys.stderr)
        else:
            print(
                "Auth timeout. Possible causes:\n"
                "  • wrong --ssid (must match the Wi-Fi the Mac is joined to)\n"
                "  • dash already paired with another phone (power-cycle the dash)\n"
                "  • Mac firewall blocking UDP/2002 inbound\n"
                "Proceeding anyway; nav commands will likely be ignored.",
                file=sys.stderr,
            )

    # ----- Post-auth nav sequence, mirroring NavigationRootFragment ----
    # Real-app order, reconstructed from nav_open_ok.pcap (`tripper8` build):
    #   1.            q3c.g   (0556 0001 55)   projection keep-alive, ~3 Hz
    #   2. t0+ 63 ms  q3c.z2  (0680 0001 0B)   ★ "start navigation" — ONCE
    #   3. t0+ 86 ms  0x007E  route card
    #   4. …          q3c.g repeats at ~3 Hz
    #   5. t0+457 ms  FIRST RTP packet on UDP/5000
    # Our earlier code had this backwards (ffmpeg → …wait… → z2×3), which is
    # why the dash's nav surface never allocated a decoder for *this* session
    # and every RTP packet fell on the floor. We now stay strictly in the
    # phone's order, and the "pre-z2 wait" is repurposed as "ffmpeg warm-up
    # delay AFTER z2" (how long before we actually start streaming pixels).
    send_nav_now = args.no_auth or auth is None or auth_ok
    video_stream: VideoStream | None = None
    proj_hb_stop = threading.Event()
    proj_hb_thr: threading.Thread | None = None
    nav_info_stop = threading.Event()
    nav_info_thr: threading.Thread | None = None
    route_card_stop = threading.Event()
    route_card_thr: threading.Thread | None = None
    projection_started = False

    if send_nav_now:
        # Step 1: (optional) nav context + empty favourite lists (from F0()).
        if args.nav_mode_kick:
            tx.send_hex(Q3C_Q_NAV_CTX)
            tx.send_hex(Q3C_R_EMPTY_LISTS)
            print("Sent q3c.q + q3c.r (nav context + empty lists)", file=sys.stderr)

        # --static-image synthesizes a lavfi `color=...` input that we
        # feed into the exact same encoder + RTP packetizer as a real
        # file. This is the cheapest way to answer "is it the content or
        # the wrapper?" — if the dash still blinks on a solid-color
        # single-frame stream, the problem is in SPS/PPS/packetization,
        # not in the video content.
        static_image_spec: str | None = None
        if args.static_image is not None:
            static_image_spec = (
                f"color=c={args.static_image}"
                f":s={DASH_VIDEO_WIDTH}x{DASH_VIDEO_HEIGHT}"
                f":r={DASH_VIDEO_FPS}"
            )
            print(
                f"--static-image active: feeding ffmpeg '{static_image_spec}' "
                "(diagnostic — bypasses --video)",
                file=sys.stderr,
            )
        want_video = bool(args.video) or static_image_spec is not None
        video_path_ok = (
            static_image_spec is not None
            or (bool(args.video) and os.path.isfile(args.video))
        )
        if bool(args.video) and static_image_spec is None and not video_path_ok:
            print(f"--video path not found: {args.video}", file=sys.stderr)

        # --- Pre-z2 route-card burst ----------------------------------
        # CRITICAL ORDERING (reconstructed from nav_open_ok.pcap frames
        # 1414..1475, times 16.221 .. 17.913):
        #   t+0.00   0x007E route card  ← destination name + full sub-TLV set
        #   t+0.10   0x007E route card  (retry #1)
        #   t+0.73   0x007E route card  (retry #2)
        #   t+1.23   0x007E route card  (retry #3)
        #   t+1.61   q3c.g  (0556 0001 55)         ← projection-on frame
        #   t+1.67   q3c.t8 placeholder (060a0002 0000)
        #   t+1.67   q3c.z2 (0680 0001 0B)         ← nav START, ONCE
        #   t+1.69   0x007E route card  (final confirmation)
        #   t+~2.12  first RTP packet on UDP/5000
        #
        # Our previous order (z2 → route card → RTP) left the dash with no
        # destination when z2 landed, so its decoder surface never
        # allocated a UDP/5000 listener — hence the 6 port-unreachable
        # ICMPs spaced 5 s apart during the whole stream in capture_bike_to_dash.
        # Sending the route card first reproduces "user picked a destination
        # BEFORE starting navigation", which is the dash's precondition for
        # opening the video pipe.
        route_pkt_template = (
            build_navigation_packet(args.route_title, 0) if args.send_nav_once else None
        )
        route_pkt_projection_on = (
            build_navigation_packet(args.route_title, 0, projection_on=True)
            if args.send_nav_once
            else None
        )
        if route_pkt_template is not None:
            reps = max(1, int(args.route_card_pre_z2))
            gap = max(0.0, float(args.route_card_gap))
            for i in range(reps):
                tx.send(route_pkt_template)
                if i < reps - 1 and gap > 0:
                    time.sleep(gap)
            print(
                f"Sent 0x007e route card x{reps} (title={args.route_title!r}) — "
                "BEFORE z2, establishes destination for the dash's nav decoder",
                file=sys.stderr,
            )

        # Step 2: q3c.g — projection-on frame, then start the heartbeat.
        if video_path_ok:
            tx.send_hex(Q3C_G_PROJ_FRAME)
            proj_hb_thr = threading.Thread(
                target=projection_heartbeat_loop,
                args=(tx, proj_hb_stop, float(DASH_VIDEO_FPS)),
                daemon=True,
            )
            proj_hb_thr.start()
            projection_started = True
            print(
                f"Sent q3c.g; projection heartbeat running at {DASH_VIDEO_FPS} Hz",
                file=sys.stderr,
            )

        # Step 3: q3c.z2 — now that the destination is set, tell the dash
        # to transition into active-navigation mode. The real phone sends
        # this exactly once (seq 0x1A in nav_open_ok.pcap).
        if args.nav_mode_kick or video_path_ok:
            reps = max(1, int(args.z2_repeat))
            for _ in range(reps):
                tx.send_hex(Q3C_Z2_START_NAV)
                if reps > 1:
                    time.sleep(0.1)
            print(f"Sent q3c.z2 x{reps} (nav start)", file=sys.stderr)

        # Step 4: one more 0x007E right after z2, mirroring the phone
        # (frame 1475, 22 ms after z2). Acts as a "destination still valid"
        # confirmation while the dash allocates the decoder surface.
        if route_pkt_template is not None:
            tx.send(route_pkt_template)
            print("Sent 0x007e route card (post-z2 confirmation)", file=sys.stderr)

        # Step 5: brief warm-up so the dash has opened its decoder surface
        # before the first FU-A fragment lands. The real phone waits ~450 ms.
        if video_path_ok and args.pre_z2_wait > 0:
            print(
                f"Waiting {args.pre_z2_wait:.1f}s after z2 before starting "
                "RTP (matches phone's post-z2 delay)…",
                file=sys.stderr,
            )
            time.sleep(args.pre_z2_wait)

        # Step 6: start RTP + 1 Hz nav-info.
        if video_path_ok:
            try:
                video_stream = start_ffmpeg_rtp(
                    video=(
                        static_image_spec
                        if static_image_spec is not None
                        else os.path.abspath(args.video)
                    ),
                    bike_ip=args.bike_ip,
                    rtp_port=args.rtp_port,
                    pkt_size=args.pkt_size,
                    extra_args=shlex.split(args.ffmpeg_extra) if args.ffmpeg_extra else [],
                    loop=args.video_loop and static_image_spec is None,
                )
            except Exception as exc:
                print(f"ffmpeg start failed: {exc}", file=sys.stderr)

            if video_stream is not None and args.nav_info:
                # Active-navigation TLVs at ~1 Hz. Without them the bubble
                # stays on the "3 dots" placeholder and the dash eventually
                # fires its watchdog on the whole nav session.
                unit_map = {
                    "m": NAV_UNIT_METERS,
                    "km": NAV_UNIT_KM,
                    "ft": NAV_UNIT_FEET,
                    "mi": NAV_UNIT_MILES,
                }
                nav_unit_code = unit_map[args.nav_unit]

                def _nav_pkt_factory() -> bytes:
                    return build_active_nav_packet(
                        primary_maneuver=args.nav_maneuver,
                        primary_distance_m=args.nav_primary_distance,
                        primary_unit=nav_unit_code,
                        total_distance_m=args.nav_total_distance,
                        total_distance_unit=nav_unit_code,
                        use_comma_decimal=args.nav_comma_decimal,
                        projection_on=True,
                        decimal_fmt_on=args.nav_decimal_fmt,
                    )

                nav_info_thr = threading.Thread(
                    target=nav_info_loop,
                    args=(
                        tx,
                        nav_info_stop,
                        _nav_pkt_factory,
                        1.0 / max(0.1, args.nav_info_rate),
                    ),
                    daemon=True,
                )
                nav_info_thr.start()
                print(
                    f"Nav-info TLV stream running at "
                    f"{args.nav_info_rate:.2f} Hz "
                    f"(maneuver=0x{args.nav_maneuver:02X}, "
                    f"{args.nav_primary_distance}{args.nav_unit} "
                    f"to next turn, {args.nav_total_distance}"
                    f"{args.nav_unit} total)",
                    file=sys.stderr,
                )

                # Route-card keep-alive. Real phone resends 0x007E at ~1 Hz
                # during streaming; without this the dash's "destination
                # still valid" watchdog tears the decoder down after ~15–20 s
                # even with port 5000 open and RTP flowing (observed in
                # capture_bike_to_dash2.pcapng: 354 RTP pkts accepted, then
                # loading-dots → timeout at ~20 s).
                if video_stream is not None and route_pkt_projection_on is not None \
                        and args.route_card_rate > 0:
                    tx.send(route_pkt_projection_on)
                    route_card_thr = threading.Thread(
                        target=route_card_keepalive_loop,
                        args=(
                            tx,
                            route_card_stop,
                            route_pkt_projection_on,
                            1.0 / max(0.1, args.route_card_rate),
                        ),
                        daemon=True,
                    )
                    route_card_thr.start()
                    print(
                        f"Route-card keep-alive running at "
                        f"{args.route_card_rate:.2f} Hz "
                        "(projection-active 0605=55)",
                        file=sys.stderr,
                    )
    else:
        if args.nav_mode_kick or args.send_nav_once or args.video or args.static_image:
            print(
                "Skipping nav/video sequence: auth not confirmed (use --no-auth "
                "to override, but the dash will ignore these commands).",
                file=sys.stderr,
            )

    t = threading.Thread(target=tick_loop, args=(tx, stop, args), daemon=True)
    t.start()
    print("1Hz metadata tick running. Ctrl+C to stop.", file=sys.stderr)
    try:
        while True:
            # If ffmpeg exits on its own (EOF without --video-loop, or an
            # error), surface that in the main loop so the user notices.
            if video_stream is not None and video_stream.proc.poll() is not None:
                rc = video_stream.proc.returncode
                print(
                    f"ffmpeg exited (rc={rc}); nav screen will stall shortly.",
                    file=sys.stderr,
                )
                # Wait for the packetizer thread to drain the last NAL(s).
                video_stream.stop.set()
                video_stream.thread.join(timeout=1.0)
                video_stream = None
            time.sleep(1.0)
    except KeyboardInterrupt:
        stop.set()
        rx_stop.set()
        proj_hb_stop.set()
        nav_info_stop.set()
        route_card_stop.set()
        t.join(timeout=1.0)
        if proj_hb_thr is not None:
            proj_hb_thr.join(timeout=1.0)
        if nav_info_thr is not None:
            nav_info_thr.join(timeout=1.0)
        if route_card_thr is not None:
            route_card_thr.join(timeout=1.0)
        if rx_thr is not None:
            rx_thr.join(timeout=1.0)
        return 0
    finally:
        proj_hb_stop.set()
        nav_info_stop.set()
        route_card_stop.set()
        if proj_hb_thr is not None:
            proj_hb_thr.join(timeout=1.0)
        if nav_info_thr is not None:
            nav_info_thr.join(timeout=1.0)
        if route_card_thr is not None:
            route_card_thr.join(timeout=1.0)
        # Mirror NavigationFragment.Y7() cleanup: tell the dash we've
        # stopped projecting so its next session doesn't start with stale
        # state. Best-effort — ignore errors since we're tearing down.
        if projection_started:
            try:
                tx.send_hex(Q3C_H_PROJ_STOP)
                tx.send_hex(Q3C_X_PROJ_OFF)
            except Exception:
                pass
        if video_stream is not None:
            video_stream.stop.set()
            try:
                video_stream.proc.terminate()
                try:
                    video_stream.proc.wait(timeout=2.0)
                except subprocess.TimeoutExpired:
                    video_stream.proc.kill()
            except Exception:
                pass
            try:
                video_stream.thread.join(timeout=2.0)
            except Exception:
                pass
        if rx_sock is not None:
            rx_sock.close()
        sock.close()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

