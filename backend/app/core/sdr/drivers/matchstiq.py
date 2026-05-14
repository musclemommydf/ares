"""
Epiq Solutions Matchstiq X40 driver.

Matchstiq X40 is a tactical wideband SDR (10 MHz – 6 GHz, up to 100 MHz IBW)
with an embedded ARM SoC. Two access modes:

  1. Soapy/SoapySDR over USB or Ethernet — see Epiq's SidekiqUSDR / SoapySidekiq
     project. Pulled into Ares via the soapy driver shim.
  2. Direct over Epiq's Sidekiq C API (libsidekiq) when low-latency is critical.

Tactical use case: single-channel observer in Ares (no coherent DF) or paired
with an external coherent-clock chain.

CLEAR-STUB: real-hardware path requires libsidekiq + USB driver. Synthetic
fallback keeps the rest of the pipeline operational; the Soapy path is the
recommended real route — wire it via `sdr.registry.register('matchstiq',
SoapyDriver(args='driver=sidekiq'))`.
"""

from __future__ import annotations

import logging
from typing import Optional

from ...df.arrays import ArrayGeometry
from .base import DriverCapabilities, IqFrame, SdrDriver
from .synthetic import SyntheticDriver

log = logging.getLogger(__name__)


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
        self.center_hz = 1.0e9
        self.sample_rate = 30.72e6
        self.gain_db = 30.0

    def open(self) -> None:
        # TODO(hw): try libsidekiq → fall back to Soapy → fall back to synthetic.
        log.info("matchstiq driver: using synthetic IQ — wire libsidekiq or "
                  "Soapy 'driver=sidekiq' for real hardware.")
        self._fallback = SyntheticDriver(channels=max(1, self.channels),
                                          array=self.array or ArrayGeometry.ula(max(1, self.channels), 0.1))
        self._fallback.open()

    def close(self) -> None:
        if self._fallback: self._fallback.close(); self._fallback = None

    def set_frequency(self, freq_hz: float) -> None:
        self.center_hz = float(freq_hz)
        if self._fallback: self._fallback.set_frequency(freq_hz)

    def set_sample_rate(self, rate_hz: float) -> None:
        self.sample_rate = float(rate_hz)
        if self._fallback: self._fallback.set_sample_rate(rate_hz)

    def set_gain(self, gain_db: float) -> None:
        self.gain_db = float(gain_db)
        if self._fallback: self._fallback.set_gain(gain_db)

    def _read_iq_impl(self, n_samples: int) -> IqFrame:
        if self._fallback: return self._fallback.read_iq(n_samples)
        raise NotImplementedError("Matchstiq real-HW path not yet implemented — Soapy 'driver=sidekiq' recommended")
