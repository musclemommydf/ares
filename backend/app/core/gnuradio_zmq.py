# SPDX-License-Identifier: MIT OR Apache-2.0
# Copyright (c) 2026 Ares

"""
GNU Radio ↔ Ares bridge over ZeroMQ.

GR's `zeromq` module provides PUB / SUB / PUSH / PULL blocks
(`zmq_pub_sink`, `zmq_sub_source`) that move complex IQ between a flowgraph
and any external process. This is the cleanest way to combine Ares's bundled
DSP with custom GR processing (operator's own demods, channelisers, signal
ML, etc.) without forking either codebase.

Two modes:

  Source:  Ares pulls IQ from a GR flowgraph (e.g. a custom demod chain
           producing complex baseband at a target frequency). Use this if
           the operator's hardware is owned by a GR flowgraph and Ares should
           act as the analysis backend.

  Sink:    Ares pushes its captured IQ to a GR flowgraph (e.g. for additional
           filtering / decimation / visualisation that lives in GR). Use this
           if Ares owns the SDR and GR is doing post-processing.

ZeroMQ is optional (`pip install pyzmq`). When unavailable, the helpers
raise `ImportError`; the rest of Ares keeps working.
"""

from __future__ import annotations

import logging
from typing import Iterator, Optional

import numpy as np

log = logging.getLogger(__name__)


def _require_zmq():
    try:
        import zmq                                          # noqa: F401
        return zmq
    except ImportError as e:
        raise ImportError("pyzmq not installed (pip install pyzmq) — GR bridge unavailable") from e


class ZmqIqSource:
    """SUB / PULL socket that receives interleaved complex64 samples from GR.
    `address` is a ZMQ endpoint, e.g. 'tcp://192.168.1.10:5555'."""

    def __init__(self, address: str, *, sub: bool = True, topic: bytes = b""):
        zmq = _require_zmq()
        self._ctx = zmq.Context.instance()
        self._sock = self._ctx.socket(zmq.SUB if sub else zmq.PULL)
        if sub:
            self._sock.setsockopt(zmq.SUBSCRIBE, topic)
        self._sock.connect(address)
        self._address = address
        self._sub = sub

    def recv(self, *, timeout_ms: int = 500) -> Optional[np.ndarray]:
        zmq = _require_zmq()
        if self._sock.poll(timeout=timeout_ms) == 0:
            return None
        raw = self._sock.recv(copy=True)
        # GR ships complex32 = interleaved float32 I, Q.
        arr = np.frombuffer(raw, dtype=np.complex64)
        return arr.copy()

    def stream(self, *, timeout_ms: int = 500) -> Iterator[np.ndarray]:
        while True:
            chunk = self.recv(timeout_ms=timeout_ms)
            if chunk is None:
                continue
            yield chunk

    def close(self) -> None:
        try: self._sock.close()
        except Exception: pass


class ZmqIqSink:
    """PUB / PUSH socket that sends interleaved complex64 to a GR flowgraph."""

    def __init__(self, address: str, *, pub: bool = True):
        zmq = _require_zmq()
        self._ctx = zmq.Context.instance()
        self._sock = self._ctx.socket(zmq.PUB if pub else zmq.PUSH)
        self._sock.bind(address)
        self._address = address

    def send(self, iq: np.ndarray) -> None:
        if iq.dtype != np.complex64:
            iq = iq.astype(np.complex64)
        self._sock.send(iq.tobytes(), copy=True)

    def close(self) -> None:
        try: self._sock.close()
        except Exception: pass


def status() -> dict:
    """Probe pyzmq availability for the /df/gnuradio endpoint."""
    try:
        import zmq
        return {"pyzmq": True, "zmq_version": zmq.zmq_version(), "pyzmq_version": zmq.pyzmq_version()}
    except ImportError:
        return {"pyzmq": False, "note": "install pyzmq to enable the GR bridge"}
