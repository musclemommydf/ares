# SPDX-License-Identifier: MIT OR Apache-2.0
# Copyright (c) 2026 Ares

"""
Ares — offline data-pack builder (Workstream A.2).

Downloads data into ``data/packs/<layer>/<id>/`` and registers a manifest, with
progress reported on the job record from :mod:`app.core.packs`.

Implemented:
  - **terrain**: SRTM 1-arc-second (~30 m) ``.hgt`` tiles for a bbox (or the whole
    planet) from the AWS open elevation-tiles bucket. Stored decompressed; the
    3D globe consumes them directly via the heightmap endpoint
    (``GET /api/v1/terrain/heightmap/<id>`` → Cesium CustomHeightmapTerrainProvider).
  - **osm**: XYZ raster tiles for a bbox + zoom range from a configurable tile
    server (default OSM). **Bulk tile downloading is rate-limited and capped**;
    point ``source`` at your own tile server for anything large (OSM's tile
    policy forbids heavy bulk use).
  - **imagery**: the same XYZ-raster path against a satellite/aerial tile source
    (default ESRI World Imagery) — this is the "AO imagery pack" the globe uses
    as an offline `ImageryLayer`. Same rate-limit / cap caveat: for a large AO,
    point ``source`` at your own / a licensed tile server.
  - **buildings**: OSM building footprints (ways) via Overpass → a GeoJSON file
    the 3D globe extrudes.

Not yet: clutter / land-cover rasters; the provider-chain refactor that *grows*
a terrain/imagery pack from online fetches on a connected box — follow-up work.
"""
from __future__ import annotations

import asyncio
import gzip
import json
import logging
import math
import shutil
import time
from pathlib import Path
from typing import Iterable, Optional

import aiohttp

from app.config import PACKS_DIR
from app.core.packs import PackManifest, register_pack

log = logging.getLogger(__name__)

SRTM1_SKADI = "https://s3.amazonaws.com/elevation-tiles-prod/skadi/{band}/{name}.hgt.gz"
SRTM1_BYTES_PER_TILE = 25_934_402  # 3601*3601*2 bytes, uncompressed
DEFAULT_OSM_TILES = "https://tile.openstreetmap.org/{z}/{x}/{y}.png"
# ESRI World Imagery — satellite/aerial XYZ (note {z}/{y}/{x} ordering; we format
# with named placeholders so the order in the template doesn't matter). For a
# large AO or sustained use, point `source` at your own / a licensed tile server.
DEFAULT_IMAGERY_TILES = "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}"
OSM_TILE_BYTES_EST = 20_000
IMAGERY_TILE_BYTES_EST = 18_000
MAX_OSM_TILES = 200_000          # refuse runaway jobs against public servers
OSM_REQ_DELAY_S = 0.12          # be polite (~8 req/s) when hitting a public tile server
_DISK_HEADROOM = 2 * 1024**3    # keep 2 GB free

DEFAULT_OVERPASS = "https://overpass-api.de/api/interpreter"
BUILDINGS_CELL_DEG = 0.05        # query Overpass in 0.05° cells and merge
MAX_BUILDINGS_CELLS = 600        # ~1.7° × 1° max in one job (Overpass is a shared resource)
OVERPASS_DELAY_S = 1.0          # be polite to the public Overpass instance

# ESA WorldCover 10 m land-cover — AWS open-data bucket, 3°×3° GeoTIFF tiles, no auth.
WORLDCOVER_URL = "https://esa-worldcover.s3.eu-central-1.amazonaws.com/v200/2021/map/ESA_WorldCover_10m_2021_v200_{tile}_Map.tif"
WORLDCOVER_BYTES_PER_TILE = 130 * 1024**2   # ~80–180 MB per 3° tile; planning estimate
MAX_WORLDCOVER_TILES = 64        # 64 × 3° tiles ≈ a generous regional AO; bigger ⇒ bring your own
WORLDCOVER_CLASSES = ("10=tree 20=shrub 30=grassland 40=cropland 50=built-up 60=bare/sparse "
                      "70=snow/ice 80=permanent-water 90=herbaceous-wetland 95=mangroves 100=moss/lichen")


