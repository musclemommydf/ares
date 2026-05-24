# SPDX-License-Identifier: MIT OR Apache-2.0
# Copyright (c) 2026 Ares

"""
chat.py — group chat over the MANET (Workstream D).

Text messaging between Ares nodes that rides the *same* mesh as the DF feed:

  * a message broadcast on this node's ``/api/v1/sdr/stream`` as ``{"type":"chat", ...}``;
  * peer nodes' :class:`PeerMesh` loops pick it up and re-ingest it (dedup by
    ``(origin_node, msg_id)``, loop-safe — a node ignores its own bounce, and a
    message propagates transitively so a *partial* mesh still delivers, with a
    hop-count cap);
  * a CoT **GeoChat** (``b-t-f``) is pushed to every configured TAK target, so
    ATAK / WinTAK clients on the bus see (and can answer) the same chat;
  * incoming GeoChat CoT (from ATAK or another Ares) is routed back in by the CoT
    listener — so it's one chat across Ares nodes *and* ATAK.

Rooms (a.k.a. channels) namespace the conversation; ``All`` is the default. A
rolling buffer per room is kept in memory (and included in the WS snapshot so a
fresh client backfills). No persistence to disk by default — it's tactical
chatter, not a record-of-message store.
"""
from __future__ import annotations

import logging
import time
import uuid
from collections import deque
from typing import Optional

log = logging.getLogger(__name__)

_BUFFER_PER_ROOM = 300
_MAX_HOPS = 8
_DEDUP_MAX = 4096


