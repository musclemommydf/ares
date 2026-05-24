# SPDX-License-Identifier: MIT OR Apache-2.0
# Copyright (c) 2026 Ares

"""
Ares — single-channel & multi-receiver geolocation algorithms.

Exposes the methods in ``app.core.df.single_channel`` over HTTP. Every
algorithm here runs **entirely in-process** (numpy / scipy / scikit) — no
external service, no cloud call, no third-party DSP. The frontend Algorithms
tab and any third-party tooling can drive these endpoints to fix an emitter
from RSS, Doppler, kinematic IQ snapshots, multi-receiver TDOA, or any
combination of the above.

Endpoint summary
----------------
GET  /algorithms/methods                        — catalogue + feasibility
POST /algorithms/feasibility                    — diagnose which methods can run on the given obs
POST /algorithms/rss_path_loss                  — multi-pose RSSI → ML emitter fix
POST /algorithms/rss_gradient                   — local RSS gradient → bearing
POST /algorithms/doppler_cpa                    — single-pass Doppler S-curve → CPA fix
POST /algorithms/fdoa_track                     — multi-pose Doppler grid fix
POST /algorithms/synthetic_aperture             — coherent IQ snapshots → DoA (Bartlett/Capon/MUSIC)
POST /algorithms/phase_interferometry           — phase-Δ between snapshots → DoA
POST /algorithms/tdoa_multi_receiver            — multi-Rx TDOA → emitter fix
POST /algorithms/ml_grid_fusion                 — universal ML grid: AoA + RSS + Doppler + TDOA
POST /algorithms/ekf_track                      — sequential EKF over heterogeneous obs

A common request shape is used: each algorithm takes a list of observations
plus a small set of algorithm-specific knobs. The response is the dict
returned by the underlying core function (always JSON-safe, never raises).
"""
from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field, ConfigDict

from app.core.auth import require_auth
from app.core.df import single_channel as sc

router = APIRouter(tags=["algorithms"], prefix="/algorithms")


# ─────────────────────────────────────────────────────────────────────────────
class GenericObservation(BaseModel):
    """A heterogeneous observation. Different methods use different subsets
    of fields — the model accepts everything and the per-method handler
    picks what it needs. ``model_config`` permits extras so a deployment can
    attach metadata (timestamp, label, …) without us policing it."""
    model_config = ConfigDict(extra="allow")
    lat: Optional[float] = None
    lon: Optional[float] = None
    rssi_dbm: Optional[float] = None
    power_dbm: Optional[float] = None
    frequency_offset_hz: Optional[float] = None
    bearing_deg: Optional[float] = None
    sigma_deg: Optional[float] = None
    vx_mps: Optional[float] = None
    vy_mps: Optional[float] = None
    v_mps: Optional[float] = None
    t: Optional[float] = None
    t_arrival_s: Optional[float] = None
    x_m: Optional[float] = None
    y_m: Optional[float] = None
    iq_re: Optional[float] = None
    iq_im: Optional[float] = None
    kind: Optional[str] = None    # 'aoa' / 'rss' / 'doppler' / 'tdoa'


def _materialise(observations: list[GenericObservation]) -> list[dict]:
    """Pydantic → plain dicts, with iq_re/iq_im promoted to a complex value."""
    out = []
    for o in observations:
        d = o.model_dump(exclude_none=True)
        if "iq_re" in d and "iq_im" in d:
            d["iq_complex"] = complex(float(d["iq_re"]), float(d["iq_im"]))
        out.append(d)
    return out


