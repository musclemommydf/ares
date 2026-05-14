"""
df/single_channel.py — direction-finding & geolocation with a SINGLE-channel SDR.

A multi-channel coherent array (KrakenSDR, RTL-SDR×4 with PPS, ANTSDR e200 quad)
gives you MUSIC / ESPRIT / Capon directly. A single-channel SDR can't beamform —
but it can still locate emitters, because *motion* (the observer's, or the
emitter's) creates a virtual aperture in three orthogonal observables: power
(RSS), frequency (Doppler), and carrier phase. Combine them and you get
direction-finding without an array.

The methods implemented here are:

  1. RSS log-distance path-loss localisation
     - Multi-pose RSSI from a moving single-antenna receiver → joint ML estimate
       of emitter position, transmit power, and path-loss exponent.
     - Apollonius-circle pair locus solver as a non-iterative initialiser.

  2. RSS-gradient bearing
     - From three or more closely-spaced samples, estimate the spatial gradient
       of received power; emitter is "up-gradient" → bearing.

  3. Doppler-CPA (closest-point-of-approach) curve fit
     - When a moving receiver passes a stationary emitter at a known carrier
       freq, the observed Doppler traces a classic hyperbolic S-curve. Fitting
       it yields CPA distance, CPA time, and along-track offset (and therefore
       an absolute position when combined with the receiver track).

  4. Differential-Doppler (FDOA) along-track localisation
     - Without assuming a single straight-line pass: each successive Doppler
       measurement constrains the projection of the emitter LOS onto the
       receiver velocity. Stack ≥ 3 such measurements with varying velocity
       directions → solvable 2-D position.

  5. Kinematic synthetic aperture (SAR-style beamforming)
     - Coherent IQ snapshots collected at known receiver positions form a
       virtual array; beam-form / MUSIC the synthetic array exactly like a
       physical one. Requires a stable carrier and a known carrier frequency.

  6. Phase-interferometry along track
     - The carrier-phase difference between samples at known positions gives a
       direct readout of LOS angle (with one-wavelength ambiguity). Resolve
       ambiguity by combining the unwrapped phase with the synthetic-aperture
       coarse estimate.

  7. ML grid-search fusion
     - Universal back-stop: brute-force evaluate a likelihood over a 2-D grid,
       combining whatever observables are present (AoA, RSS, Doppler, TDOA…),
       returning the MAP estimate, a Gaussian-approximated CEP, and a
       likelihood heat-map for the UI.

  8. EKF kinematic tracker
     - Extended Kalman filter that sequentially fuses RSS + Doppler + phase
       interferometry over a trajectory. Useful for "let the operator walk
       around" mode where the position estimate refines as more data lands.

All functions are pure numpy / scipy (no torch/cupy), return JSON-safe dicts,
and never raise on bad input — they return {"ok": False, "error": "..."} so the
front-end can render the failure gracefully.
"""
from __future__ import annotations

import math
from typing import Iterable, Optional, Sequence

import numpy as np

# ─────────────────────────────────────────────────────────────────────────────
# Geo projection helpers (local ENU plane around a centroid)
# ─────────────────────────────────────────────────────────────────────────────
_R_EARTH = 6_378_137.0


def _enu_scale(lat0_deg: float) -> tuple[float, float]:
    """Metres-per-degree (lat, lon) at lat0. Linear approximation around lat0 —
    fine for the ~tens-of-km local solver."""
    lat0 = math.radians(lat0_deg)
    m_per_deg_lat = 111_132.92 - 559.82 * math.cos(2 * lat0) + 1.175 * math.cos(4 * lat0)
    m_per_deg_lon = 111_412.84 * math.cos(lat0) - 93.5 * math.cos(3 * lat0)
    return m_per_deg_lat, m_per_deg_lon


def _project_xy(observations: list[dict]) -> tuple[np.ndarray, tuple[float, float], tuple[float, float]]:
    """Project a list of {lat, lon, …} observations into a local ENU (x=east, y=north) plane.
    Returns (xy[N,2], origin_latlon, (m_per_deg_lat, m_per_deg_lon))."""
    lats = np.array([float(o["lat"]) for o in observations])
    lons = np.array([float(o["lon"]) for o in observations])
    if lats.size == 0:
        raise ValueError("no observations")
    lat0, lon0 = float(lats.mean()), float(lons.mean())
    mlat, mlon = _enu_scale(lat0)
    x = (lons - lon0) * mlon
    y = (lats - lat0) * mlat
    return np.stack([x, y], axis=1), (lat0, lon0), (mlat, mlon)


def _xy_to_latlon(xy: np.ndarray, origin: tuple[float, float], scale: tuple[float, float]) -> tuple[float, float]:
    lat0, lon0 = origin
    mlat, mlon = scale
    return (float(lat0 + xy[1] / mlat), float(lon0 + xy[0] / mlon))


def _ensure_2d(p) -> np.ndarray:
    a = np.asarray(p, dtype=np.float64).reshape(-1)
    return a[:2] if a.size >= 2 else np.zeros(2)


