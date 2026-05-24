# SPDX-License-Identifier: MIT OR Apache-2.0
# Copyright (c) 2026 Ares

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
    type: str = Field("uca", pattern="^(ula|uca|adcock|custom)$")
    n: Optional[int] = None                       # element count (ula/uca/adcock ring)
    spacing_m: Optional[float] = None             # ula element spacing
    radius_m: Optional[float] = None              # uca / adcock ring radius
    sense: bool = True                            # adcock: add a central omni sense element (Watson-Watt 360°)
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
    method: str = Field("interferometry", pattern="^(interferometry|music|capon|bartlett|correlative|watson_watt|doppler)$")
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
            "watson_watt": "Watson-Watt / Adcock amplitude DF — IQ snapshots; crossed N-S/E-W loops + omni sense → 0–360° (ALARIS Adcock heads)",
            "correlative": "correlative DF (CDF / CIDF) — IQ snapshots; max complex-pattern correlation vs the manifold (ALARIS 3-channel correlative DF)",
            "doppler": "pseudo-Doppler / phase-mode DF — IQ snapshots; bearing from the 1st spatial Fourier mode of a circular array",
        },
        "array_builders": ["ula(n, spacing_m)", "uca(n, radius_m)",
                            "adcock(n, radius_m, sense)", "custom(positions_m: N×2|N×3 ENU metres)"],
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
    method: str = Field("music", pattern="^(music|capon|bartlett|correlative|watson_watt|doppler)$")
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


# ─────────────────────────────────────────────────────────────────────────────
# Bundled in-process DSP pipeline (replaces external krakensdr_doa daemon)
# ─────────────────────────────────────────────────────────────────────────────

class PseudoSpectrumRequest(BaseModel):
    """IQ-based pseudo-spectrum (the bundled DSP path).
    iq is an (M, N) complex matrix flattened row-major into a length-2*M*N list
    of [re, im, re, im, ...] floats. array describes the geometry."""
    iq_flat: list[float]
    n_channels: int = Field(..., ge=2, le=32)
    n_samples: int = Field(..., ge=16, le=1_048_576)
    frequency_hz: float = Field(..., gt=0)
    algorithm: str = Field("music", pattern="^(bartlett|capon|music|mem|root_music|esprit)$")
    array: ArraySpec
    n_sources: int = Field(1, ge=1, le=16)
    az_resolution_deg: float = Field(1.0, ge=0.1, le=10)
    calibration_id: Optional[str] = None


def _build_geom(spec: "ArraySpec"):
    from app.core.df.arrays import ArrayGeometry
    if spec.type == "uca":
        return ArrayGeometry.uca(spec.n_channels, getattr(spec, "radius_m", None) or
                                  (spec.spacing_wavelengths or 0.4) * (299792458.0 / 433.92e6))
    if spec.type == "ula":
        return ArrayGeometry.ula(spec.n_channels, getattr(spec, "spacing_m", None) or
                                  (spec.spacing_wavelengths or 0.5) * (299792458.0 / 433.92e6))
    pos = getattr(spec, "positions", None) or []
    return ArrayGeometry.custom(pos if pos else [[0.0, 0.0]] * max(1, spec.n_channels))


@router.post("/pseudo_spectrum")
async def df_pseudo_spectrum(req: PseudoSpectrumRequest):
    """Run MUSIC/Bartlett/Capon/MEM/root-MUSIC/ESPRIT on a flattened IQ matrix.
    This is the in-process DSP path — no krakensdr_doa daemon needed."""
    import numpy as np
    from app.core.df.algorithms import covariance_from_iq, pseudo_spectrum
    from app.core.df.calibration import apply_gain, load_calibration
    from pathlib import Path
    expected = 2 * req.n_channels * req.n_samples
    if len(req.iq_flat) != expected:
        raise HTTPException(400, f"iq_flat length {len(req.iq_flat)} != 2*M*N = {expected}")
    iq = (np.asarray(req.iq_flat[0::2], dtype=np.float32)
          + 1j * np.asarray(req.iq_flat[1::2], dtype=np.float32)).reshape(req.n_channels, req.n_samples)
    if req.calibration_id:
        cal_path = Path("data/df_state/cal") / f"{req.calibration_id}.json"
        if cal_path.exists():
            iq = apply_gain(iq, load_calibration(cal_path))
    geom = _build_geom(req.array)
    R = covariance_from_iq(iq)
    return pseudo_spectrum(R, geom, req.frequency_hz, req.algorithm,
                            n_sources=req.n_sources,
                            az_resolution_deg=req.az_resolution_deg)


class SourceCountRequest(BaseModel):
    iq_flat: list[float]
    n_channels: int = Field(..., ge=2, le=32)
    n_samples: int = Field(..., ge=16, le=1_048_576)


@router.post("/source_count")
async def df_source_count(req: SourceCountRequest):
    """AIC/MDL number-of-sources estimate."""
    import numpy as np
    from app.core.df.algorithms import covariance_from_iq
    from app.core.df.source_count import aic_mdl
    expected = 2 * req.n_channels * req.n_samples
    if len(req.iq_flat) != expected:
        raise HTTPException(400, f"iq_flat length {len(req.iq_flat)} != 2*M*N = {expected}")
    iq = (np.asarray(req.iq_flat[0::2], dtype=np.float32)
          + 1j * np.asarray(req.iq_flat[1::2], dtype=np.float32)).reshape(req.n_channels, req.n_samples)
    R = covariance_from_iq(iq)
    return aic_mdl(R, req.n_samples)


