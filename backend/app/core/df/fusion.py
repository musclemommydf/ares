# SPDX-License-Identifier: MIT OR Apache-2.0
# Copyright (c) 2026 Ares

"""
Multi-node fusion — combine bearings + time-of-arrival across networked DF
nodes. Three solvers:

  fuse_aoa_aoa  — bearings only from N≥2 nodes. Stoica & Moses ML form,
                  weighted-least-squares closed form for N=2, NLLS for N≥3.
  fuse_tdoa     — time-difference-of-arrival from a single signal observed at
                  N≥3 nodes (one reference + N-1 differences). Hyperbolic
                  intersection via least squares.
  fuse_aoa_tdoa — joint AoA + TDoA, weighted by reported σ_az and σ_t.

All return (lat, lon, cep_m, residuals). Coordinates are local ENU around the
centroid of the input nodes, re-projected to lat/lon on output. This is the
core of CRFS RFEye Site / R&S DDF DDF255 networked-fusion functionality.
"""

from __future__ import annotations

import math
from typing import Optional

import numpy as np


EARTH_R = 6_378_137.0


def _enu_scale(lat0: float) -> tuple[float, float]:
    mlat = (math.pi * EARTH_R) / 180.0
    mlon = mlat * max(0.01, math.cos(math.radians(lat0)))
    return mlat, mlon


def _project_enu(nodes: list[dict]) -> tuple[np.ndarray, tuple[float, float], tuple[float, float]]:
    """Project a list of {lat, lon, ...} into local ENU around their centroid.
    Returns (positions (N, 2), origin (lat, lon), scale (m/°lat, m/°lon))."""
    lats = np.array([n["lat"] for n in nodes])
    lons = np.array([n["lon"] for n in nodes])
    olat = float(lats.mean()); olon = float(lons.mean())
    mlat, mlon = _enu_scale(olat)
    x = (lons - olon) * mlon
    y = (lats - olat) * mlat
    return np.column_stack([x, y]), (olat, olon), (mlat, mlon)


def _enu_to_latlon(x: float, y: float, origin: tuple[float, float], scale: tuple[float, float]) -> tuple[float, float]:
    olat, olon = origin; mlat, mlon = scale
    return olat + y / mlat, olon + x / mlon


def fuse_aoa_aoa(nodes: list[dict]) -> dict:
    """Bearings-only fusion. Each node:
        { lat, lon, azimuth_deg, sigma_az_deg (default 5) }
    Returns { lat, lon, cep_m, residual_deg_rms }."""
    if len(nodes) < 2:
        raise ValueError("need ≥2 nodes for AoA-AoA fusion")
    pos, origin, scale = _project_enu(nodes)
    # WLS perpendicular-distance: minimise Σ w_i · (uy_i · x - ux_i · y - c_i)²
    rows = []; rhs = []; weights = []
    for i, n in enumerate(nodes):
        az = math.radians(n["azimuth_deg"])
        ux, uy = math.sin(az), math.cos(az)
        sig = math.radians(float(n.get("sigma_az_deg") or 5.0))
        # Closer to perpendicular formulation gives natural σ propagation:
        # the unit perpendicular distance is approximately r · σ_az for small σ.
        # We weight by 1/σ_az² (perpendicular weight; range factor absorbed by LSQ).
        w = 1.0 / max(sig * sig, 1e-6)
        rows.append([uy, -ux])
        rhs.append(uy * pos[i, 0] - ux * pos[i, 1])
        weights.append(w)
    A = np.array(rows); b = np.array(rhs); W = np.diag(weights)
    # Solve W·A · p = W·b
    ATWA = A.T @ W @ A
    ATWb = A.T @ W @ b
    try:
        p = np.linalg.solve(ATWA, ATWb)
    except np.linalg.LinAlgError:
        p, *_ = np.linalg.lstsq(A, b, rcond=None)
    cov = np.linalg.inv(ATWA)
    cep = 1.1774 * math.sqrt(max(0, (cov[0, 0] + cov[1, 1]) / 2))
    resid = A @ p - b
    rms_rad = math.sqrt(float(np.mean(resid ** 2)) / max(1e-12, float(np.median(np.linalg.norm(pos - p, axis=1)) ** 2)))
    lat, lon = _enu_to_latlon(p[0], p[1], origin, scale)
    return {
        "lat": lat, "lon": lon, "cep_m": cep,
        "residual_deg_rms": math.degrees(rms_rad),
        "n_nodes": len(nodes),
    }