# ── helpers ──────────────────────────────────────────────────────────────────
def _clip_bbox(bbox: Optional[list[float]]) -> tuple[float, float, float, float]:
    if not bbox:
        return (-180.0, -56.0, 180.0, 60.0)  # SRTM land coverage as the "whole planet" default
    w, s, e, n = bbox
    return (max(-180.0, w), max(-90.0, s), min(180.0, e), min(90.0, n))


def _iter_deg_tiles(bbox) -> list[tuple[int, int]]:
    w, s, e, n = bbox
    out = []
    for lat in range(math.floor(s), math.ceil(n)):
        for lon in range(math.floor(w), math.ceil(e)):
            out.append((lat, lon))
    return out


def _hgt_name(lat: int, lon: int) -> str:
    return f"{'N' if lat >= 0 else 'S'}{abs(lat):02d}{'E' if lon >= 0 else 'W'}{abs(lon):03d}"


def _lonlat_to_tile(lon: float, lat: float, z: int) -> tuple[int, int]:
    n = 2 ** z
    x = int((lon + 180.0) / 360.0 * n)
    lat_r = math.radians(max(-85.05112878, min(85.05112878, lat)))
    y = int((1.0 - math.asinh(math.tan(lat_r)) / math.pi) / 2.0 * n)
    return max(0, min(n - 1, x)), max(0, min(n - 1, y))


def _check_disk(need_bytes: int, where: Path) -> None:
    try:
        free = shutil.disk_usage(where).free
    except OSError:
        return
    if free < need_bytes + _DISK_HEADROOM:
        raise RuntimeError(f"insufficient disk: need ~{need_bytes/1e9:.1f} GB + headroom, "
                           f"have {free/1e9:.1f} GB free at {where}")


def estimate_bytes(layers: list[str], bbox: Optional[list[float]], max_zoom: Optional[int] = None) -> dict:
    """Estimate the download size, per layer, for a pack request — *without* fetching anything.
    Drives the "Get download estimate" button in the Layer Manager. Returns one entry per layer:
    ``{tiles, bytes, note}``. Counts are exact (the same enumeration the builders use); per-tile
    byte estimates are the same constants the builders use to plan disk checks."""
    bb = _clip_bbox(bbox)
    w, s, e, n = bb
    out: dict = {}
    for layer in layers:
        if layer == "terrain":
            tiles = _iter_deg_tiles(bb)
            out[layer] = {
                "tiles": len(tiles), "bytes": len(tiles) * SRTM1_BYTES_PER_TILE,
                "note": f"SRTM 30 m · 1° × 1° tiles · ~{SRTM1_BYTES_PER_TILE / 1e6:.0f} MB each (uncompressed)",
            }
        elif layer in ("osm", "imagery"):
            zmax = int(max_zoom or (12 if layer == "osm" else 15))
            n_tiles = 0
            for z in range(0, zmax + 1):
                x0, y0 = _lonlat_to_tile(w, n, z)
                x1, y1 = _lonlat_to_tile(e, s, z)
                n_tiles += (abs(x1 - x0) + 1) * (abs(y1 - y0) + 1)
                if n_tiles > MAX_OSM_TILES:
                    out[layer] = {
                        "tiles": MAX_OSM_TILES, "bytes": MAX_OSM_TILES * (OSM_TILE_BYTES_EST if layer == "osm" else IMAGERY_TILE_BYTES_EST),
                        "exceeds_cap": True, "max_zoom": zmax,
                        "note": f"would exceed the {MAX_OSM_TILES:,}-tile cap at z{zmax} — shrink the bbox or the max zoom",
                    }
                    break
            else:
                per = OSM_TILE_BYTES_EST if layer == "osm" else IMAGERY_TILE_BYTES_EST
                out[layer] = {"tiles": int(n_tiles), "bytes": int(n_tiles) * per, "max_zoom": zmax,
                              "note": f"XYZ tiles z0–z{zmax} · ~{per / 1024:.0f} kB each"}
        elif layer == "clutter":
            lat0 = math.floor(s / 3.0) * 3; lon0 = math.floor(w / 3.0) * 3
            count = 0
            for la in range(lat0, math.ceil(n / 3.0) * 3, 3):
                for lo in range(lon0, math.ceil(e / 3.0) * 3, 3):
                    count += 1
            exceeds = count > MAX_WORLDCOVER_TILES
            out[layer] = {"tiles": count, "bytes": count * WORLDCOVER_BYTES_PER_TILE, "exceeds_cap": exceeds,
                          "note": f"ESA WorldCover 10 m · 3° × 3° GeoTIFFs · ~{WORLDCOVER_BYTES_PER_TILE / 1e6:.0f} MB each" +
                                  (f" — would exceed the {MAX_WORLDCOVER_TILES}-tile cap; shrink the bbox" if exceeds else "")}
        elif layer == "buildings":
            cells = 0
            for cs in _frange(s, n, BUILDINGS_CELL_DEG):
                for cw in _frange(w, e, BUILDINGS_CELL_DEG):
                    cells += 1
            exceeds = cells > MAX_BUILDINGS_CELLS
            # buildings: heavily data-dependent (urban ≫ rural). Use a wide planning estimate.
            avg_per_cell = 250 * 1024   # ~250 kB / 0.05° cell on average — urban can be 10× more
            out[layer] = {"tiles": cells, "bytes": cells * avg_per_cell, "exceeds_cap": exceeds,
                          "note": "OSM buildings (Overpass) · highly variable: urban cells can be 10× this, rural near zero" +
                                  (f" — would exceed the {MAX_BUILDINGS_CELLS}-cell cap; shrink the bbox" if exceeds else "")}
        else:
            out[layer] = {"tiles": 0, "bytes": 0, "note": f"no estimator for layer {layer!r}"}
    total = sum(v.get("bytes", 0) for v in out.values())
    return {"per_layer": out, "total_bytes": total, "bbox": list(bb)}


