"""
FastAPI Routes
REST + WebSocket endpoints for the RF propagation simulator.
"""
import json
import math
import asyncio
import logging
from typing import Optional, Any
from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from app.config import settings
from app.core.simulation import (
    RFSimulator, get_simulator,
    CoverageRequest, PointToPointRequest,
    TransmitterConfig, ReceiverConfig,
)
from app.core.propagation.antenna import AntennaConfig, AntennaType, ANTENNA_CATALOGUE
from app.core.propagation.models import PropagationModel
from app.core.propagation.space_weather import fetch_space_weather
from app.core.propagation.terrain import TerrainManager, haversine_distance, destination_point
from app.core.propagation.atmosphere import (
    get_surface_refractivity, compute_muf, compute_luf
)
from app.core.propagation.space_weather import SpaceWeatherState
from app.core.propagation.materials import material_info
from app.core.propagation.ray_tracer import RayTraceRequest, get_ray_tracer

log = logging.getLogger(__name__)
router = APIRouter()


# ─────────────────────────────────────────────────────────────────────────────
# Pydantic models for request validation
# ─────────────────────────────────────────────────────────────────────────────

class AntennaModel(BaseModel):
    type: str = "dipole_half_wave"
    gain_dbi: Optional[float] = None
    tilt_deg: float = 0.0
    azimuth_deg: float = 0.0
    height_m: float = settings.default_emitter_agl_m  # 6ft AGL default
    diameter_m: float = 1.2
    efficiency: float = 0.55
    elements: int = 9
    array_elements: int = 64
    polarization: str = "vertical"
    frequency_hz: float = 433e6
    custom_pattern_json: Optional[str] = None


class TransmitterModel(BaseModel):
    lat: float = Field(37.7749, ge=-90, le=90)
    lon: float = Field(-122.4194, ge=-180, le=180)
    height_m: float = Field(settings.default_emitter_agl_m, ge=0, le=10000)  # 6ft AGL default
    altitude_m: float = Field(0.0, ge=0, le=30000)  # sea level to 30k ft
    power_dbm: float = Field(27.0, ge=-30, le=100)
    frequency_hz: float = Field(433e6, ge=1, le=300e9)
    antenna: AntennaModel = Field(default_factory=AntennaModel)


class ReceiverModel(BaseModel):
    height_m: float = Field(1.5, ge=0, le=10000)
    altitude_m: float = Field(0.0, ge=0, le=30000)
    sensitivity_dbm: float = Field(-100.0, le=0)
    noise_figure_db: float = Field(3.0, ge=0)
    required_snr_db: float = Field(10.0, ge=0)
    antenna: AntennaModel = Field(default_factory=AntennaModel)


class AtmosphereModel(BaseModel):
    temperature_c: float = 15.0
    pressure_hpa: float = 1013.25
    humidity_percent: float = Field(60.0, ge=0, le=100)
    rain_rate_mm_per_hr: float = Field(0.0, ge=0, le=300)
    visibility_km: float = Field(10.0, ge=0.01)
    refractivity_gradient: float = -40.0  # dN/dh in N-units/km
    altitude_m: float = 0.0


class CoverageRequestModel(BaseModel):
    transmitter: TransmitterModel = Field(default_factory=TransmitterModel)
    receiver: ReceiverModel = Field(default_factory=ReceiverModel)
    propagation_model: str = "itm"
    wave_type: str = "auto"
    radius_km: float = Field(50.0, gt=0, le=2000)
    num_radials: int = Field(360, ge=8, le=3600)
    points_per_radial: int = Field(300, ge=10, le=2000)
    min_signal_dbm: float = Field(-120.0, le=0)
    atmosphere: Optional[AtmosphereModel] = None
    use_gpu: bool = False
    terrain_resolution: str = "srtm3"
    include_buildings: bool = False
    fetch_space_weather: bool = True
    utc_datetime: Optional[str] = None
    # Context: 1=urban/conservative/D-layer, 2=average/E-layer, 3=rural/optimistic/F-layer
    context: int = Field(2, ge=1, le=3)
    # Diffraction model applied on top of empirical path loss models
    diffraction_model: str = "none"   # none | single_knife_edge | epstein_peterson | bullington | giovanelli | deygout
    # Radar-specific
    rcs_m2: float = Field(1.0, gt=0)   # Radar cross section (m²)
    # Clutter/land cover
    clutter_height_m: float = Field(0.0, ge=0)   # additional clutter height above terrain
    # Polar (azimuth-plane) radiation pattern.  See polar_patterns.POLAR_PATTERNS
    # for the catalogue of ids.  "omni" preserves the antenna's natural pattern.
    polar_pattern: str = "omni"
    # Peak antenna gain (dBi) — used as the boresight gain when polar_pattern
    # is set.  None falls back to the antenna config's own gain.
    polar_peak_gain_dbi: Optional[float] = Field(None, ge=-20.0, le=80.0)
    # Scanning-radar sweep arc in degrees, centered on the antenna boresight.
    # 0 = focused (no sweep). 360 = effectively omni. Otherwise the polar
    # pattern is azimuth-averaged across the swept arc; outside the arc the
    # raw pattern gain at that offset is used.
    sweep_deg: float = Field(0.0, ge=0.0, le=360.0)
    # OSM building fetch radius (metres) — used when include_buildings is True
    buildings_radius_m: float = Field(500.0, ge=50.0, le=10000.0)


class P2PRequestModel(BaseModel):
    transmitter: TransmitterModel = Field(default_factory=TransmitterModel)
    receiver_lat: float = Field(37.9, ge=-90, le=90)
    receiver_lon: float = Field(-122.0, ge=-180, le=180)
    receiver_height_m: float = Field(1.5, ge=0)
    receiver_altitude_m: float = Field(0.0, ge=0, le=30000)
    propagation_model: str = "itm"
    wave_type: str = "auto"
    atmosphere: Optional[AtmosphereModel] = None
    use_gpu: bool = False
    fetch_space_weather: bool = True
    utc_datetime: Optional[str] = None
    num_profile_points: int = Field(512, ge=10, le=2000)
    context: int = Field(2, ge=1, le=3)
    diffraction_model: str = "none"
    rcs_m2: float = Field(1.0, gt=0)
    clutter_height_m: float = Field(0.0, ge=0)


class LoBRangeEstimateRequest(BaseModel):
    observer_lat: float = Field(..., ge=-90, le=90)
    observer_lon: float = Field(..., ge=-180, le=180)
    observer_height_m: float = Field(1.5, ge=0, le=500)
    azimuth_deg: float = Field(..., ge=0, le=360)
    frequency_hz: float = Field(..., gt=0)
    tx_power_dbm: float
    observed_rssi_dbm: float
    propagation_model: str = "itm"
    diffraction_model: str = "deygout"
    clutter_height_m: float = Field(0.0, ge=0, le=200)
    terrain_resolution: str = "srtm1"
    context: int = Field(2, ge=1, le=3)
    max_range_km: float = Field(150.0, gt=0, le=500)
    num_points: int = Field(300, ge=50, le=1000)
    atmosphere: Optional[AtmosphereModel] = None


class BestSiteCandidateModel(BaseModel):
    lat: float = Field(..., ge=-90, le=90)
    lon: float = Field(..., ge=-180, le=180)
    height_m: float = Field(30.0, ge=0)
    label: str = ""


class BestSiteRequestModel(BaseModel):
    candidates: list[BestSiteCandidateModel] = Field(..., min_length=2, max_length=20)
    base_transmitter: TransmitterModel = Field(default_factory=TransmitterModel)
    receiver: ReceiverModel = Field(default_factory=ReceiverModel)
    propagation_model: str = "itm"
    wave_type: str = "auto"
    radius_km: float = Field(30.0, gt=0, le=500)
    num_radials: int = Field(180, ge=8, le=720)
    points_per_radial: int = Field(150, ge=10, le=500)
    min_signal_dbm: float = Field(-100.0, le=0)
    atmosphere: Optional[AtmosphereModel] = None
    use_gpu: bool = False
    terrain_resolution: str = "srtm3"
    context: int = Field(2, ge=1, le=3)
    diffraction_model: str = "none"


# ─────────────────────────────────────────────────────────────────────────────
# Converters
# ─────────────────────────────────────────────────────────────────────────────

def _build_antenna(m: AntennaModel) -> AntennaConfig:
    try:
        atype = AntennaType(m.type)
    except ValueError:
        atype = AntennaType.DIPOLE_HALF_WAVE
    return AntennaConfig(
        type=atype, gain_dbi=m.gain_dbi, tilt_deg=m.tilt_deg,
        azimuth_deg=m.azimuth_deg, height_m=m.height_m,
        diameter_m=m.diameter_m, efficiency=m.efficiency,
        elements=m.elements, array_elements=m.array_elements,
        polarization=m.polarization, frequency_hz=m.frequency_hz,
        custom_pattern_json=m.custom_pattern_json,
    )


def _build_transmitter(m: TransmitterModel) -> TransmitterConfig:
    return TransmitterConfig(
        lat=m.lat, lon=m.lon, height_m=m.height_m,
        altitude_m=m.altitude_m, power_dbm=m.power_dbm,
        frequency_hz=m.frequency_hz,
        antenna=_build_antenna(m.antenna),
    )


def _build_receiver(m: ReceiverModel) -> ReceiverConfig:
    return ReceiverConfig(
        height_m=m.height_m, altitude_m=m.altitude_m,
        sensitivity_dbm=m.sensitivity_dbm,
        noise_figure_db=m.noise_figure_db,
        required_snr_db=m.required_snr_db,
        antenna=_build_antenna(m.antenna),
    )


def _build_coverage_req(m: CoverageRequestModel) -> CoverageRequest:
    try:
        model = PropagationModel(m.propagation_model)
    except ValueError:
        model = PropagationModel.ITM
    return CoverageRequest(
        transmitter=_build_transmitter(m.transmitter),
        receiver=_build_receiver(m.receiver),
        propagation_model=model,
        wave_type=m.wave_type,
        radius_km=m.radius_km,
        num_radials=m.num_radials,
        points_per_radial=m.points_per_radial,
        min_signal_dbm=m.min_signal_dbm,
        atmosphere=m.atmosphere.dict() if m.atmosphere else None,
        use_gpu=m.use_gpu,
        terrain_resolution=m.terrain_resolution,
        include_buildings=m.include_buildings,
        fetch_space_weather=m.fetch_space_weather,
        utc_datetime=m.utc_datetime,
        context=m.context,
        diffraction_model=m.diffraction_model if m.diffraction_model != "none" else None,
        rcs_m2=m.rcs_m2,
        clutter_height_m=m.clutter_height_m,
        polar_pattern=m.polar_pattern,
        polar_peak_gain_dbi=m.polar_peak_gain_dbi,
        sweep_deg=m.sweep_deg,
        buildings_radius_m=m.buildings_radius_m,
    )


# ─────────────────────────────────────────────────────────────────────────────
# API Routes
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/health")
async def health():
    """Health check endpoint."""
    return {"status": "ok", "version": settings.app_version}


@router.get("/propagation/auto_select")
async def auto_select_model(
    frequency_hz: float = Query(..., gt=0),
    radius_km: float = Query(50.0, gt=0),
    tx_altitude_m: float = Query(0.0, ge=0),
    rx_altitude_m: float = Query(0.0, ge=0),
    environment: str = Query("auto", description="auto|urban|suburban|rural|maritime|airborne"),
):
    """
    Recommend the best propagation model given link parameters.
    Returns model ID + reasoning.
    """
    freq_mhz = frequency_hz / 1e6
    is_airborne = tx_altitude_m > 150 or rx_altitude_m > 150
    is_hf = freq_mhz < 30
    is_vhf = 30 <= freq_mhz < 300
    is_uhf = 300 <= freq_mhz < 3000
    is_micro = freq_mhz >= 3000

    if is_airborne:
        if 100 <= freq_mhz <= 15500:
            model = "itu_p528"
            reason = "Airborne link detected — ITU-R P.528 is the standard for air-ground propagation"
        else:
            model = "fspl"
            reason = "Airborne link at extreme frequency — Free Space Path Loss (FSPL)"
    elif is_hf:
        if radius_km > 200:
            model = "itu_p1546"
            reason = "Long-distance HF path — ITU-R P.1546 handles ionospheric effects"
        else:
            model = "itm"
            reason = "Short HF path — ITM/Longley-Rice accounts for terrain"
    elif is_vhf or (is_uhf and freq_mhz < 1500):
        if environment in ("urban",):
            model = "hata_urban" if freq_mhz <= 1500 else "cost231_hata"
            reason = f"VHF/UHF urban environment — {'Hata Urban' if freq_mhz <= 1500 else 'COST-231 Hata'}"
        elif environment in ("suburban",):
            model = "hata_suburban"
            reason = "VHF/UHF suburban environment — Hata Suburban"
        elif environment in ("maritime",):
            model = "two_ray"
            reason = "Maritime path — Two-Ray ground reflection model"
        else:
            model = "itm"
            reason = "VHF/UHF general terrain — ITM/Longley-Rice best accuracy"
    elif is_uhf and freq_mhz < 3000:
        if environment in ("urban", "suburban"):
            model = "cost231_hata"
            reason = "Upper UHF urban/suburban — COST-231 Hata (1500–2000 MHz)"
        else:
            model = "itm"
            reason = "Upper UHF with terrain — ITM/Longley-Rice"
    elif is_micro:
        if freq_mhz >= 6000 and radius_km > 5:
            model = "two_ray"
            reason = "Microwave >6 GHz over moderate range — Two-Ray reflection model"
        else:
            model = "fspl"
            reason = "Microwave line-of-sight — Free Space Path Loss"
    else:
        model = "itm"
        reason = "General purpose — ITM/Longley-Rice terrain model"

    return {
        "model": model,
        "reason": reason,
        "frequency_mhz": round(freq_mhz, 3),
        "is_airborne": is_airborne,
    }


@router.post("/simulate/coverage")
async def simulate_coverage(req: CoverageRequestModel):
    """
    Compute RF coverage area.
    Returns GeoJSON FeatureCollection + metadata for heatmap rendering.
    """
    sim = get_simulator()
    try:
        sim_req = _build_coverage_req(req)
        result = await sim.compute_coverage(sim_req)
        return {
            "status": "ok",
            "geojson": result.geojson,
            "metadata": {
                "max_range_km": result.max_range_km,
                "avg_signal_dbm": result.avg_signal_dbm,
                "covered_area_km2": result.covered_area_km2,
                "space_weather": result.space_weather,
                "warnings": result.warnings,
                "computation_time_s": result.computation_time_s,
                "gpu_used": result.gpu_used,
                "num_points": len(result.points),
            },
        }
    except Exception as e:
        log.exception("Coverage simulation error")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/simulate/coverage_raster")
async def simulate_coverage_raster(req: CoverageRequestModel, grid_size: int = 48):
    """Per-pixel raster coverage: one ITM path per cell of a grid_size×grid_size lat/lon
    grid over ±radius_km — even coverage everywhere (no radial thinning at range). Heavier
    than /simulate/coverage; grid_size clamped to ≤ 96. Same response shape."""
    sim = get_simulator()
    try:
        result = await sim.compute_coverage_raster(_build_coverage_req(req), grid_size=grid_size)
        return {
            "status": "ok", "geojson": result.geojson,
            "metadata": {
                "mode": "raster", "max_range_km": result.max_range_km, "avg_signal_dbm": result.avg_signal_dbm,
                "covered_area_km2": result.covered_area_km2, "computation_time_s": result.computation_time_s,
                "num_points": len(result.points),
            },
        }
    except Exception as e:
        log.exception("Raster coverage error")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/simulate/p2p")
async def simulate_p2p(req: P2PRequestModel):
    """
    Point-to-point link budget and terrain profile.
    Returns terrain elevation, LOS line, Fresnel zones, full link budget.
    """
    sim = get_simulator()
    try:
        model = PropagationModel(req.propagation_model) if req.propagation_model else PropagationModel.ITM
        p2p_req = PointToPointRequest(
            transmitter=_build_transmitter(req.transmitter),
            receiver_lat=req.receiver_lat,
            receiver_lon=req.receiver_lon,
            receiver_height_m=req.receiver_height_m,
            receiver_altitude_m=req.receiver_altitude_m,
            propagation_model=model,
            wave_type=req.wave_type,
            atmosphere=req.atmosphere.dict() if req.atmosphere else None,
            use_gpu=req.use_gpu,
            fetch_space_weather=req.fetch_space_weather,
            utc_datetime=req.utc_datetime,
            num_profile_points=req.num_profile_points,
            context=req.context,
            diffraction_model=req.diffraction_model if req.diffraction_model != "none" else None,
            rcs_m2=req.rcs_m2,
            clutter_height_m=req.clutter_height_m,
        )
        result = await sim.compute_point_to_point(p2p_req)
        return {
            "status": "ok",
            "profile": {
                "distances_m": result.distances_m,
                "elevations_m": result.elevations_m,
                "los_heights_m": result.los_heights_m,
                "fresnel_radii_m": result.fresnel_radii_m,
                "total_distance_m": result.total_distance_m,
            },
            "result": {
                "path_loss_db": result.path_loss_db,
                "received_signal_dbm": result.received_signal_dbm,
                "propagation_mode": result.propagation_mode,
                "link_budget": result.link_budget,
                "warnings": result.warnings,
                "space_weather": result.space_weather,
            },
        }
    except Exception as e:
        log.exception("P2P simulation error")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/lob/range_estimate")
async def lob_range_estimate(req: LoBRangeEstimateRequest):
    """
    Terrain-aware LoB range estimation.
    Runs a single terrain radial from the observer in the bearing direction
    and returns the interpolated distance where signal == observed RSSI.
    """
    sim = get_simulator()
    try:
        result = await sim.compute_lob_range(
            observer_lat=req.observer_lat,
            observer_lon=req.observer_lon,
            observer_height_m=req.observer_height_m,
            azimuth_deg=req.azimuth_deg,
            frequency_hz=req.frequency_hz,
            tx_power_dbm=req.tx_power_dbm,
            observed_rssi_dbm=req.observed_rssi_dbm,
            propagation_model=req.propagation_model,
            diffraction_model=req.diffraction_model,
            clutter_height_m=req.clutter_height_m,
            terrain_resolution=req.terrain_resolution,
            context=req.context,
            max_range_km=req.max_range_km,
            num_points=req.num_points,
            atmosphere=req.atmosphere.dict() if req.atmosphere else None,
        )
        return {"status": "ok", **result}
    except Exception as e:
        log.exception("LoB range estimate error")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/simulate/best_site")
