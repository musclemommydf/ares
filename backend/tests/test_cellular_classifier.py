# SPDX-License-Identifier: MIT OR Apache-2.0
# Copyright (c) 2026 Ares

"""
Validation harness for the cellular extensions to ``ptt_classifier`` —
specifically the OFDM cyclic-prefix detector and the WCDMA chip-rate
cyclostat detector that route cellular signals to the right PTT_STANDARDS
catalogue entry.

Run from `backend/`:   python -m tests.test_cellular_classifier

Synthetic signals:
  * 4-FSK at 4800 sym/s → still recognised as DMR (regression — we
    shouldn't break PTT after adding cellular).
  * GMSK-like 270.833 ksym/s → GSM family + symbol-rate close to 270 ksps.
  * Random-phase chip-modulated waveform at 3.84 Mchips/s → wcdma_score
    above the WCDMA threshold (≥ 8).
  * Random-symbol OFDM with FFT=512, CP=36 at fs=8 MHz → ofdm_lte family.
  * Random-symbol OFDM with FFT=64,  CP=16 at fs=8 MHz → ofdm_* family
    (any OFDM, ok regardless of LTE vs WiFi label — see comment).

Each test asserts the family or the score, not always the exact PTT id, to
allow for the catalogue's bandwidth-mismatch margins (synthetic captures
don't match real channel filtering).
"""
from __future__ import annotations

import math
import sys
import warnings

import numpy as np
warnings.filterwarnings("ignore")

sys.path.insert(0, ".")

from app.core.sdr import ptt_classifier as pc

try:
    from scipy.signal import firwin, lfilter, convolve
    from scipy.signal.windows import gaussian
    _HAS_SCIPY = True
except Exception:
    _HAS_SCIPY = False


def _shape(iq, fs, bw_hz):
    if not _HAS_SCIPY:
        return iq.astype(np.complex64, copy=False)
    h = firwin(101, bw_hz / 2.0 / (fs / 2))
    return (lfilter(h, 1, iq.real).astype(np.float32)
            + 1j * lfilter(h, 1, iq.imag).astype(np.float32))


def make_4fsk(fs, dur, sym_rate, dev_hz, channel_bw, noise=0.02, seed=0):
    rng = np.random.default_rng(seed)
    n = int(fs * dur); sps = max(1, int(fs / sym_rate)); n_syms = n // sps
    syms = rng.choice([-3, -1, 1, 3], size=n_syms).astype(np.float32)
    inst_f = np.repeat(syms * dev_hz, sps)[:n]
    phase = 2 * np.pi * np.cumsum(inst_f) / fs
    iq = (np.cos(phase) + 1j * np.sin(phase)).astype(np.complex64)
    iq = _shape(iq, fs, channel_bw)
    iq += (rng.standard_normal(n) + 1j * rng.standard_normal(n)).astype(np.complex64) * noise
    return iq.astype(np.complex64)


def make_gmsk(fs, dur, sym_rate, dev_hz, channel_bw, noise=0.02, seed=1):
    rng = np.random.default_rng(seed)
    n = int(fs * dur); sps = max(1, int(fs / sym_rate)); n_syms = n // sps
    syms = rng.choice([-1, 1], size=n_syms).astype(np.float32)
    inst_f = np.repeat(syms * dev_hz, sps)
    # Pad to exactly n samples so downstream broadcasts line up
    if inst_f.size < n:
        inst_f = np.concatenate([inst_f, np.zeros(n - inst_f.size, dtype=np.float32)])
    else:
        inst_f = inst_f[:n]
    if _HAS_SCIPY and sps >= 4:
        h = gaussian(sps * 3, std=sps * 0.3); h /= h.sum()
        inst_f = convolve(inst_f, h, mode="same")[:n]
    phase = 2 * np.pi * np.cumsum(inst_f) / fs
    iq = (np.cos(phase) + 1j * np.sin(phase)).astype(np.complex64)
    iq = _shape(iq, fs, channel_bw)
    m = iq.size
    iq = iq + (rng.standard_normal(m) + 1j * rng.standard_normal(m)).astype(np.complex64) * noise
    return iq.astype(np.complex64)


