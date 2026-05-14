"""
Multi-baseline interferometry — DF from inter-element phase differences across
*multiple* baseline lengths and/or orientations.

A single baseline has two fundamental ambiguities:
  1. Left–right of the baseline axis (cos(θ−axis) is symmetric → ±off-axis
     angles give the same phase).
  2. Phase wrap when |baseline| > λ/2 (so multiple θ candidates fall within
     [0, 360)).

To resolve them you need a SECOND baseline at a different orientation. The
two baselines' candidate sets intersect at the true bearing. If the user
supplies a `prior_bearing_deg` we additionally pick the nearest of any
remaining candidates.

Returns (always):
  {
    "bearing_deg":          best estimate (None if no candidates),
    "candidates":           the intersection of per-baseline candidate sets,
    "sigma_az_deg":         Cramér-Rao 1-σ from the longest baseline,
    "ambiguous":            len(candidates) > 1,
    "details":              [ { d_m, axis_deg, per-baseline candidates } ],
  }

Phase-sign convention:
  φ_AB = (2π/λ) · b̂ · ŝ        with ŝ = (sinθ, cosθ),  b̂ = baseline unit vec
  (positive phase ⇔ source is on the +b̂ side of the array)
"""

from __future__ import annotations

import math
from typing import Optional

import numpy as np


def _baseline_candidates(phase_rad: float, baseline_vec: np.ndarray,
                          wavelength_m: float) -> list[float]:
    """Every true-bearing θ ∈ [0, 360) consistent with the measured phase."""
    d = float(np.linalg.norm(baseline_vec))
    if d <= 0:
        return []
    # Baseline axis bearing — atan2(east, north), true convention.
    axis_deg = (math.degrees(math.atan2(baseline_vec[0], baseline_vec[1])) + 360) % 360
    # phase = (2π d / λ) cos(θ − axis)
    # Solve for θ for every integer k that puts cos(θ−axis) in [-1, +1].
    max_k = max(1, int(math.ceil(2 * d / wavelength_m)) + 1)
    candidates: list[float] = []
    for k in range(-max_k, max_k + 1):
        c = (phase_rad + 2 * math.pi * k) * wavelength_m / (2 * math.pi * d)
        if -1.0 <= c <= 1.0:
            off = math.degrees(math.acos(c))
            # Both ±off are mathematically valid — keep both, deduped below.
            for theta in ((axis_deg + off) % 360, (axis_deg - off + 360) % 360):
                if not any(_circ_close(theta, t) for t in candidates):
                    candidates.append(theta)
    return candidates


def _circ_close(a: float, b: float, tol_deg: float = 0.5) -> bool:
    return min(abs(a - b), 360 - abs(a - b)) <= tol_deg


def _intersect(a: list[float], b: list[float], tol_deg: float = 6.0) -> list[float]:
    """Bearings that appear (within ±tol) in BOTH lists. Useful tolerance grows
    with the shortest baseline's σ + phase noise; 6° is a safe default for
    typical operational σ ~ 3°."""
    out: list[float] = []
    for x in a:
        for y in b:
            if _circ_close(x, y, tol_deg):
                # Use the mean of the two candidates (circular).
                rad = np.radians([x, y])
                m = (math.degrees(math.atan2(np.sin(rad).mean(), np.cos(rad).mean())) + 360) % 360
                if not any(_circ_close(m, o, tol_deg) for o in out):
                    out.append(m)
                break
    return out


def resolve_bearing(baselines: list[dict], wavelength_m: float,
                    prior_bearing_deg: Optional[float] = None,
                    intersect_tol_deg: float = 6.0) -> dict:
    """Resolve a true bearing from a list of baseline measurements.

    baselines: [ { vec_m: [east_m, north_m], phase_rad, sigma_rad? } ]
    prior_bearing_deg: optional coarse direction (e.g. previous fix) used to
                       break a remaining ambiguity; None ⇒ return all
                       candidates.
    intersect_tol_deg: angular tolerance for cross-baseline candidate match
                       (grow this with poor phase SNR).
    """
    if not baselines:
        return {"bearing_deg": None, "candidates": [], "sigma_az_deg": None,
                "ambiguous": False, "details": []}

    per_baseline = []
    details = []
    for b in baselines:
        vec = np.asarray(b["vec_m"], dtype=float)
        cands = _baseline_candidates(float(b["phase_rad"]), vec, wavelength_m)
        per_baseline.append(cands)
        details.append({
            "d_m": float(np.linalg.norm(vec)),
            "axis_deg": (math.degrees(math.atan2(vec[0], vec[1])) + 360) % 360,
            "candidates": cands,
        })

    # Intersect candidate sets across all baselines.
    consensus = per_baseline[0][:]
    for nxt in per_baseline[1:]:
        consensus = _intersect(consensus, nxt, intersect_tol_deg)
        if not consensus:
            break

    # If no consensus (e.g. degenerate/co-linear baselines), fall back to the
    # union of the shortest baseline's candidates — still ambiguous but at
    # least exposes the operator's options.
    if not consensus:
        consensus = per_baseline[0][:]

    # Pick a single best bearing.
    if prior_bearing_deg is not None and consensus:
        best = min(consensus,
                    key=lambda t: min(abs(t - prior_bearing_deg), 360 - abs(t - prior_bearing_deg)))
    else:
        best = consensus[0] if consensus else None

    # Cramér-Rao σ ≈ σ_φ · λ / (2π · d_longest)
    sigma_phi = float(np.mean([b.get("sigma_rad", 0.1) for b in baselines]))
    longest = max(float(np.linalg.norm(b["vec_m"])) for b in baselines)
    sigma_az_rad = sigma_phi * wavelength_m / (2 * math.pi * max(longest, 1e-6))

    return {
        "bearing_deg": best,
        "candidates": consensus,
        "sigma_az_deg": math.degrees(sigma_az_rad),
        "ambiguous": len(consensus) > 1,
        "details": details,
    }
