# SPDX-License-Identifier: MIT OR Apache-2.0
# Copyright (c) 2026 Ares

"""
classic_df.py — the "classic" direction-finding estimators ALARIS DF antennas
are built for, complementing the super-resolution (MUSIC/Capon) and phase-
interferometry estimators in :mod:`app.core.df.interferometry`.

ALARIS Antennas' DF heads present element patterns suitable for two estimation
families (their own wording): the **Watson-Watt** method (driven by Adcock
element arrangements) and **3-channel correlative DF (CDF / CIDF)**. This module
implements both, plus **pseudo-Doppler** DF, all from coherent IQ snapshots and
all returning the same :class:`~app.core.df.interferometry.AoAResult` the rest of
Ares consumes — so a bearing from an Adcock/Watson-Watt head flows into the exact
same fix/CEP/CoT pipeline as a KrakenSDR MUSIC bearing.

Methods
-------
* ``watson_watt_aoa`` — Adcock / Watson-Watt amplitude DF. Forms the two crossed
  "loop" channels (N-S, E-W) and an omni "sense" channel from the array as the
  first/zeroth circular spatial harmonics — which is exactly the crossed-Adcock
  combiner for a 4-element ring and its generalisation for an 8+ element ring.
  Coherently detecting the loops against the sense gives an unambiguous 0–360°
  bearing; without a sense channel it degrades to the classic 180°-ambiguous WW.
* ``correlative_aoa`` — correlative DF / correlative interferometer: correlate
  the *measured* complex array response against the array manifold over azimuth
  and pick the best-matching steering vector. Works with a modelled manifold or a
  measured/calibrated one. This is the ALARIS "CDF" technique.
* ``pseudo_doppler_aoa`` — phase-mode (pseudo-Doppler) DF on a circular array:
  the bearing falls straight out of the phase of the first spatial Fourier mode.

All azimuths are degrees clockwise from true north; the caller adds the array
boresight heading (``observer_heading_deg``) to true-reference the bearing.
"""
from __future__ import annotations

import math
from typing import Optional

import numpy as np

from .interferometry import (
    ArrayGeometry, AoAResult, steering_matrix, _crlb_phase,
)

_C = 299_792_458.0


# ── helpers ──────────────────────────────────────────────────────────────────
def _element_azimuths(geom: ArrayGeometry) -> np.ndarray:
    """Azimuth (rad, clockwise from north) of each element about the centroid."""
    p = geom.positions_m - geom.positions_m.mean(axis=0)
    east, north = p[:, 0], p[:, 1]
    return np.arctan2(east, north)              # 0 = north, +→ east


def _radii(geom: ArrayGeometry) -> np.ndarray:
    p = geom.positions_m - geom.positions_m.mean(axis=0)
    return np.hypot(p[:, 0], p[:, 1])


def _as_channels_by_samples(X: np.ndarray, n: int) -> np.ndarray:
    X = np.asarray(X)
    if X.dtype not in (np.complex64, np.complex128, complex):
        X = X.astype(complex)
    if X.ndim != 2:
        raise ValueError("snapshots must be 2-D (N channels × K samples)")
    if X.shape[0] != n and X.shape[1] == n:
        X = X.T
    if X.shape[0] != n:
        raise ValueError(f"channel count {X.shape[0]} ≠ array size {n}")
    return X


def _snr_db_from_R(R: np.ndarray) -> Optional[float]:
    ev = np.linalg.eigvalsh(R)
    if ev.size < 2:
        return None
    noise = float(np.mean(ev[:-1]))
    return 10.0 * math.log10(max(1e-9, (float(ev[-1]) - noise) / max(1e-12, noise))) if noise > 0 else None


def _sigma_phase_from_snr(snr_db: Optional[float], n: int) -> float:
    if snr_db is None:
        return math.radians(8.0)
    return max(1e-3, 1.0 / math.sqrt(2.0 * max(0.5, 10.0 ** (snr_db / 10.0)) * max(1, n)))


