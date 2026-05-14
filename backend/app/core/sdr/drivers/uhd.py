"""
Ettus / NI USRP driver (X310 / N3xx / X4xx / N321 / X410 / X420).

Two access paths:
  1. UHD's Python bindings (`uhd` package) — preferred for tight latency.
  2. Soapy via `driver=uhd` — fall back.

Coherent multi-channel: USRP supports MIMO cables and external 10 MHz / PPS
input on every model listed. For DF, configure
    uhd.usrp.MultiUSRP("addr0=...,addr1=...")  + set_clock_source("external") + set_time_source("external")

RFNoC: USRP X-series and N321/N3xx/X4xx have an FPGA — for high-rate MUSIC,
Bartlett, or FFT, write an RFNoC block (gr-ettus / image builder) and call it
via the rfnoc-replay block here. CLEAR-STUB: full RFNoC integration needs a
prebuilt FPGA image and the rfnoc-doa block (community project); skeleton
below documents where it plugs in.
"""

from __future__ import annotations

import logging
import time
from typing import Optional

import numpy as np

from .base import DriverCapabilities, IqFrame, SdrDriver
from .synthetic import SyntheticDriver
from ...df.arrays import ArrayGeometry

log = logging.getLogger(__name__)


class UhdUsrpDriver(SdrDriver):
    capabilities = DriverCapabilities(
        name="Ettus / NI USRP (UHD)",
        driver_id="uhd_usrp",
        coherent=True,
        max_channels=8,
        sample_rate_range_hz=(0.2e6, 200e6),
        tunable_range_hz=(0, 7.2e9),
        gain_range_db=(0, 80),
        iq_capture=True,
        on_device_fft=True,
        on_device_doa=True,
        notes="RFNoC: load an rfnoc-doa image for on-FPGA MUSIC/Bartlett. "
              "Use external 10 MHz + PPS for inter-USRP coherence.",
    )

    def __init__(self, args: str = "", channels: int = 2,
                 use_external_clock: bool = True,
                 array: Optional[ArrayGeometry] = None):
        self.args = args
        self.channels = channels
        self.use_external_clock = use_external_clock
        self.array = array or ArrayGeometry.ula(channels, 0.1)
        self._usrp = None
        self._streamer = None
        self._fallback: Optional[SyntheticDriver] = None
        self.center_hz = 1.5e9
        self.sample_rate = 10e6
        self.gain_db = 30.0
        self._seq = 0

    def open(self) -> None:
        try:
            import uhd                                       # type: ignore
            self._usrp = uhd.usrp.MultiUSRP(self.args or "")
            if self.use_external_clock:
                try: self._usrp.set_clock_source("external"); self._usrp.set_time_source("external")
                except Exception as e: log.warning("usrp external clock unavailable: %s", e)
            for ch in range(self.channels):
                self._usrp.set_rx_rate(self.sample_rate, ch)
                self._usrp.set_rx_freq(uhd.libpyuhd.types.tune_request(self.center_hz), ch)
                self._usrp.set_rx_gain(self.gain_db, ch)
            sa = uhd.usrp.StreamArgs("fc32", "sc16")
            sa.channels = list(range(self.channels))
            self._streamer = self._usrp.get_rx_stream(sa)
            log.info("uhd USRP open: %s (%d ch)", self.args or "<auto>", self.channels)
        except Exception as e:
            log.warning("uhd open failed (%s) — falling back to synthetic.", e)
            self._fallback = SyntheticDriver(channels=self.channels, array=self.array)
            self._fallback.open()

    def close(self) -> None:
        if self._fallback: self._fallback.close(); self._fallback = None
        self._streamer = None; self._usrp = None

    def set_frequency(self, freq_hz: float) -> None:
        self.center_hz = float(freq_hz)
        if self._fallback: self._fallback.set_frequency(freq_hz); return
        if self._usrp:
            import uhd
            for ch in range(self.channels):
                self._usrp.set_rx_freq(uhd.libpyuhd.types.tune_request(self.center_hz), ch)

    def set_sample_rate(self, rate_hz: float) -> None:
        self.sample_rate = float(rate_hz)
        if self._fallback: self._fallback.set_sample_rate(rate_hz); return
        if self._usrp:
            for ch in range(self.channels):
                self._usrp.set_rx_rate(self.sample_rate, ch)

    def set_gain(self, gain_db: float) -> None:
        self.gain_db = float(gain_db)
        if self._fallback: self._fallback.set_gain(gain_db); return
        if self._usrp:
            for ch in range(self.channels):
                self._usrp.set_rx_gain(self.gain_db, ch)

    def _read_iq_impl(self, n_samples: int) -> IqFrame:
        if self._fallback: return self._fallback.read_iq(n_samples)
        if not self._streamer:
            raise RuntimeError("driver not opened")
        import uhd
        rx_md = uhd.types.RXMetadata()
        buf = np.zeros((self.channels, n_samples), dtype=np.complex64)
        stream_cmd = uhd.types.StreamCMD(uhd.types.StreamMode.num_done)
        stream_cmd.num_samps = n_samples
        stream_cmd.stream_now = True
        self._streamer.issue_stream_cmd(stream_cmd)
        n = self._streamer.recv(buf, rx_md, 1.0)
        self._seq += 1
        return IqFrame(
            samples=buf[:, :n], sample_rate_hz=self.sample_rate, center_freq_hz=self.center_hz,
            capture_time_ns=time.time_ns(),
            channels=self.channels, gain_db=self.gain_db, sequence=self._seq,
            metadata={"driver": "uhd_usrp", "args": self.args,
                       "external_clock": self.use_external_clock},
        )

    # ── RFNoC offload (clear-stub) ─────────────────────────────────────────
    def fpga_doa(self, az_resolution_deg: float = 1.0) -> Optional[dict]:
        """If the loaded FPGA image includes an rfnoc-doa block, return its
        result directly (FPGA-computed pseudo-spectrum). Returns None when
        unavailable; the caller falls back to CPU MUSIC."""
        # TODO(hw): rfnoc::block_id_t(rfnoc::DOA_BLOCK), configure threshold, pull peaks
        return None
