"""
Coverage Simulation Engine
Orchestrates terrain, propagation, atmosphere, antenna, and space weather
to compute coverage areas, point-to-point links, and terrain profiles.

GPU acceleration: radial sweep loop uses CuPy or NumPy batch operations.
Cache management: automatically purges terrain/building data older than TTL.
"""
import asyncio
import math
import time
import logging
import datetime
import os
import shutil
from pathlib import Path
from dataclasses import dataclass, field
from typing import Optional
import numpy as np

try:
    import cupy as cp
    GPU_AVAILABLE = True
except ImportError:
    cp = None
    GPU_AVAILABLE = False

from app.config import settings, TERRAIN_CACHE_DIR, BUILDINGS_CACHE_DIR
# Authoritative ITS Longley-Rice port (the same algorithm SPLAT! / Radio Mobile /
# the FCC use). `app.core.propagation.itm` remains as the legacy fast/empirical impl.
from app.core.propagation.itm_its import compute_itm_path_loss, CLIMATE_CONTINENTAL_TEMPERATE
from app.core.propagation.models import (
    PropagationModel, WaveType, LinkBudget, select_model,
    fspl_db, oxygen_absorption_db_per_km, water_vapour_absorption_db_per_km,
    rain_attenuation_db_per_km, apply_wave_type,
)
from app.core.propagation.diffraction import compute_diffraction_db
from app.core.propagation.terrain import TerrainManager, haversine_distance, destination_point
from app.core.geolocation import initial_bearing
from app.core.propagation.atmosphere import (
    AtmosphericConditions, compute_atmospheric_loss,
    get_surface_refractivity, altitude_to_pressure, altitude_to_temperature_c,
    radio_horizon_distance_km, effective_earth_radius_km,
)
from app.core.propagation.antenna import AntennaConfig, AntennaType, get_antenna_gain_dbi, get_antenna_beamwidth
from app.core.propagation.polar_patterns import polar_pattern_gain_db, POLAR_PATTERNS
from functools import lru_cache
from app.core.propagation.space_weather import (
    SpaceWeatherState, fetch_space_weather, apply_space_weather_corrections,
)

log = logging.getLogger(__name__)


@lru_cache(maxsize=512)
def _swept_arc_avg_gain_db(pattern_id: str, sweep_deg: float, samples: int = 72) -> float:
    """
    Time-/azimuth-averaged gain (dBi) of a polar pattern across a sweep arc
    centered on boresight. Cached because the same (pattern_id, sweep) recurs
    for every distance sample on every radial in a coverage run.

    sweep_deg <= 0 → caller should not invoke this (no sweep).
    sweep_deg >= 360 → 360° average (effectively omni).
    """
    if sweep_deg <= 0:
        return polar_pattern_gain_db(pattern_id, 0.0)
    half = min(sweep_deg, 360.0) / 2.0
    # Sample symmetrically around boresight; average in linear power, return dB.
    lin_sum = 0.0
    for s in range(samples):
        deg = -half + (s + 0.5) * (2.0 * half) / samples
        lin_sum += 10.0 ** (polar_pattern_gain_db(pattern_id, deg) / 10.0)
    avg_lin = lin_sum / samples
    return 10.0 * math.log10(max(avg_lin, 1e-12))


def _polar_pattern_gain_with_sweep_db(
    pattern_id: str, az_offset_deg: float, sweep_deg: float
) -> float:
    """
    Effective azimuth gain for a (possibly scanning) radar.
    - sweep_deg ≤ 0   → focused: raw pattern at this offset.
    - sweep_deg ≥ 360 → omni-equivalent: 360° averaged gain.
    - else, |off| ≤ sweep/2 → averaged across the swept arc.
    - else                 → raw pattern at this offset (out-of-sweep direction
      sees only side/back lobes; we don't smear those further).
    """
    if sweep_deg <= 0:
        return polar_pattern_gain_db(pattern_id, az_offset_deg)
    if sweep_deg >= 360:
        return _swept_arc_avg_gain_db(pattern_id, 360.0)
    if abs(az_offset_deg) <= sweep_deg / 2.0:
        return _swept_arc_avg_gain_db(pattern_id, sweep_deg)
    return polar_pattern_gain_db(pattern_id, az_offset_deg)

# Cache TTL (seconds). Files older than this are auto-deleted.
TERRAIN_CACHE_TTL_SECONDS = 30 * 24 * 3600  # 30 days
BUILDINGS_CACHE_TTL_SECONDS = 7 * 24 * 3600  # 7 days


# ─────────────────────────────────────────────────────────────────────────────
# Automatic cache management
# ─────────────────────────────────────────────────────────────────────────────

def purge_stale_cache(cache_dir: Path, ttl_seconds: int) -> int:
    """
    Delete files in cache_dir older than ttl_seconds.
    Returns number of files deleted.
    """
    now = time.time()
    deleted = 0
    if not cache_dir.exists():
        return 0
    for root, dirs, files in os.walk(cache_dir):
        for fname in files:
            fpath = Path(root) / fname
            try:
                mtime = fpath.stat().st_mtime
                if (now - mtime) > ttl_seconds:
                    fpath.unlink()
                    deleted += 1
            except Exception:
                pass
    return deleted


def purge_all_stale_caches():
    """Run automatic cache cleanup. Called at startup and periodically.
    Terrain tiles are NEVER purged — SRTM data is static and must be available
    offline. Old terrain tiles remain until explicitly replaced by a fresh download."""
    b = purge_stale_cache(BUILDINGS_CACHE_DIR, BUILDINGS_CACHE_TTL_SECONDS)
    if b:
        log.info(f"Cache cleanup: deleted {b} building files")


async def periodic_cache_cleanup(interval_hours: float = 24.0):
    """Background task that runs cache cleanup every interval_hours."""
    while True:
        await asyncio.sleep(interval_hours * 3600)
        purge_all_stale_caches()


# ─────────────────────────────────────────────────────────────────────────────
# Request / Response schemas
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class TransmitterConfig:
    lat: float = 37.7749
    lon: float = -122.4194
    height_m: float = settings.default_emitter_agl_m  # height above ground (m) - 6ft default
    altitude_m: float = 0.0          # site altitude ASL (m)
    power_dbm: float = 27.0          # transmit power (dBm)
    frequency_hz: float = 433e6
    antenna: AntennaConfig = field(default_factory=AntennaConfig)


@dataclass
class ReceiverConfig:
    height_m: float = 1.5
    altitude_m: float = 0.0          # e.g. for airborne receiver (up to 9144m=30kft)
    sensitivity_dbm: float = -100.0
    antenna: AntennaConfig = field(default_factory=AntennaConfig)
    noise_figure_db: float = 3.0
    required_snr_db: float = 10.0


@dataclass
class CoverageRequest:
    transmitter: TransmitterConfig = field(default_factory=TransmitterConfig)
    receiver: ReceiverConfig = field(default_factory=ReceiverConfig)
    propagation_model: PropagationModel = PropagationModel.ITM
    wave_type: str = "auto"          # auto | los | ground_wave | skywave | troposcatter
    radius_km: float = 50.0
    num_radials: int = 360           # number of azimuth directions
    points_per_radial: int = 300     # terrain samples per radial
    min_signal_dbm: float = -120.0   # threshold for coverage
    # Atmospheric
    atmosphere: Optional[dict] = None
    # Options
    use_gpu: bool = False
    terrain_resolution: str = "srtm3"   # srtm1, srtm3
    include_buildings: bool = False
    fetch_space_weather: bool = True
    # Time
    utc_datetime: Optional[str] = None  # ISO format
    # Context: 1=urban/conservative/D-layer, 2=average/E-layer, 3=rural/optimistic/F-layer
    context: int = 2
    # Diffraction model (None = disabled, 'deygout', 'bullington', etc.)
    diffraction_model: Optional[str] = None
    # Radar RCS (m²) — used when propagation_model == RADAR
    rcs_m2: float = 1.0
    # Additional clutter height above terrain (m) — added to terrain profile
    clutter_height_m: float = 0.0
    # Polar (azimuth-plane) radiation pattern applied on top of the antenna's
    # intrinsic gain pattern.  See polar_patterns.POLAR_PATTERNS for ids.
    # "omni" preserves the antenna's natural pattern unchanged.
    polar_pattern: str = "omni"
    # Peak antenna gain (dBi).  When set, this overrides the antenna config's
    # gain_dbi and is used as the boresight gain for the polar pattern.
    # None → fall back to the antenna config's own gain.
    polar_peak_gain_dbi: Optional[float] = None
    # Scanning-radar sweep arc (deg). 0 = focused. 360 = omni-equivalent.
    # Otherwise the polar pattern is azimuth-averaged across this arc centered
    # on the antenna boresight; outside the arc the raw pattern is used.
    sweep_deg: float = 0.0
    # OSM building fetch radius (metres) when include_buildings is True
    buildings_radius_m: float = 500.0


