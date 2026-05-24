# SPDX-License-Identifier: MIT OR Apache-2.0
# Copyright (c) 2026 Ares

"""
3D Ray Tracing with Material Interaction
Traces rays from a TX position, finds terrain intersections using elevation
profiles, computes Fresnel reflection coefficients, handles single-bounce
reflections, and computes penetration loss through vegetation/buildings.

Architecture follows diffraction.py patterns — pure functions operating on
elevation profiles, plus an async engine class that manages terrain fetching.
"""
import math
import asyncio
import logging
from dataclasses import dataclass, field
from typing import Optional

import numpy as np

from app.core.propagation.materials import (
    Material, MATERIALS, reflection_coefficient_db, penetration_loss_db,
)
from app.core.propagation.terrain import TerrainManager, haversine_distance, destination_point
from app.core.propagation.models import fspl_db

log = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# Request / Response dataclasses
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class RayTraceRequest:
    tx_lat:        float = 37.7749
    tx_lon:        float = -122.4194
    tx_height_m:   float = 30.0
    tx_power_dbm:  float = 27.0
    frequency_hz:  float = 433e6
    # Ray fan parameters
    num_azimuths:  int   = 36         # angular resolution
    num_elevations: int  = 5          # elevation angles to trace
    max_range_m:   float = 10_000.0   # maximum ray travel distance
    num_points:    int   = 200        # terrain sample points per ray
    # Material override for ground surface
    ground_material: str = "average_ground"
    # Vegetation layer height above terrain (0 = disabled)
    vegetation_height_m: float = 0.0
    # Building layer height (0 = disabled)
    building_height_m:  float = 0.0
    # Enable reflected ray contribution (single bounce)
    enable_reflections: bool = True
    # Receiver sensitivity for ray filtering
    min_signal_dbm: float = -120.0


@dataclass
class RayPoint:
    lat:           float
    lon:           float
    distance_m:    float
    direct_dbm:    float
    reflected_dbm: Optional[float]
    total_dbm:     float
    hit_terrain:   bool
    bounce_lat:    Optional[float] = None
    bounce_lon:    Optional[float] = None


@dataclass
class RayPath:
    azimuth_deg:   float
    elevation_deg: float
    points:        list[RayPoint] = field(default_factory=list)
    bounce_point:  Optional[tuple[float, float]] = None  # (lat, lon)


@dataclass
class RayTraceResult:
    paths:         list[RayPath] = field(default_factory=list)
    geojson:       Optional[dict] = None
    warnings:      list[str]     = field(default_factory=list)
    computation_s: float         = 0.0


# ─────────────────────────────────────────────────────────────────────────────
# Pure geometry / physics helpers
# ─────────────────────────────────────────────────────────────────────────────

def _find_terrain_intersection(
    elev_arr: np.ndarray,
    dist_arr: np.ndarray,
    tx_elev_m: float,
    tx_agl_m: float,
    ray_elev_deg: float,
) -> Optional[int]:
    """
    Find the first index where the ray hits the terrain.
    Returns index into elev_arr / dist_arr, or None if ray clears the profile.

    The ray starts at tx_elev_m + tx_agl_m with elevation angle ray_elev_deg.
    For negative elevation angles the ray goes downward (toward ground).
    """
    tx_total_h = tx_elev_m + tx_agl_m
    tan_el = math.tan(math.radians(ray_elev_deg))

    for i in range(1, len(elev_arr)):
        d = dist_arr[i]
        ray_h = tx_total_h + tan_el * d
        if ray_h <= elev_arr[i]:
            return i
    return None


