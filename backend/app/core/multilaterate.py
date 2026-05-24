# SPDX-License-Identifier: MIT OR Apache-2.0
# Copyright (c) 2026 Ares

"""
TDOA / FDOA multilateration (Workstream D — geolocation).

Hyperbolic (time-difference-of-arrival) and, optionally, Doppler-difference
(frequency-difference-of-arrival) emitter location from ≥3 spatially separated
receivers — the technique CRFS/Epiq/R&S networked systems use, which the
bearing-only path in :mod:`app.core.geolocation` doesn't cover.

Approach: ENU about the mean receiver position; Chan-style closed-form (linearised
TDOA) for the initial guess, then weighted Gauss-Newton on the TDOA residuals
(analytic Jacobian) plus optional FDOA residuals (finite-difference Jacobian);
emitter-position covariance from (JᵀWJ)⁻¹ → GDOP and a geometry-correct error
ellipse (reusing :func:`app.core.geolocation.error_ellipse_from_cov`). 2-D solve
(receivers and emitter taken at a common height).
"""
from __future__ import annotations

import math
from typing import Optional, Sequence

import numpy as np

from app.core.geolocation import (
    M_PER_DEG_LAT, _mpd_lon, _ellipse_polygon, error_ellipse_from_cov, cep_from_cov,
)

C_LIGHT = 299_792_458.0


def _enu(receivers: Sequence[dict]) -> tuple[float, float, float, np.ndarray]:
    lat0 = sum(r["lat"] for r in receivers) / len(receivers)
    lon0 = sum(r["lon"] for r in receivers) / len(receivers)
    mpd_lon = _mpd_lon(lat0)
    xy = np.array([[(r["lon"] - lon0) * mpd_lon, (r["lat"] - lat0) * M_PER_DEG_LAT] for r in receivers], dtype=float)
    return lat0, lon0, mpd_lon, xy


def _chan_initial(xy: np.ndarray, rd: np.ndarray, ref: int) -> np.ndarray:
    """Linearised (Chan/Friedlander) closed-form TDOA fix → initial guess (ENU m).
    `rd[i]` = c·TDOA_i = R_i − R_ref for i ≠ ref. Solved in a frame with the
    reference receiver at the origin: with unknowns (p_x, p_y, ρ=‖p‖),
        2·x_i'·p_x + 2·y_i'·p_y + 2·rd_i·ρ = ‖x_i'‖² − rd_i²        (i ≠ ref)."""
    n = xy.shape[0]
    p0 = xy[ref]
    rows, b = [], []
    for i in range(n):
        if i == ref:
            continue
        xp = xy[i] - p0
        rows.append([2.0 * xp[0], 2.0 * xp[1], 2.0 * rd[i]])
        b.append(float(xp @ xp) - rd[i] ** 2)
    A = np.asarray(rows, dtype=float)
    bb = np.asarray(b, dtype=float)
    try:
        sol, *_ = np.linalg.lstsq(A, bb, rcond=None)
        return np.array([sol[0], sol[1]]) + p0
    except np.linalg.LinAlgError:
        return xy.mean(axis=0)