async def simulate_best_site(req: BestSiteRequestModel):
    """
    Best Site Analysis — run coverage from multiple candidate sites in parallel
    and rank them by covered area and average signal strength.
    Returns per-site metrics plus the best site's GeoJSON for visualisation.
    """
    sim = get_simulator()
    try:
        model_enum = PropagationModel.ITM
        try:
            model_enum = PropagationModel(req.propagation_model)
        except ValueError:
            pass

        results = []
        tasks = []
        for cand in req.candidates:
            tx_m = req.base_transmitter.model_copy(update={
                "lat": cand.lat, "lon": cand.lon, "height_m": cand.height_m
            })
            cov_req = CoverageRequest(
                transmitter=_build_transmitter(tx_m),
                receiver=_build_receiver(req.receiver),
                propagation_model=model_enum,
                wave_type=req.wave_type,
                radius_km=req.radius_km,
                num_radials=req.num_radials,
                points_per_radial=req.points_per_radial,
                min_signal_dbm=req.min_signal_dbm,
                atmosphere=req.atmosphere.dict() if req.atmosphere else None,
                use_gpu=req.use_gpu,
                terrain_resolution=req.terrain_resolution,
                fetch_space_weather=False,
                context=req.context,
                diffraction_model=req.diffraction_model if req.diffraction_model != "none" else None,
            )
            tasks.append(sim.compute_coverage(cov_req))

        site_results = await asyncio.gather(*tasks, return_exceptions=True)

        best_idx = 0
        best_score = float('-inf')
        for i, (cand, res) in enumerate(zip(req.candidates, site_results)):
            if isinstance(res, Exception):
                results.append({
                    "label": cand.label or f"Site {i+1}",
                    "lat": cand.lat, "lon": cand.lon,
                    "error": str(res),
                })
                continue
            score = res.covered_area_km2 + max(0.0, 100.0 + res.avg_signal_dbm)
            results.append({
                "label": cand.label or f"Site {i+1}",
                "lat": cand.lat, "lon": cand.lon,
                "height_m": cand.height_m,
                "covered_area_km2": round(res.covered_area_km2, 2),
                "avg_signal_dbm": round(res.avg_signal_dbm, 1),
                "max_range_km": round(res.max_range_km, 2),
                "score": round(score, 2),
            })
            if score > best_score:
                best_score = score
                best_idx = i

        # Sort by score
        scored = [r for r in results if "score" in r]
        scored.sort(key=lambda x: x["score"], reverse=True)

        best_geojson = None
        best_res = site_results[best_idx]
        if not isinstance(best_res, Exception):
            best_geojson = best_res.geojson

        return {
            "status": "ok",
            "sites": scored,
            "best_site_index": best_idx,
            "best_geojson": best_geojson,
        }
    except Exception as e:
        log.exception("Best site analysis error")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/terrain/profile")
async def terrain_profile(
    lat1: float = Query(..., ge=-90, le=90),
    lon1: float = Query(..., ge=-180, le=180),
    lat2: float = Query(..., ge=-90, le=90),
    lon2: float = Query(..., ge=-180, le=180),
    resolution: str = Query("srtm3"),
    num_points: int = Query(512, ge=10, le=2000),
):
    """Get terrain elevation profile between two points."""
    terrain = TerrainManager(resolution=resolution)
    try:
        dist_arr, elev_arr = await terrain.get_elevation_profile(
            lat1, lon1, lat2, lon2, num_points
        )
        total_dist = haversine_distance(lat1, lon1, lat2, lon2)
        return {
            "distances_m": dist_arr.tolist(),
            "elevations_m": elev_arr.tolist(),
            "total_distance_m": total_dist,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/terrain/elevation")
async def terrain_elevation(
    lat: float = Query(..., ge=-90, le=90),
    lon: float = Query(..., ge=-180, le=180),
):
    """Get elevation at a single point."""
    terrain = TerrainManager()
    try:
        elev = await terrain.get_elevation(lat, lon)
        return {"lat": lat, "lon": lon, "elevation_m": elev}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/terrain/buildings")
async def terrain_buildings(
    lat: float = Query(..., ge=-90, le=90),
    lon: float = Query(..., ge=-180, le=180),
    radius_m: float = Query(500.0, ge=50, le=10000),
):
    """
    Fetch OSM building footprints as GeoJSON FeatureCollection.
    Polygon geometries + height/material properties for map overlay.

    Degrades gracefully when offline (Workstream A.3): if the live Overpass query
    fails, footprints are served from an installed ``buildings`` data pack that
    covers the point (``source: "pack"``); only errors when neither is available.
    """
    terrain = TerrainManager()
    try:
        buildings = await terrain.get_buildings(lat, lon, radius_m)
        geojson = terrain.buildings_to_geojson(buildings)
        return {"geojson": geojson, "count": len(buildings), "source": "live"}
    except Exception as e:
        from app.core import packs as _packs
        pack_fc = _packs.buildings_near(lat, lon, radius_m)
        if pack_fc is not None:
            return {"geojson": pack_fc, "count": len(pack_fc.get("features", [])),
                    "source": "pack", "degraded_reason": f"{type(e).__name__}: {e}"}
        raise HTTPException(status_code=503, detail=f"Buildings unavailable (no live Overpass, no covering pack): {e}")
    finally:
        await terrain.close()


@router.get("/terrain/grid")
async def terrain_grid(
    lat: float = Query(..., ge=-90, le=90),
    lon: float = Query(..., ge=-180, le=180),
    radius_km: float = Query(5.0, ge=0.5, le=100),
    grid_size: int = Query(30, ge=5, le=60),
    resolution: str = Query("srtm3", pattern="^(srtm1|srtm3)$"),
):
    """
    Return a 2D elevation grid around (lat, lon) for 3D terrain rendering — the real terrain
    (SRTM, or a covering offline pack). grid_size × grid_size points covering ±radius_km from
    centre. Adds `flat: true` when no terrain source was reachable (all-zero grid) so the
    client can say so rather than imply the area is genuinely flat.
    """
    terrain = TerrainManager(resolution=resolution)
    try:
        grid = await terrain.get_elevation_grid(lat, lon, radius_km, grid_size)
        try:
            rows = grid.get("elevations") or []
            flat_vals = [v for r in rows for v in r]
            grid["flat"] = bool(flat_vals) and (max(flat_vals) - min(flat_vals) < 1.0)
            grid["resolution"] = resolution
        except Exception:
            pass
        return grid
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        await terrain.close()


@router.get("/terrain/contours")
async def terrain_contours(
    lat: float = Query(..., ge=-90, le=90),
    lon: float = Query(..., ge=-180, le=180),
    radius_km: float = Query(10.0, ge=0.5, le=100),
    interval_m: float = Query(50.0, ge=1, le=2000),
    grid_size: int = Query(80, ge=20, le=200),
    resolution: str = Query("srtm3", pattern="^(srtm1|srtm3)$"),
):
    """
    Generate elevation contour lines as GeoJSON.

    Samples a `grid_size × grid_size` elevation grid spanning ±radius_km from
    (lat, lon), then runs matplotlib's contour generator at `interval_m`
    intervals between the grid's min and max heights. Returns a FeatureCollection
    of LineString features tagged with `elevation_m` so the client can style
    bands (every 5th line darker, etc.).
    """
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import numpy as np

    terrain = TerrainManager(resolution=resolution)
    try:
        grid = await terrain.get_elevation_grid(lat, lon, radius_km, grid_size)
        rows = grid.get("elevations") or []
        if not rows or not rows[0]:
            return {"type": "FeatureCollection", "features": [], "note": "no terrain data"}
        Z = np.array(rows, dtype=float)
        if Z.max() - Z.min() < 0.5:
            return {"type": "FeatureCollection", "features": [], "note": "area is effectively flat"}
        # Build the lat/lon grid axes. get_elevation_grid samples in a square
        # centered on (lat, lon); we reconstruct that span here so contour
        # coordinates land in real-world lat/lon for the GeoJSON output.
        # ±radius_km in metres → degrees (~111_320 m per degree latitude).
        deg_lat = radius_km * 1000.0 / 111_320.0
        deg_lon = deg_lat / max(0.1, math.cos(math.radians(lat)))
        lons = np.linspace(lon - deg_lon, lon + deg_lon, Z.shape[1])
        lats = np.linspace(lat - deg_lat, lat + deg_lat, Z.shape[0])
        zmin = float(np.floor(Z.min() / interval_m) * interval_m)
        zmax = float(np.ceil(Z.max() / interval_m) * interval_m)
        levels = np.arange(zmin, zmax + interval_m, interval_m)
        if len(levels) > 200:
            raise HTTPException(400, f"too many contour levels ({len(levels)}); raise interval_m")
        fig, ax = plt.subplots()
        try:
            cs = ax.contour(lons, lats, Z, levels=levels)
            features = []
            # Newer matplotlib (3.8+) exposes paths via cs.get_paths() per level.
            for level_idx, level in enumerate(cs.levels):
                try:
                    seg_lists = cs.allsegs[level_idx]
                except Exception:
                    seg_lists = []
                for seg in seg_lists:
                    if len(seg) < 2:
                        continue
                    coords = [[float(x), float(y)] for x, y in seg]
                    features.append({
                        "type": "Feature",
                        "geometry": {"type": "LineString", "coordinates": coords},
                        "properties": {"elevation_m": float(level)},
                    })
            return {"type": "FeatureCollection", "features": features,
                    "metadata": {"levels": [float(l) for l in levels], "interval_m": interval_m,
                                 "min_m": float(Z.min()), "max_m": float(Z.max())}}
        finally:
            plt.close(fig)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        await terrain.close()


class ViewshedRequest(BaseModel):
    lat: float = Field(..., ge=-90, le=90)
    lon: float = Field(..., ge=-180, le=180)
    observer_height_m: float = Field(2.0, ge=0, le=500)
    target_height_m: float = Field(2.0, ge=0, le=500)
    radius_km: float = Field(10.0, gt=0, le=50)
    num_radials: int = Field(180, ge=16, le=720)
    points_per_radial: int = Field(80, ge=10, le=400)
    resolution: str = Field("srtm3", pattern="^(srtm1|srtm3)$")
    earth_curvature: bool = Field(True)


@router.post("/viewshed")
async def viewshed(req: ViewshedRequest):
    """
    Binary line-of-sight viewshed from an observer.

    Walks `num_radials` outward from (lat, lon), sampling elevation along each
    bearing. A point is "visible" if no terrain between observer and target
    rises above the straight-line ray at that point (accounting for Earth
    curvature when enabled). Returns the visible region as a GeoJSON polygon
    so the client renders it as a single coverage-style overlay.

    This is the pure-geometry LoS upper bound — no RF, no diffraction. Distinct
    from /simulate/coverage which models actual signal propagation.
    """
    import numpy as np

    terrain = TerrainManager(resolution=req.resolution)
    try:
        # Sample observer height first (so we can detect "observer underground"
        # and surface a useful error rather than a degenerate viewshed).
        obs_terrain = await terrain.get_elevation(req.lat, req.lon)
        obs_alt = (obs_terrain or 0.0) + req.observer_height_m
        # k=4/3 Earth radius approximation for refractive bending of the ray.
        R_eff = 6_371_000.0 * (4.0 / 3.0 if req.earth_curvature else 1.0)
        # Reach: equivalent metres at each step.
        step_m = (req.radius_km * 1000.0) / req.points_per_radial
        boundary = []   # [(lat, lon), ...] visible-edge points, one per radial
        for ri in range(req.num_radials):
            az = (360.0 / req.num_radials) * ri
            last_visible_d = 0.0
            blocked = False
            for ki in range(1, req.points_per_radial + 1):
                d = ki * step_m
                p = destination_point(req.lat, req.lon, az, d)
                # Earth-curvature drop: terrain at distance d sits geometrically
                # lower than a flat-earth model by d² / (2 R_eff).
                t_h = (await terrain.get_elevation(p[0], p[1])) or 0.0
                drop = (d * d) / (2.0 * R_eff)
                # Required ray altitude at this distance for LoS to a target_height_m
                # antenna there: must be ≥ ground + target_height - drop.
                target_floor = t_h + req.target_height_m - drop
                # Observer-line altitude AT distance d (linear from obs_alt towards target_floor
                # only matters if we compare against intervening terrain).
                # Use the steepest-ratio test: visible iff (target_floor - obs_alt) / d >=
                # max over k<=ki of (terrain_k - obs_alt) / d_k. We approximate by tracking
                # the max ratio seen so far on this radial.
                if ki == 1:
                    max_ratio = (target_floor - obs_alt) / d
                    last_visible_d = d
                    continue
                ratio = (target_floor - obs_alt) / d
                if ratio < max_ratio:
                    # Behind the ridge — not visible. Mark and stop walking.
                    blocked = True
                    break
                max_ratio = ratio
                last_visible_d = d
            # Edge point for this radial (last visible distance, or full radius if unblocked)
            edge = destination_point(req.lat, req.lon, az, last_visible_d if blocked else req.radius_km * 1000.0)
            boundary.append([edge[1], edge[0]])    # GeoJSON is [lon, lat]
        # Close the polygon ring
        if boundary and boundary[0] != boundary[-1]:
            boundary.append(boundary[0])
        return {
            "type": "FeatureCollection",
            "features": [{
                "type": "Feature",
                "geometry": {"type": "Polygon", "coordinates": [boundary]},
                "properties": {
                    "observer": {"lat": req.lat, "lon": req.lon, "height_m": req.observer_height_m},
                    "target_height_m": req.target_height_m,
                    "radius_km": req.radius_km,
                    "kind": "viewshed",
                },
            }],
            "metadata": {
                "observer_terrain_m": float(obs_terrain or 0.0),
                "observer_total_alt_m": float(obs_alt),
                "num_radials": req.num_radials,
            },
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        await terrain.close()


@router.get("/space_weather")
async def space_weather_endpoint():
    """Current space weather from NOAA SWPC.

    Degrades gracefully when offline: returns the last-known values (``stale: true``)
    or an operator override (PUT /api/v1/net/override/space_weather) if set; only
    503s when nothing has ever been fetched and no override exists.
    """
    from app.core import net_state

    async def _fetch() -> dict:
        sw = await fetch_space_weather()
        return {
            "summary": sw.propagation_summary(),
            "raw": {
                "f10_7": sw.f10_7,
                "kp_index": sw.kp_index,
                "xray_flux": sw.xray_flux,
                "radio_blackout_class": sw.radio_blackout_class,
                "storm_class": sw.storm_class,
                "hf_blackout": sw.hf_blackout,
                "aurora_activity": sw.aurora_activity,
                "polar_cap_absorption": sw.polar_cap_absorption,
                "ionospheric_storm": sw.ionospheric_storm,
            },
        }

    try:
        res = await net_state.fetch_or_degrade("space_weather", _fetch)
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"Space weather unavailable: {e}")
    payload = res["data"]
    return {
        "status": "ok",
        "data": payload.get("summary"),
        "raw": payload.get("raw"),
        "source": res["source"],          # live | cache | override
        "stale": res["stale"],
        "as_of": res.get("as_of"),
    }


@router.get("/antenna/catalogue")
async def antenna_catalogue():
    """Return list of available antenna types with metadata."""
    catalogue = []
    for atype, meta in ANTENNA_CATALOGUE.items():
        catalogue.append({
            "id": atype.value,
            **meta,
        })
    return {"antennas": catalogue}


@router.get("/antenna/polar_patterns")
async def polar_pattern_catalogue():
    """
    Return the catalogue of polar (azimuth-plane) radiation patterns with
    derived -3 dB and -6 dB beamwidths.  These replace the historical
    hard-cutoff `beam_width_deg` parameter — the simulator uses the named
    pattern's smooth gain curve directly.
    """
    from app.core.propagation.polar_patterns import list_polar_patterns
    return {"patterns": list_polar_patterns()}


@router.get("/antenna/presets")
async def antenna_presets():
    """
    Return manufacturer antenna presets pulled from public datasheets.
    Each preset carries the fields needed to populate the antenna config
    in one click: peak gain (dBi), polar pattern, polarization, the
    closest backend AntennaType id, plus operational frequency range.
    Selecting a preset on the frontend overwrites every relevant antenna
    parameter so the simulator runs against datasheet-consistent values.
    """
    return {"presets": _ANTENNA_PRESETS}


class PatternImportRequest(BaseModel):
    format: str = "msi"          # "msi" | "planet" | "nec2"
    content: str                 # the raw file text


@router.post("/antenna/import_pattern")
async def antenna_import_pattern(req: PatternImportRequest):
    """Import a measured antenna pattern — NSMA/Planet (`.msi`/`.pln`/`.ant`) or an
    NEC-2 RP listing — and return the parsed metadata plus a ``custom_pattern_json``
    you can drop straight onto an antenna config (``type: "custom"``); the coverage
    engine then uses the *measured* pattern, not an analytic approximation."""
    from app.core.propagation.pattern_import import parse_msi, msi_to_custom_pattern_json, parse_nec2_rp
    fmt = (req.format or "msi").lower()
    try:
        if fmt in ("msi", "planet", "pln", "ant"):
            parsed = parse_msi(req.content)
            cpj = msi_to_custom_pattern_json(parsed)
            return {"status": "ok", "format": "msi", "metadata": parsed, "custom_pattern_json": cpj}
        elif fmt in ("nec2", "nec"):
            grid = parse_nec2_rp(req.content)
            if grid is None:
                raise HTTPException(400, "could not locate a RADIATION PATTERNS table in the NEC-2 listing")
            import json as _json
            return {"status": "ok", "format": "nec2",
                    "metadata": {"n_az": len(grid["azimuth"]), "n_el": len(grid["elevation"])},
                    "custom_pattern_json": _json.dumps(grid)}
        else:
            raise HTTPException(400, f"unknown pattern format {req.format!r}; expected msi | planet | nec2")
    except HTTPException:
        raise
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:  # pragma: no cover
        raise HTTPException(500, f"pattern import failed: {e}")


@router.get("/propagation/models")
async def propagation_models():
    """Return list of available propagation models with descriptions."""
    models = [
        {"id": "itm", "name": "Longley-Rice ITM", "freq_range": "20 MHz–20 GHz",
         "description": "Terrain-based irregular terrain model. Best general-purpose model."},
        {"id": "fspl", "name": "Free Space Path Loss",
         "freq_range": "All", "description": "Theoretical line-of-sight, no terrain."},
        {"id": "hata_urban", "name": "Okumura-Hata Urban",
         "freq_range": "150–1500 MHz", "description": "Empirical model for urban areas."},
        {"id": "hata_suburban", "name": "Okumura-Hata Suburban",
         "freq_range": "150–1500 MHz", "description": "Empirical model for suburban areas."},
        {"id": "hata_rural", "name": "Okumura-Hata Rural/Open",
         "freq_range": "150–1500 MHz", "description": "Empirical model for rural/open areas."},
        {"id": "cost231_hata", "name": "COST-231 Hata",
         "freq_range": "1500–2000 MHz", "description": "Extension of Hata for higher freq."},
        {"id": "two_ray", "name": "Two-Ray Ground Reflection",
         "freq_range": "All", "description": "Flat earth direct + reflected ray model."},
        {"id": "itu_p1546", "name": "ITU-R P.1546",
         "freq_range": "30–3000 MHz", "description": "ITU point-to-area predictions."},
        {"id": "itu_p528", "name": "ITU-R P.528 (Aeronautical)",
         "freq_range": "100 MHz–15.5 GHz", "description": "Air-ground propagation model."},
        {"id": "egli", "name": "Egli",
         "freq_range": "40–900 MHz", "description": "Simple rural empirical model (1957)."},
        {"id": "sui", "name": "Stanford University Interim (SUI)",
         "freq_range": "2–11 GHz", "description": "IEEE 802.16 WiMAX model."},
        {"id": "plane_earth", "name": "Plane Earth",
         "freq_range": "All", "description": "4th power law, flat earth approximation."},
        {"id": "ericsson", "name": "Ericsson 9999",
         "freq_range": "150–1900 MHz", "description": "Generalised empirical model with context coefficients (urban/average/rural)."},
        {"id": "nvis_hf", "name": "HF NVIS",
         "freq_range": "2–30 MHz", "description": "Near Vertical Incidence Skywave — short/medium range HF via ionosphere. Context sets reflective layer (D/E/F)."},
        {"id": "radar", "name": "Radar (two-way)",
         "freq_range": "All", "description": "Radar range equation — two-way path loss for target detection. Requires RCS parameter."},
    ]
    return {"models": models}


@router.get("/hf/muf")
async def hf_muf(
    lat1: float = Query(...), lon1: float = Query(...),
    lat2: float = Query(...), lon2: float = Query(...),
    freq_mhz: float = Query(14.0, gt=1.0, le=30.0, description="operating frequency to evaluate (also returns MUF/FOT/LUF)"),
    tx_power_w: float = Query(1000.0, gt=0),
    tx_gain_dbi: float = Query(2.0),
    rx_gain_dbi: float = Query(2.0),
    bandwidth_hz: float = Query(3000.0, gt=0),
    required_snr_db: float = Query(9.0),
    environment: str = Query("rural", description="noise environment: quiet_rural|rural|residential|business"),
    datetime_utc: Optional[str] = Query(None, description="ISO 8601 UTC; omit for now"),
):
    """HF sky-wave circuit prediction (ITU-R P.533-style): multi-hop F2 geometry,
    parameterised foF2, P.533 D-region absorption, MUF/FOT/HPF/LUF, basic loss,
    received SNR vs an ITU-R P.372 noise floor, and circuit reliability. Uses a
    local ``ITURHFPROP`` / ``voacapl`` binary if one is on the PATH (the reference
    engines with the CCIR/URSI coefficient maps), otherwise Ares's own model."""
    import datetime as _dt
    from app.core.propagation import hf as _hf
    from app.core.propagation.space_weather import SpaceWeatherState
    try:
        sw = await fetch_space_weather()
    except Exception:
        sw = SpaceWeatherState()
    when = None
    if datetime_utc:
        try:
            when = _dt.datetime.fromisoformat(datetime_utc.replace("Z", "+00:00"))
        except ValueError:
            raise HTTPException(400, "Invalid datetime_utc; use ISO 8601 (e.g. 2026-05-12T14:00:00Z)")
    c = _hf.predict_hf_circuit(lat1, lon1, lat2, lon2, freq_mhz, when=when,
                               f10_7=getattr(sw, "f10_7", None),
                               tx_power_w=tx_power_w, tx_gain_dbi=tx_gain_dbi, rx_gain_dbi=rx_gain_dbi,
                               bandwidth_hz=bandwidth_hz, required_snr_db=required_snr_db, environment=environment)
    return {
        "distance_km": c.distance_km, "n_hops": c.n_hops, "hop_length_km": c.hop_length_km,
        "takeoff_deg": c.takeoff_deg, "foF2_mhz": c.foF2_mhz,
        "muf_mhz": c.muf_mhz, "fot_mhz": c.fot_mhz, "hpf_mhz": c.hpf_mhz, "luf_mhz": c.luf_mhz,
        "optimal_freq_mhz": c.fot_mhz,
        "operating_freq_mhz": c.operating_freq_mhz, "mode": c.mode,
        "basic_loss_db": c.basic_loss_db, "absorption_db": c.absorption_db,
        "rx_power_dbm": c.rx_power_dbm, "noise_dbm": c.noise_dbm, "snr_db": c.snr_db,
        "required_snr_db": c.required_snr_db, "reliability_pct": c.reliability_pct,
        "control_points": c.control_points,
        "f10_7": getattr(sw, "f10_7", None),
        "backend": _hf.external_engine_available() or c.backend,
        "notes": c.notes,
    }


@router.get("/weather/current")
async def weather_current(
    lat: float = Query(..., ge=-90, le=90),
    lon: float = Query(..., ge=-180, le=180),
    datetime_utc: Optional[str] = Query(None, description="ISO 8601 UTC datetime, e.g. 2024-06-01T12:00:00Z. Omit for current."),
):
    """
    Fetch real-time or historical/forecast weather from Open-Meteo (no API key).
    Returns atmospheric parameters ready to plug into the atmosphere panel.

    Degrades gracefully when offline (Workstream A.3): on a fetch failure it returns
    the last-known weather for this location (``stale: true``) or an operator override
    (PUT /api/v1/net/override/weather:<lat>,<lon>); only 503s when nothing is cached.
    """
    import datetime as _dt
    from app.core import net_state

    target_dt: Optional[_dt.datetime] = None
    if datetime_utc:
        try:
            target_dt = _dt.datetime.fromisoformat(datetime_utc.replace("Z", "+00:00"))
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid datetime format. Use ISO 8601, e.g. 2024-06-01T12:00:00Z")

    cache_key = f"weather:{round(lat, 3)},{round(lon, 3)}" + (f"@{datetime_utc}" if datetime_utc else "")

    async def _fetch() -> dict:
        return await _open_meteo_weather(lat, lon, target_dt)

    try:
        res = await net_state.fetch_or_degrade(cache_key, _fetch)
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"Weather unavailable: {e}")
    out = dict(res["data"])
    if res["source"] != "live":
        out["source"] = res["source"]            # cache | override
    out["stale"] = res["stale"]
    out["as_of"] = res.get("as_of")
    if res.get("error"):
        out["degraded_reason"] = res["error"]
    return out


