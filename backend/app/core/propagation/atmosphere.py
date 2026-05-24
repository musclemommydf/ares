# SPDX-License-Identifier: MIT OR Apache-2.0
# Copyright (c) 2026 Ares

"""
Atmospheric Propagation Effects
Covers: tropospheric refractivity, ducting, rain, fog, oxygen/water-vapour
        absorption, ionospheric effects (HF), and altitude-dependent models.
Also includes time-of-day effects on tropospheric propagation.

References: ITU-R P.453, P.676, P.838, P.840, P.1144
"""
import math
import datetime
from dataclasses import dataclass, field
from typing import Optional

try:
    import numpy as np
except ImportError:
    import numpy as np  # always available


@dataclass
class AtmosphericConditions:
    """Atmospheric state for a propagation path."""
    temperature_c: float = 15.0          # surface temperature (°C)
    pressure_hpa: float = 1013.25        # surface pressure (hPa)
    humidity_percent: float = 60.0       # relative humidity (%)
    rain_rate_mm_per_hr: float = 0.0     # rain rate (mm/hr)
    visibility_km: float = 10.0          # visibility (km, for fog/haze)
    surface_refractivity: float = 301.0  # N-units (ITU-R P.453)
    refractivity_gradient: float = -40.0 # dN/dh (N-units/km), standard=-40
    # Altitude affects everything
    altitude_m: float = 0.0             # transmitter altitude
    receiver_altitude_m: float = 0.0    # receiver altitude
    # Time for day/night effects
    utc_time: Optional[datetime.datetime] = None

    def __post_init__(self):
        if self.utc_time is None:
            self.utc_time = datetime.datetime.utcnow()

    @property
    def water_vapour_g_per_m3(self) -> float:
        """Absolute humidity from relative humidity and temperature."""
        # Magnus formula for saturation vapor pressure
        e_s = 6.1078 * math.exp(17.2694 * self.temperature_c /
                                  (self.temperature_c + 238.3))  # hPa
        e = self.humidity_percent / 100.0 * e_s
        # Ideal gas law: ρ_w = 217 * e / (T_K)
        T_K = self.temperature_c + 273.15
        return 217.0 * e / T_K  # g/m³

    @property
    def is_night(self) -> bool:
        """Rough day/night determination based on UTC hour (±6h for any longitude)."""
        if self.utc_time is None:
            return False
        h = self.utc_time.hour
        return not (6 <= h <= 20)

    @property
    def ducting_present(self) -> bool:
        """
        Tropospheric ducting occurs when refractivity gradient < -157 N/km.
        (Corresponding to effective earth radius → infinity and beyond.)
        """
        return self.refractivity_gradient < -157.0


@dataclass
class AtmosphericLoss:
    """All atmospheric loss contributions (dB)."""
    oxygen_db: float = 0.0
    water_vapour_db: float = 0.0
    rain_db: float = 0.0
    cloud_fog_db: float = 0.0
    total_db: float = 0.0
    ducting_gain_db: float = 0.0   # negative = path enhancement
    ionospheric_absorption_db: float = 0.0