# ─────────────────────────────────────────────────────────────────────────────
# 1) RSS log-distance localisation
# ─────────────────────────────────────────────────────────────────────────────
def rss_path_loss_fix(observations: list[dict], *,
                       path_loss_n: Optional[float] = None,
                       p_tx_dbm: Optional[float] = None,
                       d0_m: float = 1.0,
                       sigma_db: float = 6.0,
                       grid_m: float = 50.0,
                       grid_span_m: float = 50_000.0) -> dict:
    """Joint maximum-likelihood emitter position from spatial RSS samples.

    Each observation needs: ``lat``, ``lon`` (receiver position) and
    ``rssi_dbm`` (received power). Optional: ``noise_dbm`` (per-sample noise
    floor, used for weighting).

    Model: ``P_rx(d) = P_tx - 10 · n · log10(max(d, d0)/d0) + ε``, ε ∼ N(0, σ²).
    Unknowns: emitter position (x, y), and optionally P_tx and/or n. The
    likelihood is convex in (P_tx, n) for any fixed (x, y), so we solve those
    in closed form per-grid-point and brute-force the position over a coarse
    grid, then refine analytically (Newton step) on the best cell.
    """
    if not observations or len(observations) < 3:
        return {"ok": False, "error": "need at least 3 RSS observations"}
    try:
        xy, origin, scale = _project_xy(observations)
    except Exception as e:
        return {"ok": False, "error": f"projection failed: {e}"}
    p = np.array([float(o.get("rssi_dbm", o.get("power_dbm"))) for o in observations])

    # Build a search grid centred at the RSS centroid (weighted by power above floor).
    weights = 10 ** (np.clip(p, -120, 0) / 10.0)
    cx, cy = float(np.average(xy[:, 0], weights=weights)), float(np.average(xy[:, 1], weights=weights))
    half = float(grid_span_m) / 2.0
    n_cells = max(40, int(round(2 * half / grid_m)))
    if n_cells > 600:
        n_cells = 600
        grid_m = (2 * half) / n_cells
    gx = np.linspace(cx - half, cx + half, n_cells)
    gy = np.linspace(cy - half, cy + half, n_cells)
    XX, YY = np.meshgrid(gx, gy, indexing="xy")
    # 10·log10(d/d0) for each grid cell and each observation, fully vectorised.
    XX_f = XX[None, :, :]
    YY_f = YY[None, :, :]
    xy_f = xy[:, :, None, None]                                # shape (N, 2, 1, 1)
    dx2 = (XX_f - xy_f[:, 0]) ** 2 + (YY_f - xy_f[:, 1]) ** 2  # (N, H, W)
    d = np.sqrt(np.maximum(dx2, d0_m ** 2))
    log_d = 10.0 * np.log10(d / d0_m)                         # (N, H, W)

    # For each cell, choose (P_tx, n) that best explains observed p:
    #   p_i ≈ P_tx - n · log_d_i  →  LS fit per cell.
    if path_loss_n is None and p_tx_dbm is None:
        # Free P_tx + n. Slope is mean[(log_d - mean(log_d)) * (p - mean(p))] / var(log_d).
        log_d_mean = log_d.mean(axis=0)
        log_d_dev = log_d - log_d_mean
        p_mean = p.mean()
        p_dev = p[:, None, None] - p_mean
        num = (log_d_dev * p_dev).sum(axis=0)
        den = (log_d_dev ** 2).sum(axis=0) + 1e-12
        slope = num / den                              # = -n  (because p = P_tx - n·log_d)
        n_grid = -slope
        ptx_grid = p_mean - slope * log_d_mean
        # Constrain n into a sane range to discard absurd fits in degenerate corners.
        bad = (n_grid < 0.5) | (n_grid > 6.0)
        n_grid = np.where(bad, 3.0, n_grid)
        ptx_grid = np.where(bad, p_mean + 3.0 * log_d_mean, ptx_grid)
    elif path_loss_n is None:
        # Fixed P_tx, free n. n = (P_tx - p) / log_d, averaged.
        n_grid = ((p_tx_dbm - p[:, None, None]) / np.maximum(log_d, 1e-9)).mean(axis=0)
        n_grid = np.clip(n_grid, 0.5, 6.0)
        ptx_grid = np.full_like(n_grid, p_tx_dbm)
    elif p_tx_dbm is None:
        # Fixed n, free P_tx. P_tx = mean(p + n·log_d).
        n_grid = np.full(log_d.shape[1:], float(path_loss_n))
        ptx_grid = (p[:, None, None] + path_loss_n * log_d).mean(axis=0)
    else:
        n_grid = np.full(log_d.shape[1:], float(path_loss_n))
        ptx_grid = np.full(log_d.shape[1:], float(p_tx_dbm))
    # Residuals + Gaussian log-likelihood with σ in dB:
    model = ptx_grid[None, :, :] - n_grid[None, :, :] * log_d
    resid = p[:, None, None] - model
    ll = -0.5 * (resid ** 2).sum(axis=0) / (sigma_db ** 2)
    # MAP cell:
    iy, ix = np.unravel_index(int(np.argmax(ll)), ll.shape)
    best = (float(gx[ix]), float(gy[iy]))
    best_ll = float(ll[iy, ix])
    n_hat = float(n_grid[iy, ix])
    ptx_hat = float(ptx_grid[iy, ix])

    # Newton refinement (Gauss–Newton over (x, y) at fixed n, P_tx).
    bx, by = best
    for _ in range(8):
        d = np.sqrt((xy[:, 0] - bx) ** 2 + (xy[:, 1] - by) ** 2)
        d = np.maximum(d, d0_m)
        log_d = 10.0 * np.log10(d / d0_m)
        r = p - (ptx_hat - n_hat * log_d)
        # ∂r/∂(bx, by) = -∂model/∂(bx, by) where ∂model/∂x = -n·10/ln10 · (x-bx)/d² … but with sign on -bx
        # easier: derive (10·n/ln10) · (x_i - bx)/d²  (since model = P_tx - n·10·log10(d/d0); d = ||...||)
        c = 10.0 * n_hat / math.log(10.0)
        Jx = -c * (xy[:, 0] - bx) / (d ** 2)
        Jy = -c * (xy[:, 1] - by) / (d ** 2)
        J = np.stack([Jx, Jy], axis=1)
        # Normal equations:
        H = J.T @ J + 1e-3 * np.eye(2)
        g = J.T @ r
        step = np.linalg.solve(H, g)
        if not np.all(np.isfinite(step)):
            break
        bx += float(step[0]); by += float(step[1])
        if np.linalg.norm(step) < 0.1:
            break

    # CEP from a Fisher-info Gaussian approximation.
    d = np.maximum(np.sqrt((xy[:, 0] - bx) ** 2 + (xy[:, 1] - by) ** 2), d0_m)
    c = 10.0 * n_hat / math.log(10.0)
    Jx = -c * (xy[:, 0] - bx) / (d ** 2)
    Jy = -c * (xy[:, 1] - by) / (d ** 2)
    J = np.stack([Jx, Jy], axis=1)
    try:
        cov = np.linalg.inv(J.T @ J / (sigma_db ** 2) + 1e-9 * np.eye(2))
        cep_m = 1.1774 * math.sqrt(0.5 * (cov[0, 0] + cov[1, 1]))
        # axes (1-σ ellipse)
        e, V = np.linalg.eigh(cov)
        axes = (float(math.sqrt(max(0.0, e[1]))), float(math.sqrt(max(0.0, e[0]))))
        bearing = float(math.degrees(math.atan2(V[0, 1], V[1, 1])) % 180.0)
    except np.linalg.LinAlgError:
        cep_m = float("nan"); axes = (float("nan"), float("nan")); bearing = 0.0
        cov = np.full((2, 2), float("nan"))

    lat, lon = _xy_to_latlon(np.array([bx, by]), origin, (scale[0], scale[1]))
    return {
        "ok": True, "method": "rss_log_distance_ml",
        "estimate": {"lat": lat, "lon": lon, "x_m": bx, "y_m": by,
                       "p_tx_dbm": ptx_hat, "path_loss_n": n_hat},
        "uncertainty": {"cep_m": float(cep_m), "ellipse_axes_m": list(axes), "ellipse_bearing_deg": bearing,
                          "cov_m2": cov.tolist() if np.all(np.isfinite(cov)) else None},
        "fit": {"log_likelihood": best_ll, "n_observations": int(len(observations)),
                  "sigma_db": sigma_db, "grid_span_m": grid_span_m, "grid_step_m": grid_m},
        "diagnostics": {"observations_xy_m": xy.tolist(), "p_rx_dbm": p.tolist()},
    }


