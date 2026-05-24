# SPDX-License-Identifier: MIT OR Apache-2.0
# Copyright (c) 2026 Ares

"""
ADALM-Pluto driver (Analog Devices AD9363 / AD9364, single-board SDR).

The ADALM-Pluto is a low-cost AD936x transceiver on a Zynq-7010. Stock it is a
*single* RX channel, 325 MHz – 3.8 GHz, ≤20 MSPS. Two well-known firmware mods
unlock the chip's true envelope:

  * the **AD9364 mod** (``fw_setenv compatible ad9364``) → 70 MHz – 6 GHz tuning
    and up to 61.44 MSPS; and
  * the **2R2T MIMO mod** → the AD9361's second RX, which shares the LO and the
    ADC sample clock with the first, so the two channels are **phase-coherent**
    on one board — a two-element interferometer / DF baseline (no external 10 MHz
    or PPS needed).

The driver advertises that full envelope and exposes ``channels=2`` for the
modded path. Crucially, pyadi reaches the 2nd RX only through ``adi.ad9361``
(not ``adi.Pluto``, which is hard-wired to the single-RX ad9364 manifold), so
``open()`` instantiates ``adi.ad9361`` and probes channel 1 when DF is asked
for; on a board without the MIMO mod it falls back to single-RX ``adi.Pluto``.
A bare 2-RX board has no built-in calibration source, so the inter-channel phase
offset (random per power-cycle) must be calibrated out before bearings are
trustworthy — compass-calibrate the device against a known emitter; a 2-element
baseline is also front/back ambiguous (the mirror solution about the array axis).

Access paths, in preference order — all in-process, no external SDR app:
  1. **pyadi-iio** (``import adi``; ``adi.ad9361(uri=...)`` for 2× RX MIMO, else
     ``adi.Pluto(uri=...)``) — the Analog Devices supported binding over libiio.
     Default URI ``ip:192.168.2.1`` (USB-NIC), or ``usb:`` / ``ip:pluto.local``.
     This is the primary, fully-implemented path.
  2. **SoapySDR** (``driver=plutosdr``) via the shared :mod:`app.core.sdr.iq_capture`
     capture layer — used when pyadi-iio isn't installed but SoapyPlutoSDR is.
  3. **Synthetic** — when neither libiio nor Soapy can reach a board, the
     pipeline stays testable with phantom emitters (same fallback every other
     driver uses).

IQ from pyadi arrives as 12-bit samples scaled to ±2048; we normalise by 2^11
to land in roughly ±1.0 complex64, matching the synthetic/Soapy paths so the
downstream DSP (PSD in dBFS, MUSIC/Bartlett, demod) sees one consistent scale.
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


class PlutoSdrDriver(SdrDriver):
    capabilities = DriverCapabilities(
        name="ADALM-Pluto (AD9363/AD9364)",
        driver_id="plutosdr",
        coherent=True,                       # 2nd RX is LO/clock-shared → phase-coherent
        max_channels=2,                      # 1 stock; 2 with the 2R2T firmware mod
        sample_rate_range_hz=(521e3, 61.44e6),
        tunable_range_hz=(70e6, 6e9),        # 70 MHz–6 GHz needs the AD9364 firmware mod (stock: 325 MHz–3.8 GHz)
        gain_range_db=(0.0, 73.0),           # AD9361 RX manual hardware gain
        iq_capture=True,
        on_device_fft=False,
        on_device_doa=False,
        tx_capable=True,                     # AD936x is a transceiver — drives a NIC uplink
        notes="Single-board AD936x. The AD9364 firmware mod unlocks 70 MHz–6 GHz "
              "and up to 61.44 MSPS (stock AD9363: 325 MHz–3.8 GHz); the 2R2T MIMO "
              "mod enables the 2nd, LO/clock-shared RX → 2 phase-coherent channels "
              "for DF (pyadi opens it as adi.ad9361). A bare 2-RX board has no "
              "built-in reference, so DF needs inter-channel phase calibration "
              "(compass-calibrate against a known emitter). Prefers pyadi-iio, then "
              "SoapySDR (driver=plutosdr).",
    )

    def __init__(self, uri: str = "ip:192.168.2.1", channels: int = 1,
                 gain_mode: str = "manual",
                 array: Optional[ArrayGeometry] = None):
        self.uri = uri or "ip:192.168.2.1"
        self.channels = max(1, min(2, int(channels)))
        self.gain_mode = (gain_mode or "manual").lower()   # manual | slow_attack | fast_attack | hybrid
        self.tx_atten_db = 10.0            # TX attenuation (0 = full power; AD936x range 0…89)
        self._lock = threading.RLock()     # serialise rx()/tx() on the shared libiio context
        self._tx_lo_set: Optional[int] = None
        # 2nd RX is ~λ/2 from the first on the modded board's two SMA ports; a
        # 2-element ULA is the right geometry for the interferometric baseline.
        self.array = array or ArrayGeometry.ula(self.channels, 0.5 * 299_792_458.0 / 433.92e6)
        self.center_hz = 433.92e6
        self.sample_rate = 2.4e6
        self.gain_db = 40.0
        self._sdr = None                 # pyadi handle (adi.Pluto or adi.ad9361)
        self._pyadi_class = "none"       # "Pluto" (1× RX) | "ad9361" (2× RX, 2R2T MIMO)
        self._backend = "none"           # "pyadi" | "soapy" | "synthetic"
        self._soapy_dev: Optional[dict] = None
        self._fallback: Optional[SyntheticDriver] = None
        self._buf_size = 0               # last rx_buffer_size set on the pyadi handle
        self._seq = 0

    # ── lifecycle ──────────────────────────────────────────────────────────────
    def open(self) -> None:
        # 1) pyadi-iio — the canonical Pluto path.
        try:
            import adi                                            # type: ignore
            # The 2R2T "MIMO" firmware exposes the AD9361's 2nd RX as a second
            # *complex* channel — pyadi reaches it only through adi.ad9361, never
            # adi.Pluto (which is wired to the single-RX ad9364 manifold). Probe
            # for it by actually enabling channel 1; fall back to single-RX when
            # the board has no MIMO mod (or only 1 channel was asked for).
            s = None
            if self.channels >= 2:
                try:
                    s = adi.ad9361(uri=self.uri)
                    s.rx_enabled_channels = [0, 1]      # raises if the 2nd RX isn't present
                    self._sdr, self._pyadi_class, self.channels = s, "ad9361", 2
                except Exception as e:
                    log.warning("pluto %s: 2nd RX unavailable (%s) — no 2R2T MIMO firmware? "
                                "using single-RX adi.Pluto (DF needs the MIMO mod).", self.uri, e)
                    try:
                        if s is not None:
                            s.rx_destroy_buffer()
                    except Exception:
                        pass
                    self._sdr = None
            if self._sdr is None:
                self._sdr = adi.Pluto(uri=self.uri)
                self._sdr.rx_enabled_channels = [0]
                self._pyadi_class, self.channels = "Pluto", 1
            self._sdr.sample_rate = int(self.sample_rate)
            self._sdr.rx_rf_bandwidth = int(self.sample_rate)
            self._sdr.rx_lo = int(self.center_hz)        # one LO shared by both RX → phase-coherent
            self._apply_gain_pyadi()
            self._backend = "pyadi"
            if not (325e6 <= self.center_hz <= 3.8e9):
                log.info("pluto tuned to %.4f GHz — outside the stock AD9363 band "
                         "(0.325–3.8 GHz); needs the AD9364 firmware mod (70 MHz–6 GHz).",
                         self.center_hz / 1e9)
            log.info("pluto open via pyadi-iio (%s): %s (%d coherent ch)",
                     self._pyadi_class, self.uri, self.channels)
            return
        except Exception as e:
            log.info("pyadi-iio Pluto open failed (%s) — trying SoapySDR.", e)
            self._sdr = None

        # 2) SoapySDR via the shared capture layer (driver=plutosdr).
        try:
            from .. import iq_capture
            if iq_capture.available():
                args = "driver=plutosdr"
                # default URI is implicit for Soapy; pass a non-default uri through
                if self.uri and self.uri != "ip:192.168.2.1":
                    args += f",uri={self.uri}"
                probe = iq_capture.capture(
                    {"metadata": {"soapy": args}}, self.center_hz, self.sample_rate,
                    1024, channels=tuple(range(self.channels)), gain_db=self.gain_db,
                )
                if probe is not None:
                    self._soapy_dev = {"metadata": {"soapy": args, "gain_db": self.gain_db}}
                    self._backend = "soapy"
                    log.info("pluto open via SoapySDR: %s (%d ch)", args, self.channels)
                    return
                log.info("SoapySDR present but no Pluto answered (args=%s).", args)
        except Exception as e:
            log.info("SoapySDR Pluto path unavailable (%s).", e)

        # 3) Synthetic fallback — keep the pipeline alive with phantom emitters.
        log.warning("no Pluto reachable (pyadi/Soapy) — falling back to synthetic IQ.")
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
        self._soapy_dev = None
        self._buf_size = 0
        self._backend = "none"

    # ── tuning ─────────────────────────────────────────────────────────────────
    def set_frequency(self, freq_hz: float) -> None:
        self.center_hz = float(freq_hz)
        if self._backend == "synthetic" and self._fallback:
            self._fallback.set_frequency(freq_hz); return
        if self._backend == "pyadi" and self._sdr is not None:
            self._sdr.rx_lo = int(self.center_hz)
        # soapy: applied per-capture in iq_capture._set_chain

    def set_sample_rate(self, rate_hz: float) -> None:
        self.sample_rate = float(rate_hz)
        if self._backend == "synthetic" and self._fallback:
            self._fallback.set_sample_rate(rate_hz); return
        if self._backend == "pyadi" and self._sdr is not None:
            self._sdr.sample_rate = int(self.sample_rate)
            # keep the analog RX filter roughly matched to the new rate
            try:
                self._sdr.rx_rf_bandwidth = int(self.sample_rate)
            except Exception:
                pass

    def set_gain(self, gain_db: float) -> None:
        self.gain_db = float(gain_db)
        if self._backend == "synthetic" and self._fallback:
            self._fallback.set_gain(gain_db); return
        if self._backend == "pyadi" and self._sdr is not None:
            self._apply_gain_pyadi()
        elif self._backend == "soapy" and self._soapy_dev is not None:
            self._soapy_dev["metadata"]["gain_db"] = self.gain_db

    def _apply_gain_pyadi(self) -> None:
        """Set per-channel gain mode + manual gain on the pyadi handle."""
        for ch in range(self.channels):
            try:
                setattr(self._sdr, f"gain_control_mode_chan{ch}", self.gain_mode)
                if self.gain_mode == "manual":
                    setattr(self._sdr, f"rx_hardwaregain_chan{ch}", float(self.gain_db))
            except Exception as e:
                log.debug("pluto gain set failed (ch%d): %s", ch, e)

    # ── transmit (NIC uplink) ────────────────────────────────────────────────────
    def transmit(self, samples: np.ndarray) -> None:
        """Put a one-shot baseband burst on the air via the AD936x TX chain.

        `samples` is ~unit-amplitude complex64 at the current sample rate; we
        peak-normalise and scale to the AD936x 12-bit TX full-scale. The TX LO
        tracks the RX centre frequency (one band for the NIC link). On the
        synthetic fallback this loops back into RX; SoapySDR TX isn't wired
        (use pyadi for a transmitting Pluto)."""
        if self._backend == "synthetic" and self._fallback:
            self._fallback.transmit(samples); return
        if self._backend == "soapy" and self._soapy_dev is not None:
            from .. import iq_capture
            ok = iq_capture.transmit(self._soapy_dev, self.center_hz, self.sample_rate,
                                     np.asarray(samples, dtype=np.complex64).ravel(),
                                     channel=0, gain_db=-float(self.tx_atten_db))
            if not ok:
                raise RuntimeError("plutosdr SoapySDR transmit failed (TX unsupported by this Soapy module?)")
            return
        if self._backend != "pyadi" or self._sdr is None:
            raise NotImplementedError("plutosdr transmit needs the pyadi-iio or SoapySDR backend")
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
                    self._tx_lo_set = int(self.center_hz)
                try:
                    self._sdr.tx_hardwaregain_chan0 = -float(self.tx_atten_db)
                except Exception:
                    pass
                self._sdr.tx(tx)
            except Exception as e:
                raise RuntimeError(f"plutosdr tx failed: {e}") from e

    # ── IQ read ────────────────────────────────────────────────────────────────
    def _read_iq_impl(self, n_samples: int) -> IqFrame:
        n_samples = int(max(256, n_samples))
        if self._backend == "synthetic" and self._fallback:
            return self._fallback.read_iq(n_samples)
        if self._backend == "pyadi":
            return self._read_pyadi(n_samples)
        if self._backend == "soapy":
            return self._read_soapy(n_samples)
        raise RuntimeError("plutosdr driver not opened")

    def _read_pyadi(self, n_samples: int) -> IqFrame:
        with self._lock:
            if n_samples != self._buf_size:
                # rebuild the libiio buffer at the new length
                try:
                    self._sdr.rx_destroy_buffer()
                except Exception:
                    pass
                self._sdr.rx_buffer_size = int(n_samples)
                self._buf_size = int(n_samples)
            raw = self._sdr.rx()                      # ndarray (1ch) or list of ndarrays (Nch)
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
            metadata={"driver": "plutosdr", "backend": "pyadi", "uri": self.uri,
                      "pyadi_class": self._pyadi_class, "gain_mode": self.gain_mode,
                      "coherent": data.shape[0] >= 2,
                      "needs_phase_cal": data.shape[0] >= 2},
        )

    def _read_soapy(self, n_samples: int) -> IqFrame:
        from .. import iq_capture
        x = iq_capture.capture(self._soapy_dev, self.center_hz, self.sample_rate,
                               n_samples, channels=tuple(range(self.channels)),
                               gain_db=self.gain_db)
        if x is None:
            raise RuntimeError("plutosdr SoapySDR capture returned no samples")
        if isinstance(x, list):
            data = np.stack([np.asarray(c, dtype=np.complex64) for c in x], axis=0)
        else:
            data = np.asarray(x, dtype=np.complex64)[np.newaxis, :]
        self._seq += 1
        return IqFrame(
            samples=data, sample_rate_hz=self.sample_rate, center_freq_hz=self.center_hz,
            capture_time_ns=time.time_ns(), channels=data.shape[0],
            gain_db=self.gain_db, sequence=self._seq,
            metadata={"driver": "plutosdr", "backend": "soapy", "uri": self.uri,
                      "coherent": data.shape[0] >= 2},
        )