def compute_atmospheric_loss(freq_hz: float,
                              distance_m: float,
                              conditions: AtmosphericConditions,
                              path_elevation_angle_deg: float = 0.0) -> AtmosphericLoss:
    """
    Compute total atmospheric losses (dB) for a given path.
    Accounts for oxygen, water vapour, rain, clouds/fog, ducting,
    and ionospheric absorption (for HF frequencies).
    """
    loss = AtmosphericLoss()
    freq_ghz = freq_hz / 1e9
    dist_km = distance_m / 1000.0

    # ── Altitude-corrected path length ────────────────────────────────────────
    # For elevated antennas the effective path through the atmosphere is shorter
    if path_elevation_angle_deg > 0:
        # Slant range through atmosphere (simplified flat-earth approx)
        sin_el = math.sin(math.radians(max(0.0, path_elevation_angle_deg)))
        effective_dist_km = dist_km * max(sin_el, 0.01)
    else:
        effective_dist_km = dist_km

    # ── Gaseous absorption (O₂ + H₂O) — ITU-R P.676 ─────────────────────────
    if freq_ghz >= 1.0:
        from app.core.propagation.models import (
            oxygen_absorption_db_per_km,
            water_vapour_absorption_db_per_km,
        )
        gamma_o = oxygen_absorption_db_per_km(freq_ghz)
        gamma_w = water_vapour_absorption_db_per_km(freq_ghz,
                                                     conditions.water_vapour_g_per_m3)
        # Altitude correction: atmosphere is thinner at altitude
        alt_factor = math.exp(-conditions.altitude_m / 8500.0)  # scale height ~8.5 km
        loss.oxygen_db = gamma_o * effective_dist_km * alt_factor
        loss.water_vapour_db = gamma_w * effective_dist_km * alt_factor

    # ── Rain attenuation — ITU-R P.838 ───────────────────────────────────────
    if conditions.rain_rate_mm_per_hr > 0 and freq_ghz >= 1.0:
        from app.core.propagation.models import rain_attenuation_db_per_km
        gamma_R = rain_attenuation_db_per_km(freq_ghz, conditions.rain_rate_mm_per_hr)
        # Effective rain path length (rain cells rarely extend >20 km)
        r_eff = 1.0 / (0.477 * effective_dist_km ** 0.633 * gamma_R ** 0.073 /
                       (conditions.rain_rate_mm_per_hr ** 0.123) + 1.0 / effective_dist_km)
        loss.rain_db = gamma_R * r_eff

    # ── Cloud / Fog attenuation — ITU-R P.840 ────────────────────────────────
    if conditions.visibility_km < 1.0 and freq_ghz > 10.0:
        # Fog liquid water content from visibility (Kunkel 1984)
        lwc = 0.05 * (conditions.visibility_km ** -1.43)  # g/m³
        # Cloud specific attenuation: K_l (dB/km per g/m³)
        K_l = _cloud_specific_attenuation(freq_ghz, conditions.temperature_c)
        loss.cloud_fog_db = K_l * lwc * effective_dist_km

    # ── Tropospheric ducting ──────────────────────────────────────────────────
    if conditions.ducting_present and freq_ghz > 0.1:
        # Ducting can provide significant enhancement (negative loss)
        # Simplified model: up to 20 dB enhancement for strong duct
        gradient_excess = abs(conditions.refractivity_gradient) - 157.0
        loss.ducting_gain_db = -min(20.0, 0.5 * gradient_excess)

    # ── Ionospheric effects (HF — 3–30 MHz) ──────────────────────────────────
    if freq_hz < 30e6 and freq_hz >= 3e6:
        loss.ionospheric_absorption_db = _ionospheric_absorption(
            freq_hz, conditions, path_elevation_angle_deg)

    # ── Total ─────────────────────────────────────────────────────────────────
    loss.total_db = (loss.oxygen_db
                     + loss.water_vapour_db
                     + loss.rain_db
                     + loss.cloud_fog_db
                     + loss.ducting_gain_db
                     + loss.ionospheric_absorption_db)

    return loss


def _cloud_specific_attenuation(freq_ghz: float, temp_c: float) -> float:
    """
    Liquid water cloud specific attenuation coefficient (dB/km per g/m³).
    ITU-R P.840-7 approximation.
    """
    # Simplified Rayleigh approximation
    eps_0 = 77.6 + 103.3 * (300.0 / (temp_c + 273.15) - 1.0)
    eps_inf = 5.48
    f_p = 20.2  # GHz (principal relaxation frequency at 20°C)
    f = freq_ghz

    eps_r = eps_inf + (eps_0 - eps_inf) / (1.0 + (f / f_p) ** 2)
    eps_i = (eps_0 - eps_inf) * (f / f_p) / (1.0 + (f / f_p) ** 2)
    # Absorption efficiency
    K_l = 0.819 * f * eps_i / (eps_r ** 2 + (1.0 + 1.5 * eps_r) ** 2 / (1.0 + eps_i ** 2))
    return max(0.0, K_l)


def _ionospheric_absorption(freq_hz: float,
                              conditions: AtmosphericConditions,
                              elevation_deg: float = 45.0) -> float:
    """
    Ionospheric absorption (dB) for HF signals.
    Uses simplified CCIR/ITU method.
    D-layer absorption dominates below ~10 MHz.
    """
    f_mhz = freq_hz / 1e6
    # D-layer absorption is proportional to 1/f²
    # and depends on solar activity (approximated by time of day)
    is_day = not conditions.is_night
    solar_factor = 1.0 if is_day else 0.1  # D-layer disappears at night

    # Standard D-layer absorption (dB, one-way) for zenith path
    # Based on CCIR Report 252
    chi = max(0.0, 90.0 - elevation_deg)  # solar zenith approx
    cos_chi = math.cos(math.radians(min(chi, 89.0)))

    A_D = (677.2 / (f_mhz + 10.3) ** 2) * solar_factor * cos_chi ** 0.75

    return max(0.0, A_D)


