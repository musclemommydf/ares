# SPDX-License-Identifier: MIT OR Apache-2.0
# Copyright (c) 2026 Ares

"""
RF Propagation Models
Implements: FSPL, Hata-Okumura, COST-231, Two-Ray, ITU-R P.1546,
            ITU-R P.452, ITU-R P.528 (aeronautical), Egli, Walfisch-Ikegami,
            and more. Also handles GPU acceleration via CuPy (falls back to NumPy).
"""
import math
import numpy as np
from dataclasses import dataclass
from enum import Enum
from typing import Optional

# GPU acceleration: try CuPy first, fall back to NumPy
try:
    import cupy as xp
    GPU_AVAILABLE = True
except ImportError:
    xp = np
    GPU_AVAILABLE = False


class WaveType(str, Enum):
    AUTO          = "auto"          # System picks based on frequency/geometry
    LOS           = "los"           # Line-of-sight only (hard block beyond horizon)
    GROUND_WAVE   = "ground_wave"   # Surface wave — LF/MF/lower HF, follows Earth curvature
    SKYWAVE       = "skywave"       # Ionospheric skip — HF (3–30 MHz), long-distance hops
    TROPOSCATTER  = "troposcatter"  # Tropospheric scatter — UHF, beyond-horizon


class PropagationModel(str, Enum):
    FSPL = "fspl"                       # Free Space Path Loss
    ITM = "itm"                         # Longley-Rice (ITM) ← default
    HATA_URBAN = "hata_urban"           # Okumura-Hata Urban
    HATA_SUBURBAN = "hata_suburban"
    HATA_RURAL = "hata_rural"
    COST231_HATA = "cost231_hata"       # COST-231 Hata (1.5–2 GHz)
    COST231_WI = "cost231_wi"           # COST-231 Walfisch-Ikegami
    TWO_RAY = "two_ray"                 # Two-ray ground reflection
    ITU_P1546 = "itu_p1546"            # ITU-R P.1546 (30–3000 MHz)
    ITU_P452 = "itu_p452"              # ITU-R P.452 (interference)
    ITU_P528 = "itu_p528"              # ITU-R P.528 (aeronautical)
    EGLI = "egli"                       # Egli model
    PLANE_EARTH = "plane_earth"         # Plane earth (4th power law)
    ERICSSON = "ericsson"               # Ericsson 9999 model
    SUI = "sui"                         # Stanford University Interim
    ECC33 = "ecc33"                     # ECC-33 model
    NVIS_HF = "nvis_hf"                 # HF Near Vertical Incidence Skywave
    RADAR = "radar"                     # Radar equation (two-way path loss)


@dataclass
class LinkBudget:
    """Full RF link budget with all gains and losses."""
    # Transmitter
    tx_power_dbm: float = 27.0          # Transmit power
    tx_antenna_gain_dbi: float = 0.0    # TX antenna gain
    tx_cable_loss_db: float = 0.0       # Feedline loss
    # Receiver
    rx_antenna_gain_dbi: float = 0.0
    rx_cable_loss_db: float = 0.0
    rx_sensitivity_dbm: float = -100.0  # Minimum detectable signal
    rx_noise_figure_db: float = 3.0
    # Path
    path_loss_db: float = 0.0
    # Additional losses
    polarization_mismatch_db: float = 0.0
    atmospheric_loss_db: float = 0.0
    rain_loss_db: float = 0.0
    foliage_loss_db: float = 0.0
    building_penetration_db: float = 0.0
    # Margins
    fade_margin_db: float = 10.0
    interference_margin_db: float = 0.0

    @property
    def eirp_dbm(self) -> float:
        return self.tx_power_dbm + self.tx_antenna_gain_dbi - self.tx_cable_loss_db

    @property
    def received_power_dbm(self) -> float:
        return (self.eirp_dbm
                - self.path_loss_db
                - self.atmospheric_loss_db
                - self.rain_loss_db
                - self.foliage_loss_db
                - self.building_penetration_db
                - self.polarization_mismatch_db
                + self.rx_antenna_gain_dbi
                - self.rx_cable_loss_db)

    @property
    def link_margin_db(self) -> float:
        return self.received_power_dbm - self.rx_sensitivity_dbm

    @property
    def is_viable(self) -> bool:
        return self.link_margin_db >= self.fade_margin_db

    @property
    def max_eirp_dbm_legal(self) -> float:
        """Placeholder for regulatory EIRP limits (to be populated by band)."""
        return 36.0  # Common ISM default

    def to_dict(self) -> dict:
        return {
            "tx_power_dbm": self.tx_power_dbm,
            "tx_antenna_gain_dbi": self.tx_antenna_gain_dbi,
            "eirp_dbm": self.eirp_dbm,
            "path_loss_db": self.path_loss_db,
            "atmospheric_loss_db": self.atmospheric_loss_db,
            "rain_loss_db": self.rain_loss_db,
            "rx_antenna_gain_dbi": self.rx_antenna_gain_dbi,
            "received_power_dbm": self.received_power_dbm,
            "rx_sensitivity_dbm": self.rx_sensitivity_dbm,
            "link_margin_db": self.link_margin_db,
            "fade_margin_db": self.fade_margin_db,
            "is_viable": self.is_viable,
        }


# ─────────────────────────────────────────────────────────────────────────────
# Individual propagation model functions
# ─────────────────────────────────────────────────────────────────────────────

def fspl_db(distance_m: float, frequency_hz: float) -> float:
    """Free Space Path Loss (dB). Valid for any frequency and distance."""
    if distance_m <= 0 or frequency_hz <= 0:
        return 0.0
    return 20.0 * math.log10(4.0 * math.pi * distance_m * frequency_hz / 3e8)


