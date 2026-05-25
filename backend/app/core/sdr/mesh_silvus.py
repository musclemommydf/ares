# SPDX-License-Identifier: MIT OR Apache-2.0
# Copyright (c) 2026 Ares

"""
mesh_silvus.py — Silvus StreamCaster auto-peering (Track D, D2.3).

A Silvus StreamCaster is an *IP* MANET radio, so the existing WebSocket peer
mesh (:mod:`app.core.sdr.mesh`) already runs over it once you add the peer URLs
by hand. This adapter removes the manual step: it polls the radio's local
JSON-RPC API for the current neighbour list + link quality and keeps the Ares
peer set in sync — peers appear/disappear with the RF mesh, and link SNR is
surfaced for the UI.

The Silvus API is a JSON-RPC POST to ``http://<radio-ip>/streamscape_api`` with
methods like ``routing_table`` / ``network_status``. Network access is isolated
in :meth:`_fetch` so the parsing + sync logic is unit-testable with a stub (see
``test_mesh_silvus``).
"""
from __future__ import annotations

import json
import logging
from typing import Any, Optional
from urllib import request as _urlrequest

log = logging.getLogger(__name__)


class SilvusAdapter:
    def __init__(self, radio_ip: str, peer_port: int = 8000, scheme: str = "http",
                 timeout_s: float = 4.0) -> None:
        self.radio_ip = radio_ip
        self.peer_port = peer_port          # the Ares backend port on each peer node
        self.scheme = scheme                # how peers' Ares UIs are reached
        self.timeout_s = timeout_s
        self._api = f"http://{radio_ip}/streamscape_api"

    # ── network (isolated for testability) ───────────────────────────────────
    def _fetch(self, method: str, params: Optional[list] = None) -> Any:
        body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method,
                            "params": params or []}).encode()
        req = _urlrequest.Request(self._api, data=body,
                                  headers={"Content-Type": "application/json"})
        with _urlrequest.urlopen(req, timeout=self.timeout_s) as resp:
            return json.loads(resp.read().decode())

    # ── parsing ──────────────────────────────────────────────────────────────
    @staticmethod
    def _parse_nodes(payload: Any) -> list[dict]:
        """Normalise a Silvus routing/network response into
        ``[{ip, snr_db, hops}]``. Tolerant of the API's list-of-rows shape."""
        rows = payload.get("result", payload) if isinstance(payload, dict) else payload
        out: list[dict] = []
        for r in (rows or []):
            if not isinstance(r, dict):
                continue
            ip = r.get("ip") or r.get("ip_address") or r.get("nodeId") or r.get("node_ip")
            if not ip:
                continue
            snr = r.get("snr") or r.get("snr_db") or r.get("rssi")
            hops = r.get("hops") or r.get("hop_count")
            try:
                snr = float(snr) if snr is not None else None
            except (TypeError, ValueError):
                snr = None
            out.append({"ip": str(ip), "snr_db": snr,
                        "hops": int(hops) if isinstance(hops, (int, float)) else None})
        return out

    def neighbours(self) -> list[dict]:
        """Current RF neighbours with link quality (best-effort; [] on error)."""
        try:
            return self._parse_nodes(self._fetch("routing_table"))
        except Exception as e:
            log.debug("silvus routing_table failed: %s", e)
            return []

    def peer_url(self, ip: str) -> str:
        return f"{self.scheme}://{ip}:{self.peer_port}"

    # ── sync into the Ares peer mesh ─────────────────────────────────────────
    def sync_peers(self, peer_mesh, self_ips: Optional[set[str]] = None) -> dict:
        """Add Ares peers for every Silvus neighbour (minus our own IPs); never
        removes manually-added peers — only ones this adapter introduced."""
        self_ips = self_ips or set()
        nbrs = self.neighbours()
        desired = {self.peer_url(n["ip"]) for n in nbrs if n["ip"] not in self_ips}
        existing = set(peer_mesh.list_peers())
        added = []
        for url in desired - existing:
            try:
                peer_mesh.add_peer(url)
                added.append(url)
            except Exception:
                log.debug("add_peer failed for %s", url, exc_info=True)
        return {"neighbours": nbrs, "added": added,
                "links": {n["ip"]: n["snr_db"] for n in nbrs}}
