# SPDX-License-Identifier: MIT OR Apache-2.0
# Copyright (c) 2026 Ares

"""
Space Weather & Ionospheric Conditions
Fetches real-time data from NOAA SWPC and applies effects to HF/VHF propagation.

Data sources:
  - NOAA SWPC JSON feeds (no API key required)
  - Solar flux index (F10.7), Kp index, X-ray flux
  - Alerts for radio blackouts, geomagnetic storms
"""
import json
import math
import asyncio
import logging
import datetime
from dataclasses import dataclass, field
from typing import Optional
import aiohttp

from app.config import DATA_DIR

log = logging.getLogger(__name__)

# Disk cache — persists across restarts so offline use gets last real data
DISK_CACHE_PATH = DATA_DIR / "space_weather_cache.json"

# NOAA SWPC endpoints (all free, no key needed)
SWPC_BASE = "https://services.swpc.noaa.gov"
ENDPOINTS = {
    "kp_1min": f"{SWPC_BASE}/json/planetary_k_index_1m.json",
    "kp_3hr": f"{SWPC_BASE}/products/noaa-planetary-k-index.json",
    "solar_flux": f"{SWPC_BASE}/json/f107_cm_flux.json",
    "xray": f"{SWPC_BASE}/json/goes/primary/xrays-7-day.json",
    "alerts": f"{SWPC_BASE}/products/alerts.json",
    "aurora_30min": f"{SWPC_BASE}/products/noaa-aurora-forecast-30min.json",
    "solar_wind": f"{SWPC_BASE}/products/solar-wind/plasma-7-day.json",
    "geomag": f"{SWPC_BASE}/products/Geomag/GeomagForecast.json",
    "iono": f"{SWPC_BASE}/products/ionosphere/total-electron-content.json",
}

# Cache to avoid hammering NOAA
_cache: dict = {}
_cache_expiry: dict = {}
CACHE_TTL_SECONDS = 300  # 5-minute cache


@dataclass
class SpaceWeatherState:
    """Current space weather conditions relevant to RF propagation."""
    # Solar
    f10_7: float = 150.0           # Solar flux index (sfu, 10^-22 W/m²/Hz)
    f10_7_81day: float = 150.0     # 81-day average (for ionospheric models)
    sunspot_number: float = 50.0

    # Geomagnetic
    kp_index: float = 2.0          # Planetary K index (0–9)
    ap_index: float = 7.0          # Ap (linear equiv. of Kp)
    dst_index: float = 0.0         # Dst (storm strength, nT)

    # X-ray / Radio blackout
    xray_flux: float = 1e-8        # W/m² (background ~1e-8, X1 = 1e-4)
    radio_blackout_class: str = "None"  # None, R1..R5

    # Geomagnetic storm
    storm_class: str = "None"      # None, G1..G5
    aurora_activity: str = "Low"   # Low, Medium, High, Severe

    # Derived
    timestamp: Optional[datetime.datetime] = None
    is_solar_minimum: bool = False
    is_solar_maximum: bool = False

    # Propagation effects (computed)
    hf_blackout: bool = False       # Complete HF blackout (X-class flare)
    polar_cap_absorption: bool = False  # PCA event
    ionospheric_storm: bool = False

    def __post_init__(self):
        if self.timestamp is None:
            self.timestamp = datetime.datetime.utcnow()
        self._update_derived()

    def _update_derived(self):
        # X-ray class
        if self.xray_flux >= 1e-3:
            self.radio_blackout_class = "R5"
            self.hf_blackout = True
        elif self.xray_flux >= 5e-4:
            self.radio_blackout_class = "R4"
            self.hf_blackout = True
        elif self.xray_flux >= 1e-4:
            self.radio_blackout_class = "R3"
            self.hf_blackout = True
        elif self.xray_flux >= 5e-5:
            self.radio_blackout_class = "R2"
        elif self.xray_flux >= 1e-5:
            self.radio_blackout_class = "R1"
        else:
            self.radio_blackout_class = "None"

        # Geomagnetic storm class
        if self.kp_index >= 9:
            self.storm_class = "G5"
        elif self.kp_index >= 8:
            self.storm_class = "G4"
        elif self.kp_index >= 7:
            self.storm_class = "G3"
        elif self.kp_index >= 6:
            self.storm_class = "G2"
        elif self.kp_index >= 5:
            self.storm_class = "G1"
        else:
            self.storm_class = "None"

        # Aurora / PCA
        self.polar_cap_absorption = self.kp_index >= 7
        self.ionospheric_storm = self.kp_index >= 5

        if self.kp_index >= 7:
            self.aurora_activity = "Severe"
        elif self.kp_index >= 5:
            self.aurora_activity = "High"
        elif self.kp_index >= 3:
            self.aurora_activity = "Medium"
        else:
            self.aurora_activity = "Low"

        self.is_solar_maximum = self.f10_7 > 180
        self.is_solar_minimum = self.f10_7 < 80

    def propagation_summary(self) -> dict:
        """Human-readable summary of propagation conditions."""
        return {
            "solar_flux_f107": self.f10_7,
            "kp_index": self.kp_index,
            "storm_class": self.storm_class,
            "radio_blackout": self.radio_blackout_class,
            "hf_propagation": self._hf_cond(),
            "aurora_activity": self.aurora_activity,
            "polar_cap_absorption": self.polar_cap_absorption,
            "ionospheric_storm": self.ionospheric_storm,
            "vhf_sporadic_e_likely": self._sporadic_e_likely(),
            "timestamp_utc": self.timestamp.isoformat() if self.timestamp else None,
        }

    def _hf_cond(self) -> str:
        if self.hf_blackout:
            return "BLACKOUT — HF unusable (solar flare)"
        elif self.polar_cap_absorption:
            return "POOR — Polar cap absorption (high lat paths)"
        elif self.ionospheric_storm:
            return "DISTURBED — Geomagnetic storm affecting ionosphere"
        elif self.f10_7 > 180:
            return "EXCELLENT — Solar max, high MUF"
        elif self.f10_7 > 130:
            return "GOOD — Active sun"
        elif self.f10_7 > 80:
            return "FAIR — Moderate solar activity"
        else:
            return "POOR — Solar minimum, low ionospheric density"

    def _sporadic_e_likely(self) -> bool:
        """Sporadic-E more common May–August and Oct–Nov, late afternoon."""
        if self.timestamp is None:
            return False
        month = self.timestamp.month
        hour = self.timestamp.hour
        return month in (5, 6, 7, 8, 10, 11) and 14 <= hour <= 20