async def _open_meteo_weather(lat: float, lon: float, target_dt) -> dict:
    """The actual Open-Meteo call + parse. Raises on any failure (the caller wraps
    it in net_state.fetch_or_degrade for offline fallback)."""
    import aiohttp as _aiohttp
    import datetime as _dt

    base = "https://api.open-meteo.com/v1/forecast"
    variables = "temperature_2m,relative_humidity_2m,surface_pressure,rain,snowfall,visibility,wind_speed_10m,weather_code"

    params: dict = {
        "latitude": lat,
        "longitude": lon,
        "wind_speed_unit": "ms",
        "timezone": "UTC",
    }

    if target_dt:
        date_str = target_dt.strftime("%Y-%m-%d")
        now_utc = _dt.datetime.now(_dt.timezone.utc)
        delta_days = (now_utc.date() - target_dt.date()).days
        if delta_days > 0:
            # Historical
            params["hourly"] = variables
            params["start_date"] = date_str
            params["end_date"] = date_str
            params["past_days"] = min(delta_days + 1, 92)
        else:
            # Forecast
            params["hourly"] = variables
            params["start_date"] = date_str
            params["end_date"] = (target_dt + _dt.timedelta(days=1)).strftime("%Y-%m-%d")
            params["forecast_days"] = 16
    else:
        params["current"] = variables

    try:
        timeout = _aiohttp.ClientTimeout(total=10)
        async with _aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(base, params=params) as resp:
                if resp.status != 200:
                    raise HTTPException(status_code=503, detail="Weather API unavailable")
                data = await resp.json()
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"Weather fetch failed: {e}")

    # Extract values
    temp_c = press_hpa = humid = rain = vis = wind = None

    if "current" in data:
        c = data["current"]
        temp_c   = c.get("temperature_2m")
        press_hpa = c.get("surface_pressure")
        humid    = c.get("relative_humidity_2m")
        rain     = (c.get("rain") or 0.0) + (c.get("snowfall") or 0.0) * 10.0
        vis      = (c.get("visibility") or 10000.0) / 1000.0  # m → km
        wind     = c.get("wind_speed_10m") or 0.0
    elif "hourly" in data and target_dt:
        hourly = data["hourly"]
        times  = hourly.get("time", [])
        target_str = target_dt.strftime("%Y-%m-%dT%H:00")
        # Find closest hour
        idx = next((i for i, t in enumerate(times) if t == target_str), None)
        if idx is None and times:
            # fallback: nearest
            target_ts = target_dt.timestamp()
            idx = min(range(len(times)), key=lambda i: abs(
                _dt.datetime.fromisoformat(times[i]).replace(
                    tzinfo=_dt.timezone.utc).timestamp() - target_ts
            ))
        if idx is not None:
            def _get(key): return (hourly.get(key) or [None])[idx]
            temp_c    = _get("temperature_2m")
            press_hpa = _get("surface_pressure")
            humid     = _get("relative_humidity_2m")
            rain      = (_get("rain") or 0.0) + (_get("snowfall") or 0.0) * 10.0
            vis       = (_get("visibility") or 10000.0) / 1000.0
            wind      = _get("wind_speed_10m") or 0.0

    if temp_c is None:
        raise HTTPException(status_code=503, detail="No weather data returned for this location/time")

    # Estimate refractivity gradient from temperature + humidity
    # Standard = -40 N/km; tropical moist = -50; dry hot = -20
    humid_frac = (humid or 60) / 100.0
    temp_factor = max(0.0, min(1.0, (temp_c - 0) / 40.0)) if temp_c else 0.5
    gradient = -40 - 15 * humid_frac + 10 * (1 - temp_factor)
    gradient = round(max(-100.0, min(-15.0, gradient)), 1)

    # Rain rate: Open-Meteo gives mm/hr accumulated — estimate intensity
    rain_rate = float(rain or 0.0)
    # Visibility → fog/haze (keep as-is, clamp)
    vis_km = max(0.1, min(100.0, float(vis or 10.0)))

    timestamp = target_dt.isoformat() if target_dt else _dt.datetime.now(_dt.timezone.utc).isoformat()

    return {
        "status": "ok",
        "source": "Open-Meteo",
        "timestamp_utc": timestamp,
        "location": {"lat": lat, "lon": lon},
        "atmosphere": {
            "temperature_c":       round(float(temp_c), 1),
            "pressure_hpa":        round(float(press_hpa or 1013.25), 1),
            "humidity_percent":    round(float(humid or 60), 0),
            "rain_rate_mm_per_hr": round(rain_rate, 1),
            "visibility_km":       round(vis_km, 1),
            "refractivity_gradient": gradient,
        },
        "raw": {
            "wind_speed_ms": round(float(wind or 0.0), 1),
        },
    }


@router.get("/devices/presets")
async def device_presets():
    """Return catalogue of common radio/drone/satellite device presets.

    Each preset is enriched with a `polar_pattern` id derived from its
    `antenna_type` so the frontend can apply both fields when the user
    selects a device.
    """
    from app.core.propagation.polar_patterns import ANTENNA_TYPE_TO_POLAR_PATTERN
    enriched = []
    for d in _DEVICE_PRESETS:
        enriched.append({
            **d,
            "polar_pattern": d.get(
                "polar_pattern",
                ANTENNA_TYPE_TO_POLAR_PATTERN.get(d.get("antenna_type", ""), "omni"),
            ),
        })
    return {"devices": enriched}