def hata_urban_db(distance_km: float, freq_mhz: float,
                  tx_height_m: float, rx_height_m: float) -> float:
    """
    Okumura-Hata model for urban areas.
    Valid: 150–1500 MHz, 1–20 km, hb=30–200m, hm=1–10m
    """
    fc = freq_mhz
    hb = tx_height_m  # base station height
    hm = rx_height_m  # mobile height
    d = distance_km

    # Mobile antenna height correction factor
    if fc <= 300:
        a_hm = (1.1 * math.log10(fc) - 0.7) * hm - (1.56 * math.log10(fc) - 0.8)
    else:
        a_hm = 3.2 * (math.log10(11.75 * hm)) ** 2 - 4.97  # large city

    L = (69.55
         + 26.16 * math.log10(fc)
         - 13.82 * math.log10(max(hb, 1.0))
         - a_hm
         + (44.9 - 6.55 * math.log10(max(hb, 1.0))) * math.log10(max(d, 0.001)))
    return L


def hata_suburban_db(distance_km: float, freq_mhz: float,
                     tx_height_m: float, rx_height_m: float) -> float:
    """Hata Suburban — reduced by 2*[log10(fc/28)]^2 + 5.4"""
    L_urban = hata_urban_db(distance_km, freq_mhz, tx_height_m, rx_height_m)
    return L_urban - 2.0 * (math.log10(freq_mhz / 28.0)) ** 2 - 5.4


def hata_rural_db(distance_km: float, freq_mhz: float,
                  tx_height_m: float, rx_height_m: float) -> float:
    """Hata Rural / Open area."""
    L_urban = hata_urban_db(distance_km, freq_mhz, tx_height_m, rx_height_m)
    return L_urban - 4.78 * (math.log10(freq_mhz)) ** 2 + 18.33 * math.log10(freq_mhz) - 40.94


def cost231_hata_db(distance_km: float, freq_mhz: float,
                    tx_height_m: float, rx_height_m: float,
                    city_size: str = "large") -> float:
    """
    COST-231 Hata model extension.
    Valid: 1500–2000 MHz, 1–20 km, hb=30–200m, hm=1–10m
    C_m = 0 for medium/small city, 3 dB for large city/metropolitan centre.
    """
    fc = freq_mhz
    hb = tx_height_m
    hm = rx_height_m
    d = distance_km
    C_m = 3.0 if city_size == "large" else 0.0

    a_hm = (1.1 * math.log10(fc) - 0.7) * hm - (1.56 * math.log10(fc) - 0.8)

    L = (46.3
         + 33.9 * math.log10(fc)
         - 13.82 * math.log10(max(hb, 1.0))
         - a_hm
         + (44.9 - 6.55 * math.log10(max(hb, 1.0))) * math.log10(max(d, 0.001))
         + C_m)
    return L


def two_ray_db(distance_m: float, freq_hz: float,
               tx_height_m: float, rx_height_m: float) -> float:
    """
    Two-ray ground reflection model.
    Valid for d >> max(ht, hr), requires flat ground.
    For large d: L = 40log10(d) - 20log10(ht*hr).
    """
    ht = tx_height_m
    hr = rx_height_m
    d = distance_m
    lam = 3e8 / freq_hz

    # Critical distance (breakpoint)
    d_c = 4.0 * ht * hr / lam

    if d < d_c:
        return fspl_db(d, freq_hz)
    else:
        # Plane-earth two-ray formula
        L = 40.0 * math.log10(d) - 20.0 * math.log10(ht * hr)
        return L


def itu_p1546_db(distance_km: float, freq_mhz: float,
                  tx_height_m: float, rx_height_m: float,
                  environment: str = "rural", time_percent: float = 50.0) -> float:
    """
    ITU-R P.1546-6 field strength prediction (simplified).
    Valid: 30–3000 MHz, 1–1000 km, various environments.
    Returns path loss in dB.
    """
    # Simplified implementation: ITU-R P.1546 uses interpolated tables
    # We use an approximation based on the correction factors
    h_eff = tx_height_m  # effective height
    d = distance_km
    f = freq_mhz

    # Base formula (rough ITU-R P.1546 approximation)
    if f < 300:
        A = 69.55 + 26.16 * math.log10(f) - 13.82 * math.log10(max(h_eff, 1))
        B = 44.9 - 6.55 * math.log10(max(h_eff, 1))
    else:
        A = 46.3 + 33.9 * math.log10(f) - 13.82 * math.log10(max(h_eff, 1))
        B = 44.9 - 6.55 * math.log10(max(h_eff, 1))

    L = A + B * math.log10(max(d, 0.001))

    # Time percentage correction
    if time_percent != 50.0:
        sigma = 5.5  # typical location variability
        from scipy.stats import norm
        z = norm.ppf(time_percent / 100.0)
        L += z * sigma

    # Environment correction
    env_correction = {"urban": 3.0, "suburban": 0.0, "rural": -5.0, "open": -10.0}
    L += env_correction.get(environment, 0.0)

    return L


