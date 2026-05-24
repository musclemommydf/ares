# SPDX-License-Identifier: MIT OR Apache-2.0
# Copyright (c) 2026 Ares

"""
Polar (azimuth-plane) radiation patterns.

Replaces the historical "beam_width_deg" hard-cutoff with named, physically
plausible patterns that have smooth roll-off and (where appropriate) side
and back lobes — closer to what a real antenna actually radiates.

A pattern is described by one of two analytic forms:

  * 'card' — cardioid family.  Linear amplitude  R(θ) = |a + b·cos θ|
            normalised so the boresight value is 0 dB.  Covers omni
            (a=1, b=0) through dipole / figure-8 (a=0, b=1).

  * 'lobes' — sum of Gaussian lobes (max-combined).  Each lobe is
              specified as (peak_dB, center_deg, hpbw_deg) where hpbw_deg
              is the full -3 dB width of *that* lobe.  Multiple lobes let
              us model a main lobe + back lobe (Yagi, sector) and a main
              lobe + first side lobe + back lobe (parabolic dish).

All gains returned are *relative* (peak = 0 dB).  The simulator multiplies
this by the user's peak gain in dBi to get the absolute gain in a given
direction, which is then added to the link budget — no hard cutoffs.

The function `compute_pattern_beamwidths` numerically derives the -3 dB
and -6 dB beamwidths by sampling the pattern; this is what the UI shows
in place of the old `beam_width_deg` input.
"""
import math
from typing import Optional

# Floor for very deep nulls — keeps log10 finite and reflects the fact
# that real antennas don't have -∞ nulls (-40 dB is already deeper than
# most real-world measurements).
_FLOOR_DB = -40.0


# ─────────────────────────────────────────────────────────────────────────────
# Pattern catalogue
# ─────────────────────────────────────────────────────────────────────────────
#
# kind='card':  R(θ) = |a + b·cos θ|, normalised to the boresight value.
# kind='lobes': max over Gaussian lobes (peak_dB, center_deg, hpbw_deg).
#               hpbw_deg is the full -3 dB width of that individual lobe.