# ── tracker (server-side multi-target track-while-scan) ──────────────────────

class TrackerStepObs(BaseModel):
    lat: float; lon: float; azimuth_deg: float
    frequency_hz: float = 0.0
    t: Optional[float] = None
    sigma_az_deg: float = 5.0


class TrackerStepRequest(BaseModel):
    observations: list[TrackerStepObs]


@router.post("/tracker/step")
async def df_tracker_step(req: TrackerStepRequest):
    from app.core.df.tracker import get_tracker
    tracker = get_tracker()
    obs = [o.dict() for o in req.observations]
    return {"tracks": tracker.step(obs)}


@router.get("/tracker/state")
async def df_tracker_state():
    from app.core.df.tracker import get_tracker
    return {"tracks": get_tracker().serialise()}


@router.post("/tracker/reset")
async def df_tracker_reset():
    from app.core.df.tracker import reset_tracker
    reset_tracker()
    return {"status": "reset"}


# ── multi-node fusion ────────────────────────────────────────────────────────

class FuseNode(BaseModel):
    lat: float; lon: float
    azimuth_deg: Optional[float] = None
    sigma_az_deg: float = 5.0
    toa_ns: Optional[float] = None
    sigma_t_ns: float = 50.0


class FuseRequest(BaseModel):
    mode: str = Field("aoa_aoa", pattern="^(aoa_aoa|tdoa|aoa_tdoa)$")
    nodes: list[FuseNode]
    aoa_nodes: Optional[list[FuseNode]] = None
    tdoa_nodes: Optional[list[FuseNode]] = None


@router.post("/fuse")
async def df_fuse(req: FuseRequest):
    from app.core.df import fusion
    if req.mode == "aoa_aoa":
        return fusion.fuse_aoa_aoa([n.dict(exclude_none=True) for n in req.nodes])
    if req.mode == "tdoa":
        return fusion.fuse_tdoa([n.dict(exclude_none=True) for n in req.nodes])
    return fusion.fuse_aoa_tdoa(
        [n.dict(exclude_none=True) for n in (req.aoa_nodes or [])],
        [n.dict(exclude_none=True) for n in (req.tdoa_nodes or [])],
    )


# ── calibration ──────────────────────────────────────────────────────────────

class CalibrationSaveRequest(BaseModel):
    device_id: str
    amplitude: list[float]
    phase_deg: list[float]
    note: str = ""


@router.post("/calibration/save")
async def df_cal_save(req: CalibrationSaveRequest):
    import numpy as np
    from app.core.df.calibration import save_calibration
    from pathlib import Path
    d = np.array(req.amplitude, dtype=float) * np.exp(1j * np.radians(np.array(req.phase_deg, dtype=float)))
    path = save_calibration(Path("data/df_state/cal") / f"{req.device_id}.json",
                            req.device_id, d, {"note": req.note})
    return {"status": "ok", "path": str(path)}


@router.get("/calibration/{device_id}")
async def df_cal_load(device_id: str):
    from pathlib import Path
    import json
    p = Path("data/df_state/cal") / f"{device_id}.json"
    if not p.exists():
        raise HTTPException(404, f"no calibration for device {device_id}")
    return json.loads(p.read_text())


@router.delete("/calibration/{device_id}")
async def df_cal_delete(device_id: str):
    from pathlib import Path
    p = Path("data/df_state/cal") / f"{device_id}.json"
    if p.exists(): p.unlink()
    return {"status": "deleted", "device_id": device_id}


# ── tasking queue ────────────────────────────────────────────────────────────

class TaskingEntry(BaseModel):
    id: Optional[str] = None
    frequency_hz: float
    span_hz: float = 200_000
    dwell_s: float = 2.0
    priority: int = 5
    label: str = ""
    enabled: bool = True


def _tasking_path():
    from pathlib import Path
    p = Path("data/df_state/tasking.json")
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _load_tasking():
    import json
    from pathlib import Path
    p = _tasking_path()
    if p.exists():
        return json.loads(p.read_text())
    return {"entries": [], "cursor": 0}


def _save_tasking(state):
    import json
    _tasking_path().write_text(__import__('json').dumps(state, indent=2))


@router.get("/tasking")
async def df_tasking_list():
    return _load_tasking()


@router.post("/tasking")
async def df_tasking_add(entry: TaskingEntry):
    import uuid
    state = _load_tasking()
    e = entry.dict()
    e["id"] = e.get("id") or str(uuid.uuid4())
    state["entries"].append(e)
    _save_tasking(state)
    return e


@router.put("/tasking/{entry_id}")
async def df_tasking_update(entry_id: str, entry: TaskingEntry):
    state = _load_tasking()
    for i, e in enumerate(state["entries"]):
        if e["id"] == entry_id:
            state["entries"][i] = {**e, **entry.dict(), "id": entry_id}
            _save_tasking(state); return state["entries"][i]
    raise HTTPException(404, "no such entry")


@router.delete("/tasking/{entry_id}")
async def df_tasking_delete(entry_id: str):
    state = _load_tasking()
    state["entries"] = [e for e in state["entries"] if e["id"] != entry_id]
    _save_tasking(state)
    return {"status": "deleted"}


