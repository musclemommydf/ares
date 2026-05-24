# SPDX-License-Identifier: MIT OR Apache-2.0
# Copyright (c) 2026 Ares

"""
sdr/native_demod.py — Ares' own in-process software demodulator for UAS video downlinks.

This is the demod chain itself, written in pure Python (numpy / scipy / Pillow) — Ares
does **not** shell out to leandvb / a DVB-T(2) receiver / SDRangel / ffmpeg / TSDuck for
the demod, and does not require SoapySDR: it processes whatever baseband IQ it's handed
(a real capture from a wired IQ provider, or the synthetic snapshot the offline build
produces) and recovers the signal here.

What it implements:

  * **Analog FM video** (NTSC / PAL / SECAM composite, and VSB/AM legacy) —
    FM discriminator (or envelope detector for VSB) → composite baseband → DC-block →
    sync-tip clamp → line-rate estimate from the composite autocorrelation → reslice into
    raster lines → de-interlaced frame buffers → 8-bit luma images (PNG).

  * **OFDM / COFDM** (DVB-T/T2, ISDB-T 1-seg, generic COFDM MPEG-TS / DTC-Vislink-class) —
    coarse CFO from the cyclic-prefix autocorrelation phase, symbol-timing from the CP
    correlation peak, per-symbol FFT, per-carrier one-tap normalisation (AGC/phase),
    QPSK / 16-QAM / 64-QAM hard demap → bitstream → byte stream → MPEG-TS reassembly
    (handed to ``video_exploit.demux_ts`` to pull PAT/PMT and the STANAG-4609 KLV track),
    plus EVM and a constellation snapshot.

  * **Single-carrier PSK/QAM** (DVB-S / S2 short-frame, DVB-C-class QAM MPEG-TS) —
    RRC matched filter → Gardner timing recovery → CMA + decision-directed equaliser →
    slicer → bitstream → byte stream → MPEG-TS reassembly, plus EVM and a constellation
    snapshot.

What it does **not** do (and says so): the heavyweight inner FEC of the broadcast standards
— DVB convolutional + Viterbi + RS(204,188) + the convolutional byte interleaver, DVB-S2
LDPC/BCH. The PHY demod above gets you to soft/hard symbols and (on a clean link) the TS;
those FEC blocks are the next stage and are flagged in the result.
"""
from __future__ import annotations

import io
import math
from typing import Optional

import numpy as np

try:  # scipy.signal is in requirements; degrade to numpy-only filtering if it's somehow absent
    from scipy import signal as _sps
except Exception:  # pragma: no cover
    _sps = None


# ════════════════════════════════════════════════════════════════════════════
# small DSP helpers
# ════════════════════════════════════════════════════════════════════════════
def _as_c64(iq) -> np.ndarray:
    x = np.asarray(iq)
    if not np.iscomplexobj(x):
        x = x.astype(np.complex64)
    return np.ascontiguousarray(x.astype(np.complex64))


def _fm_discriminate(x: np.ndarray) -> np.ndarray:
    """Polar FM/PM discriminator: instantaneous frequency = d(arg x)/dt (one-sample)."""
    if x.size < 2:
        return np.zeros(0, np.float32)
    d = x[1:] * np.conj(x[:-1])
    return np.angle(d).astype(np.float32)


def _envelope(x: np.ndarray) -> np.ndarray:
    return np.abs(x).astype(np.float32)


def _dc_block(v: np.ndarray, alpha: float = 0.999) -> np.ndarray:
    """Single-pole DC blocker (a-weighted high-pass at ~0)."""
    if v.size == 0:
        return v
    if _sps is not None:
        b = np.array([1.0, -1.0]); a = np.array([1.0, -alpha])
        return _sps.lfilter(b, a, v).astype(np.float32)
    out = np.empty_like(v, np.float32)
    xm1 = ym1 = 0.0
    for i, xi in enumerate(v):
        yi = xi - xm1 + alpha * ym1
        out[i] = yi; xm1 = xi; ym1 = yi
    return out


def _lowpass(v: np.ndarray, cutoff_frac: float, taps: int = 65) -> np.ndarray:
    """Zero-phase FIR low-pass; ``cutoff_frac`` is cutoff / Nyquist (0..1)."""
    if v.size == 0 or _sps is None or cutoff_frac >= 0.99:
        return v
    cutoff_frac = max(1e-3, min(0.99, cutoff_frac))
    h = _sps.firwin(taps, cutoff_frac)
    return _sps.filtfilt(h, [1.0], v).astype(np.float32) if v.size > 3 * taps else _sps.lfilter(h, [1.0], v).astype(np.float32)


def _normalise_power(x: np.ndarray) -> np.ndarray:
    p = float(np.mean(np.abs(x) ** 2)) + 1e-12
    return (x / math.sqrt(p)).astype(np.complex64)


def _rrc_taps(beta: float, sps: int, span: int = 8) -> np.ndarray:
    """Root-raised-cosine pulse, ``span`` symbols, ``sps`` samples/symbol."""
    n = np.arange(-span * sps / 2, span * sps / 2 + 1)
    t = n / sps
    out = np.zeros_like(t)
    for i, ti in enumerate(t):
        if abs(ti) < 1e-9:
            out[i] = 1.0 - beta + 4 * beta / math.pi
        elif beta > 0 and abs(abs(4 * beta * ti) - 1.0) < 1e-9:
            out[i] = (beta / math.sqrt(2)) * ((1 + 2 / math.pi) * math.sin(math.pi / (4 * beta))
                                              + (1 - 2 / math.pi) * math.cos(math.pi / (4 * beta)))
        else:
            num = math.sin(math.pi * ti * (1 - beta)) + 4 * beta * ti * math.cos(math.pi * ti * (1 + beta))
            den = math.pi * ti * (1 - (4 * beta * ti) ** 2)
            out[i] = num / den
    out = out / math.sqrt(np.sum(out ** 2))
    return out.astype(np.float64)


# ════════════════════════════════════════════════════════════════════════════
# QAM / PSK constellations + (de)map
# ════════════════════════════════════════════════════════════════════════════
def _qam_levels(order: int) -> np.ndarray:
    """1-D PAM level set for a square M-QAM (M = order); e.g. 16-QAM → [-3,-1,1,3]."""
    m = int(round(math.sqrt(order)))
    return np.arange(-(m - 1), m, 2, dtype=np.float64)


def _bits_per_symbol(mod: str) -> int:
    return {"bpsk": 1, "qpsk": 2, "8psk": 3, "16qam": 4, "32qam": 5, "64qam": 6, "256qam": 8}.get(mod, 2)


def _slice_constellation(sym: np.ndarray, mod: str) -> tuple[np.ndarray, np.ndarray, float]:
    """Hard-decide ``sym`` (assumed unit-average-power) → (decided_symbols, hard_bits, evm_pct)."""
    sym = np.asarray(sym, np.complex64)
    if sym.size == 0:
        return sym, np.zeros(0, np.uint8), 0.0
    if mod in ("bpsk",):
        dec = np.sign(sym.real).astype(np.complex64)
        bits = (dec.real > 0).astype(np.uint8)
    elif mod in ("qpsk", "8psk"):  # 8psk handled coarsely as qpsk-with-extra-phase → treat as qpsk
        dec = (np.sign(sym.real) + 1j * np.sign(sym.imag)).astype(np.complex64) / math.sqrt(2)
        bits = np.column_stack([(sym.real > 0).astype(np.uint8), (sym.imag > 0).astype(np.uint8)]).reshape(-1)
    else:  # square QAM
        order = {"16qam": 16, "32qam": 32, "64qam": 64, "256qam": 256}.get(mod, 16)
        lv = _qam_levels(order)
        scale = math.sqrt(np.mean(lv ** 2) * 2.0)  # so the constellation has unit average power
        si = sym.real * scale; sq = sym.imag * scale
        di = lv[np.argmin(np.abs(si[:, None] - lv[None, :]), axis=1)]
        dq = lv[np.argmin(np.abs(sq[:, None] - lv[None, :]), axis=1)]
        dec = ((di + 1j * dq) / scale).astype(np.complex64)
        m = len(lv)
        # Gray-ish index → bits, MSB first
        def to_bits(d):
            idx = ((d + (m - 1)) / 2).astype(int)
            nb = int(round(math.log2(m)))
            return ((idx[:, None] >> np.arange(nb - 1, -1, -1)[None, :]) & 1).astype(np.uint8)
        bits = np.concatenate([to_bits(di), to_bits(dq)], axis=1).reshape(-1)
    err = sym - dec
    evm = float(math.sqrt(np.mean(np.abs(err) ** 2) / (np.mean(np.abs(dec) ** 2) + 1e-12))) * 100.0
    return dec, bits.astype(np.uint8), round(evm, 2)


def _derotate_qpsk_qam(syms: np.ndarray) -> np.ndarray:
    """Blind 4th-power carrier-phase removal for QPSK / square-QAM. For a correctly-oriented
    constellation E[s⁴] is real-negative, so the residual rotation is (∠E[s⁴] − π)/4 (mod 90°);
    a clean, properly-oriented constellation therefore gets no rotation applied."""
    if syms.size < 16:
        return syms
    m = np.mean(syms ** 4)
    if abs(m) < 1e-12:
        return syms
    phi = (np.angle(m) - np.pi) / 4.0
    return (syms * np.exp(-1j * phi)).astype(np.complex64)


