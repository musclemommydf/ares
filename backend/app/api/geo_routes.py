# SPDX-License-Identifier: MIT OR Apache-2.0
# Copyright (c) 2026 Ares

"""
Ares — geolocation routes (Workstream C / D).

POST /api/v1/geolocate/fix            Lines-of-Bearing → ML Cut/Fix → covariance error ellipse + GeoJSON.
POST /api/v1/geolocate/multilaterate  TDOA (± FDOA) from ≥3 receivers → hyperbolic fix + error ellipse + GeoJSON.

Shared by the web app, mobile app and the ATAK plugin. With ``options.terrain_aware``
each bearing without an explicit ``estimated_distance_m`` is capped using the
propagation engine — a terrain radial from the observer along the bearing finds the
distance at which the modelled signal crosses the observed RSSI (the same maths as
``POST /api/v1/lob/range_estimate``). This is the "Ares does what SOOTHSAYER can't"
bit: DF bearings that respect mountains.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.core.auth import require_auth
from app.core.geolocation import solve_fix

log = logging.getLogger(__name__)
router = APIRouter(tags=["geolocation"])


class Observation(BaseModel):
    lat: float = Field(..., ge=-90, le=90)
    lon: float = Field(..., ge=-180, le=180)
    azimuth_deg: float = Field(..., ge=0, le=360)
    frequency_hz: float = Field(..., gt=0)
    rssi_dbm: float = -80.0
    tx_power_dbm: float = 30.0
    confidence_pct: float = Field(80.0, ge=0, le=100)
    observer_height_m: float = 1.5
    environment: str = "suburban"
    clutter_height_m: float = 0.0
    device_type: str = ""
    device_id: str = ""
    time: str = ""
    estimated_distance_m: float = 0.0  # terrain-aware cap if known; 0 ⇒ derive (RSSI model, or terrain if requested)
    id: Optional[str] = None


class FixOptions(BaseModel):
    rx_hpbw_deg: Optional[float] = None      # receiver -3 dB beamwidth → widens the CEP
    lob_length_m: Optional[float] = None     # override bearing-wedge render length
    terrain_aware: bool = False              # cap each bearing via a terrain radial (propagation engine)
    propagation_model: str = "itm"
    diffraction_model: str = "deygout"
    max_range_km: float = 150.0


class FixRequest(BaseModel):
    observations: list[Observation]
    options: FixOptions = FixOptions()


async def _terrain_cap(obs: list[dict], opt: FixOptions) -> dict:
    """Fill in estimated_distance_m for observations that lack it, using terrain radials.
    Returns a dict of {obs index: 'terrain'|'terrain_low'|'kept'} for the response."""
    from app.core.simulation import get_simulator
    sim = get_simulator()
    src: dict[int, str] = {}

    async def one(i: int, o: dict):
        if o.get("estimated_distance_m", 0) and o["estimated_distance_m"] > 0:
            src[i] = "kept"
            return
        try:
            r = await sim.compute_lob_range(
                observer_lat=o["lat"], observer_lon=o["lon"],
                observer_height_m=o.get("observer_height_m", 1.5),
                azimuth_deg=o["azimuth_deg"], frequency_hz=o["frequency_hz"],
                tx_power_dbm=o.get("tx_power_dbm", 30.0), observed_rssi_dbm=o.get("rssi_dbm", -80.0),
                propagation_model=opt.propagation_model, diffraction_model=opt.diffraction_model,
                clutter_height_m=o.get("clutter_height_m", 0.0), max_range_km=opt.max_range_km,
            )
            d = r.get("estimated_distance_m")
            if d and d > 0:
                o["estimated_distance_m"] = float(d)
                src[i] = "terrain_low" if r.get("confidence") == "low" else "terrain"
            else:
                src[i] = "rssi_fallback"
        except Exception as e:  # terrain unavailable / offline → fall back to the RSSI model in solve_fix
            log.warning("terrain LoB range failed for obs %d (%.4f,%.4f az=%.0f): %s: %s",
                        i, o["lat"], o["lon"], o["azimuth_deg"], type(e).__name__, e)
            src[i] = "rssi_fallback"

    await asyncio.gather(*(one(i, o) for i, o in enumerate(obs)))
    return src


@router.post("/geolocate/fix")
async def geolocate_fix(req: FixRequest, principal: dict = Depends(require_auth)):
    if not req.observations:
        raise HTTPException(400, "no observations supplied")
    obs = [o.dict() for o in req.observations]
    src: Optional[dict] = None
    if req.options.terrain_aware:
        src = await _terrain_cap(obs, req.options)
    try:
        result = solve_fix(obs, req.options.dict())
    except Exception as e:  # pragma: no cover - defensive
        raise HTTPException(500, f"geolocate failed: {e}")
    out = {"status": "ok", "terrain_aware": req.options.terrain_aware, **result}
    if src is not None:
        out["range_source"] = src  # per-observation: terrain | terrain_low | kept | rssi_fallback
    return out


# ── TDOA / FDOA multilateration ──────────────────────────────────────────────
class TdoaReceiver(BaseModel):
    lat: float = Field(..., ge=-90, le=90)
    lon: float = Field(..., ge=-180, le=180)
    tdoa_s: float = 0.0                       # arrival-time minus the reference receiver's (s); ref's value ignored
    tdoa_sigma_s: float = 30e-9               # 1-σ TDOA measurement noise (s)
    fdoa_hz: Optional[float] = None           # frequency offset minus the reference's (Hz); requires vx/vy
    fdoa_sigma_hz: float = 10.0
    vx: float = 0.0                           # ENU velocity east (m/s) — only used for FDOA
    vy: float = 0.0                           # ENU velocity north (m/s)


class MultilaterateRequest(BaseModel):
    receivers: list[TdoaReceiver]
    ref_index: int = 0
    frequency_hz: float = 1.0e9               # carrier — needed to interpret FDOA geometrically


@router.post("/geolocate/multilaterate")
async def geolocate_multilaterate(req: MultilaterateRequest, principal: dict = Depends(require_auth)):
    if len(req.receivers) < 3:
        raise HTTPException(400, "TDOA multilateration needs ≥3 receivers")
    from app.core.multilaterate import tdoa_fdoa_fix
    recs = [{"lat": r.lat, "lon": r.lon, "vx": r.vx, "vy": r.vy} for r in req.receivers]
    tdoa = [r.tdoa_s for r in req.receivers]
    tsig = [r.tdoa_sigma_s for r in req.receivers]
    have_f = any(r.fdoa_hz is not None for r in req.receivers)
    fdoa = [r.fdoa_hz for r in req.receivers] if have_f else None
    fsig = [r.fdoa_sigma_hz for r in req.receivers] if have_f else None
    try:
        res = tdoa_fdoa_fix(recs, tdoa, tsig, fdoa_hz=fdoa, fdoa_sigma_hz=fsig,
                            freq_hz=req.frequency_hz, ref_index=req.ref_index)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:  # pragma: no cover
        raise HTTPException(500, f"multilateration failed: {e}")
    return {"status": "ok", **res}
