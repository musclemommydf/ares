# SPDX-License-Identifier: MIT OR Apache-2.0
# Copyright (c) 2026 Ares

"""
mesh_transport.py — transport abstraction for the MANET (Track D, D2.1).

Today the only mesh transport is an IP WebSocket (:class:`app.core.sdr.mesh.PeerMesh`).
Low-bandwidth links (Meshtastic LoRa, D2.2) and managed IP radios (Silvus
StreamCaster, D2.3) plug in here: each :class:`MeshTransport` implementation
serialises events with :mod:`app.core.sdr.mesh_codec`, puts them on the air, and
on receive calls the injected sinks — the *same* ``_on_lob`` ingest and chat
ingest the WebSocket path already uses, so a LoB that arrives over LoRa fuses
identically to one that arrives over IP.

This module is the seam the bridges build on; ``mesh.py``'s WS loop is the
reference "ip" transport and is not changed by introducing this interface.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Awaitable, Callable, Optional

LobSink = Callable[[dict], Awaitable[None]]   # async fn(lob_dict)  — e.g. PeerMesh._on_lob
ChatSink = Callable[[dict], None]             # sync  fn(chat_dict) — e.g. chat_hub.ingest_peer


class MeshTransport(ABC):
    """One physical/logical path to peer Ares nodes. Symmetric: it sends local
    LoB/chat out and delivers inbound ones to the injected sinks. Implementations
    must dedup/verify nothing themselves — the sinks (PeerMesh / chat_hub) already
    HMAC-verify and dedup, so a transport just moves bytes."""

    name: str = "transport"

    def __init__(self, on_lob: Optional[LobSink] = None,
                 on_chat: Optional[ChatSink] = None) -> None:
        self._on_lob = on_lob
        self._on_chat = on_chat

    def set_sinks(self, on_lob: LobSink, on_chat: ChatSink) -> None:
        self._on_lob, self._on_chat = on_lob, on_chat

    @abstractmethod
    async def start(self) -> None: ...

    @abstractmethod
    async def stop(self) -> None: ...

    @abstractmethod
    async def send_lob(self, lob: dict) -> None: ...

    @abstractmethod
    async def send_chat(self, msg: dict) -> None: ...

    def status(self) -> dict:
        return {"transport": self.name}
