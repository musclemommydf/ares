# SPDX-License-Identifier: MIT OR Apache-2.0
# Copyright (c) 2026 Ares

"""
Terrain Data Manager
Auto-downloads and caches SRTM/Copernicus DEM terrain elevation data.
Supports: SRTM 1-arc (30m), SRTM 3-arc (90m), Copernicus GLO-30, ASTER GDEM.
Also downloads building/obstacle data from OpenStreetMap via Overpass API.

GPU acceleration: Uses CuPy for batch terrain profile operations when available.
"""
import os
import math
import struct
import zipfile
import logging
import asyncio
import hashlib
from pathlib import Path
from typing import Optional
import numpy as np
import aiohttp
import aiofiles
from functools import lru_cache

try:
    import cupy as cp
    GPU_AVAILABLE = True
except ImportError:
    cp = None
    GPU_AVAILABLE = False

log = logging.getLogger(__name__)

EARTH_RADIUS_M = 6371000.0

# SRTM tile parameters
SRTM1_PIXELS = 3601   # 1 arc-second (30m)
SRTM3_PIXELS = 1201   # 3 arc-second (90m)

# Data source URLs
SRTM1_BASE = "https://s3.amazonaws.com/elevation-tiles-prod/skadi/{lat_band}/{filename}"
SRTM3_BASE = "https://srtm.csi.cgiar.org/wp-content/uploads/files/srtm_5x5/TIFF/{filename}"
COPERNICUS_BASE = "https://opentopography.s3.sdsc.edu/raster/COP30/COP30_hh/{filename}"
OPENTOPODATA_API = "https://api.opentopodata.org/v1/srtm30m"  # Free API, no key needed

# OpenStreetMap Overpass API
OVERPASS_API = "https://overpass-api.de/api/interpreter"

# Cache directory + offline data packs
from app.config import TERRAIN_CACHE_DIR, BUILDINGS_CACHE_DIR, PACKS_DIR


def _find_in_terrain_packs(filename: str) -> Optional[Path]:
    """Look for an SRTM ``.hgt`` tile inside any installed terrain pack
    (``data/packs/terrain/<id>/<filename>``). Lets pre-staged / offline terrain
    feed the propagation engine without going to the network."""
    try:
        for p in (PACKS_DIR / "terrain").glob(f"*/{filename}"):
            if p.is_file():
                return p
    except OSError:
        pass
    return None


def _buildings_from_packs(bbox: tuple[float, float, float, float]) -> Optional[list[dict]]:
    """Pull building footprints from installed building packs (data/packs/buildings/
    <id>/buildings.geojson) that fall inside ``bbox`` (s, w, n, e). Returns the same
    dict shape as ``TerrainManager._parse_osm_buildings`` (lat/lon centroid, height_m,
    material, rf_loss_db, polygon as [[lat,lon],…]), or ``None`` if no pack covers it."""
    import json
    s, w, n, e = bbox
    out: list[dict] = []
    found_pack = False
    try:
        for f in (PACKS_DIR / "buildings").glob("*/buildings.geojson"):
            found_pack = True
            try:
                fc = json.loads(f.read_text())
            except Exception:
                continue
            for feat in fc.get("features", []):
                geom = feat.get("geometry") or {}
                if geom.get("type") != "Polygon":
                    continue
                ring = (geom.get("coordinates") or [[]])[0]
                if len(ring) < 3:
                    continue
                clon = sum(p[0] for p in ring) / len(ring)
                clat = sum(p[1] for p in ring) / len(ring)
                if not (s <= clat <= n and w <= clon <= e):
                    continue
                props = feat.get("properties", {})
                height = props.get("height_m")
                if not isinstance(height, (int, float)) or height <= 0:
                    height = 10.0
                material = props.get("material") or "concrete"
                out.append({
                    "lat": clat, "lon": clon, "height_m": float(height),
                    "material": material, "rf_loss_db": _material_rf_loss(material),
                    "polygon": [[p[1], p[0]] for p in ring],   # pack stores [lon,lat] → [lat,lon]
                })
    except OSError:
        return None
    return out if found_pack else None