def _save_disk_cache(state: 'SpaceWeatherState') -> None:
    """Persist space weather state to disk for offline use."""
    try:
        payload = {
            "f10_7": state.f10_7,
            "f10_7_81day": state.f10_7_81day,
            "sunspot_number": state.sunspot_number,
            "kp_index": state.kp_index,
            "ap_index": state.ap_index,
            "dst_index": state.dst_index,
            "xray_flux": state.xray_flux,
            "timestamp_utc": state.timestamp.isoformat() if state.timestamp else None,
        }
        tmp = DISK_CACHE_PATH.with_suffix('.tmp')
        tmp.write_text(json.dumps(payload))
        tmp.replace(DISK_CACHE_PATH)  # atomic
    except Exception as e:
        log.debug(f"Space weather disk cache write failed: {e}")


def _load_disk_cache() -> Optional['SpaceWeatherState']:
    """Load last-known space weather from disk. Returns None if unavailable."""
    try:
        if not DISK_CACHE_PATH.exists():
            return None
        payload = json.loads(DISK_CACHE_PATH.read_text())
        state = SpaceWeatherState(
            f10_7=float(payload.get("f10_7", 150)),
            f10_7_81day=float(payload.get("f10_7_81day", 150)),
            sunspot_number=float(payload.get("sunspot_number", 50)),
            kp_index=float(payload.get("kp_index", 2)),
            ap_index=float(payload.get("ap_index", 7)),
            dst_index=float(payload.get("dst_index", 0)),
            xray_flux=float(payload.get("xray_flux", 1e-8)),
        )
        ts = payload.get("timestamp_utc")
        state.timestamp = datetime.datetime.fromisoformat(ts) if ts else datetime.datetime.utcnow()
        state._update_derived()
        log.info("Space weather loaded from disk cache (offline mode)")
        return state
    except Exception as e:
        log.debug(f"Space weather disk cache read failed: {e}")
        return None


