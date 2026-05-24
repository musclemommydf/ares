# SPDX-License-Identifier: MIT OR Apache-2.0
# Copyright (c) 2026 Ares

"""
Mode-S / ADS-B decoder.

ADS-B (Mode-S extended squitter, DF17/18) at 1090 MHz transmits aircraft
position, velocity, callsign, and identification. We decode the 112-bit long
squitter to:

  - ICAO 24-bit address  → aircraft identity
  - Type code 1..4       → identification + category (callsign in 8 chars)
  - Type code 9..18      → airborne position (CPR-encoded lat/lon)
  - Type code 19         → airborne velocity (ground speed + heading + V/S)
  - Type code 5..8       → surface position
  - Type code 20..22     → barometric altitude
  - Type code 23..27, 31 — ops/squawk/status

This module is a self-contained pure-Python decoder (no pyModeS dependency,
though it follows pyModeS's reference implementation closely so output is
compatible). Demodulation upstream is the responsibility of the SDR adapter;
this layer accepts 112-bit packets as `bytes` or hex strings.

Outputs flow into the existing `app.core.cot` publisher so ADS-B contacts
appear as native CoT tracks in ATAK without external software.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Optional


_CHARSET = "#ABCDEFGHIJKLMNOPQRSTUVWXYZ#####_###############0123456789######"


def _bits(packet: bytes) -> str:
    return "".join(f"{b:08b}" for b in packet)


def hex_to_bytes(s: str) -> bytes:
    s = s.strip().replace(" ", "")
    if len(s) % 2:
        raise ValueError("hex string must have even length")
    return bytes.fromhex(s)


# ── CRC ──────────────────────────────────────────────────────────────────────
# Mode-S CRC-24 generator polynomial:
#   G(x) = x^24 + x^23 + x^22 + … + x^3 + 1   →  0x1FFF409  (25 bits).
# Reference: ICAO Annex 10 Vol IV, §3.1.2.3.3.
_CRC_GENERATOR = 0x1FFF409


def crc_check(packet: bytes) -> int:
    """Compute the 24-bit CRC residual of a Mode-S packet.

    Conventions:
      - For DF11 / DF17 / DF18 the parity field is the CRC of the prior bits,
        so the residual is 0 for a valid uncorrupted message.
      - For DF0 / 4 / 5 / 16 / 20 / 21 the parity is XORed with the 24-bit
        ICAO address, so the residual EQUALS the address (use `validate_crc`).

    Implementation follows pyModeS / ICAO Doc 9871: process the message bit
    by bit (high-bit first); whenever the active bit is 1, XOR the 25-bit
    generator aligned so its MSB lands on that bit.
    """
    msg = int.from_bytes(packet, "big")
    n_bits = len(packet) * 8
    # Iterate over the data bits (everything except the trailing 24-bit parity).
    for i in range(n_bits - 24):
        # Position of the bit we're testing — MSB-first, so bit (n_bits-1-i).
        bit_pos = n_bits - 1 - i
        if msg & (1 << bit_pos):
            # Align generator's MSB (bit 24) with bit_pos, then XOR the full 25 bits.
            msg ^= _CRC_GENERATOR << (bit_pos - 24)
    return msg & 0xFFFFFF


def validate_crc(packet: bytes) -> tuple[bool, int]:
    """Per-DF validation. Returns (valid, expected_icao_for_address_squitter).

    For DF11/17/18 we require residual == 0.
    For DF0/4/5/16/20/21 the residual is the address — we can't know the
    expected address without context, so we return (True, residual) and let
    the caller treat that residual as the ICAO it came from.
    """
    d = df(packet)
    r = crc_check(packet)
    if d in (11, 17, 18):
        return r == 0, 0
    return True, r


# ── field extraction ────────────────────────────────────────────────────────
def df(packet: bytes) -> int:   return (packet[0] >> 3) & 0x1F
def ca(packet: bytes) -> int:   return packet[0] & 0x07
def icao(packet: bytes) -> str: return packet[1:4].hex().upper()
def tc(packet: bytes) -> int:   return (packet[4] >> 3) & 0x1F


def callsign(packet: bytes) -> Optional[str]:
    """For DF17/18, TC 1..4 — 8-character callsign in 6-bit Brady code."""
    if tc(packet) not in (1, 2, 3, 4):
        return None
    bits = _bits(packet[5:11])     # 48 bits ME[9:56]
    out = ""
    for i in range(8):
        c = int(bits[i * 6 : i * 6 + 6], 2)
        if c < len(_CHARSET):
            out += _CHARSET[c]
    return out.replace("#", "").strip()


def velocity(packet: bytes) -> Optional[dict]:
    """For DF17/18, TC 19 — airborne velocity (ground or air)."""
    if tc(packet) != 19:
        return None
    me = _bits(packet[4:11])
    subtype = int(me[5:8], 2)
    if subtype not in (1, 2):
        return None
    s_ew = int(me[13], 2); v_ew = int(me[14:24], 2) - 1
    s_ns = int(me[24], 2); v_ns = int(me[25:35], 2) - 1
    vx = -v_ew if s_ew else v_ew
    vy = -v_ns if s_ns else v_ns
    speed_kt = math.sqrt(vx * vx + vy * vy)
    heading = (math.degrees(math.atan2(vx, vy)) + 360) % 360
    s_vr = int(me[44], 2); raw_vr = int(me[45:54], 2) - 1
    vert_fps = (64 * raw_vr) * (-1 if s_vr else 1)
    return {"speed_kt": speed_kt, "heading_deg": heading, "vertical_fpm": vert_fps * 60 / 1.0}


# ── CPR decode (airborne position) ───────────────────────────────────────────
def _cpr_nl(lat: float) -> int:
    if lat == 0: return 59
    if abs(lat) == 87: return 2
    if abs(lat) > 87: return 1
    nz = 15
    a = 1 - math.cos(math.pi / (2 * nz))
    b = math.cos(math.pi * abs(lat) / 180) ** 2
    return int(math.floor((2 * math.pi) / math.acos(1 - a / b)))


def cpr_global_position(even: dict, odd: dict) -> Optional[tuple[float, float]]:
    """Globally unambiguous CPR decode — needs one even + one odd frame within ~10 s."""
    if not even or not odd:
        return None
    lat_e, lon_e, t_e = even["lat_cpr"], even["lon_cpr"], even["t"]
    lat_o, lon_o, t_o = odd["lat_cpr"],  odd["lon_cpr"],  odd["t"]
    air_d_lat_e = 360.0 / 60
    air_d_lat_o = 360.0 / 59
    j = math.floor(((59 * lat_e) - (60 * lat_o)) / 131072 + 0.5)
    lat_e_dec = air_d_lat_e * ((j % 60) + lat_e / 131072.0)
    lat_o_dec = air_d_lat_o * ((j % 59) + lat_o / 131072.0)
    if lat_e_dec >= 270: lat_e_dec -= 360
    if lat_o_dec >= 270: lat_o_dec -= 360
    lat = lat_e_dec if t_e > t_o else lat_o_dec
    nl = _cpr_nl(lat)
    ni = max(nl - (0 if t_e > t_o else 1), 1)
    m = math.floor(((lon_e * (nl - 1)) - (lon_o * nl)) / 131072 + 0.5)
    if t_e > t_o:
        lon = (360.0 / ni) * ((m % ni) + lon_e / 131072.0)
    else:
        lon = (360.0 / ni) * ((m % ni) + lon_o / 131072.0)
    if lon > 180: lon -= 360
    return lat, lon


def airborne_position(packet: bytes, t: float) -> Optional[dict]:
    """Returns { tc, alt_ft, oe ('even' | 'odd'), lat_cpr, lon_cpr, t }."""
    type_code = tc(packet)
    if type_code < 9 or type_code > 18:
        return None
    me = _bits(packet[4:11])
    alt_bits = me[8:20]
    alt_bits = alt_bits[:7] + alt_bits[8:]                        # remove Q bit at index 7
    n = int(alt_bits, 2)
    alt_ft = n * 25 - 1000 if me[15] == "1" else None
    oe = "even" if me[21] == "0" else "odd"
    lat_cpr = int(me[22:39], 2)
    lon_cpr = int(me[39:56], 2)
    return {"tc": type_code, "alt_ft": alt_ft, "oe": oe,
            "lat_cpr": lat_cpr, "lon_cpr": lon_cpr, "t": t}


# ── decoder state-machine ───────────────────────────────────────────────────
@dataclass
class Aircraft:
    icao: str
    callsign: Optional[str] = None
    lat: Optional[float] = None
    lon: Optional[float] = None
    alt_ft: Optional[int] = None
    speed_kt: Optional[float] = None
    heading_deg: Optional[float] = None
    vertical_fpm: Optional[float] = None
    last_even: Optional[dict] = None
    last_odd: Optional[dict] = None
    last_update_t: float = 0.0
    n_msgs: int = 0


@dataclass
class Mode_SDecoderState:
    aircraft: dict[str, Aircraft] = field(default_factory=dict)

    def step(self, packet: bytes, t: float) -> Optional[Aircraft]:
        """Decode one Mode-S 112-bit message. Returns the updated aircraft."""
        if len(packet) != 14:
            return None
        d = df(packet)
        # Verify CRC for DF17 (extended squitter). DF18 / DF19 also accepted; DF17 has CRC == 0.
        if d == 17 and crc_check(packet) != 0:
            return None
        if d not in (17, 18):
            return None
        addr = icao(packet)
        ac = self.aircraft.get(addr) or Aircraft(icao=addr)
        ac.n_msgs += 1; ac.last_update_t = t
        cs = callsign(packet)
        if cs: ac.callsign = cs
        v = velocity(packet)
        if v:
            ac.speed_kt = v["speed_kt"]; ac.heading_deg = v["heading_deg"]; ac.vertical_fpm = v["vertical_fpm"]
        pos = airborne_position(packet, t)
        if pos:
            ac.alt_ft = pos["alt_ft"] or ac.alt_ft
            (ac.last_even if pos["oe"] == "even" else ac.__setattr__("last_odd", pos)) if False else None
            if pos["oe"] == "even": ac.last_even = pos
            else: ac.last_odd = pos
            if ac.last_even and ac.last_odd and abs(ac.last_even["t"] - ac.last_odd["t"]) < 10:
                ll = cpr_global_position(ac.last_even, ac.last_odd)
                if ll:
                    ac.lat, ac.lon = ll
        self.aircraft[addr] = ac
        return ac


def aircraft_to_cot(ac: Aircraft) -> Optional[dict]:
    """ADS-B aircraft → CoT-compatible dict. Caller serialises to XML.
    The MITRE-recommended CoT type for non-cooperative air is `a-n-A-C-F`;
    we use `a-n-A` (neutral, air) so it works in vanilla ATAK without a
    civilian air symbol pack."""
    if ac.lat is None or ac.lon is None:
        return None
    return {
        "uid": f"adsb-{ac.icao}",
        "type": "a-n-A",
        "callsign": ac.callsign or ac.icao,
        "lat": ac.lat, "lon": ac.lon,
        "hae_m": (ac.alt_ft * 0.3048) if ac.alt_ft is not None else 9999999.0,
        "ce_m": 200.0, "le_m": 50.0,
        "course_deg": ac.heading_deg,
        "speed_mps": (ac.speed_kt * 0.514444) if ac.speed_kt is not None else None,
        "remarks": f"ADS-B ICAO {ac.icao} · {ac.n_msgs} msgs",
    }
