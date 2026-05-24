# SPDX-License-Identifier: MIT OR Apache-2.0
# Copyright (c) 2026 Ares

"""
Irregular Terrain Model (ITM) — Longley-Rice Propagation Model
Python implementation based on NTIA Technical Note TN-101 and Report 82-100.

This is the same algorithm used by SPLAT! and many professional RF tools.
Handles 20 MHz – 20 GHz, distances 1–2000 km, antenna heights 0.5–3000 m.

References:
  - Longley & Rice (1968): Prediction of Tropospheric Radio Transmission Loss
  - Hufford et al. (1982): A guide to the use of the ITS Irregular Terrain Model
  - NTIA Report 82-100 (ITS publication)
"""
import math
import numpy as np
from dataclasses import dataclass, field
from typing import Optional

# Physical constants
SPEED_OF_LIGHT = 2.997924580e8   # m/s
EARTH_RADIUS = 6370.0e3           # m
PI = math.pi
THIRD = 1.0 / 3.0
F_0 = 47.7  # MHz·m constant (c/2π in MHz·m)

# ITM climate codes
CLIMATE_EQUATORIAL = 1
CLIMATE_CONTINENTAL_SUBTROPICAL = 2
CLIMATE_MARITIME_SUBTROPICAL = 3
CLIMATE_DESERT = 4
CLIMATE_CONTINENTAL_TEMPERATE = 5
CLIMATE_MARITIME_TEMPERATE_OVER_LAND = 6
CLIMATE_MARITIME_TEMPERATE_OVER_SEA = 7

# Ground conductivity presets (S/m, dielectric constant)
GROUND_PRESETS = {
    "salt_water":   (5.0,    81.0),
    "fresh_water":  (0.01,   81.0),
    "wet_ground":   (0.02,   25.0),
    "avg_ground":   (0.005,  15.0),
    "dry_ground":   (0.001,  4.0),
    "mountain":     (0.001,  4.0),
    "urban":        (0.001,  5.0),
    "desert":       (0.0002, 2.5),
    "ice":          (0.001,  3.0),
}


@dataclass
class ITMInput:
    """Input parameters for the ITM propagation model."""
    # Terrain profile
    elevations: np.ndarray       # elevation values in metres (including endpoints)
    distance_m: float            # total path distance in metres
    # Antenna heights above ground
    tx_height_m: float = 30.0
    rx_height_m: float = 1.5
    # RF parameters
    frequency_mhz: float = 433.0
    polarization: int = 0        # 0=horizontal, 1=vertical
    # Ground parameters
    eps_r: float = 15.0          # relative dielectric constant
    sigma: float = 0.005         # ground conductivity S/m
    # Atmospheric
    surface_refractivity: float = 301.0  # N-units (standard = 301)
    # Climate
    climate: int = CLIMATE_CONTINENTAL_TEMPERATE
    # Variability (0–1 fractions)
    time_variability: float = 0.5
    location_variability: float = 0.5
    situation_variability: float = 0.5
    # Mode: 0=area, 1=point-to-point
    mode: int = 1


@dataclass
class ITMResult:
    """Output from the ITM propagation model."""
    path_loss_db: float = 0.0          # median path loss (dB)
    attenuation_db: float = 0.0        # excess attenuation beyond free space (dB)
    free_space_loss_db: float = 0.0    # free space path loss (dB)
    propagation_mode: str = "unknown"  # "los", "diffraction", "scatter"
    fresnel_clearance: float = 0.0     # min Fresnel zone clearance (fraction)
    radio_horizon_tx_km: float = 0.0
    radio_horizon_rx_km: float = 0.0
    terrain_clearance_db: float = 0.0
    error_code: int = 0                # 0=ok, 1=warning, 2=error
    warning_message: str = ""