POLAR_PATTERNS = {
    # ── Cardioid family (smooth analytic shapes, no side lobes) ─────────
    "omni": {
        "kind": "card", "a": 1.0, "b": 0.0,
        "label": "Omnidirectional",
        "category": "Omni",
        "description": "Uniform 360° coverage. No directionality.",
    },
    "subcardioid": {
        "kind": "card", "a": 0.7, "b": 0.3,
        "label": "Sub-cardioid",
        "category": "Cardioid family",
        "description": "Wide front lobe, gentle rear attenuation; no deep null.",
    },
    "cardioid": {
        "kind": "card", "a": 0.5, "b": 0.5,
        "label": "Cardioid",
        "category": "Cardioid family",
        "description": "Heart-shaped pattern with deep null directly behind boresight.",
    },
    "supercardioid": {
        "kind": "card", "a": 0.37, "b": 0.63,
        "label": "Super-cardioid",
        "category": "Cardioid family",
        "description": "Tighter forward beam than cardioid; small rear lobe, deepest rejection at ~127°.",
    },
    "hypercardioid": {
        "kind": "card", "a": 0.25, "b": 0.75,
        "label": "Hyper-cardioid",
        "category": "Cardioid family",
        "description": "Even tighter front beam; larger rear lobe (~-6 dB), max rejection at ~109°.",
    },
    "figure_8": {
        "kind": "card", "a": 0.0, "b": 1.0,
        "label": "Figure-8 / Bidirectional",
        "category": "Cardioid family",
        "description": "Equal main and rear lobes, deep nulls at ±90° (free-space dipole).",
    },

    # ── Sector / panel antennas (cellular base-station style) ──────────
    "sector_60": {
        "kind": "lobes",
        "lobes": [(0.0, 0.0, 60.0), (-25.0, 180.0, 120.0)],
        "label": "Sector 60°",
        "category": "Sector",
        "description": "60° HPBW main lobe with ~25 dB front-to-back ratio.",
    },
    "sector_90": {
        "kind": "lobes",
        "lobes": [(0.0, 0.0, 90.0), (-25.0, 180.0, 150.0)],
        "label": "Sector 90°",
        "category": "Sector",
        "description": "90° HPBW main lobe with ~25 dB front-to-back ratio.",
    },
    "sector_120": {
        "kind": "lobes",
        "lobes": [(0.0, 0.0, 120.0), (-22.0, 180.0, 180.0)],
        "label": "Sector 120°",
        "category": "Sector",
        "description": "120° HPBW main lobe — typical 3-sector cellular cell.",
    },

    # ── Yagi-Uda (HPBW narrows with element count, ~25 dB F/B) ────────
    "yagi_3": {
        "kind": "lobes",
        "lobes": [(0.0, 0.0, 46.0), (-18.0, 180.0, 60.0)],
        "label": "Yagi 3-element",
        "category": "Directional",
        "description": "Compact Yagi, ~46° HPBW, modest rear lobe.",
    },
    "yagi_5": {
        "kind": "lobes",
        "lobes": [(0.0, 0.0, 36.0), (-20.0, 180.0, 55.0)],
        "label": "Yagi 5-element",
        "category": "Directional",
        "description": "Common Yagi, ~36° HPBW.",
    },
    "yagi_9": {
        "kind": "lobes",
        "lobes": [(0.0, 0.0, 27.0), (-22.0, 180.0, 50.0)],
        "label": "Yagi 9-element",
        "category": "Directional",
        "description": "High-gain Yagi, ~27° HPBW.",
    },
    "yagi_15": {
        "kind": "lobes",
        "lobes": [(0.0, 0.0, 21.0), (-25.0, 180.0, 45.0)],
        "label": "Yagi 15-element",
        "category": "Directional",
        "description": "Long-boom Yagi, ~21° HPBW.",
    },
    "log_periodic": {
        "kind": "lobes",
        "lobes": [(0.0, 0.0, 60.0), (-20.0, 180.0, 120.0)],
        "label": "Log-Periodic",
        "category": "Directional",
        "description": "Broadband directional, ~60° HPBW across decade bandwidth.",
    },

    # ── Aperture antennas ──────────────────────────────────────────────
    "horn": {
        "kind": "lobes",
        "lobes": [(0.0, 0.0, 30.0), (-25.0, 180.0, 50.0)],
        "label": "Horn",
        "category": "Aperture",
        "description": "Pyramidal horn, ~30° HPBW, controlled side lobes.",
    },
    "parabolic_narrow": {
        "kind": "lobes",
        # Main + first side lobe (~12° from boresight, -22 dB) + back
        "lobes": [(0.0, 0.0, 5.0), (-22.0, 12.0, 8.0), (-22.0, -12.0, 8.0),
                  (-30.0, 180.0, 60.0)],
        "label": "Parabolic dish (narrow)",
        "category": "Aperture",
        "description": "Highly directional, ~5° HPBW, first side lobe at ±12°.",
    },
    "parabolic_medium": {
        "kind": "lobes",
        "lobes": [(0.0, 0.0, 12.0), (-22.0, 25.0, 18.0), (-22.0, -25.0, 18.0),
                  (-30.0, 180.0, 60.0)],
        "label": "Parabolic dish (medium)",
        "category": "Aperture",
        "description": "Mid-size dish, ~12° HPBW, first side lobe at ±25°.",
    },

    # ── Planar / circular ──────────────────────────────────────────────
    "patch": {
        "kind": "lobes",
        "lobes": [(0.0, 0.0, 80.0), (-20.0, 180.0, 200.0)],
        "label": "Patch / Microstrip",
        "category": "Planar",
        "description": "Hemispherical patch, ~80° HPBW, low rear radiation.",
    },
    "helical": {
        "kind": "lobes",
        "lobes": [(0.0, 0.0, 52.0), (-15.0, 180.0, 80.0)],
        "label": "Helical (axial mode)",
        "category": "Circular pol",
        "description": "Axial-mode helix, ~52° HPBW, circularly polarised.",
    },

    # ── Phased / steered ───────────────────────────────────────────────
    "phased_array": {
        "kind": "lobes",
        "lobes": [(0.0, 0.0, 12.0), (-18.0, 30.0, 25.0), (-18.0, -30.0, 25.0),
                  (-25.0, 180.0, 80.0)],
        "label": "Phased array",
        "category": "Array",
        "description": "Steered array, ~12° HPBW, grating-lobe-like side lobes.",
    },

    # ── Marine / surface-search radar arrays (slotted waveguide) ───────
    "marine_radar_fan": {
        "kind": "lobes",
        # slotted-waveguide marine-radar open array: very narrow horizontal
        # fan beam, fan-shaped in elevation, ~-26 dB first side lobes, low back
        "lobes": [(0.0, 0.0, 1.9), (-26.0, 5.0, 3.0), (-26.0, -5.0, 3.0),
                  (-32.0, 10.0, 4.0), (-32.0, -10.0, 4.0), (-35.0, 180.0, 90.0)],
        "label": "Marine radar fan beam (open array)",
        "category": "Radar",
        "description": "Slotted-waveguide marine-radar open array — ~2° horizontal beamwidth, fan-shaped in elevation (~20–25° VBW), ~-26 dB first side lobes.",
    },
    "marine_radar_fan_wide": {
        "kind": "lobes",
        "lobes": [(0.0, 0.0, 5.2), (-24.0, 12.0, 6.0), (-24.0, -12.0, 6.0),
                  (-32.0, 180.0, 90.0)],
        "label": "Marine radar fan beam (compact / radome)",
        "category": "Radar",
        "description": "Compact marine-radar array or radome antenna — ~5° horizontal beamwidth, fan-shaped in elevation.",
    },
}


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────