def tdoa_fdoa_fix(
    receivers: Sequence[dict],                 # [{lat, lon, vx?, vy?}] — vx/vy = ENU velocity (m/s) for FDOA
    tdoa_s: Sequence[float],                   # TDOA_i (s) for each receiver, relative to `ref_index` (ref's entry ignored)
    tdoa_sigma_s: Optional[Sequence[float]] = None,
    fdoa_hz: Optional[Sequence[float]] = None,  # FDOA_i (Hz) relative to ref, or None for TDOA-only
    fdoa_sigma_hz: Optional[Sequence[float]] = None,
    freq_hz: float = 1.0e9,                    # carrier (needed to turn FDOA into a geometry constraint)
    ref_index: int = 0,
    max_iter: int = 30,
) -> dict:
    """Solve emitter lat/lon from TDOAs (and optional FDOAs). Returns lat/lon, the
    ENU position covariance, position σ, GDOP, the 95 % error ellipse, the residual
    norm, and a GeoJSON FeatureCollection (receivers, emitter point, error ellipse)."""
    n = len(receivers)
    if n < 3:
        raise ValueError("TDOA multilateration needs ≥3 receivers")
    if len(tdoa_s) != n:
        raise ValueError("tdoa_s must have one entry per receiver (the ref entry is ignored)")
    ref = max(0, min(n - 1, int(ref_index)))
    lat0, lon0, mpd_lon, xy = _enu(receivers)
    sig_t = list(tdoa_sigma_s) if tdoa_sigma_s is not None else [30e-9] * n   # 30 ns default
    have_fdoa = fdoa_hz is not None and any(v is not None for v in fdoa_hz)
    vel = np.array([[float(r.get("vx", 0.0)), float(r.get("vy", 0.0))] for r in receivers], dtype=float)
    sig_f = list(fdoa_sigma_hz) if fdoa_sigma_hz is not None else [10.0] * n   # 10 Hz default
    lam = C_LIGHT / max(1.0, float(freq_hz))

    rd = np.zeros(n)                                    # c·TDOA, R_i − R_ref
    for i in range(n):
        rd[i] = C_LIGHT * float(tdoa_s[i])
    rd[ref] = 0.0

    p = _chan_initial(xy, rd, ref)

    def ranges(pt):
        d = pt - xy
        R = np.sqrt((d * d).sum(axis=1))
        R = np.maximum(R, 1.0)
        u = d / R[:, None]                              # unit vectors emitter→… actually …→emitter? d = pt - xy ⇒ from receiver to emitter
        return R, u

    def fdoa_model(pt):
        # Doppler at receiver i (stationary emitter, receiver moving v_i):
        # f_d,i = (1/λ) v_i · (r_i − p)/|r_i − p| = -(1/λ) v_i · u_i  (u_i = (p−r_i)/R_i)
        R, u = ranges(pt)
        fd = -(vel * u).sum(axis=1) / lam
        return fd - fd[ref]

    # Gauss-Newton
    last_res = float("inf")
    for _ in range(max_iter):
        R, u = ranges(p)
        # TDOA residuals + analytic Jacobian
        rows, res, w = [], [], []
        for i in range(n):
            if i == ref:
                continue
            pred = R[i] - R[ref]
            res.append(rd[i] - pred)
            j = u[i] - u[ref]                            # ∂(R_i − R_ref)/∂p
            rows.append(j)
            w.append(1.0 / (C_LIGHT * sig_t[i]) ** 2)
        if have_fdoa:
            fd_pred = fdoa_model(p)
            eps = 1.0
            for i in range(n):
                if i == ref or fdoa_hz[i] is None:
                    continue
                res.append(float(fdoa_hz[i]) - fd_pred[i])
                # finite-difference Jacobian for FDOA (analytic form is messy; this is exact to O(eps))
                gx = (fdoa_model(p + np.array([eps, 0.0]))[i] - fd_pred[i]) / eps
                gy = (fdoa_model(p + np.array([0.0, eps]))[i] - fd_pred[i]) / eps
                rows.append(np.array([gx, gy]))
                w.append(1.0 / (sig_f[i]) ** 2)
        if len(rows) < 2:
            break
        J = np.asarray(rows, dtype=float)
        rr = np.asarray(res, dtype=float)
        W = np.diag(w)
        JtWJ = J.T @ W @ J
        JtWr = J.T @ W @ rr
        try:
            step = np.linalg.solve(JtWJ, JtWr)
        except np.linalg.LinAlgError:
            break
        # damped step (Levenberg-ish guard against overshoot far from the solution)
        if np.linalg.norm(step) > 5e5:
            step = step / np.linalg.norm(step) * 5e5
        p = p + step
        cur = float(np.sqrt((rr * rr).sum()))
        if abs(last_res - cur) < 1e-6 or np.linalg.norm(step) < 0.5:
            last_res = cur
            break
        last_res = cur

    # final covariance / GDOP
    R, u = ranges(p)
    rows, w, res_scaled = [], [], []
    rows_geo = []
    for i in range(n):
        if i == ref:
            continue
        rows.append(u[i] - u[ref])
        rows_geo.append((u[i] - u[ref]) / C_LIGHT)      # geometry per second of TDOA
        w.append(1.0 / (C_LIGHT * sig_t[i]) ** 2)
        res_scaled.append((rd[i] - (R[i] - R[ref])) / (C_LIGHT * sig_t[i]))
    m = len(rows)
    J = np.asarray(rows, dtype=float)
    W = np.diag(w)
    try:
        cov = np.linalg.inv(J.T @ W @ J)
        chi2 = float(np.sum(np.asarray(res_scaled) ** 2))
        dof = max(1, m - 2)
        cov = cov * max(1.0, chi2 / dof)
    except np.linalg.LinAlgError:
        cov = np.array([[1e10, 0.0], [0.0, 1e10]])
    try:
        Jg = np.asarray(rows_geo, dtype=float)
        gdop = math.sqrt(max(0.0, float(np.trace(np.linalg.inv(Jg.T @ Jg)))))   # metres per second of TDOA error
    except np.linalg.LinAlgError:
        gdop = float("inf")

    emitter_lat = lat0 + float(p[1]) / M_PER_DEG_LAT
    emitter_lon = lon0 + float(p[0]) / mpd_lon
    smaj95, smin95, rot95 = error_ellipse_from_cov(cov, 0.95)
    cep_m = cep_from_cov(cov)
    pos_sigma = math.sqrt(max(0.0, float(np.trace(cov))))

    feats = []
    for r in receivers:
        feats.append({"type": "Feature", "geometry": {"type": "Point", "coordinates": [r["lon"], r["lat"]]},
                      "properties": {"type": "tdoa_receiver"}})
    feats.append({"type": "Feature",
                  "geometry": {"type": "Polygon", "coordinates": [_ellipse_polygon(emitter_lat, emitter_lon, smaj95, smin95, rot95)]},
                  "properties": {"type": "error_ellipse_95", "confidence": 0.95,
                                 "semiMajorM": round(smaj95), "semiMinorM": round(smin95), "rotDeg": round(rot95, 1)}})
    feats.append({"type": "Feature", "geometry": {"type": "Point", "coordinates": [emitter_lon, emitter_lat]},
                  "properties": {"type": "suspected_emitter", "kind": "tdoa" + ("_fdoa" if have_fdoa else ""),
                                 "method": "tdoa_fdoa" if have_fdoa else "tdoa", "n_receivers": n,
                                 "frequency_hz": freq_hz, "cep_m": round(cep_m),
                                 "position_sigma_m": round(pos_sigma), "gdop": round(gdop, 1) if math.isfinite(gdop) else None}})

    return {
        "lat": emitter_lat, "lon": emitter_lon,
        "covariance_enu": [[float(cov[0, 0]), float(cov[0, 1])], [float(cov[1, 0]), float(cov[1, 1])]],
        "position_sigma_m": pos_sigma, "gdop": gdop, "residual_norm": last_res,
        "cep_m": cep_m, "error_ellipse_95": {"semiMajorM": smaj95, "semiMinorM": smin95, "rotDeg": rot95},
        "n_receivers": n, "used_fdoa": have_fdoa,
        "geojson": {"type": "FeatureCollection", "features": feats},
    }
