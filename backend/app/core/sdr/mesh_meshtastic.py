# SPDX-License-Identifier: MIT OR Apache-2.0
# Copyright (c) 2026 Ares

"""
mesh_meshtastic.py — Meshtastic (LoRa) MANET transport (Track D, D2.2).

Carries the same HMAC-signed LoB / chat events the IP WebSocket mesh carries
(:mod:`app.core.sdr.mesh`), but over a Meshtastic radio so Ares nodes share a
fused DF picture with **no IP infrastructure** — just LoRa. Events are packed
with :mod:`app.core.sdr.mesh_codec` (a LoB is ~144 B, well under a LoRa frame)
and sent on a Meshtastic *private* port; inbound frames are decoded and handed to
the same sinks the WS path uses (``PeerMesh._on_lob`` / ``chat_hub.ingest_peer``),
so a LoB that arrives over LoRa fuses identically to one over IP.

The ``meshtastic`` Python package is an optional dependency, imported lazily in
:meth:`start`. The framing + dispatch logic (``_fit`` / ``_deliver``) needs no
radio and is what ``test_mesh_meshtastic`` exercises.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Optional

from app.core.sdr import mesh_codec
from app.core.sdr.mesh_transport import ChatSink, LobSink, MeshTransport

log = logging.getLogger(__name__)

# Meshtastic Data payloads top out around 237 bytes; leave headroom.
MAX_PAYLOAD = 233
# Meshtastic PRIVATE_APP portnum (256) — both LoB and chat ride it; the codec's
# leading magic byte tells them apart on receive.
PRIVATE_APP_PORT = 256


class MeshtasticTransport(MeshTransport):
    name = "meshtastic"

    def __init__(self, port: Optional[str] = None, tcp_host: Optional[str] = None,
                 ble: Optional[str] = None,
                 on_lob: Optional[LobSink] = None, on_chat: Optional[ChatSink] = None) -> None:
        super().__init__(on_lob, on_chat)
        self.port = port            # serial device, e.g. /dev/ttyUSB0
        self.tcp_host = tcp_host    # network-attached radio
        self.ble = ble              # BLE address
        self._iface = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._dropped = 0
        self._sent = 0
        self._recv = 0

    # ── framing (no radio needed — unit-tested) ──────────────────────────────
    @staticmethod
    def _fit(frame: bytes) -> bool:
        """True if the frame fits a single LoRa payload."""
        return len(frame) <= MAX_PAYLOAD

    async def _deliver(self, payload: bytes) -> None:
        """Decode one received payload and fan it out to the sinks."""
        try:
            kind, d = mesh_codec.decode(payload)
        except ValueError:
            return
        self._recv += 1
        if kind == "lob" and self._on_lob is not None:
            await self._on_lob(d)
        elif kind == "chat" and self._on_chat is not None:
            self._on_chat(d)

    # ── lifecycle (lazy-imports the meshtastic lib) ──────────────────────────
    async def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        try:
            import meshtastic                       # noqa: F401
            from pubsub import pub
            if self.tcp_host:
                from meshtastic.tcp_interface import TCPInterface
                self._iface = TCPInterface(self.tcp_host)
            elif self.ble:
                from meshtastic.ble_interface import BLEInterface
                self._iface = BLEInterface(self.ble)
            else:
                from meshtastic.serial_interface import SerialInterface
                self._iface = SerialInterface(self.port)
            pub.subscribe(self._on_receive, "meshtastic.receive.data")
            log.info("meshtastic transport up (%s)", self.tcp_host or self.ble or self.port or "auto")
        except Exception as e:
            log.warning("meshtastic transport unavailable: %s "
                        "(install the 'meshtastic' package + attach a radio)", e)
            self._iface = None

    async def stop(self) -> None:
        if self._iface is not None:
            try:
                self._iface.close()
            except Exception:
                pass
            self._iface = None

    # ── send ─────────────────────────────────────────────────────────────────
    async def send_lob(self, lob: dict) -> None:
        self._send(mesh_codec.encode_lob(lob))

    async def send_chat(self, msg: dict) -> None:
        self._send(mesh_codec.encode_chat(msg))

    def _send(self, frame: bytes) -> None:
        if not self._fit(frame):
            self._dropped += 1
            log.warning("meshtastic: dropping %d-byte frame (> %d B LoRa limit)", len(frame), MAX_PAYLOAD)
            return
        if self._iface is None:
            return
        try:
            self._iface.sendData(frame, portNum=PRIVATE_APP_PORT)
            self._sent += 1
        except Exception:
            log.debug("meshtastic send failed", exc_info=True)

    # ── receive (meshtastic pubsub → async sink) ─────────────────────────────
    def _on_receive(self, packet=None, interface=None) -> None:
        try:
            payload = (packet or {}).get("decoded", {}).get("payload")
            if not payload or self._loop is None:
                return
            asyncio.run_coroutine_threadsafe(self._deliver(bytes(payload)), self._loop)
        except Exception:
            log.debug("meshtastic receive failed", exc_info=True)

    def status(self) -> dict:
        return {"transport": self.name, "connected": self._iface is not None,
                "sent": self._sent, "recv": self._recv, "dropped_oversize": self._dropped}
