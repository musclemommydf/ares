"""
ANTSDR e200 driver (AD9361-based coherent multi-channel SDR).

ANTSDR e200 is built around the Analog Devices AD9361 dual-channel
transceiver on a Zynq SoC. The popular "KrakenSDR-clone" variants chain N×
e200 boards with shared LO + sample-clock to provide 2N coherent channels for
DF — typically 4-channel (2× e200) or 8-channel (4× e200).

Wire-level access is via libiio over TCP (default port 30431) or USB. Frames
are pulled via iio_buffer_refill, sample format Q.11 little-endian I/Q.

CLEAR-STUB: needs the hardware to validate iio paths + per-board sample-clock
sync (PPS or external 10 MHz). Synthetic fallback below keeps the pipeline
testable.
"""

from __future__ import annotations

import logging
import time
from typing import Optional

import numpy as np

from ...df.arrays import ArrayGeometry
from .base import DriverCapabilities, IqFrame, SdrDriver
from .synthetic import SyntheticDriver

log = logging.getLogger(__name__)


class AntsdrE200Driver(SdrDriver):
    capabilities = DriverCapabilities(
        name="ANTSDR e200 (coherent chain)",
        driver_id="antsdr_e200",
        coherent=True,
        max_channels=8,
        sample_rate_range_hz=(0.7e6, 61.44e6),
        tunable_range_hz=(70e6, 6e9),
        gain_range_db=(0, 76),
        iq_capture=True,
        notes="AD9361 dual-channel + SoC. PPS-locked clock-sharing for >2ch coherent.",
    )

    def __init__(self, uri: str = "ip:192.168.1.10", channels: int = 4,
                 array: Optional[ArrayGeometry] = None):
        self.uri = uri
        self.channels = channels
        self.array = array or ArrayGeometry.uca(channels, 0.058)
        self._ctx = None
        self._fallback: Optional[SyntheticDriver] = None
        self.center_hz = 433.92e6
        self.sample_rate = 2.4e6
        self.gain_db = 40.0
        self._seq = 0

    def open(self) -> None:
        try:
            import iio                                       # noqa: F401  (optional dep)
            self._ctx = iio.Context(self.uri)
            log.info("antsdr e200 connected at %s", self.uri)
            # TODO(hw): find iio channels rx_lo / voltage[0..N] / buffer.
            # ad9361_phy = self._ctx.find_device("ad9361-phy")
            # rx_phy = ad9361_phy.find_channel("RX_LO", True)
            # rx_phy.attrs["frequency"].value = str(int(self.center_hz))
            # ad9361_lpc = self._ctx.find_device("cf-ad9361-lpc")
            # for ch in ad9361_lpc.channels: ch.enabled = True
            # self._buf = iio.Buffer(ad9361_lpc, 65536)
        except Exception as e:
            log.warning("antsdr e200 unreachable (%s) — falling back to synthetic.", e)
            self._ctx = None
            self._fallback = SyntheticDriver(channels=self.channels, array=self.array)
            self._fallback.open()

    def close(self) -> None:
        if self._fallback:
            self._fallback.close(); self._fallback = None
        # TODO(hw): release iio.Buffer + Context handles cleanly.
        self._ctx = None

    def set_frequency(self, freq_hz: float) -> None:
        self.center_hz = float(freq_hz)
        if self._fallback: self._fallback.set_frequency(freq_hz)
        # TODO(hw): ad9361_phy.find_channel("RX_LO", True).attrs["frequency"].value = ...

    def set_sample_rate(self, rate_hz: float) -> None:
        self.sample_rate = float(rate_hz)
        if self._fallback: self._fallback.set_sample_rate(rate_hz)
        # TODO(hw): ad9361_phy.attrs["sampling_frequency"] = ...

    def set_gain(self, gain_db: float) -> None:
        self.gain_db = float(gain_db)
        if self._fallback: self._fallback.set_gain(gain_db)
        # TODO(hw): set per-channel gain mode = manual, gain values.

    def _read_iq_impl(self, n_samples: int) -> IqFrame:
        if self._fallback: return self._fallback.read_iq(n_samples)
        if self._ctx is None:
            raise RuntimeError("driver not opened")
        # TODO(hw): self._buf.refill(); raw = bytes(self._buf.read()); parse Q.11 IQ
        raise NotImplementedError("ANTSDR e200 frame pull not yet implemented — fallback engages when unreachable")
