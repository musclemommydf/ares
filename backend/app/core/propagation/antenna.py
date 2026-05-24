# SPDX-License-Identifier: MIT OR Apache-2.0
# Copyright (c) 2026 Ares

"""
Antenna Pattern Models
Provides gain patterns (dBi) for common antenna types as a function of
elevation and azimuth angles. Supports custom NEC/HFSS pattern import.

Patterns are stored as (elevation_deg, azimuth_deg) → gain_dBi functions.
"""
import math
import json
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional, Callable
import numpy as np

from app.config import settings


class AntennaType(str, Enum):
    ISOTROPIC = "isotropic"
    DIPOLE_HALF_WAVE = "dipole_half_wave"
    DIPOLE_QUARTER_WAVE = "dipole_quarter_wave"      # monopole over ground
    DIPOLE_FULL_WAVE = "dipole_full_wave"
    YAGI_3EL = "yagi_3el"
    YAGI_5EL = "yagi_5el"
    YAGI_9EL = "yagi_9el"
    YAGI_15EL = "yagi_15el"
    LOG_PERIODIC = "log_periodic"
    PARABOLIC_DISH = "parabolic_dish"
    HORN = "horn"
    PATCH = "patch"
    SECTOR_60 = "sector_60"
    SECTOR_90 = "sector_90"
    SECTOR_120 = "sector_120"
    OMNI_5DBI = "omni_5dbi"
    OMNI_9DBI = "omni_9dbi"
    OMNIDIRECTIONAL = "omnidirectional"
    PHASED_ARRAY = "phased_array"
    WHIP_QUARTER_WAVE = "whip_quarter_wave"
    LOOP = "loop"
    HELICAL = "helical"
    CROSSED_DIPOLE = "crossed_dipole"             # circular polarisation
    COLLINEAR_2EL = "collinear_2el"
    COLLINEAR_4EL = "collinear_4el"
    GROUND_PLANE = "ground_plane"
    CUSTOM = "custom"


@dataclass
class AntennaConfig:
    """Complete antenna configuration."""
    type: AntennaType = AntennaType.DIPOLE_HALF_WAVE
    gain_dbi: Optional[float] = None          # override calculated gain
    tilt_deg: float = 0.0                      # electrical downtilt (degrees)
    azimuth_deg: float = 0.0                   # antenna boresight azimuth
    height_m: float = settings.default_emitter_agl_m  # height above ground - 6ft AGL default
    # For dish/horn
    diameter_m: float = 1.2                    # dish diameter
    efficiency: float = 0.55                   # aperture efficiency
    # For phased array
    array_elements: int = 64
    # For Yagi
    elements: int = 9
    # Custom pattern
    custom_pattern_json: Optional[str] = None  # JSON with azimuth/elevation tables
    # Polarization
    polarization: str = "vertical"             # "vertical", "horizontal", "circular"
    frequency_hz: float = 433e6               # for electrically scaled patterns


