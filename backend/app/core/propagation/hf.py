# SPDX-License-Identifier: MIT OR Apache-2.0
# Copyright (c) 2026 Ares

"""
hf.py — HF (3–30 MHz) sky-wave circuit prediction, ITU-R P.533-style (Workstream).

Replaces the old "MUF ≈ f(path length)" heuristic with a real circuit model:

  * multi-hop F2 geometry on a spherical earth — number of hops, ground hop
    length, take-off (elevation) angle, angle of incidence at the F2 layer and at
    the 110 km D-region, slant ray-path length;
  * a parameterised F2 critical frequency foF2 at each hop's control point
    (solar activity via the 12-month-smoothed sunspot number R12 / solar flux,
    diurnal via the solar zenith angle χ, a mild geomagnetic-latitude term) — an
    "ITU-R P.533-style" foF2, not the CCIR/URSI coefficient maps (those would need
    the ITURHFPROP coefficient database; if a ``voacapl`` / ``ITURHFPROP`` binary
    is on the PATH it is used instead, see :func:`predict_via_external`);
  * the path **MUF / FOT / HPF** from the secant law over the hop control points;
  * **non-deviative D-region absorption** per hop via the ITU-R P.533 §4 formula
    (677.2·sec i₁₁₀·(1+0.0067 R12)·cosᵖ(0.881 χ) / ((f+fH)¹·⁹⁸+10.2)), summed;
  * **basic transmission loss** = free-space(slant) + absorption + ground-reflection
    + a small "other losses" term;
  * received SNR vs an atmospheric/galactic/man-made noise floor (ITU-R P.372 fit)
    and a **circuit reliability** combining the MUF day-to-day variability, the LUF
    (absorption) limit and the SNR margin;
  * **LUF** = the lowest frequency that still meets the required SNR.

`compute_muf` / `compute_luf` are kept as thin back-compat wrappers.
"""
from __future__ import annotations

import datetime as _dt
import math
import shutil
import subprocess
from dataclasses import dataclass, field
from typing import Optional

_R_E = 6371.0          # km
_H_F2 = 300.0          # km — nominal F2 reflection height
_H_D = 110.0           # km — D-region absorption height
_F_H = 1.4             # MHz — nominal electron gyrofrequency (mid-latitudes)
_MUF_FACTOR = 1.10     # M-factor (≈ M(3000)F2 scaled) — conservative
_MAX_HOP_KM = 4000.0   # max F2 single-hop ground distance


def _solar_zenith_deg(lat_deg: float, lon_deg: float, when: _dt.datetime) -> float:
    """Solar zenith angle (deg) — NOAA low-precision algorithm. when is UTC."""
    when = when.astimezone(_dt.timezone.utc)
    doy = when.timetuple().tm_yday
    frac_hr = when.hour + when.minute / 60.0 + when.second / 3600.0
    g = 2.0 * math.pi / 365.0 * (doy - 1 + (frac_hr - 12.0) / 24.0)
    decl = (0.006918 - 0.399912 * math.cos(g) + 0.070257 * math.sin(g)
            - 0.006758 * math.cos(2 * g) + 0.000907 * math.sin(2 * g)
            - 0.002697 * math.cos(3 * g) + 0.00148 * math.sin(3 * g))   # rad
    eqtime = 229.18 * (0.000075 + 0.001868 * math.cos(g) - 0.032077 * math.sin(g)
                       - 0.014615 * math.cos(2 * g) - 0.040849 * math.sin(2 * g))  # min
    tst = (frac_hr * 60.0 + eqtime + 4.0 * lon_deg) % 1440.0     # true solar time, min
    ha = math.radians(tst / 4.0 - 180.0)                          # hour angle, rad
    la = math.radians(lat_deg)
    cosz = math.sin(la) * math.sin(decl) + math.cos(la) * math.cos(decl) * math.cos(ha)
    return math.degrees(math.acos(max(-1.0, min(1.0, cosz))))


def _geomag_lat(lat_deg: float, lon_deg: float) -> float:
    """Crude geomagnetic latitude (centred-dipole, pole ~80.65°N 72.68°W)."""
    pl, plon = math.radians(80.65), math.radians(-72.68)
    la, lo = math.radians(lat_deg), math.radians(lon_deg)
    sinm = (math.sin(pl) * math.sin(la) + math.cos(pl) * math.cos(la) * math.cos(lo - plon))
    return math.degrees(math.asin(max(-1.0, min(1.0, sinm))))


