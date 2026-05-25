# SPDX-License-Identifier: MIT OR Apache-2.0
# Copyright (c) 2026 Ares

"""
APRS decoder — AX.25 / TNC2 frames → station positions.

APRS (Automatic Packet Reporting System) rides 1200-baud Bell-202 AFSK AX.25,
usually 144.390 MHz (NA) / 144.800 (EU). This module is the *frame layer*:
given an AX.25 frame (KISS bytes) or a TNC2-format text line
(``SRC>DEST,path:info``) it parses the callsigns + path and the APRS info field
into a position fix. The AFSK1200 + HDLC demod upstream is the SDR adapter's job
(or an external TNC / multimon-ng), exactly as mode_s.py takes 112-bit packets,
not IQ.

Position formats handled:
  - uncompressed  (``!`` ``=`` ``@`` ``/``)         DDMM.mmN / DDDMM.mmW
  - compressed    (base-91, 13 chars)
  - Mic-E         (`` ` `` / ``'`` — lat in the dest callsign, lon/speed in info)
  - object        (``;`` named object reports)

Decoded stations flow to the map via an OSINT GeoJSON layer (osint.feeds "aprs")
and to ATAK via app.core.cot (station_to_cot). Strictly receive/decode — no TX.
Self-test: ``python -m app.core.sdr...`` no — ``python -m tests.test_aprs``.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Optional


# ── frame parsing ────────────────────────────────────────────────────────────
def parse_tnc2(line: str) -> Optional[dict]:
    """``SRC>DEST,DIGI1,DIGI2*:info`` → {source, dest, digis, info, raw}."""
    line = line.strip()
    if ">" not in line or ":" not in line:
        return None
    header, info = line.split(":", 1)
    if ">" not in header:
        return None
    src, rest = header.split(">", 1)
    parts = rest.split(",")
    return {"source": src.strip(), "dest": parts[0].strip(),
            "digis": [d.strip() for d in parts[1:]], "info": info, "raw": line}


def parse_ax25(frame: bytes) -> Optional[dict]:
    """KISS-stripped AX.25 UI frame → {source, dest, digis, info, raw}."""
    f = bytes(frame)
    if f[:1] == b"\xc0":
        f = f[1:]
    if f[:1] == b"\x00":          # KISS data command byte
        f = f[1:]
    if f[-1:] == b"\xc0":
        f = f[:-1]
    f = f.replace(b"\xdb\xdc", b"\xc0").replace(b"\xdb\xdd", b"\xdb")  # KISS unescape

    addrs: list[str] = []
    i = 0
    while i + 7 <= len(f):
        cs = "".join(chr(b >> 1) for b in f[i:i + 6]).strip()
        ssid = (f[i + 6] >> 1) & 0x0F
        addrs.append(cs + (f"-{ssid}" if ssid else ""))
        last = f[i + 6] & 0x01
        i += 7
        if last:
            break
    if len(addrs) < 2 or i + 2 > len(f):
        return None
    info = f[i + 2:].decode("latin-1")    # skip control + PID; latin-1 keeps Mic-E bytes 1:1
    return {"source": addrs[1], "dest": addrs[0], "digis": addrs[2:], "info": info, "raw": f}


# ── coordinate helpers ───────────────────────────────────────────────────────
def _parse_lat(s: str) -> Optional[float]:
    # DDMM.mmH  (H = N/S); spaces = position ambiguity → 0
    if len(s) < 8:
        return None
    s = s.replace(" ", "0")
    try:
        deg = int(s[0:2]); minutes = float(s[2:7])
    except ValueError:
        return None
    v = deg + minutes / 60.0
    return -v if s[7] in "Ss" else v


def _parse_lon(s: str) -> Optional[float]:
    # DDDMM.mmH  (H = E/W)
    if len(s) < 9:
        return None
    s = s.replace(" ", "0")
    try:
        deg = int(s[0:3]); minutes = float(s[3:8])
    except ValueError:
        return None
    v = deg + minutes / 60.0
    return -v if s[8] in "Ww" else v


def _extract_ext(comment: str) -> dict:
    """Pull a leading CSE (course/speed ``ddd/ddd`` kt) + an ``/A=dddddd`` altitude."""
    out: dict = {}
    if len(comment) >= 7 and comment[3] == "/" and comment[:3].isdigit() and comment[4:7].isdigit():
        out["course_deg"] = float(comment[:3])
        out["speed_kt"] = float(comment[4:7])
        comment = comment[7:]
    idx = comment.find("/A=")
    if idx >= 0 and comment[idx + 3:idx + 9].isdigit():
        out["altitude_ft"] = float(comment[idx + 3:idx + 9])
    out["comment"] = comment.strip()
    return out


# ── position formats ─────────────────────────────────────────────────────────
def _parse_uncompressed(body: str) -> Optional[dict]:
    if len(body) < 19:
        return None
    lat = _parse_lat(body[0:8])
    lon = _parse_lon(body[9:18])
    if lat is None or lon is None:
        return None
    res = {"lat": lat, "lon": lon, "symbol": body[8] + body[18]}
    res.update(_extract_ext(body[19:]))
    return res


def _b91(s: str) -> int:
    v = 0
    for ch in s:
        v = v * 91 + (ord(ch) - 33)
    return v


def _parse_compressed(body: str) -> Optional[dict]:
    if len(body) < 13:
        return None
    lat = 90.0 - _b91(body[1:5]) / 380926.0
    lon = -180.0 + _b91(body[5:9]) / 190463.0
    res = {"lat": lat, "lon": lon, "symbol": body[0] + body[9]}
    c = ord(body[10]) - 33
    if 0 <= c <= 89:                       # cs = course/speed
        res["course_deg"] = float(c * 4)
        res["speed_kt"] = round(1.08 ** (ord(body[11]) - 33) - 1, 1)
    res["comment"] = body[13:].strip()
    return res


def _parse_position(body: str) -> Optional[dict]:
    if not body:
        return None
    return _parse_uncompressed(body) if (body[0].isdigit() or body[0] == " ") else _parse_compressed(body)


def _mice_digit(c: str) -> Optional[int]:
    if "0" <= c <= "9":
        return ord(c) - 48
    if "A" <= c <= "J":
        return ord(c) - 65
    if "P" <= c <= "Y":
        return ord(c) - 80
    if c in "KLZ":
        return 0                           # position ambiguity → 0
    return None


def _mice_upper(c: str) -> bool:
    return "P" <= c <= "Z"                  # the 'high' set: North / +100° offset / West


def _parse_mice(dest: str, info: str) -> Optional[dict]:
    d = dest[:6]
    if len(d) < 6 or len(info) < 9:
        return None
    digs = [_mice_digit(c) for c in d]
    if any(x is None for x in digs):
        return None
    lat = (digs[0] * 10 + digs[1]) + ((digs[2] * 10 + digs[3]) + (digs[4] * 10 + digs[5]) / 100.0) / 60.0
    if not _mice_upper(d[3]):
        lat = -lat                          # South
    lon_off = 100 if _mice_upper(d[4]) else 0
    west = _mice_upper(d[5])

    lon_deg = (ord(info[1]) - 28) + lon_off
    if 180 <= lon_deg <= 189:
        lon_deg -= 80
    elif 190 <= lon_deg <= 199:
        lon_deg -= 190
    lon_min = ord(info[2]) - 28
    if lon_min >= 60:
        lon_min -= 60
    lon = lon_deg + (lon_min + (ord(info[3]) - 28) / 100.0) / 60.0
    if west:
        lon = -lon

    sp = ord(info[4]) - 28
    dc = ord(info[5]) - 28
    se = ord(info[6]) - 28
    speed = sp * 10 + dc // 10
    if speed >= 800:
        speed -= 800
    course = (dc % 10) * 100 + se
    if course >= 400:
        course -= 400
    return {"lat": lat, "lon": lon, "symbol": info[8] + info[7],
            "speed_kt": float(speed), "course_deg": float(course), "comment": info[9:].strip()}


def _parse_object(info: str) -> Optional[dict]:
    # ;NAME-----*ddhhmmz<position>   (name = 9 chars)
    if len(info) < 19:
        return None
    name = info[1:10].strip()
    body = info[18:]                        # skip '*'/'_' + 7-char timestamp
    pos = _parse_position(body)
    if pos:
        pos["name"] = name
    return pos


def _parse_info(info: str, dest: str) -> Optional[dict]:
    if not info:
        return None
    t = info[0]
    if t in "!=":
        return _parse_position(info[1:])
    if t in "/@":
        return _parse_position(info[8:]) if len(info) > 8 else None     # 7-char timestamp
    if t in "`'":
        return _parse_mice(dest, info)
    if t == ";":
        return _parse_object(info)
    return None                            # status / message / telemetry → no position


# ── decoder state ────────────────────────────────────────────────────────────
@dataclass
class Station:
    callsign: str
    lat: Optional[float] = None
    lon: Optional[float] = None
    symbol: Optional[str] = None
    course_deg: Optional[float] = None
    speed_kt: Optional[float] = None
    altitude_ft: Optional[float] = None
    comment: str = ""
    dest: Optional[str] = None
    path: list = field(default_factory=list)
    is_object: bool = False
    last_update_t: float = 0.0
    n_msgs: int = 0


@dataclass
class AprsDecoderState:
    stations: dict[str, Station] = field(default_factory=dict)

    def step(self, frame, t: Optional[float] = None) -> Optional[Station]:
        """Decode one frame (TNC2 str | AX.25 bytes | pre-parsed dict). Returns
        the updated Station (keyed by source callsign, or object name)."""
        t = time.time() if t is None else t
        if isinstance(frame, dict):
            p = frame
        elif isinstance(frame, (bytes, bytearray)):
            p = parse_ax25(frame)
        else:
            p = parse_tnc2(str(frame))
        if not p:
            return None
        pos = _parse_info(p["info"], p["dest"])
        key = (pos.get("name") if pos else None) or p["source"]
        st = self.stations.get(key) or Station(callsign=key)
        st.n_msgs += 1
        st.last_update_t = t
        st.dest = p["dest"]
        st.path = p["digis"]
        if pos:
            if pos.get("name"):
                st.is_object = True
            for k in ("lat", "lon", "symbol", "course_deg", "speed_kt", "altitude_ft"):
                if pos.get(k) is not None:
                    setattr(st, k, pos[k])
            if pos.get("comment"):
                st.comment = pos["comment"]
        self.stations[key] = st
        return st

    def prune(self, max_age_s: float, now: Optional[float] = None) -> int:
        now = time.time() if now is None else now
        old = [k for k, s in self.stations.items() if now - s.last_update_t > max_age_s]
        for k in old:
            del self.stations[k]
        return len(old)


# Process-wide decoder: the API feeds it, the OSINT "aprs" layer reads it.
decoder = AprsDecoderState()


# ── outputs ──────────────────────────────────────────────────────────────────
def station_to_cot(st: Station) -> Optional[dict]:
    """APRS station → CoT-compatible dict (friendly ground). Caller serialises."""
    if st.lat is None or st.lon is None:
        return None
    return {
        "uid": f"aprs-{st.callsign}",
        "type": "a-f-G-U-C",
        "callsign": st.callsign,
        "lat": st.lat, "lon": st.lon,
        "hae_m": (st.altitude_ft * 0.3048) if st.altitude_ft is not None else 9999999.0,
        "ce_m": 100.0, "le_m": 50.0,
        "course_deg": st.course_deg,
        "speed_mps": (st.speed_kt * 0.514444) if st.speed_kt is not None else None,
        "remarks": f"APRS {st.symbol or ''} {st.comment}".strip() + f" · {st.n_msgs} msgs",
    }


def stations_geojson(dec: Optional[AprsDecoderState] = None) -> dict:
    """Decoded stations with a fix → GeoJSON FeatureCollection (for the map layer)."""
    dec = dec or decoder
    feats = []
    for st in dec.stations.values():
        if st.lat is None or st.lon is None:
            continue
        feats.append({
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [st.lon, st.lat]},
            "properties": {"name": st.callsign, "symbol": st.symbol, "comment": st.comment,
                           "course_deg": st.course_deg, "speed_kt": st.speed_kt,
                           "altitude_ft": st.altitude_ft, "object": st.is_object,
                           "n_msgs": st.n_msgs, "last_update_t": st.last_update_t, "kind": "aprs"},
        })
    return {"type": "FeatureCollection", "features": feats}
