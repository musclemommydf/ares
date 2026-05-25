# SPDX-License-Identifier: MIT OR Apache-2.0
# Copyright (c) 2026 Ares

"""
Diffraction Models
Implements multiple obstacle diffraction models for terrain-aware path loss:
  - Single Knife Edge (Fresnel-Kirchhoff)
  - Epstein-Peterson (1953) — sequential multi-obstacle
  - Bullington (1977)      — fast multi-obstacle approximation
  - Giovanelli (1984)      — multi-obstacle with combining factor
  - Deygout (1994)         — priority-based multi-obstacle (default)

These complement empirical models (Hata, FSPL, etc.) by adding terrain-aware
diffraction losses/gains that those models otherwise ignore.  ITM already
includes diffraction internally, so applying these on top of ITM is redundant.

All functions accept:
  elevations  — terrain heights (m ASL) at each profile point
  distances_m — accumulated distance (m) at each profile point
  tx_height_m — TX antenna height AGL at first profile point
  rx_height_m — RX antenna height AGL at last profile point
  freq_hz     — carrier frequency (Hz)

Return value: diffraction path loss correction (dB, positive = extra loss).
A small negative value means diffraction gain (constructive interference).
"""
import math
from typing import Sequence


# ─────────────────────────────────────────────────────────────────────────────
# Fresnel-Kirchhoff helper
# ─────────────────────────────────────────────────────────────────────────────

def _fresnel_v(h_obstacle_m: float, d1_m: float, d2_m: float,
               wavelength_m: float) -> float:
    """
    Fresnel diffraction parameter v for a single knife-edge obstacle.

    h_obstacle_m: clearance height — positive if obstacle is ABOVE the LOS
                  line, negative if below (open sky).
    d1_m, d2_m:  distance from TX to obstacle and obstacle to RX (m).
    Returns v (dimensionless).
    """
    if d1_m <= 0 or d2_m <= 0 or wavelength_m <= 0:
        return 0.0
    return h_obstacle_m * math.sqrt(2.0 * (d1_m + d2_m) /
                                     (wavelength_m * d1_m * d2_m))


def _knife_edge_loss_db(v: float) -> float:
    """
    Knife-edge diffraction loss (dB) — ITU-R P.526-15 approximation.
    Returns positive dB values (additional path loss beyond free space).

      v < -0.7 → 0 dB  (full Fresnel clearance — no extra loss)
      v = 0    → ~6 dB  (obstacle exactly on LOS)
      v > 0    → increasing loss
    """
    if v < -0.7:
        return 0.0
    # ITU-R P.526-15 formula: J(v) = 6.9 + 20·log10(√((v-0.1)²+1) + v - 0.1)
    inner = math.sqrt((v - 0.1) ** 2 + 1.0) + v - 0.1
    if inner <= 0:
        return 0.0
    return max(0.0, 6.9 + 20.0 * math.log10(inner))


def _los_height_at(d: float, d_total: float,
                   h_tx_asl: float, h_rx_asl: float) -> float:
    """Linear interpolation of the LOS height at distance d."""
    if d_total <= 0:
        return h_tx_asl
    return h_tx_asl + (h_rx_asl - h_tx_asl) * d / d_total


def _clearances(elev: Sequence[float], dist: Sequence[float],
                tx_height_m: float, rx_height_m: float
                ) -> list[tuple[float, float]]:
    """
    For each interior profile point, compute (clearance_m, distance_m)
    where clearance_m is how far the terrain sticks above the TX→RX LOS line
    (positive = above LOS = obstacle).
    """
    n = len(elev)
    if n < 3:
        return []

    h_tx_asl = elev[0] + tx_height_m
    h_rx_asl = elev[-1] + rx_height_m
    d_total = dist[-1] - dist[0]

    result: list[tuple[float, float]] = []
    for i in range(1, n - 1):
        d = dist[i] - dist[0]
        los_h = _los_height_at(d, d_total, h_tx_asl, h_rx_asl)
        clearance = elev[i] - los_h          # positive → obstacle above LOS
        result.append((clearance, dist[i] - dist[0]))
    return result


# ─────────────────────────────────────────────────────────────────────────────
# Public diffraction models
# ─────────────────────────────────────────────────────────────────────────────

def single_knife_edge_db(elevations: Sequence[float],
                          distances_m: Sequence[float],
                          tx_height_m: float,
                          rx_height_m: float,
                          freq_hz: float) -> float:
    """
    Single Knife Edge — Huygens geometric formula.
    Finds the single most obstructing terrain point and applies knife-edge loss.
    Optimistic: ignores all other obstacles.
    """
    n = len(elevations)
    if n < 3 or freq_hz <= 0:
        return 0.0

    wl = 3e8 / freq_hz
    d_total = distances_m[-1] - distances_m[0]
    clearances = _clearances(elevations, distances_m, tx_height_m, rx_height_m)
    if not clearances:
        return 0.0

    # Worst (highest) obstacle
    best_h, best_d = max(clearances, key=lambda x: x[0])
    if best_h <= 0:
        return 0.0   # clear LOS — no diffraction

    d1 = best_d
    d2 = d_total - best_d
    if d1 <= 0 or d2 <= 0:
        return 0.0

    v = _fresnel_v(best_h, d1, d2, wl)
    return max(0.0, _knife_edge_loss_db(v))