@dataclass
class PointToPointRequest:
    transmitter: TransmitterConfig = field(default_factory=TransmitterConfig)
    receiver_lat: float = 37.9
    receiver_lon: float = -122.0
    receiver_height_m: float = 1.5
    receiver_altitude_m: float = 0.0
    propagation_model: PropagationModel = PropagationModel.ITM
    wave_type: str = "auto"
    atmosphere: Optional[dict] = None
    use_gpu: bool = False
    fetch_space_weather: bool = True
    utc_datetime: Optional[str] = None
    num_profile_points: int = 512
    context: int = 2
    diffraction_model: Optional[str] = None
    rcs_m2: float = 1.0
    clutter_height_m: float = 0.0


@dataclass
class CoveragePoint:
    lat: float
    lon: float
    distance_m: float
    signal_dbm: float
    path_loss_db: float
    is_covered: bool
    propagation_mode: str = "unknown"


@dataclass
class CoverageResult:
    points: list[CoveragePoint] = field(default_factory=list)
    transmitter: Optional[TransmitterConfig] = None
    max_range_km: float = 0.0
    avg_signal_dbm: float = 0.0
    covered_area_km2: float = 0.0
    space_weather: Optional[dict] = None
    warnings: list[str] = field(default_factory=list)
    computation_time_s: float = 0.0
    gpu_used: bool = False
    geojson: Optional[dict] = None


@dataclass
class TerrainProfileResult:
    distances_m: list[float] = field(default_factory=list)
    elevations_m: list[float] = field(default_factory=list)
    los_heights_m: list[float] = field(default_factory=list)
    fresnel_radii_m: list[float] = field(default_factory=list)
    total_distance_m: float = 0.0
    path_loss_db: float = 0.0
    received_signal_dbm: float = 0.0
    link_budget: Optional[dict] = None
    propagation_mode: str = "unknown"
    warnings: list[str] = field(default_factory=list)
    space_weather: Optional[dict] = None


# ─────────────────────────────────────────────────────────────────────────────
# Main simulation engine
# ─────────────────────────────────────────────────────────────────────────────