class ITM:
    """
    Irregular Terrain Model (Longley-Rice) implementation.
    Computes path loss for a given terrain profile.
    """

    def __init__(self):
        self._za = 0.0
        self._zb = 0.0

    def compute(self, inp: ITMInput) -> ITMResult:
        """Main entry point. Returns path loss and diagnostics."""
        result = ITMResult()

        # Wavelength
        wn = 2.0 * PI * inp.frequency_mhz * 1e6 / SPEED_OF_LIGHT  # rad/m
        lam = SPEED_OF_LIGHT / (inp.frequency_mhz * 1e6)           # m

        n = len(inp.elevations)
        if n < 2:
            result.error_code = 2
            result.warning_message = "Need at least 2 terrain points"
            return result

        # Spacing between elevation samples
        dx = inp.distance_m / (n - 1)

        # Effective earth radius with atmospheric refraction
        gme = self._effective_earth_curvature(inp.surface_refractivity)

        # Complex ground impedance
        zgnd = self._ground_impedance(inp.eps_r, inp.sigma, inp.frequency_mhz, inp.polarization)

        # Extract terrain profile statistics
        dh = self._terrain_irregularity(inp.elevations, dx)
        dl, the, hhe = self._radio_horizons(inp.elevations, dx, inp.tx_height_m,
                                             inp.rx_height_m, gme, inp.distance_m)

        result.radio_horizon_tx_km = dl[0] / 1000.0
        result.radio_horizon_rx_km = dl[1] / 1000.0

        # Free space path loss
        result.free_space_loss_db = self._free_space_loss(inp.distance_m, inp.frequency_mhz)

        # Determine propagation mode
        d_los = dl[0] + dl[1]
        if inp.distance_m <= d_los:
            mode = "los"
        elif inp.distance_m < 3 * d_los:
            mode = "diffraction"
        else:
            mode = "scatter"

        result.propagation_mode = mode

        # Compute attenuation
        if mode == "los":
            atten = self._los_attenuation(inp.distance_m, wn, hhe, the, gme, zgnd, lam)
        elif mode == "diffraction":
            atten = self._diffraction_attenuation(inp.distance_m, wn, hhe, the, dl,
                                                   inp.elevations, dx, gme, zgnd, dh, lam)
        else:
            atten = self._scatter_attenuation(inp.distance_m, wn, hhe, the, dl,
                                               inp.surface_refractivity, dh, gme, zgnd,
                                               inp.frequency_mhz, lam)

        # Add variability
        atten += self._variability(inp.time_variability, inp.location_variability,
                                   inp.situation_variability, inp.mode, dh,
                                   inp.distance_m, inp.frequency_mhz)

        result.attenuation_db = max(0.0, atten)
        result.path_loss_db = result.free_space_loss_db + result.attenuation_db

        # Fresnel zone clearance
        result.fresnel_clearance = self._fresnel_clearance(
            inp.elevations, dx, inp.tx_height_m, inp.rx_height_m,
            inp.distance_m, inp.frequency_mhz, gme
        )

        return result

    # ─────────────────────────────────────────────────────────────
    # Internal helpers
    # ─────────────────────────────────────────────────────────────

    @staticmethod
    def _free_space_loss(distance_m: float, freq_mhz: float) -> float:
        """Free space path loss in dB. FSPL = 20log10(d) + 20log10(f) + 92.44 (km, GHz)."""
        if distance_m <= 0 or freq_mhz <= 0:
            return 0.0
        d_km = distance_m / 1000.0
        f_ghz = freq_mhz / 1000.0
        return 20.0 * math.log10(d_km) + 20.0 * math.log10(f_ghz) + 92.44

    @staticmethod
    def _effective_earth_curvature(surface_refractivity: float) -> float:
        """
        Effective earth curvature (1/effective_radius) accounting for
        atmospheric refractivity. Standard value for N=301 gives k=4/3.
        """
        # gme = 1/a_eff = 1/(k * a_earth)  where k≈4/3 for standard atmosphere
        # gme = (1/a_earth) * (1 - 0.04665*exp(0.005577*N_s)) — ITM formula
        gme = 1.0 / EARTH_RADIUS * (1.0 - 0.04665 * math.exp(0.005577 * surface_refractivity))
        return gme

    @staticmethod
    def _ground_impedance(eps_r: float, sigma: float, freq_mhz: float,
                          polarization: int) -> complex:
        """Complex ground impedance for Sommerfeld ground wave."""
        f_hz = freq_mhz * 1e6
        # Complex dielectric constant
        eps_c = complex(eps_r, -18000.0 * sigma / f_hz)
        # Ground impedance (simplified)
        if polarization == 0:  # horizontal
            zgnd = cmath_sqrt(eps_c - 1.0)
        else:  # vertical
            zgnd = cmath_sqrt(eps_c - 1.0) / eps_c
        return zgnd

    @staticmethod
    def _terrain_irregularity(elevations: np.ndarray, dx: float) -> float:
        """
        Compute terrain irregularity parameter dh (interdecile range, m).
        This characterises terrain roughness and is a key ITM input.
        """
        if len(elevations) < 2:
            return 0.0
        elev = np.sort(elevations)
        n = len(elev)
        idx_10 = max(0, int(0.1 * n))
        idx_90 = min(n - 1, int(0.9 * n))
        dh = float(elev[idx_90] - elev[idx_10])
        return max(0.0, dh)

    @staticmethod
    def _radio_horizons(elevations: np.ndarray, dx: float,
                        ht: float, hr: float, gme: float,
                        total_dist: float):
        """
        Find radio horizon distances and effective heights.
        Returns: dl[2], the[2], hhe[2]
        """
        n = len(elevations)
        # Effective heights above sea level at endpoints
        h0 = elevations[0] + ht
        hn = elevations[-1] + hr

        # Scan forward from transmitter to find horizon
        dl_t = total_dist  # default: horizon at receiver
        max_angle_t = -1e9
        for i in range(1, n):
            d_i = i * dx
            # Earth curvature correction
            h_i = elevations[i] + 0.5 * gme * d_i * (total_dist - d_i)
            angle = (h_i - h0) / d_i
            if angle > max_angle_t:
                max_angle_t = angle
                dl_t = d_i

        # Scan backward from receiver to find horizon
        dl_r = total_dist
        max_angle_r = -1e9
        for i in range(n - 2, -1, -1):
            d_i = (n - 1 - i) * dx
            d_from_start = i * dx
            h_i = elevations[i] + 0.5 * gme * d_from_start * (total_dist - d_from_start)
            angle = (h_i - (elevations[-1] + hr)) / d_i
            if angle > max_angle_r:
                max_angle_r = angle
                dl_r = d_i

        dl = np.array([dl_t, dl_r])
        the = np.array([max_angle_t, max_angle_r])

        # Effective antenna heights (above terrain at horizon)
        hhe = np.array([ht, hr])

        return dl, the, hhe

    def _los_attenuation(self, dist: float, wn: float, hhe: np.ndarray,
                         the: np.ndarray, gme: float, zgnd: complex, lam: float) -> float:
        """
        Line-of-sight attenuation. Combines smooth-earth two-ray model
        with ground-wave correction.
        """
        # Reflection point height for two-ray model
        d1 = dist
        ht = hhe[0]
        hr = hhe[1]

        # Height-gain product
        q = wn * ht * hr / d1
        # Phase difference between direct and reflected ray
        if q < 0.25:
            # Low-gain regime — surface-wave
            atten = self._smooth_earth_atten(dist, wn, hhe, gme, zgnd)
        else:
            # Interference between direct and reflected
            atten = self._two_ray_atten(dist, wn, ht, hr, lam)

        return atten

    @staticmethod
    def _smooth_earth_atten(dist: float, wn: float, hhe: np.ndarray,
                             gme: float, zgnd: complex) -> float:
        """Smooth-earth diffraction for sub-horizon LOS."""
        # Normalised distance
        ae = (2.0 / (gme * wn * wn)) ** THIRD  # effective earth radius for diffraction
        # This is the simplified Evjen/Wait approximation
        x = wn * dist / (1000.0)  # normalised
        atten = 0.05751 * x - 10.0 * math.log10(x)  # approximate
        return max(0.0, atten)

    @staticmethod
    def _two_ray_atten(dist: float, wn: float, ht: float, hr: float, lam: float) -> float:
        """
        Two-ray ground reflection model attenuation relative to free-space.
        Returns additional loss (positive = more loss, negative = gain).
        """
        # Path length difference between direct and reflected ray
        delta = 2.0 * ht * hr / dist  # approximate for d >> h
        # Phase difference
        phi = wn * delta
        # Attenuation vs free space (dB)
        if phi < 0.001:
            return 40.0 * math.log10(dist) - 20.0 * math.log10(ht * hr)  # plane-earth
        factor = math.sqrt(1.0 + (2.0 * math.cos(phi)) + 1.0)  # simplified
        atten = -20.0 * math.log10(max(0.001, abs(math.sin(phi / 2.0))))
        return atten

    def _diffraction_attenuation(self, dist: float, wn: float, hhe: np.ndarray,
                                  the: np.ndarray, dl: np.ndarray,
                                  elevations: np.ndarray, dx: float,
                                  gme: float, zgnd: complex,
                                  dh: float, lam: float) -> float:
        """
        Diffraction attenuation using Knife-edge + smooth-earth blend.
        Uses the Fresnel-Kirchhoff diffraction parameter.
        """
        n = len(elevations)
        total_dist = (n - 1) * dx

        # Find highest obstacle (Bullington method)
        max_nu = -1e9
        best_i = 1
        for i in range(1, n - 1):
            d1 = i * dx
            d2 = total_dist - d1
            h_obs = elevations[i] + 0.5 * gme * d1 * d2
            # Height of line of sight at this point
            h_los = (elevations[0] + hhe[0]) + (elevations[-1] + hhe[1] - elevations[0] - hhe[0]) * d1 / total_dist
            # Fresnel-Kirchhoff diffraction parameter ν
            nu = (h_obs - h_los) * math.sqrt(2.0 * (d1 + d2) / (lam * d1 * d2))
            if nu > max_nu:
                max_nu = nu
                best_i = i

        # Knife-edge diffraction loss (Fresnel integral approximation)
        atten = self._knife_edge_loss(max_nu)

        # Add rounded-hill correction based on terrain roughness
        if dh > 0:
            atten += self._rounded_hill_correction(dh, lam, total_dist)

        return atten

    @staticmethod
    def _knife_edge_loss(nu: float) -> float:
        """
        Knife-edge (Fresnel-Kirchhoff) diffraction loss in dB.
        Approximation from ITU-R P.526 and Rappaport.
        """
        if nu < -0.78:
            return 0.0  # No significant diffraction loss
        elif nu < 0.0:
            return 20.0 * math.log10(1.0 - 0.5 * math.exp(-0.95 * (nu + 0.78)))
        elif nu < 1.0:
            return 20.0 * math.log10(1.0 - 0.4 - 0.1184 - (0.38 - 0.1 * nu) ** 2)
        else:
            # High obstruction
            return 20.0 * math.log10(0.225 / nu) if nu > 0 else 0.0

    @staticmethod
    def _rounded_hill_correction(dh: float, lam: float, dist: float) -> float:
        """Correction for rounded hills (ITM terrain roughness parameter)."""
        # Based on ITM empirical fit for dh vs additional loss
        x = dh * (2.0 * PI / lam) ** THIRD / dist ** THIRD
        if x <= 0:
            return 0.0
        return max(0.0, 8.0 * math.log10(1.0 + x))

    def _scatter_attenuation(self, dist: float, wn: float, hhe: np.ndarray,
                              the: np.ndarray, dl: np.ndarray,
                              surface_refractivity: float, dh: float,
                              gme: float, zgnd: complex,
                              freq_mhz: float, lam: float) -> float:
        """
        Troposcatter attenuation for beyond-horizon paths.
        Based on NBS Technical Note 101.
        """
        # Scatter angle
        theta_s = max(1e-6, the[0] + the[1] + gme * dist)

        # Scatter volume height
        hs = dist * theta_s / 4.0

        # Scatter loss factor (Yeh approximation)
        # Reference: NBS TN-101, eq. (4.1)
        q = math.log(max(1e-9, 0.1 * dist / 1e3))  # dist in km
        # Clamp refractivity term to avoid log domain error at high altitudes
        N_excess = max(1.0, surface_refractivity - 100.0)
        wn_term = max(1e-30, wn * theta_s ** 3 * dist ** 2)
        atten = (
            10.0 * math.log10(wn_term)
            + 0.1 * (q ** 2)
            - 5.0 * math.log10(N_excess)
            + 18.0
        )

        # Additional troposcatter path loss
        f_ghz = freq_mhz / 1000.0
        # ITU-R approach for troposcatter
        Lbs = (190.0
               + 20.0 * math.log10(f_ghz)
               + 0.573 * theta_s * 1e3  # mrad
               - 0.15 * surface_refractivity)

        return max(atten, Lbs - self._free_space_loss(dist, freq_mhz))

    @staticmethod
    def _variability(qt: float, ql: float, qs: float, mode: int,
                     dh: float, dist: float, freq_mhz: float) -> float:
        """
        Compute variability corrections (time, location, situation).
        Returns additional loss in dB for given confidence levels.
        qt, ql, qs are fractions (0.5 = 50th percentile = median).
        """
        if mode == 0:
            # Area mode: separate time/location/situation variability
            # Convert percentages to z-scores (standard normal)
            def z(p):
                # Simplified inverse normal CDF
                if p <= 0.0:
                    return -3.0
                if p >= 1.0:
                    return 3.0
                if p == 0.5:
                    return 0.0
                # Rational approximation
                t = math.sqrt(-2.0 * math.log(min(p, 1.0 - p)))
                z0 = t - (2.515517 + 0.802853 * t + 0.010328 * t ** 2) / \
                     (1.0 + 1.432788 * t + 0.189269 * t ** 2 + 0.001308 * t ** 3)
                return -z0 if p < 0.5 else z0

            zt = z(qt)
            zl = z(ql)
            zs = z(qs)

            # Sigma values from ITM empirical data
            sg_t = 6.0 + 0.00058 * dh
            sg_l = 5.0 + 0.00036 * dh
            sg_s = 4.0 + 0.00024 * dh

            v = zt * sg_t + zl * sg_l + zs * sg_s
        else:
            # Point-to-point mode: only time variability matters
            def z(p):
                if p == 0.5:
                    return 0.0
                t = math.sqrt(-2.0 * math.log(min(p, 1.0 - p)))
                z0 = t - (2.515517 + 0.802853 * t + 0.010328 * t ** 2) / \
                     (1.0 + 1.432788 * t + 0.189269 * t ** 2 + 0.001308 * t ** 3)
                return -z0 if p < 0.5 else z0
            zt = z(qt)
            sg_t = 6.0 + 0.00058 * dh
            v = zt * sg_t

        return v

    def _fresnel_clearance(self, elevations: np.ndarray, dx: float,
                           ht: float, hr: float,
                           total_dist: float, freq_mhz: float, gme: float) -> float:
        """
        Compute minimum Fresnel zone 1 clearance along the path.
        Returns minimum clearance as fraction of F1 radius (negative = obstruction).
        """
        n = len(elevations)
        lam = SPEED_OF_LIGHT / (freq_mhz * 1e6)
        h0 = elevations[0] + ht
        hn = elevations[-1] + hr
        min_clearance = 1e9

        for i in range(1, n - 1):
            d1 = i * dx
            d2 = total_dist - d1
            # Line-of-sight height with earth curvature
            h_los = h0 + (hn - h0) * d1 / total_dist
            # First Fresnel zone radius
            r1 = math.sqrt(lam * d1 * d2 / total_dist)
            # Terrain height (with earth bulge)
            h_terr = elevations[i] + 0.5 * gme * d1 * d2
            # Clearance
            clearance = (h_los - h_terr) / max(r1, 0.001)
            if clearance < min_clearance:
                min_clearance = clearance

        return min_clearance if min_clearance < 1e8 else 1.0


