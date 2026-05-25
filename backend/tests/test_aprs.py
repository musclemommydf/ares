# SPDX-License-Identifier: MIT OR Apache-2.0
# Copyright (c) 2026 Ares

"""
Validation harness for the APRS decoder (decoders/aprs.py).

Run from `backend/`:   python -m tests.test_aprs

Tests:
  1. Uncompressed position — the canonical APRS-spec example
     (!4903.50N/07201.75W) → 49.0583, -72.0292, symbol "/-".
  2. Course/speed extension — "088/036" → course 88°, speed 36 kt.
  3. Timestamped position (@…z…) parses the same way past the 7-char stamp.
  4. Compressed (base-91) — round-trip encode → decode within 1e-3°.
  5. Mic-E — round-trip encode → decode for a NW and a SE/​offset station.
  6. AX.25 framing — build a UI frame, parse callsigns + info.
  7. Decoder + outputs — step() builds the station table; GeoJSON + CoT emit.
"""
from __future__ import annotations

import sys

sys.path.insert(0, ".")

from app.core.decoders import aprs


# ── encoders (test-local, inverse of the decoder) ────────────────────────────
def _b91e(v: int, width: int) -> str:
    out = []
    for _ in range(width):
        out.append(chr(33 + v % 91))
        v //= 91
    return "".join(reversed(out))


def _compress(lat: float, lon: float, sym_table="/", sym_code=">") -> str:
    y = round((90.0 - lat) * 380926.0)
    x = round((lon + 180.0) * 190463.0)
    return sym_table + _b91e(y, 4) + _b91e(x, 4) + sym_code + "  " + "A"   # no cs


