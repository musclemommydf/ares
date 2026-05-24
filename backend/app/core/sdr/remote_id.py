# SPDX-License-Identifier: MIT OR Apache-2.0
# Copyright (c) 2026 Ares

"""
sdr/remote_id.py — UAS telemetry-beacon demux: ASTM F3411 (FAA Remote ID /
ASD-STAN prEN 4709-002 "Open Drone ID") and DJI DroneID.

This is the **unencrypted** broadcast every modern drone emits — drone serial,
position/altitude/speed, operator/home-point location — over WiFi (NaN action
frames / Beacon vendor IEs) or Bluetooth 4/5 advertising (and, for DJI's pre-RID
format, an OFDM burst on a WiFi channel). Parsing it is the open, legitimate way
to **detect, identify and geolocate a UAS and its pilot**; it is a separate
channel from the (often encrypted) video downlink and there is no decryption here.

What runs in pure Python, with no external tooling:
  * a full **ASTM F3411 message decoder** (Basic ID, Location/Vector, Self-ID,
    System, Operator ID, and Message Pack) — and an encoder, so a captured 25-byte
    message / pack becomes ``{serial, ua_type, lat, lon, alt, speed, track, vspeed,
    operator_lat, operator_lon, area_radius_m, operator_id, …}``;
  * a best-effort **DJI DroneID** plaintext parser (the documented v1 layout;
    v2's obfuscated tail is identified and left to the published key/decoder);
  * GeoJSON (drone point · operator/pilot point · operating-area circle) and CoT.

What's handed off (PATH-detected, like the rest of Ares): the RF side — recovering
the *bytes* from the air needs a WiFi/BT sniffer (the F3411 path) or an OFDM
receiver for the DJI burst (a Remote-ID decoder, the ``dji_droneid`` tooling, or a
WiFi monitor-mode capture). Without one, ``decode_rid`` reports what to install and
a synthetic beacon drives the map + CoT path offline; ``parse_f3411`` /
``parse_dji_droneid`` work on any bytes you already have.
"""
from __future__ import annotations

import math
import os
import shutil
import struct
import time
import uuid
from typing import Optional

# ── ASTM F3411 (Open Drone ID) ───────────────────────────────────────────────
RID_PROTOCOL_VERSION = 0x02  # F3411-22a / prEN 4709-002

MSG_BASIC_ID, MSG_LOCATION, MSG_AUTH, MSG_SELF_ID, MSG_SYSTEM, MSG_OPERATOR_ID, MSG_PACK = 0x0, 0x1, 0x2, 0x3, 0x4, 0x5, 0xF
_MSG_NAME = {0x0: "basic_id", 0x1: "location_vector", 0x2: "authentication", 0x3: "self_id",
             0x4: "system", 0x5: "operator_id", 0xF: "message_pack"}
_ID_TYPE = {0: "none", 1: "serial_number", 2: "caa_registration", 3: "utm_uuid", 4: "specific_session_id"}
_UA_TYPE = {0: "none", 1: "aeroplane", 2: "helicopter_multirotor", 3: "gyroplane", 4: "hybrid_lift",
            5: "ornithopter", 6: "glider", 7: "kite", 8: "free_balloon", 9: "captive_balloon",
            10: "airship", 11: "free_fall_parachute", 12: "rocket", 13: "tethered_powered_aircraft",
            14: "ground_obstacle", 15: "other"}
_OP_STATUS = {0: "undeclared", 1: "ground", 2: "airborne", 3: "emergency", 4: "remote_id_system_failure"}
_OP_LOC_TYPE = {0: "takeoff", 1: "live_gnss", 2: "fixed"}


def _i32le(b: bytes) -> int:
    return struct.unpack("<i", b[:4].ljust(4, b"\x00"))[0]


def _u16le(b: bytes) -> int:
    return struct.unpack("<H", b[:2].ljust(2, b"\x00"))[0]


