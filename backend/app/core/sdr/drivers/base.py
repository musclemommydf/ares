# SPDX-License-Identifier: MIT OR Apache-2.0
# Copyright (c) 2026 Ares

"""
SDR driver abstraction.

Every backend driver (Soapy, UHD, HeIMDALL/Kraken, ANTSDR-e200, Matchstiq X40,
ADALM-Pluto, synthetic) implements the same SdrDriver interface. The DF pipeline only ever
sees this interface — it never imports vendor SDKs directly. Hot-swap by
changing the registry.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional

import numpy as np


@dataclass
class DriverCapabilities:
    """What a given driver can do — informs the UI which knobs to expose."""
    name: str                                   # human label
    driver_id: str                              # short id, e.g. "kraken"
    coherent: bool                              # multi-channel phase-aligned?
    max_channels: int
    sample_rate_range_hz: tuple[float, float]
    tunable_range_hz: tuple[float, float]
    gain_range_db: tuple[float, float]
    iq_capture: bool = True                     # can dump raw IQ?
    on_device_fft: bool = False                 # FPGA spectrum (RFNoC etc.)?
    on_device_doa: bool = False                 # FPGA MUSIC/beamform?
    tx_capable: bool = False                    # can transmit baseband IQ? (NIC full-duplex)
    cal_source: bool = False                    # has a switchable coherence reference (noise source) for auto-cal?
    notes: str = ""


@dataclass
class IqFrame:
    """Coherent IQ snapshot. shape = (channels, samples), dtype = complex64."""
    samples: np.ndarray
    sample_rate_hz: float
    center_freq_hz: float
    capture_time_ns: int                        # nanoseconds since unix epoch
    channels: int
    gain_db: float
    sequence: int                               # monotonic per-driver
    metadata: dict


class SdrDriver(ABC):
    """Abstract SDR driver. Concrete drivers must implement all six methods."""

    capabilities: DriverCapabilities

    @abstractmethod
    def open(self) -> None: ...

    @abstractmethod
    def close(self) -> None: ...

    @abstractmethod
    def set_frequency(self, freq_hz: float) -> None: ...

    @abstractmethod
    def set_sample_rate(self, rate_hz: float) -> None: ...

    @abstractmethod
    def set_gain(self, gain_db: float) -> None: ...

    def read_iq(self, n_samples: int) -> IqFrame:
        """Template method: call the subclass's `_read_iq_impl`, then forward
        the frame to the health monitor so /df/health surfaces clipping rate +
        last-frame age + dropouts. Subclasses implement `_read_iq_impl`.

        Backwards-compat: subclasses that historically overrode `read_iq`
        directly still work — they just bypass health instrumentation. The
        recommended override is `_read_iq_impl`.
        """
        frame = self._read_iq_impl(n_samples)
        try:
            from app.core import sdr_health  # type: ignore
        except Exception:
            sdr_health = None
        # Best-effort instrumentation; never let it disrupt the IQ path.
        if sdr_health is not None and frame is not None:
            try:
                # device_id falls back to the driver's id; multi-instance
                # drivers can override `device_id` on the instance.
                dev_id = getattr(self, "device_id", None) or self.capabilities.driver_id
                sdr_health.record_frame(dev_id, frame.samples)
            except Exception:
                pass
        return frame

    @abstractmethod
    def _read_iq_impl(self, n_samples: int) -> IqFrame:
        """Actual IQ read — implemented by each concrete driver."""
        ...

    # ── transmit (optional) ──────────────────────────────────────────────────
    # Drivers backing a true transceiver override this to put baseband IQ on the
    # air; it is what lets an SDR act as a NIC's *uplink* (see app.core.sdr.tap_nic).
    # `samples` is a 1-D complex64 baseband waveform at the driver's current
    # sample rate / centre frequency, roughly unit-amplitude (the driver scales
    # to its own full-scale). Default: not supported → receive-only NIC.
    def transmit(self, samples: np.ndarray) -> None:
        raise NotImplementedError(
            f"{self.capabilities.driver_id} is receive-only — no IQ transmit")

    # ── coherence calibration source (optional) ─────────────────────────────────
    # Coherent arrays drift in inter-channel phase/gain. Drivers whose hardware can
    # switch a common reference (a noise source coupled to every element, as the
    # KrakenSDR HeIMDALL DAQ does) override this; the auto-calibration loop in
    # app.core.sdr.live_df toggles it ON, captures a reference block to estimate the
    # per-channel correction, then toggles it OFF. Default: no source → no auto-cal.
    def set_calibration_source(self, on: bool) -> None:
        raise NotImplementedError(
            f"{self.capabilities.driver_id} has no switchable calibration source")

    # Optional capabilities — drivers that don't support these can raise NotImplementedError.
    def stream_iq(self, n_samples: int):
        """Generator yielding IqFrames at the driver's native chunk size."""
        while True:
            yield self.read_iq(n_samples)

    def estimate_psd(self, n_fft: int = 1024) -> dict:
        """On-device PSD if available (RFNoC), else read IQ + fall back to CPU FFT."""
        frame = self.read_iq(n_fft)
        fft = np.fft.fftshift(np.fft.fft(frame.samples[0], n_fft))
        psd_db = 20 * np.log10(np.maximum(np.abs(fft), 1e-12))
        freqs = np.fft.fftshift(np.fft.fftfreq(n_fft, d=1.0 / frame.sample_rate_hz)) + frame.center_freq_hz
        return {
            "freqs_hz": freqs.tolist(),
            "psd_db": psd_db.tolist(),
            "center_freq_hz": frame.center_freq_hz,
            "sample_rate_hz": frame.sample_rate_hz,
        }