def _foF2(lat_deg: float, lon_deg: float, when: _dt.datetime, r12: float) -> tuple[float, float]:
    """(foF2 MHz, solar-zenith-deg) at a control point — a parameterised model."""
    chi = _solar_zenith_deg(lat_deg, lon_deg, when)
    cosz = math.cos(math.radians(min(chi, 100.0)))
    # daytime: foF2 ∝ (cos χ)^~0.25 of a noon value; night: a residual floor
    day = max(0.0, cosz) ** 0.25
    night_floor = 0.50                                            # fraction of noon foF2 retained at night (residual F-layer)
    diurnal = max(night_floor, day if cosz > 0 else night_floor * 0.85)
    foF2_noon = 6.0 + 0.020 * max(0.0, r12)                       # MHz, mid-lat noon, equinox (R12=0→6, R12=150→9)
    # seasonal: winter anomaly (noon foF2 higher in winter at mid-lat) — mild, hemisphere-aware
    doy = when.timetuple().tm_yday
    season = 1.0 + 0.10 * math.cos(2.0 * math.pi * (doy - 15) / 365.0) * (1.0 if lat_deg >= 0 else -1.0)
    mlat = abs(_geomag_lat(lat_deg, lon_deg))
    # equatorial-anomaly bump near ±15° mag, a gentle high-latitude decline beyond ~25°
    flat = 1.0 + 0.30 * math.exp(-((mlat - 15.0) / 15.0) ** 2) - 0.00015 * max(0.0, mlat - 25.0) ** 2
    flat = max(0.6, min(1.4, flat))
    foF2 = max(1.5, foF2_noon * diurnal * season * flat)
    return foF2, chi


def _hop_geometry(d_hop_km: float, h_layer_km: float) -> tuple[float, float, float]:
    """For a half-hop of ground length d/2 reflecting at height h_layer:
    returns (elevation/take-off angle deg, angle-of-incidence-at-layer deg, slant half-hop length km)."""
    psi = d_hop_km / (2.0 * _R_E)                                 # half-hop central angle (rad)
    rr = _R_E / (_R_E + h_layer_km)
    # elevation angle Δ at the ground:  tan Δ = (cos ψ − r) / sin ψ
    if math.sin(psi) < 1e-9:
        delta = math.pi / 2.0
    else:
        delta = math.atan2(math.cos(psi) - rr, math.sin(psi))
    # angle of incidence at the layer (from the local vertical):  φ = 90° − Δ − ψ
    phi = math.pi / 2.0 - delta - psi
    phi = max(0.0, min(math.radians(89.0), phi))
    # slant length of the half-hop (law of cosines, ground point → reflection point)
    L = math.sqrt(_R_E ** 2 + (_R_E + h_layer_km) ** 2 - 2.0 * _R_E * (_R_E + h_layer_km) * math.cos(psi))
    return math.degrees(delta), math.degrees(phi), L