_DEVICE_PRESETS = [
    # ── Tactical HF Radios ──────────────────────────────────────────────────
    {"id": "prc160", "category": "Tactical HF", "label": "AN/PRC-160(V)",
     "manufacturer": "L3Harris", "frequency_hz": 10e6,
     "freq_min_hz": 1.6e6, "freq_max_hz": 60e6,
     "power_dbm": 43, "antenna_type": "whip_quarter_wave",
     "antenna_polarization": "vertical", "antenna_gain_dbi": None, "antenna_tilt_deg": 0,
     "height_m": 2, "rx_sensitivity_dbm": -107, "rx_noise_figure_db": 6,
     "notes": "HF/VUHF manpack, ALE/ECCM"},
    {"id": "prc152", "category": "Tactical VHF/UHF", "label": "AN/PRC-152A",
     "manufacturer": "L3Harris", "frequency_hz": 150e6,
     "freq_min_hz": 30e6, "freq_max_hz": 512e6,
     "power_dbm": 37, "antenna_type": "dipole_half_wave",
     "antenna_polarization": "vertical", "antenna_gain_dbi": 2.15, "antenna_tilt_deg": 0,
     "height_m": 2, "rx_sensitivity_dbm": -113, "rx_noise_figure_db": 5,
     "notes": "MBITR successor, multiband"},
    {"id": "prc148", "category": "Tactical VHF/UHF", "label": "AN/PRC-148 MBITR",
     "manufacturer": "Thales", "frequency_hz": 150e6,
     "freq_min_hz": 30e6, "freq_max_hz": 512e6,
     "power_dbm": 37, "antenna_type": "dipole_half_wave",
     "antenna_polarization": "vertical", "antenna_gain_dbi": 2.15, "antenna_tilt_deg": 0,
     "height_m": 2, "rx_sensitivity_dbm": -113, "rx_noise_figure_db": 5,
     "notes": "Multiband Inter/Intra Team Radio"},
    {"id": "prc119", "category": "Tactical VHF", "label": "AN/PRC-119 SINCGARS",
     "manufacturer": "ITT / L3Harris", "frequency_hz": 50e6,
     "freq_min_hz": 30e6, "freq_max_hz": 87.975e6,
     "power_dbm": 40, "antenna_type": "whip_quarter_wave",
     "antenna_polarization": "vertical", "antenna_gain_dbi": None, "antenna_tilt_deg": 0,
     "height_m": 2, "rx_sensitivity_dbm": -113, "rx_noise_figure_db": 6,
     "notes": "Squad net, ECCM frequency hopping"},
    {"id": "prc117g", "category": "Tactical VHF/UHF", "label": "AN/PRC-117G",
     "manufacturer": "L3Harris", "frequency_hz": 150e6,
     "freq_min_hz": 30e6, "freq_max_hz": 512e6,
     "power_dbm": 43, "antenna_type": "dipole_half_wave",
     "antenna_polarization": "vertical", "antenna_gain_dbi": 2.15, "antenna_tilt_deg": 0,
     "height_m": 2, "rx_sensitivity_dbm": -113, "rx_noise_figure_db": 5,
     "notes": "Wideband manpack, SATCOM capable"},
    {"id": "vrc90", "category": "Tactical VHF (Vehicle)", "label": "AN/VRC-90 (SINCGARS vehicular)",
     "manufacturer": "ITT", "frequency_hz": 50e6,
     "freq_min_hz": 30e6, "freq_max_hz": 87.975e6,
     "power_dbm": 47, "antenna_type": "whip_quarter_wave",
     "antenna_polarization": "vertical", "antenna_gain_dbi": None, "antenna_tilt_deg": 0,
     "height_m": 4, "rx_sensitivity_dbm": -113, "rx_noise_figure_db": 6,
     "notes": "Vehicle-mount SINCGARS, 50W"},
    # ── MANET / Mesh Radios ──────────────────────────────────────────────────
    {"id": "silvus4200", "category": "MANET Mesh", "label": "Silvus SC-4200",
     "manufacturer": "Silvus Technologies", "frequency_hz": 4900e6,
     "freq_min_hz": 4400e6, "freq_max_hz": 5900e6,
     "power_dbm": 30, "antenna_type": "patch",
     "antenna_polarization": "vertical", "antenna_gain_dbi": 3.0, "antenna_tilt_deg": 0,
     "height_m": 2, "rx_sensitivity_dbm": -90, "rx_noise_figure_db": 8,
     "notes": "2×2 MIMO StreamCaster, IP mesh"},
    {"id": "silvus4400", "category": "MANET Mesh", "label": "Silvus SC-4400",
     "manufacturer": "Silvus Technologies", "frequency_hz": 4900e6,
     "freq_min_hz": 4400e6, "freq_max_hz": 5900e6,
     "power_dbm": 33, "antenna_type": "patch",
     "antenna_polarization": "vertical", "antenna_gain_dbi": 3.0, "antenna_tilt_deg": 0,
     "height_m": 2, "rx_sensitivity_dbm": -90, "rx_noise_figure_db": 8,
     "notes": "4×4 MIMO StreamCaster, UAS / ground"},
    {"id": "trellisware950", "category": "MANET Mesh", "label": "TrellisWare TW-950 TSM",
     "manufacturer": "TrellisWare", "frequency_hz": 350e6,
     "freq_min_hz": 225e6, "freq_max_hz": 450e6,
     "power_dbm": 37, "antenna_type": "dipole_half_wave",
     "antenna_polarization": "vertical", "antenna_gain_dbi": 2.15, "antenna_tilt_deg": 0,
     "height_m": 2, "rx_sensitivity_dbm": -110, "rx_noise_figure_db": 6,
     "notes": "Tactical Scalable MANET, UHF"},
    {"id": "mpu5", "category": "MANET Mesh", "label": "MPU-5 (Wave Relay)",
     "manufacturer": "Persistent Systems", "frequency_hz": 2400e6,
     "freq_min_hz": 2300e6, "freq_max_hz": 2500e6,
     "power_dbm": 27, "antenna_type": "patch",
     "antenna_polarization": "vertical", "antenna_gain_dbi": 3.0, "antenna_tilt_deg": 0,
     "height_m": 2, "rx_sensitivity_dbm": -90, "rx_noise_figure_db": 8,
     "notes": "Compact IP MANET, popular on UAS"},
    {"id": "domo370", "category": "MANET Mesh", "label": "Domo TW-370",
     "manufacturer": "Domo Tactical", "frequency_hz": 4900e6,
     "freq_min_hz": 4400e6, "freq_max_hz": 5900e6,
     "power_dbm": 30, "antenna_type": "patch",
     "antenna_polarization": "vertical", "antenna_gain_dbi": 3.0, "antenna_tilt_deg": 0,
     "height_m": 2, "rx_sensitivity_dbm": -90, "rx_noise_figure_db": 8,
     "notes": "MilSpec mesh, NATO STANAG"},
    # ── Satellite Devices ────────────────────────────────────────────────────
    {"id": "iridium9603", "category": "Satellite", "label": "Iridium 9603 SBD",
     "manufacturer": "Iridium", "frequency_hz": 1621e6,
     "freq_min_hz": 1616e6, "freq_max_hz": 1626.5e6,
     "power_dbm": 31, "antenna_type": "patch",
     "antenna_polarization": "circular", "antenna_gain_dbi": 0.0, "antenna_tilt_deg": 0,
     "height_m": 1, "rx_sensitivity_dbm": -106, "rx_noise_figure_db": 6,
     "notes": "Short Burst Data, L-band LEO SATCOM"},
    {"id": "garmin_inreach", "category": "Satellite", "label": "Garmin inReach",
     "manufacturer": "Garmin", "frequency_hz": 1621e6,
     "freq_min_hz": 1616e6, "freq_max_hz": 1626.5e6,
     "power_dbm": 27, "antenna_type": "patch",
     "antenna_polarization": "circular", "antenna_gain_dbi": 0.0, "antenna_tilt_deg": 0,
     "height_m": 1, "rx_sensitivity_dbm": -106, "rx_noise_figure_db": 6,
     "notes": "2-way Iridium messaging, tracking"},
    {"id": "shout_nano", "category": "Satellite", "label": "SHOUT nano",
     "manufacturer": "NAL Research", "frequency_hz": 1621e6,
     "freq_min_hz": 1616e6, "freq_max_hz": 1626.5e6,
     "power_dbm": 30, "antenna_type": "patch",
     "antenna_polarization": "circular", "antenna_gain_dbi": 0.0, "antenna_tilt_deg": 0,
     "height_m": 1, "rx_sensitivity_dbm": -106, "rx_noise_figure_db": 6,
     "notes": "Iridium SBD tracker, milspec"},
    {"id": "bgan", "category": "Satellite", "label": "BGAN Terminal",
     "manufacturer": "Inmarsat", "frequency_hz": 1600e6,
     "freq_min_hz": 1518e6, "freq_max_hz": 1675e6,
     "power_dbm": 33, "antenna_type": "patch",
     "antenna_polarization": "circular", "antenna_gain_dbi": 12.0, "antenna_tilt_deg": 0,
     "height_m": 1, "rx_sensitivity_dbm": -100, "rx_noise_figure_db": 5,
     "notes": "Broadband Global Area Network, GEO"},
    # ── UAS / Drone Datalinks ────────────────────────────────────────────────
    {"id": "dji_ocusync3_24", "category": "UAS Datalink", "label": "DJI OcuSync 3 (2.4 GHz)",
     "manufacturer": "DJI", "frequency_hz": 2437e6,
     "freq_min_hz": 2400e6, "freq_max_hz": 2483.5e6,
     "power_dbm": 31, "antenna_type": "patch",
     "antenna_polarization": "vertical", "antenna_gain_dbi": 3.0, "antenna_tilt_deg": 0,
     "height_m": 50, "rx_sensitivity_dbm": -100, "rx_noise_figure_db": 8,
     "notes": "Mavic 3 / Mini 4 Pro downlink"},
    {"id": "dji_ocusync3_58", "category": "UAS Datalink", "label": "DJI OcuSync 3 (5.8 GHz)",
     "manufacturer": "DJI", "frequency_hz": 5800e6,
     "freq_min_hz": 5725e6, "freq_max_hz": 5850e6,
     "power_dbm": 26, "antenna_type": "patch",
     "antenna_polarization": "vertical", "antenna_gain_dbi": 3.0, "antenna_tilt_deg": 0,
     "height_m": 50, "rx_sensitivity_dbm": -95, "rx_noise_figure_db": 8,
     "notes": "Mavic 3 / Mini 4 Pro, 5.8 GHz"},
    {"id": "dji_900", "category": "UAS Datalink", "label": "DJI Mini 4 Pro (900 MHz)",
     "manufacturer": "DJI", "frequency_hz": 915e6,
     "freq_min_hz": 902e6, "freq_max_hz": 928e6,
     "power_dbm": 30, "antenna_type": "dipole_half_wave",
     "antenna_polarization": "vertical", "antenna_gain_dbi": 2.15, "antenna_tilt_deg": 0,
     "height_m": 50, "rx_sensitivity_dbm": -103, "rx_noise_figure_db": 7,
     "notes": "900 MHz band, long-range mode"},
    {"id": "skydio_x2", "category": "UAS Datalink", "label": "Skydio X2D",
     "manufacturer": "Skydio", "frequency_hz": 915e6,
     "freq_min_hz": 900e6, "freq_max_hz": 928e6,
     "power_dbm": 30, "antenna_type": "dipole_half_wave",
     "antenna_polarization": "vertical", "antenna_gain_dbi": 2.15, "antenna_tilt_deg": 0,
     "height_m": 50, "rx_sensitivity_dbm": -103, "rx_noise_figure_db": 7,
     "notes": "Primary 900 MHz + 5.8 GHz fallback"},
    {"id": "neros_archer", "category": "UAS Datalink", "label": "Neros Archer",
     "manufacturer": "Neros", "frequency_hz": 1800e6,
     "freq_min_hz": 300e6, "freq_max_hz": 4400e6,
     "power_dbm": 33, "antenna_type": "patch",
     "antenna_polarization": "vertical", "antenna_gain_dbi": 3.0, "antenna_tilt_deg": 0,
     "height_m": 50, "rx_sensitivity_dbm": -100, "rx_noise_figure_db": 7,
     "notes": "Wideband tactical UAS datalink"},
    {"id": "aerojet_tw970", "category": "UAS Datalink", "label": "TrellisWare TW-970 UAS",
     "manufacturer": "TrellisWare", "frequency_hz": 2400e6,
     "freq_min_hz": 2300e6, "freq_max_hz": 2500e6,
     "power_dbm": 30, "antenna_type": "patch",
     "antenna_polarization": "vertical", "antenna_gain_dbi": 3.0, "antenna_tilt_deg": 0,
     "height_m": 50, "rx_sensitivity_dbm": -95, "rx_noise_figure_db": 7,
     "notes": "MANET mesh, airborne node"},
    # ── Russian Military Radios ──────────────────────────────────────────────
    {"id": "r168_akveduk", "category": "Russian Tactical", "label": "R-168-5UN-2 Akveduk",
     "manufacturer": "Elaks", "frequency_hz": 8e6,
     "freq_min_hz": 1.5e6, "freq_max_hz": 30e6,
     "power_dbm": 43, "antenna_type": "whip_quarter_wave",
     "antenna_polarization": "vertical", "antenna_gain_dbi": None, "antenna_tilt_deg": 0,
     "height_m": 2, "rx_sensitivity_dbm": -110, "rx_noise_figure_db": 6,
     "notes": "HF/VHF manpack, ALE, freq-hopping; replaces R-159/R-143"},
    {"id": "azart_r187", "category": "Russian Tactical", "label": "Azart R-187-P1",
     "manufacturer": "INTERN", "frequency_hz": 150e6,
     "freq_min_hz": 30e6, "freq_max_hz": 512e6,
     "power_dbm": 37, "antenna_type": "dipole_half_wave",
     "antenna_polarization": "vertical", "antenna_gain_dbi": 2.15, "antenna_tilt_deg": 0,
     "height_m": 2, "rx_sensitivity_dbm": -113, "rx_noise_figure_db": 5,
     "notes": "VHF/UHF MANET manpack, ATAK-compatible, freq-hopping"},
    # ── Chinese PLA Military Radios ──────────────────────────────────────────
    {"id": "cs_vrc8b", "category": "Chinese Tactical", "label": "CS/VRC8B",
     "manufacturer": "CETC", "frequency_hz": 50e6,
     "freq_min_hz": 30e6, "freq_max_hz": 87.975e6,
     "power_dbm": 40, "antenna_type": "whip_quarter_wave",
     "antenna_polarization": "vertical", "antenna_gain_dbi": None, "antenna_tilt_deg": 0,
     "height_m": 2, "rx_sensitivity_dbm": -113, "rx_noise_figure_db": 6,
     "notes": "PLA SINCGARS-equivalent VHF manpack, freq-hopping"},
    {"id": "pla_type030", "category": "Chinese Tactical", "label": "PLA Type-030 JTDRS",
     "manufacturer": "CETC", "frequency_hz": 2400e6,
     "freq_min_hz": 2300e6, "freq_max_hz": 2500e6,
     "power_dbm": 30, "antenna_type": "patch",
     "antenna_polarization": "vertical", "antenna_gain_dbi": 3.0, "antenna_tilt_deg": 0,
     "height_m": 2, "rx_sensitivity_dbm": -95, "rx_noise_figure_db": 7,
     "notes": "Joint Tactical Data Radio System, IP MANET mesh"},
    # ── Widely-Exported Global Tactical ─────────────────────────────────────
    {"id": "rs_m3ar", "category": "NATO/Export Tactical", "label": "R&S M3AR",
     "manufacturer": "Rohde & Schwarz", "frequency_hz": 150e6,
     "freq_min_hz": 30e6, "freq_max_hz": 512e6,
     "power_dbm": 37, "antenna_type": "dipole_half_wave",
     "antenna_polarization": "vertical", "antenna_gain_dbi": 2.15, "antenna_tilt_deg": 0,
     "height_m": 2, "rx_sensitivity_dbm": -113, "rx_noise_figure_db": 5,
     "notes": "NATO-standard multiband manpack, wideband IP, widely exported"},
    # ── IADS — Integrated Air Defense Systems ───────────────────────────────
    # Representative parameters; specific systems vary widely. Sweep arcs
    # capture typical scan/track behavior — surveillance radars sweep 360°,
    # acquisition radars sweep ~120°, tracking/fire-control radars are more
    # narrowly steered.
    {"id": "iads_ew_uhf", "category": "IADS", "label": "Early Warning Radar (UHF/L-band)",
     "manufacturer": "Generic", "frequency_hz": 425e6,
     "freq_min_hz": 200e6, "freq_max_hz": 1300e6,
     "power_dbm": 90, "antenna_type": "yagi_9el",
     "antenna_polarization": "horizontal", "antenna_gain_dbi": 30, "antenna_tilt_deg": 0,
     "height_m": 12, "rx_sensitivity_dbm": -120, "rx_noise_figure_db": 4,
     "polar_pattern": "yagi_9", "sweep_deg": 360,
     "notes": "Long-range surveillance (e.g. AN/FPS-117, P-18). 360° rotating, ~1 MW peak."},
    {"id": "iads_acq_sband", "category": "IADS", "label": "Target Acquisition Radar (S-band)",
     "manufacturer": "Generic", "frequency_hz": 3000e6,
     "freq_min_hz": 2000e6, "freq_max_hz": 4000e6,
     "power_dbm": 85, "antenna_type": "parabolic_dish",
     "antenna_polarization": "horizontal", "antenna_gain_dbi": 32, "antenna_tilt_deg": 0,
     "height_m": 8, "rx_sensitivity_dbm": -115, "rx_noise_figure_db": 4,
     "polar_pattern": "parabolic_medium", "sweep_deg": 360,
     "notes": "Medium-range acquisition (e.g. 36D6, AN/MPQ-50). 360° rotating S-band."},
    {"id": "iads_track_xband", "category": "IADS", "label": "Target Tracking Radar (X-band)",
     "manufacturer": "Generic", "frequency_hz": 9000e6,
     "freq_min_hz": 8000e6, "freq_max_hz": 12000e6,
     "power_dbm": 80, "antenna_type": "phased_array",
     "antenna_polarization": "vertical", "antenna_gain_dbi": 36, "antenna_tilt_deg": 0,
     "height_m": 6, "rx_sensitivity_dbm": -118, "rx_noise_figure_db": 3,
     "polar_pattern": "parabolic_medium", "sweep_deg": 30,
     "notes": "Narrow-beam track radar (e.g. AN/MPQ-65 pencil-beam). Steered ±15° for engagement."},
    {"id": "iads_fc_xband", "category": "IADS", "label": "Fire Control Radar (X-band)",
     "manufacturer": "Generic", "frequency_hz": 10000e6,
     "freq_min_hz": 8000e6, "freq_max_hz": 12000e6,
     "power_dbm": 78, "antenna_type": "parabolic_dish",
     "antenna_polarization": "vertical", "antenna_gain_dbi": 38, "antenna_tilt_deg": 0,
     "height_m": 5, "rx_sensitivity_dbm": -118, "rx_noise_figure_db": 3,
     "polar_pattern": "parabolic_medium", "sweep_deg": 0,
     "notes": "Engagement-quality FCR (e.g. AN/MPQ-53, 30N6E). Locked on target — no sweep."},
    {"id": "iads_gpsjam_ground", "category": "IADS", "label": "GPS Jammer (Ground)",
     "manufacturer": "Generic", "frequency_hz": 1575.42e6,
     "freq_min_hz": 1217e6, "freq_max_hz": 1610e6,
     "power_dbm": 50, "antenna_type": "omnidirectional",
     "antenna_polarization": "circular", "antenna_gain_dbi": 5, "antenna_tilt_deg": 0,
     "height_m": 5, "rx_sensitivity_dbm": -100, "rx_noise_figure_db": 6,
     "polar_pattern": "omni", "sweep_deg": 360,
     "notes": "L1/L2 GPS denial, 100W vehicular/fixed. Targets GNSS receivers in 5–50 km radius."},
    {"id": "iads_gpsjam_air", "category": "IADS", "label": "GPS Jammer (Airborne Pod)",
     "manufacturer": "Generic", "frequency_hz": 1575.42e6,
     "freq_min_hz": 1217e6, "freq_max_hz": 1610e6,
     "power_dbm": 43, "antenna_type": "patch",
     "antenna_polarization": "circular", "antenna_gain_dbi": 8, "antenna_tilt_deg": -45,
     "height_m": 3000, "rx_sensitivity_dbm": -100, "rx_noise_figure_db": 6,
     "polar_pattern": "cardioid", "sweep_deg": 0,
     "notes": "Podded airborne GPS denial (e.g. SPN-43-class). Forward-/down-looking, 20W effective."},
    # ── Satellite messengers / phones (handheld L-band terminals) ───────────
    {"id": "satpaq", "category": "Satellite Messenger", "label": "SATPAQ (smartphone satellite SMS)",
     "manufacturer": "Higher Ground LLC", "frequency_hz": 1621e6,
     "freq_min_hz": 1616e6, "freq_max_hz": 1626.5e6,
     "power_dbm": 30, "antenna_type": "patch",
     "antenna_polarization": "circular", "antenna_gain_dbi": 2.0, "antenna_tilt_deg": 0,
     "height_m": 1.5, "rx_sensitivity_dbm": -118, "rx_noise_figure_db": 3,
     "notes": "Clip-on satellite SMS for phones (L-band short-burst-data class). EIRP/antenna indicative — verify against the datasheet."},
    {"id": "iridium9575", "category": "Satellite Phone", "label": "Iridium 9575 Extreme",
     "manufacturer": "Iridium", "frequency_hz": 1621e6,
     "freq_min_hz": 1616e6, "freq_max_hz": 1626.5e6,
     "power_dbm": 32, "antenna_type": "whip_quarter_wave",
     "antenna_polarization": "circular", "antenna_gain_dbi": 1.0, "antenna_tilt_deg": 0,
     "height_m": 1.5, "rx_sensitivity_dbm": -118, "rx_noise_figure_db": 3,
     "notes": "Iridium LEO handset, L-band, retractable quarter-wave whip; ~1.6 W peak."},
    {"id": "isatphone2", "category": "Satellite Phone", "label": "Inmarsat IsatPhone 2",
     "manufacturer": "Inmarsat", "frequency_hz": 1643e6,
     "freq_min_hz": 1626.5e6, "freq_max_hz": 1660.5e6,
     "power_dbm": 33, "antenna_type": "patch",
     "antenna_polarization": "circular", "antenna_gain_dbi": 3.0, "antenna_tilt_deg": 0,
     "height_m": 1.5, "rx_sensitivity_dbm": -120, "rx_noise_figure_db": 2,
     "notes": "Inmarsat GEO handset; L-band uplink, flip-up patch (~2 W)."},
    {"id": "thuraya_xt", "category": "Satellite Phone", "label": "Thuraya XT-LITE",
     "manufacturer": "Thuraya", "frequency_hz": 1643e6,
     "freq_min_hz": 1626.5e6, "freq_max_hz": 1660.5e6,
     "power_dbm": 33, "antenna_type": "patch",
     "antenna_polarization": "circular", "antenna_gain_dbi": 3.0, "antenna_tilt_deg": 0,
     "height_m": 1.5, "rx_sensitivity_dbm": -118, "rx_noise_figure_db": 3,
     "notes": "Thuraya GEO handset (regional coverage, L-band uplink)."},
    {"id": "globalstar1700", "category": "Satellite Phone", "label": "Globalstar GSP-1700",
     "manufacturer": "Globalstar", "frequency_hz": 1618e6,
     "freq_min_hz": 1610e6, "freq_max_hz": 1626.5e6,
     "power_dbm": 30, "antenna_type": "whip_quarter_wave",
     "antenna_polarization": "circular", "antenna_gain_dbi": 1.0, "antenna_tilt_deg": 0,
     "height_m": 1.5, "rx_sensitivity_dbm": -116, "rx_noise_figure_db": 3,
     "notes": "Globalstar LEO handset, L-band user uplink."},
    # ── GNSS / PNT — the SATELLITE (space segment) modelled as the source ────
    # (GNSS receivers don't transmit; these model the downlink — use for link / coverage /
    #  RFI / jamming-vs-signal studies. Set the TX altitude to the MEO value in `altitude_m`.)
    {"id": "gnss_gps_l1", "category": "GNSS / PNT", "label": "GPS L1 C/A — space segment (GPS satellite)",
     "manufacturer": "USSF / Lockheed Martin (GPS III)", "frequency_hz": 1575.42e6,
     "freq_min_hz": 1575.42e6, "freq_max_hz": 1575.42e6,
     "power_dbm": 44, "antenna_type": "patch",
     "antenna_polarization": "circular", "antenna_gain_dbi": 13.0, "antenna_tilt_deg": 0,
     "height_m": 0, "altitude_m": 20200000, "rx_sensitivity_dbm": -157, "rx_noise_figure_db": 3,
     "notes": "A GPS satellite (MEO ≈20,200 km) as the transmitter — Earth-coverage shaped helix, ≈27 W (≈+44 dBm). Set the TX altitude to ≈20,200,000 m for the right geometry."},
    {"id": "gnss_gps_l5", "category": "GNSS / PNT", "label": "GPS L5 — space segment (safety-of-life)",
     "manufacturer": "USSF", "frequency_hz": 1176.45e6,
     "freq_min_hz": 1176.45e6, "freq_max_hz": 1176.45e6,
     "power_dbm": 45, "antenna_type": "patch",
     "antenna_polarization": "circular", "antenna_gain_dbi": 13.0, "antenna_tilt_deg": 0,
     "height_m": 0, "altitude_m": 20200000, "rx_sensitivity_dbm": -156, "rx_noise_figure_db": 3,
     "notes": "GPS L5 downlink — higher power than L1. MEO satellite as the source (altitude ≈20,200 km)."},
    {"id": "gnss_glonass_l1", "category": "GNSS / PNT", "label": "GLONASS L1OF — space segment",
     "manufacturer": "Roscosmos", "frequency_hz": 1602e6,
     "freq_min_hz": 1598e6, "freq_max_hz": 1606e6,
     "power_dbm": 44, "antenna_type": "patch",
     "antenna_polarization": "circular", "antenna_gain_dbi": 13.0, "antenna_tilt_deg": 0,
     "height_m": 0, "altitude_m": 19100000, "rx_sensitivity_dbm": -157, "rx_noise_figure_db": 3,
     "notes": "GLONASS FDMA L1 (band centre shown; per-satellite channels offset). MEO ≈19,100 km."},
    {"id": "gnss_galileo_e1", "category": "GNSS / PNT", "label": "Galileo E1 — space segment",
     "manufacturer": "ESA / EUSPA", "frequency_hz": 1575.42e6,
     "freq_min_hz": 1575.42e6, "freq_max_hz": 1575.42e6,
     "power_dbm": 44, "antenna_type": "patch",
     "antenna_polarization": "circular", "antenna_gain_dbi": 13.0, "antenna_tilt_deg": 0,
     "height_m": 0, "altitude_m": 23222000, "rx_sensitivity_dbm": -157, "rx_noise_figure_db": 3,
     "notes": "Galileo E1 (shares the GPS L1 centre frequency). MEO ≈23,222 km."},
    {"id": "gnss_repeater_l1", "category": "GNSS / PNT", "label": "GPS L1 re-radiator / repeater (indoor/test)",
     "manufacturer": "(generic)", "frequency_hz": 1575.42e6,
     "freq_min_hz": 1575.42e6, "freq_max_hz": 1575.42e6,
     "power_dbm": -60, "antenna_type": "patch",
     "antenna_polarization": "circular", "antenna_gain_dbi": 3.0, "antenna_tilt_deg": 0,
     "height_m": 3, "rx_sensitivity_dbm": -157, "rx_noise_figure_db": 3,
     "notes": "Ground GPS L1 repeater — re-broadcasts the live sky signal indoors / in a hangar. Very low EIRP by design and regulation; included for leakage/coverage modelling."},
    # ── UAS / drone datalinks (airborne — set the TX height to the flight altitude) ──
    {"id": "fpv_58ghz_analog", "category": "UAS / Drone", "label": "Analog FPV video — 5.8 GHz (raceband)",
     "manufacturer": "(generic)", "frequency_hz": 5740e6,
     "freq_min_hz": 5645e6, "freq_max_hz": 5945e6,
     "power_dbm": 27, "antenna_type": "dipole_half_wave",
     "antenna_polarization": "circular", "antenna_gain_dbi": 2.0, "antenna_tilt_deg": 0,
     "height_m": 100, "rx_sensitivity_dbm": -90, "rx_noise_figure_db": 8,
     "notes": "Wideband-FM analog video downlink; 25 mW–1 W typical (≈600 mW shown), cloverleaf/dipole. Airborne — TX height = flight altitude (≈100 m AGL default)."},
    {"id": "dji_fpv_o3", "category": "UAS / Drone", "label": "DJI O3 / O4 Air Unit (digital FPV)",
     "manufacturer": "DJI", "frequency_hz": 5800e6,
     "freq_min_hz": 5725e6, "freq_max_hz": 5850e6,
     "power_dbm": 30, "antenna_type": "patch",
     "antenna_polarization": "vertical", "antenna_gain_dbi": 2.0, "antenna_tilt_deg": 0,
     "height_m": 120, "rx_sensitivity_dbm": -92, "rx_noise_figure_db": 7,
     "notes": "DJI OcuSync digital FPV downlink (also runs at 2.4 GHz). ≈1 W EIRP (FCC) / ≈25 mW (CE). AES-encrypted video. Airborne (≈120 m AGL)."},
    {"id": "isr_cband_los", "category": "UAS / Drone", "label": "ISR LOS datalink — C-band (MQ-class)",
     "manufacturer": "(generic)", "frequency_hz": 4700e6,
     "freq_min_hz": 4400e6, "freq_max_hz": 5000e6,
     "power_dbm": 37, "antenna_type": "patch",
     "antenna_polarization": "vertical", "antenna_gain_dbi": 10.0, "antenna_tilt_deg": 0,
     "height_m": 5000, "rx_sensitivity_dbm": -95, "rx_noise_figure_db": 5,
     "notes": "Medium-altitude ISR line-of-sight downlink (C-band, MPEG-TS / CDL-class), steered antenna ≈10 dBi. The SATCOM relay path is separate (Ku ≈14–15 GHz). Airborne (≈5 km)."},
    {"id": "remote_id_beacon", "category": "UAS / Drone", "label": "Drone Remote ID beacon (ASTM F3411)",
     "manufacturer": "(generic)", "frequency_hz": 2437e6,
     "freq_min_hz": 2400e6, "freq_max_hz": 2483.5e6,
     "power_dbm": 20, "antenna_type": "dipole_half_wave",
     "antenna_polarization": "vertical", "antenna_gain_dbi": 2.0, "antenna_tilt_deg": 0,
     "height_m": 120, "rx_sensitivity_dbm": -92, "rx_noise_figure_db": 7,
     "notes": "FAA / ASTM F3411 Remote ID broadcast (WiFi NAN / beacon, ≈100 mW). Models the detection range of the unencrypted Remote ID telemetry. Airborne (≈120 m AGL)."},
    # ── Blue Force Tracking (L-band SATCOM position-reporting transceiver) ──
    {"id": "riverjack_bft", "category": "Blue Force Tracking", "label": "RiverJack BFT (Blue Force Tracking transceiver)",
     "manufacturer": "BFT-2 program", "frequency_hz": 1645e6,
     "freq_min_hz": 1626.5e6, "freq_max_hz": 1660.5e6,
     "power_dbm": 36, "antenna_type": "patch",
     "antenna_polarization": "circular", "antenna_gain_dbi": 4.0, "antenna_tilt_deg": 0,
     "height_m": 2.5, "rx_sensitivity_dbm": -118, "rx_noise_figure_db": 3,
     "notes": "Blue Force Tracking — BFT-2-class L-band MSS position-reporting transceiver, vehicle-mounted (low-duty-cycle position bursts via a GEO/MEO bird, ~3-5 W EIRP into a small patch/dome). RF parameters here are indicative of the BFT-2 class — verify against the specific unit's spec; BFT waveform/system details are often controlled / not publicly published."},
]