def _fail(job: dict, msg: str) -> dict:
    job.update(status="error", detail=msg, finished=time.time())
    log.warning("pack job %s failed: %s", job.get("job_id"), msg)
    return job


# ── terrain ──────────────────────────────────────────────────────────────────
async def build_terrain_pack(job: dict) -> dict:
    bbox = _clip_bbox(job.get("bbox"))
    tiles = _iter_deg_tiles(bbox)
    if not tiles:
        return _fail(job, "empty bbox")
    pack_id = f"terrain-srtm30-{int(time.time())}"
    out_dir = PACKS_DIR / "terrain" / pack_id
    out_dir.mkdir(parents=True, exist_ok=True)
    try:
        _check_disk(SRTM1_BYTES_PER_TILE * len(tiles), out_dir)
    except RuntimeError as e:
        shutil.rmtree(out_dir, ignore_errors=True)
        return _fail(job, str(e))

    job.update(status="running", progress=0.0, total=len(tiles), done=0,
               detail=f"downloading {len(tiles)} SRTM30 tile(s)")
    ok = 0
    timeout = aiohttp.ClientTimeout(total=60, connect=15)
    sem = asyncio.Semaphore(8)
    async with aiohttp.ClientSession(timeout=timeout) as sess:
        async def one(lat: int, lon: int):
            nonlocal ok
            name = _hgt_name(lat, lon)
            dst = out_dir / f"{name}.hgt"
            if dst.exists():
                ok += 1
                return
            url = SRTM1_SKADI.format(band=name[:3], name=name)
            async with sem:
                try:
                    async with sess.get(url) as r:
                        if r.status == 200:
                            raw = await r.read()
                            dst.write_bytes(gzip.decompress(raw))
                            ok += 1
                        # 403/404 ⇒ ocean tile; just skip silently
                except Exception:
                    pass
            job["done"] = job.get("done", 0) + 1
            job["progress"] = round(job["done"] / job["total"], 4)

        # chunk so progress updates flow and we don't open thousands of tasks at once
        for i in range(0, len(tiles), 64):
            await asyncio.gather(*(one(la, lo) for la, lo in tiles[i:i + 64]))

    if ok == 0:
        shutil.rmtree(out_dir, ignore_errors=True)
        return _fail(job, "no terrain tiles downloaded (offline, or bbox is all ocean)")
    manifest = register_pack(PackManifest(
        id=pack_id, layer="terrain", name=f"SRTM 30 m — bbox {bbox}", source="srtm30-skadi",
        format="hgt", bbox=list(bbox), resolution_m=30,
        extra={"n_tiles": ok, "cesium_ready": True,
               "note": "served to the 3D globe via /api/v1/terrain/heightmap/<id> (Cesium CustomHeightmapTerrainProvider)"},
    ))
    job.update(status="done", progress=1.0, finished=time.time(),
               detail=f"terrain pack {pack_id}: {ok} tile(s)", pack=manifest)
    return job