def get_antenna_gain_dbi(ant: AntennaConfig,
                          elevation_deg: float = 0.0,
                          azimuth_offset_deg: float = 0.0) -> float:
    """
    Return antenna gain (dBi) in direction (elevation, azimuth_offset from boresight).
    elevation_deg: angle above horizon (+ = up, - = down)
    azimuth_offset_deg: angle from antenna boresight
    """
    t = ant.type

    if ant.gain_dbi is not None and t not in (AntennaType.DIPOLE_HALF_WAVE,
                                               AntennaType.PARABOLIC_DISH,
                                               AntennaType.HORN,
                                               AntennaType.PHASED_ARRAY):
        return ant.gain_dbi

    if t == AntennaType.ISOTROPIC:
        return 0.0

    elif t in (AntennaType.DIPOLE_HALF_WAVE, AntennaType.DIPOLE_FULL_WAVE,
               AntennaType.CROSSED_DIPOLE):
        return _dipole_pattern(elevation_deg, azimuth_offset_deg, ant.tilt_deg, t)

    elif t == AntennaType.DIPOLE_QUARTER_WAVE or t == AntennaType.WHIP_QUARTER_WAVE:
        return _monopole_pattern(elevation_deg, azimuth_offset_deg, ant.tilt_deg)

    elif t == AntennaType.GROUND_PLANE:
        return _monopole_pattern(elevation_deg, azimuth_offset_deg, ant.tilt_deg)

    elif t in (AntennaType.YAGI_3EL, AntennaType.YAGI_5EL,
               AntennaType.YAGI_9EL, AntennaType.YAGI_15EL):
        n_el = {"yagi_3el": 3, "yagi_5el": 5, "yagi_9el": 9, "yagi_15el": 15}[t.value]
        return _yagi_pattern(elevation_deg, azimuth_offset_deg, ant.tilt_deg, n_el)

    elif t == AntennaType.LOG_PERIODIC:
        return _lpda_pattern(elevation_deg, azimuth_offset_deg, ant.tilt_deg)

    elif t == AntennaType.PARABOLIC_DISH:
        return _parabolic_dish_gain(ant.diameter_m, ant.frequency_hz, ant.efficiency,
                                    elevation_deg, azimuth_offset_deg, ant.tilt_deg)

    elif t == AntennaType.HORN:
        return _horn_gain(ant.diameter_m, ant.frequency_hz,
                          elevation_deg, azimuth_offset_deg)

    elif t in (AntennaType.SECTOR_60, AntennaType.SECTOR_90, AntennaType.SECTOR_120):
        bw = {"sector_60": 60, "sector_90": 90, "sector_120": 120}[t.value]
        return _sector_pattern(elevation_deg, azimuth_offset_deg, ant.tilt_deg, bw)

    elif t == AntennaType.OMNIDIRECTIONAL:
        return _omni_pattern(elevation_deg, 0.0, ant.tilt_deg)

    elif t == AntennaType.OMNI_5DBI:
        return _omni_pattern(elevation_deg, 0.0, ant.tilt_deg, peak_gain=5.0)

    elif t == AntennaType.OMNI_9DBI:
        return _omni_pattern(elevation_deg, 0.0, ant.tilt_deg, peak_gain=9.0)

    elif t in (AntennaType.COLLINEAR_2EL, AntennaType.COLLINEAR_4EL):
        n = 2 if t == AntennaType.COLLINEAR_2EL else 4
        return _collinear_pattern(elevation_deg, 0.0, ant.tilt_deg, n)

    elif t == AntennaType.PATCH:
        return _patch_pattern(elevation_deg, azimuth_offset_deg, ant.tilt_deg)

    elif t == AntennaType.HELICAL:
        return _helical_pattern(elevation_deg, azimuth_offset_deg, ant.tilt_deg)

    elif t == AntennaType.LOOP:
        return _loop_pattern(elevation_deg, azimuth_offset_deg)

    elif t == AntennaType.PHASED_ARRAY:
        return _phased_array_gain(ant.array_elements, elevation_deg,
                                   azimuth_offset_deg, ant.tilt_deg)

    elif t == AntennaType.CUSTOM:
        if ant.custom_pattern_json:
            return _custom_pattern(ant.custom_pattern_json,
                                    elevation_deg, azimuth_offset_deg)
        return 0.0

    return 0.0


# ─────────────────────────────────────────────────────────────────────────────
# Pattern implementations
# ─────────────────────────────────────────────────────────────────────────────

def _dipole_pattern(el_deg: float, az_deg: float, tilt: float, atype) -> float:
    """Half-wave dipole. Peak 2.15 dBi in broadside plane, null on axis."""
    # Dipole axis is vertical; pattern is sin(θ) in elevation
    theta = math.radians(90.0 - el_deg - tilt)   # angle from dipole axis
    sin_theta = abs(math.sin(theta))
    if sin_theta < 1e-10:
        return -40.0  # null
    if atype == AntennaType.DIPOLE_HALF_WAVE:
        # F(θ) = cos(π/2·cos(θ)) / sin(θ)
        cos_term = math.cos(math.pi / 2.0 * math.cos(theta))
        F = cos_term / sin_theta
    else:  # full-wave
        cos_term = math.cos(math.pi * math.cos(theta))
        F = (1.0 + cos_term) / (2.0 * sin_theta + 1e-10)
    return max(-40.0, 2.15 + 20.0 * math.log10(max(abs(F), 1e-10) / 1.0))


def _monopole_pattern(el_deg: float, az_deg: float, tilt: float) -> float:
    """Quarter-wave monopole over perfect ground. Peak ≈ 5.19 dBi."""
    theta = math.radians(90.0 - el_deg - tilt)
    sin_theta = abs(math.sin(theta))
    if el_deg < 0 or sin_theta < 1e-10:
        return -40.0  # below ground
    cos_term = math.cos(math.pi / 2.0 * math.cos(theta))
    F = cos_term / max(sin_theta, 1e-10)
    return max(-40.0, 5.19 + 20.0 * math.log10(max(abs(F), 1e-10)))


