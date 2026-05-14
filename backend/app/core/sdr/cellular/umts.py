"""
UMTS (3G WCDMA) presence detector + DF-trigger.

There is no production-grade open-source decoder that recovers the UMTS
BCCH / PCH from raw IQ today — UMTS uses code-division multiplexing with
many simultaneous primary scrambling codes, and the BCCH is buried under a
spread-spectrum carrier. So we don't claim to decode UMTS. Instead:

  * Detect that a 5 MHz WCDMA carrier is present at this frequency by
    looking for the 3.84 Mchips/s cyclostationary signature in the IQ
    (autocorrelation of |IQ|² with a chip-period lag).
  * When detected, mark the cell as a target (kind=``umts_cell``) at the
    SDR's location, with the observed RSSI — this lets the operator at
    least track *that* a UMTS cell exists at this freq and run DF on it
    via the existing single-channel or array DF pipeline.

This session is in-process numpy/scipy — no GNU Radio, no external CLI.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Optional

import numpy as np

from .session import CellularSession

log = logging.getLogger(__name__)


class UmtsDetectorSession(CellularSession):
    KIND = "umts"

    WCDMA_CHIP_RATE = 3.84e6

    def __init__(self, sid: str, device, center_hz: float):
        super().__init__(sid=sid, device=device, center_hz=center_hz,
                          bandwidth_hz=5_000_000)
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        meta = (device or {}).get("metadata") or {}
        self._observer = {"lat": meta.get("lat"), "lon": meta.get("lon")}

    def _start_impl(self) -> None:
        self._thread = threading.Thread(target=self._run, name=f"umts-{self.sid}", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        # Lazy import to keep module-level import cheap
        from app.core.sdr import uas_video
        sample_rate = 6_000_000.0    # capture a bit wider than the 5 MHz channel
        n = int(sample_rate * 0.05)   # 50 ms windows
        while not self._stop_event.is_set():
            iq = uas_video._capture_iq(self.device, self.center_hz, sample_rate, n, 0)
            if iq is None or iq.size < 1024:
                time.sleep(1.0)
                continue
            try:
                score, rssi_dbm = self._wcdma_score(iq, sample_rate)
            except Exception:
                score, rssi_dbm = 0.0, None
            if score > 5.0:
                self.emit({
                    "event_kind": "umts_cell_detection",
                    "identifier_kind": "umts_cell",
                    "identifier_value": f"{self.center_hz/1e6:.3f}MHz",
                    "rssi_dbm": rssi_dbm,
                    "observer_lat": self._observer.get("lat"),
                    "observer_lon": self._observer.get("lon"),
                    "frequency_hz": self.center_hz,
                    "detection_score": score,
                    "method": "wcdma_cyclostationary",
                })
            # Cadence: one window every 2 s
            for _ in range(20):
                if self._stop_event.is_set():
                    break
                time.sleep(0.1)

    def _wcdma_score(self, iq: np.ndarray, fs: float) -> tuple[float, Optional[float]]:
        """Detect WCDMA 3.84 Mchips/s cyclostationarity.

        The squared envelope of a WCDMA signal has a strong cycle at the chip
        rate. We FFT |IQ|² (DC-blocked) and look for peaks at f_chip and
        2·f_chip. The score is (peak power / median power) summed over those
        two lines.
        """
        iq = np.asarray(iq, dtype=np.complex64)
        sq = (iq * np.conj(iq)).real.astype(np.float32)
        sq = sq - sq.mean()
        sq *= np.hanning(sq.size).astype(np.float32)
        P = np.abs(np.fft.rfft(sq)) ** 2
        freqs = np.fft.rfftfreq(sq.size, d=1.0 / fs)
        floor = float(np.median(P) + 1e-12)
        score = 0.0
        for harmonic in (1, 2):
            target = harmonic * self.WCDMA_CHIP_RATE
            idx = int(np.argmin(np.abs(freqs - target)))
            score += float(P[idx]) / floor
        rssi_dbm = float(10.0 * np.log10(float(np.mean(np.abs(iq) ** 2)) + 1e-12)) - 30.0
        return score, rssi_dbm

    def _stop_impl(self) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