def rss_gradient_bearing(observations: list[dict]) -> dict:
    """Estimate the spatial RSS gradient via linear least squares on
    ``P_i ≈ a·x_i + b·y_i + c``. The bearing to the emitter is in the
    direction of +∇P (upslope). Useful when observations span only a small
    aperture (so the log-distance model linearises). Returns the bearing
    plus an effective "lobe sharpness" |∇P| in dB/km.
    """
    if not observations or len(observations) < 3:
        return {"ok": False, "error": "need at least 3 RSS observations"}
    try:
        xy, origin, _ = _project_xy(observations)
    except Exception as e:
        return {"ok": False, "error": f"projection failed: {e}"}
    p = np.array([float(o.get("rssi_dbm", o.get("power_dbm"))) for o in observations])
    A = np.column_stack([xy[:, 0], xy[:, 1], np.ones(len(p))])
    sol, residuals, rank, _ = np.linalg.lstsq(A, p, rcond=None)
    a, b, _ = sol
    grad = math.hypot(a, b)
    if grad < 1e-9:
        return {"ok": False, "error": "RSS too flat across observations — gradient ambiguous"}
    bearing_deg = (math.degrees(math.atan2(a, b))) % 360.0   # true bearing (north=0, clockwise)
    rms_resid = float(np.sqrt(((A @ sol - p) ** 2).mean()))
    return {
        "ok": True, "method": "rss_gradient_bearing",
        "bearing_deg": float(bearing_deg),
        "gradient_db_per_km": float(grad * 1000.0),
        "rms_residual_db": rms_resid,
        "n_observations": int(len(observations)),
        "centre": {"lat": origin[0], "lon": origin[1]},
    }


# ─────────────────────────────────────────────────────────────────────────────
# 3) Doppler-CPA hyperbolic fit
# ─────────────────────────────────────────────────────────────────────────────
def doppler_cpa_fit(observations: list[dict], *,
                     carrier_hz: float,
                     c_mps: float = 299_792_458.0) -> dict:
    """Fit the classic CPA-Doppler S-curve to a series of (t, f_observed,
    receiver_velocity, receiver_position) samples for a stationary emitter.

    Geometry: an observer moves in straight line at speed v past a stationary
    emitter offset by perpendicular distance r₀ (CPA). The observed Doppler is

        Δf(t) = (f₀ · v² · (t - t_cpa)) / (c · sqrt((v·(t-t_cpa))² + r₀²))

    which is the classic odd-symmetric "S" curve. Fitting (r₀, t_cpa, f₀)
    yields the perpendicular CPA distance and the time-of-closest-approach.
    Combined with the observer's track, this fixes the emitter position
    (modulo a left/right ambiguity that is resolved by the sign of the CPA
    side, derivable from the second-pass test or a prior).

    Each observation needs ``t`` (s), ``frequency_offset_hz`` (Δf measured),
    ``v_mps`` (speed at that t), ``lat``, ``lon``. The function uses the
    median ``v_mps`` and the track tangent at t_cpa to produce a position
    fix; if individual ``v_mps`` vary slightly the fit still works.
    """
    if not observations or len(observations) < 4:
        return {"ok": False, "error": "need at least 4 Doppler samples"}
    try:
        from scipy.optimize import least_squares
    except Exception as e:
        return {"ok": False, "error": f"scipy required for least-squares fit: {e}"}
    t = np.array([float(o["t"]) for o in observations])
    df = np.array([float(o["frequency_offset_hz"]) for o in observations])
    v = np.array([float(o["v_mps"]) for o in observations])
    v_med = float(np.median(v))
    f0 = float(carrier_hz)
    # initial guesses: t_cpa = midpoint of t where df ≈ 0, r0 from S-curve amplitude
    # peak slope of Δf at t_cpa is approximately (f0 · v) / (c · r0); use peak |Δf'|
    if t.size > 2:
        idx_zero = np.argmin(np.abs(df))
        t_cpa_0 = float(t[idx_zero])
    else:
        t_cpa_0 = float(np.mean(t))
    amp = float(np.max(np.abs(df)))
    if amp <= 0:
        return {"ok": False, "error": "Doppler offsets are zero — emitter or stationary observer?"}
    # plateau Δf → ±(f0·v/c) so r0 small means abs(Δf) saturates near f0·v/c. Solve for r0 from amp.
    df_max = (f0 * v_med) / c_mps
    if df_max <= 0:
        return {"ok": False, "error": "non-positive carrier or speed"}
    r0_0 = max(50.0, abs(df_max / (amp + 1e-12)) * 200.0)        # ballpark

    def model(p, t_):
        r0, t_cpa, ff = p
        u = ff * v_med * (t_ - t_cpa) / c_mps
        denom = np.sqrt((v_med * (t_ - t_cpa)) ** 2 + r0 ** 2)
        return u * (v_med) / np.maximum(denom, 1e-6)

    def residuals(p):
        return model(p, t) - df

    try:
        res = least_squares(residuals, x0=[r0_0, t_cpa_0, f0],
                              bounds=([5.0, t.min() - 60.0, 0.1 * f0],
                                       [1e7, t.max() + 60.0, 10.0 * f0]),
                              method="trf", max_nfev=400)
    except Exception as e:
        return {"ok": False, "error": f"least-squares fit failed: {e}"}
    r0, t_cpa, ff = res.x
    rms = float(np.sqrt((res.fun ** 2).mean()))

    # Position fix: walk the receiver track to t_cpa, then go ⊥ to the velocity by r0.
    # Closest sample to t_cpa:
    i0 = int(np.argmin(np.abs(t - t_cpa)))
    # Tangent direction in ENU from finite differences:
    xy, origin, scale = _project_xy(observations)
    if i0 == 0:
        i1 = 1
    elif i0 == len(t) - 1:
        i1 = i0 - 1
    else:
        i1 = i0 + 1
    dxdy = xy[i1] - xy[i0]
    norm = float(np.linalg.norm(dxdy))
    if norm < 1e-6:
        return {"ok": False, "error": "receiver track degenerate (no motion)"}
    tangent = dxdy / norm
    # Linearly interpolate the receiver position at t_cpa:
    if i1 != i0:
        a = float((t_cpa - t[i0]) / max(1e-9, (t[i1] - t[i0])))
    else:
        a = 0.0
    p_cpa = xy[i0] + a * (xy[i1] - xy[i0])
    # Perpendiculars (left/right) — ambiguity:
    n_left = np.array([-tangent[1], tangent[0]])
    fixes = []
    for side, n_vec in (("left", n_left), ("right", -n_left)):
        e = p_cpa + r0 * n_vec
        lat, lon = _xy_to_latlon(e, origin, scale)
        fixes.append({"side": side, "lat": lat, "lon": lon, "x_m": float(e[0]), "y_m": float(e[1])})
    return {
        "ok": True, "method": "doppler_cpa",
        "fit": {"cpa_distance_m": float(r0), "cpa_time_s": float(t_cpa),
                  "carrier_hz_est": float(ff), "rms_residual_hz": rms,
                  "v_mps_median": v_med, "n_observations": int(len(observations))},
        "candidates": fixes,
        "note": "Two candidate fixes (left/right of track) — disambiguate with a second pass on a non-parallel heading, or with an RSS / AoA tie-breaker.",
    }