def epstein_peterson_db(elevations: Sequence[float],
                         distances_m: Sequence[float],
                         tx_height_m: float,
                         rx_height_m: float,
                         freq_hz: float) -> float:
    """
    Epstein-Peterson (1953) — sequential multi-obstacle model.
    Finds all obstacles that break LOS and sums their individual knife-edge
    losses.  Can be conservative (over-estimates loss) because it ignores
    inter-obstacle coupling.
    """
    n = len(elevations)
    if n < 3 or freq_hz <= 0:
        return 0.0

    wl = 3e8 / freq_hz
    d_total = distances_m[-1] - distances_m[0]
    h_tx_asl = elevations[0] + tx_height_m
    h_rx_asl = elevations[-1] + rx_height_m

    # Identify all above-LOS obstacles
    clearances = _clearances(elevations, distances_m, tx_height_m, rx_height_m)
    obstacles = [(h, d) for h, d in clearances if h > 0]
    if not obstacles:
        return 0.0

    total_loss = 0.0
    for (h, d) in obstacles:
        d1 = d
        d2 = d_total - d
        if d1 <= 0 or d2 <= 0:
            continue
        v = _fresnel_v(h, d1, d2, wl)
        total_loss += max(0.0, _knife_edge_loss_db(v))

    return total_loss


def bullington_db(elevations: Sequence[float],
                   distances_m: Sequence[float],
                   tx_height_m: float,
                   rx_height_m: float,
                   freq_hz: float) -> float:
    """
    Bullington (1977) — fast multi-obstacle model.
    Constructs an equivalent single knife edge by finding the intersection
    of the TX and RX horizon lines, then applies a single knife-edge loss.
    Trade-off between accuracy and speed.  Recommended for VHF/UHF sub-GHz.
    """
    n = len(elevations)
    if n < 3 or freq_hz <= 0:
        return 0.0

    wl = 3e8 / freq_hz
    d_total = distances_m[-1] - distances_m[0]
    h_tx_asl = elevations[0] + tx_height_m
    h_rx_asl = elevations[-1] + rx_height_m

    # Find TX horizon: farthest point along radial that TX can see
    # (maximum positive slope from TX over the terrain)
    max_slope_tx = float('-inf')
    best_d_tx = 0.0
    best_h_at_tx = h_tx_asl
    for i in range(1, n - 1):
        d = distances_m[i] - distances_m[0]
        if d <= 0:
            continue
        slope = (elevations[i] - h_tx_asl) / d
        if slope > max_slope_tx:
            max_slope_tx = slope
            best_d_tx = d
            best_h_at_tx = elevations[i]

    # Find RX horizon: farthest point looking back from RX
    max_slope_rx = float('-inf')
    best_d_rx = d_total
    best_h_at_rx = h_rx_asl
    for i in range(1, n - 1):
        d = d_total - (distances_m[i] - distances_m[0])
        if d <= 0:
            continue
        slope = (elevations[i] - h_rx_asl) / d
        if slope > max_slope_rx:
            max_slope_rx = slope
            best_d_rx = distances_m[i] - distances_m[0]
            best_h_at_rx = elevations[i]

    # Equivalent Bullington edge: intersection of the two horizon lines
    # Line from TX: height = h_tx_asl + max_slope_tx * d
    # Line from RX: height = h_rx_asl + max_slope_rx * (d_total - d)
    # Solve: h_tx_asl + max_slope_tx*d = h_rx_asl + max_slope_rx*(d_total-d)
    denom = max_slope_tx + max_slope_rx
    if abs(denom) < 1e-9:
        # Parallel lines — no intersection, use midpoint
        d_edge = d_total / 2.0
    else:
        d_edge = (h_rx_asl - h_tx_asl + max_slope_rx * d_total) / denom
        d_edge = max(1.0, min(d_total - 1.0, d_edge))

    h_edge = h_tx_asl + max_slope_tx * d_edge
    # Clearance above LOS line
    los_at_edge = _los_height_at(d_edge, d_total, h_tx_asl, h_rx_asl)
    clearance = h_edge - los_at_edge

    if clearance <= 0:
        return 0.0   # paths intersect below LOS → clear

    d1 = d_edge
    d2 = d_total - d_edge
    if d1 <= 0 or d2 <= 0:
        return 0.0

    v = _fresnel_v(clearance, d1, d2, wl)
    return max(0.0, _knife_edge_loss_db(v))