# ── XYZ raster tiles (osm base map / satellite imagery) ──────────────────────
async def _build_xyz_pack(job: dict, *, layer: str, default_source: str,
                          default_zmax: int, ext: str, label: str,
                          tile_bytes_est: int) -> dict:
    """Download an XYZ raster tile pyramid (z0..zmax over the bbox) into
    ``data/packs/<layer>/<id>/{z}/{x}/{y}.<ext>`` and register a manifest the
    Cesium globe consumes as a `UrlTemplateImageryProvider`. Shared by the
    ``osm`` (base map) and ``imagery`` (satellite/aerial) layers."""
    bbox = _clip_bbox(job.get("bbox"))
    zmax = job.get("max_zoom") or default_zmax
    zmin = 0
    src = job.get("source") or default_source
    tiles: list[tuple[int, int, int]] = []
    for z in range(zmin, zmax + 1):
        x0, y0 = _lonlat_to_tile(bbox[0], bbox[3], z)  # NW
        x1, y1 = _lonlat_to_tile(bbox[2], bbox[1], z)  # SE
        for x in range(min(x0, x1), max(x0, x1) + 1):
            for y in range(min(y0, y1), max(y0, y1) + 1):
                tiles.append((z, x, y))
                if len(tiles) > MAX_OSM_TILES:
                    return _fail(job, f"refusing {layer} pack: > {MAX_OSM_TILES} tiles — "
                                      "shrink the bbox / max_zoom, or point `source` at your own tile server")
    if not tiles:
        return _fail(job, "empty bbox")
    pack_id = f"{layer}-z{zmax}-{int(time.time())}"
    out_dir = PACKS_DIR / layer / pack_id
    out_dir.mkdir(parents=True, exist_ok=True)
    try:
        _check_disk(tile_bytes_est * len(tiles), out_dir)
    except RuntimeError as e:
        shutil.rmtree(out_dir, ignore_errors=True)
        return _fail(job, str(e))

    job.update(status="running", progress=0.0, total=len(tiles), done=0,
               detail=f"downloading {len(tiles)} {label} tile(s) z0–{zmax} (rate-limited)")
    ok = 0
    timeout = aiohttp.ClientTimeout(total=30, connect=10)
    headers = {"User-Agent": "ares-pack-builder/0.1 (+https://github.com/)"}
    async with aiohttp.ClientSession(timeout=timeout, headers=headers) as sess:
        for (z, x, y) in tiles:
            dst = out_dir / str(z) / str(x) / f"{y}.{ext}"
            if not dst.exists():
                dst.parent.mkdir(parents=True, exist_ok=True)
                url = src.format(z=z, x=x, y=y)
                try:
                    async with sess.get(url) as r:
                        if r.status == 200:
                            dst.write_bytes(await r.read())
                            ok += 1
                except Exception:
                    pass
                await asyncio.sleep(OSM_REQ_DELAY_S)
            else:
                ok += 1
            job["done"] += 1
            job["progress"] = round(job["done"] / job["total"], 4)

    if ok == 0:
        shutil.rmtree(out_dir, ignore_errors=True)
        return _fail(job, f"no {label} tiles downloaded (offline?)")
    manifest = register_pack(PackManifest(
        id=pack_id, layer=layer, name=f"{label} raster z0–{zmax} — bbox {bbox}", source=src,
        format="xyz", bbox=list(bbox), zoom_min=zmin, zoom_max=zmax,
        extra={"n_tiles": ok, "tile_template": f"{{z}}/{{x}}/{{y}}.{ext}"},
    ))
    job.update(status="done", progress=1.0, finished=time.time(),
               detail=f"{layer} pack {pack_id}: {ok} tile(s)", pack=manifest)
    return job


async def build_osm_pack(job: dict) -> dict:
    return await _build_xyz_pack(job, layer="osm", default_source=DEFAULT_OSM_TILES,
                                 default_zmax=12, ext="png", label="OSM",
                                 tile_bytes_est=OSM_TILE_BYTES_EST)


async def build_imagery_pack(job: dict) -> dict:
    return await _build_xyz_pack(job, layer="imagery", default_source=DEFAULT_IMAGERY_TILES,
                                 default_zmax=15, ext="jpg", label="imagery",
                                 tile_bytes_est=IMAGERY_TILE_BYTES_EST)


