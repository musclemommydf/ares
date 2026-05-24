# SPDX-License-Identifier: MIT OR Apache-2.0
# Copyright (c) 2026 Ares

"""
interferometry.py — array direction-finding (Workstream D).

The array-signal-processing layer Ares previously *didn't* have — it consumed
bearings from KrakenSDR/external pipelines; now it can produce them. Given a
multi-element antenna array (ULA / UCA / arbitrary 2-D or 3-D geometry) and either
the inter-channel **phase differences** or the raw **IQ snapshots** at a known
frequency, it estimates the angle of arrival with the proper estimators and their
Cramér-Rao-bound uncertainties:

  * **Phase interferometry** (``aoa_interferometry``) — the rigorous multi-baseline
    method: build the array manifold over an (az, el) grid, find the AoA whose
    *unwrapped* model phase-difference vector best matches the *wrapped* measured
    one (this is also exactly correlative interferometry / phase-only ML), refine
    with Gauss-Newton; long baselines give precision, short baselines resolve the
    2π ambiguity; reports the CRLB σ_az / σ_el = σ_φ · √diag((JᵀJ)⁻¹) and any
    ambiguity siblings.
  * **MUSIC** (``aoa_music``), **Capon/MVDR** (``aoa_capon``), **Bartlett**
    (``aoa_bartlett``) — covariance-based super-resolution / beamforming from IQ
    snapshots, with optional forward-backward spatial smoothing for coherent
    (multipath) sources; reports the spatial spectrum and the deterministic CRLB.

All angles: azimuth from true north (clockwise), elevation up from the horizon.
The array's mechanical boresight/heading is applied by the caller (or via
``observer_heading_deg`` in the route) so the returned bearing is true-referenced.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Optional, Sequence

import numpy as np

_C = 299_792_458.0


# ── array geometry ───────────────────────────────────────────────────────────
@dataclass
class ArrayGeometry:
    """Element positions in metres in a local ENU frame (east, north, up), N×3.
    Built from ``ula(...)`` / ``uca(...)`` / explicit positions. The array
    boresight (the +y / north axis for ULA, 0° for UCA) is the angular reference;
    the caller rotates by the platform heading to get a true bearing."""
    positions_m: np.ndarray            # (N, 3)
    name: str = "custom"

    @property
    def n(self) -> int:
        return self.positions_m.shape[0]

    @property
    def is_collinear(self) -> bool:
        p = self.positions_m - self.positions_m.mean(axis=0)
        if p.shape[0] < 3:
            return True
        # collinear ⇔ the 2 smallest singular values are ~0
        s = np.linalg.svd(p, compute_uv=False)
        return s[1] < 1e-6 * max(1e-9, s[0])

    @property
    def is_planar_horizontal(self) -> bool:
        return float(np.max(np.abs(self.positions_m[:, 2]))) < 1e-6

    @staticmethod
    def ula(n: int, spacing_m: float, along: str = "north") -> "ArrayGeometry":
        idx = (np.arange(n) - (n - 1) / 2.0) * spacing_m
        pos = np.zeros((n, 3))
        pos[:, 1 if along == "north" else 0] = idx
        return ArrayGeometry(pos, name=f"ULA-{n}@{spacing_m:.3g}m")

    @staticmethod
    def uca(n: int, radius_m: float) -> "ArrayGeometry":
        ang = np.arange(n) * 2.0 * math.pi / n          # element 0 toward north (+y)
        pos = np.zeros((n, 3))
        pos[:, 0] = radius_m * np.sin(ang)              # east
        pos[:, 1] = radius_m * np.cos(ang)             # north
        return ArrayGeometry(pos, name=f"UCA-{n}@{radius_m:.3g}m")

    @staticmethod
    def adcock(n: int, radius_m: float, sense: bool = True) -> "ArrayGeometry":
        """Adcock DF array: ``n`` ring elements (n=4 → crossed N/E/S/W pairs,
        n=8 → HF Adcock) optionally plus a central omni *sense* element used by
        Watson-Watt to resolve the 180° front/back ambiguity. Geometrically a
        UCA (+ centre); the Watson-Watt combiner forms the loop/sense channels."""
        ring = ArrayGeometry.uca(n, radius_m).positions_m
        pos = np.vstack([ring, np.zeros((1, 3))]) if sense else ring
        return ArrayGeometry(pos, name=f"Adcock-{n}{'+S' if sense else ''}@{radius_m:.3g}m")

    @staticmethod
    def from_positions(positions: Sequence[Sequence[float]], name: str = "custom") -> "ArrayGeometry":
        p = np.asarray(positions, dtype=float)
        if p.ndim != 2 or p.shape[1] not in (2, 3):
            raise ValueError("positions must be N×2 or N×3 (metres)")
        if p.shape[1] == 2:
            p = np.hstack([p, np.zeros((p.shape[0], 1))])
        return ArrayGeometry(p, name=name)


def _direction_unit(az_deg: np.ndarray, el_deg: np.ndarray) -> np.ndarray:
    """Unit vector(s) pointing *toward* the source (ENU), shape (..., 3)."""
    az = np.radians(az_deg)
    el = np.radians(el_deg)
    ce = np.cos(el)
    return np.stack([np.sin(az) * ce, np.cos(az) * ce, np.sin(el)], axis=-1)


def steering_matrix(geom: ArrayGeometry, freq_hz: float, az_deg: np.ndarray, el_deg: np.ndarray) -> np.ndarray:
    """Array manifold a(θ): exp(j (2π/λ) p·û). Returns shape (..., N) complex."""
    lam = _C / max(1.0, float(freq_hz))
    u = _direction_unit(np.asarray(az_deg, dtype=float), np.asarray(el_deg, dtype=float))   # (..., 3)
    phase = (2.0 * math.pi / lam) * (u @ geom.positions_m.T)                                  # (..., N)
    return np.exp(1j * phase)


def model_phase_diff(geom: ArrayGeometry, freq_hz: float, az_deg, el_deg, ref: int = 0) -> np.ndarray:
    """*Unwrapped* model phase differences Δψ_i = ψ_i − ψ_ref (rad). Shape (..., N)."""
    lam = _C / max(1.0, float(freq_hz))
    u = _direction_unit(np.asarray(az_deg, dtype=float), np.asarray(el_deg, dtype=float))
    psi = (2.0 * math.pi / lam) * (u @ geom.positions_m.T)
    return psi - psi[..., ref:ref + 1]


def _wrap(a):
    return (a + math.pi) % (2.0 * math.pi) - math.pi


# ── grid cache ───────────────────────────────────────────────────────────────
@lru_cache(maxsize=16)
def _grid(geom_key: tuple, freq_hz: float, az_step: float, el_min: float, el_max: float, el_step: float, ref: int):
    geom = _GEOM_BY_KEY[geom_key]
    az = np.arange(0.0, 360.0, az_step)
    el = np.arange(el_min, el_max + el_step * 0.5, el_step)
    AZ, EL = np.meshgrid(az, el, indexing="ij")             # (Naz, Nel)
    A = steering_matrix(geom, freq_hz, AZ, EL)              # (Naz, Nel, N)
    PD = model_phase_diff(geom, freq_hz, AZ, EL, ref=ref)   # (Naz, Nel, N) unwrapped
    return az, el, AZ, EL, A, PD


_GEOM_BY_KEY: dict = {}


def _geom_key(geom: ArrayGeometry) -> tuple:
    k = (geom.name, tuple(map(tuple, np.round(geom.positions_m, 6).tolist())))
    _GEOM_BY_KEY[k] = geom
    return k


# ── results ──────────────────────────────────────────────────────────────────
@dataclass
class AoAResult:
    method: str
    az_deg: float
    el_deg: float
    sigma_az_deg: float
    sigma_el_deg: float
    quality: float                       # 0..1 — fit quality (1 = perfect)
    az_true_deg: Optional[float] = None  # az + observer heading, if supplied
    n_sources: int = 1
    ambiguities: list = field(default_factory=list)   # [{az_deg, el_deg, score_db}] — other strong solutions
    spectrum: Optional[dict] = None      # {"az_deg":[...], "power_db":[...]} for the beamformers
    snr_db: Optional[float] = None
    n_elements: int = 0
    array: str = ""
    notes: list = field(default_factory=list)


def _crlb_phase(geom: ArrayGeometry, freq_hz: float, az_deg: float, el_deg: float, sigma_phase_rad: float,
                ref: int, full_3d: bool) -> tuple[float, float]:
    """CRLB σ_az, σ_el (deg) for the phase-difference ML estimator at (az, el):
    Cov = σ_φ² (Jᵀ J)⁻¹, J = ∂Δψ/∂(az, el) in rad/rad."""
    d = 1e-4   # rad — finite difference step in az/el
    pd0 = model_phase_diff(geom, freq_hz, az_deg, el_deg, ref=ref)
    daz = (model_phase_diff(geom, freq_hz, az_deg + math.degrees(d), el_deg, ref=ref) - pd0) / d
    if full_3d:
        de = (model_phase_diff(geom, freq_hz, az_deg, el_deg + math.degrees(d), ref=ref) - pd0) / d
        J = np.stack([daz, de], axis=1)                # (N, 2)
        try:
            cov = (sigma_phase_rad ** 2) * np.linalg.inv(J.T @ J)
            return math.degrees(math.sqrt(max(0.0, cov[0, 0]))), math.degrees(math.sqrt(max(0.0, cov[1, 1])))
        except np.linalg.LinAlgError:
            pass
    # azimuth-only
    fisher = float(daz @ daz)
    if fisher <= 0:
        return 90.0, 90.0
    return math.degrees(math.sqrt((sigma_phase_rad ** 2) / fisher)), 90.0


# ── phase interferometry (the headline) ──────────────────────────────────────
def aoa_interferometry(
    geom: ArrayGeometry, freq_hz: float, measured_phase_diff_rad: Sequence[float],
    *, ref: int = 0, sigma_phase_deg: float = 8.0, az_step: float = 1.0,
    el_range: tuple[float, float] = (-10.0, 80.0), el_step: float = 5.0,
    refine: bool = True, ambiguity_db: float = 1.0, observer_heading_deg: Optional[float] = None,
) -> AoAResult:
    """Estimate AoA from inter-channel phase differences m_i = arg(x_i / x_ref).
    Multi-baseline → ambiguity-resolved; CRLB σ reported. ``measured_phase_diff_rad``
    is length N (the ref element's entry is ignored)."""
    m = np.asarray(measured_phase_diff_rad, dtype=float).copy()
    if m.shape[0] != geom.n:
        raise ValueError(f"expected {geom.n} phase values, got {m.shape[0]}")
    m[ref] = 0.0
    full_3d = (not geom.is_collinear) and (not geom.is_planar_horizontal)   # az-only for ULA & horizontal UCA; full az+el only with vertical extent
    el_lo, el_hi = (el_range if full_3d else (0.0, 0.0))
    el_st = (el_step if full_3d else 1.0)
    az, el, AZ, EL, A, PD = _grid(_geom_key(geom), float(freq_hz), float(az_step), el_lo, el_hi, el_st, int(ref))
    # cost = Σ |wrap(m - Δψ_model)|²  over the grid
    resid = _wrap(m[None, None, :] - PD)                       # (Naz, Nel, N)
    cost = np.sum(resid * resid, axis=-1)                      # (Naz, Nel)
    i_best = np.unravel_index(int(np.argmin(cost)), cost.shape)
    best_az, best_el = float(AZ[i_best]), float(EL[i_best])
    best_cost = float(cost[i_best])
    sigma_phase_rad = math.radians(max(0.5, sigma_phase_deg))
    # ambiguity siblings: other grid minima whose cost is within `ambiguity_db` of the best
    # (express the cost as an equivalent "power" 1/(1+cost) and compare in dB)
    pwr = 1.0 / (1.0 + cost)
    thr = (10.0 ** (-ambiguity_db / 10.0)) * float(pwr[i_best])
    sib = []
    cm = cost.copy()
    # suppress a small neighbourhood of the global best, then look for the next peaks
    def _suppress(idx, rad=int(round(8.0 / az_step))):
        ai, ei = idx
        for da in range(-rad, rad + 1):
            cm[(ai + da) % cm.shape[0], :] = np.inf
    _suppress(i_best)
    for _ in range(4):
        j = np.unravel_index(int(np.argmin(cm)), cm.shape)
        if not np.isfinite(cm[j]):
            break
        if 1.0 / (1.0 + float(cm[j])) >= thr:
            sib.append({"az_deg": round(float(AZ[j]), 1), "el_deg": round(float(EL[j]), 1),
                        "score_db": round(10.0 * math.log10((1.0 / (1.0 + float(cm[j]))) / float(pwr[i_best])), 1)})
            _suppress(j)
        else:
            break
    # Gauss-Newton refine on (az[, el]) minimising Σ wrap(m - Δψ(θ))²
    if refine:
        x = np.array([best_az, best_el], dtype=float)
        for _ in range(12):
            pd = model_phase_diff(geom, freq_hz, x[0], x[1], ref=ref)
            r = _wrap(m - pd)
            d = 1e-4
            jaz = (model_phase_diff(geom, freq_hz, x[0] + math.degrees(d), x[1], ref=ref) - pd) / d
            if full_3d:
                jel = (model_phase_diff(geom, freq_hz, x[0], x[1] + math.degrees(d), ref=ref) - pd) / d
                J = np.stack([jaz, jel], axis=1)
            else:
                J = jaz[:, None]
            try:
                step = np.linalg.solve(J.T @ J, J.T @ r)
            except np.linalg.LinAlgError:
                break
            x[:len(step)] += np.degrees(step)
            x[0] %= 360.0
            if not full_3d:
                x[1] = 0.0
            if float(np.sum(step * step)) < 1e-12:
                break
        best_az, best_el = float(x[0] % 360.0), float(x[1])
    s_az, s_el = _crlb_phase(geom, freq_hz, best_az, best_el, sigma_phase_rad, ref, full_3d)
    rms_resid = math.sqrt(best_cost / max(1, geom.n - 1))
    quality = float(math.exp(-rms_resid))                    # 1 at perfect, decays with the residual
    notes = []
    if geom.is_collinear:
        notes.append("collinear array: azimuth-only; the mirror solution about the array axis is an unresolved ambiguity unless the manifold is non-symmetric")
    res = AoAResult(method="interferometry", az_deg=round(best_az, 2), el_deg=round(best_el, 2),
                    sigma_az_deg=round(s_az, 2), sigma_el_deg=round(s_el, 2), quality=round(quality, 3),
                    ambiguities=sib, n_elements=geom.n, array=geom.name, notes=notes)
    if observer_heading_deg is not None:
        res.az_true_deg = round((best_az + float(observer_heading_deg)) % 360.0, 2)
    return res


# ── covariance-based estimators (MUSIC / Capon / Bartlett) ───────────────────
def _smooth_fb(R: np.ndarray) -> np.ndarray:
    """Forward-backward averaging — decorrelates coherent (multipath) sources for a ULA."""
    n = R.shape[0]
    J = np.fliplr(np.eye(n))
    return 0.5 * (R + J @ R.conj() @ J)


def _spectral_aoa(
    geom: ArrayGeometry, freq_hz: float, R: np.ndarray, kind: str, n_sources: int,
    az_step: float, el_range: tuple[float, float], el_step: float, snr_db: Optional[float],
    observer_heading_deg: Optional[float],
) -> AoAResult:
    full_3d = (not geom.is_collinear) and (not geom.is_planar_horizontal)   # az-only for ULA & horizontal UCA; full az+el only with vertical extent
    el_lo, el_hi = (el_range if full_3d else (0.0, 0.0))
    el_st = (el_step if full_3d else 1.0)
    az = np.arange(0.0, 360.0, az_step)
    el = np.arange(el_lo, el_hi + el_st * 0.5, el_st)
    AZ, EL = np.meshgrid(az, el, indexing="ij")
    A = steering_matrix(geom, freq_hz, AZ, EL)               # (Naz, Nel, N)
    if kind == "music":
        evals, evecs = np.linalg.eigh(R)                     # ascending
        k = max(1, min(geom.n - 1, int(n_sources)))
        En = evecs[:, : geom.n - k]                          # noise subspace
        proj = A.conj() @ En                                 # aᴴ E_n  per grid point — (Naz, Nel, N-k)
        denom = np.sum(np.abs(proj) ** 2, axis=-1)
        P = 1.0 / np.maximum(denom, 1e-12)
    elif kind == "capon":
        try:
            Rinv = np.linalg.inv(R + 1e-6 * np.trace(R) / geom.n * np.eye(geom.n))
        except np.linalg.LinAlgError:
            Rinv = np.linalg.pinv(R)
        quad = np.real(np.einsum("...i,ij,...j->...", A.conj(), Rinv, A))
        P = 1.0 / np.maximum(quad, 1e-12)
    else:  # bartlett (conventional beamformer)
        kind = "bartlett"
        P = np.real(np.einsum("...i,ij,...j->...", A.conj(), R, A))
    Pdb = 10.0 * np.log10(np.maximum(P, 1e-12))
    Pdb -= float(np.max(Pdb))
    i_best = np.unravel_index(int(np.argmax(P)), P.shape)
    best_az, best_el = float(AZ[i_best]), float(EL[i_best])
    # CRLB-ish σ from the manifold gradient and an effective phase noise from SNR (or a 6° default)
    sigma_phase_rad = math.radians(8.0) if snr_db is None else 1.0 / math.sqrt(2.0 * max(0.5, 10.0 ** (snr_db / 10.0)) * max(1, R.shape[0]))
    s_az, s_el = _crlb_phase(geom, freq_hz, best_az, best_el, max(1e-3, sigma_phase_rad), 0, full_3d)
    # az-cut spectrum (at the best elevation row)
    el_idx = i_best[1]
    spec = {"az_deg": az.tolist(), "power_db": [round(float(v), 2) for v in Pdb[:, el_idx]]}
    # extra peaks (multi-source) along the az cut. Suppress the *main lobe*
    # (a ±half-window, circular in azimuth) before looking for the next peak —
    # otherwise the "second peak" is just the main peak's neighbouring bin.
    sib = []
    cut = Pdb[:, el_idx].copy()
    half = max(1, int(round(8.0 / az_step)))
    n_az = cut.shape[0]
    for off in range(-half, half + 1):
        cut[(int(i_best[0]) + off) % n_az] = -1e9
    # quality = peak-to-highest-sidelobe (dB) → 0..1; the spectrum is normalised
    # so the peak sits at 0 dB, hence quality = −(highest sidelobe)/20.
    highest_sidelobe = float(np.max(cut))
    quality = float(np.clip((0.0 - highest_sidelobe) / 20.0, 0.0, 1.0))
    for _ in range(max(0, int(n_sources) - 1)):
        j = int(np.argmax(cut))
        if cut[j] < -20.0:
            break
        sib.append({"az_deg": round(float(az[j]), 1), "el_deg": round(best_el, 1), "score_db": round(float(cut[j]), 1)})
        for off in range(-half, half + 1):
            cut[(j + off) % n_az] = -1e9
    res = AoAResult(method=kind, az_deg=round(best_az, 2), el_deg=round(best_el, 2),
                    sigma_az_deg=round(s_az, 2), sigma_el_deg=round(s_el, 2), quality=round(quality, 3),
                    n_sources=int(n_sources), ambiguities=sib, spectrum=spec, snr_db=snr_db,
                    n_elements=geom.n, array=geom.name)
    if observer_heading_deg is not None:
        res.az_true_deg = round((best_az + float(observer_heading_deg)) % 360.0, 2)
    return res


def aoa_from_snapshots(
    geom: ArrayGeometry, freq_hz: float, snapshots: np.ndarray,
    *, method: str = "music", n_sources: int = 1, fb_smoothing: bool = False,
    az_step: float = 1.0, el_range: tuple[float, float] = (-10.0, 80.0), el_step: float = 5.0,
    observer_heading_deg: Optional[float] = None,
) -> AoAResult:
    """AoA from IQ snapshots (complex, shape (N, K) — N channels × K samples).
    method ∈ {music, capon, bartlett}. `fb_smoothing` applies forward-backward
    averaging (ULA — decorrelates coherent multipath)."""
    X = np.asarray(snapshots)
    if X.dtype != complex and X.dtype != np.complex128 and X.dtype != np.complex64:
        X = X.astype(complex)
    if X.ndim != 2:
        raise ValueError("snapshots must be 2-D (N channels × K samples)")
    if X.shape[0] != geom.n and X.shape[1] == geom.n:
        X = X.T            # be forgiving about orientation
    if X.shape[0] != geom.n:
        raise ValueError(f"channel count {X.shape[0]} ≠ array size {geom.n}")
    # ALARIS-class classic estimators (Adcock/Watson-Watt, correlative DF, Doppler)
    m = method.lower()
    if m in ("watson_watt", "watson-watt", "ww", "adcock", "correlative", "cdf",
             "cidf", "doppler", "pseudo_doppler"):
        from . import classic_df
        if m in ("watson_watt", "watson-watt", "ww", "adcock"):
            return classic_df.watson_watt_aoa(geom, freq_hz, X, observer_heading_deg=observer_heading_deg)
        if m in ("doppler", "pseudo_doppler"):
            return classic_df.pseudo_doppler_aoa(geom, freq_hz, X, observer_heading_deg=observer_heading_deg)
        return classic_df.correlative_aoa(geom, freq_hz, X, az_step=az_step, el_range=el_range,
                                          el_step=el_step, observer_heading_deg=observer_heading_deg)
    K = X.shape[1]
    R = (X @ X.conj().T) / max(1, K)
    if fb_smoothing:
        R = _smooth_fb(R)
    # crude SNR: largest eigenvalue vs the mean of the noise eigenvalues
    ev = np.linalg.eigvalsh(R)
    k = max(1, min(geom.n - 1, int(n_sources)))
    noise = float(np.mean(ev[: geom.n - k])) if geom.n - k > 0 else float(ev[0])
    snr_db = 10.0 * math.log10(max(1e-9, (float(ev[-1]) - noise) / max(1e-12, noise))) if noise > 0 else None
    return _spectral_aoa(geom, freq_hz, R, method.lower(), n_sources, az_step, el_range, el_step, snr_db, observer_heading_deg)


# ── helpers for callers (e.g. an SDR adapter) ────────────────────────────────
def geometry_from_spec(spec: dict) -> ArrayGeometry:
    """Build an ArrayGeometry from a JSON-ish spec:
      {"type":"ula","n":5,"spacing_m":0.34}
      {"type":"uca","n":5,"radius_m":0.21}
      {"type":"custom","positions_m":[[e,n,u],...]}  (u optional)"""
    t = (spec.get("type") or "custom").lower()
    if t == "ula":
        return ArrayGeometry.ula(int(spec["n"]), float(spec["spacing_m"]), along=spec.get("along", "north"))
    if t == "uca":
        return ArrayGeometry.uca(int(spec["n"]), float(spec["radius_m"]))
    if t == "adcock":
        return ArrayGeometry.adcock(int(spec["n"]), float(spec["radius_m"]), sense=bool(spec.get("sense", True)))
    return ArrayGeometry.from_positions(spec["positions_m"], name=spec.get("name", "custom"))


def aoa_to_lob(res: AoAResult, observer: dict, frequency_hz: float, rssi_dbm: float = -75.0) -> dict:
    """Turn an AoA result into a ``geolocation.LoB``-shaped dict ready for
    ``/geolocate/fix`` — the σ_az becomes the confidence so the array's measured
    uncertainty propagates straight into the ML triangulation + error ellipse."""
    az_true = res.az_true_deg if res.az_true_deg is not None else res.az_deg
    conf = max(5.0, min(99.0, 100.0 - res.sigma_az_deg * 3.0))   # σ 1°→97 %, 10°→70 %, 30°→10 %
    return {
        "lat": observer["lat"], "lon": observer["lon"], "azimuth_deg": az_true % 360.0,
        "frequency_hz": frequency_hz, "rssi_dbm": rssi_dbm, "confidence_pct": conf,
        "observer_height_m": observer.get("height_m", 1.5),
        "environment": observer.get("environment", "suburban"),
        "device_type": "interferometer", "device_id": observer.get("device_id", ""),
    }