def _mice(lat, lon, speed, course, sym_code=">", sym_table="/") -> tuple[str, str]:
    la = abs(lat)
    deg = int(la); mins = (la - deg) * 60; mm = int(mins); hund = round((mins - mm) * 100)
    digs = [deg // 10, deg % 10, mm // 10, mm % 10, hund // 10, hund % 10]
    north = lat >= 0; west = lon < 0; lon_off = abs(lon) >= 100
    dest = []
    for i, d in enumerate(digs):
        hi = (i == 3 and north) or (i == 4 and lon_off) or (i == 5 and west)
        dest.append(chr(ord("P") + d) if hi else chr(ord("0") + d))
    lo = abs(lon)
    lon_deg = int(lo); lmf = (lo - lon_deg) * 60; lmin = int(lmf); lhund = round((lmf - lmin) * 100)
    info1 = (lon_deg - (100 if lon_off else 0)) + 28
    sp = int(speed)
    dc = (sp % 10) * 10 + course // 100
    info = ("`" + chr(info1) + chr(lmin + 28) + chr(lhund + 28)
            + chr(sp // 10 + 28) + chr(dc + 28) + chr(course % 100 + 28) + sym_code + sym_table)
    return "".join(dest), info


def _ax25(src, dest, info, src_ssid=0, dest_ssid=0) -> bytes:
    def addr(call, ssid, last):
        b = bytes((ord(c) << 1) & 0xFE for c in call.ljust(6)[:6])
        return b + bytes([0x60 | ((ssid & 0x0F) << 1) | (1 if last else 0)])
    return addr(dest, dest_ssid, False) + addr(src, src_ssid, True) + bytes([0x03, 0xF0]) + info.encode("latin-1")


# ── tests ────────────────────────────────────────────────────────────────────
def test_uncompressed():
    st = aprs.AprsDecoderState().step("K1ABC>APRS,TCPIP*:!4903.50N/07201.75W-Test")
    ok = (st and abs(st.lat - 49.05833) < 1e-4 and abs(st.lon + 72.02917) < 1e-4
          and st.symbol == "/-" and st.comment == "Test" and st.callsign == "K1ABC")
    return ("uncompressed (spec example)", ok,
            f"lat={st.lat:.5f} lon={st.lon:.5f} sym={st.symbol!r}" if st else "no fix")


def test_course_speed():
    st = aprs.AprsDecoderState().step("N0CALL>APRS:!4903.50N/07201.75W>088/036Comment")
    ok = st and st.course_deg == 88.0 and st.speed_kt == 36.0 and st.comment == "Comment"
    return ("course/speed extension", ok,
            f"course={st.course_deg} speed={st.speed_kt} kt" if st else "no fix")


def test_timestamped():
    st = aprs.AprsDecoderState().step("K1ABC>APRS:@092345z4903.50N/07201.75W>")
    ok = st and abs(st.lat - 49.05833) < 1e-4 and abs(st.lon + 72.02917) < 1e-4
    return ("timestamped position", ok, f"lat={st.lat:.5f} lon={st.lon:.5f}" if st else "no fix")


def test_compressed():
    body = _compress(37.12345, -122.6789)
    st = aprs.AprsDecoderState().step("W6XYZ>APRS:=" + body)
    ok = st and abs(st.lat - 37.12345) < 1e-3 and abs(st.lon + 122.6789) < 1e-3
    return ("compressed (base-91 round-trip)", ok,
            f"lat={st.lat:.5f} lon={st.lon:.5f}" if st else "no fix")


def test_mice():
    cases = [(49.0583, -72.0292, 36, 88), (-33.8600, 151.2090, 0, 0)]
    worst = 0.0
    for lat, lon, sp, crs in cases:
        dest, info = _mice(lat, lon, sp, crs)
        st = aprs.AprsDecoderState().step({"source": "VK2ABC", "dest": dest, "digis": [], "info": info})
        if not st or st.lat is None:
            return ("Mic-E round-trip", False, f"no fix for {lat},{lon}")
        worst = max(worst, abs(st.lat - lat), abs(st.lon - lon))
        if abs(st.speed_kt - sp) > 0.5 or abs(st.course_deg - crs) > 0.5:
            return ("Mic-E round-trip", False, f"speed/course off: {st.speed_kt}/{st.course_deg}")
    ok = worst < 1e-2
    return ("Mic-E round-trip (NW + SE)", ok, f"max |Δ| = {worst:.4f}°")


def test_ax25():
    frame = _ax25("K1ABC", "APRS", "!4903.50N/07201.75W-", src_ssid=7)
    p = aprs.parse_ax25(frame)
    ok = p and p["source"] == "K1ABC-7" and p["dest"] == "APRS" and p["info"].startswith("!4903.50N")
    st = aprs.AprsDecoderState().step(frame)
    ok = ok and st and abs(st.lat - 49.05833) < 1e-4
    return ("AX.25 frame parse", bool(ok), f"src={p['source']!r} dest={p['dest']!r}" if p else "parse failed")


def test_decoder_outputs():
    dec = aprs.AprsDecoderState()
    dec.step("K1ABC>APRS:!4903.50N/07201.75W-A")
    dec.step("W6XYZ>APRS:!3725.00N/12215.00W>B")
    gj = aprs.stations_geojson(dec)
    cot = aprs.station_to_cot(dec.stations["K1ABC"])
    ok = (gj["type"] == "FeatureCollection" and len(gj["features"]) == 2
          and gj["features"][0]["properties"]["kind"] == "aprs"
          and cot and cot["uid"] == "aprs-K1ABC" and cot["type"].startswith("a-f-G"))
    return ("decoder → GeoJSON + CoT", ok, f"{len(gj['features'])} features, cot uid={cot['uid']!r}")


def main() -> int:
    tests = [test_uncompressed, test_course_speed, test_timestamped, test_compressed,
             test_mice, test_ax25, test_decoder_outputs]
    passed = 0
    print("=" * 72)
    print("Ares — APRS decoder validation harness")
    print("=" * 72)
    for fn in tests:
        try:
            name, ok, detail = fn()
        except Exception as e:
            print(f"  ✗ {fn.__name__}  CRASH  {type(e).__name__}: {e}")
            continue
        flag = "✓" if ok else "✗"
        print(f"  {flag} {name:34s}  {detail}")
        if ok:
            passed += 1
    print("-" * 72)
    print(f"  {passed}/{len(tests)} APRS tests passed")
    return 0 if passed == len(tests) else 1


if __name__ == "__main__":
    sys.exit(main())