# ─────────────────────────────────────────────────────────────────────────────
# Antenna presets — manufacturer datasheet values
# ─────────────────────────────────────────────────────────────────────────────
#
# Each preset captures the parameters the simulator actually consumes:
#   - peak_gain_dbi: typical mid-band gain from the datasheet
#   - polar_pattern: closest entry in polar_patterns.POLAR_PATTERNS
#   - polarization:  "linear" | "vertical" | "horizontal" | "circular"
#   - antenna_type:  closest AntennaType for elevation-pattern shape
#   - freq_min_hz / freq_max_hz: operational range
#   - notes: brief datasheet summary (peak / VSWR / construction)
#
# Single-value gain is a simplification — broadband antennas (bilogs,
# DRGHs) have gain that varies several dB across the band.  The notes
# call out the curve shape so operators can override `polar_peak_gain_dbi`
# at the working frequency.

_ANTENNA_PRESETS = [
    # ── Electro-Metrics — full EM Antennas catalogue ───────────────────
    # Sourced from em-antennas.com product sitemap (2026-05).  Peak gains
    # are nominal mid-band values consistent with each antenna class —
    # broadband models (LPDA, biconical, UWB omni) vary several dB across
    # frequency, so override `polar_peak_gain_dbi` for in-band precision.

    # ── Active monopoles & vertical rods ───────────────────────────────
    {"id": "em6892", "manufacturer": "Electro-Metrics", "model": "EM-6892",
     "label": "EM-6892 Active Vertical (1 kHz – 50 MHz)", "category": "Active Monopole",
     "freq_min_hz": 1e3, "freq_max_hz": 50e6,
     "peak_gain_dbi": 0.0, "polar_pattern": "omni",
     "polarization": "vertical", "antenna_type": "whip_quarter_wave",
     "notes": "Active 1 m rod for HF/LF EMC; calibrated antenna factor."},
    {"id": "em6899", "manufacturer": "Electro-Metrics", "model": "EM-6899",
     "label": "EM-6899 Passive Vertical (1 kHz – 50 MHz)", "category": "Passive Monopole",
     "freq_min_hz": 1e3, "freq_max_hz": 50e6,
     "peak_gain_dbi": 0.0, "polar_pattern": "omni",
     "polarization": "vertical", "antenna_type": "whip_quarter_wave",
     "notes": "Passive 1 m rod, no preamp."},
    {"id": "em6905", "manufacturer": "Electro-Metrics", "model": "EM-6905",
     "label": "EM-6905 Active Vertical (1 kHz – 200 MHz)", "category": "Active Monopole",
     "freq_min_hz": 1e3, "freq_max_hz": 200e6,
     "peak_gain_dbi": 0.0, "polar_pattern": "omni",
     "polarization": "vertical", "antenna_type": "whip_quarter_wave",
     "notes": "Extended-range active rod, useful through low VHF."},

    # ── Whips, blades, longwire, tunable dipole ────────────────────────
    {"id": "em6500", "manufacturer": "Electro-Metrics", "model": "EM-6500",
     "label": "EM-6500 Whip", "category": "Whip",
     "freq_min_hz": 25e6, "freq_max_hz": 1e9,
     "peak_gain_dbi": 2.0, "polar_pattern": "omni",
     "polarization": "vertical", "antenna_type": "whip_quarter_wave",
     "notes": "Tactical whip antenna."},
    {"id": "em6501", "manufacturer": "Electro-Metrics", "model": "EM-6501",
     "label": "EM-6501 Wideband Blade Monopole (2 – 520 MHz)", "category": "Blade",
     "freq_min_hz": 2e6, "freq_max_hz": 520e6,
     "peak_gain_dbi": 2.0, "polar_pattern": "omni",
     "polarization": "vertical", "antenna_type": "whip_quarter_wave",
     "notes": "Low-profile wideband blade monopole, vehicle / airborne."},
    {"id": "em6898", "manufacturer": "Electro-Metrics", "model": "EM-6898",
     "label": "EM-6898 Vertical Whip", "category": "Whip",
     "freq_min_hz": 25e6, "freq_max_hz": 1e9,
     "peak_gain_dbi": 2.0, "polar_pattern": "omni",
     "polarization": "vertical", "antenna_type": "whip_quarter_wave",
     "notes": "Vertical whip with N connector (straight or right-angle variants)."},
    {"id": "em6903", "manufacturer": "Electro-Metrics", "model": "EM-6903",
     "label": "EM-6903 Vertical Whip (right-angle)", "category": "Whip",
     "freq_min_hz": 25e6, "freq_max_hz": 1e9,
     "peak_gain_dbi": 2.0, "polar_pattern": "omni",
     "polarization": "vertical", "antenna_type": "whip_quarter_wave",
     "notes": "Vertical whip, right-angle N connector."},
    {"id": "em6904_1", "manufacturer": "Electro-Metrics", "model": "EM-6904-1",
     "label": "EM-6904-1 Whip Omnidirectional", "category": "Whip",
     "freq_min_hz": 25e6, "freq_max_hz": 1e9,
     "peak_gain_dbi": 2.0, "polar_pattern": "omni",
     "polarization": "vertical", "antenna_type": "whip_quarter_wave",
     "notes": "General-purpose omnidirectional whip."},
    {"id": "em6844", "manufacturer": "Electro-Metrics", "model": "EM-6844",
     "label": "EM-6844 Long Wire", "category": "Long Wire",
     "freq_min_hz": 1e6, "freq_max_hz": 30e6,
     "peak_gain_dbi": 0.0, "polar_pattern": "omni",
     "polarization": "horizontal", "antenna_type": "dipole_half_wave",
     "notes": "HF long-wire receive antenna."},
    {"id": "em6924", "manufacturer": "Electro-Metrics", "model": "EM-6924",
     "label": "EM-6924 Tunable Dipole Set (28 MHz – 1 GHz)", "category": "Dipole",
     "freq_min_hz": 28e6, "freq_max_hz": 1e9,
     "peak_gain_dbi": 2.15, "polar_pattern": "omni",
     "polarization": "linear", "antenna_type": "dipole_half_wave",
     "notes": "Adjustable half-wave dipole set for calibration use."},

    # ── Discones (broadband omni) ──────────────────────────────────────
    {"id": "em6105", "manufacturer": "Electro-Metrics", "model": "EM-6105",
     "label": "EM-6105 Discone (10 kHz – 2 GHz)", "category": "Discone",
     "freq_min_hz": 10e3, "freq_max_hz": 2e9,
     "peak_gain_dbi": 0.0, "polar_pattern": "omni",
     "polarization": "vertical", "antenna_type": "ground_plane",
     "notes": "Wideband discone, vertical, omnidirectional."},
    {"id": "em6105_1", "manufacturer": "Electro-Metrics", "model": "EM-6105-1",
     "label": "EM-6105-1 Discone (30 MHz – 3 GHz)", "category": "Discone",
     "freq_min_hz": 30e6, "freq_max_hz": 3e9,
     "peak_gain_dbi": 0.0, "polar_pattern": "omni",
     "polarization": "vertical", "antenna_type": "ground_plane",
     "notes": "Higher-band discone variant."},
    {"id": "em6115", "manufacturer": "Electro-Metrics", "model": "EM-6115",
     "label": "EM-6115 Wideband Discone (10 kHz – 2 GHz)", "category": "Discone",
     "freq_min_hz": 10e3, "freq_max_hz": 2e9,
     "peak_gain_dbi": 0.0, "polar_pattern": "omni",
     "polarization": "vertical", "antenna_type": "ground_plane",
     "notes": "Decade-wide discone for VHF/UHF monitoring."},

    # ── Biconicals (omni in azimuth, dipole-like elevation) ────────────
    {"id": "em6912a", "manufacturer": "Electro-Metrics", "model": "EM-6912A",
     "label": "EM-6912A Biconical (30 – 300 MHz)", "category": "Biconical",
     "freq_min_hz": 30e6, "freq_max_hz": 300e6,
     "peak_gain_dbi": 1.0, "polar_pattern": "omni",
     "polarization": "vertical", "antenna_type": "dipole_full_wave",
     "notes": "Classic CISPR biconical, ~130 cm tip-to-tip, VSWR avg 1.4:1."},
    {"id": "em6913", "manufacturer": "Electro-Metrics", "model": "EM-6913",
     "label": "EM-6913 High-Field Biconical (20 – 300 MHz)", "category": "Biconical",
     "freq_min_hz": 20e6, "freq_max_hz": 300e6,
     "peak_gain_dbi": 2.0, "polar_pattern": "omni",
     "polarization": "linear", "antenna_type": "dipole_full_wave",
     "notes": "High-power biconical for radiated immunity testing."},
    {"id": "em6917b_1", "manufacturer": "Electro-Metrics", "model": "EM-6917B-1",
     "label": "EM-6917B-1 Biconical (30 MHz – 3 GHz)", "category": "Biconical",
     "freq_min_hz": 30e6, "freq_max_hz": 3e9,
     "peak_gain_dbi": 2.0, "polar_pattern": "omni",
     "polarization": "linear", "antenna_type": "dipole_full_wave",
     "notes": "Wideband biconical, extended UHF coverage."},
    {"id": "em6917b_2", "manufacturer": "Electro-Metrics", "model": "EM-6917B-2",
     "label": "EM-6917B-2 Biconical (26 MHz – 3 GHz)", "category": "Biconical",
     "freq_min_hz": 26e6, "freq_max_hz": 3e9,
     "peak_gain_dbi": 2.0, "polar_pattern": "omni",
     "polarization": "linear", "antenna_type": "dipole_full_wave",
     "notes": "Low-end-extended wideband biconical."},

    # ── Biconilog (combined biconical + LP) ────────────────────────────
    {"id": "em6917c", "manufacturer": "Electro-Metrics", "model": "EM-6917C",
     "label": "EM-6917C Biconilog (26 MHz – 1 GHz)", "category": "Bicone-LP Hybrid",
     "freq_min_hz": 26e6, "freq_max_hz": 1e9,
     "peak_gain_dbi": 5.0, "polar_pattern": "log_periodic",
     "polarization": "linear", "antenna_type": "log_periodic",
     "notes": "Single-antenna full EMC sweep, biconical below ~200 MHz transitioning to LP."},

    # ── Log-periodic dipole arrays ─────────────────────────────────────
    {"id": "em6936", "manufacturer": "Electro-Metrics", "model": "EM-6936",
     "label": "EM-6936 LPDA (700 – 1300 MHz)", "category": "Log-Periodic",
     "freq_min_hz": 700e6, "freq_max_hz": 1300e6,
     "peak_gain_dbi": 7.0, "polar_pattern": "log_periodic",
     "polarization": "linear", "antenna_type": "log_periodic",
     "notes": "Cellular-band LPDA."},
    {"id": "em6937", "manufacturer": "Electro-Metrics", "model": "EM-6937",
     "label": "EM-6937 LPDA (1 – 18 GHz)", "category": "Log-Periodic",
     "freq_min_hz": 1.1e9, "freq_max_hz": 18e9,
     "peak_gain_dbi": 8.0, "polar_pattern": "log_periodic",
     "polarization": "linear", "antenna_type": "log_periodic",
     "notes": "Wide-decade LPDA covering microwave EMC test."},
    {"id": "em6939", "manufacturer": "Electro-Metrics", "model": "EM-6939",
     "label": "EM-6939 LPDA (800 MHz – 20 GHz)", "category": "Log-Periodic",
     "freq_min_hz": 800e6, "freq_max_hz": 20e9,
     "peak_gain_dbi": 8.0, "polar_pattern": "log_periodic",
     "polarization": "linear", "antenna_type": "log_periodic",
     "notes": "Long-range LPDA, broadband microwave coverage."},
    {"id": "em6942", "manufacturer": "Electro-Metrics", "model": "EM-6942",
     "label": "EM-6942 Tactical LPDA (400 MHz – 4 GHz)", "category": "Log-Periodic",
     "freq_min_hz": 400e6, "freq_max_hz": 4e9,
     "peak_gain_dbi": 7.0, "polar_pattern": "log_periodic",
     "polarization": "linear", "antenna_type": "log_periodic",
     "notes": "Field-deployable tactical LPDA."},
    {"id": "em6944", "manufacturer": "Electro-Metrics", "model": "EM-6944",
     "label": "EM-6944 LPDA (200 MHz – 3 GHz)", "category": "Log-Periodic",
     "freq_min_hz": 200e6, "freq_max_hz": 3e9,
     "peak_gain_dbi": 7.0, "polar_pattern": "log_periodic",
     "polarization": "linear", "antenna_type": "log_periodic",
     "notes": "Decade LPDA for general EMC use."},
    {"id": "em6946", "manufacturer": "Electro-Metrics", "model": "EM-6946",
     "label": "EM-6946 LPDA (300 MHz – 3 GHz)", "category": "Log-Periodic",
     "freq_min_hz": 300e6, "freq_max_hz": 3e9,
     "peak_gain_dbi": 7.0, "polar_pattern": "log_periodic",
     "polarization": "linear", "antenna_type": "log_periodic",
     "notes": "Compact LPDA, popular for chamber test."},
    {"id": "em6947", "manufacturer": "Electro-Metrics", "model": "EM-6947",
     "label": "EM-6947 LPDA (200 MHz – 5 GHz)", "category": "Log-Periodic",
     "freq_min_hz": 200e6, "freq_max_hz": 5e9,
     "peak_gain_dbi": 6.0, "polar_pattern": "log_periodic",
     "polarization": "linear", "antenna_type": "log_periodic",
     "notes": "6 dBi typical (datasheet)."},
    {"id": "em6950", "manufacturer": "Electro-Metrics", "model": "EM-6950",
     "label": "EM-6950 LPDA (200 MHz – 1 GHz)", "category": "Log-Periodic",
     "freq_min_hz": 200e6, "freq_max_hz": 1e9,
     "peak_gain_dbi": 7.0, "polar_pattern": "log_periodic",
     "polarization": "linear", "antenna_type": "log_periodic",
     "notes": "VHF/UHF LPDA."},
    {"id": "em6951", "manufacturer": "Electro-Metrics", "model": "EM-6951",
     "label": "EM-6951 LPDA (300 MHz – 1 GHz)", "category": "Log-Periodic",
     "freq_min_hz": 300e6, "freq_max_hz": 1e9,
     "peak_gain_dbi": 7.0, "polar_pattern": "log_periodic",
     "polarization": "linear", "antenna_type": "log_periodic",
     "notes": "Compact UHF LPDA."},
    {"id": "em6952", "manufacturer": "Electro-Metrics", "model": "EM-6952",
     "label": "EM-6952 LPDA (1 – 18 GHz)", "category": "Log-Periodic",
     "freq_min_hz": 1e9, "freq_max_hz": 18e9,
     "peak_gain_dbi": 8.0, "polar_pattern": "log_periodic",
     "polarization": "linear", "antenna_type": "log_periodic",
     "notes": "Microwave LPDA."},
    {"id": "em6952_1", "manufacturer": "Electro-Metrics", "model": "EM-6952-1",
     "label": "EM-6952-1 LPDA (1.1 – 2 GHz)", "category": "Log-Periodic",
     "freq_min_hz": 1.1e9, "freq_max_hz": 2e9,
     "peak_gain_dbi": 7.0, "polar_pattern": "log_periodic",
     "polarization": "linear", "antenna_type": "log_periodic",
     "notes": "Mid-band variant of EM-6952 family."},
    {"id": "em6954", "manufacturer": "Electro-Metrics", "model": "EM-6954",
     "label": "EM-6954 LPDA (70 MHz – 1 GHz)", "category": "Log-Periodic",
     "freq_min_hz": 70e6, "freq_max_hz": 1e9,
     "peak_gain_dbi": 7.0, "polar_pattern": "log_periodic",
     "polarization": "linear", "antenna_type": "log_periodic",
     "notes": "Low-VHF–UHF LPDA, large structure."},
    {"id": "em6956", "manufacturer": "Electro-Metrics", "model": "EM-6956",
     "label": "EM-6956 LPDA (500 MHz – 3 GHz)", "category": "Log-Periodic",
     "freq_min_hz": 500e6, "freq_max_hz": 3e9,
     "peak_gain_dbi": 7.0, "polar_pattern": "log_periodic",
     "polarization": "linear", "antenna_type": "log_periodic",
     "notes": "Compact LPDA for handheld DF."},
    {"id": "em6956_1", "manufacturer": "Electro-Metrics", "model": "EM-6956-1",
     "label": "EM-6956-1 LPDA (1 – 2.5 GHz)", "category": "Log-Periodic",
     "freq_min_hz": 1e9, "freq_max_hz": 2.5e9,
     "peak_gain_dbi": 7.0, "polar_pattern": "log_periodic",
     "polarization": "linear", "antenna_type": "log_periodic",
     "notes": "Mid-band variant of EM-6956 family."},

    # ── Horns (octave band, double-ridged guide, pyramidal, std gain) ──
    {"id": "em6119", "manufacturer": "Electro-Metrics", "model": "EM-6119",
     "label": "EM-6119 Pyramidal Horn (1.7 – 1.9 GHz)", "category": "Horn",
     "freq_min_hz": 1.7e9, "freq_max_hz": 1.9e9,
     "peak_gain_dbi": 14.0, "polar_pattern": "horn",
     "polarization": "linear", "antenna_type": "horn",
     "notes": "Narrowband pyramidal horn, GPS-band gain reference."},
    {"id": "em6960", "manufacturer": "Electro-Metrics", "model": "EM-6960",
     "label": "EM-6960 Ridged Guide (200 MHz – 2 GHz)", "category": "Horn",
     "freq_min_hz": 200e6, "freq_max_hz": 2e9,
     "peak_gain_dbi": 9.0, "polar_pattern": "horn",
     "polarization": "linear", "antenna_type": "horn",
     "notes": "Decade-wide ridged-guide horn, ~6→12 dBi across band."},
    {"id": "em6963", "manufacturer": "Electro-Metrics", "model": "EM-6963",
     "label": "EM-6963 Dual-Ridge Horn (18 – 40 GHz)", "category": "Horn",
     "freq_min_hz": 18e9, "freq_max_hz": 40e9,
     "peak_gain_dbi": 12.0, "polar_pattern": "horn",
     "polarization": "linear", "antenna_type": "horn",
     "notes": "mmWave DRGH for 5G NR / radar / SATCOM testing."},
    {"id": "em6968", "manufacturer": "Electro-Metrics", "model": "EM-6968",
     "label": "EM-6968 Double-Ridged Guide (2.5 – 7.5 GHz)", "category": "Horn",
     "freq_min_hz": 2.5e9, "freq_max_hz": 7.5e9,
     "peak_gain_dbi": 12.0, "polar_pattern": "horn",
     "polarization": "linear", "antenna_type": "horn",
     "notes": "C-band DRGH."},
    {"id": "em6969", "manufacturer": "Electro-Metrics", "model": "EM-6969",
     "label": "EM-6969 Ridged Guide (6 – 18 GHz)", "category": "Horn",
     "freq_min_hz": 6e9, "freq_max_hz": 18e9,
     "peak_gain_dbi": 12.0, "polar_pattern": "horn",
     "polarization": "linear", "antenna_type": "horn",
     "notes": "X / Ku-band ridged-guide horn."},
    {"id": "em7020", "manufacturer": "Electro-Metrics", "model": "EM-7020",
     "label": "EM-7020 Octave-Band Horn (1 – 2.5 GHz)", "category": "Horn",
     "freq_min_hz": 1e9, "freq_max_hz": 2.5e9,
     "peak_gain_dbi": 12.0, "polar_pattern": "horn",
     "polarization": "linear", "antenna_type": "horn",
     "notes": "Octave-band horn, low-band of the EM-7020 family."},
    {"id": "em7021", "manufacturer": "Electro-Metrics", "model": "EM-7021",
     "label": "EM-7021 Octave-Band Horn (2 – 5 GHz)", "category": "Horn",
     "freq_min_hz": 2e9, "freq_max_hz": 5e9,
     "peak_gain_dbi": 12.0, "polar_pattern": "horn",
     "polarization": "linear", "antenna_type": "horn",
     "notes": "Octave-band horn, mid-band."},
    {"id": "em7022", "manufacturer": "Electro-Metrics", "model": "EM-7022",
     "label": "EM-7022 Octave-Band Horn (4 – 10 GHz)", "category": "Horn",
     "freq_min_hz": 4e9, "freq_max_hz": 10e9,
     "peak_gain_dbi": 13.0, "polar_pattern": "horn",
     "polarization": "linear", "antenna_type": "horn",
     "notes": "Octave-band horn, high-band."},
    {"id": "em7102", "manufacturer": "Electro-Metrics", "model": "EM-7102",
     "label": "EM-7102 Dual-Ridged Horn (1 – 18 GHz)", "category": "Horn",
     "freq_min_hz": 1e9, "freq_max_hz": 18e9,
     "peak_gain_dbi": 13.0, "polar_pattern": "horn",
     "polarization": "linear", "antenna_type": "horn",
     "notes": "Workhorse DRGH, ~12-15 dBi above 3 GHz; 6 dBi at 1 GHz; 80°→20° HPBW."},
    {"id": "em7101", "manufacturer": "Electro-Metrics", "model": "EM-7101",
     "label": "EM-7101 Standard Gain Horn Kit (18 – 40 GHz)", "category": "Horn",
     "freq_min_hz": 18e9, "freq_max_hz": 40e9,
     "peak_gain_dbi": 20.0, "polar_pattern": "horn",
     "polarization": "linear", "antenna_type": "horn",
     "notes": "Calibrated mmWave standard-gain horn set."},
    {"id": "em7103", "manufacturer": "Electro-Metrics", "model": "EM-7103",
     "label": "EM-7103 Standard Gain Horn (18 – 26.5 GHz)", "category": "Horn",
     "freq_min_hz": 18e9, "freq_max_hz": 26.5e9,
     "peak_gain_dbi": 20.0, "polar_pattern": "horn",
     "polarization": "linear", "antenna_type": "horn",
     "notes": "K-band SGH."},
    {"id": "em7104", "manufacturer": "Electro-Metrics", "model": "EM-7104",
     "label": "EM-7104 Standard Gain Horn (26.5 – 40 GHz)", "category": "Horn",
     "freq_min_hz": 26.5e9, "freq_max_hz": 40e9,
     "peak_gain_dbi": 22.0, "polar_pattern": "horn",
     "polarization": "linear", "antenna_type": "horn",
     "notes": "Ka-band SGH."},

    # ── Loops & magnetic-field antennas ────────────────────────────────
    {"id": "em6869", "manufacturer": "Electro-Metrics", "model": "EM-6869",
     "label": "EM-6869 Active Loop (9 kHz – 50 MHz)", "category": "Active Loop",
     "freq_min_hz": 9e3, "freq_max_hz": 50e6,
     "peak_gain_dbi": 0.0, "polar_pattern": "figure_8",
     "polarization": "horizontal", "antenna_type": "loop",
     "notes": "Shielded magnetic-field loop with built-in preamp."},
    {"id": "em6870", "manufacturer": "Electro-Metrics", "model": "EM-6870",
     "label": "EM-6870 Loop (20 Hz – 100 kHz)", "category": "Loop",
     "freq_min_hz": 20.0, "freq_max_hz": 100e3,
     "peak_gain_dbi": 0.0, "polar_pattern": "figure_8",
     "polarization": "horizontal", "antenna_type": "loop",
     "notes": "VLF/ELF loop for low-frequency H-field measurements."},
    {"id": "em6871", "manufacturer": "Electro-Metrics", "model": "EM-6871",
     "label": "EM-6871 Loop (30 Hz – 1 MHz)", "category": "Loop",
     "freq_min_hz": 30.0, "freq_max_hz": 1e6,
     "peak_gain_dbi": 0.0, "polar_pattern": "figure_8",
     "polarization": "horizontal", "antenna_type": "loop",
     "notes": "ELF/VLF/LF receive loop."},
    {"id": "em6872", "manufacturer": "Electro-Metrics", "model": "EM-6872",
     "label": "EM-6872 Loop (100 kHz – 30 MHz)", "category": "Loop",
     "freq_min_hz": 100e3, "freq_max_hz": 30e6,
     "peak_gain_dbi": 0.0, "polar_pattern": "figure_8",
     "polarization": "horizontal", "antenna_type": "loop",
     "notes": "MF/HF loop antenna."},
    {"id": "em6873", "manufacturer": "Electro-Metrics", "model": "EM-6873",
     "label": "EM-6873 Loop Sensor (20 Hz – 100 kHz)", "category": "Loop",
     "freq_min_hz": 20.0, "freq_max_hz": 100e3,
     "peak_gain_dbi": 0.0, "polar_pattern": "figure_8",
     "polarization": "horizontal", "antenna_type": "loop",
     "notes": "Low-frequency H-field sensor."},
    {"id": "em6874", "manufacturer": "Electro-Metrics", "model": "EM-6874",
     "label": "EM-6874 Loop (20 Hz – 30 MHz)", "category": "Loop",
     "freq_min_hz": 20.0, "freq_max_hz": 30e6,
     "peak_gain_dbi": 0.0, "polar_pattern": "figure_8",
     "polarization": "horizontal", "antenna_type": "loop",
     "notes": "Wideband loop, ELF–HF coverage."},
    {"id": "em6876", "manufacturer": "Electro-Metrics", "model": "EM-6876",
     "label": "EM-6876 Active Loop (9 kHz – 30 MHz)", "category": "Active Loop",
     "freq_min_hz": 9e3, "freq_max_hz": 30e6,
     "peak_gain_dbi": 0.0, "polar_pattern": "figure_8",
     "polarization": "horizontal", "antenna_type": "loop",
     "notes": "Active HF loop with preamp."},
    {"id": "em6877", "manufacturer": "Electro-Metrics", "model": "EM-6877",
     "label": "EM-6877 Loop (9 kHz – 30 MHz)", "category": "Loop",
     "freq_min_hz": 9e3, "freq_max_hz": 30e6,
     "peak_gain_dbi": 0.0, "polar_pattern": "figure_8",
     "polarization": "horizontal", "antenna_type": "loop",
     "notes": "Passive HF loop."},
    {"id": "em6878", "manufacturer": "Electro-Metrics", "model": "EM-6878",
     "label": "EM-6878 Loop (9 kHz – 30 MHz)", "category": "Loop",
     "freq_min_hz": 9e3, "freq_max_hz": 30e6,
     "peak_gain_dbi": 0.0, "polar_pattern": "figure_8",
     "polarization": "horizontal", "antenna_type": "loop",
     "notes": "HF loop variant."},
    {"id": "em6879", "manufacturer": "Electro-Metrics", "model": "EM-6879",
     "label": "EM-6879 Loop (10 kHz – 30 MHz)", "category": "Loop",
     "freq_min_hz": 10e3, "freq_max_hz": 30e6,
     "peak_gain_dbi": 0.0, "polar_pattern": "figure_8",
     "polarization": "horizontal", "antenna_type": "loop",
     "notes": "HF receive loop."},
    {"id": "em6902", "manufacturer": "Electro-Metrics", "model": "EM-6902",
     "label": "EM-6902 Magnetic Field", "category": "Magnetic Field",
     "freq_min_hz": 30e3, "freq_max_hz": 50e6,
     "peak_gain_dbi": 0.0, "polar_pattern": "figure_8",
     "polarization": "horizontal", "antenna_type": "loop",
     "notes": "H-field reference antenna."},
    {"id": "em6906", "manufacturer": "Electro-Metrics", "model": "EM-6906",
     "label": "EM-6906 Magnetic Field", "category": "Magnetic Field",
     "freq_min_hz": 30e3, "freq_max_hz": 200e6,
     "peak_gain_dbi": 0.0, "polar_pattern": "figure_8",
     "polarization": "horizontal", "antenna_type": "loop",
     "notes": "Wideband H-field antenna."},

    # ── Wideband / ultra-wideband omni-directional ─────────────────────
    {"id": "em6104_1", "manufacturer": "Electro-Metrics", "model": "EM-6104-1",
     "label": "EM-6104-1 Omni (20 MHz – 18 GHz)", "category": "Wideband Omni",
     "freq_min_hz": 20e6, "freq_max_hz": 18e9,
     "peak_gain_dbi": 2.0, "polar_pattern": "omni",
     "polarization": "vertical", "antenna_type": "omnidirectional",
     "notes": "Ultra-wideband vertical omni."},
    {"id": "em6116", "manufacturer": "Electro-Metrics", "model": "EM-6116",
     "label": "EM-6116 Omni (2 – 10 GHz)", "category": "Wideband Omni",
     "freq_min_hz": 2e9, "freq_max_hz": 10e9,
     "peak_gain_dbi": 3.0, "polar_pattern": "omni",
     "polarization": "vertical", "antenna_type": "omnidirectional",
     "notes": "C/X-band omnidirectional, ~3 dBi."},
    {"id": "em6725", "manufacturer": "Electro-Metrics", "model": "EM-6725",
     "label": "EM-6725 Omni Passive (600 MHz – 18 GHz)", "category": "Wideband Omni",
     "freq_min_hz": 600e6, "freq_max_hz": 18e9,
     "peak_gain_dbi": 2.0, "polar_pattern": "omni",
     "polarization": "vertical", "antenna_type": "omnidirectional",
     "notes": "Passive UHF–microwave omni."},
    {"id": "em6841", "manufacturer": "Electro-Metrics", "model": "EM-6841",
     "label": "EM-6841 Rugged UWB Omni (20 MHz – 43.5 GHz)", "category": "UWB Omni",
     "freq_min_hz": 20e6, "freq_max_hz": 43.5e9,
     "peak_gain_dbi": 2.0, "polar_pattern": "omni",
     "polarization": "vertical", "antenna_type": "omnidirectional",
     "notes": "Ruggedised passive UWB vertical omni."},
    {"id": "em6842", "manufacturer": "Electro-Metrics", "model": "EM-6842",
     "label": "EM-6842 Omni-directional", "category": "Wideband Omni",
     "freq_min_hz": 30e6, "freq_max_hz": 6e9,
     "peak_gain_dbi": 2.0, "polar_pattern": "omni",
     "polarization": "vertical", "antenna_type": "omnidirectional",
     "notes": "General-purpose vertical omni."},
    {"id": "em6843", "manufacturer": "Electro-Metrics", "model": "EM-6843",
     "label": "EM-6843 Rugged UWB Omni (300 MHz – 43.5 GHz)", "category": "UWB Omni",
     "freq_min_hz": 300e6, "freq_max_hz": 43.5e9,
     "peak_gain_dbi": 2.0, "polar_pattern": "omni",
     "polarization": "vertical", "antenna_type": "omnidirectional",
     "notes": "Higher-band rugged UWB omni."},
    {"id": "em6851", "manufacturer": "Electro-Metrics", "model": "EM-6851",
     "label": "EM-6851 Omni (2 – 18 GHz)", "category": "Wideband Omni",
     "freq_min_hz": 2e9, "freq_max_hz": 18e9,
     "peak_gain_dbi": 2.0, "polar_pattern": "omni",
     "polarization": "vertical", "antenna_type": "omnidirectional",
     "notes": "Microwave vertical omni."},
    {"id": "em6853", "manufacturer": "Electro-Metrics", "model": "EM-6853",
     "label": "EM-6853 UWB Omni (300 MHz – 43.5 GHz)", "category": "UWB Omni",
     "freq_min_hz": 300e6, "freq_max_hz": 43.5e9,
     "peak_gain_dbi": 2.0, "polar_pattern": "omni",
     "polarization": "vertical", "antenna_type": "omnidirectional",
     "notes": "Lab-grade UWB vertical omni."},
    {"id": "em6854", "manufacturer": "Electro-Metrics", "model": "EM-6854",
     "label": "EM-6854 Wideband Omni (20 MHz – 40 GHz)", "category": "UWB Omni",
     "freq_min_hz": 20e6, "freq_max_hz": 40e9,
     "peak_gain_dbi": 2.0, "polar_pattern": "omni",
     "polarization": "vertical", "antenna_type": "omnidirectional",
     "notes": "Extended-range UWB omni."},
    {"id": "em6856", "manufacturer": "Electro-Metrics", "model": "EM-6856",
     "label": "EM-6856 UWB Omni (20 MHz – 43.5 GHz)", "category": "UWB Omni",
     "freq_min_hz": 20e6, "freq_max_hz": 43.5e9,
     "peak_gain_dbi": 2.0, "polar_pattern": "omni",
     "polarization": "vertical", "antenna_type": "omnidirectional",
     "notes": "2 dBi nominal (datasheet); ~45° elevation HPBW."},
    {"id": "em6865", "manufacturer": "Electro-Metrics", "model": "EM-6865",
     "label": "EM-6865 Omni (2 – 18 GHz)", "category": "Wideband Omni",
     "freq_min_hz": 2e9, "freq_max_hz": 18e9,
     "peak_gain_dbi": 2.0, "polar_pattern": "omni",
     "polarization": "vertical", "antenna_type": "omnidirectional",
     "notes": "Wideband microwave omni."},
    {"id": "em6865a", "manufacturer": "Electro-Metrics", "model": "EM-6865A",
     "label": "EM-6865A Omni (2 – 18 GHz)", "category": "Wideband Omni",
     "freq_min_hz": 2e9, "freq_max_hz": 18e9,
     "peak_gain_dbi": 3.0, "polar_pattern": "omni",
     "polarization": "vertical", "antenna_type": "omnidirectional",
     "notes": "Improved 'A' revision of EM-6865."},

    # ── Ultra-wideband directional / kit ───────────────────────────────
    {"id": "em6600", "manufacturer": "Electro-Metrics", "model": "EM-6600",
     "label": "EM-6600 UWB Antenna (1.3 – 50 GHz)", "category": "UWB Directional",
     "freq_min_hz": 1.3e9, "freq_max_hz": 50e9,
     "peak_gain_dbi": 8.0, "polar_pattern": "horn",
     "polarization": "linear", "antenna_type": "horn",
     "notes": "Decade-wide UWB directional, microwave to mmWave."},
    {"id": "em6945", "manufacturer": "Electro-Metrics", "model": "EM-6945",
     "label": "EM-6945 UWB Antenna (500 MHz – 25 GHz)", "category": "UWB Directional",
     "freq_min_hz": 500e6, "freq_max_hz": 25e9,
     "peak_gain_dbi": 7.0, "polar_pattern": "log_periodic",
     "polarization": "linear", "antenna_type": "log_periodic",
     "notes": "UWB directional with LPDA-like radiation."},
    {"id": "em6964", "manufacturer": "Electro-Metrics", "model": "EM-6964",
     "label": "EM-6964 UWB Antenna (250 MHz – 26 GHz)", "category": "UWB Directional",
     "freq_min_hz": 250e6, "freq_max_hz": 26e9,
     "peak_gain_dbi": 7.0, "polar_pattern": "log_periodic",
     "polarization": "linear", "antenna_type": "log_periodic",
     "notes": "Wider-band UWB directional."},

    # ── Tactical, manpack, handheld ────────────────────────────────────
    {"id": "em6550", "manufacturer": "Electro-Metrics", "model": "EM-6550",
     "label": "EM-6550 Wideband Manpack", "category": "Manpack",
     "freq_min_hz": 30e6, "freq_max_hz": 6e9,
     "peak_gain_dbi": 2.0, "polar_pattern": "omni",
     "polarization": "vertical", "antenna_type": "whip_quarter_wave",
     "notes": "Wideband manpack antenna for tactical comms."},
    {"id": "em6907", "manufacturer": "Electro-Metrics", "model": "EM-6907",
     "label": "EM-6907 Passive Handheld Directional", "category": "Handheld DF",
     "freq_min_hz": 100e6, "freq_max_hz": 6e9,
     "peak_gain_dbi": 6.0, "polar_pattern": "log_periodic",
     "polarization": "linear", "antenna_type": "log_periodic",
     "notes": "Handheld direction-finding LPDA."},

    # ── Flat-panel patches ─────────────────────────────────────────────
    {"id": "em6173", "manufacturer": "Electro-Metrics", "model": "EM-6173",
     "label": "EM-6173 Flat Panel", "category": "Panel",
     "freq_min_hz": 700e6, "freq_max_hz": 6e9,
     "peak_gain_dbi": 8.0, "polar_pattern": "patch",
     "polarization": "linear", "antenna_type": "patch",
     "notes": "Low-profile microstrip panel."},
    {"id": "em6174", "manufacturer": "Electro-Metrics", "model": "EM-6174",
     "label": "EM-6174 Flat Panel", "category": "Panel",
     "freq_min_hz": 700e6, "freq_max_hz": 6e9,
     "peak_gain_dbi": 8.0, "polar_pattern": "patch",
     "polarization": "linear", "antenna_type": "patch",
     "notes": "Flat panel sibling of EM-6173."},

    # ── MIMO directional panels (5G/4G test) ───────────────────────────
    {"id": "em6241", "manufacturer": "Electro-Metrics", "model": "EM-6241",
     "label": "EM-6241 Directional SISO Vertical", "category": "MIMO Panel",
     "freq_min_hz": 600e6, "freq_max_hz": 6e9,
     "peak_gain_dbi": 9.0, "polar_pattern": "sector_120",
     "polarization": "vertical", "antenna_type": "sector_120",
     "notes": "Single-port vertically-polarised directional panel."},
    {"id": "em6242", "manufacturer": "Electro-Metrics", "model": "EM-6242",
     "label": "EM-6242 Directional 2x2 MIMO (slant)", "category": "MIMO Panel",
     "freq_min_hz": 600e6, "freq_max_hz": 6e9,
     "peak_gain_dbi": 9.0, "polar_pattern": "sector_120",
     "polarization": "linear", "antenna_type": "sector_120",
     "notes": "2x2 MIMO panel, ±45° slant polarisation."},
    {"id": "em6244", "manufacturer": "Electro-Metrics", "model": "EM-6244",
     "label": "EM-6244 Directional 4x4 MIMO (slant)", "category": "MIMO Panel",
     "freq_min_hz": 600e6, "freq_max_hz": 6e9,
     "peak_gain_dbi": 9.0, "polar_pattern": "sector_120",
     "polarization": "linear", "antenna_type": "sector_120",
     "notes": "4x4 MIMO panel, ±45° slant polarisation."},
    {"id": "em6481", "manufacturer": "Electro-Metrics", "model": "EM-6481",
     "label": "EM-6481 Directional SISO Vertical (mmWave)", "category": "MIMO Panel",
     "freq_min_hz": 24e9, "freq_max_hz": 40e9,
     "peak_gain_dbi": 12.0, "polar_pattern": "sector_60",
     "polarization": "vertical", "antenna_type": "sector_60",
     "notes": "5G NR FR2 single-port directional panel."},
    {"id": "em6482", "manufacturer": "Electro-Metrics", "model": "EM-6482",
     "label": "EM-6482 Directional 2x2 MIMO (mmWave)", "category": "MIMO Panel",
     "freq_min_hz": 24e9, "freq_max_hz": 40e9,
     "peak_gain_dbi": 12.0, "polar_pattern": "sector_60",
     "polarization": "linear", "antenna_type": "sector_60",
     "notes": "5G NR FR2 2x2 MIMO panel."},
    {"id": "em6484", "manufacturer": "Electro-Metrics", "model": "EM-6484",
     "label": "EM-6484 Directional 4x4 MIMO (mmWave)", "category": "MIMO Panel",
     "freq_min_hz": 24e9, "freq_max_hz": 40e9,
     "peak_gain_dbi": 12.0, "polar_pattern": "sector_60",
     "polarization": "linear", "antenna_type": "sector_60",
     "notes": "5G NR FR2 4x4 MIMO panel."},

    # ── Aaronia (spectrum-analyzer field antennas) ─────────────────────
    {"id": "aaronia_bicolog30100x", "manufacturer": "Aaronia", "model": "BicoLOG 30100X",
     "label": "BicoLOG 30100X (30 MHz – 1 GHz)", "category": "Bicone-LP Hybrid",
     "freq_min_hz": 30e6, "freq_max_hz": 1e9,
     "peak_gain_dbi": 5.0, "polar_pattern": "log_periodic",
     "polarization": "linear", "antenna_type": "log_periodic",
     "notes": "Compact biconical/log-periodic for EMC and spectrum monitoring; ~5 dBi typical."},
    {"id": "aaronia_bicolog7300x", "manufacturer": "Aaronia", "model": "BicoLOG 7300X",
     "label": "BicoLOG 7300X (30 MHz – 7.5 GHz)", "category": "Bicone-LP Hybrid",
     "freq_min_hz": 30e6, "freq_max_hz": 7.5e9,
     "peak_gain_dbi": 5.0, "polar_pattern": "log_periodic",
     "polarization": "linear", "antenna_type": "log_periodic",
     "notes": "Wideband bilog for full VHF–microwave coverage in one antenna."},
    {"id": "aaronia_hyperlog4060", "manufacturer": "Aaronia", "model": "HyperLOG 4060",
     "label": "HyperLOG 4060 (400 MHz – 6 GHz)", "category": "Log-Periodic",
     "freq_min_hz": 400e6, "freq_max_hz": 6e9,
     "peak_gain_dbi": 5.0, "polar_pattern": "log_periodic",
     "polarization": "linear", "antenna_type": "log_periodic",
     "notes": "Compact handheld LPDA; cellular and Wi-Fi sweeps."},
    {"id": "aaronia_hyperlog7060", "manufacturer": "Aaronia", "model": "HyperLOG 7060",
     "label": "HyperLOG 7060 (680 MHz – 6 GHz)", "category": "Log-Periodic",
     "freq_min_hz": 680e6, "freq_max_hz": 6e9,
     "peak_gain_dbi": 6.0, "polar_pattern": "log_periodic",
     "polarization": "linear", "antenna_type": "log_periodic",
     "notes": "Mid-range LPDA; improved high-band performance over 4060."},
    {"id": "aaronia_hyperlog60100x", "manufacturer": "Aaronia", "model": "HyperLOG 60100X",
     "label": "HyperLOG 60100X (680 MHz – 10 GHz)", "category": "Log-Periodic",
     "freq_min_hz": 680e6, "freq_max_hz": 10e9,
     "peak_gain_dbi": 6.0, "polar_pattern": "log_periodic",
     "polarization": "linear", "antenna_type": "log_periodic",
     "notes": "Decade-wide LPDA, extends to 10 GHz; ~6 dBi typical, peak ~9 dBi."},
    {"id": "aaronia_omnilog30800", "manufacturer": "Aaronia", "model": "OmniLOG 30800",
     "label": "OmniLOG 30800 (300 MHz – 8 GHz)", "category": "Omni",
     "freq_min_hz": 300e6, "freq_max_hz": 8e9,
     "peak_gain_dbi": 3.0, "polar_pattern": "omni",
     "polarization": "vertical", "antenna_type": "omnidirectional",
     "notes": "Wideband vertical omni for monitoring; ~3 dBi typical."},
    {"id": "aaronia_omnilog70600", "manufacturer": "Aaronia", "model": "OmniLOG 70600",
     "label": "OmniLOG 70600 (700 MHz – 6 GHz)", "category": "Omni",
     "freq_min_hz": 700e6, "freq_max_hz": 6e9,
     "peak_gain_dbi": 3.0, "polar_pattern": "omni",
     "polarization": "vertical", "antenna_type": "omnidirectional",
     "notes": "Cellular / Wi-Fi vertical omni, ~3 dBi."},

    # ── JEM Engineering (military / aerospace) ─────────────────────────
    {"id": "jem138", "manufacturer": "JEM Engineering", "model": "JEM-138",
     "label": "JEM-138 Blade (30 – 512 MHz)", "category": "VHF/UHF Blade",
     "freq_min_hz": 30e6, "freq_max_hz": 512e6,
     "peak_gain_dbi": 0.0, "polar_pattern": "omni",
     "polarization": "vertical", "antenna_type": "whip_quarter_wave",
     "notes": "Wideband military blade for land/airborne platforms; ~0 dBi typical."},
    {"id": "jem148", "manufacturer": "JEM Engineering", "model": "JEM-148",
     "label": "JEM-148 Blade (225 – 2000 MHz)", "category": "Multiband Blade",
     "freq_min_hz": 225e6, "freq_max_hz": 2e9,
     "peak_gain_dbi": 3.0, "polar_pattern": "omni",
     "polarization": "vertical", "antenna_type": "whip_quarter_wave",
     "notes": "Multiband communications blade, common on tactical vehicles and UAS."},
    {"id": "jem176", "manufacturer": "JEM Engineering", "model": "JEM-176",
     "label": "JEM-176 GPS Patch (L1)", "category": "GPS",
     "freq_min_hz": 1.572e9, "freq_max_hz": 1.578e9,
     "peak_gain_dbi": 5.0, "polar_pattern": "patch",
     "polarization": "circular", "antenna_type": "patch",
     "notes": "L1 GPS patch, RHCP, ceramic substrate."},
    {"id": "jem185", "manufacturer": "JEM Engineering", "model": "JEM-185",
     "label": "JEM-185 Biconical (30 MHz – 6 GHz)", "category": "Bicone-LP Hybrid",
     "freq_min_hz": 30e6, "freq_max_hz": 6e9,
     "peak_gain_dbi": 5.0, "polar_pattern": "log_periodic",
     "polarization": "linear", "antenna_type": "log_periodic",
     "notes": "Wideband biconical/log-periodic for SIGINT and EMC test."},
    {"id": "jem1020", "manufacturer": "JEM Engineering", "model": "JEM-1020",
     "label": "JEM-1020 LPDA (100 – 1000 MHz)", "category": "Log-Periodic",
     "freq_min_hz": 100e6, "freq_max_hz": 1e9,
     "peak_gain_dbi": 7.0, "polar_pattern": "log_periodic",
     "polarization": "linear", "antenna_type": "log_periodic",
     "notes": "Directional VHF/UHF LPDA for signal hunting and direction finding."},
    {"id": "jem147", "manufacturer": "JEM Engineering", "model": "JEM-147",
     "label": "JEM-147 Wideband Blade (225 – 2500 MHz)", "category": "Multiband Blade",
     "freq_min_hz": 225e6, "freq_max_hz": 2.5e9,
     "peak_gain_dbi": 3.0, "polar_pattern": "omni",
     "polarization": "vertical", "antenna_type": "whip_quarter_wave",
     "notes": "Conformal wideband blade, low profile for airborne installation."},

    # ── Common generics (sanity defaults) ──────────────────────────────
    {"id": "generic_whip_24", "manufacturer": "Generic", "model": "Whip 2.4 GHz",
     "label": "Whip 2.4 GHz (Wi-Fi / Zigbee)", "category": "Whip",
     "freq_min_hz": 2.4e9, "freq_max_hz": 2.5e9,
     "peak_gain_dbi": 2.0, "polar_pattern": "omni",
     "polarization": "vertical", "antenna_type": "whip_quarter_wave",
     "notes": "Standard rubber-duck whip, ~2 dBi typical."},
    {"id": "generic_whip_58", "manufacturer": "Generic", "model": "Whip 5.8 GHz",
     "label": "Whip 5.8 GHz (Wi-Fi / FPV)", "category": "Whip",
     "freq_min_hz": 5.7e9, "freq_max_hz": 5.9e9,
     "peak_gain_dbi": 3.0, "polar_pattern": "omni",
     "polarization": "vertical", "antenna_type": "whip_quarter_wave",
     "notes": "5 GHz dipole / sleeved monopole, ~3 dBi."},
    {"id": "generic_yagi_70cm", "manufacturer": "Generic", "model": "Yagi 9-el 70 cm",
     "label": "9-element Yagi 70 cm (430 – 440 MHz)", "category": "Directional",
     "freq_min_hz": 430e6, "freq_max_hz": 440e6,
     "peak_gain_dbi": 13.0, "polar_pattern": "yagi_9",
     "polarization": "linear", "antenna_type": "yagi_9el",
     "notes": "Long-yagi 70 cm DXing antenna, ~13 dBi."},
    {"id": "generic_yagi_2m", "manufacturer": "Generic", "model": "Yagi 5-el 2 m",
     "label": "5-element Yagi 2 m (144 – 148 MHz)", "category": "Directional",
     "freq_min_hz": 144e6, "freq_max_hz": 148e6,
     "peak_gain_dbi": 10.0, "polar_pattern": "yagi_5",
     "polarization": "linear", "antenna_type": "yagi_5el",
     "notes": "Common 2 m portable Yagi, ~10 dBi."},
    {"id": "generic_dish_058_58", "manufacturer": "Generic", "model": "0.6 m dish 5.8 GHz",
     "label": "0.6 m parabolic 5.8 GHz", "category": "Aperture",
     "freq_min_hz": 5.7e9, "freq_max_hz": 5.9e9,
     "peak_gain_dbi": 24.0, "polar_pattern": "parabolic_medium",
     "polarization": "linear", "antenna_type": "parabolic_dish",
     "notes": "Compact dish for point-to-point links, ~24 dBi."},
    {"id": "generic_sector_24_120", "manufacturer": "Generic", "model": "Sector 120° 2.4 GHz",
     "label": "Sector 120° 2.4 GHz", "category": "Sector",
     "freq_min_hz": 2.4e9, "freq_max_hz": 2.5e9,
     "peak_gain_dbi": 14.0, "polar_pattern": "sector_120",
     "polarization": "linear", "antenna_type": "sector_120",
     "notes": "Common 3-sector outdoor Wi-Fi panel."},
    {"id": "generic_sector_5_60", "manufacturer": "Generic", "model": "Sector 60° 5 GHz",
     "label": "Sector 60° 5 GHz", "category": "Sector",
     "freq_min_hz": 5e9, "freq_max_hz": 6e9,
     "peak_gain_dbi": 18.0, "polar_pattern": "sector_60",
     "polarization": "linear", "antenna_type": "sector_60",
     "notes": "Tight-sector 5 GHz panel, ~18 dBi."},

    # ── Furuno marine / surface-search radar antennas ──────────────────
    # Slotted-waveguide arrays for Furuno X-band (9.3–9.5 GHz) and S-band
    # (2.9–3.1 GHz) radars. Marine radar antennas radiate a horizontally
    # polarised narrow azimuth "fan" beam (the antenna rotates to sweep it);
    # gains/beamwidths are nominal datasheet figures (FAR-15xx/21xx/2xx7,
    # NavNet DRS series). Horizontal beamwidth narrows with array length.
    {"id": "furuno_xn12af_4", "manufacturer": "Furuno", "model": "XN12AF/4 (4 ft open array)",
     "label": "Furuno XN12AF/4 — 4 ft X-band open array (~1.9° HBW)", "category": "Marine radar",
     "freq_min_hz": 9.3e9, "freq_max_hz": 9.5e9,
     "peak_gain_dbi": 25.0, "polar_pattern": "marine_radar_fan",
     "polarization": "horizontal", "antenna_type": "phased_array",
     "notes": "4-foot slotted-waveguide open array, X-band 9410 MHz. ~1.9° horizontal × ~20° vertical fan beam, ~25 dBi. Used on FAR-15xx/21xx and DRS6A X-Class."},
    {"id": "furuno_xn13af_6", "manufacturer": "Furuno", "model": "XN13AF/6 (6.5 ft open array)",
     "label": "Furuno XN13AF/6 — 6.5 ft X-band open array (~1.23° HBW)", "category": "Marine radar",
     "freq_min_hz": 9.3e9, "freq_max_hz": 9.5e9,
     "peak_gain_dbi": 28.0, "polar_pattern": "marine_radar_fan",
     "polarization": "horizontal", "antenna_type": "phased_array",
     "notes": "6.5-foot slotted-waveguide open array, X-band. ~1.23° horizontal × ~20° vertical fan beam, ~28 dBi."},
    {"id": "furuno_xn20af_8", "manufacturer": "Furuno", "model": "XN20AF/8 (8 ft open array)",
     "label": "Furuno XN20AF/8 — 8 ft X-band open array (~0.95° HBW)", "category": "Marine radar",
     "freq_min_hz": 9.3e9, "freq_max_hz": 9.5e9,
     "peak_gain_dbi": 30.0, "polar_pattern": "marine_radar_fan",
     "polarization": "horizontal", "antenna_type": "phased_array",
     "notes": "8-foot slotted-waveguide open array, X-band. ~0.95° horizontal × ~20° vertical fan beam, ~30 dBi — high-resolution commercial/IMO installs."},
    {"id": "furuno_xn24af_12s", "manufacturer": "Furuno", "model": "XN24AF/12 (12 ft S-band open array)",
     "label": "Furuno XN24AF/12 — 12 ft S-band open array (~1.8° HBW)", "category": "Marine radar",
     "freq_min_hz": 2.9e9, "freq_max_hz": 3.1e9,
     "peak_gain_dbi": 28.0, "polar_pattern": "marine_radar_fan",
     "polarization": "horizontal", "antenna_type": "phased_array",
     "notes": "12-foot S-band (3050 MHz) slotted-waveguide open array for FAR-2xx7 S-band radars. ~1.8° horizontal × ~25° vertical fan beam, ~28 dBi — long-range / heavy-weather."},
    {"id": "furuno_drs4d_radome", "manufacturer": "Furuno", "model": "DRS4D-NXT / RSB-0070 (18\" radome)",
     "label": "Furuno DRS4D-NXT — 18\" X-band radome (~5.2° HBW)", "category": "Marine radar",
     "freq_min_hz": 9.3e9, "freq_max_hz": 9.5e9,
     "peak_gain_dbi": 24.0, "polar_pattern": "marine_radar_fan_wide",
     "polarization": "horizontal", "antenna_type": "phased_array",
     "notes": "NavNet 18-inch enclosed (radome) solid-state Doppler radar antenna. ~5.2° horizontal × ~25° vertical fan beam, ~24 dBi — common on yachts/small craft."},
    {"id": "furuno_drs6anxt_radome", "manufacturer": "Furuno", "model": "DRS6A-NXT / RSB-0094 (24\" radome)",
     "label": "Furuno DRS6A-NXT — 24\" X-band radome (~3.9° HBW)", "category": "Marine radar",
     "freq_min_hz": 9.3e9, "freq_max_hz": 9.5e9,
     "peak_gain_dbi": 26.0, "polar_pattern": "marine_radar_fan_wide",
     "polarization": "horizontal", "antenna_type": "phased_array",
     "notes": "NavNet 24-inch enclosed (radome) solid-state Doppler radar antenna. ~3.9° horizontal × ~25° vertical fan beam, ~26 dBi."},
]