@router.post("/tasking/cursor/advance")
async def df_tasking_advance():
    """Move to the next enabled entry in the queue; returns the new active entry."""
    state = _load_tasking()
    entries = [e for e in state["entries"] if e.get("enabled", True)]
    if not entries:
        return {"active": None}
    state["cursor"] = (state.get("cursor", 0) + 1) % len(entries)
    _save_tasking(state)
    return {"active": entries[state["cursor"]]}


# ── SDR drivers ──────────────────────────────────────────────────────────────

@router.get("/drivers")
async def df_drivers():
    """List the bundled IQ-level driver backends — KrakenSDR/HeIMDALL, ANTSDR
    e200, Matchstiq X40, UHD USRP, ADALM-Pluto, synthetic. Soapy-based devices
    stay on the existing /sdr path."""
    from app.core.sdr import drivers
    return {"drivers": drivers.list_drivers()}


# ── live IQ-to-bearing DF (instantiates a registry driver in-process) ──────────

class VfoSpec(BaseModel):
    """One narrowband VFO carved from the wideband capture (KrakenSDR-style)."""
    name: str = ""
    offset_hz: Optional[float] = None      # relative to the device centre…
    freq_hz: Optional[float] = None        # …or an absolute RF frequency
    bandwidth_hz: float = 0.0              # 0 ⇒ the whole capture band
    squelch_db: Optional[float] = None     # manual power gate (dBFS); null ⇒ auto


class LiveDfStartRequest(BaseModel):
    """Spin up the in-process DF pipeline on a registry driver: it pulls coherent
    IQ off the radio and runs Ares's own MUSIC/Capon/Bartlett solver, streaming
    bearings + fixes over the SDR WebSocket (and CoT) like any other device."""
    driver_id: str                                  # one of GET /df/drivers
    name: str = "live-df"
    frequency_hz: float = Field(..., gt=0)
    channels: int = Field(2, ge=2, le=64)           # DF needs ≥2 coherent channels
    array_type: str = Field("uca", pattern="^(ula|uca|adcock|custom)$")
    array_spacing_wavelengths: float = 0.4
    array_sense: bool = True                         # adcock: central omni sense element (Watson-Watt 360°)
    array_radius_m: Optional[float] = None           # adcock/uca: explicit ring radius (else derived from spacing)
    sample_rate_hz: float = Field(2.4e6, gt=0)
    gain_db: Optional[float] = None                 # null ⇒ the driver's AGC
    method: str = Field("music", pattern="^(music|capon|bartlett|correlative|watson_watt|doppler)$")
    n_snapshots: int = Field(4096, ge=256, le=262144)
    dwell_s: float = Field(1.0, ge=0.05, le=60.0)
    n_sources: int = Field(1, ge=1, le=8)
    fb_smoothing: bool = False
    az_step_deg: float = Field(1.0, gt=0.05, le=10.0)
    min_snr_db: float = 3.0
    min_quality: float = 0.10
    lat: float = 0.0
    lon: float = 0.0
    antenna_heading_deg: float = 0.0                # array boresight bearing → true-referenced LoB
    observer_height_m: float = 1.5
    environment: str = "suburban"
    use_gps: bool = True
    auto_coverage: bool = False
    driver_args: dict = {}                          # extra kwargs for drivers.create (uri, args, ...)
    array_positions_m: Optional[list[list[float]]] = None   # custom geometry (N×2/N×3 ENU metres)
    # multi-VFO: DF several narrowband channels from one wideband capture
    vfos: list[VfoSpec] = []
    auto_squelch: Optional[bool] = None             # null ⇒ on iff >1 VFO
    squelch_margin_db: float = 8.0                  # auto-squelch margin above the learned noise floor
    # auto-calibration (needs a driver with a switchable coherence source, cal_source=True)
    auto_calibrate: bool = False
    cal_interval_s: float = Field(300.0, ge=10.0, le=86400.0)