def _alt(raw_u16: int) -> Optional[float]:
    return None if raw_u16 == 0 else raw_u16 * 0.5 - 1000.0


def _ascii(b: bytes) -> str:
    return b.split(b"\x00", 1)[0].decode("ascii", "replace").strip()


def _parse_basic_id(body: bytes) -> dict:
    id_type, ua_type = (body[0] >> 4) & 0x0F, body[0] & 0x0F
    return {"id_type": _ID_TYPE.get(id_type, str(id_type)), "id_type_code": id_type,
            "ua_type": _UA_TYPE.get(ua_type, str(ua_type)), "ua_type_code": ua_type,
            "uas_id": _ascii(body[1:21])}


def _parse_location(body: bytes) -> dict:
    status = (body[0] >> 4) & 0x0F
    flags = body[0] & 0x0F
    ew_seg = bool(flags & 0x02)
    speed_mult = 0.75 if (flags & 0x08) else 0.25
    track = body[1] + (180 if ew_seg else 0)
    spd_raw = body[2]
    speed = None if spd_raw == 255 else (spd_raw * 0.25 if speed_mult == 0.25 else spd_raw * 0.75 + 255 * 0.25)
    vspeed = struct.unpack("<b", body[3:4])[0] * 0.5
    lat = _i32le(body[4:8]) * 1e-7
    lon = _i32le(body[8:12]) * 1e-7
    return {"operational_status": _OP_STATUS.get(status, str(status)), "height_type": "above_takeoff" if (flags & 0x04) else "agl",
            "track_deg": track % 360, "speed_m_s": (round(speed, 2) if speed is not None else None),
            "vertical_speed_m_s": round(vspeed, 2),
            "latitude_deg": round(lat, 7) if lat else None, "longitude_deg": round(lon, 7) if lon else None,
            "pressure_altitude_m": _alt(_u16le(body[12:14])), "geodetic_altitude_m": _alt(_u16le(body[14:16])),
            "height_m": _alt(_u16le(body[16:18])),
            "timestamp_tenths_of_hour": _u16le(body[20:22])}


def _parse_system(body: bytes) -> dict:
    f = body[0]
    op_loc_type = f & 0x03
    op_lat = _i32le(body[1:5]) * 1e-7
    op_lon = _i32le(body[5:9]) * 1e-7
    area_count = _u16le(body[9:11])
    area_radius_m = body[11] * 10
    cat_class = body[16]
    return {"operator_location_type": _OP_LOC_TYPE.get(op_loc_type, str(op_loc_type)),
            "operator_latitude_deg": round(op_lat, 7) if op_lat else None,
            "operator_longitude_deg": round(op_lon, 7) if op_lon else None,
            "area_count": area_count, "area_radius_m": area_radius_m,
            "area_ceiling_m": _alt(_u16le(body[12:14])), "area_floor_m": _alt(_u16le(body[14:16])),
            "ua_category_eu": (cat_class >> 4) & 0x0F, "ua_class_eu": cat_class & 0x0F,
            "operator_altitude_geo_m": _alt(_u16le(body[17:19]))}


def _parse_self_id(body: bytes) -> dict:
    return {"description_type": body[0], "self_id_text": _ascii(body[1:24])}


def _parse_operator_id(body: bytes) -> dict:
    return {"operator_id_type": body[0], "operator_id": _ascii(body[1:21])}


_BODY_PARSERS = {MSG_BASIC_ID: _parse_basic_id, MSG_LOCATION: _parse_location, MSG_SYSTEM: _parse_system,
                 MSG_SELF_ID: _parse_self_id, MSG_OPERATOR_ID: _parse_operator_id}


def _parse_one(msg: bytes) -> dict:
    if len(msg) < 25:
        return {"error": "short message"}
    mtype, version = (msg[0] >> 4) & 0x0F, msg[0] & 0x0F
    body = msg[1:25]
    out = {"message_type": _MSG_NAME.get(mtype, str(mtype)), "message_type_code": mtype, "protocol_version": version}
    p = _BODY_PARSERS.get(mtype)
    if p:
        try:
            out.update(p(body))
        except Exception as e:  # pragma: no cover
            out["parse_error"] = str(e)
    return out


