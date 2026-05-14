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

import math
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
        notes="Phantom emitters at fixed DoAs. Useful for development & demo.",
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

    def open(self) -> None: self._open = True
    def close(self) -> None: self._open = False
    def set_frequency(self, freq_hz: float) -> None: self.center_hz = float(freq_hz)
    def set_sample_rate(self, rate_hz: float) -> None: self.sample_rate = float(rate_hz)
    def set_gain(self, gain_db: float) -> None: self.gain_db = float(gain_db)

    def set_emitters(self, emitters: list[dict]) -> None:
        self.emitters = list(emitters or [])

    def _read_iq_impl(self, n_samples: int) -> IqFrame:
        if not self._open:
            self.open()
        M = self.array.n
        t = np.arange(n_samples, dtype=float) / self.sample_rate
        x = np.zeros((M, n_samples), dtype=np.complex64)
        for em in self.emitters:
            az = float(em["az_deg"])
            df = float(em.get("freq_offset_hz", 0.0))
            snr = float(em.get("snr_db", 20.0))
            amp = 10 ** (snr / 20.0)
            a = steering_vector(self.array, self.center_hz, az).astype(np.complex64)
            # Phantom signal: complex sinusoid at the offset + random phase per snapshot
            phase = np.exp(2j * np.pi * df * t + 1j * np.random.uniform(0, 2 * np.pi))
            x += np.outer(a, amp * phase.astype(np.complex64))
        # Per-element complex Gaussian noise (σ=1).
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