def _yagi_pattern(el_deg: float, az_deg: float, tilt: float, n_elements: int) -> float:
    """
    Simplified Yagi-Uda pattern based on number of elements.
    Peak gain approx: 3el≈8dBi, 5el≈10dBi, 9el≈14dBi, 15el≈17dBi
    """
    gains = {3: 8.0, 5: 10.0, 9: 14.0, 15: 17.0}
    peak = gains.get(n_elements, 10.0 + 3.0 * math.log2(n_elements))

    # Beamwidth narrows with more elements
    hpbw_az = 80.0 / math.sqrt(n_elements)  # half-power beamwidth (deg)
    hpbw_el = 80.0 / math.sqrt(n_elements)

    az_norm = az_deg / hpbw_az
    el_norm = (el_deg - tilt) / hpbw_el

    # Gaussian beam approximation
    gain = peak - 12.0 * az_norm ** 2 - 12.0 * el_norm ** 2

    # Front-to-back ratio ~25 dB for Yagi
    if abs(az_deg) > 90:
        gain -= 25.0

    return max(-30.0, gain)


def _lpda_pattern(el_deg: float, az_deg: float, tilt: float) -> float:
    """Log-Periodic Dipole Array. Broadband, ~8–11 dBi."""
    peak = 9.0
    hpbw = 60.0
    az_norm = az_deg / (hpbw / 2)
    el_norm = (el_deg - tilt) / (hpbw / 2)
    gain = peak - 12.0 * (az_norm ** 2 + el_norm ** 2) / 4.0
    if abs(az_deg) > 90:
        gain -= 20.0
    return max(-20.0, gain)


def _parabolic_dish_gain(diameter_m: float, freq_hz: float, efficiency: float,
                          el_deg: float, az_deg: float, tilt: float) -> float:
    """
    Parabolic dish gain and pattern.
    Peak gain: G = η (πD/λ)²
    """
    lam = 3e8 / freq_hz
    if lam <= 0:
        return 0.0
    G_linear = efficiency * (math.pi * diameter_m / lam) ** 2
    G_peak_dbi = 10.0 * math.log10(max(1.0, G_linear))

    # Half-power beamwidth (deg)
    hpbw = 70.0 * lam / diameter_m   # degrees

    # Off-axis angle
    theta = math.sqrt(az_deg ** 2 + (el_deg - tilt) ** 2)

    if theta < hpbw / 2:
        return G_peak_dbi
    elif theta < 20 * lam / diameter_m * 180 / math.pi:
        # First side lobe region
        return G_peak_dbi - 25.0 * (theta / (hpbw / 2)) ** 1.5
    else:
        # Far sidelobe: ITU-R S.580 limit
        return max(-10.0, 32.0 - 25.0 * math.log10(max(1.0, theta)))


def _horn_gain(aperture_m: float, freq_hz: float,
               el_deg: float, az_deg: float) -> float:
    """Pyramidal horn antenna gain and pattern."""
    lam = 3e8 / freq_hz
    G_peak = 10.0 * math.log10(max(1.0, 5.0 * (aperture_m / lam) ** 2))
    hpbw = 55.0 * lam / aperture_m
    theta = math.sqrt(az_deg ** 2 + el_deg ** 2)
    gain = G_peak - 12.0 * (theta / hpbw) ** 2
    return max(-20.0, gain)


def _sector_pattern(el_deg: float, az_deg: float, tilt: float, beamwidth: int) -> float:
    """
    Sector panel antenna (base station).
    Typical gain: 60°→18dBi, 90°→16dBi, 120°→14dBi
    """
    gains = {60: 18.0, 90: 16.0, 120: 14.0}
    peak = gains.get(beamwidth, 15.0)

    # Horizontal pattern: cosine approximation within beamwidth
    az_half = beamwidth / 2.0
    if abs(az_deg) <= az_half:
        az_gain = 0.0  # within beamwidth
    elif abs(az_deg) <= 180:
        az_gain = -min(25.0, 12.0 * ((abs(az_deg) - az_half) / az_half) ** 2)
    else:
        az_gain = -25.0  # back lobe

    # Vertical pattern: 7° electrical half-power beamwidth
    el_hpbw = 7.0
    el_norm = (el_deg - tilt) / el_hpbw
    el_gain = -12.0 * el_norm ** 2

    return max(-30.0, peak + az_gain + el_gain)


def _omni_pattern(el_deg: float, az_deg: float, tilt: float,
                   peak_gain: float = 2.15) -> float:
    """Omnidirectional antenna (elevated dipole collinear)."""
    el_hpbw = max(5.0, 40.0 / peak_gain)
    el_norm = (el_deg - tilt) / el_hpbw
    gain = peak_gain - 12.0 * el_norm ** 2
    return max(-20.0, gain)