def parse_f3411(data: bytes) -> dict:
    """Parse one F3411 message or a Message Pack. Returns the individual messages plus
    a flattened ``summary`` (serial, drone lat/lon/alt/speed, operator lat/lon, …)."""
    data = bytes(data)
    if len(data) < 4:
        return {"error": "too short"}
    mtype = (data[0] >> 4) & 0x0F
    msgs: list[dict]
    if mtype == MSG_PACK:
        msg_size = data[1] or 25
        count = data[2]
        msgs = [_parse_one(data[3 + i * msg_size: 3 + (i + 1) * msg_size]) for i in range(count)]
    elif len(data) >= 3 + 25 and data[1] == 25 and data[2] <= 12 and (data[0] >> 4) == MSG_PACK:
        count = data[2]
        msgs = [_parse_one(data[3 + i * 25: 3 + (i + 1) * 25]) for i in range(count)]
    else:
        msgs = [_parse_one(data[i:i + 25]) for i in range(0, len(data) - 24, 25)] or [_parse_one(data)]
    # flatten the interesting fields
    s: dict = {"format": "astm_f3411", "n_messages": len(msgs)}
    for m in msgs:
        if m.get("message_type") == "basic_id" and m.get("uas_id"):
            s.setdefault("serial", m["uas_id"]); s["id_type"] = m.get("id_type"); s["ua_type"] = m.get("ua_type")
        if m.get("message_type") == "location_vector":
            for k_src, k_dst in (("latitude_deg", "drone_lat"), ("longitude_deg", "drone_lon"),
                                 ("geodetic_altitude_m", "drone_alt_m"), ("speed_m_s", "drone_speed_m_s"),
                                 ("track_deg", "drone_track_deg"), ("vertical_speed_m_s", "drone_vspeed_m_s"),
                                 ("operational_status", "operational_status"), ("height_m", "drone_height_m")):
                if m.get(k_src) is not None:
                    s[k_dst] = m[k_src]
        if m.get("message_type") == "system":
            for k_src, k_dst in (("operator_latitude_deg", "operator_lat"), ("operator_longitude_deg", "operator_lon"),
                                 ("operator_altitude_geo_m", "operator_alt_m"), ("area_radius_m", "area_radius_m"),
                                 ("operator_location_type", "operator_location_type")):
                if m.get(k_src) is not None:
                    s[k_dst] = m[k_src]
        if m.get("message_type") == "operator_id" and m.get("operator_id"):
            s["operator_id"] = m["operator_id"]
        if m.get("message_type") == "self_id" and m.get("self_id_text"):
            s["self_id"] = m["self_id_text"]
    return {"messages": msgs, "summary": s}


# ── F3411 encoder (for tests + the synthetic offline beacon) ─────────────────
def _enc_msg(mtype: int, body: bytes) -> bytes:
    return bytes([((mtype & 0x0F) << 4) | (RID_PROTOCOL_VERSION & 0x0F)]) + body[:24].ljust(24, b"\x00")


def _enc_alt(m: Optional[float]) -> int:
    return 0 if m is None else max(0, min(0xFFFF, int(round((m + 1000.0) / 0.5))))