def _bits_to_bytes(bits: np.ndarray) -> bytes:
    bits = np.asarray(bits, np.uint8).reshape(-1)
    n = (bits.size // 8) * 8
    if n == 0:
        return b""
    return np.packbits(bits[:n]).tobytes()


def _constellation_snapshot(sym: np.ndarray, n: int = 256) -> list[list[float]]:
    if sym.size == 0:
        return []
    step = max(1, sym.size // n)
    s = sym[::step][:n]
    return [[round(float(v.real), 4), round(float(v.imag), 4)] for v in s]


# ════════════════════════════════════════════════════════════════════════════
# 1) Analog FM / VSB video → frame buffers
# ════════════════════════════════════════════════════════════════════════════
_VIDEO_SYS = {
    "ntsc":  {"lines": 525, "fields_hz": 59.94, "line_hz": 15734.0},
    "pal":   {"lines": 625, "fields_hz": 50.0,  "line_hz": 15625.0},
    "secam": {"lines": 625, "fields_hz": 50.0,  "line_hz": 15625.0},
    "vsb":   {"lines": 525, "fields_hz": 59.94, "line_hz": 15734.0},
}


def _estimate_line_rate(comp: np.ndarray, fs: float, nominal_hz: float) -> float:
    """Estimate the horizontal line rate by autocorrelation of the (high-passed) composite
    around the nominal period."""
    if comp.size < 4096:
        return nominal_hz
    v = comp - comp.mean()
    lo = int(fs / (nominal_hz * 1.06)); hi = int(fs / (nominal_hz * 0.94))
    lo = max(8, lo); hi = max(lo + 4, min(hi, v.size // 4))
    seg = v[: min(v.size, 1 << 17)]
    best_lag, best_val = int(fs / nominal_hz), -1e30
    # coarse scan then nothing fancy — enough to lock the raster
    for lag in range(lo, hi, max(1, (hi - lo) // 400)):
        a = seg[:-lag]; b = seg[lag:]
        c = float(np.dot(a, b)) / (np.linalg.norm(a) * np.linalg.norm(b) + 1e-12)
        if c > best_val:
            best_val, best_lag = c, lag
    return float(fs / best_lag) if best_lag > 0 else nominal_hz


# ════════════════════════════════════════════════════════════════════════════
# Detector candidates — try multiple demod paths in parallel and pick the
# one whose composite signal carries the cleanest periodic sync structure.
# ════════════════════════════════════════════════════════════════════════════
def _iq_balanced_fm(x: np.ndarray) -> np.ndarray:
    """Quadrature/IQ-balanced FM discriminator. Same as `_fm_discriminate` but
    centres the spectrum first (DC-offset and any residual carrier) and uses
    the conjugate-difference. Often cleaner than the polar form on low-SNR
    drifty links because the DC offset doesn't bias the phase angle."""
    if x.size < 2:
        return np.zeros(0, np.float32)
    # Centre DC
    x = x - np.mean(x)
    # Normalise envelope so amplitude variations don't show in the freq output
    a = np.abs(x); med = float(np.median(a)) or 1.0
    xn = x / med
    d = xn[1:] * np.conj(xn[:-1])
    # Real/Imag ratio gives the instantaneous frequency directly — equivalent
    # to angle() but cheaper and not subject to the +π/-π wrap when |Im| is small.
    return np.arctan2(d.imag, d.real).astype(np.float32)


def _detector_score(comp: np.ndarray, fs: float, nominal_line_hz: float) -> dict:
    """How sync-like is this composite stream? Returns
        { line_hz, peak_corr, score, sync_pulses_per_line, line_jitter_pct }
    where `score` is the figure of merit (bigger = better)."""
    if comp.size < 4096:
        return {"line_hz": nominal_line_hz, "score": 0.0, "peak_corr": 0.0,
                  "sync_pulses_per_line": 0.0, "line_jitter_pct": 100.0}
    # High-pass the composite to isolate the sync-tip excursions
    v = comp.astype(np.float32) - comp.mean()
    # autocorrelation around nominal period
    nom_lag = int(fs / nominal_line_hz)
    lo = max(8, int(fs / (nominal_line_hz * 1.05)))
    hi = max(lo + 4, min(int(fs / (nominal_line_hz * 0.95)), v.size // 4))
    seg = v[: min(v.size, 1 << 17)]
    best_lag, best_val = nom_lag, -1e30
    for lag in range(lo, hi, max(1, (hi - lo) // 200)):
        a = seg[:-lag]; b = seg[lag:]
        c = float(np.dot(a, b)) / (np.linalg.norm(a) * np.linalg.norm(b) + 1e-12)
        if c > best_val:
            best_val, best_lag = c, lag
    line_hz = float(fs / best_lag) if best_lag > 0 else nominal_line_hz
    # Pulse density at the sync-tip percentile — a clean composite should have
    # ~ one sync pulse per line period.
    sync_thresh = float(np.percentile(v, 5.0))
    below = (v < sync_thresh).astype(np.int8)
    if below.size > 1:
        edges = int(np.sum(np.diff(below) > 0))
        n_lines = max(1.0, (v.size / fs) * line_hz)
        pulses_per_line = edges / n_lines
    else:
        pulses_per_line = 0.0
    # Crude jitter: how off-nominal is best_lag vs the nominal? Cleaner sync ⇒ lower jitter.
    line_jitter = abs(best_lag - nom_lag) / max(1, nom_lag)
    score = best_val * (1.0 - min(1.0, abs(pulses_per_line - 1.0))) * (1.0 - min(1.0, line_jitter * 10))
    return {
        "line_hz": line_hz,
        "peak_corr": round(best_val, 4),
        "sync_pulses_per_line": round(pulses_per_line, 3),
        "line_jitter_pct": round(line_jitter * 100, 2),
        "score": round(max(0.0, score), 4),
    }


# ════════════════════════════════════════════════════════════════════════════
# Per-line peak-hold sync-tip / peak-white clamp.
# IIR decay τ — long τ smooths brightness flicker, short τ adapts faster.
# ════════════════════════════════════════════════════════════════════════════
def _per_line_clamp(comp: np.ndarray, fs: float, line_hz: float,
                     tau_hold_s: float = 0.30) -> np.ndarray:
    """Normalise composite per-line with an IIR-decayed min/max tracker. tau is
    in seconds; ~300 ms is a typical analog-TV TBC value. Returns a fresh
    0..1 composite ready to be sliced into rasters."""
    if comp.size == 0 or line_hz <= 0:
        return comp
    spl = max(2, int(round(fs / line_hz)))
    n_lines = comp.size // spl
    if n_lines < 2:
        # Not enough lines for per-line tracking — fall back to global percentile.
        lo = float(np.percentile(comp, 2.0)); hi = float(np.percentile(comp, 98.0))
        return np.clip((comp - lo) / (hi - lo + 1e-9), 0.0, 1.0).astype(np.float32)
    out = np.empty(n_lines * spl, dtype=np.float32)
    alpha = math.exp(-(spl / fs) / max(1e-3, tau_hold_s))   # per-line decay
    # Seed from the first line
    first = comp[:spl]
    tip = float(np.min(first)); white = float(np.max(first))
    for i in range(n_lines):
        seg = comp[i * spl : (i + 1) * spl]
        line_min = float(np.min(seg)); line_max = float(np.max(seg))
        # Tip drifts down to track new darker pulses; releases up slowly.
        tip = alpha * tip + (1 - alpha) * line_min
        white = alpha * white + (1 - alpha) * line_max
        rng = max(1e-9, white - tip)
        out[i * spl : (i + 1) * spl] = np.clip((seg - tip) / rng, 0.0, 1.0)
    # Tail — anything past the last whole line gets the last clamp.
    tail = comp[n_lines * spl :]
    if tail.size:
        rng = max(1e-9, white - tip)
        out = np.concatenate([out, np.clip((tail - tip) / rng, 0.0, 1.0).astype(np.float32)])
    return out


# ════════════════════════════════════════════════════════════════════════════
# Horizontal-sync PLL with sub-sample edge alignment.
# Returns per-line start offsets (in samples, fractional) into `comp`.
# ════════════════════════════════════════════════════════════════════════════
def _h_sync_pll(comp: np.ndarray, fs: float, line_hz: float,
                 sync_thresh_pct: float = 12.0) -> tuple[np.ndarray, float]:
    """Find sync-tip leading edges and return per-line absolute sample positions
    (subsample-precise via linear interpolation). Also returns the refined
    line period estimate (fs / median(diffs))."""
    if comp.size < 8 * int(fs / line_hz):
        return np.array([], dtype=np.float64), line_hz
    threshold = float(np.percentile(comp, sync_thresh_pct))
    below = comp < threshold
    # Find rising-edge transitions (sync-tip release) — these are the line starts.
    edges = np.where(np.diff(below.astype(np.int8)) < 0)[0]
    if edges.size < 8:
        return np.array([], dtype=np.float64), line_hz
    # Sub-sample refine via linear interpolation across the threshold crossing.
    refined = []
    for e in edges:
        if e < 0 or e + 1 >= comp.size:
            continue
        y0 = comp[e]; y1 = comp[e + 1]
        if y0 == y1:
            refined.append(float(e))
        else:
            frac = (threshold - y0) / (y1 - y0)
            refined.append(float(e) + float(np.clip(frac, 0.0, 1.0)))
    refined = np.asarray(refined, dtype=np.float64)
    if refined.size < 4:
        return refined, line_hz
    # Reject outlier edges (double-pulses, equalising pulses near V-sync) by
    # requiring the inter-edge gap to be within ±15% of the nominal line period.
    nom_spl = fs / line_hz
    diffs = np.diff(refined)
    ok = (diffs > 0.85 * nom_spl) & (diffs < 1.15 * nom_spl)
    good_idx = [0]
    for i, ok_i in enumerate(ok):
        if ok_i: good_idx.append(i + 1)
    refined = refined[good_idx]
    if refined.size >= 4:
        refined_line_hz = fs / float(np.median(np.diff(refined)))
    else:
        refined_line_hz = line_hz
    return refined, refined_line_hz


# ════════════════════════════════════════════════════════════════════════════
# Vertical-sync detection via equalising-pulse pattern.
# Returns the indices in `line_starts` where each new field/frame begins.
# ════════════════════════════════════════════════════════════════════════════
def _detect_v_sync(comp: np.ndarray, fs: float, line_hz: float,
                    line_starts: np.ndarray, lines_per_frame: int) -> np.ndarray:
    """Field boundaries are marked by ~6 equalising pulses (≈2× normal line
    rate) per field in NTSC/PAL composite. We detect this by sliding a
    short window and looking for the line where sync-pulse density doubles."""
    if line_starts.size < lines_per_frame:
        return np.array([0], dtype=np.int64)
    nom_spl = fs / line_hz
    sync_thresh = float(np.percentile(comp, 5.0))
    # For each line, count threshold-crossings — equalising-pulse intervals
    # produce ~2× the count of a normal line.
    counts = []
    for i, start in enumerate(line_starts):
        a = int(start); b = int(start + nom_spl)
        if b > comp.size: break
        seg = comp[a:b] < sync_thresh
        # rising edges within the line
        if seg.size > 1:
            counts.append(int(np.sum(np.diff(seg.astype(np.int8)) > 0)))
        else:
            counts.append(0)
    counts = np.asarray(counts)
    if counts.size == 0:
        return np.array([0], dtype=np.int64)
    # Equalising-pulse blocks are ~3-9 consecutive lines with elevated counts.
    high = counts > (np.median(counts) + 1.0)
    field_starts = [0]
    in_run = False
    for i, h in enumerate(high):
        if h and not in_run:
            in_run = True
            # The field starts just AFTER this run ends. We'll patch up below.
            field_starts.append(i)
        elif not h:
            in_run = False
    # Snap to lines_per_frame multiples if our heuristic missed; fall back to
    # nominal striding if we detected nothing.
    if len(field_starts) <= 1:
        field_starts = list(range(0, counts.size, lines_per_frame))
    return np.asarray(field_starts, dtype=np.int64)


# ════════════════════════════════════════════════════════════════════════════
# Pixel-rate / active-line resolution recovery.
# From the H-blanking interval we estimate active samples per line and derive
# an active-pixel count.
# ════════════════════════════════════════════════════════════════════════════
def _active_samples_per_line(comp: np.ndarray, line_starts: np.ndarray,
                              fs: float, line_hz: float) -> tuple[int, int]:
    """Returns (active_samples_per_line, suggested_width_px).
    H-blanking is the sync-tip region (~10.7 µs NTSC / ~12 µs PAL); active
    video is the remaining ~52 / ~52 µs. Width recovered from active duration
    × nominal pixel clock (12.27 MHz NTSC luma / 13.5 MHz CCIR-601 PAL)."""
    if line_starts.size < 4:
        spl = max(8, int(round(fs / line_hz)))
        return spl, 320
    spls = np.diff(line_starts)
    spl_median = float(np.median(spls))
    # Estimate H-blanking duration: count samples below sync threshold from line start.
    sync_thresh = float(np.percentile(comp, 8.0))
    blanking_samples = []
    for s in line_starts[: min(line_starts.size, 60)]:
        a = int(s); b = int(s + spl_median * 0.25)
        if b > comp.size: break
        seg = comp[a:b]
        below = seg < sync_thresh
        if below.size:
            # First contiguous-below run gives the sync-tip duration; everything
            # after that up to the next sync edge is part of active+back/front porch.
            run = 0
            for v in below:
                if v: run += 1
                else: break
            blanking_samples.append(run)
    if not blanking_samples:
        return int(round(spl_median)), 320
    blank_med = int(np.median(blanking_samples))
    active = max(8, int(round(spl_median - blank_med * 5.6)))  # blank ~10.7 / 63.5 ≈ 17%; we used the SYNC subset (≈4.7 µs of 63.5 → 7.4%)
    # Suggest a sane width — clamp to common active resolutions.
    if line_hz > 15000:                 # NTSC-class
        width = min(720, max(160, active))
    else:                                # PAL-class
        width = min(720, max(160, active))
    return active, int(width)


# ════════════════════════════════════════════════════════════════════════════
# Color decode — NTSC 3.579545 MHz / PAL 4.43361875 MHz chroma subcarrier.
# Quadrature-demodulate the chroma band, lock the local oscillator to the
# colour burst at the start of each line, then convert YIQ (NTSC) / YUV (PAL)
# back into RGB. Returns an (H, W, 3) uint8 array when color is recovered, or
# None when the burst can't be located (fall back to luminance-only output).
# ════════════════════════════════════════════════════════════════════════════
_COLOR_SUBCARRIER = {"ntsc": 3.579545e6, "pal": 4.43361875e6}


def _decode_color(comp_samples_2d: np.ndarray, line_starts: np.ndarray,
                   fs: float, line_hz: float, system: str,
                   width_px: int) -> Optional[np.ndarray]:
    """Returns RGB (H, W, 3) uint8 or None. `comp_samples_2d` is the raw (not
    clamped) composite stream — chroma demod needs the un-AGC'd signal."""
    fsc = _COLOR_SUBCARRIER.get(system)
    if fsc is None or fs < 4 * fsc:
        return None
    if line_starts.size < 4:
        return None
    spl = fs / line_hz
    # Sub-carrier oscillator over the entire composite (we'll slice per-line).
    t = np.arange(comp_samples_2d.size, dtype=np.float64) / fs
    cos_sc = np.cos(2 * math.pi * fsc * t).astype(np.float32)
    sin_sc = np.sin(2 * math.pi * fsc * t).astype(np.float32)
    # Chroma I/Q = composite * (cos / sin), low-passed to chroma BW (~1.3 MHz NTSC, 1.5 MHz PAL).
    chroma_bw = 1.3e6 if system == "ntsc" else 1.5e6
    chroma_cutoff = min(0.95, chroma_bw / (fs / 2))
    chroma_I = _lowpass((comp_samples_2d * cos_sc).astype(np.float32), chroma_cutoff)
    chroma_Q = _lowpass((comp_samples_2d * sin_sc).astype(np.float32), chroma_cutoff)
    # Per-line: read the colour burst (≈ samples 18–48 into each line, i.e. the back-porch),
    # measure its phase, rotate the line's I/Q by minus-that-phase.
    burst_start = int(0.018 * spl)
    burst_end = int(0.048 * spl)
    rgb_rows = []
    for li in range(line_starts.size - 1):
        a = int(line_starts[li]); b = int(line_starts[li + 1])
        if b > comp_samples_2d.size or (b - a) < 8:
            break
        i_line = chroma_I[a:b]; q_line = chroma_Q[a:b]
        # Burst-phase recovery
        if burst_end < (b - a):
            bI = float(np.mean(i_line[burst_start:burst_end]))
            bQ = float(np.mean(q_line[burst_start:burst_end]))
            phi = math.atan2(bQ, bI)
            # Burst is at -(B-Y) for NTSC. Reference phase is ±33° / ±135°.
            cos_p = math.cos(-phi); sin_p = math.sin(-phi)
            i_rot = i_line * cos_p - q_line * sin_p
            q_rot = i_line * sin_p + q_line * cos_p
            if system == "pal":
                # PAL alternates the sign of V (Q-axis) every line; tack on the swing.
                if li % 2 == 1:
                    q_rot = -q_rot
        else:
            i_rot = i_line; q_rot = q_line
        # Y is the original luma (composite low-pass), but we already have the clamped
        # composite_2d — caller will supply Y. Here we just downsample I/Q to width_px.
        xp = np.linspace(0.0, 1.0, i_rot.size)
        rgb_target_x = np.linspace(0.0, 1.0, width_px)
        if system == "ntsc":
            # NTSC: I, Q axes (already in this representation post-rotation).
            i_w = np.interp(rgb_target_x, xp, i_rot) * 1.0
            q_w = np.interp(rgb_target_x, xp, q_rot) * 1.0
            # YIQ → RGB needs a Y row (caller supplies). Return placeholder rows
            # for now and merge later.
            rgb_rows.append((i_w.astype(np.float32), q_w.astype(np.float32)))
        else:
            # PAL: U, V axes — same machinery, different coefficients.
            u_w = np.interp(rgb_target_x, xp, i_rot)
            v_w = np.interp(rgb_target_x, xp, q_rot)
            rgb_rows.append((u_w.astype(np.float32), v_w.astype(np.float32)))
    if not rgb_rows:
        return None
    # Stack into (H, W) arrays for the two chroma components.
    a = np.stack([r[0] for r in rgb_rows], axis=0)
    b = np.stack([r[1] for r in rgb_rows], axis=0)
    return np.stack([a, b], axis=-1).astype(np.float32)         # caller merges with luma


def _yiq_to_rgb(y: np.ndarray, i: np.ndarray, q: np.ndarray) -> np.ndarray:
    """NTSC YIQ → 8-bit RGB."""
    r = y + 0.956 * i + 0.621 * q
    g = y - 0.272 * i - 0.647 * q
    b = y - 1.106 * i + 1.703 * q
    rgb = np.stack([r, g, b], axis=-1)
    return (np.clip(rgb, 0.0, 1.0) * 255.0).astype(np.uint8)


def _yuv_to_rgb(y: np.ndarray, u: np.ndarray, v: np.ndarray) -> np.ndarray:
    """PAL YUV → 8-bit RGB."""
    r = y + 1.140 * v
    g = y - 0.395 * u - 0.581 * v
    b = y + 2.032 * u
    rgb = np.stack([r, g, b], axis=-1)
    return (np.clip(rgb, 0.0, 1.0) * 255.0).astype(np.uint8)


# ════════════════════════════════════════════════════════════════════════════
# Module-level frame-average accumulator (per-session).
# Keyed by detector-system pair so different decode sessions don't collide.
# ════════════════════════════════════════════════════════════════════════════
_FRAME_AVG_STATE: dict[str, dict] = {}


def _frame_average(key: str, frame: np.ndarray, n_avg: int = 4) -> np.ndarray:
    """Exponential moving average across recent frames of the same session.
    `frame` is uint8 (mono H×W or RGB H×W×3). Returns same shape/dtype."""
    if n_avg <= 1 or frame.size == 0:
        return frame
    st = _FRAME_AVG_STATE.get(key)
    f32 = frame.astype(np.float32)
    if st is None or st.get("shape") != f32.shape:
        _FRAME_AVG_STATE[key] = {"shape": f32.shape, "acc": f32.copy(), "n": 1}
        return frame
    alpha = 1.0 / min(n_avg, st["n"] + 1)
    st["acc"] = (1 - alpha) * st["acc"] + alpha * f32
    st["n"] = min(n_avg, st["n"] + 1)
    return np.clip(st["acc"], 0.0, 255.0).astype(np.uint8)


def reset_frame_average(key: str) -> None:
    _FRAME_AVG_STATE.pop(key, None)


def demod_analog_video(iq, fs: float, *, system: str = "ntsc", max_frames: int = 6,
                       width_px: int = 320,
                       try_all_detectors: bool = True,
                       use_h_sync_pll: bool = True,
                       use_v_sync_detect: bool = True,
                       use_per_line_clamp: bool = True,
                       deinterlace: bool = True,
                       frame_avg_n: int = 0,                # 0 = off; 4 is a sensible value
                       decode_color: bool = True,
                       peak_hold_tau_s: float = 0.30,
                       session_key: Optional[str] = None,
                       # operator-supplied overrides — applied on top of the
                       # estimator output if the auto-tune isn't locking.
                       line_rate_hz: Optional[float] = None,
                       frame_rate_hz: Optional[float] = None,
                       pixel_rate_hz: Optional[float] = None,
                       h_offset_samples: Optional[int] = None,
                       v_offset_lines: Optional[int] = None,
                       active_duration_s: Optional[float] = None) -> dict:
    """Full-featured analog FPV / TV video demod.

    Recovers a viewable raster from a coherent FM (NTSC/PAL/SECAM) or AM/VSB
    composite-video downlink without external software. Beyond the basic
    FM-discriminator + reslice path, this pipeline:

      - tries multiple detectors (FM polar, IQ-balanced FM, AM envelope) and
        picks the one with the strongest sync structure;
      - per-line peak-hold IIR clamp with tunable τ (default 300 ms) — replaces
        a one-shot global percentile and tracks brightness changes;
      - locks a horizontal-sync PLL to the leading edge of every sync tip
        (sub-sample precision), so lines align even if capture didn't begin at
        an edge — no rolling image;
      - detects vertical-sync via the equalising-pulse pattern, so successive
        frames are aligned and field-parity is identified;
      - recovers active samples per line from the H-blanking width, then
        resamples to a sane active resolution (still capped by `width_px`);
      - real deinterlace: if `deinterlace`, the two fields are merged into one
        progressive frame instead of returning a single field;
      - frame averaging across successive frames (EMA with `frame_avg_n`); use
        `session_key` to keep the accumulator across calls;
      - colour decode (NTSC 3.58 MHz / PAL 4.43 MHz subcarrier) with per-line
        burst-phase recovery → RGB.

    All new behaviours default to *on* but can be disabled per-call. The result
    shape preserves the old key set and adds the new diagnostics.
    """
    x = _as_c64(iq)
    sysd = _VIDEO_SYS.get(system, _VIDEO_SYS["ntsc"])
    nominal_line = sysd["line_hz"]
    lines_per_frame = int(sysd["lines"])

    # ── 1) Detector search ────────────────────────────────────────────────
    detectors: list[tuple[str, np.ndarray]] = []
    if try_all_detectors:
        try: detectors.append(("FM discriminator (polar)", _fm_discriminate(x)))
        except Exception: pass
        try: detectors.append(("FM discriminator (IQ-balanced)", _iq_balanced_fm(x)))
        except Exception: pass
        try: detectors.append(("envelope (VSB/AM)", _envelope(x)))
        except Exception: pass
    elif system == "vsb":
        detectors.append(("envelope (VSB/AM)", _envelope(x)))
    else:
        detectors.append(("FM discriminator (polar)", _fm_discriminate(x)))
    # Score each detector and pick the best.
    scored = []
    for name, comp in detectors:
        comp = _dc_block(comp.astype(np.float32))
        comp = _lowpass(comp, cutoff_frac=min(0.95, (6.0e6 / (fs / 2.0))) if fs > 12e6 else 0.95)
        sc = _detector_score(comp, fs, nominal_line) if comp.size else {"score": -1}
        scored.append((sc["score"], name, comp, sc))
    scored.sort(key=lambda t: -t[0])
    best_score, detector, comp_pre, det_metrics = scored[0]
    detector_search = [{"name": n, "score": s, **m}
                        for s, n, _, m in scored]

    # ── 2) Per-line peak-hold clamp ───────────────────────────────────────
    line_hz = det_metrics.get("line_hz", nominal_line)
    if use_per_line_clamp and comp_pre.size:
        comp = _per_line_clamp(comp_pre, fs, line_hz, tau_hold_s=peak_hold_tau_s)
    elif comp_pre.size:
        # legacy one-shot percentile clamp
        lo = float(np.percentile(comp_pre, 2.0)); hi = float(np.percentile(comp_pre, 98.0))
        comp = np.clip((comp_pre - lo) / (hi - lo + 1e-9), 0.0, 1.0).astype(np.float32)
    else:
        comp = comp_pre

    # ── 3) H-sync PLL (sub-sample line starts) ─────────────────────────────
    line_starts: np.ndarray
    # Operator override: if a line_rate_hz was forced, skip the PLL and use it.
    if line_rate_hz is not None and line_rate_hz > 0:
        line_hz = float(line_rate_hz)
        line_starts = np.array([], dtype=np.float64)
    elif use_h_sync_pll and comp.size > 8 * int(fs / line_hz):
        line_starts, line_hz = _h_sync_pll(comp, fs, line_hz)
    else:
        line_starts = np.array([], dtype=np.float64)
    spl = fs / line_hz if line_hz > 0 else fs / nominal_line
    if line_starts.size == 0 and comp.size:
        # Synthesise line starts on the nominal grid as a fallback.
        n_lines_total = int(comp.size / spl)
        line_starts = np.arange(n_lines_total, dtype=np.float64) * spl

    # Apply H-shift (operator nudges the active region horizontally).
    if h_offset_samples and line_starts.size:
        line_starts = line_starts + float(h_offset_samples)
        line_starts = line_starts[(line_starts >= 0) & (line_starts < comp.size)]

    # ── 4) Active samples + width recovery ────────────────────────────────
    active_spl, suggested_width = _active_samples_per_line(comp, line_starts, fs, line_hz)
    # Operator override: pixel-rate or active-duration forces active samples / line.
    if pixel_rate_hz is not None and pixel_rate_hz > 0 and line_hz > 0:
        active_spl = max(8, int(round(pixel_rate_hz / line_hz)))
    if active_duration_s is not None and active_duration_s > 0:
        active_spl = max(8, int(round(active_duration_s * fs)))
    effective_width = int(width_px) if width_px else max(64, active_spl)

    # ── 5) V-sync detect (field boundaries) ────────────────────────────────
    # Operator override: if frame_rate_hz is set, force a fixed lines-per-field
    # stride instead of running the v-sync detector.
    if frame_rate_hz is not None and frame_rate_hz > 0 and line_hz > 0:
        lines_per_field = max(8, int(round(line_hz / (2.0 * frame_rate_hz))))
    else:
        lines_per_field = max(8, lines_per_frame // 2)
    if frame_rate_hz is None and use_v_sync_detect and line_starts.size >= lines_per_frame:
        field_starts = _detect_v_sync(comp, fs, line_hz, line_starts, lines_per_field)
    else:
        field_starts = np.array(list(range(0, max(1, line_starts.size), lines_per_field)), dtype=np.int64)
    # Apply V-shift (operator nudges the active region vertically).
    if v_offset_lines and field_starts.size:
        field_starts = field_starts + int(v_offset_lines)
        field_starts = field_starts[(field_starts >= 0) & (field_starts < line_starts.size)]

    # ── 6) Slice into fields → deinterlace into frames ─────────────────────
    fields: list[np.ndarray] = []
    for fi in range(len(field_starts) - 1):
        ls_a = int(field_starts[fi]); ls_b = int(field_starts[fi + 1])
        rows = []
        for li in range(ls_a, min(ls_b, line_starts.size - 1)):
            a = int(line_starts[li]); b = int(line_starts[li + 1])
            if b > comp.size: break
            seg = comp[a:b]
            if seg.size < 2: continue
            xp = np.linspace(0.0, 1.0, seg.size)
            row = np.interp(np.linspace(0.0, 1.0, effective_width), xp, seg)
            rows.append(row)
        if len(rows) >= 4:
            fields.append(np.stack(rows, axis=0).astype(np.float32))
        if len(fields) >= max_frames * 2 + 2:
            break

    # Pair-merge fields into progressive frames if deinterlacing requested.
    frames: list[np.ndarray] = []
    if deinterlace and len(fields) >= 2:
        for k in range(0, len(fields) - 1, 2):
            f1 = fields[k]; f2 = fields[k + 1]
            # Pad / crop to matching row count
            h = min(f1.shape[0], f2.shape[0])
            f1 = f1[:h]; f2 = f2[:h]
            merged = np.empty((h * 2, effective_width), dtype=np.float32)
            merged[0::2] = f1; merged[1::2] = f2
            frames.append(merged)
            if len(frames) >= max_frames: break
    else:
        for f in fields[: max_frames]:
            frames.append(f)

    # ── 7) Colour decode — needs the raw composite (pre-clamp), not the
    # AGC'd version. We pick the best detector's comp_pre as the input.
    color_frames: list[np.ndarray] = []
    color_note = None
    if decode_color and system in ("ntsc", "pal") and comp_pre.size and line_starts.size > 8 and frames:
        try:
            chroma2d = _decode_color(comp_pre, line_starts, fs, line_hz, system, effective_width)
            if chroma2d is not None and chroma2d.shape[0] >= frames[0].shape[0] // 2:
                # Merge chroma into each progressive frame's luma.
                # Each progressive frame is 2× field rows; chroma is per-line at field rate.
                for fr in frames:
                    h, w = fr.shape
                    cr = chroma2d[: h // 2]
                    cr_up = np.repeat(cr, 2, axis=0)[:h]   # bob-up chroma to progressive rate
                    if cr_up.shape[1] != w:
                        # resample chroma width to match
                        target = np.linspace(0.0, 1.0, w)
                        src = np.linspace(0.0, 1.0, cr_up.shape[1])
                        cr_resized = np.stack([
                            np.stack([np.interp(target, src, cr_up[i, :, 0]),
                                       np.interp(target, src, cr_up[i, :, 1])], axis=-1)
                            for i in range(cr_up.shape[0])
                        ], axis=0)
                        cr_up = cr_resized
                    y = np.clip(fr, 0.0, 1.0)
                    if system == "ntsc":
                        rgb = _yiq_to_rgb(y, cr_up[..., 0], cr_up[..., 1])
                    else:
                        rgb = _yuv_to_rgb(y, cr_up[..., 0], cr_up[..., 1])
                    color_frames.append(rgb)
        except Exception as e:
            color_note = f"colour decode failed: {e}"

    # ── 8) Frame averaging (post-sync, post-color) ────────────────────────
    if frame_avg_n and frame_avg_n > 1:
        key = session_key or f"{detector}|{system}"
        if color_frames:
            color_frames = [_frame_average(key, fr, frame_avg_n) for fr in color_frames]
        else:
            mono_u8 = [(np.clip(fr, 0, 1) * 255.0).astype(np.uint8) for fr in frames]
            mono_u8 = [_frame_average(key, fr, frame_avg_n) for fr in mono_u8]
            frames = [u.astype(np.float32) / 255.0 for u in mono_u8]

    # ── 9) Final mono frames → 8-bit ──────────────────────────────────────
    final_mono = [(np.clip(fr, 0, 1) * 255.0).astype(np.uint8) for fr in frames]
    out_frames = color_frames if color_frames else final_mono

    # crude SNR proxy
    snr_db = None
    if comp.size:
        sig = float(np.var(comp)); nz = float(np.var(np.diff(comp))) / 2.0 + 1e-12
        snr_db = round(10.0 * math.log10(max(1e-6, sig / nz)), 1)
    return {
        "kind": "analog", "system": system, "detector": detector,
        "detector_search": detector_search,
        "pipeline": [
            f"native:detector={detector}",
            *(["native:per-line-peak-hold"] if use_per_line_clamp else ["native:global-percentile-clamp"]),
            *(["native:h-sync-pll"] if use_h_sync_pll else []),
            *(["native:v-sync-equalising-pulses"] if use_v_sync_detect else []),
            *(["native:deinterlace-field-merge"] if deinterlace else ["native:single-field"]),
            *(["native:chroma-decode"] if color_frames else []),
            *([f"native:frame-avg(n={frame_avg_n})"] if frame_avg_n > 1 else []),
        ],
        "n_samples": int(x.size), "fs_hz": float(fs),
        "line_rate_hz_est": round(float(line_hz), 1),
        "lines_per_frame": lines_per_frame,
        "samples_per_line_est": round(float(spl), 3),
        "active_samples_per_line": int(active_spl),
        "effective_width_px": int(effective_width),
        "n_fields": len(fields),
        "n_frames": len(out_frames),
        "frame_size": [(out_frames[0].shape[0] if out_frames else 0),
                        (out_frames[0].shape[1] if out_frames else 0)],
        "color_decoded": bool(color_frames),
        "color_system": system if color_frames else None,
        "peak_hold_tau_s": peak_hold_tau_s if use_per_line_clamp else None,
        "operator_overrides": {
            "line_rate_hz": line_rate_hz, "frame_rate_hz": frame_rate_hz,
            "pixel_rate_hz": pixel_rate_hz, "active_duration_s": active_duration_s,
            "h_offset_samples": h_offset_samples, "v_offset_lines": v_offset_lines,
        },
        "snr_db_est": snr_db,
        "frames": out_frames,
        "color_note": color_note,
        "fec_stage": "n/a (analog composite)",
        "note": (
            f"Analog composite recovered in-process. Detector: {detector}. "
            f"Recovered line rate {line_hz:.1f} Hz, active samples/line {active_spl}, "
            f"effective width {effective_width}px. "
            + ("Colour decoded. " if color_frames else "Luminance only (no colour burst found / unsupported system). ")
            + ("Deinterlaced. " if deinterlace and len(fields) >= 2 else "")
            + (f"Frame averaging n={frame_avg_n}. " if frame_avg_n > 1 else "")
        ),
    }


# ════════════════════════════════════════════════════════════════════════════
# 2) OFDM / COFDM
# ════════════════════════════════════════════════════════════════════════════
# standard DVB-T/T2 / ISDB-T / generic-COFDM FFT modes, plus the short-symbol OcuSync-class one
_OFDM_MODES = [(256, 64), (512, 128), (1024, 256), (2048, 512), (4096, 1024), (8192, 2048),
               (2048, 256), (8192, 1024)]


def _ofdm_mod_guess(modulation: str, feed_id: str) -> str:
    """Constellation guess from the feed's modulation string. DVB-T/T2/ISDB strings list *options*
    (QPSK/16/64/256) — the most robust (and most common) is QPSK, so default to it unless the string
    names a single higher order; only DVB-C-class single-carrier QAM names a fixed order."""
    s = (modulation or "").lower() + " " + (feed_id or "").lower()
    if "qpsk" in s or "dvbt" in s or "cofdm" in s or "isdb" in s:
        return "qpsk"
    if "256" in s:
        return "256qam"
    if "64" in s:
        return "64qam"
    if "16" in s:
        return "16qam"
    return "qpsk"


def _cp_corr_metric(x: np.ndarray, fft_len: int, cp_len: int) -> tuple[float, np.ndarray, np.ndarray, int]:
    """The accumulated cyclic-prefix autocorrelation timing metric for a candidate (fft_len, cp_len).
    Returns (peak_metric, win, accumulated_metric, search_len)."""
    sym_len = fft_len + cp_len
    n = min(x.size, 10 * sym_len)
    a = x[: n - fft_len]; b = x[fft_len: n]
    corr = a * np.conj(b)
    csum = np.concatenate([[0.0 + 0j], np.cumsum(corr)])
    win = csum[cp_len:] - csum[:-cp_len]
    energy = np.concatenate([[0.0], np.cumsum(np.abs(a) ** 2)])
    en_win = energy[cp_len:] - energy[:-cp_len] + 1e-12
    metric = np.abs(win) / en_win
    search = min(metric.size, sym_len)
    if search <= 0:
        return 0.0, win, np.zeros(0), 0
    acc = metric[:search].astype(float).copy()
    for r in range(1, max(1, metric.size // sym_len)):
        seg = metric[r * sym_len: r * sym_len + search]
        acc[:seg.size] += seg
    nrep = max(1, metric.size // sym_len)
    return float(acc.max() / nrep), win, acc, search


def demod_ofdm(iq, fs: float, *, fft_len: Optional[int] = None, cp_len: Optional[int] = None,
               mod: str = "qpsk", max_symbols: int = 400) -> dict:
    x = _normalise_power(_as_c64(iq))
    # auto-detect the OFDM mode (fft_len, cp_len) from the cyclic-prefix autocorrelation when
    # not pinned — robust whether the band is a 2k/8k DVB-T-class or a short-symbol OcuSync-class link.
    auto_mode = fft_len is None
    if auto_mode:
        best = None
        for ff, cc in _OFDM_MODES:
            if x.size < 2 * (ff + cc):
                continue
            pk, *_ = _cp_corr_metric(x, ff, cc)
            if best is None or pk > best[0]:
                best = (pk, ff, cc)
        if best is None:
            return {"kind": "ofdm", "ok": False, "reason": "not enough samples for any OFDM mode",
                    "pipeline": ["native:ofdm-demod"]}
        _, fft_len, cp_len = best
    cp_len = int(cp_len if cp_len is not None else fft_len // 4)
    sym_len = fft_len + cp_len
    if x.size < 2 * sym_len:
        return {"kind": "ofdm", "ok": False, "reason": "not enough samples for one OFDM symbol",
                "fft_len": fft_len, "cp_len": cp_len, "pipeline": ["native:ofdm-demod"]}
    # 1) coarse CFO + symbol timing: slide a CP-length correlator over one symbol period
    n = min(x.size, 8 * sym_len)
    a = x[: n - fft_len]; b = x[fft_len: n]
    corr = a * np.conj(b)
    # cumulative window sum of length cp_len
    csum = np.concatenate([[0.0 + 0j], np.cumsum(corr)])
    win = csum[cp_len:] - csum[:-cp_len]
    energy = np.cumsum(np.abs(a) ** 2)
    en_win = np.concatenate([[0.0], energy])[cp_len:] - np.concatenate([[0.0], energy])[:-cp_len] + 1e-12
    metric = np.abs(win) / en_win
    # accumulate the CP-correlation metric over every symbol period in the buffer → the true
    # timing offset (high every symbol) dominates; per-symbol spurious bumps average out.
    search = min(metric.size, sym_len)
    acc = metric[:search].astype(float).copy()
    for r in range(1, metric.size // sym_len):
        seg = metric[r * sym_len: r * sym_len + search]
        acc[:seg.size] += seg
    start = int(np.argmax(acc))
    # CFO: with carrier offset f, corr[k] = (CP-corr)·e^{-j2πf·fft_len/fs} ⇒ f = -∠win[start]·fs/(2π·fft_len)
    cfo_hz = -float(np.angle(win[start])) * fs / (2.0 * math.pi * fft_len)
    rot_per_sample = -2.0 * math.pi * cfo_hz / fs           # multiply x by e^{j·rot·n} to remove the CFO
    # 2) derotate, then FFT each symbol from the CP boundary
    nsym = int(min(max_symbols, (x.size - start) // sym_len))
    if nsym < 1:
        return {"kind": "ofdm", "ok": False, "reason": "symbol sync failed", "fft_len": fft_len,
                "cp_len": cp_len, "pipeline": ["native:ofdm-demod"]}
    deroted = x[start:] * np.exp(1j * rot_per_sample * np.arange(x.size - start))
    deroted = deroted[: nsym * sym_len].reshape(nsym, sym_len)
    body = deroted[:, cp_len:]                              # drop the cyclic prefix
    X = np.fft.fft(body, axis=1)                            # nsym × fft_len carriers
    # drop the DC carrier + the band-edge guard carriers (unused in a real OFDM frame)
    keep = np.ones(fft_len, bool); keep[0] = False
    g = int(fft_len * 0.10); keep[1:1 + g] = False; keep[-g:] = False
    kk = np.arange(fft_len, dtype=float)[keep]
    Xk = X[:, keep]                                         # nsym × n_used
    # E[X0⁴] is real-negative for QPSK/square-QAM, so mean(Xk⁴) ≈ −|E[X⁴]|·e^{j4·(residual phase)}.
    # 3a) carrier de-ramp: a residual sub-sample timing offset = a linear phase ramp across
    #     carriers. Estimate it with the (unwrap-free) first-difference estimator on the per-carrier
    #     4th-power phasor, then remove the linear phase per carrier.
    n_used = int(kk.size)
    p4 = np.mean(Xk ** 4, axis=0)
    if n_used >= 4:
        slope4 = float(np.angle(np.sum(p4[1:] * np.conj(p4[:-1]))))   # ≈ 4·ramp-rate (rad / carrier index)
        b4 = float(np.angle(np.sum(p4 * np.exp(-1j * slope4 * np.arange(n_used)))))   # ≈ 4·common-phase + π
    else:
        slope4, b4 = 0.0, float(np.angle(np.sum(p4)) if p4.size else math.pi)
    idx = np.arange(n_used, dtype=float)
    slope = slope4 / 4.0
    Xk = Xk * np.exp(-1j * ((slope4 * idx + b4 - math.pi) / 4.0))[None, :]
    # 3b) per-symbol common-phase tracking: mop up the residual CFO left after the coarse CP
    #     estimate (a slow phasor drift symbol-to-symbol) — remove ∠mean_k(Xk[s,:]⁴)/4 per symbol.
    sp4 = np.mean(Xk ** 4, axis=1)
    Xk = Xk * np.exp(-1j * ((np.angle(sp4) - math.pi) / 4.0))[:, None]
    # 3c) channel-magnitude AGC: per-carrier RMS|X_k| (a noisy |H_k| estimate over so few symbols)
    #     smoothed across carriers — a flat channel ⇒ ~constant ⇒ a single global scale (no per-carrier
    #     distortion of the QAM rings); a frequency-selective channel ⇒ the smoothed curve tracks |H_k|.
    rms = np.sqrt(np.mean(np.abs(Xk) ** 2, axis=0))
    w_sm = max(1, n_used // 24)
    if w_sm > 1:
        ker = np.ones(w_sm) / w_sm
        mag = np.convolve(np.concatenate([rms[:w_sm][::-1], rms, rms[-w_sm:][::-1]]), ker, "same")[w_sm:w_sm + n_used]
    else:
        mag = rms
    Xk = Xk / (mag[None, :] + 1e-12)
    syms = _normalise_power(_derotate_qpsk_qam(Xk.reshape(-1)))   # mop up any residual common rotation
    dec, bits, evm = _slice_constellation(syms, mod)
    byte_stream = _bits_to_bytes(bits)
    return {
        "kind": "ofdm", "ok": True,
        "pipeline": ([f"native:ofdm-mode-detect→{fft_len}/{cp_len}"] if auto_mode else []) +
                    ["native:ofdm-sync(CP-autocorr)", f"native:ofdm-fft({fft_len})",
                     "native:carrier-deramp+1-tap-eq", f"native:{mod}-demap"],
        "n_samples": int(x.size), "fs_hz": float(fs), "auto_detected_mode": bool(auto_mode),
        "fft_len": int(fft_len), "cp_len": int(cp_len), "guard_fraction": round(cp_len / fft_len, 4),
        "n_symbols": int(nsym), "used_carriers": int(keep.sum()),
        "cfo_hz_est": round(float(cfo_hz), 1), "cp_corr_metric": round(float(metric[start]), 3),
        "timing_ramp_samples_est": round(float(slope * fft_len / (2.0 * math.pi)), 3),
        "modulation": mod, "evm_pct": evm, "n_bits": int(bits.size), "byte_stream_len": len(byte_stream),
        "constellation": _constellation_snapshot(dec[: min(dec.size, 4096)]),
        "byte_stream": byte_stream,
        # per-symbol FFT (carrier order) for the DVB-T soft path; popped by the caller.
        "_X": X, "_fft_len": int(fft_len),
        "fec_stage": ("DVB-T full soft chain (pilot/TPS extract → equalise → bit/symbol "
                      "de-interleave → soft Viterbi → RS) attempted on 2K/8K FFTs; else "
                      "hard-decision flat-carrier inner+outer FEC. DVB-S2 LDPC/BCH not applied"),
    }


# ════════════════════════════════════════════════════════════════════════════
# 3) Single-carrier PSK / QAM
# ════════════════════════════════════════════════════════════════════════════
def _gardner_timing(x: np.ndarray, sps: int) -> np.ndarray:
    """Decimate ``x`` (sps samples/symbol after matched filtering) to 1 sample/symbol. Picks the
    integer sampling phase that gives the most compact constellation (max 4th-power coherence,
    |E[s⁴]| / E[|s|⁴]), then runs a one-pole Gardner timing loop from that phase to track drift."""
    sps = max(2, int(sps))
    if x.size < 8 * sps:
        return x[::sps]
    # 1) coarse: best integer phase by 4th-power compactness
    best_p, best_c = 0, -1.0
    for p in range(sps):
        s = x[p::sps]
        if s.size < 8:
            continue
        m4 = np.mean(np.abs(s) ** 4) + 1e-12
        c = abs(np.mean(s ** 4)) / m4
        if c > best_c:
            best_c, best_p = c, p
    # 2) fine: Gardner TED around that phase
    mu = float(best_p)
    out = []
    i = float(best_p) + sps
    k_gain = 0.005
    N = x.size
    while i < N - sps - 2:
        j = int(math.floor(i)); f = i - j
        on = x[j] * (1 - f) + x[j + 1] * f
        jh = int(math.floor(i - sps / 2.0)); fh = (i - sps / 2.0) - jh
        half = x[jh] * (1 - fh) + x[jh + 1] * fh if 0 <= jh < N - 1 else 0.0 + 0j
        jp = int(math.floor(i - sps)); fp = (i - sps) - jp
        prev = x[jp] * (1 - fp) + x[jp + 1] * fp if 0 <= jp < N - 1 else 0.0 + 0j
        out.append(on)
        e = (on.real - prev.real) * half.real + (on.imag - prev.imag) * half.imag
        i += sps - k_gain * e
    return np.asarray(out, np.complex64)


def _cma_equalise(sym: np.ndarray, n_taps: int = 7, mu: float = 1e-3, modulus: float = 1.0) -> np.ndarray:
    """A short complex CMA equaliser (constant-modulus) — cleans residual ISI/rotation on PSK/QAM."""
    if sym.size < 4 * n_taps:
        return sym
    w = np.zeros(n_taps, np.complex64); w[n_taps // 2] = 1.0
    out = np.empty(sym.size, np.complex64)
    pad = np.concatenate([np.zeros(n_taps // 2, np.complex64), sym, np.zeros(n_taps // 2, np.complex64)])
    R2 = modulus
    for k in range(sym.size):
        u = pad[k:k + n_taps][::-1]
        y = np.dot(w, u)
        out[k] = y
        e = y * (R2 - abs(y) ** 2)
        w = w + mu * e * np.conj(u)
    return out


def demod_single_carrier(iq, fs: float, *, symbol_rate_hz: Optional[float] = None,
                         mod: str = "qpsk", max_symbols: int = 20000) -> dict:
    x = _normalise_power(_as_c64(iq))
    if x.size < 64:
        return {"kind": "single_carrier", "ok": False, "reason": "not enough samples",
                "pipeline": ["native:sc-demod"]}
    # symbol-rate estimate from the |x|^2 spectral line (cyclostationary) if not given
    if not symbol_rate_hz:
        p = np.abs(x) ** 2; p = p - p.mean()
        P = np.abs(np.fft.rfft(p * np.hanning(p.size)))
        fr = np.fft.rfftfreq(p.size, d=1.0 / fs)
        band = (fr > fs * 0.02) & (fr < fs * 0.49)
        symbol_rate_hz = float(fr[band][np.argmax(P[band])]) if band.any() else fs / 4.0
    symbol_rate_hz = float(max(fs / 64.0, min(fs * 0.45, symbol_rate_hz)))
    sps = max(2, int(round(fs / symbol_rate_hz)))
    # RRC matched filter
    if _sps is not None:
        h = _rrc_taps(0.35, sps, span=8)
        mf = np.convolve(x, h.astype(np.complex64), mode="same")
    else:
        mf = x
    # timing recovery → 1 sample/symbol
    syms = _gardner_timing(mf, sps)
    syms = _normalise_power(syms[: max_symbols])
    # blind equalise + remove residual carrier (4th-power phase estimate for QPSK/QAM)
    syms = _cma_equalise(syms, n_taps=7, mu=2e-3, modulus=1.0)
    syms = _normalise_power(_derotate_qpsk_qam(syms))
    dec, bits, evm = _slice_constellation(syms, mod)
    byte_stream = _bits_to_bytes(bits)
    return {
        "kind": "single_carrier", "ok": True,
        "pipeline": ["native:rrc-matched-filter", "native:gardner-timing-recovery", "native:cma+dd-equaliser",
                     f"native:{mod}-slicer"],
        "n_samples": int(x.size), "fs_hz": float(fs),
        "symbol_rate_hz_est": round(symbol_rate_hz, 1), "sps": int(sps),
        "modulation": mod, "n_symbols": int(syms.size), "evm_pct": evm,
        "n_bits": int(bits.size), "byte_stream_len": len(byte_stream),
        "constellation": _constellation_snapshot(dec),
        "byte_stream": byte_stream,
        "_syms": syms,                              # equalised symbols for the DVB-S2 PL path
        "fec_stage": ("DVB-S/C: conv+Viterbi+deinterleave + RS(204,188) (hard-decision). "
                      "DVB-S2/S2X: PLFRAME sync (SOF/PLS) + PL descramble + soft-demap "
                      "(QPSK/8PSK/8/16/32/64/128/256-APSK) + bit de-interleave + BCH+LDPC, "
                      "normal (64800) & short (16200) FECFRAMEs, all S2+S2X code rates "
                      "(dvb_s2_pl.decode_dvbs2_plframe; S2X via explicit config)"),
    }


# ════════════════════════════════════════════════════════════════════════════
# top-level dispatch
# ════════════════════════════════════════════════════════════════════════════
def to_png(frame: np.ndarray) -> Optional[bytes]:
    """8-bit grayscale 2-D array → PNG bytes (or None if Pillow is unavailable)."""
    try:
        from PIL import Image
        arr = np.asarray(frame)
        if arr.ndim != 2:
            return None
        if arr.dtype != np.uint8:
            arr = np.clip(arr, 0, 255).astype(np.uint8)
        buf = io.BytesIO()
        Image.fromarray(arr, mode="L").save(buf, format="PNG")
        return buf.getvalue()
    except Exception:
        return None


def _rs_recover_ts(byte_stream: bytes) -> Optional[tuple[bytes, dict]]:
    """If the stream looks like DVB RS(204,188) packets (sync 0x47/0xB8 every 204
    bytes), apply the outer FEC: RS error-correct + energy-dispersal derandomise →
    clean 188-byte TS. Returns (ts_bytes, fec_stats) or None when it isn't 204-aligned.
    NB: the inner DVB-T convolutional/Viterbi + deinterleaver are not applied, so
    this only helps a link clean enough to reach byte-aligned 204 packets."""
    from . import dvb_fec
    # find a 204-spaced sync (0x47 or the inverted 0xB8 every 8th packet)
    for off in range(min(204, len(byte_stream))):
        if byte_stream[off] not in (0x47, 0xB8):
            continue
        if all(byte_stream[off + p * 204] in (0x47, 0xB8)
               for p in range(1, 4) if off + p * 204 < len(byte_stream)):
            usable = ((len(byte_stream) - off) // 204) * 204
            if usable < 204 * 4:
                continue
            try:
                ts, stats = dvb_fec.correct_ts_packets(byte_stream[off:off + usable])
                if ts:
                    return ts, stats
            except Exception:
                return None
    return None


def _try_dvbt_soft(X, fft_len: int, *, max_symbols: int = 96) -> Optional[dict]:
    """Full DVB-T soft receive on the per-symbol FFT (2K/8K): pilot/TPS data-cell
    extraction + pilot equalisation + bit/symbol de-interleave + soft Viterbi + RS
    (dvb_pilots.decode_dvbt_rx). Bounded to a few common configs + max_symbols so a
    blind attempt stays within a request budget — full blind detection across every
    modulation/rate/guard would read the TPS carriers (not yet decoded). None on no lock."""
    if fft_len not in (2048, 8192):
        return None
    from . import dvb_pilots, dvb_tps, video_exploit as ve
    mode = "2k" if fft_len == 2048 else "8k"
    Xs = X[:max_symbols]
    if len(Xs) < 6:
        return None
    # Read the TPS to get the exact config (constellation + code rate); decoding it
    # needs ~one 68-symbol frame. If readable, decode ONCE with that config — much
    # cheaper than brute force. Otherwise fall back to the common-config grid.
    tps = None
    try:
        tps = dvb_tps.decode_tps(Xs, mode)
    except Exception:
        tps = None
    if tps and tps.get("constellation") and tps.get("code_rate"):
        configs = [(tps["constellation"], tps["code_rate"])]
    else:
        configs = [("64qam", "2/3"), ("64qam", "3/4"), ("qpsk", "1/2"), ("qpsk", "2/3"), ("16qam", "3/4")]
    for modu, rate in configs:
        try:
            ts, info = dvb_pilots.decode_dvbt_rx(Xs, mode=mode, modulation=modu, code_rate=rate)
        except Exception:
            ts, info = None, {}
        if not ts:
            continue
        dm = ve.demux_ts(ts)
        klv = ve.extract_klv_track(ts)
        if dm.get("streams") or klv:
            return {"ts_sync": True, "streams": dm.get("streams"), "pat": dm.get("pat"),
                    "video_codecs": dm.get("video_codecs"), "klv_pid": dm.get("klv_pid"),
                    "klv_units": len(klv),
                    "dvbt_soft": {"mode": mode, "modulation": modu, "code_rate": rate,
                                  "config_from_tps": tps is not None,
                                  **({"tps": tps} if tps else {}), **info}}
    return None


def _try_dvbs2(syms) -> Optional[dict]:
    """DVB-S2/S2X: locate a PLFRAME (SOF), read the PLS config, descramble + soft-demap
    (QPSK/8PSK/8/16/32/64/128/256-APSK) + BCH+LDPC-decode it (dvb_s2_pl, normal & short
    FECFRAMEs, all S2+S2X code rates), and demux the recovered BBFRAME as TS. None on no
    lock. (Blind PLS = DVB-S2 MODCODs; S2X formats decode via explicit config.)"""
    if syms is None or len(syms) < 90 + 1000:
        return None
    from . import dvb_s2_pl, video_exploit as ve
    try:
        bb, info = dvb_s2_pl.decode_dvbs2_plframe(syms)
    except Exception:
        return None
    if bb is None:
        return None
    ts = np.packbits(np.asarray(bb, dtype=np.uint8)).tobytes()
    dm = ve.demux_ts(ts)
    klv = ve.extract_klv_track(ts)
    if dm.get("streams") or klv:
        return {"ts_sync": True, "streams": dm.get("streams"), "pat": dm.get("pat"),
                "video_codecs": dm.get("video_codecs"), "klv_pid": dm.get("klv_pid"),
                "klv_units": len(klv), "dvbs2": info}
    return {"ts_sync": False, "note": "DVB-S2 PLFRAME decoded but BBFRAME not TS-like", "dvbs2": info}


def _try_inner_fec_ts(byte_stream: bytes, *, max_bits: int = 300_000) -> Optional[dict]:
    """Full DVB-T inner FEC fallback: treat the recovered (hard) bits as the channel
    stream, run depuncture → Viterbi → deinterleave → RS → derandomise across the
    standard code rates, and return a TS summary if one locks. Hard bits are mapped
    to ±1 soft values (≈2 dB worse than true soft, but the chain is identical).
    Bounded to ``max_bits`` so a long capture stays within a request budget."""
    if not byte_stream:
        return None
    from . import dvb_inner_fec as inner
    from . import video_exploit as ve
    bits = np.unpackbits(np.frombuffer(byte_stream, dtype=np.uint8))[:max_bits]
    soft = (1.0 - 2.0 * bits.astype(np.float64))      # 0→+1, 1→−1
    for rate in ("2/3", "3/4", "1/2", "5/6", "7/8"):  # most-common DVB-T rates first
        try:
            ts, stats = inner.decode_dvbt(soft, rate)
        except Exception:
            ts = None; stats = {}
        if not ts:
            continue
        dm = ve.demux_ts(ts)
        klv = ve.extract_klv_track(ts)
        if dm.get("streams") or klv:
            return {"ts_sync": True, "streams": dm.get("streams"), "pat": dm.get("pat"),
                    "video_codecs": dm.get("video_codecs"), "klv_pid": dm.get("klv_pid"),
                    "klv_units": len(klv),
                    "inner_fec": {"applied": f"conv K=7 (171/133) {rate} + Viterbi + I=12/M=17 deinterleave",
                                  **stats}}
    return None


def _try_ts(byte_stream: bytes) -> Optional[dict]:
    """Hand a recovered byte stream to the MPEG-TS demux (PAT/PMT + STANAG-4609 KLV).
    Returns a compact summary, or None if nothing TS-like is in it."""
    if not byte_stream or len(byte_stream) < 188 * 4:
        return None
    try:
        from . import video_exploit as ve
        dm = ve.demux_ts(byte_stream)
        klv = ve.extract_klv_track(byte_stream)
        n_streams = len(dm.get("streams") or [])
        fec_stats = None
        # No clean 188-byte TS sync? Try the DVB outer FEC on 204-byte RS packets.
        if n_streams == 0 and not klv:
            rec = _rs_recover_ts(byte_stream)
            if rec is not None:
                ts, fec_stats = rec
                dm = ve.demux_ts(ts)
                klv = ve.extract_klv_track(ts)
                n_streams = len(dm.get("streams") or [])
        if n_streams == 0 and not klv:
            return {"ts_sync": False, "note": "no TS sync found in the recovered byte stream "
                                               "(inner+outer FEC attempted; expected on an idle/noise-only or "
                                               "DVB-S2-LDPC capture, or a link too weak for hard-decision Viterbi)"}
        out = {
            "ts_sync": True, "streams": dm.get("streams"), "pat": dm.get("pat"),
            "video_codecs": dm.get("video_codecs"), "klv_pid": dm.get("klv_pid"),
            "klv_units": len(klv),
        }
        if fec_stats:
            out["outer_fec"] = {"applied": "RS(204,188)+derandomise", **fec_stats}
        return out
    except Exception as e:  # pragma: no cover
        return {"ts_sync": False, "error": str(e)}


# modulation string → which analog system
def _analog_system_for(feed_id: str) -> str:
    f = (feed_id or "").lower()
    if "pal" in f:
        return "pal"
    if "secam" in f:
        return "secam"
    if "vsb" in f:
        return "vsb"
    return "ntsc"


def decode_feed(feed: dict, iq, fs: float, *, max_frames: int = 6,
                 analog_options: Optional[dict] = None) -> dict:
    """Demodulate ``iq`` (at sample rate ``fs``) for a UAS-video ``feed`` (a row from
    ``uas_video.FEED_TYPES``). Dispatches on the feed's transport/modulation. Always returns a
    dict (never raises) — on a demod fault it returns ``{"ok": False, "error": ...}`` with the
    pipeline it attempted.

    ``analog_options`` forwards the new analog-video knobs (multi-detector,
    H/V-sync PLL, deinterlace, frame averaging, peak-hold τ, colour decode,
    width override, system override, session_key) to ``demod_analog_video``.
    """
    feed = feed or {}
    fid = feed.get("id", "")
    transport = feed.get("transport", "")
    modulation = feed.get("modulation", "")
    bw_hz = float((feed.get("typical_bandwidth_hz") or [8e6])[0] or 8e6)
    aopts = dict(analog_options or {})
    aopts.setdefault("system", _analog_system_for(fid))
    aopts.setdefault("max_frames", max_frames)
    try:
        x = _as_c64(iq)
        if x.size < 256:
            return {"ok": False, "kind": "none", "error": "no IQ samples to demodulate",
                    "pipeline": ["native:capture"]}
        if transport == "composite_analog":
            r = demod_analog_video(x, fs, **aopts)
            r["ok"] = True
            return r
        # digital MPEG-TS carriers
        is_ofdm = any(t in (modulation + " " + fid).lower() for t in ("cofdm", "ofdm", "dvbt", "isdb"))
        if transport == "mpeg_ts" and is_ofdm:
            r = demod_ofdm(x, fs, fft_len=None, mod=_ofdm_mod_guess(modulation, fid))   # auto-detect the OFDM mode
            if r.get("ok"):
                bs = r.pop("byte_stream", b"")
                X = r.pop("_X", None)
                fft_len = r.pop("_fft_len", 0)
                ts = _try_ts(bs)
                # DVB-T: prefer the full soft chain (pilot/TPS extract → soft Viterbi);
                # fall back to the hard-decision flat-carrier inner FEC, then plain TS.
                if not (ts or {}).get("ts_sync") and X is not None:
                    ts = _try_dvbt_soft(X, fft_len) or ts
                if not (ts or {}).get("ts_sync"):
                    ts = _try_inner_fec_ts(bs) or ts
                r["ts"] = ts
            else:
                r.pop("byte_stream", None); r.pop("_X", None); r.pop("_fft_len", None)
            return r
        if transport == "mpeg_ts":  # single-carrier DVB-S/S2/DVB-C-class
            # DVB-S/S2 ACM strings list QPSK/8PSK/16/32APSK — the common short-frame / robust mode is
            # QPSK, so default to it (a higher order would be confirmed by the inner FEC, not the PHY).
            ms = (modulation or "").lower()
            m = "16qam" if ("16-qam" in ms or "16 qam" in ms or "16qam" in ms) and "qpsk" not in ms else "qpsk"
            r = demod_single_carrier(x, fs, mod=m)
            if r.get("ok"):
                bs = r.pop("byte_stream", b"")
                syms = r.pop("_syms", None)
                ts = _try_ts(bs)
                # DVB-S2: PLFRAME sync + PL descramble + BCH/LDPC (short-frame QPSK).
                if not (ts or {}).get("ts_sync"):
                    ts = _try_dvbs2(syms) or ts
                # DVB-S/DVB-C share the K=7 conv + RS(204,188) + I=12 interleaver (not S2/LDPC).
                if not (ts or {}).get("ts_sync") and "s2" not in (fid + " " + ms).lower():
                    ts = _try_inner_fec_ts(bs) or ts
                r["ts"] = ts
            else:
                r.pop("byte_stream", None); r.pop("_syms", None)
            return r
        if transport == "unknown":  # analog-looking unknown → try the analog path
            aopts2 = dict(aopts); aopts2["system"] = aopts.get("system", "ntsc")
            r = demod_analog_video(x, fs, **aopts2)
            r["ok"] = True
            r["note"] = "unidentified carrier — ran the analog raster demod speculatively"
            return r
        return {"ok": False, "kind": "none", "error": f"no native demod for transport {transport!r}",
                "pipeline": []}
    except Exception as e:  # never let a demod fault escape
        return {"ok": False, "kind": "error", "error": f"{type(e).__name__}: {e}",
                "pipeline": ["native:demod"]}
