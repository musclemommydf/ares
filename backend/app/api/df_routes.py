"""
Ares — array direction-finding routes (Workstream D).

POST /api/v1/df/aoa   antenna-array snapshot → angle of arrival (+ CRLB σ, ambiguities, spectrum)
GET  /api/v1/df/info  available methods / array builders / clutter+SGP4 backend status

Either supply inter-channel **phase differences** (the usual interferometer output)
or raw **IQ snapshots** (for MUSIC / Capon / Bartlett). Give an ``observer`` block
and the response also includes a ``geolocation.LoB``-shaped object you can drop
straight into ``POST /api/v1/geolocate/fix`` — the array's measured σ propagates
into the ML triangulation and its covariance error ellipse.
"""
from __future__ import annotations

import logging
from dataclasses import asdict
from typing import Optional

import numpy as np
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.core.auth import require_auth

log = logging.getLogger(__name__)
router = APIRouter(tags=["df"], prefix="/df")


class ArraySpec(BaseModel):
    type: str = Field("uca", pattern="^(ula|uca|custom)$")
    n: Optional[int] = None                       # element count (ula/uca)
    spacing_m: Optional[float] = None             # ula element spacing
    radius_m: Optional[float] = None              # uca radius
    along: str = "north"                          # ula axis ("north" | "east")
    positions_m: Optional[list[list[float]]] = None   # custom: N×2 or N×3 metres in local ENU
    name: str = "custom"


class ObserverSpec(BaseModel):
    lat: float = Field(..., ge=-90, le=90)
    lon: float = Field(..., ge=-180, le=180)
    heading_deg: float = 0.0                       # array boresight bearing (added to the AoA → true bearing)
    height_m: float = 1.5
    device_id: str = ""
    environment: str = "suburban"


class AoaRequest(BaseModel):
    array: ArraySpec
    frequency_hz: float = Field(..., gt=0)
    # one of:
    phases_rad: Optional[list[float]] = None       # inter-channel phase differences arg(x_i / x_ref), length N
    iq_real: Optional[list[list[float]]] = None    # IQ snapshots, real part, shape (N, K)
    iq_imag: Optional[list[list[float]]] = None    # IQ snapshots, imag part, shape (N, K)
    # options
    method: str = Field("interferometry", pattern="^(interferometry|music|capon|bartlett)$")
    ref: int = 0                                   # reference element for the phase-difference path
    n_sources: int = 1
    fb_smoothing: bool = False                     # forward-backward smoothing (ULA, coherent multipath)
    sigma_phase_deg: float = 8.0                   # assumed per-channel phase noise (interferometry CRLB)
    az_step: float = 1.0
    el_min: float = -10.0
    el_max: float = 80.0
    el_step: float = 5.0
    rssi_dbm: float = -75.0
    observer: Optional[ObserverSpec] = None


@router.get("/info")
async def df_info(principal: dict = Depends(require_auth)):
    from app.core import clutter as _clutter
    from app.core.propagation.sgp4_lib import propagation_backend
    from app.core.propagation import hf as _hf
    return {
        "methods": {
            "interferometry": "multi-baseline phase interferometry / correlative interferometry — phase inputs; ambiguity-resolved; CRLB σ",
            "music": "MUSIC super-resolution — IQ snapshots; optional FB smoothing for coherent sources",
            "capon": "Capon / MVDR adaptive beamformer — IQ snapshots",
            "bartlett": "conventional (delay-and-sum) beamformer — IQ snapshots",
        },
        "array_builders": ["ula(n, spacing_m)", "uca(n, radius_m)", "custom(positions_m: N×2|N×3 ENU metres)"],
        "notes": ["azimuth from true north (clockwise); array `heading_deg` is added to get the true bearing",
                  "ULA/horizontal-UCA → azimuth-only (a planar-horizontal array has no useful elevation observability); a vertical or 3-D array also resolves elevation",
                  "a single ULA has a front/back (mirror) azimuth ambiguity — reported in `ambiguities`"],
        "clutter_backend": _clutter.status(),
        "sgp4_backend": propagation_backend(),
        "hf_engine": _hf.external_engine_available() or "ares-itu-r-p533-style",
    }