def encode_f3411_pack(*, serial: str, ua_type: int = 2, lat: float, lon: float, alt_m: float = 100.0,
                      speed_m_s: float = 12.0, track_deg: float = 90.0, vspeed_m_s: float = 0.0,
                      operator_lat: float, operator_lon: float, operator_alt_m: float = 0.0,
                      area_radius_m: int = 0, operator_id: str = "", operational_status: int = 2) -> bytes:
    """Build a Message Pack: Basic ID + Location/Vector + System + (optional) Operator ID."""
    basic = bytes([(1 << 4) | (ua_type & 0x0F)]) + serial.encode("ascii", "replace")[:20].ljust(20, b"\x00") + b"\x00\x00\x00"
    ew = 1 if track_deg >= 180 else 0
    loc = bytes([((operational_status & 0x0F) << 4) | (ew << 1)]) \
        + bytes([int(track_deg) % 180]) \
        + bytes([max(0, min(254, int(round(speed_m_s / 0.25))))]) \
        + struct.pack("<b", max(-127, min(127, int(round(vspeed_m_s / 0.5))))) \
        + struct.pack("<i", int(round(lat * 1e7))) + struct.pack("<i", int(round(lon * 1e7))) \
        + struct.pack("<H", _enc_alt(alt_m)) + struct.pack("<H", _enc_alt(alt_m)) + struct.pack("<H", _enc_alt(alt_m)) \
        + b"\x00\x00" + struct.pack("<H", 0) + b"\x00\x00"
    sysm = bytes([0x01]) + struct.pack("<i", int(round(operator_lat * 1e7))) + struct.pack("<i", int(round(operator_lon * 1e7))) \
        + struct.pack("<H", 1) + bytes([max(0, min(255, area_radius_m // 10))]) \
        + struct.pack("<H", _enc_alt(None)) + struct.pack("<H", _enc_alt(None)) + b"\x00" + struct.pack("<H", _enc_alt(operator_alt_m)) + b"\x00\x00\x00\x00\x00"
    msgs = [_enc_msg(MSG_BASIC_ID, basic), _enc_msg(MSG_LOCATION, loc), _enc_msg(MSG_SYSTEM, sysm)]
    if operator_id:
        msgs.append(_enc_msg(MSG_OPERATOR_ID, bytes([0x00]) + operator_id.encode("ascii", "replace")[:20].ljust(20, b"\x00") + b"\x00\x00\x00"))
    return bytes([(MSG_PACK << 4) | RID_PROTOCOL_VERSION, 25, len(msgs)]) + b"".join(msgs)


# ── DJI DroneID (best-effort plaintext parser) ───────────────────────────────
# DroneID v2's payload tail is obfuscated (AES-CTR with a fixed, *published* key + a
# packet-derived nonce — reverse-engineered and shipped in the open `dji_droneid`
# tooling; a fixed published descramble, not a comms decrypt). Ares does not vendor the
# key; a deployment registers a descrambler — `set_droneid_v2_descrambler(fn)` where
# `fn(payload: bytes) -> bytes` returns the de-obfuscated payload — and `parse_dji_droneid`
# applies it before parsing. Without one, the plaintext header is parsed and the tail left as-is.
_DRONEID_V2_DESCRAMBLER = None


def set_droneid_v2_descrambler(fn):
    """Register (or clear with None) a `fn(payload: bytes) -> bytes` that de-obfuscates a
    DJI DroneID v2 payload (e.g. wrapping the open `dji_droneid` AES-CTR descramble + key)."""
    global _DRONEID_V2_DESCRAMBLER
    _DRONEID_V2_DESCRAMBLER = fn


def aes_ctr_descramble(payload: bytes, key: bytes, nonce: bytes, offset: int = 0) -> bytes:
    """AES-CTR de-obfuscate ``payload[offset:]`` with ``key`` (16/24/32 B) + 16-B
    ``nonce``. The mechanism DJI DroneID v2 uses (a fixed published key + a
    packet-derived nonce — a known descramble, not a comms decrypt)."""
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    head, body = bytes(payload[:offset]), bytes(payload[offset:])
    dec = Cipher(algorithms.AES(bytes(key)), modes.CTR(bytes(nonce))).decryptor()
    return head + dec.update(body) + dec.finalize()


def _default_v2_descrambler(payload: bytes) -> bytes:
    """Default DJI DroneID v2 descrambler, enabled only when a key is configured via
    env (the published key/region from security research isn't shipped):
      ARES_DRONEID_V2_KEY    hex AES key (16/24/32 bytes)
      ARES_DRONEID_V2_IV     hex 16-byte nonce (default: zero)
      ARES_DRONEID_V2_OFFSET plaintext-header length kept in clear (default 0)
    Returns the payload unchanged if no key is set."""
    key = os.environ.get("ARES_DRONEID_V2_KEY")
    if not key:
        return payload
    nonce = bytes.fromhex(os.environ.get("ARES_DRONEID_V2_IV", "00" * 16))
    offset = int(os.environ.get("ARES_DRONEID_V2_OFFSET", "0"))
    return aes_ctr_descramble(payload, bytes.fromhex(key), nonce, offset)


# Auto-register the AES-CTR descrambler when a key is configured (safe no-op otherwise).
if os.environ.get("ARES_DRONEID_V2_KEY"):
    _DRONEID_V2_DESCRAMBLER = _default_v2_descrambler

DJI_OUI = bytes.fromhex("60601f")  # the DJI vendor OUI seen in the WiFi vendor IE / beacon


def parse_dji_droneid(data: bytes) -> dict:
    """Best-effort parse of a (de-framed) DJI DroneID payload. Handles the documented
    v1 plaintext layout; for v2, a registered descrambler (see ``set_droneid_v2_descrambler``)
    de-obfuscates the tail first, else only the plaintext header is parsed. Real frames vary
    by firmware — treat unset fields as 'not in this layout'."""
    data = bytes(data)
    out: dict = {"format": "dji_droneid", "len": len(data)}
    if len(data) < 24:
        return {**out, "error": "too short"}
    if (data[0] & 0x0F) >= 2 and _DRONEID_V2_DESCRAMBLER is not None:
        try:
            d2 = _DRONEID_V2_DESCRAMBLER(data)
            if isinstance(d2, (bytes, bytearray)) and len(d2) >= 24:
                data = bytes(d2)
                out["v2_descrambled"] = True
        except Exception as e:  # pragma: no cover
            out["v2_descramble_error"] = str(e)
    # a common v1 layout: [0:2] state flags, [2:18] serial (ASCII), [18:22] lon int32 (deg*1e7),
    # [22:26] lat int32 (deg*1e7), [26:28] alt (int16, m), [28:30] height (int16, dm), then home/pilot.
    ver = data[0] & 0x0F
    out["version_guess"] = ver
    try:
        out["serial"] = _ascii(data[2:18])
    except Exception:
        pass
    try:
        lon = struct.unpack("<i", data[18:22])[0] * 1e-7
        lat = struct.unpack("<i", data[22:26])[0] * 1e-7
        if -90 <= lat <= 90 and -180 <= lon <= 180 and (lat or lon):
            out["drone_lat"], out["drone_lon"] = round(lat, 7), round(lon, 7)
        out["drone_alt_m"] = struct.unpack("<h", data[26:28])[0] if len(data) >= 28 else None
        out["drone_height_m"] = (struct.unpack("<h", data[28:30])[0] / 10.0) if len(data) >= 30 else None
        if len(data) >= 50:
            v_n, v_e, v_u = (struct.unpack("<h", data[30:32])[0] / 100.0, struct.unpack("<h", data[32:34])[0] / 100.0,
                             struct.unpack("<h", data[34:36])[0] / 100.0)
            out["drone_speed_m_s"] = round(math.hypot(v_n, v_e), 2)
            out["drone_vspeed_m_s"] = round(v_u, 2)
            hlon = struct.unpack("<i", data[36:40])[0] * 1e-7
            hlat = struct.unpack("<i", data[40:44])[0] * 1e-7
            plon = struct.unpack("<i", data[44:48])[0] * 1e-7
            plat = struct.unpack("<i", data[48:52])[0] * 1e-7 if len(data) >= 52 else None
            if -90 <= hlat <= 90 and -180 <= hlon <= 180 and (hlat or hlon):
                out["home_lat"], out["home_lon"] = round(hlat, 7), round(hlon, 7)
            if plat is not None and -90 <= plat <= 90 and -180 <= plon <= 180 and (plat or plon):
                out["operator_lat"], out["operator_lon"] = round(plat, 7), round(plon, 7)
    except Exception as e:  # pragma: no cover
        out["parse_error"] = str(e)
    if ver >= 2 and not out.get("v2_descrambled"):
        out["note"] = ("DroneID v2: obfuscated tail (AES-CTR). The descrambler is implemented "
                       "(aes_ctr_descramble / auto-registered when keyed) — set ARES_DRONEID_V2_KEY "
                       "(+ _IV / _OFFSET) with the published research key to decode the full v2 fields; "
                       "Ares parsed the plaintext header only. A fixed published descramble, not a comms decrypt.")
    return out


# ── GeoJSON + CoT ────────────────────────────────────────────────────────────
def _circle(lat, lon, r_m, n=24):
    R = 6378137.0
    ring = []
    for k in range(n + 1):
        b = math.radians(k * 360.0 / n)
        dr = r_m / R
        la2 = math.asin(math.sin(math.radians(lat)) * math.cos(dr) + math.cos(math.radians(lat)) * math.sin(dr) * math.cos(b))
        lo2 = math.radians(lon) + math.atan2(math.sin(b) * math.sin(dr) * math.cos(math.radians(lat)), math.cos(dr) - math.sin(math.radians(lat)) * math.sin(la2))
        ring.append([(math.degrees(lo2) + 540) % 360 - 180, math.degrees(la2)])
    return ring


def rid_to_geojson(parsed: dict) -> dict:
    """A FeatureCollection: a drone point, an operator/pilot point, a home-point, and an
    operating-area circle — tagged with ``rid_glx`` ∈ {drone, operator, home, area}."""
    s = parsed.get("summary", parsed)
    feats: list[dict] = []
    serial = s.get("serial") or s.get("operator_id") or "UAS"
    if s.get("drone_lat") is not None and s.get("drone_lon") is not None:
        feats.append({"type": "Feature", "geometry": {"type": "Point", "coordinates": [s["drone_lon"], s["drone_lat"]]},
                      "properties": {"rid_glx": "drone", "serial": serial, "alt_m": s.get("drone_alt_m"),
                                     "speed_m_s": s.get("drone_speed_m_s"), "track_deg": s.get("drone_track_deg"),
                                     "status": s.get("operational_status"), "ua_type": s.get("ua_type"), "color": "#ef4444"}})
    if s.get("operator_lat") is not None and s.get("operator_lon") is not None:
        feats.append({"type": "Feature", "geometry": {"type": "Point", "coordinates": [s["operator_lon"], s["operator_lat"]]},
                      "properties": {"rid_glx": "operator", "serial": serial, "operator_id": s.get("operator_id"),
                                     "operator_location_type": s.get("operator_location_type"), "color": "#a855f7"}})
        if s.get("area_radius_m"):
            feats.append({"type": "Feature", "geometry": {"type": "Polygon", "coordinates": [_circle(s["operator_lat"], s["operator_lon"], s["area_radius_m"])]},
                          "properties": {"rid_glx": "area", "serial": serial, "radius_m": s["area_radius_m"], "color": "#a855f7"}})
    if s.get("home_lat") is not None and s.get("home_lon") is not None:
        feats.append({"type": "Feature", "geometry": {"type": "Point", "coordinates": [s["home_lon"], s["home_lat"]]},
                      "properties": {"rid_glx": "home", "serial": serial, "color": "#f59e0b"}})
    return {"type": "FeatureCollection", "features": feats}


def rid_to_cot(parsed: dict) -> dict:
    """Best-effort: drop the drone (UAS) and the operator (ground) as CoT, tagged with the serial."""
    try:
        from app.core import cot as _cot
    except Exception:
        return {"sent": False, "reason": "cot module unavailable"}
    s = parsed.get("summary", parsed)
    serial = s.get("serial") or s.get("operator_id") or "UAS"
    sent = 0
    if hasattr(_cot, "_event") and hasattr(_cot, "_send_all"):
        if s.get("drone_lat") is not None and s.get("drone_lon") is not None:
            try:
                _cot._send_all(_cot._event(f"ares-rid-uav-{serial}", "a-u-A-M-F", float(s["drone_lat"]), float(s["drone_lon"]),
                                           remarks=f"Remote ID: {serial} · {s.get('drone_alt_m', '?')} m · {s.get('operational_status', '?')}"))
                sent += 1
            except Exception:
                pass
        if s.get("operator_lat") is not None and s.get("operator_lon") is not None:
            try:
                _cot._send_all(_cot._event(f"ares-rid-op-{serial}", "a-u-G", float(s["operator_lat"]), float(s["operator_lon"]),
                                           remarks=f"UAS operator (Remote ID {serial}){' · ' + s['operator_id'] if s.get('operator_id') else ''}"))
                sent += 1
            except Exception:
                pass
    return {"sent": sent, "serial": serial}


# ── decode session glue (real OTA sniff via rid_sniffer; synthetic offline) ──
_RID_SESSIONS: dict[str, dict] = {}
_RID_SNIFFERS: dict[str, "object"] = {}     # sid -> RemoteIdSniffer
_RID_TOOLS = ("dji_droneid", "rid-decoder", "horus_droneid", "tshark", "tcpdump")


def available_tools() -> dict:
    from . import rid_sniffer
    tools = {t: bool(shutil.which(t)) for t in _RID_TOOLS}
    tools["bleak"] = rid_sniffer.bleak_available()        # in-process BLE OpenDroneID capture
    tools["scapy"] = rid_sniffer.scapy_available()        # WiFi monitor-mode capture
    return tools


def _synthetic_beacon(t: float, base_lat: float = 36.114, base_lon: float = -115.173) -> dict:
    ang = (t * 8.0) % 360.0
    dr = 1500.0 / 6378137.0
    dla = math.degrees(dr * math.cos(math.radians(ang)))
    dlo = math.degrees(dr * math.sin(math.radians(ang)) / math.cos(math.radians(base_lat)))
    pkt = encode_f3411_pack(serial="1581F" + uuid.uuid4().hex[:11].upper(), ua_type=2,
                            lat=base_lat + dla, lon=base_lon + dlo, alt_m=120.0 + 30 * math.sin(t / 9),
                            speed_m_s=14.0, track_deg=(ang + 90) % 360, vspeed_m_s=0.0,
                            operator_lat=base_lat, operator_lon=base_lon, operator_alt_m=2.0,
                            area_radius_m=300, operator_id="OP-DEMO-01", operational_status=2)
    p = parse_f3411(pkt)
    p["_packet_hex"] = pkt.hex()
    p["summary"]["_synthetic"] = True
    return p


def decode_rid(device: Optional[dict], *, frequency_hz: float = 2.437e9, kind: str = "f3411",
               label: str = "", wifi_iface: str = "wlan0mon", ble_adapter: str = "hci0") -> dict:
    """Start a Remote-ID / DroneID receive session. Captures real OpenDroneID/DroneID
    broadcasts over BLE (bleak) and/or WiFi monitor-mode (scapy) via ``rid_sniffer``;
    when neither is importable a synthetic beacon drives the map + CoT path so the
    chain is exercised offline. ``kind`` ∈ {f3411, dji}."""
    from . import rid_sniffer
    sid = uuid.uuid4().hex[:12]
    # DJI DroneID: WiFi vendor element + the OFDM RF burst (SDR). F3411: BLE + WiFi.
    transport = "auto"
    rf_device = device if (kind == "dji" and isinstance(device, dict) and device.get("id")) else None
    sniffer = rid_sniffer.RemoteIdSniffer(sid, transport=transport,
                                          wifi_iface=wifi_iface, ble_adapter=ble_adapter,
                                          rf_device=rf_device, rf_freq_hz=float(frequency_hz))
    active = sniffer.start()
    base = ((device or {}).get("lat", 36.114), (device or {}).get("lon", -115.173)) \
        if isinstance(device, dict) and device.get("lat") else (36.114, -115.173)
    sess = {"id": sid, "kind": kind, "device_id": (device or {}).get("id"), "frequency_hz": float(frequency_hz),
            "label": label or ("DJI DroneID" if kind == "dji" else "ASTM F3411 Remote ID"),
            "started_ts": time.time(), "metadata_url": f"/api/v1/uas/rid/sessions/{sid}/metadata",
            "_base": base}
    if active:
        _RID_SNIFFERS[sid] = sniffer
        sess["status"] = "started"
        sess["transports"] = active
        sess["pipeline"] = [f"{t}:opendroneid" for t in active] + (["dji_droneid"] if kind == "dji" else [])
        sess["message"] = f"Receiving live Remote ID over {' + '.join(active)} (OpenDroneID / F3411)."
    else:
        sess["status"] = "no_capture"
        sess["transports"] = []
        sess["pipeline"] = ["bleak (BLE OpenDroneID) and/or scapy + monitor-mode WiFi NIC"]
        sess["message"] = ("No OTA capture backend available — `pip install bleak` for BLE Remote ID "
                           "and/or `pip install scapy` with a monitor-mode WiFi adapter. "
                           "Showing a synthetic beacon for the offline demo.")
        sess["last"] = _synthetic_beacon(sess["started_ts"], *base)
    _RID_SESSIONS[sid] = sess
    return {k: v for k, v in sess.items() if not k.startswith("_")}


def rid_session_metadata(sid: str) -> Optional[dict]:
    s = _RID_SESSIONS.get(sid)
    if not s:
        return None
    sniffer = _RID_SNIFFERS.get(sid)
    if sniffer is not None:
        live = sniffer.latest()
        if live:
            p = max(live, key=lambda b: b.get("_rid_ts", 0))   # most-recent track
            s["last"] = p
            fc = {"type": "FeatureCollection",
                  "features": [f for b in live for f in rid_to_geojson(b).get("features", [])]}
            return {"session_id": sid, "kind": s["kind"], "parsed": p, "summary": p.get("summary"),
                    "live": True, "track_count": len(live), "stats": sniffer.stats(), "geojson": fc}
        # sniffer running but nothing heard yet
        return {"session_id": sid, "kind": s["kind"], "parsed": None, "summary": None,
                "live": True, "track_count": 0, "stats": sniffer.stats(),
                "geojson": {"type": "FeatureCollection", "features": []}}
    # no capture backend → synthetic demo beacon
    base = s.get("_base", (36.114, -115.173))
    p = _synthetic_beacon(time.time(), *base)
    s["last"] = p
    return {"session_id": sid, "kind": s["kind"], "parsed": p, "summary": p.get("summary"),
            "live": False, "geojson": rid_to_geojson(p)}


def list_rid_sessions() -> list[dict]:
    return [{k: v for k, v in s.items() if not k.startswith("_")} for s in _RID_SESSIONS.values()]


def stop_rid_session(sid: str) -> bool:
    sniffer = _RID_SNIFFERS.pop(sid, None)
    if sniffer is not None:
        try: sniffer.stop()
        except Exception: pass
    return _RID_SESSIONS.pop(sid, None) is not None


def status() -> dict:
    return {"astm_f3411": "decode + encode (Basic ID / Location / Self-ID / System / Operator ID / Message Pack)",
            "dji_droneid": "best-effort plaintext parse (v1 layout); v2 obfuscated tail handed to the published `dji_droneid` decoder",
            "geojson": "drone + operator + home + operating-area; CoT (UAS + operator markers)",
            "tools": available_tools(), "active_sessions": len(_RID_SESSIONS),
            "note": "This is the UNENCRYPTED telemetry beacon — not a video decrypt. RF capture (WiFi/BT sniff / OFDM RX) is handed off."}
