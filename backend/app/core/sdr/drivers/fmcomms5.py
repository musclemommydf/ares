# SPDX-License-Identifier: MIT OR Apache-2.0
# Copyright (c) 2026 Ares

"""
FMCOMMS5 driver (2× AD9361 → 4 phase-coherent RX, on a ZC706/ZCU102 carrier).

The Analog Devices **AD-FMCOMMS5-EBZ** carries *two* AD9361 transceivers on one
FMC card. The two chips share a single LO and sample clock (and are multi-chip
synchronised, "MCS"), so all **4 RX channels are phase-coherent on one board** —
a 4-element interferometer for direction finding with no external 10 MHz / PPS.
It's the usual high-channel-count companion to a Xilinx ZC706 (Zynq-7045) or
ZCU102 running the ADI Linux/HDL reference design, reachable over libiio (TCP
:30431, default ``ip:192.168.2.1``, or ``ip:analog.local``).

Access is **in-process via pyadi-iio** (``adi.FMComms5``), the ADI-supported
binding — the same path the Pluto driver uses, just the 4-channel class. pyadi
exposes channels 0,1 on chip A and 2,3 on chip B; both chips' RX LOs are tuned
together (``rx_lo`` + ``rx_lo_chip_b``) so the whole array sits on one band. A
bare board has no built-in coherence reference, so the inter-channel phase offset
(random per power-cycle, plus the A↔B chip offset) must be calibrated out before
bearings are trustworthy — compass-calibrate against a known emitter.

IQ from pyadi arrives as 12-bit samples scaled to ±2048; we normalise by 2^11 to
land in ±1.0 complex64, matching the synthetic/Pluto paths so the downstream DSP
(PSD in dBFS, MUSIC/Bartlett, demod) sees one consistent scale. When no board is
reachable the driver falls back to synthetic IQ so the pipeline stays testable.
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

_FULLSCALE = 2048.0          # AD936x 12-bit I/Q full scale (±2^11)
_C = 299_792_458.0


class FmComms5Driver(SdrDriver):
    capabilities = DriverCapabilities(
        name="FMCOMMS5 (2× AD9361, 4-ch coherent)",
        driver_id="fmcomms5",
        coherent=True,                       # 2 chips share LO + sample clock (MCS) → phase-coherent
        max_channels=4,
        sample_rate_range_hz=(521e3, 61.44e6),
        tunable_range_hz=(70e6, 6e9),        # AD9361 tuning range
        gain_range_db=(0.0, 73.0),           # AD9361 RX manual hardware gain
        iq_capture=True,
        on_device_fft=False,
        on_device_doa=False,
        tx_capable=True,                     # 4× TX as well — drives a NIC uplink
        cal_source=False,                    # no built-in noise source; calibrate against a known emitter
        notes="AD-FMCOMMS5-EBZ: two AD9361 on one FMC (ZC706/ZCU102), 4 LO/clock-shared "
              "phase-coherent RX for DF. Opened in-process via pyadi-iio (adi.FMComms5); "
              "both chips' RX LOs are tuned together. No built-in coherence reference, so "
              "DF needs inter-channel phase calibration (compass-calibrate vs a known emitter).",
    )

    def __init__(self, uri: str = "ip:192.168.2.1", channels: int = 4,
                 gain_mode: str = "manual",
                 array: Optional[ArrayGeometry] = None):
        self.uri = uri or "ip:192.168.2.1"
        self.channels = max(1, min(4, int(channels)))
        self.gain_mode = (gain_mode or "manual").lower()   # manual | slow_attack | fast_attack | hybrid
        self.tx_atten_db = 10.0            # TX attenuation (0 = full power; AD936x range 0…89)
        self._lock = threading.RLock()     # serialise rx()/tx() on the shared libiio context
        self._tx_lo_set: Optional[int] = None
        # 4 elements ~λ/2 apart — a uniform linear array is the right default baseline;
        # the device config (array_type/spacing) overrides this at the adapter level.
        self.array = array or ArrayGeometry.ula(self.channels, 0.5 * _C / 433.92e6)
        self.center_hz = 433.92e6
        self.sample_rate = 4.0e6
        self.gain_db = 40.0
        self._sdr = None                 # pyadi adi.FMComms5 handle
        self._backend = "none"           # "pyadi" | "synthetic"
        self._fallback: Optional[SyntheticDriver] = None
        self._buf_size = 0               # last rx_buffer_size set on the pyadi handle
        self._seq = 0

    # ── lifecycle ──────────────────────────────────────────────────────────────
    def open(self) -> None:
        # 1) pyadi-iio — the canonical FMCOMMS5 path (adi.FMComms5 = 2× AD9361).
        try:
            import adi                                            # type: ignore
            s = adi.FMComms5(uri=self.uri)
            s.rx_enabled_channels = list(range(self.channels))    # [0,1,2,3] across both chips
            self._sdr = s
            self._sdr.sample_rate = int(self.sample_rate)
            self._set_rf_bandwidth(int(self.sample_rate))
            self._set_los(int(self.center_hz))                    # both chips' LOs together
            self._apply_gain_pyadi()
            self._backend = "pyadi"
            if not (70e6 <= self.center_hz <= 6e9):
                log.info("fmcomms5 tuned to %.4f GHz — outside the AD9361 range (0.07–6 GHz).",
                         self.center_hz / 1e9)
            log.info("fmcomms5 open via pyadi-iio: %s (%d coherent ch)", self.uri, self.channels)
            return
        except Exception as e:
            log.info("pyadi-iio FMComms5 open failed (%s) — falling back to synthetic IQ.", e)
            self._sdr = None

        # 2) Synthetic fallback — keep the pipeline alive with phantom emitters.
        log.warning("no FMCOMMS5 reachable at %s — using synthetic IQ.", self.uri)
        self._backend = "synthetic"
        self._fallback = SyntheticDriver(channels=self.channels, array=self.array)
        self._fallback.set_frequency(self.center_hz)
        self._fallback.set_sample_rate(self.sample_rate)
        self._fallback.set_gain(self.gain_db)
        self._fallback.open()

    def close(self) -> None:
        if self._fallback:
            self._fallback.close(); self._fallback = None
        if self._sdr is not None:
            try:
                self._sdr.rx_destroy_buffer()
            except Exception:
                pass
        self._sdr = None
        self._buf_size = 0
        self._backend = "none"

    # ── tuning ─────────────────────────────────────────────────────────────────
    def _set_los(self, freq_hz: int) -> None:
        """Tune both AD9361 chips' RX LOs to the same frequency (one band across
        the 4-element array). ``rx_lo_chip_b`` is the chip-B LO in pyadi."""
        self._sdr.rx_lo = int(freq_hz)
        for attr in ("rx_lo_chip_b", "rx_lo_chan_b"):   # name varies across pyadi versions
            if hasattr(self._sdr, attr):
                try:
                    setattr(self._sdr, attr, int(freq_hz))
                except Exception as e:
                    log.debug("fmcomms5: %s set failed: %s", attr, e)
                break

    def _set_rf_bandwidth(self, bw_hz: int) -> None:
        """Match the analog RX filter to the sample rate on both chips."""
        for attr in ("rx_rf_bandwidth", "rx_rf_bandwidth_chan_b"):
            if hasattr(self._sdr, attr):
                try:
                    setattr(self._sdr, attr, int(bw_hz))
                except Exception as e:
                    log.debug("fmcomms5: %s set failed: %s", attr, e)

    def set_frequency(self, freq_hz: float) -> None:
        self.center_hz = float(freq_hz)
        if self._backend == "synthetic" and self._fallback:
            self._fallback.set_frequency(freq_hz); return
        if self._backend == "pyadi" and self._sdr is not None:
            with self._lock:
                self._set_los(int(self.center_hz))

    def set_sample_rate(self, rate_hz: float) -> None:
        self.sample_rate = float(rate_hz)
        if self._backend == "synthetic" and self._fallback:
            self._fallback.set_sample_rate(rate_hz); return
        if self._backend == "pyadi" and self._sdr is not None:
            with self._lock:
                self._sdr.sample_rate = int(self.sample_rate)
                self._set_rf_bandwidth(int(self.sample_rate))

    def set_gain(self, gain_db: float) -> None:
        self.gain_db = float(gain_db)
        if self._backend == "synthetic" and self._fallback:
            self._fallback.set_gain(gain_db); return
        if self._backend == "pyadi" and self._sdr is not None:
            self._apply_gain_pyadi()

    def _apply_gain_pyadi(self) -> None:
        """Set per-channel gain mode + manual gain across all 4 channels (0,1 on
        chip A · 2,3 on chip B)."""
        for ch in range(self.channels):
            try:
                setattr(self._sdr, f"gain_control_mode_chan{ch}", self.gain_mode)
                if self.gain_mode == "manual":
                    setattr(self._sdr, f"rx_hardwaregain_chan{ch}", float(self.gain_db))
            except Exception as e:
                log.debug("fmcomms5 gain set failed (ch%d): %s", ch, e)

    # ── transmit (NIC uplink) ────────────────────────────────────────────────────
    def transmit(self, samples: np.ndarray) -> None:
        """Put a one-shot baseband burst on the air via chip-A TX channel 0. The
        TX LO tracks the RX centre frequency (one band for the NIC link). On the
        synthetic fallback this loops back into RX."""
        if self._backend == "synthetic" and self._fallback:
            self._fallback.transmit(samples); return
        if self._backend != "pyadi" or self._sdr is None:
            raise NotImplementedError("fmcomms5 transmit needs the pyadi-iio backend")
        s = np.asarray(samples, dtype=np.complex64).ravel()
        if s.size == 0:
            return
        peak = float(np.max(np.abs(s))) or 1.0
        tx = (s / peak * (0.7 * _FULLSCALE * 8.0)).astype(np.complex64)   # ±~11.5k of ±2^14
        with self._lock:
            try:
                self._sdr.tx_destroy_buffer()
            except Exception:
                pass
            try:
                self._sdr.tx_cyclic_buffer = False
                if self._tx_lo_set != int(self.center_hz):
                    self._sdr.tx_lo = int(self.center_hz)
                    for attr in ("tx_lo_chip_b", "tx_lo_chan_b"):
                        if hasattr(self._sdr, attr):
                            try: setattr(self._sdr, attr, int(self.center_hz))
                            except Exception: pass
                            break
                    self._tx_lo_set = int(self.center_hz)
                try:
                    self._sdr.tx_hardwaregain_chan0 = -float(self.tx_atten_db)
                except Exception:
                    pass
                self._sdr.tx_enabled_channels = [0]
                self._sdr.tx(tx)
            except Exception as e:
                raise RuntimeError(f"fmcomms5 tx failed: {e}") from e

    # ── IQ read ────────────────────────────────────────────────────────────────
    def _read_iq_impl(self, n_samples: int) -> IqFrame:
        n_samples = int(max(256, n_samples))
        if self._backend == "synthetic" and self._fallback:
            return self._fallback.read_iq(n_samples)
        if self._backend != "pyadi" or self._sdr is None:
            raise RuntimeError("fmcomms5 driver not opened")
        with self._lock:
            if n_samples != self._buf_size:
                try:
                    self._sdr.rx_destroy_buffer()
                except Exception:
                    pass
                self._sdr.rx_buffer_size = int(n_samples)
                self._buf_size = int(n_samples)
            raw = self._sdr.rx()                       # list of N ndarrays (N≥2 enabled)
        if isinstance(raw, (list, tuple)):
            data = np.stack([np.asarray(c, dtype=np.complex64) for c in raw], axis=0)
        else:
            data = np.asarray(raw, dtype=np.complex64)[np.newaxis, :]
        data = (data / _FULLSCALE).astype(np.complex64)
        self._seq += 1
        return IqFrame(
            samples=data, sample_rate_hz=self.sample_rate, center_freq_hz=self.center_hz,
            capture_time_ns=time.time_ns(), channels=data.shape[0],
            gain_db=self.gain_db, sequence=self._seq,
            metadata={"driver": "fmcomms5", "backend": "pyadi", "uri": self.uri,
                      "gain_mode": self.gain_mode,
                      "coherent": data.shape[0] >= 2,
                      "needs_phase_cal": data.shape[0] >= 2},
        )