# ── catalogue + feasibility ─────────────────────────────────────────────────
@router.get("/methods")
def list_methods(_auth=Depends(require_auth)):
    return {
        "methods": [
            {"id": "rss_path_loss", "name": "RSS log-distance ML",
              "needs": ["RSS samples at distinct positions"],
              "produces": "Emitter position + P_tx + path-loss-exponent estimate",
              "single_channel": True, "stationary_emitter": True},
            {"id": "rss_gradient", "name": "RSS-gradient bearing",
              "needs": ["RSS samples spanning a small aperture"],
              "produces": "Bearing-to-emitter (no range)",
              "single_channel": True, "stationary_emitter": True},
            {"id": "doppler_cpa", "name": "Doppler closest-point-of-approach",
              "needs": ["Doppler offset along a straight pass + known carrier"],
              "produces": "CPA distance & time + position (left/right ambiguous)",
              "single_channel": True, "stationary_emitter": True},
            {"id": "fdoa_track", "name": "FDOA multi-pose grid",
              "needs": ["Doppler + 2-D velocity at ≥ 3 poses + known carrier"],
              "produces": "Emitter position fix",
              "single_channel": True, "stationary_emitter": True},
            {"id": "synthetic_aperture", "name": "Kinematic synthetic-aperture DoA",
              "needs": ["Coherent IQ snapshots at known positions + known carrier"],
              "produces": "Direction-of-arrival pseudo-spectrum (Bartlett / Capon / MUSIC)",
              "single_channel": True, "stationary_emitter": True},
            {"id": "phase_interferometry", "name": "Phase-Δ along-track DoA",
              "needs": ["Coherent IQ snapshots at known positions + known carrier"],
              "produces": "Per-baseline bearing + circular-mean bearing",
              "single_channel": True, "stationary_emitter": True},
            {"id": "tdoa_multi_receiver", "name": "Multi-receiver TDOA",
              "needs": ["≥ 2 synchronised receivers w/ time-of-arrival"],
              "produces": "Emitter position fix",
              "single_channel": False, "stationary_emitter": True},
            {"id": "ml_grid_fusion", "name": "ML grid fusion (universal)",
              "needs": ["Mix of AoA / RSS / Doppler / TDOA observations"],
              "produces": "Joint MAP fix + likelihood heatmap",
              "single_channel": True, "stationary_emitter": True},
            {"id": "ekf_track", "name": "EKF kinematic tracker",
              "needs": ["Sequence of heterogeneous observations"],
              "produces": "Sequential ML position + uncertainty per step",
              "single_channel": True, "stationary_emitter": True},
        ],
    }


class FeasibilityRequest(BaseModel):
    observations: list[GenericObservation] = Field(default_factory=list)


@router.post("/feasibility")
def feasibility(req: FeasibilityRequest, _auth=Depends(require_auth)):
    return sc.feasibility_report(_materialise(req.observations))


# ── RSS log-distance ─────────────────────────────────────────────────────────
class RssPathLossRequest(BaseModel):
    observations: list[GenericObservation]
    path_loss_n: Optional[float] = Field(None, ge=0.5, le=6.0)
    p_tx_dbm: Optional[float] = Field(None, ge=-50, le=80)
    d0_m: float = Field(1.0, gt=0)
    sigma_db: float = Field(6.0, gt=0)
    grid_m: float = Field(50.0, gt=0)
    grid_span_m: float = Field(50_000.0, gt=0)


@router.post("/rss_path_loss")
def rss_path_loss(req: RssPathLossRequest, _auth=Depends(require_auth)):
    return sc.rss_path_loss_fix(_materialise(req.observations),
                                  path_loss_n=req.path_loss_n, p_tx_dbm=req.p_tx_dbm,
                                  d0_m=req.d0_m, sigma_db=req.sigma_db,
                                  grid_m=req.grid_m, grid_span_m=req.grid_span_m)


class RssGradientRequest(BaseModel):
    observations: list[GenericObservation]


@router.post("/rss_gradient")
def rss_gradient(req: RssGradientRequest, _auth=Depends(require_auth)):
    return sc.rss_gradient_bearing(_materialise(req.observations))


# ── Doppler / FDOA ───────────────────────────────────────────────────────────
class DopplerCpaRequest(BaseModel):
    observations: list[GenericObservation]
    carrier_hz: float = Field(..., gt=0)


@router.post("/doppler_cpa")
def doppler_cpa(req: DopplerCpaRequest, _auth=Depends(require_auth)):
    return sc.doppler_cpa_fit(_materialise(req.observations), carrier_hz=req.carrier_hz)


class FdoaTrackRequest(BaseModel):
    observations: list[GenericObservation]
    carrier_hz: float = Field(..., gt=0)
    grid_span_m: float = Field(50_000.0, gt=0)
    grid_step_m: float = Field(50.0, gt=0)
    sigma_hz: float = Field(5.0, gt=0)


@router.post("/fdoa_track")
def fdoa_track(req: FdoaTrackRequest, _auth=Depends(require_auth)):
    return sc.fdoa_track_fix(_materialise(req.observations), carrier_hz=req.carrier_hz,
                               grid_span_m=req.grid_span_m, grid_step_m=req.grid_step_m,
                               sigma_hz=req.sigma_hz)


# ── Synthetic-aperture / phase-interferometry (IQ snapshots) ─────────────────
class SyntheticApertureRequest(BaseModel):
    snapshots: list[GenericObservation]
    carrier_hz: float = Field(..., gt=0)
    method: str = Field("bartlett", pattern="^(bartlett|capon|music)$")
    n_sources: int = Field(1, ge=1, le=8)
    az_step_deg: float = Field(1.0, gt=0, le=10.0)
    az_start_deg: float = Field(-180.0)
    az_end_deg: float = Field(180.0)