def list_polar_patterns() -> list[dict]:
    """Return the catalogue with derived -3 / -6 dB beamwidths for the UI."""
    out = []
    for pid, meta in POLAR_PATTERNS.items():
        h3, h6 = compute_pattern_beamwidths(pid)
        out.append({
            "id": pid,
            "label": meta["label"],
            "category": meta.get("category", ""),
            "description": meta.get("description", ""),
            "hpbw_3db_deg": h3,
            "hpbw_6db_deg": h6,
        })
    return out


def polar_pattern_gain_db(pattern_id: str, az_offset_deg: float) -> float:
    """
    Relative gain (dB, peak = 0) of `pattern_id` at azimuth offset
    `az_offset_deg` from boresight.  Floored at -40 dB.
    """
    p = POLAR_PATTERNS.get(pattern_id)
    if p is None:
        return 0.0  # unknown → omni

    # Wrap to (-180, 180]
    th = ((az_offset_deg + 180.0) % 360.0) - 180.0

    if p["kind"] == "card":
        a, b = p["a"], p["b"]
        peak_amp = abs(a + b)  # value at θ=0
        if peak_amp <= 1e-12:
            return _FLOOR_DB
        amp = abs(a + b * math.cos(math.radians(th)))
        if amp <= 1e-12:
            return _FLOOR_DB
        return max(_FLOOR_DB, 20.0 * math.log10(amp / peak_amp))

    # 'lobes' — max-combine Gaussian lobes
    g = _FLOOR_DB
    for peak_db, center_deg, hpbw_deg in p["lobes"]:
        if hpbw_deg <= 0:
            continue
        delta = abs(th - center_deg)
        if delta > 180.0:
            delta = 360.0 - delta  # shortest angular distance
        # Gaussian where -3 dB at delta = hpbw/2 ⇒ G = peak - 12·(delta/hpbw)²
        contribution = peak_db - 12.0 * (delta / hpbw_deg) ** 2
        if contribution > g:
            g = contribution
    return max(_FLOOR_DB, g)


def compute_pattern_beamwidths(
    pattern_id: str,
) -> tuple[Optional[float], Optional[float]]:
    """
    Numerically derive the -3 dB and -6 dB full beamwidths (degrees) of
    `pattern_id` by sampling the azimuth response in 0.5° steps.
    Returns (hpbw_3db, hpbw_6db); either may be None for omni patterns
    that never drop to that threshold.
    """
    p = POLAR_PATTERNS.get(pattern_id)
    if p is None:
        return (None, None)

    samples = []
    deg = 0.0
    while deg <= 180.0 + 1e-6:
        samples.append((deg, polar_pattern_gain_db(pattern_id, deg)))
        deg += 0.5

    h3 = _first_crossing_deg(samples, -3.0)
    h6 = _first_crossing_deg(samples, -6.0)
    return (None if h3 is None else round(h3 * 2.0, 1),
            None if h6 is None else round(h6 * 2.0, 1))


def _first_crossing_deg(samples: list[tuple[float, float]],
                        threshold_db: float) -> Optional[float]:
    """First azimuth offset where gain falls to/below `threshold_db`."""
    prev_deg, prev_g = samples[0]
    if prev_g <= threshold_db:
        return prev_deg
    for deg, g in samples[1:]:
        if g <= threshold_db:
            if prev_g != g:
                frac = (prev_g - threshold_db) / (prev_g - g)
                return prev_deg + frac * (deg - prev_deg)
            return deg
        prev_deg, prev_g = deg, g
    return None  # never crosses (omnidirectional)


# Heuristic mapping: existing AntennaType → polar pattern id (used to
# auto-fill a pattern when a device preset only specifies antenna_type).
ANTENNA_TYPE_TO_POLAR_PATTERN = {
    "isotropic": "omni",
    "omnidirectional": "omni",
    "omni_5dbi": "omni",
    "omni_9dbi": "omni",
    "dipole_half_wave": "omni",
    "dipole_full_wave": "omni",
    "dipole_quarter_wave": "omni",
    "whip_quarter_wave": "omni",
    "ground_plane": "omni",
    "collinear_2el": "omni",
    "collinear_4el": "omni",
    "loop": "figure_8",
    "crossed_dipole": "omni",
    "yagi_3el": "yagi_3",
    "yagi_5el": "yagi_5",
    "yagi_9el": "yagi_9",
    "yagi_15el": "yagi_15",
    "log_periodic": "log_periodic",
    "sector_60": "sector_60",
    "sector_90": "sector_90",
    "sector_120": "sector_120",
    "patch": "patch",
    "horn": "horn",
    "parabolic_dish": "parabolic_medium",
    "helical": "helical",
    "phased_array": "phased_array",
    "custom": "omni",
}