def itu_p528_db(distance_km: float, freq_mhz: float,
                tx_height_m: float, rx_height_m: float,
                time_percent: float = 50.0) -> float:
    """
    ITU-R P.528-5: Propagation curves for aeronautical mobile and
    radionavigation services (100 MHz – 15.5 GHz).
    Handles sea-level to 30,000 ft aircraft altitude.
    """
    # P.528 uses complex tabulated data. We use the key formula components.
    d = distance_km
    f = freq_mhz
    ht = tx_height_m
    hr = rx_height_m

    # Horizon distances
    d_t = 4.1 * math.sqrt(max(ht, 0))  # km (smooth earth, ht in m)
    d_r = 4.1 * math.sqrt(max(hr, 0))  # km

    d_LOS = d_t + d_r  # total LOS range

    if d <= d_LOS:
        # Line of sight: use free space
        return fspl_db(d * 1000, f * 1e6)
    else:
        # Beyond horizon: use troposcatter approximation
        # Based on ITU-R P.528 for air-ground paths
        d_ex = d - d_LOS  # excess distance
        fspl = fspl_db(d * 1000, f * 1e6)
        # Scatter loss beyond horizon
        Lbs = fspl + 0.07 * d_ex * (f / 1000.0) ** 0.3
        return Lbs


def egli_db(distance_km: float, freq_mhz: float,
            tx_height_m: float, rx_height_m: float) -> float:
    """
    Egli model (1957). Rural empirical model.
    Valid: 40–900 MHz, 1–50 km.
    """
    d = distance_km
    f = freq_mhz
    hb = tx_height_m
    hm = rx_height_m

    # Egli formula
    L = (76.3
         - 10.0 * math.log10(hb * hm)
         + 20.0 * math.log10(f)
         + 40.0 * math.log10(d))
    return L


def sui_db(distance_km: float, freq_ghz: float,
           tx_height_m: float, rx_height_m: float,
           terrain_type: str = "b") -> float:
    """
    Stanford University Interim (SUI) model.
    Developed for IEEE 802.16 WiMAX. Valid: 2–11 GHz.
    terrain_type: 'a' (hilly/moderate vegetation), 'b' (intermediate),
                  'c' (flat/light vegetation)
    """
    d0 = 0.1  # reference distance (km)
    d = distance_km
    f = freq_ghz * 1000  # MHz
    hb = tx_height_m
    hm = rx_height_m

    # SUI model parameters
    params = {
        "a": (4.6, 0.0075, 12.6),
        "b": (4.0, 0.0065, 17.1),
        "c": (3.6, 0.0050, 20.0),
    }
    a, b, c = params.get(terrain_type, params["b"])

    gamma = a - b * hb + c / hb

    # Path exponent adjustment
    s = 8.2  # log-normal shadowing (dB)

    # Basic SUI formula
    A = 20.0 * math.log10(4.0 * math.pi * d0 * 1000 * f * 1e6 / 3e8)
    L = (A
         + 10.0 * gamma * math.log10(d / d0)
         + 6.0 * math.log10(f / 2000.0)  # frequency correction
         - 10.26 * math.log10(hm / 2.0))  # height correction

    return L


def plane_earth_db(distance_m: float, freq_hz: float,
                   tx_height_m: float, rx_height_m: float) -> float:
    """Plane-earth (4th power law). Applies beyond critical distance."""
    return 40.0 * math.log10(distance_m) - 20.0 * math.log10(tx_height_m * rx_height_m)


def oxygen_absorption_db_per_km(freq_ghz: float) -> float:
    """
    Oxygen absorption (dB/km) — ITU-R P.676-12 simplified.
    Significant above ~50 GHz (oxygen band) and ~22 GHz (water vapour).
    """
    f = freq_ghz
    if f < 1:
        return 0.0
    # Simplified approximation based on ITU-R P.676
    # Oxygen resonance peak near 60 GHz
    if f < 54:
        gamma_o = 7.19e-3 + (6.09 / (f ** 2 + 0.227) + 4.81 / ((f - 57) ** 2 + 1.5)) * f ** 2 * 1e-3
    elif f < 66:
        gamma_o = math.exp(0.727 - 8.81e-2 * (f - 57.0))
    else:
        gamma_o = 0.01 + (0.32 / (f ** 2 + 0.11) + 0.15 / ((f - 118.75) ** 2 + 2.5)) * f ** 2 * 1e-3

    return max(0.0, gamma_o)


def water_vapour_absorption_db_per_km(freq_ghz: float,
                                       water_vapour_g_per_m3: float = 7.5) -> float:
    """
    Water vapour absorption (dB/km) — ITU-R P.676-12 simplified.
    Primary resonance at 22.235 GHz and 183.31 GHz.
    """
    f = freq_ghz
    rho = water_vapour_g_per_m3
    if f < 1:
        return 0.0
    # Simplified formula (valid 1–350 GHz)
    gamma_w = (0.050 + 0.0021 * rho
               + 3.6 / ((f - 22.235) ** 2 + 8.5)
               + 10.6 / ((f - 183.31) ** 2 + 9.0)
               + 8.9 / ((f - 325.153) ** 2 + 26.3)) * f ** 2 * rho * 1e-4
    return max(0.0, gamma_w)