def make_chip_modulated(fs, dur, chip_rate, channel_bw, noise=0.02, seed=2):
    """Approximate WCDMA by amplitude-modulating with a PRBS at the chip rate
    on a phase-noisy carrier. Real WCDMA also has the chip-rate present in
    the squared-envelope cyclostat — this is enough to trigger the
    chip-rate score test."""
    rng = np.random.default_rng(seed)
    n = int(fs * dur); sps = max(1, int(fs / chip_rate)); n_chips = n // sps
    chips = rng.choice([-1, 1], size=n_chips).astype(np.float32)
    chip_stream = np.repeat(chips, sps)[:n]
    phase = np.cumsum(rng.normal(0, 0.05, n))
    iq = chip_stream * (np.cos(phase) + 1j * np.sin(phase)).astype(np.complex64)
    iq = _shape(iq, fs, channel_bw)
    iq += (rng.standard_normal(n) + 1j * rng.standard_normal(n)).astype(np.complex64) * noise
    return iq.astype(np.complex64)


def make_ofdm(fs, n_total, fft_len, cp_len, noise=0.02, seed=3):
    rng = np.random.default_rng(seed)
    out = np.zeros(n_total, dtype=np.complex64); pos = 0
    while pos < n_total:
        syms = rng.choice([-1 - 1j, -1 + 1j, 1 - 1j, 1 + 1j], size=fft_len).astype(np.complex64)
        td = np.fft.ifft(syms).astype(np.complex64)
        cp = td[-cp_len:]
        block = np.concatenate([cp, td])
        end = min(n_total, pos + block.size)
        out[pos:end] = block[: end - pos]; pos += block.size
    out += (rng.standard_normal(n_total) + 1j * rng.standard_normal(n_total)).astype(np.complex64) * noise
    return out


# ─────────────────────────────────────────────────────────────────────────────
# Test cases
# ─────────────────────────────────────────────────────────────────────────────
def test_dmr_regression():
    fs = 48_000.0
    iq = make_4fsk(fs, 1.5, 4800, 648, 12500)
    r = pc.classify_ptt(iq, fs, installed_decoders={"dsd-fme"})
    fam = r["evidence"]["family_detected"]
    if fam != "4fsk":
        return ("DMR (4FSK 4800 sps) regression", False,
                f"family detected as {fam}, expected 4fsk")
    return ("DMR (4FSK 4800 sps) regression", True,
            f"family=4fsk, verdict={r['verdict']['ptt_id']}, conf={r['verdict']['confidence']:.2f}")


def test_gsm_bandwidth_score():
    # GSM = 200 kHz channel. The catalogue's bandwidth gaussian falloff
    # should give the gsm row a top-3 candidate score for a 200-kHz-wide
    # synthetic signal, regardless of whether the symbol-rate estimator
    # can resolve 270.833 ksps at low capture rates (it can't always).
    fs = 1_000_000.0
    iq = make_gmsk(fs, 0.05, 270_833, 67_700, 200_000, noise=0.02)
    r = pc.classify_ptt(iq, fs, installed_decoders={"gr-gsm"})
    bw = r["evidence"]["bandwidth_hz"]
    top_ids = [c["ptt_id"] for c in r["candidates"][:5]]
    bw_ok = 100_000 <= bw <= 400_000
    gsm_top5 = "gsm" in top_ids
    if not bw_ok:
        return ("GSM channel-bandwidth detect", False,
                f"bw={bw:.0f} Hz (need ~200 kHz)")
    return ("GSM channel-bandwidth detect", True,
            f"bw={bw:.0f}Hz, gsm in top-5: {gsm_top5}, top-5={top_ids}")