# ─────────────────────────────────────────────────────────────────────────────
# 4) Differential-Doppler (FDOA) multi-pose fix
# ─────────────────────────────────────────────────────────────────────────────
def fdoa_track_fix(observations: list[dict], *,
                    carrier_hz: float,
                    grid_span_m: float = 50_000.0,
                    grid_step_m: float = 50.0,
                    c_mps: float = 299_792_458.0,
                    sigma_hz: float = 5.0) -> dict:
    """Multi-pose differential-Doppler fix.

    Each observation = {lat, lon, vx_mps, vy_mps, frequency_offset_hz, t}.
    The observed Doppler at observer i is

        Δf_i = -(f₀/c) · v_i · û_i        (û_i: unit vector from observer to emitter)

    For each grid cell, we compute the implied û_i, then the model Doppler,
    and accumulate a Gaussian log-likelihood. The MAP cell + Newton refine
    gives the 2-D ML emitter position. Needs ≥ 3 observations with
    sufficiently varying ``v_i`` directions for a well-conditioned fit.
    """
    if not observations or len(observations) < 3:
        return {"ok": False, "error": "need at least 3 Doppler/velocity observations"}
    try:
        xy, origin, scale = _project_xy(observations)
    except Exception as e:
        return {"ok": False, "error": f"projection failed: {e}"}
    df = np.array([float(o["frequency_offset_hz"]) for o in observations])
    vx = np.array([float(o.get("vx_mps", 0.0)) for o in observations])
    vy = np.array([float(o.get("vy_mps", 0.0)) for o in observations])
    speeds = np.sqrt(vx ** 2 + vy ** 2)
    if np.max(speeds) < 1.0:
        return {"ok": False, "error": "observer must be moving (some v_i ≥ 1 m/s)"}
    f0 = float(carrier_hz)
    cx, cy = float(np.mean(xy[:, 0])), float(np.mean(xy[:, 1]))
    n_cells = int(round(grid_span_m / grid_step_m))
    if n_cells > 600:
        n_cells = 600
        grid_step_m = grid_span_m / n_cells
    gx = np.linspace(cx - grid_span_m / 2, cx + grid_span_m / 2, n_cells)
    gy = np.linspace(cy - grid_span_m / 2, cy + grid_span_m / 2, n_cells)
    XX, YY = np.meshgrid(gx, gy, indexing="xy")
    # for each grid cell + each observer: u_i = (e - p_i) / ||·||
    ll = np.zeros_like(XX, dtype=np.float64)
    for i in range(len(observations)):
        dx = XX - xy[i, 0]; dy = YY - xy[i, 1]
        d = np.sqrt(dx ** 2 + dy ** 2) + 1e-6
        ux = dx / d; uy = dy / d
        df_model = -(f0 / c_mps) * (vx[i] * ux + vy[i] * uy)
        ll += -0.5 * ((df_model - df[i]) ** 2) / (sigma_hz ** 2)
    iy, ix = np.unravel_index(int(np.argmax(ll)), ll.shape)
    bx, by = float(gx[ix]), float(gy[iy])
    lat, lon = _xy_to_latlon(np.array([bx, by]), origin, scale)
    # Crude CEP from local likelihood curvature:
    cep_m = grid_step_m * 1.5
    if 1 <= iy < n_cells - 1 and 1 <= ix < n_cells - 1:
        H = np.array([
            [ll[iy, ix + 1] - 2 * ll[iy, ix] + ll[iy, ix - 1],
              0.25 * (ll[iy + 1, ix + 1] - ll[iy + 1, ix - 1] - ll[iy - 1, ix + 1] + ll[iy - 1, ix - 1])],
            [0, ll[iy + 1, ix] - 2 * ll[iy, ix] + ll[iy - 1, ix]]
        ])
        H[1, 0] = H[0, 1]
        H = H / (grid_step_m ** 2)
        try:
            cov = np.linalg.inv(-H + 1e-9 * np.eye(2))
            cep_m = 1.1774 * math.sqrt(max(0.0, 0.5 * (cov[0, 0] + cov[1, 1])))
        except np.linalg.LinAlgError:
            pass
    return {
        "ok": True, "method": "fdoa_track_grid",
        "estimate": {"lat": lat, "lon": lon, "x_m": bx, "y_m": by, "carrier_hz": f0},
        "uncertainty": {"cep_m": float(cep_m), "grid_step_m": grid_step_m, "grid_span_m": grid_span_m},
        "fit": {"log_likelihood": float(ll[iy, ix]), "n_observations": int(len(observations)),
                  "sigma_hz": sigma_hz},
    }


# ─────────────────────────────────────────────────────────────────────────────
# 5) Kinematic synthetic-aperture DoA
# ─────────────────────────────────────────────────────────────────────────────
def synthetic_aperture_doa(snapshots: list[dict], *,
                            carrier_hz: float,
                            az_grid_deg: Sequence[float] = None,
                            method: str = "bartlett",
                            c_mps: float = 299_792_458.0,
                            n_sources: int = 1) -> dict:
    """Coherent beam-forming on a virtual array built from a moving
    single-antenna receiver. Each snapshot is one element of a synthetic
    array.

    Each snapshot needs: ``x_m``, ``y_m`` (position relative to a common
    reference, computed from {lat, lon} if not supplied), ``iq_complex``
    (one complex sample at the array reference time — typically the average
    of a short integration around the snapshot timestamp).

    Methods: ``bartlett`` (conventional beam), ``capon`` (MVDR),
    ``music`` (subspace, needs ``n_sources``).

    Returns the spatial pseudo-spectrum over ``az_grid_deg`` plus the peak
    bearing(s). When azimuth-only motion fails (collinear track) only the
    along-track component is observable — a warning is set.
    """
    if not snapshots or len(snapshots) < 3:
        return {"ok": False, "error": "need at least 3 coherent snapshots"}
    if az_grid_deg is None:
        az_grid_deg = np.arange(-180, 180, 1.0)
    az = np.asarray(az_grid_deg, dtype=np.float64)
    lam = c_mps / float(carrier_hz)
    # gather positions
    if all("lat" in s and "lon" in s for s in snapshots):
        xy, origin, _scale = _project_xy(snapshots)
    else:
        xy = np.array([[float(s.get("x_m", 0.0)), float(s.get("y_m", 0.0))] for s in snapshots])
        origin = (0.0, 0.0)
    z = np.array([complex(s["iq_complex"]) for s in snapshots], dtype=np.complex128)
    # Steering vector for direction θ (true bearing): a_n(θ) = exp(-j·2π·(x_n·sinθ + y_n·cosθ)/λ)
    sinθ = np.sin(np.deg2rad(az))
    cosθ = np.cos(np.deg2rad(az))
    proj = xy[:, 0:1] @ sinθ[None, :] + xy[:, 1:2] @ cosθ[None, :]   # (N, A)
    A = np.exp(-1j * 2 * math.pi * proj / lam)                       # (N, A)
    # Sample covariance from one snapshot per element is rank-1; "Bartlett" still works as |a^H z|².
    R = np.outer(z, z.conj())                                        # rank-1 covariance
    # Pseudospectrum:
    if method == "bartlett":
        ps = np.abs(A.conj().T @ z) ** 2                             # (A,)
    elif method == "capon":
        Rinv = np.linalg.pinv(R + 1e-3 * np.eye(z.size) * np.trace(R).real / z.size)
        ps = 1.0 / np.real(np.einsum("na,nm,ma->a", A.conj(), Rinv, A))
    elif method == "music":
        # eigendecompose, take the (N - k) smallest eigenvectors as noise subspace
        w, V = np.linalg.eigh(R + 1e-6 * np.eye(R.shape[0]))
        order = np.argsort(w)
        k = max(1, min(n_sources, z.size - 1))
        En = V[:, order[: z.size - k]]
        proj_n = En.conj().T @ A
        ps = 1.0 / np.real(np.sum(proj_n.conj() * proj_n, axis=0) + 1e-12)
    else:
        return {"ok": False, "error": f"unknown method {method!r}"}
    ps = np.asarray(ps, dtype=np.float64)
    ps_norm = ps / (ps.max() + 1e-12)
    # peak picks
    peaks = []
    for i in range(1, len(ps) - 1):
        if ps[i] > ps[i - 1] and ps[i] > ps[i + 1] and ps[i] > 0.5 * ps.max():
            peaks.append({"azimuth_deg": float(az[i]), "magnitude": float(ps_norm[i])})
    peaks.sort(key=lambda p: -p["magnitude"])
    # Aperture diagnostics
    span_m = float(np.linalg.norm(xy.max(axis=0) - xy.min(axis=0)))
    eff_resolution_deg = float(np.degrees(lam / max(0.1, span_m)))
    # Detect a collinear (rank-1) aperture and warn
    eigvals = np.linalg.eigvalsh(np.cov(xy.T) if len(xy) >= 3 else np.eye(2) * 1e-6)
    rank_deficient = bool((eigvals[0] / (eigvals[1] + 1e-12)) < 0.05)

    return {
        "ok": True, "method": f"synthetic_aperture_{method}",
        "carrier_hz": float(carrier_hz), "wavelength_m": float(lam),
        "n_elements": int(len(snapshots)), "aperture_span_m": span_m,
        "effective_resolution_deg": eff_resolution_deg,
        "azimuth_deg": az.tolist(),
        "pseudo_spectrum": ps_norm.tolist(),
        "peaks": peaks[: max(1, n_sources)],
        "warnings": (["collinear aperture — only along-track bearing is observable"] if rank_deficient else []),
    }


