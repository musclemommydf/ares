# SPDX-License-Identifier: MIT OR Apache-2.0
# Copyright (c) 2026 Ares

"""
Validation harness for the AFSK1200 + HDLC modem (sdr/afsk1200.py).

Run from `backend/`:   python -m tests.test_afsk1200

Tests:
  1. Clean round-trip — build an AX.25 APRS frame, modulate to Bell-202 audio,
     demod, recover the exact frame, and parse it to a position.
  2. Noisy round-trip — same with additive Gaussian noise (~9 dB) still decodes
     (FCS is all-or-nothing, so a recovered frame proves the chain).
  3. IQ path — FM-modulate the audio onto IQ, fm_demod + demod_iq recover it.
  4. FCS integrity — a single flipped data bit fails the FCS (no false frame).
"""
from __future__ import annotations

import sys

import numpy as np

sys.path.insert(0, ".")

from app.core.decoders import aprs
from app.core.sdr import afsk1200

FS = 48000


def _ax25(src, dest, info, src_ssid=0, dest_ssid=0) -> bytes:
    def addr(call, ssid, last):
        b = bytes((ord(c) << 1) & 0xFE for c in call.ljust(6)[:6])
        return b + bytes([0x60 | ((ssid & 0x0F) << 1) | (1 if last else 0)])
    return addr(dest, dest_ssid, False) + addr(src, src_ssid, True) + bytes([0x03, 0xF0]) + info.encode("latin-1")


_FRAME = _ax25("K1ABC", "APRS", "!4903.50N/07201.75W-Test", src_ssid=7)


def test_clean_roundtrip():
    audio = afsk1200.modulate([_FRAME], fs=FS)
    frames = afsk1200.demod(audio, fs=FS)
    if _FRAME not in frames:
        return ("clean round-trip", False, f"frame not recovered ({len(frames)} decoded)")
    st = aprs.AprsDecoderState().step(frames[frames.index(_FRAME)])
    ok = st and abs(st.lat - 49.05833) < 1e-4 and st.callsign == "K1ABC-7"
    return ("clean round-trip → APRS fix", ok,
            f"recovered {len(frames)} frame(s); lat={st.lat:.5f}" if st else "parse failed")


def test_noisy_roundtrip():
    rng = np.random.default_rng(0)
    audio = afsk1200.modulate([_FRAME], fs=FS)
    noisy = audio + rng.normal(0, 0.25, audio.size).astype(np.float32)   # ~9 dB SNR
    frames = afsk1200.demod(noisy, fs=FS)
    ok = _FRAME in frames
    return ("noisy round-trip (~9 dB)", ok, "recovered through noise" if ok else "lost in noise")


def test_iq_path():
    audio = afsk1200.modulate([_FRAME], fs=FS)
    dev = 2500.0
    iq = np.exp(1j * 2 * np.pi * np.cumsum(audio * dev) / FS).astype(np.complex64)
    frames = afsk1200.demod_iq(iq, fs=FS)
    ok = _FRAME in frames
    return ("IQ → FM-demod → frames", ok, "recovered from IQ" if ok else "not recovered")


def test_fcs_integrity():
    audio = afsk1200.modulate([_FRAME], fs=FS)
    frames = afsk1200.demod(audio, fs=FS)
    # FCS must accept the good frame and reject a corrupted copy
    good = _FRAME in frames
    bad = bytearray(_FRAME)
    bad[5] ^= 0x01
    reject = afsk1200.fcs(bytes(bad)) != afsk1200.fcs(_FRAME)
    return ("FCS accepts good / flags bad", good and reject,
            f"good={good}, corrupted-fcs-differs={reject}")


def main() -> int:
    tests = [test_clean_roundtrip, test_noisy_roundtrip, test_iq_path, test_fcs_integrity]
    passed = 0
    print("=" * 72)
    print("Ares — AFSK1200 + HDLC modem harness")
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
    print(f"  {passed}/{len(tests)} AFSK1200 tests passed")
    return 0 if passed == len(tests) else 1


if __name__ == "__main__":
    sys.exit(main())