def _build_live_payload(body: "LiveDfStartRequest") -> dict:
    """Translate a LiveDfStartRequest into the SDRDevice payload. Shared by the
    start route and the in-place update (Edit) route so both apply identical
    metadata + array geometry from the same form."""
    md: dict = {
        "driver_id": body.driver_id, "driver_args": dict(body.driver_args or {}),
        "sample_rate_hz": body.sample_rate_hz, "gain_db": body.gain_db,
        "method": body.method, "n_snapshots": body.n_snapshots, "dwell_s": body.dwell_s,
        "n_sources": body.n_sources, "fb_smoothing": body.fb_smoothing,
        "az_step_deg": body.az_step_deg, "min_snr_db": body.min_snr_db, "min_quality": body.min_quality,
        "squelch_margin_db": body.squelch_margin_db,
        "auto_calibrate": body.auto_calibrate, "cal_interval_s": body.cal_interval_s,
    }
    if body.vfos:
        md["vfos"] = [v.dict() for v in body.vfos]
    if body.auto_squelch is not None:
        md["auto_squelch"] = body.auto_squelch
    channels = body.channels
    if body.array_positions_m:
        md["array"] = {"type": "custom", "positions_m": body.array_positions_m, "name": f"{body.name}-custom"}
        channels = len(body.array_positions_m)        # one coherent channel per element
    elif body.array_type == "adcock":
        # Adcock ring (+ optional centre sense). Derive the ring radius from the
        # requested element spacing in wavelengths when not given explicitly.
        import math as _math
        n_ring = max(3, int(body.channels) - (1 if body.array_sense else 0))
        lam = 299_792_458.0 / max(1.0, float(body.frequency_hz))
        radius = body.array_radius_m or (body.array_spacing_wavelengths * lam / (2.0 * _math.sin(_math.pi / n_ring)))
        md["array"] = {"type": "adcock", "n": n_ring, "radius_m": float(radius), "sense": bool(body.array_sense),
                       "name": f"{body.name}-adcock"}
        channels = n_ring + (1 if body.array_sense else 0)
    return {
        "name": body.name, "type": "live_df", "host": body.driver_id, "port": 0,
        "source_class": "multi_channel", "channels": channels,
        "array_type": body.array_type, "array_spacing_wavelengths": body.array_spacing_wavelengths,
        "azimuth_reference": "absolute", "antenna_heading_deg": body.antenna_heading_deg,
        "lat": body.lat, "lon": body.lon, "observer_height_m": body.observer_height_m,
        "frequency_hz": body.frequency_hz, "environment": body.environment,
        "enabled": True, "use_gps": body.use_gps, "auto_coverage": body.auto_coverage,
        "metadata": md,
    }


@router.post("/live/start")
async def df_live_start(body: LiveDfStartRequest, principal: dict = Depends(require_auth)):
    from app.core.sdr import drivers, sdr_manager
    from app.core.security import audit
    avail = {d["id"] for d in drivers.list_drivers()}
    if body.driver_id not in avail:
        raise HTTPException(400, f"unknown driver_id {body.driver_id!r}; choose from {sorted(avail)}")
    try:
        dev = sdr_manager.add(_build_live_payload(body))
    except ValueError as e:
        raise HTTPException(400, str(e))
    audit("df.live.start", id=dev.id, driver=body.driver_id, freq_hz=body.frequency_hz,
          channels=body.channels, method=body.method, by=principal.get("sub"))
    return {"status": "started", "device": dev.public()}


@router.put("/live/{device_id}")
async def df_live_update(device_id: str, body: LiveDfStartRequest, principal: dict = Depends(require_auth)):
    """Re-configure an existing live-DF device in place (same id) from the SDR
    console's Edit button, then re-spawn it with the new parameters. Preserves the
    current enabled state (editing a stopped device doesn't start it)."""
    from app.core.sdr import drivers, sdr_manager
    from app.core.security import audit
    dev = sdr_manager.get(device_id)
    if dev is None or dev.type != "live_df":
        raise HTTPException(404, "no such live-DF device")
    avail = {d["id"] for d in drivers.list_drivers()}
    if body.driver_id not in avail:
        raise HTTPException(400, f"unknown driver_id {body.driver_id!r}; choose from {sorted(avail)}")
    patch = _build_live_payload(body)
    patch.pop("type", None)         # type is immutable
    patch.pop("enabled", None)      # don't flip enabled on an edit — manager re-spawns if it's on
    try:
        dev = sdr_manager.update(device_id, patch)
    except KeyError:
        raise HTTPException(404, "no such device")
    except ValueError as e:
        raise HTTPException(400, str(e))
    audit("df.live.update", id=device_id, driver=body.driver_id, freq_hz=body.frequency_hz,
          method=body.method, by=principal.get("sub"))
    return {"status": "updated", "device": dev.public()}


@router.post("/live/{device_id}/stop")
async def df_live_stop(device_id: str, remove: bool = False, principal: dict = Depends(require_auth)):
    """Stop a live-DF device — disables it (closing the driver) or, with
    ``?remove=true``, deletes it outright."""
    from app.core.sdr import sdr_manager
    dev = sdr_manager.get(device_id)
    if dev is None or dev.type != "live_df":
        raise HTTPException(404, "no such live-DF device")
    if remove:
        sdr_manager.remove(device_id)
        return {"status": "removed", "id": device_id}
    sdr_manager.update(device_id, {"enabled": False})
    return {"status": "stopped", "id": device_id}


@router.post("/live/{device_id}/calibrate")
async def df_live_calibrate(device_id: str, principal: dict = Depends(require_auth)):
    """Force an immediate coherence (re)calibration on a running live-DF device.
    Needs a driver with a switchable calibration source (``cal_source=True``)."""
    import time as _time
    from app.core.sdr import sdr_manager, drivers
    from app.core.security import audit
    dev = sdr_manager.get(device_id)
    if dev is None or dev.type != "live_df":
        raise HTTPException(404, "no such live-DF device")
    driver_id = (dev.metadata or {}).get("driver_id") or dev.host
    cal_capable = any(d["id"] == driver_id and d.get("cal_source") for d in drivers.list_drivers())
    if not cal_capable:
        raise HTTPException(400, f"driver {driver_id!r} has no switchable calibration source")
    # the running adapter watches metadata['force_cal'] and recalibrates next dwell
    md = dict(dev.metadata or {}); md["force_cal"] = _time.time(); dev.metadata = md
    audit("df.live.calibrate", id=device_id, by=principal.get("sub"))
    return {"status": "calibration requested", "id": device_id}