def rain_attenuation_db_per_km(freq_ghz: float, rain_rate_mm_per_hr: float,
                                 polarization: int = 0) -> float:
    """
    Rain attenuation coefficient (dB/km) — ITU-R P.838-3.
    Specific attenuation: γ_R = k * R^α
    polarization: 0=horizontal, 1=vertical
    """
    f = freq_ghz
    R = rain_rate_mm_per_hr

    # ITU-R P.838 Table 1 coefficients (log-interpolated)
    # Frequency points for kH, alphaH, kV, alphaV
    freq_table = [1, 2, 4, 6, 7, 8, 10, 12, 15, 20, 25, 30, 35, 40, 45, 50,
                  60, 70, 80, 90, 100, 120, 150, 200, 300, 400]
    kH_table = [0.0000387, 0.000154, 0.000650, 0.00175, 0.00301, 0.00454,
                0.0101, 0.0188, 0.0367, 0.0751, 0.124, 0.187, 0.263, 0.350,
                0.442, 0.536, 0.707, 0.851, 0.975, 1.06, 1.12, 1.18, 1.31,
                1.45, 1.36, 1.32]
    alphaH_table = [0.912, 0.963, 1.121, 1.308, 1.332, 1.327, 1.276, 1.217,
                    1.154, 1.099, 1.061, 1.021, 0.979, 0.939, 0.903, 0.873,
                    0.826, 0.793, 0.769, 0.753, 0.743, 0.731, 0.710, 0.689,
                    0.688, 0.683]

    # Interpolate
    if f <= freq_table[0]:
        k = kH_table[0]
        alpha = alphaH_table[0]
    elif f >= freq_table[-1]:
        k = kH_table[-1]
        alpha = alphaH_table[-1]
    else:
        for i in range(len(freq_table) - 1):
            if freq_table[i] <= f <= freq_table[i + 1]:
                t = (math.log10(f) - math.log10(freq_table[i])) / \
                    (math.log10(freq_table[i + 1]) - math.log10(freq_table[i]))
                k = 10 ** (math.log10(kH_table[i]) * (1 - t) + math.log10(kH_table[i + 1]) * t)
                alpha = alphaH_table[i] * (1 - t) + alphaH_table[i + 1] * t
                break
        else:
            k = kH_table[-1]
            alpha = alphaH_table[-1]

    # Vertical polarization: slightly different coefficients
    if polarization == 1:
        k *= 0.95  # approximate correction

    gamma_R = k * (R ** alpha)
    return gamma_R


def ericsson_9999_db(distance_km: float, freq_mhz: float,
                     tx_height_m: float, rx_height_m: float,
                     context: int = 2) -> float:
    """
    Ericsson 9999 generalised model.
    Valid: 150–1900 MHz, 1–100 km.

    context: 1 = urban/conservative
             2 = suburban/average  (default)
             3 = rural/optimistic

    L = a0 + a1·log(d) + a2·log(hb) + a3·log(hb)·log(d)
        − 3.2·(log(11.75·hm))² + g(f)
    g(f) = 44.49·log(f) − 4.78·(log(f))²
    """
    d  = max(distance_km, 0.001)
    f  = freq_mhz
    hb = max(tx_height_m, 1.0)
    hm = max(rx_height_m, 0.5)

    # Context-dependent coefficients
    coeffs = {
        1: (36.2,  30.2,  -12.0, 0.1),   # urban
        2: (43.2,  68.93, -12.0, 0.1),   # suburban/average
        3: (45.95, 100.6, -12.0, 0.1),   # rural
    }
    a0, a1, a2, a3 = coeffs.get(context, coeffs[2])

    g_f = 44.49 * math.log10(f) - 4.78 * (math.log10(f)) ** 2
    L = (a0
         + a1 * math.log10(d)
         + a2 * math.log10(hb)
         + a3 * math.log10(hb) * math.log10(d)
         - 3.2 * (math.log10(11.75 * hm)) ** 2
         + g_f)
    return L


def nvis_hf_db(distance_km: float, freq_mhz: float,
               context: int = 2) -> float:
    """
    HF Near Vertical Incidence Skywave (NVIS) path loss.
    Optimised for close-range HF communication (0–600 km) using steeply
    propagating waves that reflect from the ionosphere almost vertically.

    context: 1 = D layer (80–120 km, day only, heavily absorbing, < 5 MHz)
             2 = E layer (100–130 km, day only, moderate, 5–10 MHz)
             3 = F layer (200–400 km, day/night, low loss, 5–30 MHz)

    Loss includes:
      - Free-space loss for the two-leg slant path (TX→layer→RX)
      - Ionospheric absorption loss per layer
      - Polarisation coupling loss (~3 dB)
    """
    layer_height_km = {1: 100.0, 2: 115.0, 3: 300.0}.get(context, 115.0)
    absorption_base = {1: 22.0, 2: 10.0, 3: 4.0}.get(context, 10.0)
    f = max(freq_mhz, 0.5)

    # Slant distance for each leg: TX to apex directly above midpoint
    d_half_km = distance_km / 2.0
    slant_km = math.sqrt(d_half_km ** 2 + layer_height_km ** 2)
    total_path_m = 2.0 * slant_km * 1000.0

    # Two-way free-space loss (NVIS is single-hop; path loss like any radio link)
    fspl = fspl_db(total_path_m, f * 1e6)

    # Ionospheric absorption (higher at low frequency — D & E absorption)
    # Simplified ITU-R P.533 absorption: A ≈ a + b/f  (dB)
    absorption_b = {1: 30.0, 2: 15.0, 3: 5.0}.get(context, 15.0)
    absorption = absorption_base + absorption_b / f

    # Polarisation coupling (horizontal antenna for NVIS)
    pol_loss = 3.0

    return fspl + absorption + pol_loss


def radar_two_way_db(distance_m: float, freq_hz: float,
                     rcs_m2: float = 1.0) -> float:
    """
    Radar two-way path loss.
    Encodes the radar range equation as an equivalent one-way path loss so the
    normal link-budget arithmetic still works:

      P_r = P_t + G_t + G_r + 20·log(λ) + 10·log(RCS) − 30·log(4π) − 40·log(R)

    The 'path loss' returned here is the two-way geometric spreading term:
      L_radar = 30·log(4π) + 40·log(R) − 20·log(λ) − 10·log(RCS)

    So: P_r = EIRP + G_r − L_radar
    """
    if distance_m <= 0 or freq_hz <= 0 or rcs_m2 <= 0:
        return 300.0
    lam = 3e8 / freq_hz
    L = (30.0 * math.log10(4.0 * math.pi)
         + 40.0 * math.log10(distance_m)
         - 20.0 * math.log10(lam)
         - 10.0 * math.log10(rcs_m2))
    return L


