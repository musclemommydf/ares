# SPDX-License-Identifier: MIT OR Apache-2.0
# Copyright (c) 2026 Ares

"""
Epiq Solutions Matchstiq X40 driver.

Matchstiq X40 is a tactical wideband SDR (10 MHz – 6 GHz, up to 100 MHz IBW)
with an embedded ARM SoC. Two access modes:

  1. Soapy/SoapySDR over USB or Ethernet — see Epiq's SidekiqUSDR / SoapySidekiq
     project. Pulled into Ares via the soapy driver shim.
  2. Direct over Epiq's Sidekiq C API (libsidekiq) when low-latency is critical.

Tactical use case: single-channel observer in Ares (no coherent DF) or paired
with an external coherent-clock chain.

Real-HW path goes through SoapySDR ``driver=sidekiq`` (Epiq's SoapySidekiq);
``open()`` probes for it and uses it when present, else falls back to synthetic
IQ so the pipeline stays operational. libsidekiq-direct (lowest latency) is not
wired — SoapySidekiq is the supported route. Needs hardware to validate.
"""

from __future__ import annotations

import logging
import time
from typing import Optional

import numpy as np

from ...df.arrays import ArrayGeometry
from .base import DriverCapabilities, IqFrame, SdrDriver
from .synthetic import SyntheticDriver
from .. import iq_capture

log = logging.getLogger(__name__)

_SIDEKIQ_ARGS = "driver=sidekiq"


class MatchstiqX40Driver(SdrDriver):
    capabilities = DriverCapabilities(
        name="Epiq Matchstiq X40",
        driver_id="matchstiq_x40",
        coherent=False,           # single channel by itself
        max_channels=2,
        sample_rate_range_hz=(0.25e6, 100e6),
        tunable_range_hz=(10e6, 6e9),
        gain_range_db=(0, 76),
        iq_capture=True,
        notes="Tactical wideband (100 MHz IBW). Use Soapy 'driver=sidekiq' for real HW.",
    )

    def __init__(self, channels: int = 1, array: Optional[ArrayGeometry] = None):
        self.channels = channels
        self.array = array or ArrayGeometry.ula(channels, 0.1) if channels > 1 else None
        self._fallback: Optional[SyntheticDriver] = None
        self._soapy_dev: Optional[dict] = None     # device dict for iq_capture
        self.center_hz = 1.0e9
        self.sample_rate = 30.72e6
        self.gain_db = 30.0
        self._seq = 0

    def _soapy_device(self) -> dict:
        return {"id": "matchstiq_x40", "driver": "sidekiq",
                "metadata": {"soapy": _SIDEKIQ_ARGS, "gain_db": self.gain_db}}

    def open(self) -> None:
        # Probe SoapySidekiq with a tiny capture; use it if it returns samples.
        if iq_capture.available():
            dev = self._soapy_device()
            probe = iq_capture.capture(dev, self.center_hz, self.sample_rate, 4096,
                                       channels=(0,), gain_db=self.gain_db)
            if probe is not None:
                self._soapy_dev = dev
                log.info("matchstiq X40 connected via SoapySDR (%s)", _SIDEKIQ_ARGS)
                return
        log.warning("matchstiq X40: SoapySidekiq (%s) unavailable — using synthetic IQ.", _SIDEKIQ_ARGS)
        self._fallback = SyntheticDriver(channels=max(1, self.channels),
                                          array=self.array or ArrayGeometry.ula(max(1, self.channels), 0.1))
        self._fallback.open()

    def close(self) -> None:
        if self._fallback: self._fallback.close(); self._fallback = None
        self._soapy_dev = None

    def set_frequency(self, freq_hz: float) -> None:
        self.center_hz = float(freq_hz)
        if self._fallback: self._fallback.set_frequency(freq_hz)

    def set_sample_rate(self, rate_hz: float) -> None:
        self.sample_rate = float(rate_hz)
        if self._fallback: self._fallback.set_sample_rate(rate_hz)

    def set_gain(self, gain_db: float) -> None:
        self.gain_db = float(gain_db)
        if self._fallback: self._fallback.set_gain(gain_db)
        if self._soapy_dev: self._soapy_dev["metadata"]["gain_db"] = self.gain_db

    def _read_iq_impl(self, n_samples: int) -> IqFrame:
        if self._fallback:
            return self._fallback.read_iq(n_samples)
        if self._soapy_dev is None:
            raise RuntimeError("matchstiq driver not opened")
        chans = tuple(range(max(1, self.channels)))
        x = iq_capture.capture(self._soapy_dev, self.center_hz, self.sample_rate,
                               int(max(256, n_samples)), channels=chans, gain_db=self.gain_db)
        if x is None:
            raise RuntimeError("matchstiq SoapySDR capture returned no samples")
        if isinstance(x, list):
            data = np.stack([np.asarray(c, dtype=np.complex64) for c in x], axis=0)
        else:
            data = np.asarray(x, dtype=np.complex64)[np.newaxis, :]
        self._seq += 1
        return IqFrame(
            samples=data, sample_rate_hz=self.sample_rate, center_freq_hz=self.center_hz,
            capture_time_ns=time.time_ns(), channels=data.shape[0],
            gain_db=self.gain_db, sequence=self._seq,
            metadata={"driver": "matchstiq_x40", "backend": "soapy",
                      "soapy_args": _SIDEKIQ_ARGS, "coherent": data.shape[0] >= 2},
        )