@router.delete("/cache/purge")
async def purge_cache():
    """Manually trigger cache purge (removes stale terrain/building data)."""
    from app.core.simulation import purge_all_stale_caches
    purge_all_stale_caches()
    return {"status": "ok", "message": "Cache purge complete"}


# ─────────────────────────────────────────────────────────────────────────────
# WebSocket — real-time progress for long coverage computations
# ─────────────────────────────────────────────────────────────────────────────

@router.websocket("/ws/simulate")
async def ws_simulate(websocket: WebSocket):
    """
    WebSocket endpoint for real-time coverage simulation with progress updates.
    Client sends JSON request; server streams progress + final result.
    """
    await websocket.accept()
    try:
        data = await websocket.receive_json()
        req_model = CoverageRequestModel(**data)
        sim = get_simulator()
        sim_req = _build_coverage_req(req_model)

        # Override to reduce radials for real-time preview
        if sim_req.num_radials > 360:
            sim_req.num_radials = 360

        await websocket.send_json({"type": "progress", "value": 5,
                                    "message": "Downloading terrain data..."})

        # Compute with progress updates
        result = await sim.compute_coverage(sim_req)

        await websocket.send_json({"type": "progress", "value": 95,
                                    "message": "Rendering coverage..."})

        await websocket.send_json({
            "type": "result",
            "geojson": result.geojson,
            "metadata": {
                "max_range_km": result.max_range_km,
                "avg_signal_dbm": result.avg_signal_dbm,
                "covered_area_km2": result.covered_area_km2,
                "space_weather": result.space_weather,
                "warnings": result.warnings,
                "computation_time_s": result.computation_time_s,
                "gpu_used": result.gpu_used,
            },
        })
    except WebSocketDisconnect:
        pass
    except Exception as e:
        log.exception("WebSocket error")
        try:
            await websocket.send_json({"type": "error", "message": str(e)})
        except Exception:
            pass