@router.get("/live")
async def df_live_list(principal: dict = Depends(require_auth)):
    """The registered live-DF devices and their runtime status (incl. per-VFO
    state in ``metadata.vfo_status`` and calibration state in ``metadata.cal``)."""
    from app.core.sdr import sdr_manager
    return {"devices": [d for d in sdr_manager.list() if d.get("type") == "live_df"]}


# ── passive radar ────────────────────────────────────────────────────────────

class PassiveRadarRequest(BaseModel):
    ref_iq_flat: list[float]
    surv_iq_flat: list[float]
    n_samples: int = Field(..., ge=64, le=4_194_304)
    sample_rate_hz: float = Field(..., gt=0)
    max_range_km: float = 30.0
    max_doppler_hz: float = 200.0
    n_doppler: int = 256
    clutter_taps: int = 64


@router.post("/passive_radar/process")
async def df_passive_radar(req: PassiveRadarRequest):
    """Build a range-Doppler map from a coherent reference + surveillance pair."""
    import numpy as np
    from app.core.passive_radar import cross_ambiguity as ca
    if len(req.ref_iq_flat) != 2 * req.n_samples or len(req.surv_iq_flat) != 2 * req.n_samples:
        raise HTTPException(400, "ref/surv lengths must each equal 2*n_samples")
    ref = np.asarray(req.ref_iq_flat[0::2], dtype=np.float32) + 1j * np.asarray(req.ref_iq_flat[1::2], dtype=np.float32)
    surv = np.asarray(req.surv_iq_flat[0::2], dtype=np.float32) + 1j * np.asarray(req.surv_iq_flat[1::2], dtype=np.float32)
    surv_clean = ca.clutter_filter_extended_cancel(ref, surv, req.clutter_taps)
    return ca.cross_ambiguity(ref, surv_clean, req.sample_rate_hz,
                               req.max_range_km, req.max_doppler_hz, req.n_doppler)


@router.get("/passive_radar/illuminators")
async def df_passive_radar_illuminators(region: Optional[str] = None):
    from app.core.passive_radar import illuminators
    return {"regions": illuminators.list_regions(),
            "illuminators": illuminators.list_illuminators(region)}


# ── antenna database ─────────────────────────────────────────────────────────
import re as _re
from pathlib import Path as _Path

_ANTENNA_DIR = _Path("data/antennas")


def _slug(s: str) -> str:
    s = _re.sub(r"[^a-z0-9]+", "_", (s or "").strip().lower()).strip("_")
    return s or "custom"


def _build_positions_m(geometry: str, *, n: Optional[int] = None, radius_m: Optional[float] = None,
                       spacing_m: Optional[float] = None, sense: bool = False,
                       positions_m: Optional[list] = None) -> list[list[float]]:
    """Resolve a geometry spec to explicit element positions (N×3 ENU metres) so a
    saved antenna is fully self-describing — works for any shape or hand-placed
    combination of elements."""
    from app.core.df.interferometry import ArrayGeometry
    g = (geometry or "custom").lower()
    if g == "custom" or positions_m is not None:
        if not positions_m:
            raise ValueError("custom geometry needs positions_m (N×2 or N×3 metres)")
        return ArrayGeometry.from_positions(positions_m).positions_m.tolist()
    if g == "ula":
        return ArrayGeometry.ula(int(n), float(spacing_m)).positions_m.tolist()
    if g == "uca":
        return ArrayGeometry.uca(int(n), float(radius_m)).positions_m.tolist()
    if g == "adcock":
        return ArrayGeometry.adcock(int(n), float(radius_m), sense=bool(sense)).positions_m.tolist()
    raise ValueError(f"unknown geometry {geometry!r}")


class AntennaProfile(BaseModel):
    """A user-defined antenna/array. Either give explicit ``positions_m`` (any
    geometry, or a combination of antennas merged into one element set) or a
    parametric shape (ula/uca/adcock + n + spacing/radius); on save it is
    resolved to explicit positions so it is fully self-describing."""
    id: Optional[str] = None
    name: str
    manufacturer: str = "Custom"
    model: str = ""
    geometry: str = Field("custom", pattern="^(ula|uca|adcock|custom)$")
    n_elements: Optional[int] = None
    spacing_m: Optional[float] = None
    radius_m: Optional[float] = None
    sense: bool = False
    positions_m: Optional[list[list[float]]] = None
    df_methods: list[str] = Field(default_factory=lambda: ["music", "correlative"])
    recommended_method: Optional[str] = None
    freq_min_hz: float = 20e6
    freq_max_hz: float = 6e9
    element_type: str = "monopole"
    notes: str = ""


@router.get("/antennas")
async def df_antennas_list():
    """List the bundled antenna profiles (KrakenSDR UCA, ANTSDR clones, the
    ALARIS DF line, USRP ULA) plus any user-saved custom arrays. Read from
    backend/data/antennas/*.json."""
    import json
    out = []
    if _ANTENNA_DIR.exists():
        for p in sorted(_ANTENNA_DIR.glob("*.json")):
            try: out.append(json.loads(p.read_text()))
            except Exception: pass
    return {"antennas": out}


