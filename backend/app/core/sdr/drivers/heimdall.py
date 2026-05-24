# SPDX-License-Identifier: MIT OR Apache-2.0
# Copyright (c) 2026 Ares

"""
KrakenSDR / HeIMDALL DAQ driver.

KrakenSDR ships a 5-element coherent receiver based on RTL-SDRs and the
HeIMDALL DAQ firmware (https://github.com/krakenrf/heimdall_daq_fw). HeIMDALL's
IQ server publishes coherent IQ frames over TCP (default port 5000): the client
sends the ASCII request ``IQDownload``, the server replies with a 1024-byte
``IQ Header`` followed by ``active_ant_chs × cpi_length × complex64`` payload.

This implements the real wire format from the public reference
(``krakensdr_doa/_signal_processing/iq_header.py`` v6 / ``heimdall_daq_fw``):
the 1024-byte header layout, the 0x2bf7b95a sync word, the ``IQDownload``
pull handshake, and CF32 / CINT8 payloads. Frame *retuning* over the IQ-server
socket isn't part of that protocol — the DAQ's own control interface (FIFO on
the Pi, or the krakensdr_doa web/ZMQ API) governs centre freq / rate / gain —
so set_frequency/rate/gain update local state and are best-effort only.

The synthetic fallback engages when the DAQ is unreachable, so the rest of the
pipeline stays testable offline. Needs real KrakenSDR hardware to validate.
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


# HeIMDALL IQ-header (binary, little-endian, no alignment padding) — the public
# v6 layout from krakensdr_doa/_signal_processing/iq_header.py. Total 1024 bytes:
#   II         sync_word, frame_type
#   16s        hardware_id
#   III        unit_id, active_ant_chs, ioo_type
#   QQQ        rf_center_freq, adc_sampling_freq, sampling_freq
#   I          cpi_length
#   Q          time_stamp
#   II         daq_block_index, cpi_index
#   Q          ext_integration_cntr
#   III        data_type, sample_bit_depth, adc_overdrive_flags
#   32I        if_gains[32]
#   IIIII      delay_sync_flag, iq_sync_flag, sync_state, noise_source_state, header_version
#   194I       reserved (pads to 1024)
_HEIMDALL_VERSION = 6
_HEIMDALL_SYNC_WORD = 0x2BF7B95A
_IQ_HEADER_FMT = "<II16sIIIQQQIQIIQIII" + "I" * 32 + "IIIII" + "I" * 194
_IQ_HEADER_SIZE = 1024
_FRAME_TYPE_DATA = 0
_IQ_REQUEST = b"IQDownload"
assert struct.calcsize(_IQ_HEADER_FMT) == _IQ_HEADER_SIZE, struct.calcsize(_IQ_HEADER_FMT)


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
        # Pull handshake: request → 1024-byte header → payload. The DAQ fixes the
        # frame length (cpi_length), so we ignore n_samples and return one CPI.
        # Skip non-DATA frames (dummy / cal / ramp) until a data frame arrives.
        for _ in range(8):
            self._sock.sendall(_IQ_REQUEST)
            header = self._recv_exactly(_IQ_HEADER_SIZE)
            frame = self._decode_frame(header)
            if frame is not None:
                return frame
        raise OSError("heimdall: no DATA frame after 8 requests")

    # ── helpers ────────────────────────────────────────────────────────────
    def _recv_exactly(self, n: int) -> bytes:
        buf = bytearray()
        while len(buf) < n:
            chunk = self._sock.recv(n - len(buf))
            if not chunk:
                raise OSError("heimdall socket closed mid-frame")
            buf.extend(chunk)
        return bytes(buf)

    def _decode_frame(self, header: bytes) -> Optional[IqFrame]:
        """Parse the HeIMDALL IQ Header v6 + pull the payload. Returns None for
        non-DATA frames (caller re-requests)."""
        f = struct.unpack(_IQ_HEADER_FMT, header)
        sync_word, frame_type = f[0], f[1]
        if sync_word != _HEIMDALL_SYNC_WORD:
            raise OSError(f"heimdall: bad sync word 0x{sync_word:08X} "
                          f"(expected 0x{_HEIMDALL_SYNC_WORD:08X})")
        hardware_id = f[2].split(b"\x00", 1)[0].decode("ascii", "replace")
        unit_id, active_ant, ioo_type = f[3], f[4], f[5]
        rf_freq, adc_fs, samp_fs = f[6], f[7], f[8]
        cpi_length = f[9]
        time_stamp = f[10]
        daq_block_index, cpi_index = f[11], f[12]
        data_type, sample_bit_depth = f[14], f[15]
        adc_overdrive = f[16]
        header_version = f[53]

        if frame_type != _FRAME_TYPE_DATA:
            # Drain the payload of this non-data frame so the stream stays aligned.
            if cpi_length and active_ant:
                self._recv_exactly(active_ant * cpi_length * self._payload_stride(sample_bit_depth))
            return None
        if header_version != _HEIMDALL_VERSION:
            log.debug("heimdall header version %d (driver targets %d)", header_version, _HEIMDALL_VERSION)

        n_chans = int(active_ant) or self.channels
        stride = self._payload_stride(sample_bit_depth)
        payload = self._recv_exactly(n_chans * int(cpi_length) * stride)
        if sample_bit_depth == 8:
            # CINT8: interleaved int8 I/Q, unsigned-biased (DC at 127.5 like RTL-SDR).
            ints = np.frombuffer(payload, dtype=np.uint8).astype(np.float32)
            iq = ((ints - 127.5) / 127.5).view(np.float32)
            raw = (iq[0::2] + 1j * iq[1::2]).astype(np.complex64).reshape(n_chans, int(cpi_length))
        else:
            raw = np.frombuffer(payload, dtype=np.complex64).reshape(n_chans, int(cpi_length))

        self._seq += 1
        return IqFrame(
            samples=raw,
            sample_rate_hz=float(samp_fs),
            center_freq_hz=float(rf_freq),
            capture_time_ns=time.time_ns(),
            channels=n_chans,
            gain_db=self.gain_db,
            sequence=self._seq,
            metadata={"driver": "heimdall", "cpi_index": int(cpi_index),
                       "daq_block_index": int(daq_block_index),
                       "hardware_id": hardware_id, "unit_id": int(unit_id),
                       "adc_overdrive": int(adc_overdrive), "ioo_type": int(ioo_type),
                       "timestamp_ms": int(time_stamp), "adc_sampling_freq": float(adc_fs),
                       "coherent": n_chans >= 2, "needs_phase_cal": n_chans >= 2},
        )

    @staticmethod
    def _payload_stride(sample_bit_depth: int) -> int:
        """Bytes per complex sample: 2 for CINT8, 8 for CF32 (default)."""
        return 2 if sample_bit_depth == 8 else 8