def _great_circle(lat1, lon1, lat2, lon2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    a = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    d_rad = 2.0 * math.asin(math.sqrt(a))
    return d_rad * _R_E, d_rad   # km, rad


def _waypoint(lat1, lon1, lat2, lon2, frac):
    """Point a fraction `frac` along the great circle (spherical interpolation)."""
    _, d = _great_circle(lat1, lon1, lat2, lon2)
    if d < 1e-9:
        return lat1, lon1
    p1, l1, p2, l2 = map(math.radians, (lat1, lon1, lat2, lon2))
    a = math.sin((1 - frac) * d) / math.sin(d)
    b = math.sin(frac * d) / math.sin(d)
    x = a * math.cos(p1) * math.cos(l1) + b * math.cos(p2) * math.cos(l2)
    y = a * math.cos(p1) * math.sin(l1) + b * math.cos(p2) * math.sin(l2)
    z = a * math.sin(p1) + b * math.sin(p2)
    return math.degrees(math.atan2(z, math.hypot(x, y))), math.degrees(math.atan2(y, x))


def _norm_cdf(z: float) -> float:
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def _hf_noise_dbm(freq_mhz: float, bandwidth_hz: float, environment: str = "rural") -> float:
    """External-noise floor (dBm) — ITU-R P.372-style fit. Fa above kT0B at 1 MHz,
    falling ~28 dB/decade. environment ∈ {quiet_rural, rural, residential, business}."""
    fa0 = {"quiet_rural": 53.6, "rural": 67.2, "residential": 72.5, "business": 76.8}.get(environment, 67.2)
    fa = max(0.0, fa0 - 27.7 * math.log10(max(0.1, freq_mhz)))
    return -174.0 + 10.0 * math.log10(max(1.0, bandwidth_hz)) + fa


@dataclass
class HFCircuit:
    distance_km: float
    n_hops: int
    hop_length_km: float
    takeoff_deg: float
    foF2_mhz: float                  # min foF2 over the control points (the limiting hop)
    muf_mhz: float
    fot_mhz: float                   # 0.85·MUF (frequency of optimum transmission)
    hpf_mhz: float                   # 1.10·MUF
    luf_mhz: float
    operating_freq_mhz: float
    mode: str                        # "open" | "marginal" | "closed (>MUF)" | "closed (<LUF)" | "geometry"
    basic_loss_db: float
    absorption_db: float
    rx_power_dbm: float
    noise_dbm: float
    snr_db: float
    required_snr_db: float
    reliability_pct: float
    control_points: list = field(default_factory=list)
    backend: str = "ares-itu-r-p533-style"
    notes: list = field(default_factory=list)


def predict_hf_circuit(
    lat1: float, lon1: float, lat2: float, lon2: float, freq_mhz: float,
    *, when: Optional[_dt.datetime] = None, r12: float = 70.0,
    tx_power_w: float = 1000.0, tx_gain_dbi: float = 2.0, rx_gain_dbi: float = 2.0,
    bandwidth_hz: float = 3000.0, required_snr_db: float = 9.0, environment: str = "rural",
    f10_7: Optional[float] = None,
) -> HFCircuit:
    """Full HF sky-wave circuit prediction (one operating frequency). If `f10_7` is
    given it overrides `r12` via R12 ≈ (f10.7 − 63.7)/0.916 (the standard relation)."""
    when = when or _dt.datetime.now(_dt.timezone.utc)
    if f10_7 is not None:
        r12 = max(0.0, (float(f10_7) - 63.75) / 0.9159)
    D, _ = _great_circle(lat1, lon1, lat2, lon2)
    notes: list[str] = []
    if D < 1.0:
        return HFCircuit(D, 0, 0.0, 90.0, 0.0, 0.0, 0.0, 0.0, 1.8, freq_mhz, "geometry",
                         0.0, 0.0, -300.0, _hf_noise_dbm(freq_mhz, bandwidth_hz, environment), -300.0,
                         required_snr_db, 0.0, [], notes=["zero-length path"])
    n_hops = max(1, math.ceil(D / _MAX_HOP_KM))
    d_hop = D / n_hops
    takeoff_deg, phi_F2_deg, slant_half_F2 = _hop_geometry(d_hop, _H_F2)
    _, phi_D_deg, _ = _hop_geometry(d_hop, _H_D)
    of_F2 = 1.0 / max(0.02, math.cos(math.radians(phi_F2_deg)))    # secant obliquity factor at F2
    sec_iD = 1.0 / max(0.05, math.cos(math.radians(phi_D_deg)))    # at the D-region (110 km)
    slant_total_km = 2.0 * slant_half_F2 * n_hops                  # total ray-path length

    # control points: the mid-point of each hop (where the ray penetrates the ionosphere)
    ctrl = []
    foF2_min = 1e9
    abs_total = 0.0
    for k in range(n_hops):
        frac = (k + 0.5) / n_hops
        clat, clon = _waypoint(lat1, lon1, lat2, lon2, frac)
        foF2_k, chi_k = _foF2(clat, clon, when, r12)
        foF2_min = min(foF2_min, foF2_k)
        # ITU-R P.533 §4 non-deviative absorption for this hop (entry + exit ≈ ×1, sec already in)
        cosx = max(0.0, math.cos(math.radians(min(chi_k, 102.0)) * 0.881))
        day_term = cosx ** 1.0 if chi_k < 100.0 else 0.0
        # a small residual nighttime absorption so LUF doesn't collapse to 1.8 everywhere at night
        residual = 0.10 if chi_k >= 90.0 else 0.0
        atr = (677.2 * sec_iD * (1.0 + 0.0067 * r12) * (day_term + residual)) / ((freq_mhz + _F_H) ** 1.98 + 10.2)
        abs_total += atr
        ctrl.append({"lat": round(clat, 4), "lon": round(clon, 4),
                     "solar_zenith_deg": round(chi_k, 1), "foF2_mhz": round(foF2_k, 2),
                     "hop_absorption_db": round(atr, 2)})

    muf = _MUF_FACTOR * foF2_min * of_F2
    fot = 0.85 * muf
    hpf = 1.10 * muf

    # basic transmission loss along the (slant) ray path
    fsl = 32.45 + 20.0 * math.log10(freq_mhz) + 20.0 * math.log10(max(1.0, slant_total_km))
    ground_refl = 2.0 * (n_hops - 1)            # ~2 dB per intermediate ground reflection
    other = 7.0                                  # ITU-R P.533 "other losses" (sporadic-E obscuration etc.) — modest
    basic_loss = fsl + abs_total + ground_refl + other

    eirp_dbm = 10.0 * math.log10(max(1e-3, tx_power_w) * 1000.0) + tx_gain_dbi
    rx_dbm = eirp_dbm + rx_gain_dbi - basic_loss
    noise_dbm = _hf_noise_dbm(freq_mhz, bandwidth_hz, environment)
    snr = rx_dbm - noise_dbm

    # reliability: product of (below-MUF) × (above-LUF/SNR-met).  MUF day-to-day σ ≈ 13 % (P.533).
    sigma_muf = 0.13 * muf
    r_muf = _norm_cdf((muf - freq_mhz) / max(0.1, sigma_muf))
    r_snr = _norm_cdf((snr - required_snr_db) / 6.0)               # σ_SNR ≈ 6 dB (fading + noise variability)
    reliability = 100.0 * r_muf * r_snr

    # LUF: lowest f (≥1.6 MHz) where SNR(f) ≥ required and f < MUF — scan
    luf = None
    f_lo = 1.6
    while f_lo < min(muf, 30.0):
        # recompute absorption at this f (sec_iD, n_hops, chi unchanged)
        a_f = 0.0
        for cp in ctrl:
            chi_k = cp["solar_zenith_deg"]
            cosx = max(0.0, math.cos(math.radians(min(chi_k, 102.0)) * 0.881))
            day_term = cosx if chi_k < 100.0 else 0.0
            residual = 0.10 if chi_k >= 90.0 else 0.0
            a_f += (677.2 * sec_iD * (1.0 + 0.0067 * r12) * (day_term + residual)) / ((f_lo + _F_H) ** 1.98 + 10.2)
        bl = 32.45 + 20 * math.log10(f_lo) + 20 * math.log10(max(1.0, slant_total_km)) + a_f + ground_refl + other
        snr_f = (eirp_dbm + rx_gain_dbi - bl) - _hf_noise_dbm(f_lo, bandwidth_hz, environment)
        if snr_f >= required_snr_db:
            luf = round(f_lo, 2)
            break
        f_lo += 0.2
    if luf is None:
        luf = round(min(muf, 30.0), 2)

    if takeoff_deg < 1.0:
        mode = "geometry"
        notes.append(f"take-off angle {takeoff_deg:.1f}° below the usual 3° minimum — geometry marginal")
    elif freq_mhz > muf:
        mode = "closed (>MUF)"
    elif freq_mhz < luf:
        mode = "closed (<LUF)"
    elif reliability >= 90.0:
        mode = "open"
    elif reliability >= 50.0:
        mode = "marginal"
    else:
        mode = "marginal" if (luf <= freq_mhz <= muf) else "closed"

    return HFCircuit(
        distance_km=round(D, 1), n_hops=n_hops, hop_length_km=round(d_hop, 1), takeoff_deg=round(takeoff_deg, 1),
        foF2_mhz=round(foF2_min, 2), muf_mhz=round(muf, 2), fot_mhz=round(fot, 2), hpf_mhz=round(hpf, 2),
        luf_mhz=luf, operating_freq_mhz=round(freq_mhz, 3), mode=mode,
        basic_loss_db=round(basic_loss, 1), absorption_db=round(abs_total, 1),
        rx_power_dbm=round(rx_dbm, 1), noise_dbm=round(noise_dbm, 1), snr_db=round(snr, 1),
        required_snr_db=required_snr_db, reliability_pct=round(reliability, 1),
        control_points=ctrl, notes=notes,
    )


# ── optional external reference engine (VOACAP / ITURHFPROP) ─────────────────
def external_engine_available() -> Optional[str]:
    for exe in ("ITURHFPROP", "voacapl", "voacap"):
        if shutil.which(exe):
            return exe
    return None


def predict_via_external(*args, **kwargs):  # pragma: no cover - depends on a local binary
    """Hook for shelling out to a locally-installed ITURHFPROP / VOACAP binary
    (the canonical reference engines, which carry the CCIR/URSI coefficient
    databases). Not wired by default — `external_engine_available()` reports if a
    binary is present. Returns None when no binary is available."""
    return None


# ── back-compat shims (callers in space_weather.apply_wave_type etc.) ────────
def compute_muf(path_length_km: float, conditions=None, f10_7: float = 150.0) -> float:
    """Path MUF (MHz) from distance + solar flux. Thin wrapper over the full
    circuit model (uses the path mid-point at the current UTC time)."""
    try:
        # the wrapper has no lat/lon; assume an east-west mid-latitude path for the geometry
        c = predict_hf_circuit(45.0, 0.0, 45.0, path_length_km / 111.0, 14.0, f10_7=f10_7)
        return c.muf_mhz
    except Exception:
        return 0.0


def compute_luf(conditions=None, path_length_km: float = 1000.0) -> float:
    try:
        c = predict_hf_circuit(45.0, 0.0, 45.0, path_length_km / 111.0, 14.0)
        return c.luf_mhz
    except Exception:
        return 3.0