def cost231_wi_db(distance_km: float, freq_mhz: float,
                  tx_height_m: float, rx_height_m: float,
                  roof_height_m: float = 15.0, building_sep_m: float = 30.0,
                  street_width_m: float = 15.0, road_orientation_deg: float = 90.0,
                  metropolitan: bool = False, los: bool = False) -> float:
    """
    COST-231 Walfisch-Ikegami model. Valid: 800–2000 MHz, 0.02–5 km,
    base 4–50 m, mobile 1–3 m. Models rooftop-to-street diffraction (Lrts) +
    multiscreen diffraction (Lmsd) over a regular row of buildings.

    For street-canyon LOS a separate (lower) loss applies. NLOS is the default.
    """
    d = max(distance_km, 0.02)
    f = freq_mhz
    Lfs = 32.4 + 20.0 * math.log10(d) + 20.0 * math.log10(f)

    if los:
        # Street-canyon line-of-sight (both antennas see down the same street).
        return 42.6 + 26.0 * math.log10(d) + 20.0 * math.log10(f)

    dh_mobile = roof_height_m - rx_height_m          # rooftop above the mobile
    dh_base = tx_height_m - roof_height_m            # base above/below the rooftops
    w = max(street_width_m, 1.0)
    b = max(building_sep_m, 1.0)
    phi = max(0.0, min(road_orientation_deg, 90.0))

    # Street-orientation correction
    if phi < 35.0:
        Lori = -10.0 + 0.354 * phi
    elif phi < 55.0:
        Lori = 2.5 + 0.075 * (phi - 35.0)
    else:
        Lori = 4.0 - 0.114 * (phi - 55.0)

    # Rooftop-to-street diffraction + scatter
    Lrts = -16.9 - 10.0 * math.log10(w) + 10.0 * math.log10(f) + 20.0 * math.log10(max(dh_mobile, 0.1)) + Lori
    Lrts = max(Lrts, 0.0)

    # Multiscreen diffraction
    if tx_height_m > roof_height_m:
        Lbsh = -18.0 * math.log10(1.0 + dh_base)
        ka = 54.0
        kd = 18.0
    else:
        Lbsh = 0.0
        kd = 18.0 - 15.0 * dh_base / max(roof_height_m, 1.0)
        if d >= 0.5:
            ka = 54.0 - 0.8 * dh_base
        else:
            ka = 54.0 - 0.8 * dh_base * (d / 0.5)
    if metropolitan:
        kf = -4.0 + 1.5 * (f / 925.0 - 1.0)
    else:
        kf = -4.0 + 0.7 * (f / 925.0 - 1.0)

    Lmsd = Lbsh + ka + kd * math.log10(d) + kf * math.log10(f) - 9.0 * math.log10(b)

    if Lrts + Lmsd > 0.0:
        return Lfs + Lrts + Lmsd
    return Lfs


def ecc33_db(distance_km: float, freq_ghz: float,
             tx_height_m: float, rx_height_m: float,
             city_size: str = "medium") -> float:
    """
    ECC-33 (Hata-Okumura extended for fixed wireless, ~700 MHz–3.5 GHz).
    L = Afs + Abm − Gb − Gr.  Heights: base 30–200 m, terminal 1–10 m.
    """
    d = max(distance_km, 0.001)
    f = max(freq_ghz, 0.05)
    hb = max(tx_height_m, 1.0)
    hr = max(rx_height_m, 1.0)

    Afs = 92.4 + 20.0 * math.log10(d) + 20.0 * math.log10(f)
    Abm = (20.41 + 9.83 * math.log10(d) + 7.894 * math.log10(f)
           + 9.56 * (math.log10(f)) ** 2)
    Gb = math.log10(hb / 200.0) * (13.958 + 5.8 * (math.log10(d)) ** 2)
    if city_size == "large":
        Gr = 0.759 * hr - 1.862
    else:  # medium city / suburban
        Gr = (42.57 + 13.7 * math.log10(f)) * (math.log10(hr) - 0.585)
    return Afs + Abm - Gb - Gr


def _spherical_earth_diffraction_db(distance_km: float, freq_mhz: float,
                                    h1_m: float, h2_m: float,
                                    k_factor: float = 4.0 / 3.0) -> float:
    """
    Beyond-horizon smooth-earth diffraction loss (ITU-R P.526 first residue
    term, horizontal polarisation). Returns 0 inside the radio horizon.
    """
    ae = 6371.0 * k_factor  # effective earth radius (km)
    f = max(freq_mhz, 20.0)
    # Marginal line-of-sight (smooth-earth horizon) distance, km.
    d_los = math.sqrt(2.0 * ae) * (math.sqrt(0.001 * max(h1_m, 0.0)) + math.sqrt(0.001 * max(h2_m, 0.0)))
    if distance_km <= max(d_los, 1e-6):
        return 0.0
    beta = 1.0  # horizontal polarisation, f > 20 MHz
    X = 2.188 * beta * (f ** (1.0 / 3.0)) * (ae ** (-2.0 / 3.0)) * distance_km

    def _F(x):
        if x >= 1.6:
            return 11.0 + 10.0 * math.log10(x) - 17.6 * x
        return -20.0 * math.log10(max(x, 1e-9)) - 5.6488 * (x ** 1.425)

    def _G(h_m):
        Y = 9.575e-3 * beta * (f ** (2.0 / 3.0)) * (ae ** (-1.0 / 3.0)) * h_m
        B = beta * Y
        if B > 2.0:
            return 17.6 * math.sqrt(max(B - 1.1, 0.0)) - 5.0 * math.log10(max(B - 1.1, 1e-9)) - 8.0
        return 20.0 * math.log10(B + 0.1 * B ** 3) if B > 0 else 0.0

    E = _F(X) + _G(h1_m) + _G(h2_m)   # field strength relative to free space (dB, ≤0 beyond horizon)
    return max(-E, 0.0)


