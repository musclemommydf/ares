# SPDX-License-Identifier: MIT OR Apache-2.0
# Copyright (c) 2026 Ares

"""
vfo.py — multi-VFO digital down-conversion + per-VFO squelch for live DF.

A *VFO* (virtual frequency oscillator) is a narrowband channel carved out of one
wideband coherent capture: mix the band down by the VFO's offset, low-pass to its
bandwidth, and you have an isolated per-channel IQ block to direction-find. One
wide tune (e.g. a Pluto at 10 MSPS) then yields bearings on many signals at once,
the way KrakenSDR's multi-VFO mode does — except here it feeds Ares's own DF
solvers (MUSIC/Capon/Watson-Watt/correlative/…).

The down-conversion applies the *same* complex mixer + filter taps to every
antenna channel, so inter-channel phase coherence — the thing DF lives or dies on
— is preserved exactly.

``SquelchTracker`` learns a per-array noise floor from the stream of channel
powers and only lets a VFO through when it is genuinely active, so dead channels
don't burn DF cycles or mint phantom bearings.
"""
from __future__ import annotations

from collections import deque

import numpy as np


def _lowpass_taps(ntaps: int, fc: float) -> np.ndarray:
    """Windowed-sinc low-pass, cutoff ``fc`` in cycles/sample (0…0.5)."""
    ntaps = max(5, int(ntaps) | 1)                     # force odd for a symmetric filter
    n = np.arange(ntaps) - (ntaps - 1) / 2.0
    h = 2.0 * fc * np.sinc(2.0 * fc * n)
    w = 0.54 - 0.46 * np.cos(2.0 * np.pi * np.arange(ntaps) / (ntaps - 1))   # Hamming
    h = h * w
    return (h / np.sum(h)).astype(np.complex64)


def ddc(X: np.ndarray, fs_hz: float, offset_hz: float, bandwidth_hz: float,
        *, decim: int = 1, ntaps: int = 129) -> np.ndarray:
    """Digital down-convert a coherent block to one VFO.

    ``X`` is (channels, K) complex at sample rate ``fs_hz``. Returns the
    isolated, coherence-preserving (channels, K') block centred on ``offset_hz``
    and band-limited to ``bandwidth_hz``. A bandwidth ≥ the capture rate is a
    no-op filter (the whole band)."""
    X = np.asarray(X, dtype=np.complex64)
    if X.ndim == 1:
        X = X[None, :]
    ch, K = X.shape
    if offset_hz:
        mix = np.exp(-2j * np.pi * (float(offset_hz) / fs_hz) * np.arange(K)).astype(np.complex64)
        X = X * mix[None, :]
    if bandwidth_hz and bandwidth_hz < fs_hz * 0.98:
        fc = float(np.clip((bandwidth_hz / 2.0) / fs_hz, 1e-4, 0.49))
        taps = _lowpass_taps(min(ntaps, max(5, (K // 2) | 1)), fc)
        X = np.stack([np.convolve(X[c], taps, mode="same") for c in range(ch)], axis=0)
    if decim and decim > 1:
        X = X[:, ::int(decim)]
    return X.astype(np.complex64)


def power_dbfs(X: np.ndarray) -> float:
    """Mean power of a block in dB relative to unit full-scale."""
    return float(10.0 * np.log10(float(np.mean(np.abs(X) ** 2)) + 1e-12))


class SquelchTracker:
    """Learns a noise floor from a rolling window of channel-power samples and
    gates VFOs against it. With several VFOs in a band (some empty) the low
    percentile of the pooled powers tracks the true floor; a manual threshold
    overrides the learned one."""

    def __init__(self, margin_db: float = 8.0, window: int = 300, percentile: float = 10.0,
                 warmup: int = 8):
        self.margin_db = float(margin_db)
        self.percentile = float(percentile)
        self.warmup = int(warmup)
        self._hist: deque[float] = deque(maxlen=int(window))
        self.floor_db: float = -120.0

    def observe(self, powers_db) -> None:
        for p in (powers_db if hasattr(powers_db, "__iter__") else [powers_db]):
            if np.isfinite(p):
                self._hist.append(float(p))
        if self._hist:
            self.floor_db = float(np.percentile(np.fromiter(self._hist, float), self.percentile))

    def threshold_db(self, manual_db=None) -> float:
        return float(manual_db) if manual_db is not None else (self.floor_db + self.margin_db)

    def is_open(self, power_db: float, manual_db=None) -> bool:
        # warm-up: until the floor is learned, pass everything (a manual squelch
        # is always honoured immediately).
        if manual_db is None and len(self._hist) < self.warmup:
            return True
        return power_db >= self.threshold_db(manual_db)