class TerrainTile:
    """A single SRTM terrain tile (1°×1° at specified resolution)."""

    def __init__(self, lat: int, lon: int, resolution: str = "srtm3"):
        self.lat = lat       # SW corner latitude (-90 to 89)
        self.lon = lon       # SW corner longitude (-180 to 179)
        self.resolution = resolution
        self.n_pixels = SRTM1_PIXELS if resolution == "srtm1" else SRTM3_PIXELS
        self.data: Optional[np.ndarray] = None

    @property
    def filename(self) -> str:
        lat_c = "N" if self.lat >= 0 else "S"
        lon_c = "E" if self.lon >= 0 else "W"
        return f"{lat_c}{abs(self.lat):02d}{lon_c}{abs(self.lon):03d}.hgt"

    @property
    def cache_path(self) -> Path:
        return TERRAIN_CACHE_DIR / self.resolution / self.filename

    def get_elevation(self, lat: float, lon: float) -> float:
        """Bilinear interpolation of elevation at (lat, lon)."""
        if self.data is None:
            return 0.0
        n = self.n_pixels
        # Fractional pixel coordinates
        row_f = (self.lat + 1 - lat) * (n - 1)   # row 0 = top (N)
        col_f = (lon - self.lon) * (n - 1)         # col 0 = left (W)
        row = int(row_f)
        col = int(col_f)
        row = max(0, min(row, n - 2))
        col = max(0, min(col, n - 2))
        dr = row_f - row
        dc = col_f - col
        # Bilinear interpolation
        v00 = float(self.data[row][col])
        v01 = float(self.data[row][col + 1])
        v10 = float(self.data[row + 1][col])
        v11 = float(self.data[row + 1][col + 1])
        # Handle SRTM void (value = -32768)
        vals = [v00, v01, v10, v11]
        vals = [v if v > -1000 else 0.0 for v in vals]
        v00, v01, v10, v11 = vals
        elev = (v00 * (1 - dr) * (1 - dc)
                + v01 * (1 - dr) * dc
                + v10 * dr * (1 - dc)
                + v11 * dr * dc)
        return elev

    def load_hgt(self, path: Path) -> bool:
        """Load SRTM .hgt binary file (big-endian int16)."""
        try:
            data = np.fromfile(str(path), dtype=">i2")
            n = self.n_pixels
            if data.size == n * n:
                self.data = data.reshape((n, n)).astype(np.float32)
                return True
        except Exception as e:
            log.error(f"Failed to load HGT {path}: {e}")
        return False