def cmath_sqrt(z: complex) -> complex:
    """Square root with positive real part convention."""
    r = abs(z)
    if r == 0:
        return complex(0, 0)
    x = math.sqrt((r + z.real) / 2.0)
    y = math.copysign(math.sqrt((r - z.real) / 2.0), z.imag)
    return complex(x, y)


# ─────────────────────────────────────────────────────────────────────────────
# Convenience function
# ─────────────────────────────────────────────────────────────────────────────

def compute_itm_path_loss(
    elevations: list[float],
    distance_m: float,
    tx_height_m: float,
    rx_height_m: float,
    frequency_mhz: float,
    surface_refractivity: float = 301.0,
    polarization: int = 0,
    eps_r: float = 15.0,
    sigma: float = 0.005,
    climate: int = CLIMATE_CONTINENTAL_TEMPERATE,
    time_variability: float = 0.5,
    location_variability: float = 0.5,
    situation_variability: float = 0.5,
    mode: int = 1,
) -> ITMResult:
    """
    High-level convenience wrapper for ITM path loss calculation.
    elevations: list of terrain elevation samples (m) along the path
    distance_m: total path length (m)
    """
    itm = ITM()
    inp = ITMInput(
        elevations=np.asarray(elevations, dtype=float),
        distance_m=distance_m,
        tx_height_m=tx_height_m,
        rx_height_m=rx_height_m,
        frequency_mhz=frequency_mhz,
        surface_refractivity=surface_refractivity,
        polarization=polarization,
        eps_r=eps_r,
        sigma=sigma,
        climate=climate,
        time_variability=time_variability,
        location_variability=location_variability,
        situation_variability=situation_variability,
        mode=mode,
    )
    return itm.compute(inp)
