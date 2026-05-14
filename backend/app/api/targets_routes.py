"""
Ares — per-identifier target tracker routes.

GET    /targets                          list every tracked target snapshot
GET    /targets/{kind}/{value}           full state + observation history
GET    /targets/{kind}/{value}/range     recompute range
POST   /targets/{kind}/{value}/fix       force a position-estimate recomputation
DELETE /targets/{kind}/{value}           forget the target
POST   /targets/{kind}/{value}/observe   manual observation push (for non-SDR feeders)
GET    /targets/kinds                    catalogue of identifier kinds + Friis defaults
WS     /targets/stream                   live update feed for the UI

Every target is keyed by ``(kind, value)`` where kind is one of the entries
in ``app.core.targets.IDENTIFIER_KINDS``. All observations land in the
in-process tracker — no external service.
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field

from app.core import targets as targets_pkg
from app.core.auth import require_auth

log = logging.getLogger(__name__)
router = APIRouter(tags=["targets"], prefix="/targets")


# ─────────────────────────────────────────────────────────────────────────────
# Catalogue
# ─────────────────────────────────────────────────────────────────────────────
@router.get("/kinds")
async def list_kinds(_auth: dict = Depends(require_auth)):
    """Return the catalogue of identifier kinds + their Friis-fallback defaults."""
    return {"kinds": [
        {"id": k, **v} for k, v in targets_pkg.IDENTIFIER_KINDS.items()
    ]}


# ─────────────────────────────────────────────────────────────────────────────
# List + detail
# ─────────────────────────────────────────────────────────────────────────────
@router.get("")
async def list_targets(
    kind: Optional[str] = None,
    family: Optional[str] = None,
    min_obs: int = 1,
    _auth: dict = Depends(require_auth),
):
    tgs = targets_pkg.tracker.query(kind=kind, family=family, min_obs=min_obs)
    return {"targets": [t.to_dict() for t in tgs], "n": len(tgs)}


@router.get("/{kind}/{value}")
async def get_target(kind: str, value: str, include_history: bool = True,
                       _auth: dict = Depends(require_auth)):
    t = targets_pkg.tracker.get(kind, value)
    if t is None:
        raise HTTPException(404, f"no such target {kind}/{value}")
    hist = targets_pkg.tracker.history(kind, value) if include_history else None
    return t.to_dict(include_history=include_history, history=hist)


# ─────────────────────────────────────────────────────────────────────────────
# Range / fix recompute
# ─────────────────────────────────────────────────────────────────────────────
@router.get("/{kind}/{value}/range")
async def target_range(kind: str, value: str, _auth: dict = Depends(require_auth)):
    t = targets_pkg.tracker.get(kind, value)
    if t is None:
        raise HTTPException(404, f"no such target {kind}/{value}")
    hist = targets_pkg.tracker.history(kind, value)
    return targets_pkg.estimate_range(t, hist)


@router.post("/{kind}/{value}/fix")
async def target_fix(kind: str, value: str, _auth: dict = Depends(require_auth)):
    """Force a fresh position-estimate computation over the target's history."""
    t = targets_pkg.tracker.recompute(kind, value)
    if t is None:
        raise HTTPException(404, f"no such target {kind}/{value}")
    return t.to_dict(include_history=False)


@router.delete("/{kind}/{value}")
async def target_forget(kind: str, value: str, _auth: dict = Depends(require_auth)):
    ok = targets_pkg.tracker.forget(kind, value)
    if not ok:
        raise HTTPException(404, f"no such target {kind}/{value}")
    return {"forgotten": True, "kind": kind, "value": value}


# ─────────────────────────────────────────────────────────────────────────────
# Manual observation push (for tooling / unit tests / external feeders)
# ─────────────────────────────────────────────────────────────────────────────
class ObservationPush(BaseModel):
    observer_lat: float
    observer_lon: float
    rssi_dbm: Optional[float] = None
    bearing_deg: Optional[float] = Field(None, ge=0, le=360)
    sigma_deg: Optional[float] = None
    frequency_hz: Optional[float] = None
    doppler_hz: Optional[float] = None
    v_mps: Optional[float] = None
    t: Optional[float] = None
    metadata: dict[str, Any] = Field(default_factory=dict)


@router.post("/{kind}/{value}/observe")
async def push_observation(kind: str, value: str, body: ObservationPush,
                              _auth: dict = Depends(require_auth)):
    t = targets_pkg.record(
        kind, value,
        body.observer_lat, body.observer_lon,
        rssi_dbm=body.rssi_dbm,
        bearing_deg=body.bearing_deg, sigma_deg=body.sigma_deg,
        frequency_hz=body.frequency_hz,
        doppler_hz=body.doppler_hz, v_mps=body.v_mps,
        t=body.t, metadata=body.metadata,
    )
    return t.to_dict()


# ─────────────────────────────────────────────────────────────────────────────
# WebSocket — live updates
# ─────────────────────────────────────────────────────────────────────────────
@router.websocket("/stream")
async def stream(ws: WebSocket):
    await ws.accept()
    loop = asyncio.get_event_loop()
    queue: asyncio.Queue = asyncio.Queue(maxsize=1000)

    def _on_event(payload: dict) -> None:
        try:
            loop.call_soon_threadsafe(queue.put_nowait, payload)
        except Exception:
            pass

    targets_pkg.register_listener(_on_event)
    try:
        # Initial snapshot
        await ws.send_text(json.dumps({"event": "snapshot", "targets": targets_pkg.snapshot()}))
        while True:
            payload = await queue.get()
            await ws.send_text(json.dumps(payload))
    except WebSocketDisconnect:
        pass
    except Exception as e:
        log.exception("targets ws crashed: %s", e)
    finally:
        targets_pkg.unregister_listener(_on_event)