# ── building footprints (OSM via Overpass) ───────────────────────────────────
def _frange(a: float, b: float, step: float) -> list[float]:
    out, x = [], a
    while x < b - 1e-9:
        out.append(x); x += step
    return out


async def build_buildings_pack(job: dict) -> dict:
    bbox = _clip_bbox(job.get("bbox"))
    w, s, e, n = bbox
    overpass = job.get("source") or DEFAULT_OVERPASS
    cells = [(cs, cw, min(n, cs + BUILDINGS_CELL_DEG), min(e, cw + BUILDINGS_CELL_DEG))
             for cs in _frange(s, n, BUILDINGS_CELL_DEG) for cw in _frange(w, e, BUILDINGS_CELL_DEG)]
    if not cells:
        return _fail(job, "empty bbox")
    if len(cells) > MAX_BUILDINGS_CELLS:
        return _fail(job, f"refusing buildings pack: {len(cells)} Overpass cells > {MAX_BUILDINGS_CELLS} "
                          "— shrink the bbox or point `source` at your own Overpass instance")
    pack_id = f"buildings-osm-{int(time.time())}"
    out_dir = PACKS_DIR / "buildings" / pack_id
    out_dir.mkdir(parents=True, exist_ok=True)
    job.update(status="running", progress=0.0, total=len(cells), done=0,
               detail=f"querying OSM buildings in {len(cells)} cell(s)")

    features: list[dict] = []
    seen_ids: set[int] = set()
    timeout = aiohttp.ClientTimeout(total=90, connect=15)
    headers = {"User-Agent": "ares-pack-builder/0.1"}
    async with aiohttp.ClientSession(timeout=timeout, headers=headers) as sess:
        for (cs, cw, cn, ce) in cells:
            q = (f"[out:json][timeout:25];(way[\"building\"]({cs:.5f},{cw:.5f},{cn:.5f},{ce:.5f});"
                 f"relation[\"building\"]({cs:.5f},{cw:.5f},{cn:.5f},{ce:.5f}););out geom tags;")
            try:
                async with sess.post(overpass, data={"data": q}) as r:
                    if r.status == 200:
                        data = await r.json()
                        for el in data.get("elements", []):
                            if el.get("type") != "way" or el.get("id") in seen_ids:
                                continue  # relations (multipolygons) skipped for now
                            geom = el.get("geometry") or []
                            if len(geom) < 3:
                                continue
                            seen_ids.add(el["id"])
                            ring = [[g["lon"], g["lat"]] for g in geom]
                            if ring[0] != ring[-1]:
                                ring.append(ring[0])
                            tags = el.get("tags", {})
                            height = None
                            try:
                                if "height" in tags:
                                    height = float(str(tags["height"]).split()[0])
                                elif "building:levels" in tags:
                                    height = float(tags["building:levels"]) * 3.0
                            except (ValueError, IndexError):
                                pass
                            features.append({
                                "type": "Feature",
                                "geometry": {"type": "Polygon", "coordinates": [ring]},
                                "properties": {"osm_id": el["id"], "height_m": height,
                                               "building": tags.get("building"),
                                               "material": tags.get("building:material")},
                            })
            except Exception:
                pass
            job["done"] += 1
            job["progress"] = round(job["done"] / job["total"], 4)
            await asyncio.sleep(OVERPASS_DELAY_S)

    fc = {"type": "FeatureCollection", "features": features}
    (out_dir / "buildings.geojson").write_text(json.dumps(fc))
    manifest = register_pack(PackManifest(
        id=pack_id, layer="buildings", name=f"OSM buildings — bbox {bbox}", source=overpass,
        format="geojson", bbox=list(bbox),
        extra={"n_buildings": len(features), "file": "buildings.geojson",
               "note": "ways only; multipolygon relations skipped"},
    ))
    job.update(status="done", progress=1.0, finished=time.time(),
               detail=f"buildings pack {pack_id}: {len(features)} footprint(s)", pack=manifest)
    return job


# ── clutter / land cover (ESA WorldCover 10 m) ───────────────────────────────
def _wc_tile_name(lat3: int, lon3: int) -> str:
    return f"{'N' if lat3 >= 0 else 'S'}{abs(lat3):02d}{'E' if lon3 >= 0 else 'W'}{abs(lon3):03d}"