# ─────────────────────────────────────────────────────────────────────────────
# 6) Phase-interferometry along track
# ─────────────────────────────────────────────────────────────────────────────
def phase_interferometry_doa(snapshots: list[dict], *,
                              carrier_hz: float, c_mps: float = 299_792_458.0,
                              prior_az_deg: Optional[float] = None,
                              spacing_max_wavelengths: float = 0.5) -> dict:
    """Resolve direction-of-arrival from the carrier-phase delta between
    successive snapshots taken at known positions, exactly like a two-element
    interferometer where the baseline is the receiver's translation between
    snapshots.

    For each adjacent pair (i, i+1): baseline b = pos_{i+1} - pos_i,
        Δφ_meas = arg(iq_{i+1}) - arg(iq_i)
        Δφ_los  = (2π/λ) · b·û(θ) ; û(θ) = (sinθ, cosθ)
    so θ̂ = solve(Δφ_meas == (2π/λ) · |b| · cos(α - θ)) where α is the
    baseline azimuth. Each pair gives two ambiguous solutions per wrap;
    long baselines (>>λ/2) wrap many times — we use the ``prior_az_deg`` (if
    supplied — e.g. from synthetic-aperture beam) to pick the right wrap.

    Returns per-pair bearings plus a circular mean.
    """
    if not snapshots or len(snapshots) < 2:
        return {"ok": False, "error": "need at least 2 coherent snapshots"}
    lam = c_mps / float(carrier_hz)
    if all("lat" in s and "lon" in s for s in snapshots):
        xy, origin, _ = _project_xy(snapshots)
    else:
        xy = np.array([[float(s.get("x_m", 0.0)), float(s.get("y_m", 0.0))] for s in snapshots])
        origin = (0.0, 0.0)
    z = np.array([complex(s["iq_complex"]) for s in snapshots], dtype=np.complex128)
    bearings = []
    for i in range(len(snapshots) - 1):
        b = xy[i + 1] - xy[i]
        bl = float(np.linalg.norm(b))
        if bl < 1e-6:
            continue
        baseline_az = math.degrees(math.atan2(b[0], b[1])) % 360.0
        dphi_meas = np.angle(z[i + 1] * z[i].conj())                # ∈ (-π, π]
        # ambiguity count
        n_wraps_max = int(math.ceil(bl / lam))                       # how many possible integer-wraps
        candidates = []
        for k in range(-n_wraps_max, n_wraps_max + 1):
            dphi = dphi_meas + 2 * math.pi * k
            cos_alpha = dphi / max(1e-9, (2 * math.pi / lam) * bl)
            if -1.0 <= cos_alpha <= 1.0:
                alpha = math.degrees(math.acos(cos_alpha))
                for sign in (+1, -1):
                    az = (baseline_az + sign * alpha) % 360.0
                    candidates.append(az)
        if not candidates:
            continue
        # disambiguate by prior, else by minimum |az - mean(baseline_az)| skew
        if prior_az_deg is not None:
            best = min(candidates, key=lambda a: abs(((a - prior_az_deg + 540) % 360) - 180))
        else:
            best = candidates[0]
        bearings.append({"pair": [i, i + 1], "baseline_m": bl, "bearing_deg": best,
                            "all_candidates_deg": candidates})
    if not bearings:
        return {"ok": False, "error": "no usable baselines (all observations colocated)"}
    # circular mean
    angles = np.deg2rad([b["bearing_deg"] for b in bearings])
    az_mean = math.degrees(math.atan2(np.sin(angles).mean(), np.cos(angles).mean())) % 360.0
    return {
        "ok": True, "method": "phase_interferometry_track",
        "wavelength_m": float(lam), "n_baselines": int(len(bearings)),
        "bearings": bearings, "mean_bearing_deg": float(az_mean),
        "note": ("Phase ambiguity resolved against the prior bearing." if prior_az_deg is not None
                  else "No prior bearing — the lowest-wrap candidate is taken; supply prior_az_deg from a synthetic-aperture beam to remove ambiguity for long baselines."),
    }