def _interpolate_intersection(
    elev_arr: np.ndarray,
    dist_arr: np.ndarray,
    idx: int,
    tx_elev_m: float,
    tx_agl_m: float,
    ray_elev_deg: float,
) -> tuple[float, float]:
    """
    Sub-sample the terrain intersection between idx-1 and idx.
    Returns (distance_m, elevation_m) at the intersection point.
    """
    tx_total_h = tx_elev_m + tx_agl_m
    tan_el = math.tan(math.radians(ray_elev_deg))

    d0, d1 = dist_arr[idx - 1], dist_arr[idx]
    e0, e1 = elev_arr[idx - 1], elev_arr[idx]

    # Ray height at d: r(d) = tx_total_h + tan_el * d
    # Terrain linear interp: t(d) = e0 + (e1-e0)/(d1-d0) * (d-d0)
    # Solve r(d) = t(d):
    # tx_total_h + tan_el*d = e0 + slope*(d-d0)
    # d*(tan_el - slope) = e0 - tx_total_h - slope*d0
    dd = d1 - d0
    if dd < 1e-6:
        return float(d0), float(e0)

    slope = (e1 - e0) / dd
    denom = tan_el - slope
    if abs(denom) < 1e-9:
        # Nearly parallel — use midpoint
        d_int = (d0 + d1) / 2.0
    else:
        d_int = (e0 - tx_total_h + slope * (-d0)) / (-denom)
        d_int = max(d0, min(d1, d_int))

    e_int = e0 + slope * (d_int - d0)
    return float(d_int), float(e_int)


def _compute_incidence_angle(
    ray_elev_deg: float,
    terrain_slope_deg: float,
) -> float:
    """
    Compute angle of incidence from the surface normal.
    Surface normal is perpendicular to terrain slope.
    Returns angle in degrees (0 = normal, 90 = grazing).
    """
    # Angle between downward ray and surface
    # If terrain is flat (slope=0) and ray comes at 30° elevation downward,
    # angle of incidence from normal = 90 - 30 = 60°
    grazing = abs(ray_elev_deg) - abs(terrain_slope_deg)
    grazing = max(0.0, min(89.9, grazing))
    return 90.0 - grazing


def _compute_terrain_slope_deg(
    elev_arr: np.ndarray,
    dist_arr: np.ndarray,
    idx: int,
) -> float:
    """Local terrain slope in degrees at index idx."""
    i = max(1, min(idx, len(elev_arr) - 1))
    dd = dist_arr[i] - dist_arr[i - 1]
    if dd < 1e-6:
        return 0.0
    de = elev_arr[i] - elev_arr[i - 1]
    return math.degrees(math.atan2(de, dd))


def _reflected_ray_path_loss(
    d_to_bounce_m: float,
    d_bounce_to_rx_m: float,
    freq_hz: float,
    refl_db: float,
) -> float:
    """
    Total path loss for a single-bounce reflected ray.
    Uses FSPL for both legs plus the reflection coefficient.
    """
    if d_to_bounce_m < 1.0 or d_bounce_to_rx_m < 1.0:
        return 200.0
    total_dist = d_to_bounce_m + d_bounce_to_rx_m
    pl_direct = fspl_db(total_dist, freq_hz)
    # Reflection introduces an additional loss (refl_db is <= 0)
    return pl_direct - refl_db  # subtracting negative = adding positive loss


# ─────────────────────────────────────────────────────────────────────────────
# Ray Tracer Engine
# ─────────────────────────────────────────────────────────────────────────────