@router.post("/aoa")
async def df_aoa(req: AoaRequest, principal: dict = Depends(require_auth)):
    from app.core.df.interferometry import (
        geometry_from_spec, aoa_interferometry, aoa_from_snapshots, aoa_to_lob,
    )
    try:
        geom = geometry_from_spec(req.array.dict())
    except (KeyError, ValueError, TypeError) as e:
        raise HTTPException(400, f"bad array spec: {e}")
    heading = req.observer.heading_deg if req.observer else None
    el_range = (req.el_min, req.el_max)
    try:
        if req.method == "interferometry":
            if req.phases_rad is None:
                raise HTTPException(400, "method 'interferometry' requires `phases_rad` (length N)")
            res = aoa_interferometry(geom, req.frequency_hz, req.phases_rad, ref=req.ref,
                                     sigma_phase_deg=req.sigma_phase_deg, az_step=req.az_step,
                                     el_range=el_range, el_step=req.el_step, observer_heading_deg=heading)
        else:
            if req.iq_real is None or req.iq_imag is None:
                raise HTTPException(400, f"method '{req.method}' requires `iq_real` and `iq_imag` (each N×K)")
            X = np.asarray(req.iq_real, dtype=float) + 1j * np.asarray(req.iq_imag, dtype=float)
            res = aoa_from_snapshots(geom, req.frequency_hz, X, method=req.method, n_sources=req.n_sources,
                                     fb_smoothing=req.fb_smoothing, az_step=req.az_step,
                                     el_range=el_range, el_step=req.el_step, observer_heading_deg=heading)
    except HTTPException:
        raise
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:  # pragma: no cover
        raise HTTPException(500, f"AoA estimation failed: {e}")
    out = asdict(res)
    if req.observer is not None:
        out["lob"] = aoa_to_lob(res, req.observer.dict(), req.frequency_hz, rssi_dbm=req.rssi_dbm)
        out["lob"]["device_id"] = req.observer.device_id or out["lob"].get("device_id", "")
    return {"status": "ok", **out}


class AoaLiveRequest(BaseModel):
    """Native (in-process) AoA: Ares captures coherent IQ straight from the named SDR (a SignalHound /
    USRP / Epiq Sidekiq / RTL-SDR enumerated by the SDR console) and runs MUSIC/Capon/Bartlett on it
    — no external SDR application. With no SDR present it falls back to a synthetic coherent block so
    the path still exercises offline (``synthetic: true`` in the response)."""
    array: ArraySpec
    frequency_hz: float = Field(..., gt=0)
    device_id: Optional[str] = None
    channels: Optional[list[int]] = None
    n_snapshots: int = Field(4096, ge=256, le=262144)
    sample_rate_hz: float = Field(2.4e6, gt=1e5, le=20e6)
    method: str = Field("music", pattern="^(music|capon|bartlett)$")
    observer: Optional[ObserverSpec] = None
    rssi_dbm: float = -75.0


@router.post("/aoa_live")
async def df_aoa_live(req: AoaLiveRequest, principal: dict = Depends(require_auth)):
    from app.core.sdr import dsp
    from app.core.sdr import sdr_manager
    # resolve the SDR device dict from the SDR console (so its driver/serial → SoapySDR args), if given
    device = None
    if req.device_id:
        for attr in ("get", "device", "get_device"):
            fn = getattr(sdr_manager, attr, None)
            if callable(fn):
                try:
                    d = fn(req.device_id)
                except Exception:
                    d = None
                if d is not None:
                    device = d.public() if hasattr(d, "public") else (d if isinstance(d, dict) else {"id": req.device_id})
                    break
        if device is None:
            device = {"id": req.device_id, "metadata": {}}
    spec = {**req.array.dict(), "sample_rate_hz": req.sample_rate_hz}
    heading = req.observer.heading_deg if req.observer else None
    res = dsp.solve_aoa_live(device, req.frequency_hz, spec, n_snapshots=req.n_snapshots,
                             channels=req.channels, method=req.method)
    if "error" in res:
        raise HTTPException(500, res["error"])
    if heading is not None and isinstance(res.get("azimuth_deg"), (int, float)):
        res["true_bearing_deg"] = (res["azimuth_deg"] + heading) % 360.0
    if req.observer is not None and isinstance(res.get("azimuth_deg"), (int, float)):
        res["lob"] = {
            "lat": req.observer.lat, "lon": req.observer.lon, "device_id": req.observer.device_id or "",
            "bearing_deg": res.get("true_bearing_deg", res["azimuth_deg"]),
            "sigma_deg": res.get("azimuth_sigma_deg"), "frequency_hz": req.frequency_hz, "rssi_dbm": req.rssi_dbm,
            "method": "array-" + req.method, "source": res.get("iq_source"),
        }
    return {"status": "ok", **res}


@router.get("/iq_backend")
async def df_iq_backend(principal: dict = Depends(require_auth)):
    """Which SDR backend the native DF / UAS-demod capture path is using, and the SDRs it can see."""
    from app.core.sdr import iq_capture
    return iq_capture.status()