# ── Watson-Watt / Adcock ─────────────────────────────────────────────────────
def watson_watt_aoa(
    geom: ArrayGeometry, freq_hz: float, snapshots: np.ndarray, *,
    sense_index: Optional[int] = None, loop_phase_deg: float = 90.0,
    observer_heading_deg: Optional[float] = None,
) -> AoAResult:
    """Adcock / Watson-Watt amplitude-comparison DF.

    The two crossed Adcock "loops" are the first circular spatial harmonics of the
    ring, ``NS = Σ_i x_i·cos φ_i`` and ``EW = Σ_i x_i·sin φ_i`` (φ_i = element
    azimuth). For a 4-element N/E/S/W Adcock this is exactly ``(x_N−x_S)`` and
    ``(x_E−x_W)``; for more elements it is the optimal all-element combiner. The
    omni "sense" is the zeroth harmonic ``Σ_i x_i`` (or an explicit centre element
    if ``sense_index`` is given / auto-detected).

    Coherently detecting each loop against the sense (with the nominal 90°
    loop-to-sense quadrature, calibratable via ``loop_phase_deg``) yields a full
    0–360° bearing ``atan2(EŴ, NŜ)``. With no usable sense channel the estimate is
    the 180°-ambiguous principal-axis bearing and the sibling is reported.
    """
    n = geom.n
    X = _as_channels_by_samples(snapshots, n)
    phi = _element_azimuths(geom)
    radii = _radii(geom)

    # auto-detect a centre (sense) element: one sitting at (near) the centroid
    if sense_index is None:
        near0 = np.where(radii < 1e-6 + 0.05 * (np.max(radii) or 1.0))[0]
        sense_index = int(near0[0]) if near0.size and np.max(radii) > 0 else None

    # ring elements (exclude the sense element from the loop combiner)
    ring = np.ones(n, dtype=bool)
    if sense_index is not None:
        ring[sense_index] = False
    cphi = np.cos(phi) * ring
    sphi = np.sin(phi) * ring

    ns = cphi @ X                                  # (K,) N-S loop
    ew = sphi @ X                                  # (K,) E-W loop
    if sense_index is not None:
        sense = X[sense_index]
    else:
        sense = np.ones(n) @ X                      # zeroth harmonic = omni

    R = (X @ X.conj().T) / max(1, X.shape[1])
    snr_db = _snr_db_from_R(R)

    have_sense = float(np.mean(np.abs(sense) ** 2)) > 1e-12 * float(np.mean(np.abs(X) ** 2) + 1e-30)
    ambiguities: list = []
    if have_sense:
        c = np.exp(-1j * math.radians(loop_phase_deg))
        g_ns = float(np.real(np.vdot(sense, ns) * c))   # Σ ns·conj(sense)·e^{-jψ}
        g_ew = float(np.real(np.vdot(sense, ew) * c))
        az = math.degrees(math.atan2(g_ew, g_ns)) % 360.0
        notes = ["Watson-Watt (Adcock) with sense channel → unambiguous 0–360°"]
    else:
        # 180°-ambiguous principal-axis estimate from the loop covariance
        a = float(np.real(np.vdot(ns, ns)))
        b = float(np.real(np.vdot(ew, ew)))
        cross = complex(np.vdot(ns, ew))
        az = (0.5 * math.degrees(math.atan2(2.0 * cross.real, a - b))) % 360.0
        ambiguities = [{"az_deg": round((az + 180.0) % 360.0, 1), "el_deg": 0.0, "score_db": 0.0}]
        notes = ["Watson-Watt without a sense channel → 180° (front/back) ambiguous; add a sense/omni element to resolve"]

    sigma_phase = _sigma_phase_from_snr(snr_db, n)
    s_az, _ = _crlb_phase(geom, freq_hz, az, 0.0, sigma_phase, 0, False)
    loop_pow = float(np.mean(np.abs(ns) ** 2 + np.abs(ew) ** 2))
    quality = float(np.clip(loop_pow / (loop_pow + np.mean(np.abs(X) ** 2) + 1e-12), 0.05, 0.99))
    res = AoAResult(method="watson_watt", az_deg=round(az, 2), el_deg=0.0,
                    sigma_az_deg=round(s_az, 2), sigma_el_deg=90.0, quality=round(quality, 3),
                    ambiguities=ambiguities, snr_db=snr_db, n_elements=n, array=geom.name, notes=notes)
    if observer_heading_deg is not None:
        res.az_true_deg = round((az + float(observer_heading_deg)) % 360.0, 2)
    return res


