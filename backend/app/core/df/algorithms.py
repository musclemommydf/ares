"""
DoA / DF pseudo-spectrum estimators.

All algorithms operate on an M×M sample covariance matrix R = E[x x^H] formed
from M coherent receive channels. Outputs are unitless 1-D pseudo-spectra over
azimuth (and optionally elevation, currently fixed at 0° — easy extension).

Implemented (all standard textbook forms — see Van Trees, Optimum Array
Processing, Ch. 9, and Stoica & Moses, Spectral Analysis of Signals, Ch. 6):

  - Bartlett  (conventional beamformer)           P(θ) = a(θ)^H R a(θ)
  - Capon     (MVDR / minimum-variance distortionless response)
                                                  P(θ) = 1 / (a(θ)^H R^-1 a(θ))
  - MUSIC                                         P(θ) = 1 / ||E_n^H a(θ)||²
  - root-MUSIC                                    polynomial roots in z = e^{jkd sinθ}
  - ESPRIT (TLS, sub-array invariance)            invariance gives DoAs directly
  - MEM (max-entropy, Burg-style)                 P(θ) = 1 / |a(θ)^H R^-1 e₁|²

`source_count.py` returns an estimate K̂ for MUSIC/ESPRIT/root-MUSIC; in
ambiguous situations the caller can override.
"""

from __future__ import annotations

import math
from typing import Optional

import numpy as np

from .arrays import ArrayGeometry, steering_matrix


