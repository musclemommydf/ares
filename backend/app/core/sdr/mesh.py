# SPDX-License-Identifier: MIT OR Apache-2.0
# Copyright (c) 2026 Ares

"""
sdr/mesh.py — distributed multi-sensor DF over a MANET (Workstream D).

An Ares node fuses bearings from sources beyond its own SDRs:

  * **Same box** — register several SDRs on one Ares; the solver already groups
    LoBs by frequency *across devices*, so two/three antennas on one server give
    a multi-sensor Cut/Fix automatically (nothing extra needed — see
    :meth:`SDRManager._solve_and_publish`).
  * **Over a MANET** — this module. The node opens a WebSocket to each peer Ares
    node's ``/api/v1/sdr/stream``, ingests their ``lob`` events into the local
    solver (tagged with the peer's node id), and — because peers symmetrically
    subscribe to *its* stream — the union of every node's bearings is fused into
    one geolocation picture, on every node. Runs over any IP-reachable mesh (the
    same network the CoT multicast rides). Loop-safe: a node never re-ingests a
    LoB whose origin is itself, dedups by ``(origin_node, lob_id)``, and lets a
    LoB propagate transitively so a *partial* mesh still converges (full flooding
    with hop-count is a follow-up — for now A↔B↔C works, arbitrary topologies
    work as long as there's a path).

Peers: ``ARES_MESH_PEERS`` env (comma-separated node base URLs) or
``PUT /api/v1/sdr/peers``; persisted to ``data/.mesh_peers.json``. This node's id
lives in ``data/.node_id`` (a stable random hex; the hostname is the human label).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
import socket
import time
from collections import deque
from pathlib import Path
from typing import Optional

import aiohttp

from app.config import DATA_DIR

log = logging.getLogger(__name__)

_NODE_ID_FILE = DATA_DIR / ".node_id"
_PEERS_FILE = DATA_DIR / ".mesh_peers.json"


def _load_node_id() -> str:
    env = os.getenv("ARES_NODE_ID", "").strip()
    if env:
        return env
    try:
        if _NODE_ID_FILE.exists():
            v = _NODE_ID_FILE.read_text().strip()
            if v:
                return v
    except OSError:
        pass
    v = secrets.token_hex(6)
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        _NODE_ID_FILE.write_text(v)
    except OSError:
        pass
    return v


NODE_ID = _load_node_id()
try:
    NODE_LABEL = os.getenv("ARES_NODE_LABEL", "").strip() or socket.gethostname()
except Exception:
    NODE_LABEL = "ares"


def _norm_url(u: str) -> str:
    u = u.strip().rstrip("/")
    if not u:
        return ""
    if not u.startswith(("http://", "https://", "ws://", "wss://")):
        u = "http://" + u
    return u


def _ws_url(base: str) -> str:
    b = _norm_url(base)
    if b.startswith("https://"):
        b = "wss://" + b[len("https://"):]
    elif b.startswith("http://"):
        b = "ws://" + b[len("http://"):]
    url = b + "/api/v1/sdr/stream"
    try:
        from app.core import meshsec
        s = meshsec.secret()
        if s:                                  # peer nodes connect with the shared mesh secret
            from urllib.parse import quote
            url += f"?mesh_secret={quote(s)}"
    except Exception:
        pass
    return url


class PeerMesh:
    def __init__(self) -> None:
        self.node_id = NODE_ID
        self.node_label = NODE_LABEL
        self._peers: list[str] = []
        self._tasks: dict[str, asyncio.Task] = {}
        self._state: dict[str, dict] = {}    # url → {connected, node_id, label, lob_count, fix_count, last_t, error}
        self._seen: deque = deque(maxlen=4096)   # (origin_node, lob_id) — dedup
        self._seen_set: set = set()
        self._started = False
        self._on_lob = None                  # injected: async callable(LobEvent)
        self._load()

    # ── persistence ──────────────────────────────────────────────────────────
    def _load(self) -> None:
        env = os.getenv("ARES_MESH_PEERS", "").strip()
        peers: list[str] = []
        if env:
            peers = [p for p in (_norm_url(x) for x in env.split(",")) if p]
        try:
            if _PEERS_FILE.exists():
                for p in json.loads(_PEERS_FILE.read_text()).get("peers", []):
                    p = _norm_url(p)
                    if p and p not in peers:
                        peers.append(p)
        except Exception:
            log.exception("failed to read %s", _PEERS_FILE)
        self._peers = peers

    def _save(self) -> None:
        try:
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            _PEERS_FILE.write_text(json.dumps({"peers": self._peers}, indent=2))
        except OSError:
            pass

    # ── wiring ───────────────────────────────────────────────────────────────
    def set_lob_sink(self, fn) -> None:
        """Injected by the manager: ``async fn(LobEvent)`` — the local ingest path."""
        self._on_lob = fn

    # ── lifecycle ────────────────────────────────────────────────────────────
    async def start(self) -> None:
        if self._started:
            return
        self._started = True
        if self._peers:
            try:
                from app.core import meshsec
                meshsec.ensure_secret()        # peers need a shared secret to join + verify each other
            except Exception:
                pass
        for p in list(self._peers):
            self._spawn(p)
        if self._peers:
            log.info("mesh: node %s (%s) connecting to %d peer(s): %s", self.node_id, self.node_label, len(self._peers), self._peers)

    async def stop(self) -> None:
        self._started = False
        for t in list(self._tasks.values()):
            t.cancel()
        for t in list(self._tasks.values()):
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass
        self._tasks.clear()

    def _spawn(self, url: str) -> None:
        if url in self._tasks:
            return
        self._state.setdefault(url, {"connected": False, "node_id": None, "label": None,
                                     "lob_count": 0, "fix_count": 0, "last_t": 0.0, "error": ""})
        self._tasks[url] = asyncio.create_task(self._peer_loop(url), name=f"mesh:{url}")

    def _kill(self, url: str) -> None:
        t = self._tasks.pop(url, None)
        if t:
            t.cancel()
        st = self._state.get(url)
        if st:
            st["connected"] = False

    # ── peer CRUD ────────────────────────────────────────────────────────────
    def list_peers(self) -> list[str]:
        return list(self._peers)

    def add_peer(self, url: str) -> str:
        u = _norm_url(url)
        if not u:
            raise ValueError("empty peer URL")
        try:
            from app.core import meshsec
            meshsec.ensure_secret()            # adding a peer ⇒ this node now needs the shared mesh secret
        except Exception:
            pass
        if u not in self._peers:
            self._peers.append(u)
            self._save()
            if self._started:
                self._spawn(u)
        return u

    def remove_peer(self, url: str) -> bool:
        u = _norm_url(url)
        if u not in self._peers:
            return False
        self._peers.remove(u)
        self._save()
        self._kill(u)
        self._state.pop(u, None)
        return True

    def set_peers(self, urls: list[str]) -> list[str]:
        norm = [p for p in (_norm_url(x) for x in urls) if p]
        for old in [p for p in self._peers if p not in norm]:
            self._kill(old)
            self._state.pop(old, None)
        self._peers = norm
        self._save()
        if self._started:
            for p in norm:
                self._spawn(p)
        return norm

    def status(self) -> dict:
        return {"node_id": self.node_id, "node_label": self.node_label,
                "peers": [{"url": u, **self._state.get(u, {"connected": False})} for u in self._peers]}

    # ── ingest loop (per peer) ───────────────────────────────────────────────
    async def _peer_loop(self, url: str) -> None:
        backoff = 1.0
        wsurl = _ws_url(url)
        while True:
            st = self._state.setdefault(url, {"connected": False, "node_id": None, "label": None,
                                              "lob_count": 0, "fix_count": 0, "last_t": 0.0, "error": ""})
            try:
                timeout = aiohttp.ClientTimeout(total=None, sock_connect=8)
                async with aiohttp.ClientSession(timeout=timeout) as sess:
                    async with sess.ws_connect(wsurl, heartbeat=20.0) as ws:
                        st.update(connected=True, error="")
                        backoff = 1.0
                        async for msg in ws:
                            if msg.type != aiohttp.WSMsgType.TEXT:
                                if msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                                    break
                                continue
                            try:
                                m = json.loads(msg.data)
                            except Exception:
                                continue
                            t = m.get("type")
                            if t == "snapshot":
                                st["node_id"] = m.get("node_id")
                                st["label"] = m.get("node_label")
                                # backfill the recent LoBs/fixes/chat the peer already has
                                for lob in (m.get("lobs") or []):
                                    await self._ingest_lob(url, lob, st)
                                st["fix_count"] += len(m.get("fixes") or [])
                                for cm in ((m.get("chat") or {}).get("messages") or []):
                                    self._ingest_chat(cm, st)
                            elif t == "lob":
                                await self._ingest_lob(url, m.get("lob") or {}, st)
                            elif t == "fix":
                                st["fix_count"] += 1
                            elif t == "chat":
                                self._ingest_chat(m.get("msg") or {}, st)
                            st["last_t"] = time.time()
            except asyncio.CancelledError:
                st["connected"] = False
                raise
            except Exception as e:
                st.update(connected=False, error=f"{type(e).__name__}: {e}")
                await asyncio.sleep(min(30.0, backoff))
                backoff = min(30.0, backoff * 2)

    _MAX_HOPS = 8

    def _ingest_chat(self, msg: dict, st: dict) -> None:
        if not msg or "text" not in msg:
            return
        try:
            from app.core.chat import chat_hub
            r = chat_hub.ingest_peer(msg)
            if r is not None:
                st["chat_count"] = st.get("chat_count", 0) + 1
        except Exception:
            log.debug("mesh chat ingest failed", exc_info=True)

    async def _ingest_lob(self, url: str, lob: dict, st: dict) -> None:
        if not lob or "azimuth_deg" not in lob or "frequency_hz" not in lob or self._on_lob is None:
            return
        origin = lob.get("origin_node") or st.get("node_id") or url
        if origin == self.node_id:                       # our own LoB bounced back — ignore
            return
        try:
            from app.core import meshsec
            if not meshsec.verify_lob({**lob, "origin_node": origin}):   # mesh integrity — reject unsigned/forged
                st["error"] = "rejected an unsigned/forged LoB (mesh secret mismatch)"
                return
        except Exception:
            pass
        hops = int(lob.get("hops", 0)) + 1
        if hops > self._MAX_HOPS:                         # TTL — don't forward forever in a dense mesh
            return
        key = (origin, lob.get("id") or f"{lob.get('frequency_hz')}:{lob.get('t')}")
        if key in self._seen_set:                        # already fused (arrived via another path)
            return
        if len(self._seen) == self._seen.maxlen:
            old = self._seen[0]
            self._seen_set.discard(old)
        self._seen.append(key)
        self._seen_set.add(key)
        from app.core.sdr.manager import LobEvent
        try:
            ev = LobEvent(
                # keep the originator's device_id verbatim (so the signature stays valid
                # through every hop); origin_node disambiguates which sensor it was.
                device_id=str(lob.get("device_id") or f"sdr@{origin}"),
                lat=float(lob["lat"]), lon=float(lob["lon"]),
                azimuth_deg=float(lob["azimuth_deg"]) % 360.0,        # peers send Absolute LOBs
                frequency_hz=float(lob["frequency_hz"]),
                raw_azimuth_deg=float(lob["azimuth_deg"]) % 360.0,
                rssi_dbm=float(lob.get("rssi_dbm", -80.0)),
                confidence_pct=float(lob.get("confidence_pct", 80.0)),
                observer_height_m=float(lob.get("observer_height_m", 1.5)),
                environment=str(lob.get("environment", "suburban")),
                device_type=str(lob.get("device_type", "peer")),
                target_device_id=str(lob.get("target_device_id", "")),
                estimated_distance_m=float(lob.get("estimated_distance_m", 0.0)),
                t=float(lob.get("t", time.time())),
                id=str(lob.get("id") or secrets.token_hex(6)),
            )
            ev.origin_node = origin
            ev.origin_device = str(lob.get("origin_device") or lob.get("device_id") or "")
            ev.hops = hops
            ev.sig = lob.get("sig")            # carry the originator's signature so the next hop can verify it
        except Exception:
            return
        st["lob_count"] = st.get("lob_count", 0) + 1
        await self._on_lob(ev)


peer_mesh = PeerMesh()
