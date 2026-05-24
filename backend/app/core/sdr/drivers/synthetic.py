# SPDX-License-Identifier: MIT OR Apache-2.0
# Copyright (c) 2026 Ares

"""
Synthetic SDR driver — generates coherent multi-channel IQ for a list of
phantom emitters at known DoAs. The whole DF pipeline can be exercised without
any hardware attached. Used by tests, demos, and the "no SDR detected" UX
fallback so the app does something useful out of the box.

Configure phantom emitters via env or the registry API:
    sdr.set_synthetic_emitters([
        {"az_deg": 45, "freq_hz": 433.92e6, "snr_db": 20},
        {"az_deg": 200, "freq_hz": 433.92e6, "snr_db": 15},
    ])
"""

from __future__ import annotations

import collections
import math
import threading
import time
from typing import Optional

import numpy as np

from ...df.arrays import ArrayGeometry, steering_vector
from .base import DriverCapabilities, IqFrame, SdrDriver


class SyntheticDriver(SdrDriver):
    capabilities = DriverCapabilities(
        name="Synthetic coherent array",
        driver_id="synthetic",
        coherent=True,
        max_channels=8,
        sample_rate_range_hz=(0.05e6, 50e6),
        tunable_range_hz=(0, 6e9),
        gain_range_db=(0, 50),
        iq_capture=True,
        tx_capable=True,                  # transmit loops back into RX (NIC demo/tests)
        cal_source=True,                  # emulates a switchable noise source for auto-cal
        notes="Phantom emitters at fixed DoAs, with an emulated per-channel gain/phase "
              "drift + switchable noise source so auto-calibration is demonstrable offline. "
              "transmit() loops back into read_iq for the SDR-as-NIC path. Dev & demo.",
    )

    def __init__(self, *, channels: int = 5, array: Optional[ArrayGeometry] = None):
        # Default: KrakenSDR 5-element UCA at λ/2 for ~433 MHz (radius ≈ 0.06 m)
        self.array = array or ArrayGeometry.uca(channels, radius_m=0.058)
        self.center_hz = 433.92e6
        self.sample_rate = 2.4e6
        self.gain_db = 30.0
        self.emitters: list[dict] = [
            {"az_deg": 67.0, "freq_offset_hz": 0.0, "snr_db": 22.0},
        ]
        self._seq = 0
        self._open = False
        # Emulated coherent-array imperfection: a fixed per-channel complex gain
        # (phase + amplitude) the array has "drifted" by, plus a switchable noise
        # source. read_iq corrupts the phantom response with this drift; the
        # calibration loop switches the source on, measures it, and corrects it —
        # so auto-calibration produces a visible accuracy gain with no hardware.
        self._chan_error = None           # per-channel complex drift (lazy, sized to the array)
        self._cal_source_on = False
        # NIC loopback: transmit() queues baseband here, read_iq plays it back so
        # the modem + framing round-trip end-to-end with no radio attached.
        self._loopback: collections.deque[np.ndarray] = collections.deque()
        self._loopback_lock = threading.Lock()
        self._loopback_max = 1 << 22          # cap queued samples (~4M)

    def open(self) -> None: self._open = True
    def close(self) -> None:
        self._open = False
        with self._loopback_lock:
            self._loopback.clear()

    def transmit(self, samples: np.ndarray) -> None:
        """Queue baseband IQ to be returned by the next read_iq calls (loopback)."""
        s = np.asarray(samples, dtype=np.complex64).ravel()
        if s.size == 0:
            return
        with self._loopback_lock:
            queued = sum(c.size for c in self._loopback)
            if queued < self._loopback_max:
                self._loopback.append(s)

    def _drain_loopback(self, n: int) -> Optional[np.ndarray]:
        """Pop up to n samples of queued TX; None if nothing is queued."""
        with self._loopback_lock:
            if not self._loopback:
                return None
            out = np.zeros(n, dtype=np.complex64)
            filled = 0
            while self._loopback and filled < n:
                chunk = self._loopback[0]
                take = min(chunk.size, n - filled)
                out[filled:filled + take] = chunk[:take]
                if take == chunk.size:
                    self._loopback.popleft()
                else:
                    self._loopback[0] = chunk[take:]
                filled += take
        return out
    def set_frequency(self, freq_hz: float) -> None: self.center_hz = float(freq_hz)
    def set_sample_rate(self, rate_hz: float) -> None: self.sample_rate = float(rate_hz)
    def set_gain(self, gain_db: float) -> None: self.gain_db = float(gain_db)

    def set_emitters(self, emitters: list[dict]) -> None:
        self.emitters = list(emitters or [])

    def set_calibration_source(self, on: bool) -> None:
        """Emulate switching a coherent noise source onto every element."""
        self._cal_source_on = bool(on)

    def _chan_err(self, M: int) -> np.ndarray:
        """Fixed per-channel complex drift (deterministic), ref channel = 1."""
        if self._chan_error is None or self._chan_error.shape[0] != M:
            rng = np.random.default_rng(0xA12E5)
            ph = np.radians(rng.uniform(-12.0, 12.0, M)); ph[0] = 0.0
            amp = 10 ** (rng.uniform(-0.4, 0.4, M) / 20.0); amp[0] = 1.0
            self._chan_error = (amp * np.exp(1j * ph)).astype(np.complex64)
        return self._chan_error

    def _read_iq_impl(self, n_samples: int) -> IqFrame:
        if not self._open:
            self.open()
        M = self.array.n
        cerr = self._chan_err(M)
        # Calibration source: a common reference into every element, scaled by the
        # per-channel drift — what the auto-cal loop measures to derive the fix.
        if self._cal_source_on:
            ref = (np.random.standard_normal(n_samples) + 1j * np.random.standard_normal(n_samples)).astype(np.complex64)
            noise = (np.random.standard_normal((M, n_samples)) + 1j * np.random.standard_normal((M, n_samples))).astype(np.complex64) / math.sqrt(2)
            x = (cerr[:, None] * ref[None, :] * np.complex64(8.0)) + noise
            self._seq += 1
            return IqFrame(samples=x.astype(np.complex64), sample_rate_hz=self.sample_rate,
                           center_freq_hz=self.center_hz, capture_time_ns=time.time_ns(),
                           channels=M, gain_db=self.gain_db, sequence=self._seq,
                           metadata={"driver": "synthetic", "cal_source": True})
        # NIC loopback path: when transmit() has queued baseband, return it (on
        # every channel) over a light AWGN floor instead of the phantom emitters,
        # so the NIC's receiver demodulates exactly what its transmitter sent.
        lb = self._drain_loopback(n_samples)
        if lb is not None:
            noise = (np.random.standard_normal((M, n_samples))
                     + 1j * np.random.standard_normal((M, n_samples))).astype(np.complex64)
            noise *= np.complex64(0.05 / math.sqrt(2))
            x = np.tile(lb, (M, 1)).astype(np.complex64) + noise
            self._seq += 1
            return IqFrame(
                samples=x, sample_rate_hz=self.sample_rate, center_freq_hz=self.center_hz,
                capture_time_ns=time.time_ns(), channels=M, gain_db=self.gain_db,
                sequence=self._seq, metadata={"driver": "synthetic", "loopback": True},
            )
        t = np.arange(n_samples, dtype=float) / self.sample_rate
        x = np.zeros((M, n_samples), dtype=np.complex64)
        for em in self.emitters:
            az = float(em["az_deg"])
            df = float(em.get("freq_offset_hz", 0.0))
            snr = float(em.get("snr_db", 20.0))
            amp = 10 ** (snr / 20.0)
            a = steering_vector(self.array, self.center_hz, az).astype(np.complex64)
            # Phantom signal: a *narrowband-modulated* carrier at the offset — the
            # modulation gives the temporal diversity a real signal has, so the
            # spatial covariance is well-conditioned (a pure CW tone would be
            # rank-degenerate and defeat MUSIC's signal/noise split). The default
            # ~fs/32 modulation bandwidth still fits inside a typical VFO channel.
            w = max(8, int(em.get("mod_avg", 32)))
            m = (np.random.standard_normal(n_samples) + 1j * np.random.standard_normal(n_samples)).astype(np.complex64)
            m = np.convolve(m, np.ones(w, dtype=np.complex64) / w, mode="same")
            m /= math.sqrt(float(np.mean(np.abs(m) ** 2)) + 1e-12)
            sig = np.exp(2j * np.pi * df * t).astype(np.complex64) * m
            x += np.outer(a, amp * sig)
        # Corrupt the array response with the per-channel drift (what calibration
        # removes), then add per-element complex Gaussian noise (σ=1).
        x *= cerr[:, None]
        noise = (np.random.standard_normal((M, n_samples)) + 1j * np.random.standard_normal((M, n_samples))).astype(np.complex64) / math.sqrt(2)
        x += noise
        self._seq += 1
        return IqFrame(
            samples=x,
            sample_rate_hz=self.sample_rate,
            center_freq_hz=self.center_hz,
            capture_time_ns=time.time_ns(),
            channels=M,
            gain_db=self.gain_db,
            sequence=self._seq,
            metadata={"driver": "synthetic", "array": self.array.to_dict(),
                       "n_emitters": len(self.emitters)},
        )