@router.post("/antennas")
async def df_antenna_save(body: AntennaProfile, principal: dict = Depends(require_auth)):
    """Create / update a custom antenna array. Resolves the geometry to explicit
    element positions and stores it so it appears in the antenna picker and can
    drive live DF with any method."""
    import json
    from app.core.security import audit
    try:
        positions = _build_positions_m(body.geometry, n=body.n_elements, radius_m=body.radius_m,
                                        spacing_m=body.spacing_m, sense=body.sense, positions_m=body.positions_m)
    except (ValueError, TypeError, KeyError) as e:
        raise HTTPException(400, f"bad antenna geometry: {e}")
    if len(positions) < 1:
        raise HTTPException(400, "an antenna needs at least one element")
    aid = _slug(body.id or body.name)
    if not aid.startswith("custom_"):
        aid = f"custom_{aid}"
    profile = {
        "id": aid, "manufacturer": body.manufacturer or "Custom", "model": body.model or aid,
        "name": body.name, "geometry": "custom", "n_elements": len(positions),
        "positions_m": positions, "sense": bool(body.sense),
        "df_methods": body.df_methods or ["music", "correlative"],
        "recommended_method": body.recommended_method or (body.df_methods or ["music"])[0],
        "freq_min_hz": float(body.freq_min_hz), "freq_max_hz": float(body.freq_max_hz),
        "element_type": body.element_type, "notes": body.notes,
        "custom": True, "editable": True,
    }
    _ANTENNA_DIR.mkdir(parents=True, exist_ok=True)
    (_ANTENNA_DIR / f"{aid}.json").write_text(json.dumps(profile, indent=2))
    audit("df.antenna.save", id=aid, n_elements=len(positions), by=principal.get("sub"))
    return profile


@router.delete("/antennas/{antenna_id}")
async def df_antenna_delete(antenna_id: str, principal: dict = Depends(require_auth)):
    """Delete a *custom* antenna profile (bundled vendor profiles are protected)."""
    import json
    from app.core.security import audit
    p = _ANTENNA_DIR / f"{_slug(antenna_id) if antenna_id.startswith('custom_') else antenna_id}.json"
    if not p.exists():
        raise HTTPException(404, "no such antenna")
    try:
        prof = json.loads(p.read_text())
    except Exception:
        prof = {}
    if not prof.get("custom"):
        raise HTTPException(403, "bundled vendor profiles are read-only; only user-saved custom arrays can be deleted")
    p.unlink()
    audit("df.antenna.delete", id=antenna_id, by=principal.get("sub"))
    return {"deleted": True, "id": antenna_id}


class ArrayEstimateRequest(BaseModel):
    array: ArraySpec
    frequency_hz: float = Field(433.92e6, gt=0)
    snr_db: float = 15.0
    snapshots: int = Field(256, ge=1, le=1_000_000)


@router.post("/array/estimate")
async def df_array_estimate(body: ArrayEstimateRequest, principal: dict = Depends(require_auth)):
    """Expected azimuth σ (deg, CRLB) for an arbitrary array geometry — drives the
    custom-array builder's live 'expected accuracy' readout for any element layout."""
    from app.core.df.interferometry import geometry_from_spec, _crlb_phase
    try:
        geom = geometry_from_spec(body.array.dict())
    except (KeyError, ValueError, TypeError) as e:
        raise HTTPException(400, f"bad array spec: {e}")
    if geom.n < 2:
        return {"n_elements": geom.n, "can_df": False,
                "note": "≥2 elements are needed to produce a line of bearing"}
    sigma_phase_rad = 1.0 / float(np.sqrt(2.0 * max(0.5, 10.0 ** (body.snr_db / 10.0)) * max(1, body.snapshots)))
    full_3d = (not geom.is_collinear) and (not geom.is_planar_horizontal)
    vals = []
    for az in (10.0, 70.0, 130.0, 200.0, 280.0, 340.0):
        sa, _ = _crlb_phase(geom, body.frequency_hz, az, 0.0, sigma_phase_rad, 0, full_3d)
        vals.append(sa)
    crlb = float(np.mean(vals))
    aperture = float(np.max(np.linalg.norm(geom.positions_m - geom.positions_m.mean(0), axis=1)) * 2.0)
    return {
        "n_elements": geom.n, "can_df": True, "array": geom.name,
        "sigma_az_deg": round(crlb, 2), "sigma_az_best_deg": round(min(vals), 2),
        "sigma_az_worst_deg": round(max(vals), 2),
        "aperture_m": round(aperture, 3), "collinear": geom.is_collinear,
        "note": (f"≈{crlb:.1f}° CRLB σ @ {body.snr_db:.0f} dB SNR, {body.snapshots} snapshots"
                 + ("; collinear → 180° front/back ambiguous" if geom.is_collinear else "")),
    }


# ── mission package ──────────────────────────────────────────────────────────

class MissionExportRequest(BaseModel):
    name: str = "ares-mission"
    description: str = ""
    scan_list: list[dict] = []
    watchlist: list[dict] = []
    geofences: dict = {"type": "FeatureCollection", "features": []}
    antennas: list[dict] = []
    drawings: dict = {"type": "FeatureCollection", "features": []}
    templates: list[dict] = []
    packs: list[dict] = []
    notes: str = ""