def itu_p452_db(distance_km: float, freq_ghz: float,
                tx_height_m: float, rx_height_m: float,
                temp_c: float = 15.0, pressure_hpa: float = 1013.0,
                water_vapour_g_m3: float = 7.5) -> float:
    """
    ITU-R P.452 clear-air interference path loss — the LoS subset: free-space
    spreading + gaseous (O₂ + water-vapour) absorption + smooth-earth diffraction
    beyond the radio horizon (P.526). The full P.452 ducting/troposcatter/terrain
    terms need the path profile; for terrain paths use ITM, which carries it.
    """
    d = max(distance_km, 1e-3)
    f = max(freq_ghz, 1e-3)
    Lbfs = 92.5 + 20.0 * math.log10(f) + 20.0 * math.log10(d)
    Ag = (oxygen_absorption_db_per_km(f)
          + water_vapour_absorption_db_per_km(f, water_vapour_g_m3)) * d
    Ld = _spherical_earth_diffraction_db(d, f * 1000.0, tx_height_m, rx_height_m)
    Lb_diff = Lbfs + Ag + Ld                      # free-space + gas + diffraction path
    # Beyond the horizon a troposcatter path also exists; the received level follows
    # whichever mechanism is *less* lossy. Combine by power sum (P.452 blends the
    # mechanisms; the easier path dominates) once we're past the smooth-earth horizon.
    if Ld > 0.0:
        Lbs = _troposcatter_path_loss_db(d, f * 1000.0) + Ag
        return -10.0 * math.log10(10 ** (-Lb_diff / 10.0) + 10 ** (-Lbs / 10.0))
    return Lb_diff


# ─────────────────────────────────────────────────────────────────────────────
# GPU-accelerated batch computation
# ─────────────────────────────────────────────────────────────────────────────

def batch_fspl_db(distances_m: np.ndarray, frequency_hz: float,
                   use_gpu: bool = False) -> np.ndarray:
    """
    Compute FSPL for an array of distances.
    Uses CuPy GPU acceleration when available and requested.
    """
    if use_gpu and GPU_AVAILABLE:
        import cupy as cp
        d = cp.asarray(distances_m)
        d = cp.where(d <= 0, 1e-9, d)
        result = 20.0 * cp.log10(4.0 * math.pi * d * frequency_hz / 3e8)
        return cp.asnumpy(result)
    else:
        d = np.where(distances_m <= 0, 1e-9, distances_m)
        return 20.0 * np.log10(4.0 * math.pi * d * frequency_hz / 3e8)


def select_model(model: PropagationModel, distance_m: float, freq_hz: float,
                 tx_height_m: float, rx_height_m: float, **kwargs) -> float:
    """
    Route to correct propagation model and return path loss (dB).

    Extra kwargs:
      context      int   1=urban/D-layer, 2=average/E-layer, 3=rural/F-layer
      city_size    str   "large" | "medium" for COST-231 Hata
      environment  str   "urban" | "suburban" | "rural" | "open" for ITU-P.1546
      time_percent float time availability percentage (default 50)
      terrain_type str   "a"|"b"|"c" for SUI
      rcs_m2       float radar cross section (m²) for RADAR model
    """
    freq_mhz = freq_hz / 1e6
    freq_ghz = freq_hz / 1e9
    distance_km = distance_m / 1000.0
    context = int(kwargs.get("context", 2))

    if model == PropagationModel.FSPL:
        return fspl_db(distance_m, freq_hz)
    elif model == PropagationModel.HATA_URBAN:
        return hata_urban_db(distance_km, freq_mhz, tx_height_m, rx_height_m)
    elif model == PropagationModel.HATA_SUBURBAN:
        return hata_suburban_db(distance_km, freq_mhz, tx_height_m, rx_height_m)
    elif model == PropagationModel.HATA_RURAL:
        return hata_rural_db(distance_km, freq_mhz, tx_height_m, rx_height_m)
    elif model == PropagationModel.COST231_HATA:
        # context 1 = large city, 2/3 = medium/small
        city = "large" if context == 1 else kwargs.get("city_size", "medium")
        return cost231_hata_db(distance_km, freq_mhz, tx_height_m, rx_height_m, city)
    elif model == PropagationModel.TWO_RAY:
        return two_ray_db(distance_m, freq_hz, tx_height_m, rx_height_m)
    elif model == PropagationModel.ITU_P1546:
        # context maps to environment
        env_map = {1: "urban", 2: "suburban", 3: "rural"}
        env = env_map.get(context, kwargs.get("environment", "rural"))
        tp = kwargs.get("time_percent", 50.0)
        return itu_p1546_db(distance_km, freq_mhz, tx_height_m, rx_height_m, env, tp)
    elif model == PropagationModel.ITU_P528:
        tp = kwargs.get("time_percent", 50.0)
        return itu_p528_db(distance_km, freq_mhz, tx_height_m, rx_height_m, tp)
    elif model == PropagationModel.EGLI:
        return egli_db(distance_km, freq_mhz, tx_height_m, rx_height_m)
    elif model == PropagationModel.SUI:
        terrain_map = {1: "a", 2: "b", 3: "c"}
        terrain = terrain_map.get(context, kwargs.get("terrain_type", "b"))
        return sui_db(distance_km, freq_ghz, tx_height_m, rx_height_m, terrain)
    elif model == PropagationModel.PLANE_EARTH:
        return plane_earth_db(distance_m, freq_hz, tx_height_m, rx_height_m)
    elif model == PropagationModel.ERICSSON:
        return ericsson_9999_db(distance_km, freq_mhz, tx_height_m, rx_height_m, context)
    elif model == PropagationModel.NVIS_HF:
        return nvis_hf_db(distance_km, freq_mhz, context)
    elif model == PropagationModel.RADAR:
        rcs = kwargs.get("rcs_m2", 1.0)
        return radar_two_way_db(distance_m, freq_hz, rcs)
    elif model == PropagationModel.COST231_WI:
        return cost231_wi_db(
            distance_km, freq_mhz, tx_height_m, rx_height_m,
            roof_height_m=kwargs.get("roof_height_m", 15.0),
            building_sep_m=kwargs.get("building_sep_m", 30.0),
            street_width_m=kwargs.get("street_width_m", 15.0),
            road_orientation_deg=kwargs.get("road_orientation_deg", 90.0),
            metropolitan=(context == 1),
            los=bool(kwargs.get("los", False)),
        )
    elif model == PropagationModel.ECC33:
        # context 1 = large city, 2/3 = medium/suburban
        city = "large" if context == 1 else "medium"
        return ecc33_db(distance_km, freq_ghz, tx_height_m, rx_height_m, city)
    elif model == PropagationModel.ITU_P452:
        return itu_p452_db(
            distance_km, freq_ghz, tx_height_m, rx_height_m,
            temp_c=kwargs.get("temp_c", 15.0),
            pressure_hpa=kwargs.get("pressure_hpa", 1013.0),
            water_vapour_g_m3=kwargs.get("water_vapour_g_m3", 7.5),
        )
    else:
        return fspl_db(distance_m, freq_hz)