# ── correlative DF (CDF / correlative interferometer) ────────────────────────
def correlative_aoa(
    geom: ArrayGeometry, freq_hz: float, snapshots: np.ndarray, *,
    az_step: float = 1.0, el_range: tuple[float, float] = (-10.0, 80.0), el_step: float = 5.0,
    manifold: Optional[np.ndarray] = None, observer_heading_deg: Optional[float] = None,
) -> AoAResult:
    """Correlative DF: correlate the measured complex array response against the
    manifold a(θ) and pick the azimuth of maximum correlation
    ``ρ(θ) = |aᴴ(θ)·v|² / (‖a‖²‖v‖²)`` where ``v`` is the dominant signal
    eigenvector of the snapshot covariance. ``manifold`` lets a *measured/
    calibrated* response table override the modelled steering vectors (the real-
    world way correlative interferometers beat geometry error)."""
    n = geom.n
    X = _as_channels_by_samples(snapshots, n)
    R = (X @ X.conj().T) / max(1, X.shape[1])
    evals, evecs = np.linalg.eigh(R)
    v = evecs[:, -1]                                # dominant (signal) eigenvector
    snr_db = _snr_db_from_R(R)

    full_3d = (not geom.is_collinear) and (not geom.is_planar_horizontal)
    el_lo, el_hi = (el_range if full_3d else (0.0, 0.0))
    el_st = (el_step if full_3d else 1.0)
    az = np.arange(0.0, 360.0, az_step)
    el = np.arange(el_lo, el_hi + el_st * 0.5, el_st)
    AZ, EL = np.meshgrid(az, el, indexing="ij")
    A = manifold if manifold is not None else steering_matrix(geom, freq_hz, AZ, EL)   # (Naz, Nel, N)

    num = np.abs(A.conj() @ v) ** 2                 # |aᴴ v|² over the grid
    den = np.sum(np.abs(A) ** 2, axis=-1) * float(np.vdot(v, v).real)
    rho = num / np.maximum(den, 1e-12)              # 0..1 correlation
    i_best = np.unravel_index(int(np.argmax(rho)), rho.shape)
    best_az, best_el = float(AZ[i_best]), float(EL[i_best])

    Pdb = 10.0 * np.log10(np.maximum(rho, 1e-12))
    Pdb -= float(np.max(Pdb))
    el_idx = i_best[1]
    spec = {"az_deg": az.tolist(), "power_db": [round(float(v_), 2) for v_ in Pdb[:, el_idx]]}
    # secondary correlation peaks
    sib = []
    cut = Pdb[:, el_idx].copy()
    cut[i_best[0]] = -1e9
    for _ in range(3):
        j = int(np.argmax(cut))
        if cut[j] < -3.0:
            break
        sib.append({"az_deg": round(float(az[j]), 1), "el_deg": round(best_el, 1), "score_db": round(float(cut[j]), 1)})
        lo, hi = max(0, j - int(round(8.0 / az_step))), min(len(cut), j + int(round(8.0 / az_step)) + 1)
        cut[lo:hi] = -1e9

    sigma_phase = _sigma_phase_from_snr(snr_db, n)
    s_az, s_el = _crlb_phase(geom, freq_hz, best_az, best_el, sigma_phase, 0, full_3d)
    quality = float(np.clip(rho[i_best], 0.0, 1.0))
    res = AoAResult(method="correlative", az_deg=round(best_az, 2), el_deg=round(best_el, 2),
                    sigma_az_deg=round(s_az, 2), sigma_el_deg=round(s_el, 2), quality=round(quality, 3),
                    ambiguities=sib, spectrum=spec, snr_db=snr_db, n_elements=n, array=geom.name,
                    notes=["correlative DF (CDF / correlative interferometer): max complex-pattern correlation vs the manifold"])
    if observer_heading_deg is not None:
        res.az_true_deg = round((best_az + float(observer_heading_deg)) % 360.0, 2)
    return res


# ── pseudo-Doppler (phase-mode) DF ───────────────────────────────────────────
def pseudo_doppler_aoa(
    geom: ArrayGeometry, freq_hz: float, snapshots: np.ndarray, *,
    observer_heading_deg: Optional[float] = None,
) -> AoAResult:
    """Phase-mode / pseudo-Doppler DF on a circular array: the bearing is the
    phase of the first spatial Fourier mode of the array response,
    ``θ = 90° − arg(Σ_i v_i·e^{-jφ_i})``. Needs a (near-)circular array."""
    n = geom.n
    X = _as_channels_by_samples(snapshots, n)
    R = (X @ X.conj().T) / max(1, X.shape[1])
    evals, evecs = np.linalg.eigh(R)
    v = evecs[:, -1]                                # measured per-element response
    phi = _element_azimuths(geom)
    # Pseudo-Doppler demod: the element phase pattern is ψ_i ≈ k·r·cos(az − φ_i)
    # (plus an arbitrary global phase that cancels over the symmetric ring), so the
    # bearing is the argument of its first spatial Fourier coefficient:
    #   az = atan2( Σ ψ_i sin φ_i , Σ ψ_i cos φ_i )
    psi = np.angle(v)
    az = math.degrees(math.atan2(float(psi @ np.sin(phi)), float(psi @ np.cos(phi)))) % 360.0
    snr_db = _snr_db_from_R(R)
    sigma_phase = _sigma_phase_from_snr(snr_db, n)
    s_az, _ = _crlb_phase(geom, freq_hz, az, 0.0, sigma_phase, 0, False)
    mode1 = complex(np.sum(np.exp(1j * psi) * np.exp(-1j * phi)))
    quality = float(np.clip(abs(mode1) / n, 0.05, 0.99))
    res = AoAResult(method="doppler", az_deg=round(az, 2), el_deg=0.0,
                    sigma_az_deg=round(s_az, 2), sigma_el_deg=90.0, quality=round(quality, 3),
                    snr_db=snr_db, n_elements=n, array=geom.name,
                    notes=["pseudo-Doppler / phase-mode DF: bearing from the 1st spatial Fourier mode of a circular array"])
    if observer_heading_deg is not None:
        res.az_true_deg = round((az + float(observer_heading_deg)) % 360.0, 2)
    return res
