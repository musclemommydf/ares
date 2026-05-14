"""
KrakenSDR / HeIMDALL DAQ driver.

KrakenSDR ships a 5-element coherent receiver based on RTL-SDRs and the
HeIMDALL DAQ firmware (https://github.com/krakenrf/heimdall_daq_fw). HeIMDALL
publishes coherent IQ frames over TCP (default port 5000) with a fixed-format
header followed by `n_channels × n_samples × complex_float32` payload.

This driver is a CLEAR-STUB: the frame protocol and exact byte layout depend
on the running HeIMDALL version and DAQ configuration (`daq_chain_config.ini`
on the Pi 4/5). The synthetic fallback below produces realistic frames so the
rest of the pipeline is testable. Replace `_decode_frame` with the real
HeIMDALL framing once you have the hardware to validate against.

Reference for the real protocol:
  - heimdall_daq_fw/Firmware/daq_core/iq_server.c (frame header + payload)
  - krakensdr_doa/_signal_processing/iq_header.py
"""

from __future__ import annotations

import socket
import struct
import time
import logging
from typing import Optional

import numpy as np

from ...df.arrays import ArrayGeometry
from .base import DriverCapabilities, IqFrame, SdrDriver
from .synthetic import SyntheticDriver

log = logging.getLogger(__name__)


# HeIMDALL IQ-header (binary, little-endian) — fields below match
# heimdall_daq_fw/Firmware/daq_core/types.h IQ Header v6. If the firmware
# version ticks the layout, bump _HEIMDALL_VERSION and revise.
_HEIMDALL_VERSION = 6
_IQ_HEADER_FMT = "<I I I I I I Q f f I I"   # 11 fields, 56 bytes; pad to 1024 per spec
_IQ_HEADER_SIZE = 1024


def _try_connect(host: str, port: int, timeout: float = 1.0) -> Optional[socket.socket]:
    try:
        s = socket.create_connection((host, port), timeout=timeout)
        s.settimeout(2.0)
        return s
    except OSError as e:
        log.debug("heimdall connect %s:%s failed: %s", host, port, e)
        return None


class HeimdallDriver(SdrDriver):
    """HeIMDALL DAQ driver. Falls back to synthetic if the DAQ is unreachable."""

    capabilities = DriverCapabilities(
        name="KrakenSDR (HeIMDALL DAQ)",
        driver_id="heimdall",
        coherent=True,
        max_channels=5,
        sample_rate_range_hz=(0.25e6, 2.56e6),
        tunable_range_hz=(24e6, 1766e6),
        gain_range_db=(0, 50),
        iq_capture=True,
        notes="Requires heimdall_daq_fw running on the Pi. Default TCP port 5000.",
    )

    def __init__(self, host: str = "127.0.0.1", port: int = 5000, channels: int = 5,
                 array: Optional[ArrayGeometry] = None):
        self.host = host
        self.port = port
        self.channels = channels
        self.array = array or ArrayGeometry.uca(channels, 0.058)
        self._sock: Optional[socket.socket] = None
        self._fallback: Optional[SyntheticDriver] = None
        self._seq = 0
        self.center_hz = 433.92e6
        self.sample_rate = 2.4e6
        self.gain_db = 30.0

    # ── lifecycle ──────────────────────────────────────────────────────────
    def open(self) -> None:
        self._sock = _try_connect(self.host, self.port)
        if self._sock is None:
            log.warning("heimdall DAQ not reachable at %s:%d — using synthetic fallback "
                        "(replace _decode_frame() when the real hw is available).",
                        self.host, self.port)
            self._fallback = SyntheticDriver(channels=self.channels, array=self.array)
            self._fallback.open()
        else:
            log.info("heimdall DAQ connected at %s:%d", self.host, self.port)

    def close(self) -> None:
        if self._sock:
            try: self._sock.close()
            except OSError: pass
            self._sock = None
        if self._fallback:
            self._fallback.close()
            self._fallback = None

    def set_frequency(self, freq_hz: float) -> None:
        self.center_hz = float(freq_hz)
        if self._fallback: self._fallback.set_frequency(freq_hz)
        # Real impl: send CFREQ command frame to DAQ control socket (separate port).
        # TODO(hw): implement when KrakenSDR is connected for validation.

    def set_sample_rate(self, rate_hz: float) -> None:
        self.sample_rate = float(rate_hz)
        if self._fallback: self._fallback.set_sample_rate(rate_hz)
        # TODO(hw): set DAQ sample rate via control channel.

    def set_gain(self, gain_db: float) -> None:
        self.gain_db = float(gain_db)
        if self._fallback: self._fallback.set_gain(gain_db)
        # TODO(hw): apply per-channel gain via DAQ control channel.

    # ── frame I/O ──────────────────────────────────────────────────────────
    def _read_iq_impl(self, n_samples: int) -> IqFrame:
        if self._fallback is not None:
            return self._fallback.read_iq(n_samples)
        if self._sock is None:
            raise RuntimeError("driver not opened")
        # The wire protocol is request-then-pull or push-only depending on DAQ
        # config. We implement the push-only variant; for pull, send a one-byte
        # 'R' before recv. TODO(hw): confirm against the running firmware.
        header = self._recv_exactly(_IQ_HEADER_SIZE)
        frame = self._decode_frame(header, n_samples)
        return frame

    # ── helpers ────────────────────────────────────────────────────────────
    def _recv_exactly(self, n: int) -> bytes:
        buf = bytearray()
        while len(buf) < n:
            chunk = self._sock.recv(n - len(buf))
            if not chunk:
                raise OSError("heimdall socket closed mid-frame")
            buf.extend(chunk)
        return bytes(buf)

    def _decode_frame(self, header: bytes, n_samples: int) -> IqFrame:
        """Parse HeIMDALL IQ Header v6 and pull complex_float32 samples.
        TODO(hw): the exact field order below is the v6 reference layout from
        heimdall_daq_fw/types.h — verify against your firmware build.
        """
        unpacked = struct.unpack(_IQ_HEADER_FMT, header[: struct.calcsize(_IQ_HEADER_FMT)])
        (
            sync_word, frame_type, hardware_id, unit_id, active_ant, header_version,
            cpi_index, sample_rate, rf_freq, n_chans, samples_per_chan,
        ) = unpacked
        if header_version != _HEIMDALL_VERSION:
            raise OSError(f"unexpected HeIMDALL header version {header_version}")
        bytes_per_sample = 8                          # complex64 (2 × float32)
        total = n_chans * samples_per_chan * bytes_per_sample
        payload = self._recv_exactly(total)
        raw = np.frombuffer(payload, dtype=np.complex64).reshape(n_chans, samples_per_chan)
        self._seq += 1
        return IqFrame(
            samples=raw,
            sample_rate_hz=float(sample_rate),
            center_freq_hz=float(rf_freq),
            capture_time_ns=time.time_ns(),
            channels=int(n_chans),
            gain_db=self.gain_db,
            sequence=self._seq,
            metadata={"driver": "heimdall", "cpi_index": int(cpi_index),
                       "hardware_id": int(hardware_id), "unit_id": int(unit_id)},
        )