# ─────────────────────────────────────────────────────────────────────────────
# New Pydantic models
# ─────────────────────────────────────────────────────────────────────────────

class WaypointModel(BaseModel):
    lat: float = Field(..., ge=-90, le=90)
    lon: float = Field(..., ge=-180, le=180)


class RouteRequestModel(BaseModel):
    waypoints: list[WaypointModel] = Field(..., min_length=2)
    receiver_lat: float = Field(..., ge=-90, le=90)
    receiver_lon: float = Field(..., ge=-180, le=180)
    transmitter: TransmitterModel = Field(default_factory=TransmitterModel)
    receiver: ReceiverModel = Field(default_factory=ReceiverModel)
    propagation_model: str = "itm"
    wave_type: str = "auto"
    atmosphere: Optional[AtmosphereModel] = None
    context: int = Field(2, ge=1, le=3)
    diffraction_model: str = "none"
    clutter_height_m: float = Field(0.0, ge=0)


class MultipointRequestModel(BaseModel):
    tx_points: list[WaypointModel] = Field(..., min_length=1)
    receiver_lat: float = Field(..., ge=-90, le=90)
    receiver_lon: float = Field(..., ge=-180, le=180)
    transmitter: TransmitterModel = Field(default_factory=TransmitterModel)
    receiver: ReceiverModel = Field(default_factory=ReceiverModel)
    propagation_model: str = "itm"
    wave_type: str = "auto"
    atmosphere: Optional[AtmosphereModel] = None
    context: int = Field(2, ge=1, le=3)
    diffraction_model: str = "none"
    clutter_height_m: float = Field(0.0, ge=0)