@router.post("/mission/export")
async def df_mission_export(req: MissionExportRequest):
    from fastapi.responses import Response
    from app.core.mission_package import export_mission_package
    blob = export_mission_package(
        name=req.name, description=req.description,
        scan_list=req.scan_list, watchlist=req.watchlist,
        geofences=req.geofences, antennas=req.antennas,
        drawings=req.drawings, templates=req.templates,
        packs=req.packs, notes=req.notes,
    )
    safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in req.name)[:64] or "mission"
    return Response(content=blob, media_type="application/zip",
                    headers={"Content-Disposition": f'attachment; filename="{safe}.ares-mission.zip"'})


@router.post("/mission/import")
async def df_mission_import(file: bytes):
    from app.core.mission_package import import_mission_package
    return import_mission_package(file)


# ─────────────────────────────────────────────────────────────────────────────
# Wideband channelizer
# ─────────────────────────────────────────────────────────────────────────────

class ChannelizeRequest(BaseModel):
    iq_flat: list[float]
    n_samples: int = Field(..., ge=128, le=1_048_576)
    n_channels: int = Field(64, ge=2, le=512)
    sample_rate_hz: float = Field(..., gt=0)
    center_freq_hz: float = Field(..., gt=0)
    detect_threshold_db: float = 10.0
    taps_per_channel: int = 32