def get_surface_refractivity(latitude: float, altitude_m: float = 0.0,
                              season: str = "summer") -> float:
    """
    Estimate surface refractivity N_s for a location.
    Based on ITU-R P.453 mapped values (simplified latitude regression).
    """
    # N_s decreases with altitude at ~40 N-units/km
    # Latitude dependence: higher refractivity near equator
    lat = abs(latitude)
    if season == "summer":
        N_s = 350.0 - 0.8 * lat  # simplified
    else:
        N_s = 330.0 - 0.7 * lat

    # Altitude correction
    N_s -= 0.04 * altitude_m  # N units per meter

    return max(200.0, min(450.0, N_s))


def compute_muf(path_length_km: float, conditions: AtmosphericConditions,
                f10_7: float = 150.0) -> float:
    """
    Maximum Usable Frequency (MHz) for HF ionospheric propagation.
    Based on simplified ITU CCIR model.
    f10_7: Solar flux index (sfu), typical ~100–250.
    Returns MUF in MHz (0 if path doesn't support ionospheric propagation).
    """
    if path_length_km > 4000:
        return 0.0  # Too long for single-hop

    # Critical frequency of F2 layer (simplified)
    f10 = f10_7
    foF2_day = 4.5 + 0.018 * f10  # MHz, rough approximation
    foF2_night = foF2_day * 0.6

    foF2 = foF2_day if not conditions.is_night else foF2_night

    # MUF for oblique path (secant law)
    # Assume F2 layer at 300 km altitude
    h_F2 = 300.0  # km
    R_E = 6371.0  # km
    # Elevation angle for path
    D = path_length_km
    sin_i = math.sin(D / (2 * R_E + h_F2) * R_E / (R_E + h_F2))
    # MUF = foF2 / cos(i)
    cos_i = math.sqrt(max(0.0, 1.0 - sin_i ** 2))
    if cos_i < 0.01:
        return 0.0
    muf = foF2 / cos_i

    return muf


def compute_luf(conditions: AtmosphericConditions,
                path_length_km: float) -> float:
    """
    Lowest Usable Frequency (MHz) — limited by D-layer absorption.
    Below LUF the signal is absorbed before reaching F2 layer.
    """
    is_day = not conditions.is_night
    if is_day:
        # D-layer absorption strongest mid-day
        base_luf = 8.0 + path_length_km * 0.001
    else:
        base_luf = 2.0  # D-layer gone at night
    return max(1.8, base_luf)


def effective_earth_radius_km(surface_refractivity: float = 301.0) -> float:
    """
    Effective earth radius for tropospheric propagation (k*a).
    Standard atmosphere: k ≈ 4/3, a_eff ≈ 8493 km.
    """
    # ITU-R P.453 formula
    k = 1.0 / (1.0 - 0.04665 * math.exp(0.005577 * surface_refractivity))
    return 6371.0 * k


def radio_horizon_distance_km(antenna_height_m: float,
                                effective_radius_km: float = 8493.0) -> float:
    """
    Radio horizon distance (km) for given antenna height and effective earth radius.
    d_H = sqrt(2 * a_eff * h)
    """
    if antenna_height_m <= 0:
        return 0.0
    return math.sqrt(2.0 * effective_radius_km * antenna_height_m / 1000.0)


def altitude_to_pressure(altitude_m: float) -> float:
    """Standard atmosphere pressure (hPa) at given altitude."""
    # ICAO standard atmosphere
    if altitude_m <= 11000:
        return 1013.25 * (1.0 - 0.0000226 * altitude_m) ** 5.2561
    elif altitude_m <= 25000:
        return 226.32 * math.exp(-0.0001577 * (altitude_m - 11000))
    else:
        return 54.75 * (1.0 + 4.6e-6 * (altitude_m - 25000)) ** -34.164


def altitude_to_temperature_c(altitude_m: float) -> float:
    """Standard ISA temperature (°C) at altitude."""
    if altitude_m <= 11000:
        return 15.0 - 0.0065 * altitude_m
    elif altitude_m <= 25000:
        return -56.5
    else:
        return -56.5 + 0.002 * (altitude_m - 25000)