def _deygout_recurse(elevations: Sequence[float],
                      distances_m: Sequence[float],
                      tx_h_asl: float,
                      rx_h_asl: float,
                      freq_hz: float,
                      depth: int = 0) -> float:
    """Recursive Deygout sub-path evaluation."""
    n = len(elevations)
    if n < 3 or depth > 4:   # limit recursion
        return 0.0

    wl = 3e8 / freq_hz
    d_start = distances_m[0]
    d_total = distances_m[-1] - d_start

    # Find dominant obstacle (highest v value)
    best_v = float('-inf')
    best_idx = -1
    for i in range(1, n - 1):
        d = distances_m[i] - d_start
        los_h = _los_height_at(d, d_total, tx_h_asl, rx_h_asl)
        h_clear = elevations[i] - los_h
        d1 = d
        d2 = d_total - d
        if d1 <= 0 or d2 <= 0:
            continue
        v = _fresnel_v(h_clear, d1, d2, wl)
        if v > best_v:
            best_v = v
            best_idx = i

    if best_idx < 0 or best_v <= 0:
        return 0.0   # no significant obstacle

    # Diffraction loss at dominant obstacle
    loss = max(0.0, _knife_edge_loss_db(best_v))

    # Recurse on left sub-path (TX → dominant obstacle)
    left_elev = elevations[:best_idx + 1]
    left_dist = distances_m[:best_idx + 1]
    loss += _deygout_recurse(left_elev, left_dist,
                              tx_h_asl, elevations[best_idx],
                              freq_hz, depth + 1)

    # Recurse on right sub-path (dominant obstacle → RX)
    right_elev = elevations[best_idx:]
    right_dist = distances_m[best_idx:]
    loss += _deygout_recurse(right_elev, right_dist,
                              elevations[best_idx], rx_h_asl,
                              freq_hz, depth + 1)

    return loss


def deygout_db(elevations: Sequence[float],
                distances_m: Sequence[float],
                tx_height_m: float,
                rx_height_m: float,
                freq_hz: float) -> float:
    """
    Deygout (1994) — advanced multi-obstacle model with priority logic.
    Identifies the most prominent obstacle, applies knife-edge loss, then
    recursively processes each sub-path.  Most accurate of the five models
    and the CloudRF default.
    """
    n = len(elevations)
    if n < 3 or freq_hz <= 0:
        return 0.0

    h_tx_asl = elevations[0] + tx_height_m
    h_rx_asl = elevations[-1] + rx_height_m
    return _deygout_recurse(
        elevations, distances_m, h_tx_asl, h_rx_asl, freq_hz, depth=0
    )


def giovanelli_db(elevations: Sequence[float],
                   distances_m: Sequence[float],
                   tx_height_m: float,
                   rx_height_m: float,
                   freq_hz: float) -> float:
    """
    Giovanelli (1984) — multi-obstacle model with additional combining factor.
    Processes obstacles sequentially (like Epstein-Peterson) but applies a
    Bullington-style correction term that accounts for coupling between
    adjacent obstacles, giving better accuracy than simple summation.
    """
    n = len(elevations)
    if n < 3 or freq_hz <= 0:
        return 0.0

    # Sequential loss (Epstein-Peterson base)
    ep_loss = epstein_peterson_db(
        elevations, distances_m, tx_height_m, rx_height_m, freq_hz
    )
    if ep_loss <= 0:
        return 0.0

    # Bullington equivalent for the full path
    bull_loss = bullington_db(
        elevations, distances_m, tx_height_m, rx_height_m, freq_hz
    )

    # Giovanelli combining: take max then blend
    # The combining factor J accounts for obstacle interaction
    # J ≈ 0.1 to 0.4 depending on terrain roughness
    J = 0.2
    combined = bull_loss + J * (ep_loss - bull_loss)
    return max(bull_loss, combined)


# ─────────────────────────────────────────────────────────────────────────────
# Dispatch function
# ─────────────────────────────────────────────────────────────────────────────

DIFFRACTION_MODELS = {
    "single_knife_edge": single_knife_edge_db,
    "epstein_peterson":  epstein_peterson_db,
    "bullington":        bullington_db,
    "giovanelli":        giovanelli_db,
    "deygout":           deygout_db,
}


def compute_diffraction_db(elevations: Sequence[float],
                            distances_m: Sequence[float],
                            tx_height_m: float,
                            rx_height_m: float,
                            freq_hz: float,
                            model: str = "deygout") -> float:
    """
    Compute terrain diffraction loss for a given profile and model.
    Returns dB (positive = additional attenuation).
    """
    # Rust fast path (Track D, D4) — the whole module is scalar per-point math on
    # the per-pixel coverage path; the Python functions below are the fallback +
    # parity ground truth (see test_native_parity).
    from app.core import native
    if native.HAS_NATIVE:
        try:
            return native.diffraction_db(model, elevations, distances_m,
                                         tx_height_m, rx_height_m, freq_hz)
        except Exception:
            pass
    fn = DIFFRACTION_MODELS.get(model, deygout_db)
    try:
        return fn(elevations, distances_m, tx_height_m, rx_height_m, freq_hz)
    except Exception:
        return 0.0