class ManetNodeModel(BaseModel):
    lat: float = Field(..., ge=-90, le=90)
    lon: float = Field(..., ge=-180, le=180)
    height_m: float = Field(10.0, ge=0)
    label: str = ""


class ManetRequestModel(BaseModel):
    nodes: list[ManetNodeModel] = Field(..., min_length=2, max_length=30)
    transmitter: TransmitterModel = Field(default_factory=TransmitterModel)
    receiver: ReceiverModel = Field(default_factory=ReceiverModel)
    propagation_model: str = "itm"
    wave_type: str = "auto"
    atmosphere: Optional[AtmosphereModel] = None
    context: int = Field(2, ge=1, le=3)
    diffraction_model: str = "none"
    clutter_height_m: float = Field(0.0, ge=0)
    sensitivity_dbm: float = Field(-100.0, le=0)


class BestServerSiteModel(BaseModel):
    lat: float = Field(..., ge=-90, le=90)
    lon: float = Field(..., ge=-180, le=180)
    height_m: float = Field(30.0, ge=0)
    label: str = ""
    power_dbm: Optional[float] = None
    frequency_hz: Optional[float] = None


class BestServerRequestModel(BaseModel):
    query_lat: float = Field(..., ge=-90, le=90)
    query_lon: float = Field(..., ge=-180, le=180)
    tx_sites: list[BestServerSiteModel] = Field(..., min_length=1)
    transmitter: TransmitterModel = Field(default_factory=TransmitterModel)
    receiver: ReceiverModel = Field(default_factory=ReceiverModel)
    propagation_model: str = "itm"
    wave_type: str = "auto"
    atmosphere: Optional[AtmosphereModel] = None
    context: int = Field(2, ge=1, le=3)
    clutter_height_m: float = Field(0.0, ge=0)


class InterferenceRequestModel(BaseModel):
    signal_geojson: dict
    noise_geojson: dict


class SuperLayerRequestModel(BaseModel):
    layers: list[dict] = Field(..., min_length=2)
    grid_deg: float = Field(0.001, gt=0)


class BestSitePolygonRequestModel(BaseModel):
    polygon: list[WaypointModel] = Field(..., min_length=3)
    coverage_pct: float = Field(50.0, ge=5, le=100)
    base_transmitter: TransmitterModel = Field(default_factory=TransmitterModel)
    receiver: ReceiverModel = Field(default_factory=ReceiverModel)
    propagation_model: str = "itm"
    wave_type: str = "auto"
    radius_km: float = Field(30.0, gt=0, le=500)
    num_radials: int = Field(180, ge=8, le=720)
    points_per_radial: int = Field(150, ge=10, le=500)
    min_signal_dbm: float = Field(-100.0, le=0)
    atmosphere: Optional[AtmosphereModel] = None
    terrain_resolution: str = "srtm3"
    context: int = Field(2, ge=1, le=3)
    diffraction_model: str = "none"


class RayTraceRequestModel(BaseModel):
    tx_lat: float = Field(..., ge=-90, le=90)
    tx_lon: float = Field(..., ge=-180, le=180)
    tx_height_m: float = Field(30.0, ge=0)
    tx_power_dbm: float = Field(27.0, ge=-30, le=100)
    frequency_hz: float = Field(433e6, ge=1, le=300e9)
    num_azimuths: int = Field(36, ge=4, le=360)
    num_elevations: int = Field(5, ge=1, le=20)
    max_range_m: float = Field(10000.0, gt=0, le=200000)
    num_points: int = Field(200, ge=10, le=1000)
    ground_material: str = "average_ground"
    vegetation_height_m: float = Field(0.0, ge=0)
    building_height_m: float = Field(0.0, ge=0)
    enable_reflections: bool = True
    min_signal_dbm: float = Field(-120.0, le=0)


class SatelliteVisibilityRequestModel(BaseModel):
    ground_lat: float = Field(..., ge=-90, le=90)
    ground_lon: float = Field(..., ge=-180, le=180)
    ground_height_m: float = Field(0.0, ge=0)
    constellation: str = "STARLINK"   # STARLINK | ISS | GPS | etc.
    min_elevation_deg: float = Field(10.0, ge=0, le=90)


# ─────────────────────────────────────────────────────────────────────────────
# Route Analysis endpoint
# ─────────────────────────────────────────────────────────────────────────────

@router.post("/simulate/route")
async def simulate_route(req: RouteRequestModel):
    """
    Route Analysis: run P2P from each waypoint to a fixed receiver.
    Returns GeoJSON with coloured points + connecting line.
    """
    sim = get_simulator()
    try:
        model = _safe_model(req.propagation_model)
        waypoints = [(w.lat, w.lon) for w in req.waypoints]
        geojson = await sim.compute_route(
            waypoints=waypoints,
            receiver_lat=req.receiver_lat,
            receiver_lon=req.receiver_lon,
            transmitter=_build_transmitter(req.transmitter),
            receiver=_build_receiver(req.receiver),
            propagation_model=model,
            wave_type=req.wave_type,
            atmosphere=req.atmosphere.dict() if req.atmosphere else None,
            context=req.context,
            diffraction_model=req.diffraction_model if req.diffraction_model != "none" else None,
            clutter_height_m=req.clutter_height_m,
        )
        return {"status": "ok", "geojson": geojson}
    except Exception as e:
        log.exception("Route analysis error")
        raise HTTPException(status_code=500, detail=str(e))


# ─────────────────────────────────────────────────────────────────────────────
# Multipoint Analysis endpoint
# ─────────────────────────────────────────────────────────────────────────────

@router.post("/simulate/multipoint")
async def simulate_multipoint(req: MultipointRequestModel):
    """
    Multipoint Analysis: run P2P from each TX candidate to a fixed receiver.
    Returns GeoJSON with coloured signal dots.
    """
    sim = get_simulator()
    try:
        model = _safe_model(req.propagation_model)
        tx_points = [(p.lat, p.lon) for p in req.tx_points]
        geojson = await sim.compute_multipoint(
            tx_points=tx_points,
            receiver_lat=req.receiver_lat,
            receiver_lon=req.receiver_lon,
            transmitter=_build_transmitter(req.transmitter),
            receiver=_build_receiver(req.receiver),
            propagation_model=model,
            wave_type=req.wave_type,
            atmosphere=req.atmosphere.dict() if req.atmosphere else None,
            context=req.context,
            diffraction_model=req.diffraction_model if req.diffraction_model != "none" else None,
            clutter_height_m=req.clutter_height_m,
        )
        return {"status": "ok", "geojson": geojson}
    except Exception as e:
        log.exception("Multipoint analysis error")
        raise HTTPException(status_code=500, detail=str(e))


# ─────────────────────────────────────────────────────────────────────────────
# MANET Planning endpoint
# ─────────────────────────────────────────────────────────────────────────────

@router.post("/simulate/manet")
async def simulate_manet(req: ManetRequestModel):
    """
    MANET Planning: compute P2P between all N*(N-1)/2 node pairs.
    Returns GeoJSON links (LineString) + node markers (Point).
    """
    sim = get_simulator()
    try:
        model = _safe_model(req.propagation_model)
        nodes = [{"lat": n.lat, "lon": n.lon, "height_m": n.height_m, "label": n.label}
                 for n in req.nodes]
        geojson = await sim.compute_manet(
            nodes=nodes,
            transmitter=_build_transmitter(req.transmitter),
            receiver=_build_receiver(req.receiver),
            propagation_model=model,
            wave_type=req.wave_type,
            atmosphere=req.atmosphere.dict() if req.atmosphere else None,
            context=req.context,
            diffraction_model=req.diffraction_model if req.diffraction_model != "none" else None,
            clutter_height_m=req.clutter_height_m,
            sensitivity_dbm=req.sensitivity_dbm,
        )
        return {"status": "ok", "geojson": geojson}
    except Exception as e:
        log.exception("MANET planning error")
        raise HTTPException(status_code=500, detail=str(e))


# ─────────────────────────────────────────────────────────────────────────────
# Best Server endpoint
# ─────────────────────────────────────────────────────────────────────────────

@router.post("/simulate/best_server")
async def simulate_best_server(req: BestServerRequestModel):
    """
    Best Server Tool: given a clicked location, which TX site serves it best?
    Returns ranked list of sites with signal levels.
    """
    sim = get_simulator()
    try:
        model = _safe_model(req.propagation_model)
        sites = [{"lat": s.lat, "lon": s.lon, "height_m": s.height_m, "label": s.label,
                   "power_dbm": s.power_dbm, "frequency_hz": s.frequency_hz}
                 for s in req.tx_sites]
        result = await sim.compute_best_server(
            query_lat=req.query_lat,
            query_lon=req.query_lon,
            tx_sites=sites,
            transmitter=_build_transmitter(req.transmitter),
            receiver=_build_receiver(req.receiver),
            propagation_model=model,
            wave_type=req.wave_type,
            atmosphere=req.atmosphere.dict() if req.atmosphere else None,
            context=req.context,
            clutter_height_m=req.clutter_height_m,
        )
        return {"status": "ok", **result}
    except Exception as e:
        log.exception("Best server error")
        raise HTTPException(status_code=500, detail=str(e))


# ─────────────────────────────────────────────────────────────────────────────
# Interference Analysis endpoint
# ─────────────────────────────────────────────────────────────────────────────

@router.post("/simulate/interference")
async def simulate_interference(req: InterferenceRequestModel):
    """
    Interference Analysis: compute SNR = signal - noise at each location.
    Takes two GeoJSON FeatureCollections, returns new one with snr_db property.
    """
    sim = get_simulator()
    try:
        result_geojson = sim.compute_interference(req.signal_geojson, req.noise_geojson)
        return {"status": "ok", "geojson": result_geojson}
    except Exception as e:
        log.exception("Interference analysis error")
        raise HTTPException(status_code=500, detail=str(e))


# ─────────────────────────────────────────────────────────────────────────────
# Super Layer endpoint
# ─────────────────────────────────────────────────────────────────────────────

@router.post("/simulate/super_layer")
async def simulate_super_layer(req: SuperLayerRequestModel):
    """
    Super Layer Merge: merge multiple GeoJSON coverage layers, take max signal.
    Returns merged GeoJSON FeatureCollection.
    """
    sim = get_simulator()
    try:
        result_geojson = sim.compute_super_layer(req.layers, req.grid_deg)
        return {"status": "ok", "geojson": result_geojson}
    except Exception as e:
        log.exception("Super layer error")
        raise HTTPException(status_code=500, detail=str(e))


# ─────────────────────────────────────────────────────────────────────────────
# Best Site Polygon endpoint
# ─────────────────────────────────────────────────────────────────────────────

@router.post("/simulate/best_site_polygon")
async def simulate_best_site_polygon(req: BestSitePolygonRequestModel):
    """
    Best Site (Polygon): grid-sample TX candidates inside a drawn polygon,
    run coverage from each, return the best one.
    """
    sim = get_simulator()
    try:
        model = _safe_model(req.propagation_model)
        polygon_coords = [(p.lat, p.lon) for p in req.polygon]
        tx = req.base_transmitter.model_copy()
        result = await sim.compute_best_site_polygon(
            polygon_coords=polygon_coords,
            coverage_pct=req.coverage_pct,
            transmitter=_build_transmitter(tx),
            receiver=_build_receiver(req.receiver),
            propagation_model=model,
            wave_type=req.wave_type,
            radius_km=req.radius_km,
            num_radials=req.num_radials,
            points_per_radial=req.points_per_radial,
            min_signal_dbm=req.min_signal_dbm,
            atmosphere=req.atmosphere.dict() if req.atmosphere else None,
            terrain_resolution=req.terrain_resolution,
            context=req.context,
            diffraction_model=req.diffraction_model if req.diffraction_model != "none" else None,
        )
        return result
    except Exception as e:
        log.exception("Best site polygon error")
        raise HTTPException(status_code=500, detail=str(e))


# ─────────────────────────────────────────────────────────────────────────────
# Ray Trace endpoint
# ─────────────────────────────────────────────────────────────────────────────

@router.post("/simulate/ray_trace")
async def simulate_ray_trace(req: RayTraceRequestModel):
    """
    3D Ray Tracing with material interaction.
    Traces rays from TX, finds terrain intersection, computes Fresnel reflection,
    handles single-bounce reflection and vegetation/building penetration loss.
    Returns ray paths + signal at each point as GeoJSON FeatureCollection.
    """
    rt = get_ray_tracer()
    try:
        rt_req = RayTraceRequest(
            tx_lat=req.tx_lat, tx_lon=req.tx_lon,
            tx_height_m=req.tx_height_m,
            tx_power_dbm=req.tx_power_dbm,
            frequency_hz=req.frequency_hz,
            num_azimuths=req.num_azimuths,
            num_elevations=req.num_elevations,
            max_range_m=req.max_range_m,
            num_points=req.num_points,
            ground_material=req.ground_material,
            vegetation_height_m=req.vegetation_height_m,
            building_height_m=req.building_height_m,
            enable_reflections=req.enable_reflections,
            min_signal_dbm=req.min_signal_dbm,
        )
        result = await rt.trace(rt_req)
        return {
            "status": "ok",
            "geojson": result.geojson,
            "metadata": {
                "num_paths": len(result.paths),
                "computation_s": round(result.computation_s, 2),
                "warnings": result.warnings,
            },
        }
    except Exception as e:
        log.exception("Ray trace error")
        raise HTTPException(status_code=500, detail=str(e))


# ─────────────────────────────────────────────────────────────────────────────
# Satellite Visibility endpoint
# ─────────────────────────────────────────────────────────────────────────────

@router.post("/simulate/satellite_visibility")
async def simulate_satellite_visibility(req: SatelliteVisibilityRequestModel):
    """
    Satellite visibility: pull TLEs from CelesTrak, propagate each with **SGP4**
    (the canonical `sgp4` package if installed, else the vendored faithful
    near-earth SGP4 — WGS-72, SPACETRACK REPORT #3), and report sub-points,
    footprints, and true topocentric az/el/slant-range to the ground station.
    """
    import aiohttp as _aiohttp
    import datetime as _dt
    from app.core.propagation.sgp4_lib import Satellite, look_angles, propagation_backend

    constellation = req.constellation.upper().replace(" ", "+")
    url = f"https://celestrak.org/NORAD/elements/gp.php?NAME={constellation}&FORMAT=json"
    sats_raw = []
    try:
        timeout = _aiohttp.ClientTimeout(total=15)
        async with _aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url) as resp:
                if resp.status == 200:
                    sats_raw = await resp.json()
    except Exception as e:
        log.warning(f"CelesTrak fetch failed: {e}")

    if not sats_raw:
        return {"status": "ok", "geojson": {"type": "FeatureCollection", "features": []},
                "metadata": {"message": "No satellite data available from CelesTrak"}}

    sats_raw = sats_raw[:60]
    now = _dt.datetime.now(_dt.timezone.utc)
    features = []
    deep_space = 0
    total = 0
    obs_alt_m = float(getattr(req, "ground_alt_m", 0.0) or 0.0)

    for sat in sats_raw:
        line1 = sat.get("TLE_LINE1", "")
        line2 = sat.get("TLE_LINE2", "")
        name = sat.get("OBJECT_NAME", "Unknown")
        norad = sat.get("NORAD_CAT_ID", "")
        if not line1 or not line2:
            continue
        try:
            s = Satellite.from_tle(name, line1, line2)
            st = s.propagate(now)
        except Exception:
            continue
        if st.error:
            continue
        total += 1
        if st.deep_space:
            deep_space += 1
        az, el, rng = look_angles(req.ground_lat, req.ground_lon, obs_alt_m,
                                  st.lat_deg, st.lon_deg, st.alt_km)
        visible = el >= req.min_elevation_deg
        features.append({
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [st.lon_deg, st.lat_deg]},
            "properties": {
                "name": name, "norad_id": norad,
                "alt_km": round(st.alt_km, 1),
                "footprint_radius_km": round(st.footprint_radius_km, 1),
                "azimuth_deg_from_ground": round(az, 1),
                "elevation_deg_from_ground": round(el, 1),
                "slant_range_km": round(rng, 1),
                "visible": visible,
                "deep_space_sgp4": st.deep_space,
                "feature_type": "satellite",
            },
        })
        if visible:
            features.append({
                "type": "Feature",
                "geometry": {"type": "LineString", "coordinates": [[req.ground_lon, req.ground_lat], [st.lon_deg, st.lat_deg]]},
                "properties": {"name": name, "feature_type": "sat_los",
                               "elevation_deg": round(el, 1), "azimuth_deg": round(az, 1), "slant_range_km": round(rng, 1)},
            })

    visible_count = sum(1 for f in features if f.get("properties", {}).get("feature_type") == "satellite" and f["properties"]["visible"])
    md = {
        "constellation": constellation,
        "total_sats": total,
        "visible_count": visible_count,
        "ground_station": {"lat": req.ground_lat, "lon": req.ground_lon, "alt_m": obs_alt_m},
        "min_elevation_deg": req.min_elevation_deg,
        "timestamp_utc": now.isoformat(),
        "propagation_backend": propagation_backend(),
    }
    if deep_space:
        md["deep_space_count"] = deep_space
        if propagation_backend().startswith("vendored"):
            md["note"] = (f"{deep_space} satellite(s) have period ≥225 min (deep-space). The vendored "
                          "SGP4 covers the near-earth regime only — `pip install sgp4` for SDP4-grade "
                          "deep-space accuracy on those.")
    return {"status": "ok", "geojson": {"type": "FeatureCollection", "features": features}, "metadata": md}


# ─────────────────────────────────────────────────────────────────────────────
# Materials endpoint
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/materials")
async def get_materials():
    """Return list of available RF materials for ray tracing."""
    return {"materials": material_info()}


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _safe_model(model_str: str) -> PropagationModel:
    try:
        return PropagationModel(model_str)
    except ValueError:
        return PropagationModel.ITM