class RayTracer:
    """
    Async ray tracing engine.  Fetches terrain profiles via TerrainManager
    and computes direct + single-bounce signal levels.
    """

    def __init__(self):
        pass

    async def trace(self, req: RayTraceRequest) -> RayTraceResult:
        import time
        t0 = time.time()

        result = RayTraceResult()
        terrain = TerrainManager(resolution="srtm3")

        try:
            ground_mat = Material(req.ground_material)
        except ValueError:
            ground_mat = Material.AVERAGE_GROUND
            result.warnings.append(f"Unknown material '{req.ground_material}', using average_ground")

        # TX ground elevation
        try:
            tx_elev = await terrain.get_elevation(req.tx_lat, req.tx_lon)
        except Exception:
            tx_elev = 0.0
            result.warnings.append("Could not fetch TX elevation — using 0 m")

        freq_hz = req.frequency_hz

        # Elevation angles to sweep (all downward from horizontal, plus horizontal)
        el_angles = np.linspace(-60.0, 15.0, req.num_elevations)
        az_angles = np.linspace(0.0, 360.0, req.num_azimuths, endpoint=False)

        tasks = []
        for az in az_angles:
            for el in el_angles:
                tasks.append(self._trace_ray(
                    req, terrain, tx_elev, ground_mat,
                    float(az), float(el), freq_hz,
                ))

        ray_results = await asyncio.gather(*tasks, return_exceptions=True)

        for r in ray_results:
            if isinstance(r, Exception):
                log.debug(f"Ray trace error: {r}")
            elif r is not None:
                result.paths.append(r)

        result.geojson = self._build_geojson(result.paths, req.min_signal_dbm)
        result.computation_s = time.time() - t0

        await terrain.close()
        return result

    async def _trace_ray(
        self,
        req: RayTraceRequest,
        terrain: TerrainManager,
        tx_elev_m: float,
        ground_mat: Material,
        azimuth_deg: float,
        elevation_deg: float,
        freq_hz: float,
    ) -> Optional[RayPath]:
        """Trace a single ray and return RayPath with coverage points."""
        path = RayPath(azimuth_deg=azimuth_deg, elevation_deg=elevation_deg)

        # Compute endpoint of the ray
        lat2, lon2 = destination_point(
            req.tx_lat, req.tx_lon, azimuth_deg, req.max_range_m
        )

        try:
            dist_arr, elev_arr = await terrain.get_elevation_profile(
                req.tx_lat, req.tx_lon, lat2, lon2, req.num_points
            )
        except Exception:
            return None

        # Apply vegetation / building clutter
        effective_elev = elev_arr.copy()
        if req.vegetation_height_m > 0:
            effective_elev = effective_elev + req.vegetation_height_m
        if req.building_height_m > 0:
            effective_elev = effective_elev + req.building_height_m

        # Find terrain intersection
        hit_idx = _find_terrain_intersection(
            effective_elev, dist_arr, tx_elev_m, req.tx_height_m, elevation_deg
        )

        bounce_lat, bounce_lon = None, None
        refl_contribution_db = None

        if hit_idx is not None:
            # Compute bounce point
            d_hit, e_hit = _interpolate_intersection(
                effective_elev, dist_arr, hit_idx, tx_elev_m, req.tx_height_m, elevation_deg
            )
            # Lat/lon of bounce point
            frac = d_hit / req.max_range_m if req.max_range_m > 0 else 0.0
            bounce_lat = req.tx_lat + frac * (lat2 - req.tx_lat)
            bounce_lon = req.tx_lon + frac * (lon2 - req.tx_lon)
            path.bounce_point = (bounce_lat, bounce_lon)

            # Reflection coefficient
            if req.enable_reflections:
                slope_deg = _compute_terrain_slope_deg(effective_elev, dist_arr, hit_idx)
                inc_angle = _compute_incidence_angle(elevation_deg, slope_deg)
                refl_db = reflection_coefficient_db(
                    ground_mat, freq_hz, inc_angle, "vertical"
                )

                # Penetration loss for vegetation/building on top
                pen_loss = 0.0
                if req.vegetation_height_m > 0:
                    pen_loss += penetration_loss_db(Material.VEGETATION, freq_hz,
                                                     req.vegetation_height_m)
                if req.building_height_m > 0:
                    pen_loss += penetration_loss_db(Material.CONCRETE, freq_hz,
                                                     req.building_height_m)

                # Store for use in point computation below
                refl_contribution_db = refl_db - pen_loss

        # Now sample points along ray up to hit (or max range)
        max_i = hit_idx if hit_idx is not None else len(dist_arr) - 1

        for i in range(1, max_i + 1):
            d_m = float(dist_arr[i])
            if d_m < 1.0:
                continue

            frac = dist_arr[i] / req.max_range_m if req.max_range_m > 0 else float(i) / req.num_points
            pt_lat = req.tx_lat + frac * (lat2 - req.tx_lat)
            pt_lon = req.tx_lon + frac * (lon2 - req.tx_lon)

            # Direct signal (FSPL)
            direct_pl = fspl_db(d_m, freq_hz)
            direct_dbm = req.tx_power_dbm - direct_pl

            # Reflected contribution at each point (single-bounce approximation)
            reflected_dbm_pt = None
            if refl_contribution_db is not None and bounce_lat is not None and i > 0:
                # Use the point as a "virtual RX" and compute bounce path loss
                d_tx_bounce = float(dist_arr[hit_idx]) if hit_idx else d_m
                d_bounce_rx = abs(d_m - d_tx_bounce)
                if d_bounce_rx > 10.0:
                    bounce_pl = _reflected_ray_path_loss(
                        d_tx_bounce, d_bounce_rx, freq_hz, refl_contribution_db
                    )
                    reflected_dbm_pt = req.tx_power_dbm - bounce_pl

            # Combine direct + reflected (power addition)
            total_dbm = direct_dbm
            if reflected_dbm_pt is not None and reflected_dbm_pt > -200:
                p_direct = 10 ** (direct_dbm / 10.0)
                p_refl   = 10 ** (reflected_dbm_pt / 10.0)
                total_dbm = 10.0 * math.log10(p_direct + p_refl)

            hit_terrain = (i == max_i and hit_idx is not None)

            path.points.append(RayPoint(
                lat=pt_lat, lon=pt_lon, distance_m=d_m,
                direct_dbm=direct_dbm,
                reflected_dbm=reflected_dbm_pt,
                total_dbm=total_dbm,
                hit_terrain=hit_terrain,
                bounce_lat=bounce_lat if hit_terrain else None,
                bounce_lon=bounce_lon if hit_terrain else None,
            ))

        return path if path.points else None

    @staticmethod
    def _build_geojson(paths: list[RayPath], min_signal_dbm: float) -> dict:
        """Build GeoJSON FeatureCollection from ray paths."""
        features = []

        for path in paths:
            if not path.points:
                continue

            # Ray path as LineString
            coords = [[p.lon, p.lat] for p in path.points]
            if len(coords) >= 2:
                max_sig = max(p.total_dbm for p in path.points)
                features.append({
                    "type": "Feature",
                    "geometry": {"type": "LineString", "coordinates": coords},
                    "properties": {
                        "azimuth_deg":   round(path.azimuth_deg, 1),
                        "elevation_deg": round(path.elevation_deg, 1),
                        "max_signal_dbm": round(max_sig, 1),
                        "feature_type": "ray",
                    },
                })

            # Signal points (covered only)
            for p in path.points:
                if p.total_dbm >= min_signal_dbm:
                    features.append({
                        "type": "Feature",
                        "geometry": {"type": "Point", "coordinates": [p.lon, p.lat]},
                        "properties": {
                            "signal_dbm":    round(p.total_dbm, 1),
                            "direct_dbm":    round(p.direct_dbm, 1),
                            "reflected_dbm": round(p.reflected_dbm, 1) if p.reflected_dbm is not None else None,
                            "distance_m":    round(p.distance_m, 0),
                            "hit_terrain":   p.hit_terrain,
                            "covered":       True,
                            "feature_type":  "signal_point",
                        },
                    })

            # Bounce point marker
            if path.bounce_point:
                blat, blon = path.bounce_point
                features.append({
                    "type": "Feature",
                    "geometry": {"type": "Point", "coordinates": [blon, blat]},
                    "properties": {
                        "azimuth_deg":   round(path.azimuth_deg, 1),
                        "elevation_deg": round(path.elevation_deg, 1),
                        "feature_type":  "bounce",
                    },
                })

        return {"type": "FeatureCollection", "features": features}


# Singleton
_ray_tracer: Optional[RayTracer] = None


def get_ray_tracer() -> RayTracer:
    global _ray_tracer
    if _ray_tracer is None:
        _ray_tracer = RayTracer()
    return _ray_tracer