# ─────────────────────────────────────────────────────────────────────────────
# Wave type physics
# ─────────────────────────────────────────────────────────────────────────────

def _ground_wave_path_loss_db(distance_km: float, freq_mhz: float,
                               ground_type: str = "average") -> float:
    """
    Ground wave (surface wave) path loss — ITU-R P.368-9 simplified.
    Dominant propagation mode for LF/MF and lower HF (< 10 MHz).
    Signal follows Earth's curvature; range depends heavily on frequency
    and ground conductivity.

    ground_type: 'sea_water' | 'wet_ground' | 'average' | 'dry_ground'
    """
    sigma = {
        "sea_water":  5.0,
        "wet_ground": 0.03,
        "average":    0.005,
        "dry_ground": 0.001,
    }.get(ground_type, 0.005)

    # Free space baseline
    f_hz = freq_mhz * 1e6
    fspl = fspl_db(distance_km * 1000, f_hz)

    # ITU-R P.368 numerical distance p
    # p ≈ (π × d[m]) / (λ × |n²|½) where n² is complex permittivity
    # Simplified: p ≈ π × d[km] × f[MHz]² / (0.18 × σ[S/m] × 1e3)
    p = math.pi * distance_km * (freq_mhz ** 2) / (0.18 * sigma * 1e3)
    p = max(p, 1e-6)

    # Attenuation function W(p) — Sommerfeld flat-earth approximation
    # W → 1 for small p (near field), W → 1/(2.5√p) for large p
    if p < 0.1:
        W = 1.0
    elif p < 2.0:
        W = 1.0 / (1.0 + 0.62 * p + 0.55 * p ** 1.5)
    elif p < 50.0:
        W = 1.0 / (2.5 * math.sqrt(p))
    else:
        W = max(1e-6, 1.0 / (2.5 * math.sqrt(p)))

    extra_db = -20.0 * math.log10(max(W, 1e-9))
    return fspl + extra_db


def _troposcatter_path_loss_db(distance_km: float, freq_mhz: float) -> float:
    """
    Tropospheric scatter path loss — empirical approximation of ITU-R P.617.
    Valid for UHF/SHF (100 MHz – 10 GHz), 100–2000 km beyond-horizon paths.
    Returns raw path loss (dB); antenna gains applied separately in link budget.
    """
    f_ghz = freq_mhz / 1000.0
    d = max(distance_km, 10.0)
    # Empirical formula derived from ITU-R P.617 median values:
    # L = 125 + 20·log(f_GHz) + 40·log(d_km)
    # Validated against real troposcatter links at 400 MHz–4 GHz, 200–800 km
    L = 125.0 + 20.0 * math.log10(max(f_ghz, 0.05)) + 40.0 * math.log10(d)
    return max(100.0, L)