def _collinear_pattern(el_deg: float, az_deg: float,
                        tilt: float, n_elements: int) -> float:
    """Collinear array. Gain ≈ 2el:5dBi, 4el:8dBi"""
    peak = 2.15 + 10.0 * math.log10(n_elements)
    el_hpbw = 40.0 / n_elements
    el_norm = (el_deg - tilt) / el_hpbw
    gain = peak - 12.0 * el_norm ** 2
    return max(-20.0, gain)


def _patch_pattern(el_deg: float, az_deg: float, tilt: float) -> float:
    """Microstrip patch antenna. ~7 dBi, hemispherical coverage."""
    peak = 7.0
    # Only radiates in upper hemisphere
    if el_deg < -tilt - 90:
        return -40.0
    theta = math.radians(90 - el_deg - tilt)
    gain = peak + 20.0 * math.log10(max(1e-3, abs(math.cos(theta))))
    return max(-20.0, gain)


def _helical_pattern(el_deg: float, az_deg: float, tilt: float) -> float:
    """Axial-mode helical antenna. ~12 dBi, circular polarization."""
    peak = 12.0
    hpbw = 35.0
    theta = math.sqrt(az_deg ** 2 + (el_deg - tilt) ** 2)
    gain = peak - 12.0 * (theta / hpbw) ** 2
    return max(-20.0, gain)


def _loop_pattern(el_deg: float, az_deg: float) -> float:
    """Small magnetic loop antenna. Toroidal pattern, ~1.76 dBi."""
    # Pattern like a dipole but rotated 90°
    theta = math.radians(el_deg)
    sin_theta = abs(math.cos(theta))  # null on axis, max broadside
    if sin_theta < 1e-10:
        return -40.0
    return max(-30.0, 1.76 + 20.0 * math.log10(sin_theta))


def _phased_array_gain(n_elements: int, el_deg: float,
                        az_deg: float, tilt: float) -> float:
    """
    Phased array (uniform linear/planar array).
    Peak gain ≈ 10*log10(N) + 5 dBi, narrow beam.
    """
    peak = 10.0 * math.log10(n_elements) + 5.0
    n_sqrt = math.sqrt(n_elements)
    hpbw = 50.0 / n_sqrt  # degrees
    theta = math.sqrt(az_deg ** 2 + (el_deg - tilt) ** 2)
    gain = peak - 12.0 * (theta / hpbw) ** 2
    return max(-30.0, gain)


def _custom_pattern(pattern_json: str, el_deg: float, az_deg: float) -> float:
    """
    Load custom pattern from JSON.
    Format: {"azimuth": [0..360], "elevation": [−90..90],
             "gain_dbi": [[az0_el0, az0_el1, ...], [az1_el0, ...]]}
    Supports NEC-2 output and HFSS far-field export.
    """
    try:
        data = json.loads(pattern_json)
        az_arr = np.array(data["azimuth"])
        el_arr = np.array(data["elevation"])
        gain_arr = np.array(data["gain_dbi"])

        # Nearest-neighbour interpolation (upgrade to bilinear if needed)
        az_idx = np.argmin(np.abs(az_arr - (az_deg % 360)))
        el_idx = np.argmin(np.abs(el_arr - el_deg))
        return float(gain_arr[az_idx, el_idx])
    except Exception:
        return 0.0


# ─────────────────────────────────────────────────────────────────────────────
# Beamwidth helper
# ─────────────────────────────────────────────────────────────────────────────

# Antenna types that are omnidirectional in azimuth
_OMNI_TYPES = {
    AntennaType.ISOTROPIC, AntennaType.OMNIDIRECTIONAL, AntennaType.OMNI_5DBI,
    AntennaType.OMNI_9DBI, AntennaType.DIPOLE_HALF_WAVE, AntennaType.DIPOLE_FULL_WAVE,
    AntennaType.DIPOLE_QUARTER_WAVE, AntennaType.WHIP_QUARTER_WAVE,
    AntennaType.CROSSED_DIPOLE, AntennaType.COLLINEAR_2EL, AntennaType.COLLINEAR_4EL,
    AntennaType.LOOP, AntennaType.GROUND_PLANE,
}