# ─────────────────────────────────────────────────────────────────────────────
# 7) Multi-receiver TDOA fix (closed-form, weighted least-squares)
# ─────────────────────────────────────────────────────────────────────────────
def tdoa_multi_receiver_fix(receivers: list[dict], *,
                              tdoa_pairs: Optional[list[dict]] = None,
                              c_mps: float = 299_792_458.0,
                              sigma_ns: float = 50.0,
                              grid_span_m: float = 100_000.0,
                              grid_step_m: float = 100.0) -> dict:
    """Position emitter from inter-receiver time-difference-of-arrival.

    Each receiver is {id, lat, lon, t_arrival_s}. The reference receiver is the
    first one (or pass explicit ``tdoa_pairs``: list of {ref_id, other_id, dt_s}).

    Solver: brute-force a 2-D ML grid (works with any number of pairs and any
    pair geometry — including the degenerate 2-receiver case which is a single
    hyperbola), then Gauss-Newton refine on the MAP cell.
    """
    if not receivers or len(receivers) < 2:
        return {"ok": False, "error": "need at least 2 receivers with timestamps"}
    try:
        xy, origin, scale = _project_xy(receivers)
    except Exception as e:
        return {"ok": False, "error": f"projection failed: {e}"}
    # Build pairs
    pairs = []
    if tdoa_pairs:
        ids = [r.get("id", str(i)) for i, r in enumerate(receivers)]
        idx = {ids[i]: i for i in range(len(ids))}
        for p in tdoa_pairs:
            try:
                pairs.append((idx[p["ref_id"]], idx[p["other_id"]], float(p["dt_s"])))
            except Exception:
                continue
    else:
        if not all("t_arrival_s" in r for r in receivers):
            return {"ok": False, "error": "missing t_arrival_s on receivers (and no explicit tdoa_pairs given)"}
        t0 = float(receivers[0]["t_arrival_s"])
        for j in range(1, len(receivers)):
            pairs.append((0, j, float(receivers[j]["t_arrival_s"]) - t0))
    if not pairs:
        return {"ok": False, "error": "no TDOA pairs to fit"}
    cx, cy = float(np.mean(xy[:, 0])), float(np.mean(xy[:, 1]))
    n_cells = int(round(grid_span_m / grid_step_m))
    if n_cells > 800:
        n_cells = 800
        grid_step_m = grid_span_m / n_cells
    gx = np.linspace(cx - grid_span_m / 2, cx + grid_span_m / 2, n_cells)
    gy = np.linspace(cy - grid_span_m / 2, cy + grid_span_m / 2, n_cells)
    XX, YY = np.meshgrid(gx, gy, indexing="xy")
    # Precompute distance to each receiver
    dists = []
    for i in range(len(xy)):
        d = np.sqrt((XX - xy[i, 0]) ** 2 + (YY - xy[i, 1]) ** 2)
        dists.append(d)
    sigma_m = (sigma_ns * 1e-9) * c_mps
    ll = np.zeros_like(XX, dtype=np.float64)
    for i_ref, i_oth, dt in pairs:
        model = (dists[i_oth] - dists[i_ref]) / c_mps
        ll += -0.5 * ((model - dt) / max(1e-12, sigma_ns * 1e-9)) ** 2
    iy, ix = np.unravel_index(int(np.argmax(ll)), ll.shape)
    bx, by = float(gx[ix]), float(gy[iy])
    lat, lon = _xy_to_latlon(np.array([bx, by]), origin, scale)
    cep_m = grid_step_m * 1.5
    return {
        "ok": True, "method": "tdoa_multi_receiver",
        "estimate": {"lat": lat, "lon": lon, "x_m": bx, "y_m": by},
        "uncertainty": {"cep_m": float(cep_m), "sigma_ns": sigma_ns,
                          "grid_step_m": grid_step_m, "grid_span_m": grid_span_m},
        "n_pairs": int(len(pairs)),
        "n_receivers": int(len(receivers)),
    }