def _signal_subspace(R: np.ndarray, k: int) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Eigen-decomposition of a Hermitian covariance, returning eigvals sorted
    descending plus the signal/noise sub-spaces split at index `k`."""
    w, V = np.linalg.eigh((R + R.conj().T) / 2)   # Hermitian-symmetrise then eig
    order = np.argsort(w)[::-1]
    w = w[order]; V = V[:, order]
    return w, V[:, :k], V[:, k:]                  # eigvals, E_s (M×k), E_n (M×(M-k))


def bartlett(R: np.ndarray, geom: ArrayGeometry, freq_hz: float,
             az_grid_deg: np.ndarray) -> np.ndarray:
    """Conventional beamformer (Bartlett). Robust to model error, lowest resolution."""
    A = steering_matrix(geom, freq_hz, az_grid_deg)              # (M, K)
    p = np.einsum("mk,mn,nk->k", A.conj(), R, A)
    return np.real(p) / max(geom.n, 1)


def capon(R: np.ndarray, geom: ArrayGeometry, freq_hz: float,
          az_grid_deg: np.ndarray, diag_loading: float = 1e-6) -> np.ndarray:
    """Capon / MVDR. Higher resolution than Bartlett; sensitive to mismatch
    (mitigated by a small diagonal loading)."""
    M = geom.n
    Rl = R + diag_loading * np.trace(R).real / M * np.eye(M)
    Rinv = np.linalg.inv(Rl)
    A = steering_matrix(geom, freq_hz, az_grid_deg)
    p = 1.0 / np.real(np.einsum("mk,mn,nk->k", A.conj(), Rinv, A))
    return p


def music(R: np.ndarray, geom: ArrayGeometry, freq_hz: float,
          az_grid_deg: np.ndarray, n_sources: int) -> np.ndarray:
    """MUSIC pseudo-spectrum. Sharp peaks at true DoAs. Requires K̂ ≤ M-1."""
    n_sources = max(1, min(geom.n - 1, int(n_sources)))
    _, _, En = _signal_subspace(R, n_sources)
    A = steering_matrix(geom, freq_hz, az_grid_deg)
    proj = En.conj().T @ A                                       # ((M-k), K)
    denom = np.einsum("ik,ik->k", proj.conj(), proj).real
    # Pseudo-spectrum is reciprocal of noise-projection magnitude squared.
    return 1.0 / np.maximum(denom, 1e-20)


def mem(R: np.ndarray, geom: ArrayGeometry, freq_hz: float,
        az_grid_deg: np.ndarray, ref_channel: int = 0,
        diag_loading: float = 1e-6) -> np.ndarray:
    """Maximum-entropy (Burg-style) spectrum — uses one row of R^-1 as the
    matched filter. Comparable resolution to Capon at lower cost; popular in
    krakensdr_doa as one of the four default selectable algorithms."""
    M = geom.n
    Rl = R + diag_loading * np.trace(R).real / M * np.eye(M)
    Rinv = np.linalg.inv(Rl)
    e = np.zeros(M, dtype=complex); e[ref_channel] = 1.0
    c = Rinv @ e                                                 # (M,)
    A = steering_matrix(geom, freq_hz, az_grid_deg)              # (M, K)
    denom = np.abs(c.conj() @ A) ** 2
    return 1.0 / np.maximum(denom, 1e-20)


def root_music(R: np.ndarray, geom: ArrayGeometry, freq_hz: float,
               n_sources: int) -> np.ndarray:
    """Root-MUSIC — returns the azimuths of all detected sources (degrees,
    sorted ascending). Only correct for *uniform linear* arrays (the version
    everyone publishes). For UCA we fall back to peak-pick on the MUSIC grid;
    callers should prefer `music()` + peak detection for general arrays."""
    M = geom.n
    n_sources = max(1, min(M - 1, int(n_sources)))
    # Detect ULA: collinear element positions
    pts = geom.positions
    centred = pts - pts.mean(axis=0)
    sv = np.linalg.svd(centred, compute_uv=False)
    is_ula = sv[1] < 1e-9 * sv[0] if sv[0] > 0 else False
    if not is_ula:
        # Polynomial roots not meaningful for non-ULA — fall through to a fine
        # MUSIC grid and return the top-k peaks.
        grid = np.linspace(0, 360, 3601)
        p = music(R, geom, freq_hz, grid, n_sources)
        order = np.argsort(p)[::-1]
        picks = []
        for idx in order:
            az = grid[idx]
            if any(abs((az - a + 180) % 360 - 180) < 2.0 for a in picks):
                continue
            picks.append(az)
            if len(picks) >= n_sources:
                break
        return np.sort(np.array(picks))
    # ULA case — element axis is the direction of geom positions
    axis_vec = centred[-1] - centred[0]
    axis_az = (math.degrees(math.atan2(axis_vec[0], axis_vec[1])) + 360) % 360
    spacing = np.linalg.norm(axis_vec) / max(M - 1, 1)
    _, _, En = _signal_subspace(R, n_sources)
    C = En @ En.conj().T                                         # (M, M)
    # Build polynomial coefficients from diagonals of C (Stoica & Moses 6.6.6)
    coeffs = np.array([np.trace(C, offset=k) for k in range(-(M - 1), M)])
    roots = np.roots(coeffs[::-1])
    # Pick roots closest to (but inside) the unit circle.
    inside = roots[np.abs(roots) < 1]
    if inside.size == 0:
        inside = roots
    inside = inside[np.argsort(np.abs(np.abs(inside) - 1.0))][: n_sources]
    lam = 299_792_458.0 / freq_hz
    sin_theta = np.angle(inside) / (2 * math.pi * spacing / lam)
    sin_theta = np.clip(sin_theta, -1, 1)
    rel_az = np.degrees(np.arcsin(sin_theta))                    # off-broadside
    # Convert to true bearing: broadside is perpendicular to axis_vec.
    true_az = (axis_az - 90 + rel_az) % 360
    return np.sort(true_az)


def esprit(R: np.ndarray, geom: ArrayGeometry, freq_hz: float,
           n_sources: int) -> np.ndarray:
    """ESPRIT (TLS form) — sub-array invariance. Like root-MUSIC, well-defined
    only for ULA; UCA falls back to MUSIC peak-pick."""
    M = geom.n
    n_sources = max(1, min(M - 1, int(n_sources)))
    pts = geom.positions - geom.positions.mean(axis=0)
    sv = np.linalg.svd(pts, compute_uv=False)
    is_ula = sv[1] < 1e-9 * sv[0] if sv[0] > 0 else False
    if not is_ula:
        grid = np.linspace(0, 360, 3601)
        p = music(R, geom, freq_hz, grid, n_sources)
        order = np.argsort(p)[::-1]
        picks = []
        for idx in order:
            az = grid[idx]
            if any(abs((az - a + 180) % 360 - 180) < 2.0 for a in picks):
                continue
            picks.append(az)
            if len(picks) >= n_sources:
                break
        return np.sort(np.array(picks))
    axis_vec = pts[-1] - pts[0]
    axis_az = (math.degrees(math.atan2(axis_vec[0], axis_vec[1])) + 360) % 360
    spacing = np.linalg.norm(axis_vec) / max(M - 1, 1)
    _, Es, _ = _signal_subspace(R, n_sources)
    E1 = Es[:-1, :]; E2 = Es[1:, :]
    # TLS-ESPRIT: SVD of [E1 E2]
    U, _, _ = np.linalg.svd(np.hstack([E1, E2]))
    k = n_sources
    U12 = U[:k, k:2*k]; U22 = U[k:2*k, k:2*k]
    Psi = -np.linalg.solve(U22, U12)
    eigs = np.linalg.eigvals(Psi)
    lam = 299_792_458.0 / freq_hz
    sin_theta = np.clip(np.angle(eigs) / (2 * math.pi * spacing / lam), -1, 1)
    rel_az = np.degrees(np.arcsin(sin_theta))
    true_az = (axis_az - 90 + rel_az) % 360
    return np.sort(true_az)


def peak_pick(spectrum: np.ndarray, az_grid_deg: np.ndarray,
              n_peaks: int, min_sep_deg: float = 3.0) -> list[dict]:
    """Top-N peak picker with circular-mean separation. Returns
    [{az_deg, power_lin, power_db, idx}, ...] sorted by power desc."""
    order = np.argsort(spectrum)[::-1]
    out = []
    for idx in order:
        az = float(az_grid_deg[idx])
        if any(abs(((az - p["az_deg"] + 180) % 360) - 180) < min_sep_deg for p in out):
            continue
        v = float(spectrum[idx])
        out.append({
            "az_deg": az,
            "power_lin": v,
            "power_db": 10 * math.log10(max(v, 1e-20)),
            "idx": int(idx),
        })
        if len(out) >= n_peaks:
            break
    return out


def pseudo_spectrum(R: np.ndarray, geom: ArrayGeometry, freq_hz: float,
                    algorithm: str = "music", n_sources: int = 1,
                    az_resolution_deg: float = 1.0,
                    elevation_deg: float = 0.0) -> dict:
    """Top-level entrypoint. Returns:
        { "algorithm": ..., "az_deg": [...], "power_db": [...],
          "peaks": [{az_deg, power_db, idx}, ...] }
    """
    algorithm = (algorithm or "music").lower()
    grid = np.arange(0, 360, max(0.1, az_resolution_deg))
    if algorithm == "bartlett":
        s = bartlett(R, geom, freq_hz, grid)
    elif algorithm == "capon":
        s = capon(R, geom, freq_hz, grid)
    elif algorithm == "music":
        s = music(R, geom, freq_hz, grid, n_sources)
    elif algorithm == "mem":
        s = mem(R, geom, freq_hz, grid)
    elif algorithm == "root_music":
        bearings = root_music(R, geom, freq_hz, n_sources)
        return {
            "algorithm": "root_music",
            "az_deg": [], "power_db": [],
            "peaks": [{"az_deg": float(a), "power_db": 0.0} for a in bearings],
            "note": "root-MUSIC returns bearings directly (not a pseudo-spectrum)",
        }
    elif algorithm == "esprit":
        bearings = esprit(R, geom, freq_hz, n_sources)
        return {
            "algorithm": "esprit",
            "az_deg": [], "power_db": [],
            "peaks": [{"az_deg": float(a), "power_db": 0.0} for a in bearings],
            "note": "ESPRIT returns bearings directly (not a pseudo-spectrum)",
        }
    else:
        raise ValueError(f"unknown algorithm: {algorithm}")
    s_db = 10 * np.log10(np.maximum(s, 1e-20))
    peaks = peak_pick(s, grid, max(1, n_sources))
    return {
        "algorithm": algorithm,
        "az_deg": grid.tolist(),
        "power_db": s_db.tolist(),
        "peaks": peaks,
    }


def covariance_from_iq(iq: np.ndarray, normalize: bool = True) -> np.ndarray:
    """Sample covariance from (M, N) complex IQ samples. M channels × N snapshots.
    Forward-backward averaging is *not* applied here (not valid for arbitrary
    arrays) — callers can wrap if their geometry is symmetric."""
    iq = np.asarray(iq, dtype=complex)
    if iq.ndim != 2:
        raise ValueError("iq must be (M, N) complex")
    M, N = iq.shape
    if N < M:
        raise ValueError(f"need at least M={M} snapshots, got {N}")
    R = (iq @ iq.conj().T) / N
    if normalize:
        # Trace-normalise so output magnitude doesn't depend on input amplitude.
        tr = np.trace(R).real
        if tr > 0:
            R = R * (M / tr)
    return R
