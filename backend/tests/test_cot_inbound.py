# SPDX-License-Identifier: MIT OR Apache-2.0
# Copyright (c) 2026 Ares

"""
Validation harness for inbound CoT LoB/fix re-ingest (Track D, D1.4b).

Run from `backend/`:   python -m tests.test_cot_inbound

Tests:
  1. LoB (u-d-r) parse — a drawn-route CoT from an external sensor yields
     kind="lob" with the sensor position and a bearing derived from the two
     link points.
  2. Fix (a-u-G-U-C-I) parse — an unknown-ground point yields kind="fix" with
     lat/lon and the CEP from the point's ce.
  3. Own CoT skipped — a uid starting "ares-" returns None (no self-loop).
  4. Non-track CoT ignored — GeoChat (b-t-f) and friendly units (a-f-…) → None.
  5. Receiver routing — _CotRxProtocol.datagram_received feeds a parsed LoB to
     the registered track sink.
"""
from __future__ import annotations

import sys

sys.path.insert(0, ".")

from app.core import cot


def _lob_xml(uid: str, slat: float, slon: float, elat: float, elon: float) -> bytes:
    return (
        f'<event version="2.0" uid="{uid}" type="u-d-r" how="h-g-i-g-o">'
        f'<point lat="{slat}" lon="{slon}" hae="0" ce="9999999" le="0"/>'
        f'<detail>'
        f'<link uid="{uid}-a" point="{slat},{slon},0" type="b-m-p-w-GOTO" relation="c"/>'
        f'<link uid="{uid}-b" point="{elat},{elon},0" type="b-m-p-w-GOTO" relation="c"/>'
        f'<remarks>external sensor LoB</remarks>'
        f'</detail></event>'
    ).encode()


def _fix_xml(uid: str, lat: float, lon: float, ce: float) -> bytes:
    return (
        f'<event version="2.0" uid="{uid}" type="a-u-G-U-C-I" how="m-g">'
        f'<point lat="{lat}" lon="{lon}" hae="0" ce="{ce}" le="0"/>'
        f'<detail><remarks>hostile emitter</remarks></detail></event>'
    ).encode()


def test_lob_parse():
    # endpoint due north of the sensor ⇒ bearing ≈ 0/360
    t = cot._parse_cot_track(_lob_xml("ext-lob-1", 38.0, -77.0, 38.1, -77.0))
    if not t or t.get("kind") != "lob":
        return ("inbound LoB (u-d-r)", False, f"got {t}")
    az = t["azimuth_deg"]
    north = abs(((az + 180) % 360) - 180) < 2.0
    ok = north and abs(t["lat"] - 38.0) < 1e-9 and abs(t["lon"] + 77.0) < 1e-9
    return ("inbound LoB (u-d-r)", ok, f"az={az:.2f}° (expect ~0), pos={t['lat']},{t['lon']}")


def test_fix_parse():
    t = cot._parse_cot_track(_fix_xml("ext-fix-1", 38.5, -77.2, 250.0))
    ok = bool(t) and t.get("kind") == "fix" and abs(t.get("cep_m", -1) - 250.0) < 1e-6
    return ("inbound fix (a-u-G-U-C-I)", ok, f"got kind={t and t.get('kind')}, cep={t and t.get('cep_m')}")


def test_own_cot_skipped():
    t = cot._parse_cot_track(_lob_xml("ares-lob-42", 38.0, -77.0, 38.1, -77.0))
    return ("own CoT skipped", t is None, "ares-* uid returns None" if t is None else f"leaked {t}")


def test_non_track_ignored():
    geochat = b'<event version="2.0" uid="GeoChat.x.All.1" type="b-t-f"><point lat="0" lon="0" ce="9e9"/></event>'
    friendly = _fix_xml("ext-2", 1.0, 2.0, 50.0).replace(b"a-u-G-U-C-I", b"a-f-G-U-C-I")
    ok = cot._parse_cot_track(geochat) is None and cot._parse_cot_track(friendly) is None
    return ("non-track CoT ignored", ok, "GeoChat + friendly unit both → None")


def test_receiver_routing():
    captured: list[dict] = []
    cot.set_track_sink(captured.append)
    try:
        proto = cot._CotRxProtocol()
        proto.datagram_received(_lob_xml("ext-lob-9", 40.0, -80.0, 40.0, -79.9), ("127.0.0.1", 0))
        ok = len(captured) == 1 and captured[0]["kind"] == "lob" and captured[0]["uid"] == "ext-lob-9"
        return ("receiver → track sink", ok, f"sink received {len(captured)} track(s)")
    finally:
        cot.set_track_sink(None)


def main() -> int:
    tests = [test_lob_parse, test_fix_parse, test_own_cot_skipped,
             test_non_track_ignored, test_receiver_routing]
    passed = 0
    print("=" * 72)
    print("Ares — inbound CoT LoB/fix re-ingest harness")
    print("=" * 72)
    for fn in tests:
        try:
            name, ok, detail = fn()
        except Exception as e:
            print(f"  ✗ {fn.__name__}  CRASH  {type(e).__name__}: {e}")
            continue
        flag = "✓" if ok else "✗"
        print(f"  {flag} {name:28s}  {detail}")
        if ok:
            passed += 1
    print("-" * 72)
    print(f"  {passed}/{len(tests)} inbound-CoT tests passed")
    return 0 if passed == len(tests) else 1


if __name__ == "__main__":
    sys.exit(main())