class ChatHub:
    def __init__(self) -> None:
        self._rooms: dict[str, deque] = {"All": deque(maxlen=_BUFFER_PER_ROOM)}
        self._seen: deque = deque(maxlen=_DEDUP_MAX)
        self._seen_set: set = set()

    # ── identity helpers (lazy import to avoid a circular at module load) ────
    @staticmethod
    def _node():
        try:
            from app.core.sdr.mesh import NODE_ID, NODE_LABEL
            return NODE_ID, NODE_LABEL
        except Exception:
            return "local", "ares"

    @staticmethod
    def _broadcast(event: dict) -> None:
        try:
            from app.core.sdr import sdr_manager
            sdr_manager._broadcast(event)
        except Exception:
            log.debug("chat broadcast failed", exc_info=True)

    def _room(self, name: str) -> deque:
        name = (name or "All").strip() or "All"
        return self._rooms.setdefault(name, deque(maxlen=_BUFFER_PER_ROOM))

    def rooms(self) -> list[str]:
        return sorted(self._rooms.keys())

    def recent(self, room: Optional[str] = None, limit: int = 100) -> list[dict]:
        if room:
            return list(self._room(room))[-int(limit):]
        out: list[dict] = []
        for dq in self._rooms.values():
            out.extend(dq)
        out.sort(key=lambda m: m.get("t", 0))
        return out[-int(limit):]

    def snapshot(self) -> dict:
        return {"rooms": self.rooms(), "messages": self.recent(limit=80)}

    # ── outbound (this node originates a message) ───────────────────────────
    def send(self, text: str, *, room: str = "All", callsign: str = "", lat: Optional[float] = None,
             lon: Optional[float] = None, origin_node: Optional[str] = None,
             origin_label: Optional[str] = None, msg_id: Optional[str] = None, hops: int = 0,
             from_cot: bool = False, sig: Optional[str] = None) -> dict:
        text = (text or "").strip()
        if not text:
            raise ValueError("empty message")
        nid, nlabel = self._node()
        msg = {
            "id": msg_id or uuid.uuid4().hex[:12],
            "room": (room or "All").strip() or "All",
            "from_node": origin_node or nid,
            "from_label": origin_label or (callsign or nlabel),
            "callsign": callsign or origin_label or "",
            "text": text[:2000],
            "lat": (None if lat is None else float(lat)),
            "lon": (None if lon is None else float(lon)),
            "hops": int(hops),
            "t": time.time(),
            "via": "cot" if from_cot else ("mesh" if (origin_node and origin_node != nid) else "local"),
        }
        # mesh integrity: keep a relayed message's originator signature; otherwise sign it with our secret
        try:
            from app.core import meshsec
            msg["sig"] = sig or meshsec.sign_chat(msg)
        except Exception:
            if sig:
                msg["sig"] = sig
        # local store + dedup
        self._mark_seen(msg["from_node"], msg["id"])
        self._room(msg["room"]).append(msg)
        # fan out: WS (→ web UI + the mesh, which re-ingests on peers) + CoT GeoChat (→ ATAK)
        self._broadcast({"type": "chat", "msg": msg})
        try:
            import asyncio
            from app.core import cot
            asyncio.get_running_loop()           # only when there's an event loop (i.e. in the running app)
            asyncio.create_task(cot.publish_chat(msg))
        except RuntimeError:
            pass                                  # no running loop (e.g. a unit test) — skip the CoT push
        except Exception:
            log.debug("CoT GeoChat push failed", exc_info=True)
        return msg

    # ── inbound from a mesh peer (via PeerMesh._peer_loop) ──────────────────
    def ingest_peer(self, m: dict) -> Optional[dict]:
        if not m or "text" not in m:
            return None
        nid, _ = self._node()
        origin = m.get("from_node") or "peer"
        if origin == nid:                     # our own message bounced back
            return None
        try:
            from app.core import meshsec
            if not meshsec.verify_chat(m):    # mesh integrity — reject unsigned/forged chat
                return None
        except Exception:
            pass
        mid = m.get("id") or f"{m.get('t')}:{m.get('text','')[:16]}"
        if self._is_seen(origin, mid):        # already delivered (arrived via another path)
            return None
        hops = int(m.get("hops", 0)) + 1
        if hops > _MAX_HOPS:                  # TTL — don't forward forever
            return None
        return self.send(m.get("text", ""), room=m.get("room", "All"), callsign=m.get("callsign", ""),
                         lat=m.get("lat"), lon=m.get("lon"), origin_node=origin,
                         origin_label=m.get("from_label"), msg_id=mid, hops=hops, sig=m.get("sig"))

    # ── inbound from CoT GeoChat (ATAK / another Ares) ──────────────────────
    def ingest_cot(self, *, text: str, callsign: str = "", room: str = "All",
                   sender_node: str = "", lat: Optional[float] = None, lon: Optional[float] = None,
                   msg_id: Optional[str] = None) -> Optional[dict]:
        if not (text or "").strip():
            return None
        nid, _ = self._node()
        origin = sender_node or f"atak:{callsign or 'unknown'}"
        if origin == nid:                     # our own GeoChat (we just sent it) — skip
            return None
        mid = msg_id or uuid.uuid4().hex[:12]
        if self._is_seen(origin, mid):
            return None
        # don't re-publish a CoT we received as another CoT — but DO put it on the WS + mesh
        self._mark_seen(origin, mid)
        msg = {"id": mid, "room": (room or "All").strip() or "All", "from_node": origin,
               "from_label": callsign or origin, "callsign": callsign or "", "text": text.strip()[:2000],
               "lat": (None if lat is None else float(lat)), "lon": (None if lon is None else float(lon)),
               "hops": 0, "t": time.time(), "via": "cot"}
        try:
            from app.core import meshsec
            msg["sig"] = meshsec.sign_chat(msg)    # sign it as it enters the Ares mesh, so peers accept it
        except Exception:
            pass
        self._room(msg["room"]).append(msg)
        self._broadcast({"type": "chat", "msg": msg})
        return msg

    # ── dedup bookkeeping ───────────────────────────────────────────────────
    def _mark_seen(self, origin: str, mid: str) -> None:
        k = (origin, mid)
        if k in self._seen_set:
            return
        if len(self._seen) == self._seen.maxlen:
            self._seen_set.discard(self._seen[0])
        self._seen.append(k)
        self._seen_set.add(k)

    def _is_seen(self, origin: str, mid: str) -> bool:
        return (origin, mid) in self._seen_set


chat_hub = ChatHub()