def get_antenna_beamwidth(ant: AntennaConfig) -> Optional[float]:
    """
    Return the estimated horizontal half-power beamwidth (HPBW) in degrees.
    Returns None for omnidirectional antennas (full 360° coverage).
    """
    t = ant.type
    if t in _OMNI_TYPES:
        return None
    if t in (AntennaType.YAGI_3EL, AntennaType.YAGI_5EL,
             AntennaType.YAGI_9EL, AntennaType.YAGI_15EL):
        n_el = {"yagi_3el": 3, "yagi_5el": 5, "yagi_9el": 9, "yagi_15el": 15}[t.value]
        return 80.0 / math.sqrt(n_el)
    if t == AntennaType.LOG_PERIODIC:
        return 60.0
    if t == AntennaType.SECTOR_60:
        return 60.0
    if t == AntennaType.SECTOR_90:
        return 90.0
    if t == AntennaType.SECTOR_120:
        return 120.0
    if t == AntennaType.PARABOLIC_DISH:
        lam = 3e8 / max(1.0, ant.frequency_hz)
        return 70.0 * lam / max(0.01, ant.diameter_m)
    if t == AntennaType.HORN:
        lam = 3e8 / max(1.0, ant.frequency_hz)
        return 55.0 * lam / max(0.01, ant.diameter_m)
    if t == AntennaType.PATCH:
        return 80.0
    if t == AntennaType.HELICAL:
        return 52.0
    if t == AntennaType.PHASED_ARRAY:
        return max(2.0, 100.0 / math.sqrt(max(1, ant.array_elements)))
    return None  # fallback: treat as omnidirectional


# ─────────────────────────────────────────────────────────────────────────────
# Antenna metadata catalogue
# ─────────────────────────────────────────────────────────────────────────────

ANTENNA_CATALOGUE = {
    AntennaType.ISOTROPIC: {
        "name": "Isotropic", "peak_gain_dbi": 0.0,
        "description": "Theoretical isotropic radiator. Reference for all gain measurements.",
        "freq_range": "All", "polarization": "N/A"
    },
    AntennaType.DIPOLE_HALF_WAVE: {
        "name": "Half-Wave Dipole", "peak_gain_dbi": 2.15,
        "description": "Classic resonant dipole. Donut-shaped pattern, nulls off ends.",
        "freq_range": "All", "polarization": "Linear"
    },
    AntennaType.YAGI_3EL: {
        "name": "Yagi 3-Element", "peak_gain_dbi": 8.0,
        "description": "Directional Yagi-Uda with reflector, driven, director.",
        "freq_range": "HF-SHF", "polarization": "Linear"
    },
    AntennaType.YAGI_9EL: {
        "name": "Yagi 9-Element", "peak_gain_dbi": 14.0,
        "description": "High-gain Yagi, popular for terrestrial links and EME.",
        "freq_range": "VHF-SHF", "polarization": "Linear"
    },
    AntennaType.PARABOLIC_DISH: {
        "name": "Parabolic Dish", "peak_gain_dbi": "calc",
        "description": "Reflector dish. Gain depends on size and wavelength. High directivity.",
        "freq_range": "UHF-EHF", "polarization": "Any"
    },
    AntennaType.SECTOR_120: {
        "name": "Sector 120°", "peak_gain_dbi": 14.0,
        "description": "Cellular base-station sector. 120° coverage per sector, 3-sector cell.",
        "freq_range": "700 MHz–6 GHz", "polarization": "±45° dual"
    },
    AntennaType.OMNIDIRECTIONAL: {
        "name": "Omnidirectional", "peak_gain_dbi": 2.15,
        "description": "360° azimuth coverage. Compressed elevation beam.",
        "freq_range": "All", "polarization": "Vertical"
    },
    AntennaType.PHASED_ARRAY: {
        "name": "Phased Array (64-el)", "peak_gain_dbi": 23.0,
        "description": "Electronically steerable beam. Used in 5G NR mmWave.",
        "freq_range": "UHF-mmWave", "polarization": "Dual"
    },
    AntennaType.LOG_PERIODIC: {
        "name": "Log-Periodic Dipole Array", "peak_gain_dbi": 9.0,
        "description": "Broadband directional. Frequency-independent pattern.",
        "freq_range": "Broadband", "polarization": "Linear"
    },
    AntennaType.HELICAL: {
        "name": "Helical (axial mode)", "peak_gain_dbi": 12.0,
        "description": "Circularly polarized, used for satellite comms.",
        "freq_range": "VHF-UHF", "polarization": "Circular"
    },
    AntennaType.PATCH: {
        "name": "Patch / Microstrip", "peak_gain_dbi": 7.0,
        "description": "PCB-integrated antenna, hemispherical coverage, compact.",
        "freq_range": "UHF-SHF", "polarization": "Linear or Circular"
    },
}