@router.post("/channelize")
async def df_channelize(req: ChannelizeRequest):
    """Polyphase wideband channelizer + sub-channel detector. Returns the list
    of active sub-channels with centre / bw / power, so the operator (or a
    pipeline) can DF each one in parallel."""
    import numpy as np
    from app.core.df import channelizer
    if len(req.iq_flat) != 2 * req.n_samples:
        raise HTTPException(400, f"iq_flat length must equal 2*n_samples")
    iq = np.asarray(req.iq_flat[0::2], dtype=np.float32) + 1j * np.asarray(req.iq_flat[1::2], dtype=np.float32)
    channels = channelizer.channelize(iq, req.n_channels, taps_per_channel=req.taps_per_channel)
    detections = channelizer.detect_signals(channels, req.sample_rate_hz, req.center_freq_hz,
                                            threshold_db=req.detect_threshold_db)
    return {
        "n_channels": int(req.n_channels),
        "channel_bw_hz": float(req.sample_rate_hz / req.n_channels),
        "detections": detections,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Modulation classifier
# ─────────────────────────────────────────────────────────────────────────────

class ModClassRequest(BaseModel):
    iq_flat: list[float]
    n_samples: int = Field(..., ge=64, le=1_048_576)
    sample_rate_hz: float = Field(..., gt=0)


@router.post("/modclass")
async def df_modclass(req: ModClassRequest):
    """Feature-based modulation classification (CW / FM / AM / FSK / PSK /
    OFDM / spread). Returns label, confidence, raw features."""
    import numpy as np
    from app.core.df import modclass
    if len(req.iq_flat) != 2 * req.n_samples:
        raise HTTPException(400, "iq_flat length must equal 2*n_samples")
    iq = np.asarray(req.iq_flat[0::2], dtype=np.float32) + 1j * np.asarray(req.iq_flat[1::2], dtype=np.float32)
    return modclass.classify(iq, req.sample_rate_hz)


# ─────────────────────────────────────────────────────────────────────────────
# Signal watchlists (bundled environments)
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/watchlists")
async def df_watchlists():
    """List all bundled signal-of-interest watchlists (FRS/GMRS, aviation,
    drones, IoT/ISM, ham, broadcast). Each entry is { id, name, entries }."""
    import json
    from pathlib import Path
    base = Path("data/signal_watchlists")
    out = []
    if base.exists():
        for p in sorted(base.glob("*.json")):
            try: out.append(json.loads(p.read_text()))
            except Exception: pass
    return {"watchlists": out}


# ─────────────────────────────────────────────────────────────────────────────
# GM-PHD tracker
# ─────────────────────────────────────────────────────────────────────────────

class GmPhdStepRequest(BaseModel):
    observations: list[TrackerStepObs] = []


@router.post("/gmphd/step")
async def df_gmphd_step(req: GmPhdStepRequest):
    """Step the GM-PHD tracker with a batch of LoB observations."""
    from app.core.df.gmphd import get_gmphd
    return {"tracks": get_gmphd().step([o.dict() for o in req.observations])}


@router.get("/gmphd/state")
async def df_gmphd_state():
    from app.core.df.gmphd import get_gmphd
    return {"tracks": get_gmphd().serialise()}


@router.post("/gmphd/reset")
async def df_gmphd_reset():
    from app.core.df.gmphd import reset_gmphd
    reset_gmphd()
    return {"status": "reset"}


# ─────────────────────────────────────────────────────────────────────────────
# Multi-baseline interferometry
# ─────────────────────────────────────────────────────────────────────────────

class BaselineMeasurement(BaseModel):
    vec_m: list[float]                      # [east_m, north_m] from element a to b
    phase_rad: float
    sigma_rad: float = 0.1


class MultiBaselineRequest(BaseModel):
    frequency_hz: float = Field(..., gt=0)
    baselines: list[BaselineMeasurement]


@router.post("/multibaseline")
async def df_multibaseline(req: MultiBaselineRequest):
    from app.core.df.multibaseline import resolve_bearing
    wavelength = 299_792_458.0 / req.frequency_hz
    return resolve_bearing([b.dict() for b in req.baselines], wavelength)


# ─────────────────────────────────────────────────────────────────────────────
# Moving-platform compensation
# ─────────────────────────────────────────────────────────────────────────────

class MovingPlatformRequest(BaseModel):
    relative_bearing_deg: float
    platform_heading_deg: float
    speed_mps: float = 0.0
    capture_duration_s: float = 0.0


@router.post("/moving_platform")
async def df_moving_platform(req: MovingPlatformRequest):
    from app.core.df import moving_platform
    return {
        "true_bearing_deg": moving_platform.to_true_bearing(req.relative_bearing_deg, req.platform_heading_deg),
        "smear": moving_platform.smear_warning(req.speed_mps, req.capture_duration_s),
    }


# ─────────────────────────────────────────────────────────────────────────────
# DF ↔ Remote-ID anti-spoof
# ─────────────────────────────────────────────────────────────────────────────

class SpoofRequest(BaseModel):
    df_tracks: list[dict]
    rid_messages: list[dict]
    observer: Optional[dict] = None
    max_age_s: float = 30.0
    bearing_tol_deg: float = 8.0
    cep_multiplier: float = 3.0


@router.post("/spoof_check")
async def df_spoof_check(req: SpoofRequest):
    from app.core.df import spoof
    return {"matches": spoof.correlate(req.df_tracks, req.rid_messages,
                                          req.observer, req.max_age_s,
                                          req.bearing_tol_deg, req.cep_multiplier)}


# ─────────────────────────────────────────────────────────────────────────────
# ADS-B / Mode-S
# ─────────────────────────────────────────────────────────────────────────────

class ModeSDecodeRequest(BaseModel):
    messages_hex: list[str]                  # list of 14-byte (28-hex-char) packets


_MODES_DECODER = None
def _get_modes_decoder():
    global _MODES_DECODER
    if _MODES_DECODER is None:
        from app.core.decoders.mode_s import Mode_SDecoderState
        _MODES_DECODER = Mode_SDecoderState()
    return _MODES_DECODER


@router.post("/decoders/mode_s")
async def df_mode_s(req: ModeSDecodeRequest):
    """Decode a batch of Mode-S / ADS-B packets. Returns updated aircraft list."""
    import time
    from app.core.decoders import mode_s
    dec = _get_modes_decoder()
    now = time.time()
    updated = []
    for hexstr in req.messages_hex:
        try:
            ac = dec.step(mode_s.hex_to_bytes(hexstr), now)
            if ac:
                updated.append({
                    "icao": ac.icao, "callsign": ac.callsign,
                    "lat": ac.lat, "lon": ac.lon, "alt_ft": ac.alt_ft,
                    "speed_kt": ac.speed_kt, "heading_deg": ac.heading_deg,
                    "vertical_fpm": ac.vertical_fpm, "n_msgs": ac.n_msgs,
                })
        except Exception as e:
            log.debug("mode_s decode failed: %s", e)
    aircraft = [{"icao": ac.icao, "callsign": ac.callsign, "lat": ac.lat, "lon": ac.lon,
                  "alt_ft": ac.alt_ft, "speed_kt": ac.speed_kt, "heading_deg": ac.heading_deg,
                  "n_msgs": ac.n_msgs, "last_update_t": ac.last_update_t}
                 for ac in dec.aircraft.values()]
    return {"updated": updated, "aircraft": aircraft}


@router.post("/decoders/mode_s/reset")
async def df_mode_s_reset():
    global _MODES_DECODER
    _MODES_DECODER = None
    return {"status": "reset"}


# ─────────────────────────────────────────────────────────────────────────────
# SigMF replay
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/replay/list")
async def df_replay_list():
    from app.core import replay
    return {"recordings": replay.list_recordings()}


class ReplayOpenRequest(BaseModel):
    meta_path: str
    block_size: int = 65536


@router.post("/replay/open")
async def df_replay_open(req: ReplayOpenRequest):
    from app.core import replay
    return replay.open_sigmf(req.meta_path)


# ─────────────────────────────────────────────────────────────────────────────
# Time-sync + SDR health
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/time_sync")
async def df_time_sync():
    from app.core import time_sync
    return time_sync.status()


@router.get("/health")
async def df_health():
    from app.core import sdr_health
    return {"devices": sdr_health.status_all()}


# ─────────────────────────────────────────────────────────────────────────────
# Track archive
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/track_archive")
async def df_track_archive_list():
    from app.core import track_archive
    return {"tracks": track_archive.list_tracks()}


@router.get("/track_archive/{track_id}")
async def df_track_archive_get(track_id: str):
    from app.core import track_archive
    d = track_archive.get_archive(track_id)
    if d is None:
        raise HTTPException(404, f"no archive for {track_id}")
    return d


@router.delete("/track_archive/{track_id}")
async def df_track_archive_delete(track_id: str):
    from app.core import track_archive
    return {"removed": track_archive.remove_track(track_id)}


# ─────────────────────────────────────────────────────────────────────────────
# Recordings (audio)
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/recordings")
async def df_recordings():
    from app.core import audio_capture
    return {"recordings": audio_capture.list_recordings()}


# ─────────────────────────────────────────────────────────────────────────────
# GNU Radio bridge status
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/gnuradio/status")
async def df_gnuradio_status():
    from app.core import gnuradio_zmq
    return gnuradio_zmq.status()
