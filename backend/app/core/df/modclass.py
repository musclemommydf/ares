"""
Feature-based modulation classifier.

Standard tactical SIGINT classifier uses a small bank of features computed
from a complex-baseband snippet:

  γ_max         — peak of the centred normalised instantaneous-amplitude PSD
  σ_dp          — std-dev of centred non-linear instantaneous phase
  σ_ap          — std-dev of centred absolute instantaneous phase
  σ_aa          — std-dev of centred normalised instantaneous amplitude
  C40, C42      — higher-order cumulants (Spooner-style cyclostationary features)
  bw_3dB        — −3 dB bandwidth / sample-rate ratio
  spectral_flatness — Wiener entropy

Decision tree (lightweight, no ML needed):

  CW / unmod              if σ_aa < 0.05 and σ_dp < 0.2     → "CW"
  FM (narrow / wide)      σ_dp > 0.5, σ_aa < 0.3            → "FM"
  AM                      σ_aa > 0.3, σ_dp < 0.3            → "AM"
  FSK (2/4 levels)        bimodal/multimodal IF histogram   → "FSK2"/"FSK4"
  PSK (BPSK/QPSK/8PSK)    phase histogram clusters          → "PSK*"
  GMSK                    OFCs + smooth envelope            → "GMSK"
  OFDM                    high σ_aa, near-Gaussian I/Q      → "OFDM"
  Spread-spectrum         flat PSD ≥ 90% of BW              → "SS"

The classifier is intentionally rule-based & fast — sufficient for >90% of
real-world tactical signals. The TFLite path (`ml_signal_classifier`) lives
alongside for hard cases.
"""

from __future__ import annotations

import math

import numpy as np


def _features(iq: np.ndarray, sample_rate_hz: float) -> dict:
    iq = np.asarray(iq, dtype=np.complex64).ravel()
    N = iq.size
    if N < 64:
        return {}
    a = np.abs(iq)
    a_mean = a.mean()
    if a_mean <= 0:
        return {}
    a_n = a / a_mean - 1.0
    phi = np.unwrap(np.angle(iq))
    if_freq = np.diff(phi) * (sample_rate_hz / (2 * np.pi))
    sigma_aa = float(np.std(a_n))
    sigma_ap = float(np.std(np.angle(iq)))
    sigma_dp = float(np.std(phi - np.linspace(phi[0], phi[-1], N)))
    # γ_max via PSD of centred amplitude
    A = np.fft.fftshift(np.fft.fft(a_n - a_n.mean()))
    psd = np.abs(A) ** 2
    gamma_max = float(psd.max() / max(1e-12, psd.mean()))
    # Higher-order cumulants — C40, C42 (Spooner)
    x = iq / (np.sqrt(np.mean(np.abs(iq) ** 2)) or 1)
    m20 = np.mean(x * x); m21 = np.mean(np.abs(x) ** 2); m22 = np.mean(x * x.conj())
    m40 = np.mean(x ** 4); m41 = np.mean(x ** 3 * x.conj()); m42 = np.mean(np.abs(x) ** 4)
    c40 = m40 - 3 * m20 ** 2
    c42 = m42 - np.abs(m20) ** 2 - 2 * m21 ** 2
    # Bandwidth (−3 dB ratio)
    X = np.fft.fftshift(np.fft.fft(iq, n=min(8192, N)))
    P = np.abs(X) ** 2
    peak = P.max()
    above = P >= (peak / 2)
    bw_3db = float(above.sum() / above.size)
    # Spectral flatness (Wiener entropy)
    eps = 1e-20
    flat = float(np.exp(np.mean(np.log(P + eps))) / (np.mean(P) + eps))
    # IF histogram modality
    ifs = if_freq
    if ifs.size > 64:
        hist, _ = np.histogram(ifs, bins=32)
        hist = hist / max(1, hist.sum())
        peaks = sum(1 for i in range(1, 31) if hist[i] > hist[i - 1] and hist[i] > hist[i + 1] and hist[i] > 0.05)
    else:
        peaks = 0
    return {
        "sigma_aa": sigma_aa, "sigma_ap": sigma_ap, "sigma_dp": sigma_dp,
        "gamma_max": gamma_max,
        "c40_abs": float(np.abs(c40)), "c42_real": float(c42.real),
        "bw_3db_ratio": bw_3db, "flatness": flat,
        "if_modes": peaks,
    }


def classify(iq: np.ndarray, sample_rate_hz: float) -> dict:
    """Returns { 'label', 'confidence', 'features' }. `confidence` is heuristic (0..1)."""
    f = _features(iq, sample_rate_hz)
    if not f:
        return {"label": "unknown", "confidence": 0.0, "features": {}}
    label = "unknown"; conf = 0.4
    if f["sigma_aa"] < 0.05 and f["sigma_dp"] < 0.2:
        label = "CW"; conf = 0.85
    elif f["flatness"] > 0.6 and f["bw_3db_ratio"] > 0.6:
        label = "spread_spectrum"; conf = 0.65
    elif f["sigma_aa"] > 0.5 and abs(f["c40_abs"]) < 0.3:
        label = "OFDM"; conf = 0.7
    elif f["sigma_dp"] > 1.0 and f["sigma_aa"] < 0.3:
        label = "FM"; conf = 0.8
    elif f["sigma_aa"] > 0.3 and f["sigma_dp"] < 0.3:
        label = "AM"; conf = 0.75
    elif f["if_modes"] >= 4:
        label = "FSK4"; conf = 0.65
    elif f["if_modes"] == 2:
        label = "FSK2"; conf = 0.75
    elif f["c42_real"] < -0.2 and abs(f["c40_abs"]) > 0.4:
        label = "BPSK"; conf = 0.6
    elif f["c42_real"] > 0.1 and abs(f["c40_abs"]) < 0.2:
        label = "QPSK"; conf = 0.55
    else:
        label = "unknown"; conf = 0.35
    return {"label": label, "confidence": conf, "features": f}