async def fetch_space_weather(session: Optional[aiohttp.ClientSession] = None) -> SpaceWeatherState:
    """
    Fetch current space weather from NOAA SWPC.
    Returns SpaceWeatherState with real-time data.
    Falls back to last disk-cached data if NOAA is unreachable.
    Falls back to built-in defaults if no disk cache exists.
    """
    now = datetime.datetime.utcnow()
    cache_key = "space_weather"

    # Check in-memory cache
    if (cache_key in _cache and
            cache_key in _cache_expiry and
            (now - _cache_expiry[cache_key]).total_seconds() < CACHE_TTL_SECONDS):
        return _cache[cache_key]

    state = SpaceWeatherState()  # defaults
    network_ok = False

    own_session = session is None
    if own_session:
        timeout = aiohttp.ClientTimeout(total=10)
        session = aiohttp.ClientSession(timeout=timeout)

    try:
        # Fetch solar flux
        try:
            async with session.get(ENDPOINTS["solar_flux"]) as r:
                if r.status == 200:
                    data = await r.json(content_type=None)
                    if isinstance(data, list) and data:
                        last = data[-1]
                        state.f10_7 = float(last.get("flux", 150))
                        network_ok = True
        except Exception as e:
            log.debug(f"Solar flux fetch failed: {e}")

        # Fetch Kp index
        try:
            async with session.get(ENDPOINTS["kp_3hr"]) as r:
                if r.status == 200:
                    data = await r.json(content_type=None)
                    if isinstance(data, list) and len(data) > 1:
                        last = data[-1]
                        state.kp_index = float(last[1]) if isinstance(last, list) else 2.0
                        network_ok = True
        except Exception as e:
            log.debug(f"Kp fetch failed: {e}")

        # Fetch X-ray flux
        try:
            async with session.get(ENDPOINTS["xray"]) as r:
                if r.status == 200:
                    data = await r.json(content_type=None)
                    if isinstance(data, list) and data:
                        last = data[-1]
                        flux = float(last.get("flux", 1e-8) or 1e-8)
                        state.xray_flux = flux
        except Exception as e:
            log.debug(f"X-ray fetch failed: {e}")

        # Fetch alerts for summary
        try:
            async with session.get(ENDPOINTS["alerts"]) as r:
                if r.status == 200:
                    alerts = await r.json(content_type=None)
                    state._alerts = [a.get("message", "") for a in (alerts or [])
                                     if isinstance(a, dict)][:5]
        except Exception as e:
            log.debug(f"Alerts fetch failed: {e}")

        state.timestamp = now
        state._update_derived()

    finally:
        if own_session:
            await session.close()

    if network_ok:
        # Save to disk so offline restarts get the last real reading
        _save_disk_cache(state)
    else:
        # Try last disk-persisted data before falling back to SpaceWeatherState defaults
        cached = _load_disk_cache()
        if cached is not None:
            state = cached

    _cache[cache_key] = state
    _cache_expiry[cache_key] = now
    return state


def apply_space_weather_corrections(
    path_loss_db: float,
    freq_hz: float,
    space_weather: SpaceWeatherState,
    lat1: float,
    lat2: float,
    path_length_km: float,
) -> tuple[float, list[str]]:
    """
    Apply space weather propagation corrections to path loss.
    Returns (corrected_path_loss_db, list of warning messages).
    """
    warnings = []
    correction_db = 0.0
    freq_mhz = freq_hz / 1e6

    # HF band (3–30 MHz)
    if 3 <= freq_mhz <= 30:
        if space_weather.hf_blackout:
            correction_db += 100.0  # Effectively blocked
            warnings.append(f"⚠ RADIO BLACKOUT ({space_weather.radio_blackout_class}): "
                            "HF path severely attenuated by solar X-ray flare")

        elif space_weather.polar_cap_absorption:
            # PCA affects polar paths
            avg_lat = (abs(lat1) + abs(lat2)) / 2
            if avg_lat > 60:
                correction_db += 30.0
                warnings.append("⚠ Polar Cap Absorption event — high-latitude HF degraded")

        elif space_weather.ionospheric_storm:
            correction_db += 10.0 + space_weather.kp_index * 2.0
            warnings.append(f"⚠ Geomagnetic storm ({space_weather.storm_class}) — "
                            "ionosphere disturbed")

        # Solar activity affects MUF
        if space_weather.is_solar_maximum:
            warnings.append("✓ Solar maximum — high MUF, good long-distance HF likely")
        elif space_weather.is_solar_minimum:
            warnings.append("⚠ Solar minimum — reduced ionospheric density, lower MUF")

    # MF band (300 kHz – 3 MHz)
    elif 0.3 <= freq_mhz < 3:
        if space_weather.hf_blackout:
            correction_db += 20.0
            warnings.append("⚠ D-layer absorption elevated by solar flare (MF affected)")

    # VHF+ (> 30 MHz) — mainly geomagnetic scintillation
    elif freq_mhz > 30:
        if space_weather.kp_index > 5:
            # Ionospheric scintillation at VHF
            correction_db += (space_weather.kp_index - 5) * 1.5
            warnings.append(f"⚠ Geomagnetic storm may cause VHF/UHF scintillation")

        # Sporadic-E can enhance VHF propagation
        if space_weather._sporadic_e_likely() and 30 <= freq_mhz <= 200:
            correction_db -= 5.0  # Enhancement
            warnings.append("✓ Sporadic-E conditions possible — enhanced VHF propagation")

    return path_loss_db + correction_db, warnings


def kp_to_ap(kp: float) -> float:
    """Convert Kp index to Ap index (ITU-R formula)."""
    table = [0, 2, 3, 4, 5, 6, 7, 9, 12, 15, 18, 22, 27, 32, 39,
             48, 56, 67, 80, 94, 111, 132, 154, 179, 207, 236, 300, 400]
    kp_steps = [0.0 + 0.333 * i for i in range(len(table))]
    # Linear interpolation
    kp = max(0.0, min(9.0, kp))
    for i in range(len(kp_steps) - 1):
        if kp_steps[i] <= kp < kp_steps[i + 1]:
            t = (kp - kp_steps[i]) / (kp_steps[i + 1] - kp_steps[i])
            return table[i] * (1 - t) + table[i + 1] * t
    return float(table[-1])