class TerrainManager:
    """
    Manages terrain tile downloading, caching, and elevation queries.
    Uses async download for multiple tiles simultaneously.
    """

    def __init__(self, resolution: str = "srtm3", use_gpu: bool = False):
        self.resolution = resolution
        self.use_gpu = use_gpu and GPU_AVAILABLE
        self._cache: dict[tuple, TerrainTile] = {}
        self._session: Optional[aiohttp.ClientSession] = None
        self._download_locks: dict[tuple, asyncio.Lock] = {}
        # Ensure dirs exist
        (TERRAIN_CACHE_DIR / resolution).mkdir(parents=True, exist_ok=True)

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            timeout = aiohttp.ClientTimeout(total=30, connect=10)
            self._session = aiohttp.ClientSession(timeout=timeout)
        return self._session

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()

    def _tile_key(self, lat: int, lon: int) -> tuple:
        return (lat, lon, self.resolution)

    async def _load_tile(self, lat: int, lon: int) -> TerrainTile:
        key = self._tile_key(lat, lon)
        if key in self._cache:
            return self._cache[key]

        # Per-tile lock prevents duplicate concurrent downloads
        if key not in self._download_locks:
            self._download_locks[key] = asyncio.Lock()
        async with self._download_locks[key]:
            # Re-check cache after acquiring lock
            if key in self._cache:
                return self._cache[key]

            tile = TerrainTile(lat, lon, self.resolution)
            cache_path = tile.cache_path

            # Try loading from disk cache first (works fully offline)
            if cache_path.exists():
                if tile.load_hgt(cache_path):
                    log.debug(f"Loaded terrain tile {tile.filename} from cache")
                    self._cache[key] = tile
                    return tile

            # Then an installed offline terrain pack (data/packs/terrain/<id>/) — SRTM1 .hgt.
            pack_path = _find_in_terrain_packs(tile.filename)
            if pack_path is not None:
                pack_tile = TerrainTile(lat, lon, "srtm1")
                if pack_tile.load_hgt(pack_path):
                    log.debug(f"Loaded terrain tile {tile.filename} from pack {pack_path.parent.name}")
                    self._cache[key] = pack_tile
                    return pack_tile

            # If srtm3 requested but only srtm1 cached, use higher-res data
            if self.resolution == "srtm3":
                srtm1_path = TERRAIN_CACHE_DIR / "srtm1" / tile.filename
                if srtm1_path.exists():
                    hires_tile = TerrainTile(lat, lon, "srtm1")
                    if hires_tile.load_hgt(srtm1_path):
                        log.debug(f"Using cached srtm1 tile for {tile.filename} (offline fallback)")
                        self._cache[key] = hires_tile
                        return hires_tile

            # Download from network — stores in appropriate resolution directory
            downloaded = await self._download_tile(tile)

            # After download, try requested resolution first, then srtm1 fallback
            if downloaded:
                if cache_path.exists() and tile.load_hgt(cache_path):
                    self._cache[key] = tile
                    return tile
                # AWS download went to srtm1/ — use it
                srtm1_path = TERRAIN_CACHE_DIR / "srtm1" / tile.filename
                if srtm1_path.exists():
                    hires_tile = TerrainTile(lat, lon, "srtm1")
                    if hires_tile.load_hgt(srtm1_path):
                        self._cache[key] = hires_tile
                        return hires_tile

            # No tile data — simulation continues with flat terrain (last resort)
            log.warning(f"Terrain unavailable for {tile.filename} — using flat terrain")
            self._cache[key] = tile
            return tile

    async def _download_tile(self, tile: TerrainTile) -> bool:
        """Download SRTM tile from best available source.
        Always writes atomically: download → .tmp → validate → rename.
        AWS Skadi always provides SRTM1 (1 arc-second) data regardless of
        requested resolution, so we store it in the srtm1 directory even when
        a coarser resolution was requested — _load_tile uses it as fallback."""
        import gzip
        import io

        cache_path = tile.cache_path
        cache_path.parent.mkdir(parents=True, exist_ok=True)

        session = await self._get_session()
        for url, dest_resolution in self._get_tile_urls(tile):
            try:
                log.info(f"Downloading terrain tile {tile.filename}")
                async with session.get(url) as resp:
                    if resp.status != 200:
                        continue
                    data = await resp.read()

                    # Decode content
                    if url.endswith(".hgt.gz"):
                        content = gzip.decompress(data)
                    elif url.endswith(".zip"):
                        content = None
                        with zipfile.ZipFile(io.BytesIO(data)) as zf:
                            for name in zf.namelist():
                                if name.upper().endswith(".HGT"):
                                    content = zf.read(name)
                                    break
                        if content is None:
                            continue
                    else:
                        content = data

                    # Determine actual destination (AWS gives srtm1 data always)
                    dest_path = TERRAIN_CACHE_DIR / dest_resolution / tile.filename
                    dest_path.parent.mkdir(parents=True, exist_ok=True)
                    tmp_path = dest_path.with_suffix('.tmp')

                    # Atomic write: write to .tmp, validate size, then rename
                    async with aiofiles.open(tmp_path, "wb") as f:
                        await f.write(content)

                    # Validate: must be at least a 1°×1° SRTM3 tile
                    min_size = SRTM3_PIXELS * SRTM3_PIXELS * 2  # 2 bytes per sample
                    if tmp_path.stat().st_size >= min_size:
                        tmp_path.replace(dest_path)  # atomic rename
                        return True
                    else:
                        tmp_path.unlink(missing_ok=True)
                        log.debug(f"Tile {tile.filename} from {url} too small, skipping")
            except Exception as e:
                log.debug(f"Terrain download failed ({url}): {e}")
                try:
                    tmp_path = (TERRAIN_CACHE_DIR / dest_resolution / tile.filename).with_suffix('.tmp')
                    tmp_path.unlink(missing_ok=True)
                except Exception:
                    pass
                continue

        return False

    def _get_tile_urls(self, tile: TerrainTile) -> list[tuple[str, str]]:
        """Return (url, dest_resolution) pairs in priority order.
        AWS Skadi provides SRTM1 data (3601×3601) always, so it is stored in
        srtm1/ regardless of the requested resolution and used as a high-res
        fallback.  USGS provides the actual 3 arc-second data for srtm3/."""
        lat = tile.lat
        lon = tile.lon
        lat_c = "N" if lat >= 0 else "S"
        lon_c = "E" if lon >= 0 else "W"
        lat_band = f"{lat_c}{abs(lat):02d}"
        fname_hgt = tile.filename  # e.g. N37W123.hgt

        urls = []

        # 1. AWS elevation-tiles — always 1 arc-second (SRTM1), global, no auth
        aws_fname = f"{lat_c}{abs(lat):02d}{lon_c}{abs(lon):03d}.hgt.gz"
        urls.append((
            f"https://s3.amazonaws.com/elevation-tiles-prod/skadi/{lat_band}/{aws_fname}",
            "srtm1",   # store as srtm1 regardless of requested resolution
        ))

        # 2. USGS SRTM 3-arc-second zip — actual 90m data
        urls.append((
            f"https://dds.cr.usgs.gov/srtm/version2_1/SRTM3"
            f"/{self._usgs_region(lat, lon)}/{fname_hgt}.zip",
            "srtm3",
        ))

        return urls

    @staticmethod
    def _usgs_region(lat: int, lon: int) -> str:
        """Map lat/lon to USGS region name for SRTM download."""
        if -60 <= lat <= 60 and -180 <= lon <= -40:
            return "North_America" if lat >= 0 else "South_America"
        elif -60 <= lat <= 60 and -40 <= lon <= 60:
            return "Africa" if lat < 35 else "Eurasia"
        elif lat >= 0 and 60 <= lon <= 180:
            return "Eurasia"
        elif lat < 0 and 60 <= lon <= 180:
            return "Australia"
        return "Eurasia"

    def _tile_for_coord(self, lat: float, lon: float) -> tuple[int, int]:
        """Get SW corner of tile containing (lat, lon)."""
        return math.floor(lat), math.floor(lon)

    async def get_elevation(self, lat: float, lon: float) -> float:
        """Get elevation (m) at a single lat/lon point."""
        tile_lat, tile_lon = self._tile_for_coord(lat, lon)
        tile = await self._load_tile(tile_lat, tile_lon)

        if tile.data is not None:
            return tile.get_elevation(lat, lon)

        # Fallback: OpenTopoData cloud API
        return await self._cloud_elevation(lat, lon)

    async def _cloud_elevation(self, lat: float, lon: float) -> float:
        """Query OpenTopoData API for elevation."""
        try:
            session = await self._get_session()
            url = f"{OPENTOPODATA_API}?locations={lat:.6f},{lon:.6f}"
            async with session.get(url) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    results = data.get("results", [])
                    if results:
                        return float(results[0].get("elevation", 0.0) or 0.0)
        except Exception as e:
            log.warning(f"Cloud elevation API failed: {e}")
        return 0.0

    async def get_elevation_profile(self, lat1: float, lon1: float,
                                     lat2: float, lon2: float,
                                     num_points: int = 512) -> tuple[np.ndarray, np.ndarray]:
        """
        Get terrain elevation profile along great-circle path.
        Returns (distances_m, elevations_m).
        GPU-accelerated coordinate generation when available.
        """
        # Generate lat/lon points along great circle
        lats, lons = self._great_circle_points(lat1, lon1, lat2, lon2, num_points)

        # Load required tiles
        tile_keys = set()
        for la, lo in zip(lats, lons):
            tile_keys.add(self._tile_for_coord(la, lo))

        # Download tiles concurrently
        tasks = [self._load_tile(tl, tlo) for tl, tlo in tile_keys]
        await asyncio.gather(*tasks)

        # Sample elevations
        elevations = np.zeros(num_points, dtype=np.float32)
        for i, (la, lo) in enumerate(zip(lats, lons)):
            tile_lat, tile_lon = self._tile_for_coord(la, lo)
            key = self._tile_key(tile_lat, tile_lon)
            tile = self._cache.get(key)
            if tile and tile.data is not None:
                elevations[i] = tile.get_elevation(la, lo)

        # Any zeros where cloud API should fill in
        zero_mask = elevations == 0
        if zero_mask.sum() > 0 and zero_mask.sum() < num_points:
            # Interpolate from neighbors rather than API call for speed
            from scipy.interpolate import interp1d
            x = np.arange(num_points)
            valid = ~zero_mask
            if valid.sum() >= 2:
                f = interp1d(x[valid], elevations[valid], fill_value="extrapolate")
                elevations[zero_mask] = f(x[zero_mask])

        # Distance array
        total_dist = haversine_distance(lat1, lon1, lat2, lon2)
        distances = np.linspace(0, total_dist, num_points)

        return distances, elevations

    @staticmethod
    def _great_circle_points(lat1: float, lon1: float,
                              lat2: float, lon2: float,
                              n: int) -> tuple[list, list]:
        """Interpolate n points along great circle between two coords."""
        # Convert to radians
        la1, lo1 = math.radians(lat1), math.radians(lon1)
        la2, lo2 = math.radians(lat2), math.radians(lon2)

        # Angular distance
        d = math.acos(max(-1.0, min(1.0,
            math.sin(la1) * math.sin(la2) +
            math.cos(la1) * math.cos(la2) * math.cos(lo2 - lo1))))

        lats, lons = [], []
        for i in range(n):
            if d < 1e-10:
                lats.append(lat1)
                lons.append(lon1)
                continue
            f = i / (n - 1)
            A = math.sin((1 - f) * d) / math.sin(d)
            B = math.sin(f * d) / math.sin(d)
            x = A * math.cos(la1) * math.cos(lo1) + B * math.cos(la2) * math.cos(lo2)
            y = A * math.cos(la1) * math.sin(lo1) + B * math.cos(la2) * math.sin(lo2)
            z = A * math.sin(la1) + B * math.sin(la2)
            lat_i = math.degrees(math.atan2(z, math.sqrt(x ** 2 + y ** 2)))
            lon_i = math.degrees(math.atan2(y, x))
            lats.append(lat_i)
            lons.append(lon_i)

        return lats, lons

    async def get_elevation_grid(
        self, lat: float, lon: float,
        radius_km: float = 10.0, grid_size: int = 30
    ) -> dict:
        """
        Return a 2D grid of elevations around (lat, lon) for 3D terrain rendering.
        Returns {lats: [...], lons: [...], elevations: [[row0], [row1], ...]}
        """
        step_lat = (2 * radius_km / 111.32) / max(1, grid_size - 1)
        step_lon = (2 * radius_km / (111.32 * max(0.001, math.cos(math.radians(lat))))) / max(1, grid_size - 1)
        lats = [lat - radius_km / 111.32 + i * step_lat for i in range(grid_size)]
        lons = [lon - radius_km / (111.32 * max(0.001, math.cos(math.radians(lat)))) + j * step_lon
                for j in range(grid_size)]

        # Fetch elevation profiles along each latitude row (lon sweep)
        async def fetch_row(row_lat: float) -> list[float]:
            lat_a, lon_a = row_lat, lons[0]
            lat_b, lon_b = row_lat, lons[-1]
            try:
                _, elevs = await self.get_elevation_profile(lat_a, lon_a, lat_b, lon_b, grid_size)
                return [float(e) for e in elevs]
            except Exception:
                return [0.0] * grid_size

        rows = await asyncio.gather(*[fetch_row(r) for r in lats])
        return {
            "lats": lats,
            "lons": lons,
            "elevations": [list(r) for r in rows],
        }

    async def get_buildings(self, lat: float, lon: float,
                            radius_m: float = 500.0) -> list[dict]:
        """
        Fetch building footprints from OpenStreetMap via Overpass API.
        Returns list of buildings with height and material properties.
        """
        # Convert radius to degrees (approximate)
        radius_deg = radius_m / 111320.0
        bbox = (lat - radius_deg, lon - radius_deg,
                lat + radius_deg, lon + radius_deg)

        query = f"""
        [out:json][timeout:25];
        (
          way["building"]({bbox[0]:.6f},{bbox[1]:.6f},{bbox[2]:.6f},{bbox[3]:.6f});
          relation["building"]({bbox[0]:.6f},{bbox[1]:.6f},{bbox[2]:.6f},{bbox[3]:.6f});
        );
        out body;
        >;
        out skel qt;
        """

        cache_key = hashlib.md5(f"{lat:.4f}{lon:.4f}{radius_m}".encode()).hexdigest()
        cache_file = BUILDINGS_CACHE_DIR / f"{cache_key}.json"

        if cache_file.exists():
            async with aiofiles.open(cache_file, "r") as f:
                import json
                return json.loads(await f.read())

        # Then an installed building pack (data/packs/buildings/<id>/buildings.geojson) —
        # works offline and avoids hammering Overpass.
        from_packs = _buildings_from_packs(bbox)
        if from_packs is not None:
            log.debug(f"Using {len(from_packs)} building(s) from offline pack for ({lat:.4f},{lon:.4f})")
            return from_packs

        buildings = []
        try:
            session = await self._get_session()
            async with session.post(OVERPASS_API, data={"data": query}) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    buildings = self._parse_osm_buildings(data)
                    # Cache result
                    import json
                    async with aiofiles.open(cache_file, "w") as f:
                        await f.write(json.dumps(buildings))
        except Exception as e:
            log.warning(f"OSM building fetch failed: {e}")

        return buildings

    @staticmethod
    def _parse_osm_buildings(osm_data: dict) -> list[dict]:
        """Parse OSM data into building list with heights."""
        buildings = []
        nodes = {}
        for el in osm_data.get("elements", []):
            if el["type"] == "node":
                nodes[el["id"]] = (el["lat"], el["lon"])

        for el in osm_data.get("elements", []):
            if el["type"] == "way" and "tags" in el:
                tags = el["tags"]
                if "building" not in tags:
                    continue
                # Height: use height tag or levels * 3m
                height = 10.0  # default
                if "height" in tags:
                    try:
                        height = float(tags["height"].replace("m", "").strip())
                    except ValueError:
                        pass
                elif "building:levels" in tags:
                    try:
                        height = float(tags["building:levels"]) * 3.0
                    except ValueError:
                        pass

                # Material for RF attenuation
                material = tags.get("building:material", "concrete")
                rf_loss = _material_rf_loss(material)

                # Get centroid and polygon coordinates
                node_ids = el.get("nodes", [])
                if node_ids:
                    coords = [nodes[nid] for nid in node_ids if nid in nodes]
                    if coords:
                        clat = sum(c[0] for c in coords) / len(coords)
                        clon = sum(c[1] for c in coords) / len(coords)
                        buildings.append({
                            "lat": clat, "lon": clon,
                            "height_m": height,
                            "material": material,
                            "rf_loss_db": rf_loss,
                            # Polygon ring as [[lat, lon], ...] for GeoJSON/map rendering
                            "polygon": [[c[0], c[1]] for c in coords],
                        })
        return buildings

    def buildings_to_geojson(self, buildings: list[dict]) -> dict:
        """Convert building list to GeoJSON FeatureCollection for map overlay."""
        features = []
        for b in buildings:
            polygon = b.get("polygon", [])
            if len(polygon) < 3:
                continue
            # GeoJSON uses [lon, lat] order
            ring = [[pt[1], pt[0]] for pt in polygon]
            if ring[0] != ring[-1]:
                ring.append(ring[0])
            features.append({
                "type": "Feature",
                "geometry": {"type": "Polygon", "coordinates": [ring]},
                "properties": {
                    "height_m": b["height_m"],
                    "material": b["material"],
                    "rf_loss_db": b["rf_loss_db"],
                },
            })
        return {"type": "FeatureCollection", "features": features}