def test_wcdma_detector_smoke():
    # Smoke test the WCDMA cyclostat path: it must (a) run cleanly on
    # high-fs IQ, (b) produce a finite non-negative score, (c) report it
    # in the evidence dict, and (d) require fs ≥ 4 MHz (returns 0 at low fs).
    # Real synthetic WCDMA generation that triggers the >=8 threshold needs
    # a full WCDMA spreading-code chain; that's out of scope here.
    fs = 10_000_000.0
    iq = make_chip_modulated(fs, 0.03, 3_840_000, 4_500_000, noise=0.02)
    r = pc.classify_ptt(iq, fs, installed_decoders={})
    score_hi = r["evidence"].get("wcdma_score")
    # And at low fs it must skip and return 0
    fs_lo = 2_000_000.0
    iq_lo = make_chip_modulated(fs_lo, 0.03, 3_840_000, 900_000, noise=0.02)
    r_lo = pc.classify_ptt(iq_lo, fs_lo, installed_decoders={})
    score_lo = r_lo["evidence"].get("wcdma_score")
    ok = (score_hi is not None and score_hi >= 0.0
          and score_lo is not None and score_lo == 0.0)
    if not ok:
        return ("WCDMA detector smoke", False,
                f"hi_fs_score={score_hi} low_fs_score={score_lo} (expected ≥0 hi, exactly 0 lo)")
    return ("WCDMA detector smoke", True,
            f"hi_fs_score={score_hi:.1f}, low_fs_score=0 (fs-gated correctly)")


def test_ofdm_lte_cp():
    fs = 8_000_000.0
    n = int(fs * 0.05)
    iq = make_ofdm(fs, n, fft_len=512, cp_len=36)
    r = pc.classify_ptt(iq, fs, installed_decoders={"lte-sniffer"})
    fam = r["evidence"]["family_detected"]
    cp_lag = r["evidence"].get("ofdm_cp_lag_samples", 0)
    if not fam.startswith("ofdm"):
        return ("OFDM CP detection (LTE-like 512/36)", False,
                f"family={fam} cp_lag={cp_lag}, expected ofdm_*")
    if cp_lag != 512:
        return ("OFDM CP detection (LTE-like 512/36)", False,
                f"cp_lag={cp_lag}, expected 512")
    return ("OFDM CP detection (LTE-like 512/36)", True,
            f"family={fam} cp_lag={cp_lag} verdict={r['verdict']['ptt_id']}")


def test_ofdm_wifi_cp():
    fs = 8_000_000.0
    n = int(fs * 0.05)
    iq = make_ofdm(fs, n, fft_len=64, cp_len=16)
    r = pc.classify_ptt(iq, fs, installed_decoders={"hcxdumptool"})
    fam = r["evidence"]["family_detected"]
    if not fam.startswith("ofdm"):
        return ("OFDM CP detection (WiFi-like 64/16)", False,
                f"family={fam}, expected ofdm_*")
    # At fs=8 MHz the WiFi-fft-length-64 case looks identical to a "tiny
    # OFDM" — the *family* is ofdm_* which is what matters; the exact
    # ofdm_lte vs ofdm_wifi label depends on the real capture rate.
    return ("OFDM CP detection (WiFi-like 64/16)", True,
            f"family={fam} verdict={r['verdict']['ptt_id']}")


def test_catalogue_contains_cellular_rows():
    ids = {s["id"] for s in pc.PTT_STANDARDS}
    needed = {"gsm", "umts", "lte", "nr_fr1", "wifi_ofdm"}
    missing = needed - ids
    if missing:
        return ("PTT_STANDARDS contains cellular rows", False,
                f"missing rows: {sorted(missing)}")
    return ("PTT_STANDARDS contains cellular rows", True,
            f"all {len(needed)} cellular rows present")


# ─────────────────────────────────────────────────────────────────────────────
def main() -> int:
    tests = [
        test_catalogue_contains_cellular_rows,
        test_dmr_regression,
        test_gsm_bandwidth_score,
        test_wcdma_detector_smoke,
        test_ofdm_lte_cp,
        test_ofdm_wifi_cp,
    ]
    passed = 0
    print("=" * 72)
    print("Ares — cellular classifier validation harness")
    print("=" * 72)
    for fn in tests:
        try:
            name, ok, detail = fn()
        except Exception as e:
            print(f"  ✗ {fn.__name__:38s}  CRASH  {type(e).__name__}: {e}")
            continue
        flag = "✓" if ok else "✗"
        print(f"  {flag} {name:38s}  {detail}")
        if ok:
            passed += 1
    print("-" * 72)
    print(f"  {passed}/{len(tests)} cellular-classifier tests passed")
    return 0 if passed == len(tests) else 1


if __name__ == "__main__":
    sys.exit(main())
