"""
ANTSDR e200 driver (AD9361-based coherent multi-channel SDR).

ANTSDR e200 is built around the Analog Devices AD9361 dual-channel transceiver
on a Zynq SoC, reachable via libiio over TCP (default ``ip:192.168.1.10``) or
USB. A single AD9361 gives 2 phase-coherent RX channels; the "KrakenSDR-clone"
chains N× boards with a shared LO + sample-clock (PPS / external 10 MHz) for 2N
channels — that multi-board case needs one libiio context per board and is out
of scope here, so this driver drives one board (≤2 coherent channels).

Implemented over pyadi-iio (``adi.ad9361``), which handles the iio buffer refill
and Q1.11 → float scaling. Falls back to synthetic IQ when pyadi/the board is
unreachable so the pipeline stays testable. Needs hardware to validate.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Optional

import numpy as np

from ...df.arrays import ArrayGeometry
from .base import DriverCapabilities, IqFrame, SdrDriver
from .synthetic import SyntheticDriver

log = logging.getLogger(__name__)

_FULLSCALE = 2 ** 11   # AD9361 12-bit samples are Q1.11 → ±2048 full scale


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
        notes="AD9361 dual-channel + SoC. One board = 2 coherent ch; chain boards for more.",
    )

    def __init__(self, uri: str = "ip:192.168.1.10", channels: int = 2,
                 array: Optional[ArrayGeometry] = None):
        self.uri = uri
        # A single AD9361 exposes at most 2 coherent RX channels.
        self.channels = max(1, min(int(channels), 2))
        self.array = array or ArrayGeometry.uca(self.channels, 0.058)
        self._sdr = None
        self._fallback: Optional[SyntheticDriver] = None
        self._lock = threading.RLock()
        self.center_hz = 433.92e6
        self.sample_rate = 2.4e6
        self.gain_db = 40.0
        self.gain_mode = "manual"
        self._buf_size = 0
        self._seq = 0

    # ── lifecycle ──────────────────────────────────────────────────────────
    def open(self) -> None:
        try:
            import adi  # type: ignore  (pyadi-iio)
            self._sdr = adi.ad9361(uri=self.uri)
            chans = list(range(self.channels))
            self._sdr.rx_enabled_channels = chans
            self._sdr.sample_rate = int(self.sample_rate)
            self._sdr.rx_rf_bandwidth = int(self.sample_rate)
            self._sdr.rx_lo = int(self.center_hz)
            self._apply_gain()
            self._sdr.rx_buffer_size = 65536
            self._buf_size = 65536
            log.info("antsdr e200 connected at %s (%d ch, pyadi)", self.uri, self.channels)
        except Exception as e:
            log.warning("antsdr e200 unreachable (%s) — falling back to synthetic.", e)
            self._sdr = None
            self._fallback = SyntheticDriver(channels=self.channels, array=self.array)
            self._fallback.open()

    def close(self) -> None:
        if self._fallback:
            self._fallback.close(); self._fallback = None
        if self._sdr is not None:
            with self._lock:
                try: self._sdr.rx_destroy_buffer()
                except Exception: pass
            self._sdr = None

    # ── control ────────────────────────────────────────────────────────────
    def set_frequency(self, freq_hz: float) -> None:
        self.center_hz = float(freq_hz)
        if self._fallback: self._fallback.set_frequency(freq_hz); return
        if self._sdr is not None:
            with self._lock:
                try: self._sdr.rx_lo = int(self.center_hz)
                except Exception as e: log.debug("antsdr set freq failed: %s", e)

    def set_sample_rate(self, rate_hz: float) -> None:
        self.sample_rate = float(rate_hz)
        if self._fallback: self._fallback.set_sample_rate(rate_hz); return
        if self._sdr is not None:
            with self._lock:
                try:
                    self._sdr.sample_rate = int(self.sample_rate)
                    self._sdr.rx_rf_bandwidth = int(self.sample_rate)
                except Exception as e:
                    log.debug("antsdr set rate failed: %s", e)

    def set_gain(self, gain_db: float) -> None:
        self.gain_db = float(gain_db)
        if self._fallback: self._fallback.set_gain(gain_db); return
        if self._sdr is not None:
            self._apply_gain()

    def _apply_gain(self) -> None:
        for ch in range(self.channels):
            try:
                setattr(self._sdr, f"gain_control_mode_chan{ch}", self.gain_mode)
                if self.gain_mode == "manual":
                    setattr(self._sdr, f"rx_hardwaregain_chan{ch}", float(self.gain_db))
            except Exception as e:
                log.debug("antsdr gain set failed (ch%d): %s", ch, e)

    # ── frame I/O ──────────────────────────────────────────────────────────
    def _read_iq_impl(self, n_samples: int) -> IqFrame:
        if self._fallback:
            return self._fallback.read_iq(n_samples)
        if self._sdr is None:
            raise RuntimeError("driver not opened")
        n = int(max(256, n_samples))
        with self._lock:
            if n != self._buf_size:
                try: self._sdr.rx_destroy_buffer()
                except Exception: pass
                self._sdr.rx_buffer_size = n
                self._buf_size = n
            raw = self._sdr.rx()
        if isinstance(raw, (list, tuple)):
            data = np.stack([np.asarray(c, dtype=np.complex64) for c in raw], axis=0)
        else:
            data = np.asarray(raw, dtype=np.complex64)[np.newaxis, :]
        data = (data / _FULLSCALE).astype(np.complex64)
        self._seq += 1
        return IqFrame(
            samples=data,
            sample_rate_hz=self.sample_rate,
            center_freq_hz=self.center_hz,
            capture_time_ns=time.time_ns(),
            channels=data.shape[0],
            gain_db=self.gain_db,
            sequence=self._seq,
            metadata={"driver": "antsdr_e200", "uri": self.uri,
                      "coherent": data.shape[0] >= 2,
                      "needs_phase_cal": data.shape[0] >= 2},
        )
