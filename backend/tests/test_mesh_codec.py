# SPDX-License-Identifier: MIT OR Apache-2.0
# Copyright (c) 2026 Ares

"""
Validation harness for the compact MANET codec (Track D, D2.1).

Run from `backend/`:   python -m tests.test_mesh_codec

Tests:
  1. LoB round-trip — every numeric field and string survives encode→decode
     exactly (float64 fidelity), including hops and the sig.
  2. LoB HMAC preserved — a LoB signed by meshsec still *verifies* after a codec
     round-trip; the binary form does not invalidate the signature.
  3. LoB tamper rejected — flipping a byte in the encoded lat makes the decoded
     LoB fail meshsec verification (the signature still pins the content).
  4. Chat round-trip incl. no-geo — a chat *with* and *without* lat/lon both
     round-trip (None stays None, not 0.0) and both verify under meshsec.
  5. Compactness — an encoded LoB is much smaller than its JSON form and fits a
     single Meshtastic LoRa frame (< 200 bytes).
  6. Dispatch — encode()/decode() route by magic byte to the right kind.
"""
from __future__ import annotations

import json
import struct
import sys
import time

# Allow running as `python -m tests.test_mesh_codec` from backend/
sys.path.insert(0, ".")

from app.core import meshsec
from app.core.sdr import mesh_codec

_TEST_SECRET = "ares-test-mesh-secret-do-not-use-in-prod"


def _sample_lob() -> dict:
    """A LoB dict shaped like manager.asdict(LobEvent) — all numerics are floats."""
    return {
        "id": "a1b2c3d4e5f6",
        "origin_node": "9f8e7d6c5b4a",
        "origin_device": "kraken-0",
        "device_id": "kraken-0",
        "lat": 38.8976763, "lon": -77.0365298,
        "azimuth_deg": 142.736512, "frequency_hz": 462562500.0,
        "rssi_dbm": -73.4, "t": 1748131200.123456,
        "confidence_pct": 88.0, "observer_height_m": 2.0,
        "estimated_distance_m": 1234.5,
        "environment": "suburban", "device_type": "kraken",
        "target_device_id": "", "hops": 2,
    }


def _sample_chat(with_geo: bool) -> dict:
    d = {"from_node": "9f8e7d6c5b4a", "id": "m42", "room": "All",
         "text": "contact bearing 142°, possible repeater input", "t": time.time()}
    if with_geo:
        d["lat"], d["lon"] = 38.8977, -77.0365
    else:
        d["lat"], d["lon"] = None, None
    return d


def test_lob_roundtrip():
    src = _sample_lob()
    dec = mesh_codec.decode_lob(mesh_codec.encode_lob(src))
    for f in ("lat", "lon", "azimuth_deg", "frequency_hz", "rssi_dbm", "t",
              "confidence_pct", "observer_height_m", "estimated_distance_m"):
        if dec[f] != src[f]:
            return ("lob round-trip", False, f"{f}: {dec[f]!r} != {src[f]!r}")
    for f in ("id", "origin_node", "origin_device", "device_id",
              "environment", "device_type", "target_device_id"):
        if dec[f] != src[f]:
            return ("lob round-trip", False, f"{f}: {dec[f]!r} != {src[f]!r}")
    if dec["hops"] != src["hops"]:
        return ("lob round-trip", False, f"hops {dec['hops']} != {src['hops']}")
    return ("lob round-trip", True, "all 9 doubles + 7 strings + hops exact")


def test_lob_hmac_preserved():
    meshsec._SECRET = _TEST_SECRET
    try:
        src = _sample_lob()
        src["sig"] = meshsec.sign_lob(src)
        if not src["sig"]:
            return ("lob HMAC preserved", False, "sign_lob returned no signature")
        dec = mesh_codec.decode_lob(mesh_codec.encode_lob(src))
        ok = meshsec.verify_lob(dec)
        return ("lob HMAC preserved", ok,
                f"sig {src['sig'][:8]}… verifies after round-trip" if ok
                else "verify_lob failed after round-trip")
    finally:
        meshsec._SECRET = None


def test_lob_tamper_rejected():
    meshsec._SECRET = _TEST_SECRET
    try:
        src = _sample_lob()
        src["sig"] = meshsec.sign_lob(src)
        frame = bytearray(mesh_codec.encode_lob(src))
        # lat is the first f64 after magic(1)+ver(1)+hops(1)+sigflag(1)+sig(16) = offset 20.
        # Overwrite it with a value 1° off — a change well above the signed 6-decimal
        # (~0.1 m) precision, so the canonical form (and thus the HMAC) must change.
        frame[20:28] = struct.pack("<d", src["lat"] + 1.0)
        dec = mesh_codec.decode_lob(bytes(frame))
        rejected = not meshsec.verify_lob(dec)
        return ("lob tamper rejected", rejected,
                "lat-shifted frame fails verification" if rejected
                else "tampered LoB wrongly verified")
    finally:
        meshsec._SECRET = None


def test_chat_roundtrip_nogeo():
    meshsec._SECRET = _TEST_SECRET
    try:
        for with_geo in (True, False):
            src = _sample_chat(with_geo)
            src["sig"] = meshsec.sign_chat(src)
            dec = mesh_codec.decode_chat(mesh_codec.encode_chat(src))
            if dec["lat"] != src["lat"] or dec["lon"] != src["lon"]:
                return ("chat round-trip (geo/no-geo)", False,
                        f"geo={with_geo}: lat/lon {dec['lat']},{dec['lon']} != {src['lat']},{src['lon']}")
            if dec["text"] != src["text"]:
                return ("chat round-trip (geo/no-geo)", False, "text mismatch")
            if not meshsec.verify_chat(dec):
                return ("chat round-trip (geo/no-geo)", False, f"geo={with_geo}: HMAC failed")
        return ("chat round-trip (geo/no-geo)", True, "None stays None; both verify")
    finally:
        meshsec._SECRET = None


def test_compactness():
    src = _sample_lob()
    enc = mesh_codec.encode_lob(src)
    js = len(json.dumps(src).encode())
    ok = len(enc) < 200 and len(enc) < js
    return ("compactness (fits LoRa)", ok,
            f"binary {len(enc)} B vs JSON {js} B (LoRa limit ~200 B)")


def test_dispatch():
    lob = _sample_lob()
    chat = _sample_chat(True)
    k1, d1 = mesh_codec.decode(mesh_codec.encode("lob", lob))
    k2, d2 = mesh_codec.decode(mesh_codec.encode("chat", chat))
    ok = k1 == "lob" and k2 == "chat" and d1["frequency_hz"] == lob["frequency_hz"] and d2["text"] == chat["text"]
    return ("magic-byte dispatch", ok, f"decoded kinds: {k1!r}, {k2!r}")


def main() -> int:
    tests = [
        test_lob_roundtrip,
        test_lob_hmac_preserved,
        test_lob_tamper_rejected,
        test_chat_roundtrip_nogeo,
        test_compactness,
        test_dispatch,
    ]
    passed = 0
    print("=" * 72)
    print("Ares — MANET compact-codec validation harness")
    print("=" * 72)
    for fn in tests:
        try:
            name, ok, detail = fn()
        except Exception as e:
            print(f"  ✗ {fn.__name__}  CRASH  {type(e).__name__}: {e}")
            continue
        flag = "✓" if ok else "✗"
        print(f"  {flag} {name:32s}  {detail}")
        if ok:
            passed += 1
    print("-" * 72)
    print(f"  {passed}/{len(tests)} mesh-codec tests passed")
    return 0 if passed == len(tests) else 1


if __name__ == "__main__":
    sys.exit(main())
