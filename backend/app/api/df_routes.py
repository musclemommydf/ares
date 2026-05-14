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
    e200, Matchstiq X40, UHD USRP, synthetic. Soapy-based devices stay on the
    existing /sdr path."""
    from app.core.sdr import drivers
    return {"drivers": drivers.list_drivers()}


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

@router.get("/antennas")
async def df_antennas_list():
    """List the bundled antenna profiles (KrakenSDR UCA, ANTSDR clones, Alaris
    representative, USRP ULA). Read from backend/data/antennas/*.json."""
    import json
    from pathlib import Path
    base = Path("data/antennas")
    out = []
    if base.exists():
        for p in sorted(base.glob("*.json")):
            try: out.append(json.loads(p.read_text()))
            except Exception: pass
    return {"antennas": out}


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