class RFSimulator:
    """
    RF Propagation Simulation Engine.
    Handles coverage area, point-to-point link budgets, and terrain profiles.
    """

    def __init__(self):
        self._terrain = TerrainManager()
        self._sw_cache: Optional[SpaceWeatherState] = None
        self._sw_cache_time: float = 0.0
        # Run initial cache purge
        purge_all_stale_caches()

    async def compute_coverage(self, req: CoverageRequest) -> CoverageResult:
        """
        Compute RF coverage area using radial sweep.
        Returns list of coverage points that can be rendered as a heatmap.
        GPU-accelerated batch FSPL + ITM path computation.
        """
        t_start = time.time()
        result = CoverageResult(transmitter=req.transmitter)

        # Parse UTC time
        utc_time = self._parse_time(req.utc_datetime)

        # Atmospheric conditions
        atm = self._build_atmosphere(req.atmosphere, req.transmitter, utc_time)
        surface_N = get_surface_refractivity(req.transmitter.lat, req.transmitter.altitude_m)

        # Space weather
        sw_state = None
        if req.fetch_space_weather:
            try:
                sw_state = await fetch_space_weather()
                result.space_weather = sw_state.propagation_summary()
            except Exception as e:
                log.warning(f"Space weather fetch failed: {e}")

        # Terrain manager
        terrain = TerrainManager(resolution=req.terrain_resolution,
                                  use_gpu=req.use_gpu and GPU_AVAILABLE)

        tx = req.transmitter
        rx = req.receiver
        freq_hz = tx.frequency_hz
        freq_mhz = freq_hz / 1e6

        # GPU acceleration check
        use_gpu = req.use_gpu and GPU_AVAILABLE
        result.gpu_used = use_gpu

        try:
            # ── Determine beam parameters ────────────────────────────────────────
            # The polar pattern applies a smooth, physically motivated relative
            # gain (peak = 0 dB) at every azimuth offset from boresight.  No
            # hard cutoffs anywhere — even highly directional patterns have
            # finite side / back lobes.
            polar_pattern_id = req.polar_pattern if req.polar_pattern in POLAR_PATTERNS else "omni"

            # Always sweep full 360° so beam edges decay naturally.
            azimuths = np.linspace(0, 360, req.num_radials, endpoint=False)

            coverage_points: list[CoveragePoint] = []
            max_ranges: list[float] = []
            all_signals: list[float] = []

            # Process radials concurrently in batches
            batch_size = 36  # 10° batches for memory efficiency
            for batch_start in range(0, len(azimuths), batch_size):
                batch_azs = azimuths[batch_start:batch_start + batch_size]
                tasks = [
                    self._compute_radial(
                        tx, rx, az, req.radius_km, req.points_per_radial,
                        freq_hz, req.propagation_model, req.wave_type,
                        atm, surface_N, sw_state, terrain,
                        req.min_signal_dbm, use_gpu, req.include_buildings,
                        buildings_radius_m=req.buildings_radius_m,
                        context=req.context,
                        diffraction_model=req.diffraction_model,
                        rcs_m2=req.rcs_m2,
                        clutter_height_m=req.clutter_height_m,
                        polar_pattern=polar_pattern_id,
                        polar_peak_gain_dbi=req.polar_peak_gain_dbi,
                        sweep_deg=req.sweep_deg,
                    )
                    for az in batch_azs
                ]
                batch_results = await asyncio.gather(*tasks)
                for pts in batch_results:
                    coverage_points.extend(pts)
                    if pts:
                        covered = [p for p in pts if p.is_covered]
                        if covered:
                            max_ranges.append(max(p.distance_m for p in covered) / 1000.0)
                        signals = [p.signal_dbm for p in pts]
                        all_signals.extend(signals)

            result.points = coverage_points
            result.max_range_km = max(max_ranges) if max_ranges else 0.0
            result.avg_signal_dbm = float(np.mean(all_signals)) if all_signals else -999.0

            # Estimate covered area (rough — sum of radial segments)
            result.covered_area_km2 = self._estimate_covered_area(
                coverage_points, req.min_signal_dbm
            )

            # Build GeoJSON for map rendering
            result.geojson = self._build_geojson(coverage_points, req.min_signal_dbm)

            # Warn if skywave produces no coverage
            if not max_ranges and req.wave_type == "skywave":
                freq_mhz = freq_hz / 1e6
                if 3 <= freq_mhz <= 30:
                    min_skip_km = 200.0 + (freq_mhz - 3) * 30
                    if req.radius_km <= min_skip_km:
                        result.warnings.append(
                            f"Skywave skip zone: at {freq_mhz:.1f} MHz the ionospheric wave "
                            f"does not return until ~{min_skip_km:.0f} km. "
                            f"Your radius ({req.radius_km:.0f} km) is entirely inside the skip zone — "
                            f"increase the radius to {min_skip_km + 200:.0f} km or more to see coverage."
                        )
                    else:
                        result.warnings.append(
                            f"Skywave: radius ({req.radius_km:.0f} km) extends beyond the skip zone "
                            f"(~{min_skip_km:.0f} km at {freq_mhz:.1f} MHz) but no points met the "
                            f"minimum signal threshold ({req.min_signal_dbm:.0f} dBm). "
                            "Try increasing TX power, lowering the minimum signal threshold, or reducing radius."
                        )
                else:
                    result.warnings.append(
                        f"Skywave requires HF (3–30 MHz); {freq_mhz:.1f} MHz is out of range. "
                        "Switch to a HF frequency or change wave type."
                    )

            result.computation_time_s = time.time() - t_start
            return result
        finally:
            await terrain.close()

    async def _compute_radial(
        self, tx: TransmitterConfig, rx: ReceiverConfig,
        azimuth_deg: float, radius_km: float, num_points: int,
        freq_hz: float, model: PropagationModel, wave_type: str,
        atm: AtmosphericConditions, surface_N: float,
        sw_state: Optional[SpaceWeatherState],
        terrain: TerrainManager,
        min_signal_dbm: float,
        use_gpu: bool,
        include_buildings: bool,
        buildings_radius_m: float = 500.0,
        context: int = 2,
        diffraction_model: Optional[str] = None,
        rcs_m2: float = 1.0,
        clutter_height_m: float = 0.0,
        polar_pattern: str = "omni",
        polar_peak_gain_dbi: Optional[float] = None,
        sweep_deg: float = 0.0,
    ) -> list[CoveragePoint]:
        """Compute signal levels along a single radial direction."""
        points: list[CoveragePoint] = []
        freq_mhz = freq_hz / 1e6

        # Distances to sample (skip d=0)
        distances_m = np.linspace(100, radius_km * 1000, num_points)

        # Get terrain profile for this radial
        lat2, lon2 = destination_point(tx.lat, tx.lon, azimuth_deg, radius_km * 1000)
        try:
            dist_arr, elev_arr = await terrain.get_elevation_profile(
                tx.lat, tx.lon, lat2, lon2, num_points
            )
        except Exception:
            elev_arr = np.zeros(num_points)
            dist_arr = distances_m

        # Clutter (land-cover canopy / urban) added to the terrain profile so
        # vegetation & buildings obstruct the path. Prefer a per-pixel WorldCover
        # raster from an installed `clutter` data pack; else the scalar offset.
        clutter_arr = None
        try:
            from app.core import clutter as _clutter
            clutter_arr = _clutter.clutter_profile(tx.lat, tx.lon, lat2, lon2, num_points)
        except Exception:
            clutter_arr = None
        if clutter_arr is not None:
            n_pad = min(len(clutter_arr), len(elev_arr))
            elev_arr = elev_arr.copy()
            elev_arr[:n_pad] = elev_arr[:n_pad] + np.asarray(clutter_arr[:n_pad])
            if clutter_height_m > 0:
                elev_arr = elev_arr + clutter_height_m   # extra operator-set offset on top of the raster
        elif clutter_height_m > 0:
            elev_arr = elev_arr + clutter_height_m

        # TX/RX effective heights
        tx_agl = tx.height_m
        rx_agl = rx.height_m + rx.altitude_m  # include airborne offset

        # Compute path loss for each distance sample
        for i, dist_m in enumerate(distances_m):
            # Fraction along radial
            frac = i / (num_points - 1) if num_points > 1 else 1.0
            lat_pt, lon_pt = destination_point(tx.lat, tx.lon, azimuth_deg, dist_m)

            # Slice terrain profile up to this point
            slice_n = max(2, int(frac * num_points))
            elev_slice = elev_arr[:slice_n]
            dist_slice = dist_m

            # TX antenna gain in this direction
            el_angle = math.degrees(math.atan2(
                (elev_arr[min(slice_n - 1, len(elev_arr) - 1)] + rx_agl) -
                (elev_arr[0] + tx_agl),
                dist_m
            )) if dist_m > 0 else 0.0
            # Azimuth offset from TX boresight (normalized to ±180°)
            az_offset = ((azimuth_deg - tx.antenna.azimuth_deg) + 180.0) % 360.0 - 180.0

            # When the user picks a polar pattern (or supplies a peak gain),
            # the polar pattern is the source of azimuth shape and peak gain.
            # We still take the elevation contribution from the antenna's
            # intrinsic pattern (downtilt, dish cuts, etc.) — applied as a
            # delta from on-boresight so we don't double-count the peak.
            if polar_pattern != "omni" or polar_peak_gain_dbi is not None or sweep_deg > 0:
                peak_gain_dbi = polar_peak_gain_dbi
                if peak_gain_dbi is None:
                    peak_gain_dbi = tx.antenna.gain_dbi if tx.antenna.gain_dbi is not None else 0.0
                el_delta_db = (get_antenna_gain_dbi(tx.antenna, el_angle, 0.0)
                               - get_antenna_gain_dbi(tx.antenna, 0.0, 0.0))
                az_pattern_db = _polar_pattern_gain_with_sweep_db(
                    polar_pattern, az_offset, sweep_deg
                )
                tx_gain = peak_gain_dbi + az_pattern_db + el_delta_db
            else:
                tx_gain = get_antenna_gain_dbi(tx.antenna, el_angle, az_offset)

            # RX sees the signal arriving from the opposite direction of the radial
            rx_az_offset = ((azimuth_deg + 180.0 - rx.antenna.azimuth_deg) + 180.0) % 360.0 - 180.0
            rx_gain = get_antenna_gain_dbi(rx.antenna, -el_angle, rx_az_offset)

            # Path loss
            if model == PropagationModel.ITM and slice_n >= 2:
                try:
                    itm_result = compute_itm_path_loss(
                        elevations=elev_slice.tolist(),
                        distance_m=max(1.0, dist_slice),
                        tx_height_m=max(0.5, tx_agl),
                        rx_height_m=max(0.5, rx_agl),
                        frequency_mhz=max(20.0, min(20000.0, freq_mhz)),
                        surface_refractivity=max(101.0, surface_N),
                        mode=1,
                    )
                    path_loss = itm_result.path_loss_db
                    prop_mode = itm_result.propagation_mode
                except Exception:
                    path_loss = select_model(model, dist_m, freq_hz, tx_agl, rx_agl,
                                             context=context, rcs_m2=rcs_m2)
                    prop_mode = "fspl_fallback"
            else:
                path_loss = select_model(
                    model, dist_m, freq_hz, tx_agl, rx_agl,
                    context=context, rcs_m2=rcs_m2,
                )
                prop_mode = "model"

            # Diffraction correction (skip for ITM — already terrain-aware)
            if diffraction_model and model != PropagationModel.ITM and slice_n >= 3:
                dist_list = dist_arr[:slice_n].tolist()
                elev_list = elev_slice.tolist()
                diff_loss = compute_diffraction_db(
                    elev_list, dist_list, tx_agl, rx_agl, freq_hz, diffraction_model
                )
                path_loss += diff_loss
                if diff_loss > 0:
                    prop_mode = f"{prop_mode}+diffraction"

            # Atmospheric losses
            atm_loss = compute_atmospheric_loss(
                freq_hz, dist_m, atm, el_angle
            )
            path_loss += atm_loss.total_db

            # Space weather corrections
            sw_warnings: list[str] = []
            if sw_state:
                path_loss, sw_warnings = apply_space_weather_corrections(
                    path_loss, freq_hz, sw_state,
                    tx.lat, lat_pt, dist_m / 1000.0
                )

            # Wave type physics (ground wave / skywave / troposcatter / LOS)
            wt_warnings: list[str] = []
            if wave_type and wave_type != "auto":
                path_loss, prop_mode, wt_warnings = apply_wave_type(
                    path_loss, wave_type, dist_m / 1000.0,
                    freq_hz, tx.height_m + tx.altitude_m,
                    rx.height_m + rx.altitude_m,
                    sw_state,
                )

            # Received signal
            rx_power = (tx.power_dbm
                        + tx_gain
                        + rx_gain
                        - path_loss
                        - tx.antenna.height_m * 0  # placeholder feedline
                        )

            points.append(CoveragePoint(
                lat=lat_pt, lon=lon_pt,
                distance_m=float(dist_m),
                signal_dbm=float(rx_power),
                path_loss_db=float(path_loss),
                is_covered=rx_power >= min_signal_dbm,
                propagation_mode=prop_mode,
            ))

            # Early termination: if signal is 20 dB below minimum for 3 consecutive points
            if i >= 2 and rx_power < (min_signal_dbm - 20.0):
                break

        return points

    async def compute_coverage_raster(self, req: CoverageRequest, grid_size: int = 48) -> CoverageResult:
        """Per-pixel coverage: instead of a radial sweep, walk a regular grid_size×grid_size
        lat/lon grid over ±radius_km around the TX and compute one ITM path (TX→pixel) for
        every cell — even coverage everywhere, no thinning at range. Heavier than the radial
        sweep (grid_size² ITM evaluations); grid_size is clamped to ≤ 96."""
        t0 = time.time()
        grid_size = max(8, min(96, int(grid_size)))
        tx = req.transmitter
        rx = req.receiver
        freq_hz = tx.frequency_hz
        freq_mhz = max(20.0, min(20000.0, freq_hz / 1e6))
        result = CoverageResult(transmitter=tx)
        utc_time = self._parse_time(req.utc_datetime)
        atm = self._build_atmosphere(req.atmosphere, tx, utc_time)
        surface_N = max(101.0, get_surface_refractivity(tx.lat, tx.altitude_m))
        terrain = TerrainManager(resolution=req.terrain_resolution, use_gpu=req.use_gpu and GPU_AVAILABLE)
        try:
            # ± radius in degrees (lon scaled by cos lat)
            R_km = req.radius_km
            dlat = R_km / 111.32
            dlon = R_km / (111.32 * max(0.05, math.cos(math.radians(tx.lat))))
            lats = np.linspace(tx.lat - dlat, tx.lat + dlat, grid_size)
            lons = np.linspace(tx.lon - dlon, tx.lon + dlon, grid_size)
            polar_pattern_id = req.polar_pattern if req.polar_pattern in POLAR_PATTERNS else "omni"
            n_prof = max(16, min(256, int(req.points_per_radial // 4) or 64))
            tx_agl = max(0.5, tx.height_m)
            rx_agl = max(0.5, rx.height_m + rx.altitude_m)
            pts: list[CoveragePoint] = []

            async def one_cell(la: float, lo: float) -> CoveragePoint:
                d_m = haversine_distance(tx.lat, tx.lon, la, lo)
                if d_m < 1.0:
                    return CoveragePoint(lat=la, lon=lo, distance_m=0.0, signal_dbm=tx.power_dbm,
                                         path_loss_db=0.0, is_covered=True, propagation_mode="tx")
                try:
                    _, elev = await terrain.get_elevation_profile(tx.lat, tx.lon, la, lo, n_prof)
                except Exception:
                    elev = np.zeros(n_prof)
                # Per-cell clutter canopy from an installed ESA WorldCover pack (urban / forest / etc.)
                # — overrides the uniform offset; if no pack, fall back to the uniform value.
                try:
                    from app.core import clutter as _clutter
                    clutter_arr = _clutter.clutter_profile(tx.lat, tx.lon, la, lo, n_prof)
                except Exception:
                    clutter_arr = None
                if clutter_arr is not None:
                    n_pad = min(len(clutter_arr), len(elev))
                    elev[:n_pad] = elev[:n_pad] + np.asarray(clutter_arr[:n_pad])
                    if req.clutter_height_m > 0:
                        elev = elev + req.clutter_height_m
                elif req.clutter_height_m > 0:
                    elev = elev + req.clutter_height_m
                az = initial_bearing(tx.lat, tx.lon, la, lo) or 0.0
                el_angle = math.degrees(math.atan2((float(elev[-1]) + rx_agl) - (float(elev[0]) + tx_agl), d_m))
                az_off = ((az - tx.antenna.azimuth_deg) + 180.0) % 360.0 - 180.0
                if polar_pattern_id != "omni" or req.polar_peak_gain_dbi is not None or req.sweep_deg > 0:
                    pk = req.polar_peak_gain_dbi if req.polar_peak_gain_dbi is not None else (tx.antenna.gain_dbi or 0.0)
                    el_delta = get_antenna_gain_dbi(tx.antenna, el_angle, 0.0) - get_antenna_gain_dbi(tx.antenna, 0.0, 0.0)
                    tx_gain = pk + _polar_pattern_gain_with_sweep_db(polar_pattern_id, az_off, req.sweep_deg) + el_delta
                else:
                    tx_gain = get_antenna_gain_dbi(tx.antenna, el_angle, az_off)
                rx_gain = get_antenna_gain_dbi(rx.antenna, -el_angle, ((az + 180.0 - rx.antenna.azimuth_deg) + 180.0) % 360.0 - 180.0)
                try:
                    itm = compute_itm_path_loss(elevations=elev.tolist(), distance_m=max(1.0, d_m),
                                                tx_height_m=tx_agl, rx_height_m=rx_agl, frequency_mhz=freq_mhz,
                                                surface_refractivity=surface_N, mode=1)
                    pl, mode = itm.path_loss_db, itm.propagation_mode
                except Exception:
                    pl = select_model(req.propagation_model, d_m, freq_hz, tx_agl, rx_agl, context=req.context, rcs_m2=req.rcs_m2)
                    mode = "fallback"
                pl += compute_atmospheric_loss(freq_hz, d_m, atm, el_angle).total_db
                sig = tx.power_dbm + tx_gain + rx_gain - pl
                return CoveragePoint(lat=la, lon=lo, distance_m=float(d_m), signal_dbm=float(sig),
                                     path_loss_db=float(pl), is_covered=sig >= req.min_signal_dbm, propagation_mode=mode)

            for la in lats:
                row = await asyncio.gather(*(one_cell(float(la), float(lo)) for lo in lons))
                pts.extend(row)
            result.points = pts
            covered = [p for p in pts if p.is_covered]
            result.avg_signal_dbm = float(np.mean([p.signal_dbm for p in pts])) if pts else -999.0
            result.max_range_km = (max(p.distance_m for p in covered) / 1000.0) if covered else 0.0
            # rough area = covered-fraction × the grid's footprint
            cell_km2 = (2.0 * R_km / (grid_size - 1)) ** 2 if grid_size > 1 else 0.0
            result.covered_area_km2 = len(covered) * cell_km2
            result.geojson = self._build_geojson(pts, req.min_signal_dbm)
            if result.geojson is not None:
                result.geojson.setdefault("metadata", {})["mode"] = "raster"
                result.geojson["metadata"]["grid_size"] = grid_size
            result.computation_time_s = time.time() - t0
            return result
        finally:
            await terrain.close()

    async def compute_point_to_point(self, req: PointToPointRequest) -> TerrainProfileResult:
        """
        Full point-to-point link budget with terrain profile.
        Returns detailed analysis including Fresnel zones, link budget, etc.
        """
        result = TerrainProfileResult()
        tx = req.transmitter
        freq_hz = tx.frequency_hz
        freq_mhz = freq_hz / 1e6

        utc_time = self._parse_time(req.utc_datetime)
        atm = self._build_atmosphere(req.atmosphere, tx, utc_time)
        surface_N = get_surface_refractivity(tx.lat, tx.altitude_m)

        # Fetch terrain profile
        terrain = TerrainManager(resolution="srtm1", use_gpu=req.use_gpu)
        try:
            dist_arr, elev_arr = await terrain.get_elevation_profile(
                tx.lat, tx.lon,
                req.receiver_lat, req.receiver_lon,
                req.num_profile_points,
            )
        finally:
            await terrain.close()

        # Per-cell clutter canopy from an installed ESA WorldCover pack (urban / forest / etc.) —
        # overrides the uniform offset; without a pack, fall back to the uniform value.
        try:
            from app.core import clutter as _clutter
            clutter_arr = _clutter.clutter_profile(tx.lat, tx.lon, req.receiver_lat, req.receiver_lon, len(elev_arr))
        except Exception:
            clutter_arr = None
        if clutter_arr is not None:
            n_pad = min(len(clutter_arr), len(elev_arr))
            elev_arr[:n_pad] = elev_arr[:n_pad] + np.asarray(clutter_arr[:n_pad])
            if req.clutter_height_m > 0:
                elev_arr = elev_arr + req.clutter_height_m
        elif req.clutter_height_m > 0:
            elev_arr = elev_arr + req.clutter_height_m

        total_dist = haversine_distance(tx.lat, tx.lon, req.receiver_lat, req.receiver_lon)
        result.total_distance_m = total_dist
        result.distances_m = dist_arr.tolist()
        result.elevations_m = elev_arr.tolist()

        # LOS line (straight line between TX and RX heights)
        tx_height_asl = float(elev_arr[0]) + tx.height_m
        rx_height_asl = float(elev_arr[-1]) + req.receiver_height_m + req.receiver_altitude_m
        result.los_heights_m = [
            tx_height_asl + (rx_height_asl - tx_height_asl) * d / total_dist
            for d in dist_arr
        ]

        # Fresnel zone radii (F1)
        lam = 3e8 / freq_hz
        result.fresnel_radii_m = [
            math.sqrt(lam * d * (total_dist - d) / max(total_dist, 1))
            for d in dist_arr
        ]

        # Path loss
        rx_agl = req.receiver_height_m + req.receiver_altitude_m
        if req.propagation_model == PropagationModel.ITM and len(elev_arr) >= 2:
            try:
                itm = compute_itm_path_loss(
                    elevations=elev_arr.tolist(),
                    distance_m=total_dist,
                    tx_height_m=tx.height_m,
                    rx_height_m=rx_agl,
                    frequency_mhz=freq_mhz,
                    surface_refractivity=surface_N,
                    mode=1,
                )
                path_loss = itm.path_loss_db
                result.propagation_mode = itm.propagation_mode
            except Exception:
                path_loss = fspl_db(total_dist, freq_hz)
                result.propagation_mode = "fspl_fallback"
        else:
            path_loss = select_model(
                req.propagation_model, total_dist, freq_hz,
                tx.height_m, rx_agl,
                context=req.context, rcs_m2=req.rcs_m2,
            )
            result.propagation_mode = req.propagation_model.value

        # Diffraction correction (skip for ITM)
        if (req.diffraction_model and
                req.propagation_model != PropagationModel.ITM and
                len(elev_arr) >= 3):
            diff_loss = compute_diffraction_db(
                elev_arr.tolist(), dist_arr.tolist(),
                tx.height_m, rx_agl, freq_hz, req.diffraction_model,
            )
            path_loss += diff_loss
            if diff_loss > 0:
                result.propagation_mode += "+diffraction"

        # Atmospheric losses
        el_angle = math.degrees(math.atan2(
            (rx_height_asl - tx_height_asl), total_dist
        )) if total_dist > 0 else 0.0
        atm_loss = compute_atmospheric_loss(freq_hz, total_dist, atm, el_angle)
        path_loss += atm_loss.total_db

        # Space weather
        sw_warnings: list[str] = []
        sw_state: Optional[SpaceWeatherState] = None
        if req.fetch_space_weather:
            try:
                sw_state = await fetch_space_weather()
                result.space_weather = sw_state.propagation_summary()
                path_loss, sw_warnings = apply_space_weather_corrections(
                    path_loss, freq_hz, sw_state,
                    tx.lat, req.receiver_lat, total_dist / 1000.0
                )
            except Exception as e:
                log.warning(f"Space weather unavailable: {e}")

        # Wave type physics
        wt_warnings: list[str] = []
        if req.wave_type and req.wave_type != "auto":
            rx_agl_eff = req.receiver_height_m + req.receiver_altitude_m
            path_loss, wt_mode, wt_warnings = apply_wave_type(
                path_loss, req.wave_type, total_dist / 1000.0,
                freq_hz, tx.height_m + tx.altitude_m, rx_agl_eff,
                sw_state,
            )
            result.propagation_mode = wt_mode

        result.path_loss_db = path_loss
        result.warnings = sw_warnings + wt_warnings

        # Link budget
        tx_gain = get_antenna_gain_dbi(tx.antenna, el_angle, 0.0)
        rx_ant = AntennaConfig()  # default
        rx_gain = get_antenna_gain_dbi(rx_ant, -el_angle, 0.0)

        lb = LinkBudget(
            tx_power_dbm=tx.power_dbm,
            tx_antenna_gain_dbi=tx_gain,
            rx_antenna_gain_dbi=rx_gain,
            path_loss_db=path_loss,
            atmospheric_loss_db=atm_loss.total_db,
            rain_loss_db=atm_loss.rain_db,
        )
        result.received_signal_dbm = lb.received_power_dbm
        result.link_budget = lb.to_dict()

        return result

    # ─────────────────────────────────────────────────────────────────────────
    # Helpers
    # ─────────────────────────────────────────────────────────────────────────

    @staticmethod
    def _parse_time(utc_str: Optional[str]) -> datetime.datetime:
        if utc_str:
            try:
                return datetime.datetime.fromisoformat(utc_str)
            except Exception:
                pass
        return datetime.datetime.utcnow()

    @staticmethod
    def _build_atmosphere(atm_dict: Optional[dict],
                           tx: TransmitterConfig,
                           utc_time: datetime.datetime) -> AtmosphericConditions:
        """Build atmospheric conditions from request dict or defaults."""
        defaults = {
            "temperature_c": altitude_to_temperature_c(tx.altitude_m),
            "pressure_hpa": altitude_to_pressure(tx.altitude_m),
            "humidity_percent": 60.0,
            "rain_rate_mm_per_hr": 0.0,
            "visibility_km": 10.0,
            "refractivity_gradient": -40.0,
            "altitude_m": tx.altitude_m,
        }
        if atm_dict:
            defaults.update({k: v for k, v in atm_dict.items() if v is not None})
        return AtmosphericConditions(**defaults, utc_time=utc_time)

    # ─────────────────────────────────────────────────────────────────────────
    # Route Analysis
    # ─────────────────────────────────────────────────────────────────────────

    async def compute_route(
        self,
        waypoints: list[tuple[float, float]],  # (lat, lon)
        receiver_lat: float,
        receiver_lon: float,
        transmitter: TransmitterConfig,
        receiver: ReceiverConfig,
        propagation_model: "PropagationModel",
        wave_type: str = "auto",
        atmosphere: Optional[dict] = None,
        context: int = 2,
        diffraction_model: Optional[str] = None,
        clutter_height_m: float = 0.0,
    ) -> dict:
        """
        Route Analysis: run P2P from each waypoint back to a fixed receiver.
        Returns GeoJSON FeatureCollection with coloured points + line.
        """
        utc_time = datetime.datetime.utcnow()
        atm = self._build_atmosphere(atmosphere, transmitter, utc_time)

        results = []
        for lat, lon in waypoints:
            req = PointToPointRequest(
                transmitter=TransmitterConfig(
                    lat=lat, lon=lon,
                    height_m=transmitter.height_m,
                    altitude_m=transmitter.altitude_m,
                    power_dbm=transmitter.power_dbm,
                    frequency_hz=transmitter.frequency_hz,
                    antenna=transmitter.antenna,
                ),
                receiver_lat=receiver_lat,
                receiver_lon=receiver_lon,
                receiver_height_m=receiver.height_m,
                receiver_altitude_m=receiver.altitude_m,
                propagation_model=propagation_model,
                wave_type=wave_type,
                atmosphere=atmosphere,
                fetch_space_weather=False,
                num_profile_points=256,
                context=context,
                diffraction_model=diffraction_model,
                clutter_height_m=clutter_height_m,
            )
            try:
                r = await self.compute_point_to_point(req)
                results.append({
                    "lat": lat, "lon": lon,
                    "signal_dbm": round(r.received_signal_dbm, 1),
                    "path_loss_db": round(r.path_loss_db, 1),
                    "distance_m": round(r.total_distance_m, 0),
                    "propagation_mode": r.propagation_mode,
                })
            except Exception as e:
                results.append({
                    "lat": lat, "lon": lon,
                    "signal_dbm": -999.0,
                    "path_loss_db": 999.0,
                    "distance_m": haversine_distance(lat, lon, receiver_lat, receiver_lon),
                    "propagation_mode": "error",
                    "error": str(e),
                })

        # Build GeoJSON
        features = []
        for r in results:
            features.append({
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [r["lon"], r["lat"]]},
                "properties": {k: v for k, v in r.items() if k not in ("lat", "lon")},
            })
        # Route line
        if len(waypoints) >= 2:
            features.append({
                "type": "Feature",
                "geometry": {
                    "type": "LineString",
                    "coordinates": [[lon, lat] for lat, lon in waypoints],
                },
                "properties": {"feature_type": "route"},
            })

        return {
            "type": "FeatureCollection",
            "features": features,
        }

    # ─────────────────────────────────────────────────────────────────────────
    # Multipoint Analysis
    # ─────────────────────────────────────────────────────────────────────────

    async def compute_multipoint(
        self,
        tx_points: list[tuple[float, float]],  # (lat, lon)
        receiver_lat: float,
        receiver_lon: float,
        transmitter: TransmitterConfig,
        receiver: ReceiverConfig,
        propagation_model: "PropagationModel",
        wave_type: str = "auto",
        atmosphere: Optional[dict] = None,
        context: int = 2,
        diffraction_model: Optional[str] = None,
        clutter_height_m: float = 0.0,
    ) -> dict:
        """
        Multipoint Analysis: P2P from each TX candidate to fixed receiver.
        Each TX is a candidate transmitter location tested simultaneously.
        """
        # Same logic as route but no connecting line
        geojson = await self.compute_route(
            tx_points, receiver_lat, receiver_lon,
            transmitter, receiver, propagation_model,
            wave_type, atmosphere, context,
            diffraction_model, clutter_height_m,
        )
        # Remove route line feature
        geojson["features"] = [f for f in geojson["features"]
                                if f.get("properties", {}).get("feature_type") != "route"]
        return geojson

    # ─────────────────────────────────────────────────────────────────────────
    # MANET Planning
    # ─────────────────────────────────────────────────────────────────────────

    async def compute_manet(
        self,
        nodes: list[dict],   # [{lat, lon, height_m, label}]
        transmitter: TransmitterConfig,
        receiver: ReceiverConfig,
        propagation_model: "PropagationModel",
        wave_type: str = "auto",
        atmosphere: Optional[dict] = None,
        context: int = 2,
        diffraction_model: Optional[str] = None,
        clutter_height_m: float = 0.0,
        sensitivity_dbm: float = -100.0,
    ) -> dict:
        """
        MANET Planning: compute P2P between all N*(N-1)/2 node pairs.
        Returns links as GeoJSON LineString features with signal_dbm property.
        Links above sensitivity threshold are marked as 'connected'.
        """
        n = len(nodes)
        if n < 2:
            return {"type": "FeatureCollection", "features": []}

        # Build all unique pairs
        pairs = [(i, j) for i in range(n) for j in range(i + 1, n)]

        async def _pair_link(i: int, j: int) -> Optional[dict]:
            a, b = nodes[i], nodes[j]
            tx_node = TransmitterConfig(
                lat=a["lat"], lon=a["lon"],
                height_m=a.get("height_m", transmitter.height_m),
                altitude_m=transmitter.altitude_m,
                power_dbm=transmitter.power_dbm,
                frequency_hz=transmitter.frequency_hz,
                antenna=transmitter.antenna,
            )
            req = PointToPointRequest(
                transmitter=tx_node,
                receiver_lat=b["lat"],
                receiver_lon=b["lon"],
                receiver_height_m=b.get("height_m", receiver.height_m),
                receiver_altitude_m=receiver.altitude_m,
                propagation_model=propagation_model,
                wave_type=wave_type,
                atmosphere=atmosphere,
                fetch_space_weather=False,
                num_profile_points=256,
                context=context,
                diffraction_model=diffraction_model,
                clutter_height_m=clutter_height_m,
            )
            try:
                r = await self.compute_point_to_point(req)
                sig = r.received_signal_dbm
                return {
                    "type": "Feature",
                    "geometry": {
                        "type": "LineString",
                        "coordinates": [[a["lon"], a["lat"]], [b["lon"], b["lat"]]],
                    },
                    "properties": {
                        "node_a": a.get("label", f"Node {i+1}"),
                        "node_b": b.get("label", f"Node {j+1}"),
                        "signal_dbm": round(sig, 1),
                        "path_loss_db": round(r.path_loss_db, 1),
                        "distance_m": round(r.total_distance_m, 0),
                        "connected": sig >= sensitivity_dbm,
                        "propagation_mode": r.propagation_mode,
                    },
                }
            except Exception as e:
                dist = haversine_distance(a["lat"], a["lon"], b["lat"], b["lon"])
                return {
                    "type": "Feature",
                    "geometry": {
                        "type": "LineString",
                        "coordinates": [[a["lon"], a["lat"]], [b["lon"], b["lat"]]],
                    },
                    "properties": {
                        "node_a": a.get("label", f"Node {i+1}"),
                        "node_b": b.get("label", f"Node {j+1}"),
                        "signal_dbm": -999.0,
                        "path_loss_db": 999.0,
                        "distance_m": round(dist, 0),
                        "connected": False,
                        "error": str(e),
                    },
                }

        tasks = [_pair_link(i, j) for i, j in pairs]
        link_features = await asyncio.gather(*tasks)

        # Node markers
        node_features = []
        for idx, node in enumerate(nodes):
            node_features.append({
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [node["lon"], node["lat"]]},
                "properties": {
                    "label": node.get("label", f"Node {idx+1}"),
                    "height_m": node.get("height_m", 10.0),
                    "feature_type": "manet_node",
                },
            })

        return {
            "type": "FeatureCollection",
            "features": [f for f in link_features if f is not None] + node_features,
        }

    # ─────────────────────────────────────────────────────────────────────────
    # Best Server
    # ─────────────────────────────────────────────────────────────────────────

    async def compute_best_server(
        self,
        query_lat: float,
        query_lon: float,
        tx_sites: list[dict],   # [{lat, lon, height_m, label, power_dbm?, frequency_hz?}]
        transmitter: TransmitterConfig,
        receiver: ReceiverConfig,
        propagation_model: "PropagationModel",
        wave_type: str = "auto",
        atmosphere: Optional[dict] = None,
        context: int = 2,
        clutter_height_m: float = 0.0,
    ) -> dict:
        """
        Best Server: given a clicked location, which of the existing TX sites
        provides the strongest signal?
        Returns ranked list + signal values.
        """
        async def _site_signal(site: dict) -> dict:
            tx_cfg = TransmitterConfig(
                lat=site["lat"], lon=site["lon"],
                height_m=site.get("height_m", transmitter.height_m),
                altitude_m=transmitter.altitude_m,
                power_dbm=site.get("power_dbm", transmitter.power_dbm),
                frequency_hz=site.get("frequency_hz", transmitter.frequency_hz),
                antenna=transmitter.antenna,
            )
            req = PointToPointRequest(
                transmitter=tx_cfg,
                receiver_lat=query_lat,
                receiver_lon=query_lon,
                receiver_height_m=receiver.height_m,
                receiver_altitude_m=receiver.altitude_m,
                propagation_model=propagation_model,
                wave_type=wave_type,
                atmosphere=atmosphere,
                fetch_space_weather=False,
                num_profile_points=256,
                context=context,
                clutter_height_m=clutter_height_m,
            )
            try:
                r = await self.compute_point_to_point(req)
                return {
                    "label": site.get("label", ""),
                    "lat": site["lat"], "lon": site["lon"],
                    "signal_dbm": round(r.received_signal_dbm, 1),
                    "path_loss_db": round(r.path_loss_db, 1),
                    "distance_m": round(r.total_distance_m, 0),
                }
            except Exception as e:
                dist = haversine_distance(site["lat"], site["lon"], query_lat, query_lon)
                return {
                    "label": site.get("label", ""),
                    "lat": site["lat"], "lon": site["lon"],
                    "signal_dbm": -999.0,
                    "path_loss_db": 999.0,
                    "distance_m": round(dist, 0),
                    "error": str(e),
                }

        tasks = [_site_signal(s) for s in tx_sites]
        site_results = await asyncio.gather(*tasks)
        ranked = sorted(site_results, key=lambda x: x["signal_dbm"], reverse=True)

        return {
            "query": {"lat": query_lat, "lon": query_lon},
            "best_server": ranked[0] if ranked else None,
            "sites": ranked,
        }

    # ─────────────────────────────────────────────────────────────────────────
    # Interference Analysis
    # ─────────────────────────────────────────────────────────────────────────

    @staticmethod
    def compute_interference(
        signal_geojson: dict,
        noise_geojson: dict,
    ) -> dict:
        """
        Compute SNR = signal_dbm - noise_dbm at each point in the signal layer.
        Spatially matches signal points to their nearest noise point.
        Returns new GeoJSON with snr_db property.
        """
        noise_pts = [
            f for f in noise_geojson.get("features", [])
            if f.get("geometry", {}).get("type") == "Point"
        ]
        if not noise_pts:
            # No noise layer — return signal unchanged but add snr_db
            features = []
            for f in signal_geojson.get("features", []):
                if f.get("geometry", {}).get("type") != "Point":
                    continue
                props = dict(f.get("properties", {}))
                sig = props.get("signal_dbm", -120.0)
                props["snr_db"] = round(sig - (-120.0), 1)   # assume thermal floor
                features.append({**f, "properties": props})
            return {"type": "FeatureCollection", "features": features}

        # Build noise spatial index (simple lat/lon array)
        noise_coords = np.array([
            [f["geometry"]["coordinates"][1], f["geometry"]["coordinates"][0]]
            for f in noise_pts
        ])
        noise_signals = np.array([
            f.get("properties", {}).get("signal_dbm", -120.0)
            for f in noise_pts
        ])

        features = []
        for f in signal_geojson.get("features", []):
            if f.get("geometry", {}).get("type") != "Point":
                continue
            coords = f["geometry"]["coordinates"]
            sig_lat, sig_lon = coords[1], coords[0]
            props = dict(f.get("properties", {}))
            sig_dbm = props.get("signal_dbm", -120.0)

            # Nearest noise point (Euclidean in lat/lon — good enough for local areas)
            diffs = noise_coords - np.array([sig_lat, sig_lon])
            dists_sq = (diffs ** 2).sum(axis=1)
            nearest_idx = int(np.argmin(dists_sq))
            noise_dbm = float(noise_signals[nearest_idx])

            snr = sig_dbm - noise_dbm
            props["snr_db"] = round(snr, 1)
            props["noise_dbm"] = round(noise_dbm, 1)
            features.append({**f, "properties": props})

        return {"type": "FeatureCollection", "features": features}

    # ─────────────────────────────────────────────────────────────────────────
    # Super Layer Merge
    # ─────────────────────────────────────────────────────────────────────────

    @staticmethod
    def compute_super_layer(layers: list[dict], grid_deg: float = 0.001) -> dict:
        """
        Merge multiple GeoJSON coverage layers — take max signal_dbm at each
        unique location (grid-snapped to grid_deg resolution).
        Returns merged GeoJSON FeatureCollection.
        """
        # Grid key → best feature
        grid: dict[tuple[int, int], dict] = {}

        for layer in layers:
            for f in layer.get("features", []):
                if f.get("geometry", {}).get("type") != "Point":
                    continue
                coords = f["geometry"]["coordinates"]
                lon, lat = coords[0], coords[1]
                gk = (round(lat / grid_deg), round(lon / grid_deg))
                props = f.get("properties", {})
                sig = props.get("signal_dbm", -999.0)

                if gk not in grid or sig > grid[gk]["properties"].get("signal_dbm", -999.0):
                    grid[gk] = f

        return {
            "type": "FeatureCollection",
            "features": list(grid.values()),
        }

    # ─────────────────────────────────────────────────────────────────────────
    # Best Site (polygon / grid sample)
    # ─────────────────────────────────────────────────────────────────────────

    async def compute_best_site_polygon(
        self,
        polygon_coords: list[tuple[float, float]],  # [(lat, lon), ...]
        coverage_pct: float,          # 0–100, density of grid samples
        transmitter: TransmitterConfig,
        receiver: ReceiverConfig,
        propagation_model: "PropagationModel",
        wave_type: str = "auto",
        radius_km: float = 30.0,
        num_radials: int = 180,
        points_per_radial: int = 150,
        min_signal_dbm: float = -100.0,
        atmosphere: Optional[dict] = None,
        terrain_resolution: str = "srtm3",
        context: int = 2,
        diffraction_model: Optional[str] = None,
    ) -> dict:
        """
        Grid-sample candidate TX locations inside a polygon, run coverage from each,
        return the best one.  coverage_pct controls sample density (5–100).
        """
        # Compute bounding box of polygon
        lats = [p[0] for p in polygon_coords]
        lons = [p[1] for p in polygon_coords]
        bb_n, bb_s = max(lats), min(lats)
        bb_e, bb_w = max(lons), min(lons)

        # Grid spacing: coverage_pct=100 → ~0.01°, 5 → ~0.2°
        step = 0.2 - (coverage_pct / 100.0) * 0.19
        step = max(0.005, min(0.5, step))

        # Generate grid points inside polygon bounding box
        import math as _math
        candidates = []
        lat = bb_s
        while lat <= bb_n:
            lon = bb_w
            while lon <= bb_e:
                if _point_in_polygon(lat, lon, polygon_coords):
                    candidates.append({"lat": lat, "lon": lon,
                                        "height_m": transmitter.height_m,
                                        "label": f"Grid {len(candidates)+1}"})
                lon += step
            lat += step

        if not candidates:
            return {"status": "error", "detail": "No grid points fell inside the polygon"}

        # Limit to 20 candidates for performance
        if len(candidates) > 20:
            step_idx = max(1, len(candidates) // 20)
            candidates = candidates[::step_idx][:20]

        # Run coverage for each candidate
        tasks = []
        for cand in candidates:
            cov_req = CoverageRequest(
                transmitter=TransmitterConfig(
                    lat=cand["lat"], lon=cand["lon"],
                    height_m=cand["height_m"],
                    altitude_m=transmitter.altitude_m,
                    power_dbm=transmitter.power_dbm,
                    frequency_hz=transmitter.frequency_hz,
                    antenna=transmitter.antenna,
                ),
                receiver=receiver,
                propagation_model=propagation_model,
                wave_type=wave_type,
                radius_km=radius_km,
                num_radials=num_radials,
                points_per_radial=points_per_radial,
                min_signal_dbm=min_signal_dbm,
                atmosphere=atmosphere,
                terrain_resolution=terrain_resolution,
                fetch_space_weather=False,
                context=context,
                diffraction_model=diffraction_model,
            )
            tasks.append(self.compute_coverage(cov_req))

        site_results = await asyncio.gather(*tasks, return_exceptions=True)

        best_idx = 0
        best_score = float('-inf')
        scored = []
        for i, (cand, res) in enumerate(zip(candidates, site_results)):
            if isinstance(res, Exception):
                scored.append({**cand, "error": str(res)})
                continue
            score = res.covered_area_km2 + max(0.0, 100.0 + res.avg_signal_dbm)
            scored.append({
                **cand,
                "covered_area_km2": round(res.covered_area_km2, 2),
                "avg_signal_dbm":   round(res.avg_signal_dbm, 1),
                "max_range_km":     round(res.max_range_km, 2),
                "score":            round(score, 2),
            })
            if score > best_score:
                best_score = score
                best_idx = i

        scored_valid = [s for s in scored if "score" in s]
        scored_valid.sort(key=lambda x: x["score"], reverse=True)

        best_res = site_results[best_idx]
        best_geojson = best_res.geojson if not isinstance(best_res, Exception) else None

        return {
            "status": "ok",
            "sites": scored_valid,
            "best_geojson": best_geojson,
            "num_candidates": len(candidates),
        }

    async def compute_lob_range(
        self,
        observer_lat: float,
        observer_lon: float,
        observer_height_m: float,
        azimuth_deg: float,
        frequency_hz: float,
        tx_power_dbm: float,
        observed_rssi_dbm: float,
        propagation_model: str = "itm",
        diffraction_model: Optional[str] = "deygout",
        clutter_height_m: float = 0.0,
        terrain_resolution: str = "srtm1",
        context: int = 2,
        max_range_km: float = 150.0,
        num_points: int = 300,
        atmosphere: Optional[dict] = None,
    ) -> dict:
        """
        Terrain-aware LoB range estimation.

        Runs a single terrain radial from the observer location in the bearing
        direction (treating the observer as the transmitter, exploiting path-loss
        reciprocity). Finds the distance at which the signal level crosses the
        observed RSSI — that distance is the estimated emitter range.

        Returns:
            estimated_distance_m: interpolated crossing distance
            confidence: 'high' | 'low' (low if signal never drops to rssi within max_range)
            propagation_mode: ITM mode string from the radial
            profile: list of {distance_m, signal_dbm} for the UI to plot
        """
        utc_time = self._parse_time(None)

        # Build a dummy TransmitterConfig at the observer location using emitter power/freq
        tx = TransmitterConfig(
            lat=observer_lat,
            lon=observer_lon,
            height_m=observer_height_m,
            altitude_m=0.0,
            power_dbm=tx_power_dbm,
            frequency_hz=frequency_hz,
            antenna=AntennaConfig(
                type=AntennaType.DIPOLE_HALF_WAVE,
                gain_dbi=None,
                tilt_deg=0.0,
                azimuth_deg=0.0,
                height_m=observer_height_m,
            ),
        )
        rx = ReceiverConfig(
            height_m=2.0,   # typical emitter height along radial
            altitude_m=0.0,
            antenna=AntennaConfig(type=AntennaType.DIPOLE_HALF_WAVE, gain_dbi=None),
        )

        atm = self._build_atmosphere(atmosphere, tx, utc_time)
        surface_N = get_surface_refractivity(observer_lat, 0.0)

        model = PropagationModel.ITM
        try:
            model = PropagationModel(propagation_model)
        except ValueError:
            pass

        # min_signal set well below observed_rssi so the radial isn't terminated early
        min_signal = observed_rssi_dbm - 40.0

        terrain = TerrainManager(resolution=terrain_resolution, use_gpu=False)
        try:
            points = await self._compute_radial(
                tx=tx, rx=rx,
                azimuth_deg=azimuth_deg,
                radius_km=max_range_km,
                num_points=num_points,
                freq_hz=frequency_hz,
                model=model,
                wave_type="auto",
                atm=atm,
                surface_N=surface_N,
                sw_state=None,
                terrain=terrain,
                min_signal_dbm=min_signal,
                use_gpu=False,
                include_buildings=False,
                clutter_height_m=clutter_height_m,
                diffraction_model=diffraction_model if diffraction_model and diffraction_model != "none" else None,
                context=context,
            )
        finally:
            await terrain.close()

        if not points:
            return {
                "estimated_distance_m": 10000.0,
                "confidence": "low",
                "propagation_mode": "unknown",
                "profile": [],
            }

        profile = [{"distance_m": p.distance_m, "signal_dbm": p.signal_dbm} for p in points]

        # Find first crossing: signal drops from >= observed_rssi to < observed_rssi
        estimated_distance_m = None
        prop_mode = points[0].propagation_mode if points else "unknown"

        for i in range(1, len(points)):
            prev = points[i - 1]
            curr = points[i]
            prop_mode = curr.propagation_mode
            if prev.signal_dbm >= observed_rssi_dbm and curr.signal_dbm < observed_rssi_dbm:
                # Linear interpolation between the two samples
                t = (observed_rssi_dbm - prev.signal_dbm) / (curr.signal_dbm - prev.signal_dbm)
                estimated_distance_m = prev.distance_m + t * (curr.distance_m - prev.distance_m)
                break

        if estimated_distance_m is None:
            # Signal never dropped to rssi within max_range — clamp to last point
            confidence = "low"
            estimated_distance_m = points[-1].distance_m
        else:
            confidence = "high"

        return {
            "estimated_distance_m": float(max(100.0, estimated_distance_m)),
            "confidence": confidence,
            "propagation_mode": prop_mode,
            "profile": profile,
        }

    @staticmethod
    def _estimate_covered_area(points: list[CoveragePoint],
                                min_signal_dbm: float) -> float:
        """Rough covered area estimate (km²) from coverage points."""
        covered = [p for p in points if p.is_covered]
        if not covered:
            return 0.0
        # Approximate as sum of small sectors
        max_r = max(p.distance_m for p in covered) / 1000.0
        frac = len(covered) / max(len(points), 1)
        return math.pi * max_r ** 2 * frac

    @staticmethod
    def _build_geojson(points: list[CoveragePoint],
                        min_signal_dbm: float) -> dict:
        """Build GeoJSON FeatureCollection for map rendering."""
        features = []
        for p in points:
            features.append({
                "type": "Feature",
                "geometry": {
                    "type": "Point",
                    "coordinates": [p.lon, p.lat],
                },
                "properties": {
                    "signal_dbm": round(p.signal_dbm, 1),
                    "path_loss_db": round(p.path_loss_db, 1),
                    "distance_m": round(p.distance_m, 0),
                    "covered": p.is_covered,
                    "mode": p.propagation_mode,
                },
            })
        return {
            "type": "FeatureCollection",
            "features": features,
        }


# ─────────────────────────────────────────────────────────────────────────────
# Geometry helpers
# ─────────────────────────────────────────────────────────────────────────────

def _point_in_polygon(lat: float, lon: float,
                       polygon: list[tuple[float, float]]) -> bool:
    """Ray-casting point-in-polygon test. polygon is [(lat, lon), ...]."""
    n = len(polygon)
    inside = False
    j = n - 1
    for i in range(n):
        yi, xi = polygon[i]
        yj, xj = polygon[j]
        if ((yi > lat) != (yj > lat)) and (lon < (xj - xi) * (lat - yi) / (yj - yi + 1e-12) + xi):
            inside = not inside
        j = i
    return inside


# Singleton
_simulator: Optional[RFSimulator] = None


def get_simulator() -> RFSimulator:
    global _simulator
    if _simulator is None:
        _simulator = RFSimulator()
    return _simulator
