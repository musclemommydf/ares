"""
Ares — cellular / WiFi / BLE passive monitor routes.

GET    /cellular/capabilities                what's installed + which generations are decodable
POST   /cellular/start                       start a GSM / LTE / NR / UMTS / WiFi / BLE session
GET    /cellular/sessions                    list active sessions
GET    /cellular/sessions/{sid}              detail
GET    /cellular/sessions/{sid}/events       recent events (poll alternative to WS)
DELETE /cellular/sessions/{sid}              stop a session

The actual decoders live in ``app.core.sdr.cellular`` (GSM via in-process
gr-gsm flowgraph; LTE/NR via subprocess) and ``app.core.sdr.wifi_bt``
(hcxdumptool / airodump-ng / btmon subprocesses). All decoders are strictly
passive — no active probing, no decryption.
"""
from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.core.auth import require_auth
from app.core.sdr import cellular
from app.core.sdr import sdr_manager

log = logging.getLogger(__name__)
router = APIRouter(tags=["cellular"], prefix="/cellular")


# ─────────────────────────────────────────────────────────────────────────────
@router.get("/capabilities")
async def capabilities(_auth: dict = Depends(require_auth)):
    return cellular.capabilities()


# ─────────────────────────────────────────────────────────────────────────────
class StartCellularRequest(BaseModel):
    device_id: Optional[str] = None       # for cellular: which SDR to use
    interface: Optional[str] = None       # for wifi/ble: which kernel iface
    kind: str = Field(..., pattern=r"^(gsm|umts|lte|nr|wifi|ble)$")
    frequency_hz: Optional[float] = Field(None, gt=0)
    bandwidth_hz: Optional[float] = Field(None, gt=0)
    sample_rate_hz: Optional[float] = Field(None, gt=0)
    gain: Optional[float] = None
    channel: Optional[int] = None
    scs_khz: Optional[int] = Field(None, ge=15, le=240)


@router.post("/start")
async def start(body: StartCellularRequest, _auth: dict = Depends(require_auth)):
    """Start a passive monitor session. For cellular kinds a frequency_hz
    is required; for wifi/ble an interface (e.g. ``wlan0mon`` / ``hci0``) is
    required."""
    kwargs: dict = {}
    if body.kind in ("gsm", "umts", "lte", "nr"):
        if body.frequency_hz is None:
            raise HTTPException(400, f"frequency_hz is required for {body.kind} sessions")
        dev = None
        if body.device_id:
            d = sdr_manager.get(body.device_id)
            if d is None:
                raise HTTPException(404, f"no such SDR device {body.device_id!r}")
            dev = d.public()
        if body.bandwidth_hz is not None:    kwargs["bandwidth_hz"] = body.bandwidth_hz
        if body.sample_rate_hz is not None:  kwargs["sample_rate_hz"] = body.sample_rate_hz
        if body.gain is not None:            kwargs["gain"] = body.gain
        if body.scs_khz is not None:         kwargs["scs_khz"] = body.scs_khz
        try:
            sess = cellular.start_decoder(body.kind, dev, body.frequency_hz, **kwargs)
        except Exception as e:
            raise HTTPException(400, f"start failed: {e}")
    else:
        # wifi / ble — interface required
        if not body.interface:
            raise HTTPException(400, f"interface is required for {body.kind} sessions")
        if body.channel is not None:
            kwargs["channel"] = body.channel
        try:
            sess = cellular.start_decoder(body.kind, device=None, frequency_hz=0.0,
                                            interface=body.interface, **kwargs)
        except Exception as e:
            raise HTTPException(400, f"start failed: {e}")
    if sess.error:
        # Decoder couldn't bring up its backend (e.g. GR missing). Surface it
        # to the caller; the session record persists so the operator can
        # inspect the failure detail via /sessions/{sid}.
        return {"sid": sess.sid, "error": sess.error, "status": sess.status()}
    return {"sid": sess.sid, "status": sess.status()}


@router.get("/sessions")
async def list_sessions(_auth: dict = Depends(require_auth)):
    return {"sessions": cellular.list_sessions()}


@router.get("/sessions/{sid}")
async def session_detail(sid: str, _auth: dict = Depends(require_auth)):
    s = cellular.get_session(sid)
    if s is None:
        raise HTTPException(404, f"no such session {sid}")
    return s.status()


@router.get("/sessions/{sid}/events")
async def session_events(sid: str, since: int = 0, limit: int = 200,
                            _auth: dict = Depends(require_auth)):
    s = cellular.get_session(sid)
    if s is None:
        raise HTTPException(404, f"no such session {sid}")
    return {"events": s.recent_events(since_seq=since, limit=limit),
            "last_seq": (s.events[-1]["seq"] if s.events else 0)}


@router.delete("/sessions/{sid}")
async def session_stop(sid: str, _auth: dict = Depends(require_auth)):
    ok = cellular.stop_session(sid)
    if not ok:
        raise HTTPException(404, f"no such session {sid}")
    return {"stopped": True, "sid": sid}