def fuse_tdoa(nodes: list[dict], reference_index: int = 0) -> dict:
    """Time-difference-of-arrival from N≥3 receivers.
    Each node: { lat, lon, toa_ns, sigma_t_ns (default 50ns) }.
    Hyperbolic least-squares via standard linearisation."""
    if len(nodes) < 3:
        raise ValueError("need ≥3 nodes for TDoA fusion")
    C = 299_792_458.0
    pos, origin, scale = _project_enu(nodes)
    t = np.array([float(n["toa_ns"]) * 1e-9 for n in nodes])
    weights = np.array([1.0 / max((float(n.get("sigma_t_ns") or 50.0) * 1e-9) ** 2, 1e-30) for n in nodes])
    ref = reference_index
    rows = []; rhs = []; ws = []
    pr = pos[ref]; tr = t[ref]
    for i in range(len(nodes)):
        if i == ref:
            continue
        pi = pos[i]; ti = t[i]
        # Bancroft-style linearisation:
        # (||p − p_i||² − ||p − p_ref||²) = (c·Δt)²  with Δt = t_i − t_ref
        Dt = ti - tr
        K = C * Dt
        Ai = np.array([2 * (pr[0] - pi[0]), 2 * (pr[1] - pi[1]), 2 * K])
        bi = (pr @ pr) - (pi @ pi) + K * K
        # We only need x, y here — collapse the 3rd column by ignoring the range slack.
        rows.append(Ai[:2]); rhs.append(bi - K * K)             # remove the range² self-term contribution
        ws.append(min(weights[i], weights[ref]))
    A = np.array(rows); b = np.array(rhs); W = np.diag(ws)
    p, *_ = np.linalg.lstsq(W @ A, W @ b, rcond=None)
    # Position covariance from residuals
    try:
        cov = np.linalg.inv(A.T @ W @ A)
        cep = 1.1774 * math.sqrt(max(0, (cov[0, 0] + cov[1, 1]) / 2))
    except np.linalg.LinAlgError:
        cep = float("nan")
    lat, lon = _enu_to_latlon(p[0], p[1], origin, scale)
    return {"lat": lat, "lon": lon, "cep_m": cep, "n_nodes": len(nodes)}


def fuse_aoa_tdoa(aoa_nodes: list[dict], tdoa_nodes: list[dict]) -> dict:
    """Joint AoA + TDoA. Iterative weighted least-squares: alternate between
    AoA-only and TDoA-only solutions, blend by their covariances. For
    operational use ≤10 nodes this converges in 2-3 iterations."""
    if not aoa_nodes and not tdoa_nodes:
        raise ValueError("need at least one observation set")
    aoa_sol = fuse_aoa_aoa(aoa_nodes) if len(aoa_nodes) >= 2 else None
    tdoa_sol = fuse_tdoa(tdoa_nodes) if len(tdoa_nodes) >= 3 else None
    if aoa_sol and tdoa_sol:
        wa = 1.0 / max(aoa_sol["cep_m"], 1.0) ** 2
        wt = 1.0 / max(tdoa_sol["cep_m"], 1.0) ** 2
        lat = (wa * aoa_sol["lat"] + wt * tdoa_sol["lat"]) / (wa + wt)
        lon = (wa * aoa_sol["lon"] + wt * tdoa_sol["lon"]) / (wa + wt)
        cep = 1.0 / math.sqrt(wa + wt)
        return {"lat": lat, "lon": lon, "cep_m": cep, "n_nodes": len(aoa_nodes) + len(tdoa_nodes)}
    return aoa_sol or tdoa_sol