def apply_wave_type(
    path_loss_db: float,
    wave_type: str,
    distance_km: float,
    freq_hz: float,
    tx_height_m: float,
    rx_height_m: float,
    sw_state=None,
    ground_type: str = "average",
) -> tuple[float, str, list[str]]:
    """
    Apply wave-type physics on top of the base propagation model path loss.

    Returns (corrected_path_loss_db, propagation_mode_label, warnings).

    wave_type values: 'auto' | 'los' | 'ground_wave' | 'skywave' | 'troposcatter'
    """
    freq_mhz = freq_hz / 1e6
    warnings: list[str] = []

    if not wave_type or wave_type == "auto":
        return path_loss_db, "auto", warnings

    # ── Line-of-sight: hard block beyond radio horizon ────────────────────
    elif wave_type == "los":
        # Smooth-Earth radio horizon (k=4/3 effective Earth radius)
        horizon_km = 4.12 * (math.sqrt(max(tx_height_m, 0.0)) +
                              math.sqrt(max(rx_height_m, 0.0)))
        if distance_km > horizon_km * 1.05:   # 5% tolerance for terrain diffraction
            return 300.0, "beyond_horizon", [
                f"Beyond radio horizon ({horizon_km:.0f} km) — no LOS path"
            ]
        return path_loss_db, "line_of_sight", warnings

    # ── Ground wave ───────────────────────────────────────────────────────
    elif wave_type == "ground_wave":
        if freq_mhz > 30:
            warnings.append(
                f"Ground wave has negligible range above 30 MHz "
                f"(got {freq_mhz:.0f} MHz) — adding 30 dB penalty"
            )
            return path_loss_db + 30.0, "ground_wave_oor", warnings

        gw_loss = _ground_wave_path_loss_db(distance_km, freq_mhz, ground_type)

        # Above 10 MHz terrain effects still matter; blend with terrain model
        if freq_mhz > 10:
            corrected = max(path_loss_db, gw_loss)
            mode = "ground_wave_hf"
        else:
            corrected = gw_loss   # ground wave dominates at LF/MF
            mode = "ground_wave"

        if freq_mhz < 3:
            warnings.append("LF/MF: ground wave range is primary — ionosphere irrelevant by day")
        elif freq_mhz < 10:
            warnings.append("Lower HF: ground wave range limited to tens–hundreds km; "
                            "switch to Skywave for long paths")
        return corrected, mode, warnings

    # ── Skywave / ionospheric ─────────────────────────────────────────────
    elif wave_type == "skywave":
        if freq_mhz < 3 or freq_mhz > 30:
            warnings.append(
                f"Skywave requires HF (3–30 MHz); {freq_mhz:.1f} MHz is out of range"
            )
            return path_loss_db + 40.0, "skywave_oor", warnings

        # Skip zone: ground wave fades, ionospheric wave not yet returned
        min_skip_km = 200.0 + (freq_mhz - 3) * 30  # rough: 200 km at 3 MHz, 1010 km at 30 MHz
        if distance_km < min_skip_km:
            return 250.0, "skip_zone", [
                f"In skip zone (< {min_skip_km:.0f} km at {freq_mhz:.1f} MHz) — "
                "neither ground wave nor skywave reaches here"
            ]

        # F2 layer height ~300 km; slant range for single hop
        F2_HEIGHT_KM = 300.0
        slant_km = math.sqrt(distance_km ** 2 + (2 * F2_HEIGHT_KM) ** 2)

        # Number of hops needed (each F2 hop covers ~3500 km max)
        MAX_HOP_KM = 3500.0
        hops = max(1, math.ceil(distance_km / MAX_HOP_KM))
        hop_extra_db = (hops - 1) * 22.0   # each extra hop adds ~22 dB (ground reflection + absorption)

        # Ionospheric absorption: ~10 dB per path at quiet sun; more at solar min/flare
        ionospheric_absorption_db = 10.0 * hops

        # Path loss = FSPL over slant range + ionospheric absorption + extra hops
        sky_loss = (fspl_db(slant_km * 1000, freq_hz)
                    + ionospheric_absorption_db
                    + hop_extra_db)

        # Space weather: MUF / LUF check
        if sw_state is not None:
            # Rough MUF from F10.7: quiet sun ~10 MHz, active ~22 MHz, max ~30 MHz
            muf_mhz = 5.0 + sw_state.f10_7 / 7.5
            luf_mhz = max(2.0, sw_state.kp_index * 0.6)

            if freq_mhz > muf_mhz:
                sky_loss += 30.0
                warnings.append(
                    f"Frequency {freq_mhz:.1f} MHz above estimated MUF "
                    f"({muf_mhz:.0f} MHz) — signal penetrating ionosphere"
                )
            elif freq_mhz < luf_mhz:
                sky_loss += 20.0
                warnings.append(
                    f"Frequency {freq_mhz:.1f} MHz below estimated LUF "
                    f"({luf_mhz:.1f} MHz) — heavy D-layer absorption"
                )
            elif sw_state.hf_blackout:
                sky_loss += 50.0
                warnings.append("HF radio blackout in progress — skywave severely disrupted")

            # Solar activity context
            if sw_state.is_solar_maximum:
                warnings.append("Solar maximum: MUF elevated, excellent skywave conditions")
            elif sw_state.is_solar_minimum:
                warnings.append("Solar minimum: MUF suppressed, skywave range reduced")

        if hops > 1:
            warnings.append(f"Long path: {hops}-hop skywave; each hop adds ~22 dB loss")

        mode = f"skywave_{hops}hop_F2"
        return sky_loss, mode, warnings

    # ── Troposcatter ──────────────────────────────────────────────────────
    elif wave_type == "troposcatter":
        if freq_mhz < 100:
            warnings.append(
                f"Troposcatter is most effective 100 MHz–10 GHz "
                f"({freq_mhz:.0f} MHz is below optimal range)"
            )
        if freq_mhz > 10000:
            warnings.append("Above 10 GHz: atmospheric absorption reduces troposcatter efficiency")

        # Radio horizon
        horizon_km = 4.12 * (math.sqrt(max(tx_height_m, 0.0)) +
                              math.sqrt(max(rx_height_m, 0.0)))

        if distance_km <= horizon_km:
            # Within LOS: use standard propagation model
            return path_loss_db, "troposcatter_los", warnings

        # Beyond LOS: troposcatter formula
        ts_loss = _troposcatter_path_loss_db(distance_km, freq_mhz)
        warnings.append(
            f"Troposcatter beyond-horizon link — typical TX power 1–10 kW, "
            f"large directional antennas required"
        )
        return ts_loss, "troposcatter", warnings

    return path_loss_db, "standard", warnings