def _material_rf_loss(material: str) -> float:
    """RF penetration loss through building materials (dB, 2.4 GHz baseline)."""
    losses = {
        "glass": 2.0,
        "wood": 4.0,
        "brick": 8.0,
        "concrete": 15.0,
        "reinforced_concrete": 20.0,
        "metal": 30.0,
        "stone": 12.0,
        "steel": 25.0,
    }
    material_lower = material.lower()
    for key, val in losses.items():
        if key in material_lower:
            return val
    return 12.0  # default: concrete-like


def haversine_distance(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Haversine distance between two points (metres)."""
    R = EARTH_RADIUS_M
    la1, lo1 = math.radians(lat1), math.radians(lon1)
    la2, lo2 = math.radians(lat2), math.radians(lon2)
    dLat = la2 - la1
    dLon = lo2 - lo1
    a = math.sin(dLat / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin(dLon / 2) ** 2
    return 2 * R * math.asin(math.sqrt(max(0.0, a)))


def destination_point(lat: float, lon: float,
                      bearing_deg: float, distance_m: float) -> tuple[float, float]:
    """
    Compute destination lat/lon given origin, bearing (deg), and distance (m).
    Vincenty spherical approximation.
    """
    R = EARTH_RADIUS_M
    d = distance_m / R
    brg = math.radians(bearing_deg)
    la1 = math.radians(lat)
    lo1 = math.radians(lon)

    la2 = math.asin(math.sin(la1) * math.cos(d) +
                    math.cos(la1) * math.sin(d) * math.cos(brg))
    lo2 = lo1 + math.atan2(math.sin(brg) * math.sin(d) * math.cos(la1),
                            math.cos(d) - math.sin(la1) * math.sin(la2))
    return math.degrees(la2), math.degrees(lo2)
