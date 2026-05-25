# SPDX-License-Identifier: MIT OR Apache-2.0
# Copyright (c) 2026 Ares

"""
mesh_codec.py — compact binary codec for MANET LoB / chat messages (Track D, D2.1).

The default mesh transport ships LoB / chat events as JSON over a WebSocket
(:mod:`app.core.sdr.mesh`). That's fine on an IP link, but a Meshtastic LoRa
frame carries only ~200 usable bytes, and a JSON LoB is ~350-450 B. This module
packs the same events into a compact little-endian binary frame (~150 B for a
LoB) so they fit a single low-bandwidth frame.

**Critical invariant — the HMAC stays valid.** The mesh signature (:mod:`meshsec`)
is computed over a canonical string of a *subset* of fields, with floats
formatted ``"{:.6f}"``. So the codec must round-trip those fields losslessly:
every numeric field is stored as a float64 (a Python float round-trips exactly,
so it re-formats to the identical ``{:.6f}`` string), and every string verbatim.
A decoded event therefore still verifies under the same secret — exactly what
``test_mesh_codec`` asserts end-to-end.

Frame layout (little-endian)::

    LoB : 0xA1 ver hops sig?(1+16) | 9× f64 | 7× str(uint16-len + utf8)
    Chat: 0xC1 ver flags sig?(1+16) | t:f64 [lat:f64][lon:f64] | 4× str

The leading magic byte lets a single channel carry both kinds (see
:func:`decode`). ``hops`` is transport metadata (meshsec does not sign it).
"""
from __future__ import annotations

import struct
from typing import Optional

_LOB_MAGIC = 0xA1
_CHAT_MAGIC = 0xC1
_VERSION = 1

# float64 fields carried for a LoB. The first six are the ones meshsec signs
# (mirrors meshsec._LOB_FIELDS minus the strings); the rest are unsigned
# fidelity extras the fusion / range path consumes.
_LOB_DOUBLES = ("lat", "lon", "azimuth_deg", "frequency_hz", "rssi_dbm", "t",
                "confidence_pct", "observer_height_m", "estimated_distance_m")
_LOB_DOUBLE_DEFAULTS = {"rssi_dbm": -80.0, "confidence_pct": 80.0,
                        "observer_height_m": 1.5, "estimated_distance_m": 0.0}
_LOB_STRINGS = ("origin_node", "origin_device", "id", "device_id",
                "environment", "device_type", "target_device_id")

_CHAT_STRINGS = ("from_node", "id", "room", "text")


# ── primitives ───────────────────────────────────────────────────────────────
def _put_str(buf: bytearray, s) -> None:
    b = ("" if s is None else str(s)).encode("utf-8")[:0xFFFF]
    buf += struct.pack("<H", len(b))
    buf += b


def _get_str(mv: memoryview, off: int) -> tuple[str, int]:
    (n,) = struct.unpack_from("<H", mv, off)
    off += 2
    s = bytes(mv[off:off + n]).decode("utf-8", "replace")
    return s, off + n


def _sig_to_bytes(sig) -> Optional[bytes]:
    """meshsec signatures are 32 hex chars = 16 bytes. Anything else ⇒ no sig."""
    if not sig:
        return None
    try:
        b = bytes.fromhex(str(sig))
    except ValueError:
        return None
    return b if len(b) == 16 else None


# ── LoB ──────────────────────────────────────────────────────────────────────
def encode_lob(d: dict) -> bytes:
    buf = bytearray((_LOB_MAGIC, _VERSION, max(0, min(255, int(d.get("hops", 0))))))
    sig = _sig_to_bytes(d.get("sig"))
    buf.append(1 if sig else 0)
    if sig:
        buf += sig
    for f in _LOB_DOUBLES:
        v = d.get(f)
        if v is None:
            v = _LOB_DOUBLE_DEFAULTS.get(f, 0.0)
        buf += struct.pack("<d", float(v))
    for f in _LOB_STRINGS:
        _put_str(buf, d.get(f, ""))
    return bytes(buf)


def decode_lob(frame: bytes) -> dict:
    mv = memoryview(frame)
    if len(mv) < 4 or mv[0] != _LOB_MAGIC:
        raise ValueError("not a LoB frame")
    off = 2
    out: dict = {"hops": int(mv[off])}
    off += 1
    has_sig = mv[off]; off += 1
    if has_sig:
        out["sig"] = bytes(mv[off:off + 16]).hex(); off += 16
    for f in _LOB_DOUBLES:
        (v,) = struct.unpack_from("<d", mv, off); off += 8
        out[f] = v
    for f in _LOB_STRINGS:
        out[f], off = _get_str(mv, off)
    return out


# ── chat ─────────────────────────────────────────────────────────────────────
def encode_chat(d: dict) -> bytes:
    lat, lon = d.get("lat"), d.get("lon")
    # bit0 = lat present, bit1 = lon present — chat without geo keeps lat/lon None,
    # which meshsec canonicalises differently from 0.0, so presence must survive.
    flags = (1 if isinstance(lat, (int, float)) else 0) | (2 if isinstance(lon, (int, float)) else 0)
    buf = bytearray((_CHAT_MAGIC, _VERSION, flags))
    sig = _sig_to_bytes(d.get("sig"))
    buf.append(1 if sig else 0)
    if sig:
        buf += sig
    buf += struct.pack("<d", float(d.get("t") or 0.0))
    if flags & 1:
        buf += struct.pack("<d", float(lat))
    if flags & 2:
        buf += struct.pack("<d", float(lon))
    for f in _CHAT_STRINGS:
        _put_str(buf, d.get(f, ""))
    return bytes(buf)


def decode_chat(frame: bytes) -> dict:
    mv = memoryview(frame)
    if len(mv) < 4 or mv[0] != _CHAT_MAGIC:
        raise ValueError("not a chat frame")
    off = 2
    flags = mv[off]; off += 1
    has_sig = mv[off]; off += 1
    out: dict = {}
    if has_sig:
        out["sig"] = bytes(mv[off:off + 16]).hex(); off += 16
    (out["t"],) = struct.unpack_from("<d", mv, off); off += 8
    if flags & 1:
        (out["lat"],) = struct.unpack_from("<d", mv, off); off += 8
    else:
        out["lat"] = None
    if flags & 2:
        (out["lon"],) = struct.unpack_from("<d", mv, off); off += 8
    else:
        out["lon"] = None
    for f in _CHAT_STRINGS:
        out[f], off = _get_str(mv, off)
    return out


# ── one-channel dispatch ──────────────────────────────────────────────────────
def encode(kind: str, d: dict) -> bytes:
    if kind == "lob":
        return encode_lob(d)
    if kind == "chat":
        return encode_chat(d)
    raise ValueError(f"unknown kind {kind!r}")


def decode(frame: bytes) -> tuple[str, dict]:
    """Return ``(kind, dict)`` from the leading magic byte."""
    if not frame:
        raise ValueError("empty frame")
    if frame[0] == _LOB_MAGIC:
        return "lob", decode_lob(frame)
    if frame[0] == _CHAT_MAGIC:
        return "chat", decode_chat(frame)
    raise ValueError("unknown frame magic")