@router.post("/synthetic_aperture")
def synthetic_aperture(req: SyntheticApertureRequest, _auth=Depends(require_auth)):
    import numpy as np
    az = np.arange(req.az_start_deg, req.az_end_deg, req.az_step_deg)
    return sc.synthetic_aperture_doa(_materialise(req.snapshots),
                                       carrier_hz=req.carrier_hz, method=req.method,
                                       n_sources=req.n_sources, az_grid_deg=az)


class PhaseInterferometryRequest(BaseModel):
    snapshots: list[GenericObservation]
    carrier_hz: float = Field(..., gt=0)
    prior_az_deg: Optional[float] = None


@router.post("/phase_interferometry")
def phase_interferometry(req: PhaseInterferometryRequest, _auth=Depends(require_auth)):
    return sc.phase_interferometry_doa(_materialise(req.snapshots),
                                         carrier_hz=req.carrier_hz,
                                         prior_az_deg=req.prior_az_deg)


# ── Multi-receiver TDOA ──────────────────────────────────────────────────────
class TdoaPair(BaseModel):
    ref_id: str
    other_id: str
    dt_s: float


class TdoaRequest(BaseModel):
    receivers: list[GenericObservation]
    tdoa_pairs: Optional[list[TdoaPair]] = None
    sigma_ns: float = Field(50.0, gt=0)
    grid_span_m: float = Field(100_000.0, gt=0)
    grid_step_m: float = Field(100.0, gt=0)


@router.post("/tdoa_multi_receiver")
def tdoa_multi_receiver(req: TdoaRequest, _auth=Depends(require_auth)):
    pairs = [p.model_dump() for p in req.tdoa_pairs] if req.tdoa_pairs else None
    return sc.tdoa_multi_receiver_fix(_materialise(req.receivers),
                                       tdoa_pairs=pairs, sigma_ns=req.sigma_ns,
                                       grid_span_m=req.grid_span_m, grid_step_m=req.grid_step_m)


# ── ML grid fusion ───────────────────────────────────────────────────────────
class MlGridRequest(BaseModel):
    observations: list[dict]                              # keep raw — these carry nested {ref, other} for tdoa kind
    centre: Optional[list[float]] = None                  # [lat, lon]
    grid_span_m: float = Field(50_000.0, gt=0)
    grid_step_m: float = Field(100.0, gt=0)
    path_loss_n: float = Field(3.0, ge=0.5, le=6.0)
    p_tx_dbm: Optional[float] = None
    carrier_hz: Optional[float] = None
    sigma_aoa_deg: float = Field(3.0, gt=0)
    sigma_rss_db: float = Field(6.0, gt=0)
    sigma_hz: float = Field(5.0, gt=0)
    sigma_ns: float = Field(50.0, gt=0)


@router.post("/ml_grid_fusion")
def ml_grid_fusion(req: MlGridRequest, _auth=Depends(require_auth)):
    centre = tuple(req.centre) if req.centre and len(req.centre) == 2 else None
    return sc.ml_grid_fusion(req.observations, centre=centre,
                               grid_span_m=req.grid_span_m, grid_step_m=req.grid_step_m,
                               p_tx_dbm=req.p_tx_dbm, path_loss_n=req.path_loss_n,
                               carrier_hz=req.carrier_hz,
                               sigma_aoa_deg=req.sigma_aoa_deg, sigma_rss_db=req.sigma_rss_db,
                               sigma_hz=req.sigma_hz, sigma_ns=req.sigma_ns)


# ── EKF kinematic track ──────────────────────────────────────────────────────
class EkfRequest(BaseModel):
    observations: list[GenericObservation]
    initial_centre: Optional[list[float]] = None
    path_loss_n: float = Field(3.0, ge=0.5, le=6.0)
    p_tx_dbm: Optional[float] = None
    carrier_hz: Optional[float] = None
    sigma_aoa_deg: float = Field(3.0, gt=0)
    sigma_rss_db: float = Field(6.0, gt=0)
    sigma_hz: float = Field(5.0, gt=0)


@router.post("/ekf_track")
def ekf_track(req: EkfRequest, _auth=Depends(require_auth)):
    centre = tuple(req.initial_centre) if req.initial_centre and len(req.initial_centre) == 2 else None
    return sc.ekf_track_fix(_materialise(req.observations), initial_centre=centre,
                              path_loss_n=req.path_loss_n, p_tx_dbm=req.p_tx_dbm,
                              carrier_hz=req.carrier_hz, sigma_aoa_deg=req.sigma_aoa_deg,
                              sigma_rss_db=req.sigma_rss_db, sigma_hz=req.sigma_hz)