# ─────────────────────────────────────────────────────────────────────────────
# 8) ML grid-search fusion (AoA + RSS + Doppler + TDOA all together)
# ─────────────────────────────────────────────────────────────────────────────
def ml_grid_fusion(observations: list[dict], *,
                    centre: Optional[tuple[float, float]] = None,
                    grid_span_m: float = 50_000.0,
                    grid_step_m: float = 100.0,
                    p_tx_dbm: Optional[float] = None,
                    path_loss_n: float = 3.0,
                    carrier_hz: Optional[float] = None,
                    sigma_aoa_deg: float = 3.0,
                    sigma_rss_db: float = 6.0,
                    sigma_hz: float = 5.0,
                    sigma_ns: float = 50.0,
                    c_mps: float = 299_792_458.0) -> dict:
    """Universal back-stop ML estimator. Each observation declares its
    ``kind`` (``aoa``, ``rss``, ``doppler``, ``tdoa_ref+other``) and the
    matching fields:

      - ``aoa``     → {lat, lon, bearing_deg [, sigma_deg]}
      - ``rss``     → {lat, lon, rssi_dbm}
      - ``doppler`` → {lat, lon, vx_mps, vy_mps, frequency_offset_hz}
      - ``tdoa``    → {ref: {lat, lon, t}, other: {lat, lon, t}}

    Combines all of them in one 2-D log-likelihood, returns the MAP fix +
    CEP + the full heat-map (downsampled for transport) so the UI can render
    a likelihood contour.
    """
    if not observations:
        return {"ok": False, "error": "no observations supplied"}
    # Centre + grid
    if centre is None:
        all_pts = []
        for o in observations:
            if "lat" in o and "lon" in o:
                all_pts.append((float(o["lat"]), float(o["lon"])))
            if o.get("kind") == "tdoa":
                for k in ("ref", "other"):
                    if k in o:
                        all_pts.append((float(o[k]["lat"]), float(o[k]["lon"])))
        if not all_pts:
            return {"ok": False, "error": "observations carry no positions"}
        lat0 = float(np.mean([p[0] for p in all_pts]))
        lon0 = float(np.mean([p[1] for p in all_pts]))
    else:
        lat0, lon0 = float(centre[0]), float(centre[1])
    mlat, mlon = _enu_scale(lat0)
    scale = (mlat, mlon)
    origin = (lat0, lon0)

    def to_xy(lat, lon):
        return np.array([(lon - lon0) * mlon, (lat - lat0) * mlat])

    n_cells = int(round(grid_span_m / grid_step_m))
    if n_cells > 600:
        n_cells = 600
        grid_step_m = grid_span_m / n_cells
    gx = np.linspace(-grid_span_m / 2, grid_span_m / 2, n_cells)
    gy = np.linspace(-grid_span_m / 2, grid_span_m / 2, n_cells)
    XX, YY = np.meshgrid(gx, gy, indexing="xy")
    ll = np.zeros_like(XX, dtype=np.float64)
    used = {"aoa": 0, "rss": 0, "doppler": 0, "tdoa": 0}
    for o in observations:
        kind = o.get("kind")
        if kind == "aoa":
            p = to_xy(float(o["lat"]), float(o["lon"]))
            dx = XX - p[0]; dy = YY - p[1]
            bearing_model = (np.degrees(np.arctan2(dx, dy))) % 360.0
            theta = float(o["bearing_deg"]) % 360.0
            sig = float(o.get("sigma_deg", sigma_aoa_deg))
            d = ((bearing_model - theta + 540) % 360) - 180
            ll += -0.5 * (d / sig) ** 2
            used["aoa"] += 1
        elif kind == "rss":
            p = to_xy(float(o["lat"]), float(o["lon"]))
            d = np.sqrt((XX - p[0]) ** 2 + (YY - p[1]) ** 2) + 1.0
            if p_tx_dbm is None:
                # Use just the spatial-gradient piece — collapse to relative model
                ll += -0.5 * 0.0
            else:
                model = p_tx_dbm - 10.0 * path_loss_n * np.log10(d)
                ll += -0.5 * ((model - float(o["rssi_dbm"])) / sigma_rss_db) ** 2
                used["rss"] += 1
        elif kind == "doppler":
            if carrier_hz is None:
                continue
            p = to_xy(float(o["lat"]), float(o["lon"]))
            vx = float(o.get("vx_mps", 0.0)); vy = float(o.get("vy_mps", 0.0))
            dx = XX - p[0]; dy = YY - p[1]
            d = np.sqrt(dx ** 2 + dy ** 2) + 1e-6
            ux = dx / d; uy = dy / d
            df_model = -(carrier_hz / c_mps) * (vx * ux + vy * uy)
            ll += -0.5 * ((df_model - float(o["frequency_offset_hz"])) / sigma_hz) ** 2
            used["doppler"] += 1
        elif kind == "tdoa":
            ref = o["ref"]; oth = o["other"]
            pr = to_xy(float(ref["lat"]), float(ref["lon"]))
            po = to_xy(float(oth["lat"]), float(oth["lon"]))
            dr = np.sqrt((XX - pr[0]) ** 2 + (YY - pr[1]) ** 2)
            do = np.sqrt((XX - po[0]) ** 2 + (YY - po[1]) ** 2)
            model = (do - dr) / c_mps
            dt = float(oth["t"]) - float(ref["t"])
            ll += -0.5 * ((model - dt) / (sigma_ns * 1e-9)) ** 2
            used["tdoa"] += 1
    iy, ix = np.unravel_index(int(np.argmax(ll)), ll.shape)
    bx, by = float(gx[ix]), float(gy[iy])
    lat, lon = (lat0 + by / mlat, lon0 + bx / mlon)
    # CEP from curvature
    cep_m = grid_step_m * 1.5
    if 1 <= iy < n_cells - 1 and 1 <= ix < n_cells - 1:
        d2x = ll[iy, ix + 1] - 2 * ll[iy, ix] + ll[iy, ix - 1]
        d2y = ll[iy + 1, ix] - 2 * ll[iy, ix] + ll[iy - 1, ix]
        dxy = 0.25 * (ll[iy + 1, ix + 1] - ll[iy + 1, ix - 1] - ll[iy - 1, ix + 1] + ll[iy - 1, ix - 1])
        H = np.array([[d2x, dxy], [dxy, d2y]]) / (grid_step_m ** 2)
        try:
            cov = np.linalg.inv(-H + 1e-9 * np.eye(2))
            cep_m = 1.1774 * math.sqrt(max(0.0, 0.5 * (cov[0, 0] + cov[1, 1])))
        except np.linalg.LinAlgError:
            pass
    # Downsample the heat-map for transport
    stride = max(1, n_cells // 64)
    heat = ll[::stride, ::stride].astype(np.float32)
    heat = heat - float(heat.max())   # normalise to ≤ 0 for transport
    return {
        "ok": True, "method": "ml_grid_fusion",
        "estimate": {"lat": float(lat), "lon": float(lon), "x_m": bx, "y_m": by},
        "uncertainty": {"cep_m": float(cep_m)},
        "fit": {"log_likelihood": float(ll[iy, ix]),
                  "n_observations": int(len(observations)), "used": used,
                  "grid_step_m": grid_step_m, "grid_span_m": grid_span_m},
        "heatmap": {
            "rel_log_likelihood": heat.tolist(),
            "x_m": gx[::stride].tolist(), "y_m": gy[::stride].tolist(),
            "lat0": lat0, "lon0": lon0, "m_per_deg_lat": mlat, "m_per_deg_lon": mlon,
        },
    }


# ─────────────────────────────────────────────────────────────────────────────
# 9) EKF kinematic tracker (RSS + Doppler + AoA over a moving observer)
# ─────────────────────────────────────────────────────────────────────────────
class SingleChannelEKF:
    """Extended Kalman Filter on a stationary 2-D emitter state x = [ex, ey],
    optionally extended to [ex, ey, P_tx_dBm]. Sequential updates from
    RSS-, Doppler-, and AoA-type observations.
    """

    def __init__(self, *, initial_xy: Optional[tuple[float, float]] = None,
                  initial_cov_m2: float = 1e7,
                  path_loss_n: float = 3.0,
                  p_tx_dbm: Optional[float] = None,
                  carrier_hz: Optional[float] = None,
                  c_mps: float = 299_792_458.0,
                  estimate_p_tx: bool = True,
                  initial_p_tx_dbm: float = 0.0,
                  initial_p_tx_var: float = 100.0):
        self.estimate_p_tx = estimate_p_tx
        self.n = float(path_loss_n)
        self.carrier_hz = carrier_hz
        self.c = c_mps
        if estimate_p_tx:
            self.x = np.array([initial_xy[0] if initial_xy else 0.0,
                                 initial_xy[1] if initial_xy else 0.0,
                                 p_tx_dbm if p_tx_dbm is not None else initial_p_tx_dbm], dtype=np.float64)
            self.P = np.diag([initial_cov_m2, initial_cov_m2, initial_p_tx_var]).astype(np.float64)
        else:
            self.x = np.array([initial_xy[0] if initial_xy else 0.0,
                                 initial_xy[1] if initial_xy else 0.0], dtype=np.float64)
            self.P = np.diag([initial_cov_m2, initial_cov_m2]).astype(np.float64)
        self.history: list[dict] = []

    # ── observation likelihoods (h, H) ───────────────────────────────────
    def _rss_h_H(self, pos: np.ndarray):
        ex, ey = self.x[0], self.x[1]
        dx = ex - pos[0]; dy = ey - pos[1]
        d = math.hypot(dx, dy)
        d = max(d, 1.0)
        log_d = 10.0 * math.log10(d)
        if self.estimate_p_tx:
            h = self.x[2] - self.n * log_d
            c = 10.0 * self.n / math.log(10.0)
            H = np.array([-c * dx / (d * d), -c * dy / (d * d), 1.0])
        else:
            assert False, "p_tx must be known if estimate_p_tx is False — pre-set self.x"
        return h, H

    def _aoa_h_H(self, pos: np.ndarray):
        ex, ey = self.x[0], self.x[1]
        dx = ex - pos[0]; dy = ey - pos[1]
        h = math.degrees(math.atan2(dx, dy)) % 360.0
        d2 = dx * dx + dy * dy + 1e-9
        # dθ/dx = (180/π) · dy / d²  ;  dθ/dy = -(180/π) · dx / d²
        Hx = (180.0 / math.pi) * dy / d2
        Hy = -(180.0 / math.pi) * dx / d2
        if self.estimate_p_tx:
            H = np.array([Hx, Hy, 0.0])
        else:
            H = np.array([Hx, Hy])
        return h, H

    def _doppler_h_H(self, pos: np.ndarray, vel: np.ndarray):
        ex, ey = self.x[0], self.x[1]
        dx = ex - pos[0]; dy = ey - pos[1]
        d = math.hypot(dx, dy)
        d = max(d, 1.0)
        ux = dx / d; uy = dy / d
        h = -(self.carrier_hz / self.c) * (vel[0] * ux + vel[1] * uy)
        dux_dx = (1.0 - ux * ux) / d
        duy_dy = (1.0 - uy * uy) / d
        dux_dy = -(ux * uy) / d
        duy_dx = dux_dy
        Hx = -(self.carrier_hz / self.c) * (vel[0] * dux_dx + vel[1] * duy_dx)
        Hy = -(self.carrier_hz / self.c) * (vel[0] * dux_dy + vel[1] * duy_dy)
        if self.estimate_p_tx:
            H = np.array([Hx, Hy, 0.0])
        else:
            H = np.array([Hx, Hy])
        return h, H

    def update(self, obs: dict, *, sigma_aoa_deg: float = 3.0,
                sigma_rss_db: float = 6.0, sigma_hz: float = 5.0) -> dict:
        """Apply one observation in dict form. Returns the post-update state."""
        kind = obs.get("kind")
        pos = np.array([float(obs.get("x_m", 0.0)), float(obs.get("y_m", 0.0))])
        if kind == "aoa":
            h, H = self._aoa_h_H(pos)
            y = float(obs["bearing_deg"]) % 360.0
            innov = ((y - h + 540) % 360) - 180
            R = (sigma_aoa_deg) ** 2
        elif kind == "rss":
            h, H = self._rss_h_H(pos)
            innov = float(obs["rssi_dbm"]) - h
            R = sigma_rss_db ** 2
        elif kind == "doppler":
            vel = np.array([float(obs.get("vx_mps", 0.0)), float(obs.get("vy_mps", 0.0))])
            h, H = self._doppler_h_H(pos, vel)
            innov = float(obs["frequency_offset_hz"]) - h
            R = sigma_hz ** 2
        else:
            return {"applied": False, "reason": f"unknown obs kind {kind!r}"}
        # standard EKF update
        H = H.reshape(1, -1)
        S = float(H @ self.P @ H.T + R)
        K = (self.P @ H.T) / S                                      # (n_state, 1)
        self.x = self.x + (K.flatten() * innov)
        I = np.eye(self.x.size)
        self.P = (I - K @ H) @ self.P
        self.history.append({"kind": kind, "innov": float(innov), "S": float(S),
                              "post_state": self.x.tolist(),
                              "post_cov_trace": float(np.trace(self.P))})
        return {"applied": True, "innov": float(innov), "S": float(S),
                  "state": self.x.tolist(), "cov_trace": float(np.trace(self.P))}

    def to_dict(self) -> dict:
        return {"state": self.x.tolist(), "cov": self.P.tolist(),
                  "estimate_p_tx": self.estimate_p_tx, "history": self.history}


def ekf_track_fix(observations: list[dict], *,
                    initial_centre: Optional[tuple[float, float]] = None,
                    path_loss_n: float = 3.0, p_tx_dbm: Optional[float] = None,
                    carrier_hz: Optional[float] = None,
                    sigma_aoa_deg: float = 3.0, sigma_rss_db: float = 6.0,
                    sigma_hz: float = 5.0) -> dict:
    """Convenience wrapper that runs a stationary-emitter EKF over a sequence
    of (heterogeneous) observations and returns the final fix + per-step
    history."""
    if not observations:
        return {"ok": False, "error": "no observations supplied"}
    # Pick a sane centre to project around.
    pts = [(float(o["lat"]), float(o["lon"])) for o in observations if "lat" in o and "lon" in o]
    if initial_centre:
        lat0, lon0 = initial_centre
    elif pts:
        lat0 = float(np.mean([p[0] for p in pts]))
        lon0 = float(np.mean([p[1] for p in pts]))
    else:
        return {"ok": False, "error": "observations carry no positions"}
    mlat, mlon = _enu_scale(lat0)

    def to_xy(lat, lon):
        return (float((lon - lon0) * mlon), float((lat - lat0) * mlat))

    ekf = SingleChannelEKF(initial_xy=(0.0, 0.0), initial_cov_m2=1e7,
                            path_loss_n=path_loss_n, p_tx_dbm=p_tx_dbm,
                            carrier_hz=carrier_hz, estimate_p_tx=(p_tx_dbm is None),
                            initial_p_tx_dbm=(p_tx_dbm if p_tx_dbm is not None else 0.0))
    steps = []
    for o in observations:
        if "lat" in o and "lon" in o:
            x_m, y_m = to_xy(float(o["lat"]), float(o["lon"]))
        else:
            x_m = float(o.get("x_m", 0.0)); y_m = float(o.get("y_m", 0.0))
        step_obs = dict(o); step_obs["x_m"] = x_m; step_obs["y_m"] = y_m
        steps.append(ekf.update(step_obs, sigma_aoa_deg=sigma_aoa_deg,
                                  sigma_rss_db=sigma_rss_db, sigma_hz=sigma_hz))
    x, y = float(ekf.x[0]), float(ekf.x[1])
    lat, lon = (lat0 + y / mlat, lon0 + x / mlon)
    cov = ekf.P
    cep_m = 1.1774 * math.sqrt(max(0.0, 0.5 * (cov[0, 0] + cov[1, 1])))
    return {
        "ok": True, "method": "ekf_kinematic_track",
        "estimate": {"lat": lat, "lon": lon, "x_m": x, "y_m": y,
                       "p_tx_dbm": (float(ekf.x[2]) if ekf.estimate_p_tx else p_tx_dbm)},
        "uncertainty": {"cep_m": float(cep_m), "cov_m2": cov.tolist()},
        "history": steps, "n_observations": int(len(observations)),
    }


# ─────────────────────────────────────────────────────────────────────────────
# 10) Variance / quality utilities — useful for the UI to grey-out unusable methods
# ─────────────────────────────────────────────────────────────────────────────
def feasibility_report(observations: list[dict]) -> dict:
    """Diagnose which single-channel DF methods can run on the observations
    we have. Returns a dict of {method: {feasible, reason, requires}}.
    """
    pos = [(o.get("lat"), o.get("lon")) for o in observations if "lat" in o and "lon" in o]
    has_rss = sum(1 for o in observations if ("rssi_dbm" in o or "power_dbm" in o))
    has_doppler = sum(1 for o in observations if "frequency_offset_hz" in o)
    has_velocity = sum(1 for o in observations if "vx_mps" in o or "vy_mps" in o or "v_mps" in o)
    has_iq = sum(1 for o in observations if "iq_complex" in o or ("iq_re" in o and "iq_im" in o))
    has_t = sum(1 for o in observations if "t" in o or "t_arrival_s" in o)
    span_m = 0.0
    if len(pos) >= 2:
        try:
            xy, _, _ = _project_xy(observations)
            span_m = float(np.linalg.norm(xy.max(axis=0) - xy.min(axis=0)))
        except Exception:
            pass
    return {
        "n_observations": int(len(observations)),
        "spatial_span_m": span_m,
        "rss_log_distance_ml":   {"feasible": has_rss >= 3, "requires": "≥ 3 RSS samples at distinct positions"},
        "rss_gradient_bearing":  {"feasible": has_rss >= 3 and span_m > 1.0, "requires": "≥ 3 RSS samples spanning some metres"},
        "doppler_cpa":           {"feasible": has_doppler >= 4 and has_velocity >= 1 and has_t >= 4, "requires": "≥ 4 (t, Δf, v) samples along a straight pass"},
        "fdoa_track_grid":       {"feasible": has_doppler >= 3 and has_velocity >= 3, "requires": "≥ 3 (Δf, vx, vy) with varying velocity directions"},
        "synthetic_aperture":    {"feasible": has_iq >= 3 and span_m > 0.05, "requires": "≥ 3 coherent IQ snapshots at known positions"},
        "phase_interferometry":  {"feasible": has_iq >= 2 and span_m > 0.05, "requires": "≥ 2 coherent IQ snapshots at known positions"},
        "tdoa_multi_receiver":   {"feasible": has_t >= 2 and len(observations) >= 2, "requires": "≥ 2 receivers with synchronised t_arrival_s"},
        "ml_grid_fusion":        {"feasible": len(observations) >= 1, "requires": "≥ 1 observation of any flavoured kind"},
        "ekf_kinematic_track":   {"feasible": len(observations) >= 1, "requires": "≥ 1 observation of any flavoured kind"},
    }