async def build_clutter_pack(job: dict) -> dict:
    w, s, e, n = _clip_bbox(job.get("bbox"))
    # WorldCover tiles are on a 3° grid keyed by SW corner
    lat0 = math.floor(s / 3.0) * 3
    lon0 = math.floor(w / 3.0) * 3
    tiles = [(la, lo) for la in range(lat0, math.ceil(n / 3.0) * 3, 3)
             for lo in range(lon0, math.ceil(e / 3.0) * 3, 3)]
    if not tiles:
        return _fail(job, "empty bbox")
    if len(tiles) > MAX_WORLDCOVER_TILES:
        return _fail(job, f"refusing clutter pack: {len(tiles)} × 3° WorldCover tiles > {MAX_WORLDCOVER_TILES} — shrink the bbox")
    pack_id = f"clutter-worldcover-{int(time.time())}"
    out_dir = PACKS_DIR / "clutter" / pack_id
    out_dir.mkdir(parents=True, exist_ok=True)
    try:
        _check_disk(WORLDCOVER_BYTES_PER_TILE * len(tiles), out_dir)
    except RuntimeError as ex:
        shutil.rmtree(out_dir, ignore_errors=True)
        return _fail(job, str(ex))

    job.update(status="running", progress=0.0, total=len(tiles), done=0,
               detail=f"downloading {len(tiles)} ESA WorldCover 10 m tile(s)")
    ok = 0
    timeout = aiohttp.ClientTimeout(total=600, connect=20)
    headers = {"User-Agent": "ares-pack-builder/0.1"}
    async with aiohttp.ClientSession(timeout=timeout, headers=headers) as sess:
        for (la, lo) in tiles:
            name = _wc_tile_name(la, lo)
            dst = out_dir / f"{name}.tif"
            if not dst.exists():
                url = WORLDCOVER_URL.format(tile=name)
                try:
                    async with sess.get(url) as r:
                        if r.status == 200:
                            dst.write_bytes(await r.read())
                            ok += 1
                        # 403/404 ⇒ ocean tile (WorldCover has land tiles only); skip
                except Exception:
                    pass
            else:
                ok += 1
            job["done"] += 1
            job["progress"] = round(job["done"] / job["total"], 4)

    if ok == 0:
        shutil.rmtree(out_dir, ignore_errors=True)
        return _fail(job, "no WorldCover tiles downloaded (offline, or bbox is all ocean)")
    manifest = register_pack(PackManifest(
        id=pack_id, layer="clutter", name=f"ESA WorldCover 10 m — bbox {(w, s, e, n)}",
        source="esa-worldcover-v200", format="geotiff", bbox=[w, s, e, n], resolution_m=10,
        extra={"n_tiles": ok, "year": 2021, "classes": WORLDCOVER_CLASSES,
               "note": "3°×3° land-cover GeoTIFFs; a future clutter loader can map classes → attenuation/height"},
    ))
    job.update(status="done", progress=1.0, finished=time.time(),
               detail=f"clutter pack {pack_id}: {ok} tile(s)", pack=manifest)
    return job


# ── dispatch ─────────────────────────────────────────────────────────────────
async def run_job(job: dict) -> None:
    """Execute a pack-download job (in-process background task)."""
    try:
        layers = job.get("layers") or []
        if "terrain" in layers:
            await build_terrain_pack(job)
        if "osm" in layers and job.get("status") != "error":
            await build_osm_pack(job)
        if "imagery" in layers and job.get("status") != "error":
            await build_imagery_pack(job)
        if "buildings" in layers and job.get("status") != "error":
            await build_buildings_pack(job)
        if "clutter" in layers and job.get("status") != "error":
            await build_clutter_pack(job)
        for lyr in layers:
            if lyr not in ("terrain", "osm", "imagery", "buildings", "clutter") and job.get("status") != "error":
                job.setdefault("skipped", []).append(lyr)
        if job.get("status") == "not_implemented":
            job["status"] = "done" if job.get("done") else "error"
            job.setdefault("detail", "nothing built for the requested layers")
    except Exception as e:  # pragma: no cover
        _fail(job, f"unhandled: {type(e).__name__}: {e}")
