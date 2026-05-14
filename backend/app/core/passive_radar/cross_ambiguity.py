"""
Passive bistatic radar (PBR) — cross-ambiguity function over a reference and
surveillance IQ stream. Returns a range-Doppler map.

Math (Kraken's krakensdr_pr / gr-radar reference):

    χ(τ, ν) = Σ_n  s_ref(n) · s_surv*(n + τ) · e^{-j2π ν n / fs}

Operationally:
  - `s_ref`  — reference channel pointed at the illuminator (DAB, DVB-T, FM
                broadcast tower).
  - `s_surv` — surveillance channel pointed at the volume of interest;
                receives the direct-path reference *plus* delayed/Doppler-
                shifted reflections from moving targets.
  - The cross-correlation in time gives bistatic range; the Doppler dimension
    (FFT over slow time) gives target radial velocity.

This implementation uses the FFT-based decimation (Coherent Processing
Interval × Doppler bins) — standard for offline analysis on a laptop. For
realtime on a Pi 4 you'd want the same block in the existing FFT loop on the
GPU; that optimisation is deferred to the realtime path in the SDR manager.
"""

from __future__ import annotations

import math
from typing import Optional

import numpy as np


def clutter_filter_extended_cancel(s_ref: np.ndarray, s_surv: np.ndarray,
                                   filter_taps: int = 64) -> np.ndarray:
    """Least-squares direct-path clutter cancellation (the "Extensive
    Cancellation Algorithm"). Removes the bright reference-direct-path leak
    from the surveillance channel so weak target returns are visible."""
    N = min(len(s_ref), len(s_surv))
    s_ref = s_ref[:N]; s_surv = s_surv[:N]
    # Toeplitz matrix of reference delays
    L = filter_taps
    if N < 2 * L:
        return s_surv                                       # not enough samples; pass-through
    M = N - L + 1
    X = np.empty((M, L), dtype=np.complex128)
    for k in range(L):
        X[:, k] = s_ref[L - 1 - k : L - 1 - k + M]
    y = s_surv[L - 1 : L - 1 + M]
    # Least-squares clutter filter coefficients
    w, *_ = np.linalg.lstsq(X, y, rcond=None)
    cancelled = y - X @ w
    out = np.empty(N, dtype=np.complex128)
    out[: L - 1] = s_surv[: L - 1]
    out[L - 1 :] = cancelled
    return out


def cross_ambiguity(s_ref: np.ndarray, s_surv: np.ndarray,
                    sample_rate_hz: float,
                    max_range_km: float = 30.0,
                    max_doppler_hz: float = 200.0,
                    n_doppler: int = 256) -> dict:
    """Range-Doppler map.

    Returns:
        { "range_m":       [...],     # range bins, in metres (one-way reference geometry)
          "doppler_hz":    [...],     # Doppler bins
          "rd_db":         [[...]],   # |χ|² in dB, shape (n_range, n_doppler)
          "max_db":        float,
          "peak":          {"range_m", "doppler_hz", "snr_db"} }
    """
    s_ref = np.asarray(s_ref, dtype=np.complex128).ravel()
    s_surv = np.asarray(s_surv, dtype=np.complex128).ravel()
    if s_ref.size != s_surv.size:
        n = min(s_ref.size, s_surv.size)
        s_ref = s_ref[:n]; s_surv = s_surv[:n]
    # Number of range bins from max_range
    c = 299_792_458.0
    max_tau_s = (max_range_km * 1000.0) / c
    n_range = max(2, int(math.ceil(max_tau_s * sample_rate_hz)) + 1)
    # Pre-FFT decimate slow time
    n_slow = max(64, int(s_ref.size // max(1, n_doppler)))
    # Build the lag-by-lag cross-correlation, FFT'd over slow time.
    rd = np.zeros((n_range, n_doppler), dtype=np.complex128)
    for k in range(n_range):
        if k >= s_ref.size:
            break
        # Aligned slices (zero-pad implicitly via slicing)
        a = s_ref[: s_ref.size - k]
        b = np.conj(s_surv[k : k + a.size])
        prod = a * b
        # Decimate the product into Doppler bins, then FFT.
        trim = (prod.size // n_doppler) * n_doppler
        if trim == 0:
            continue
        block = prod[:trim].reshape(n_doppler, trim // n_doppler).mean(axis=1)
        rd[k, :] = np.fft.fftshift(np.fft.fft(block, n=n_doppler))
    mag = np.abs(rd) ** 2
    mag_db = 10 * np.log10(np.maximum(mag, 1e-30))
    # Peak (best target return — note (0,0) is the direct path; suppress it)
    suppressed = mag_db.copy()
    # Mask zero-Doppler ± 1 bin and zero-range ± 1 bin (direct path).
    zr = 1
    suppressed[: zr, :] = -np.inf
    suppressed[:, n_doppler // 2 - 1 : n_doppler // 2 + 2] = -np.inf
    ridx, didx = np.unravel_index(np.argmax(suppressed), suppressed.shape)
    range_axis = np.arange(n_range) * (c / sample_rate_hz)
    doppler_axis = np.fft.fftshift(np.fft.fftfreq(n_doppler, d=1.0 / (sample_rate_hz / max(1, s_ref.size // n_doppler))))
    # Restrict doppler axis to the operator-requested window when narrower.
    if max_doppler_hz > 0:
        mask = np.abs(doppler_axis) <= max_doppler_hz
        if mask.any():
            doppler_axis = doppler_axis[mask]
            mag_db = mag_db[:, mask]
            suppressed = suppressed[:, mask]
            ridx, didx = np.unravel_index(np.argmax(suppressed), suppressed.shape)
    noise = float(np.percentile(mag_db, 50))
    peak_db = float(mag_db[ridx, didx])
    return {
        "range_m": range_axis.tolist(),
        "doppler_hz": doppler_axis.tolist(),
        "rd_db": mag_db.tolist(),
        "max_db": float(mag_db.max()),
        "peak": {
            "range_m": float(range_axis[ridx]),
            "doppler_hz": float(doppler_axis[didx]),
            "snr_db": peak_db - noise,
            "power_db": peak_db,
        },
        "noise_floor_db": noise,
    }
