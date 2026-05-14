"""
Ares — system, data-pack & network-state routes (Workstream A).

GET    /api/v1/server/info             server identity, GPU, installed packs, online/offline, disk
GET    /api/v1/packs                   list installed offline data packs (optionally ?layer=terrain)
GET    /api/v1/packs/jobs              list pack-download jobs
GET    /api/v1/packs/jobs/{job_id}     one pack-download job
GET    /api/v1/packs/{pack_id}         one pack's manifest
DELETE /api/v1/packs/{pack_id}         remove a pack
POST   /api/v1/packs/download          download a pack (terrain / osm / imagery / buildings; runs in background)
GET    /api/v1/net/status              online/offline + last-known cloud data + overrides
PUT    /api/v1/net/override/{kind}     set an operator override for a cloud datum (e.g. space_weather)
DELETE /api/v1/net/override/{kind}     clear it
"""
from __future__ import annotations

import asyncio
import shutil
import time
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel

from app.config import settings, DATA_DIR, PACK_LAYERS
from app.core import packs as packs_mod
from app.core import net_state
from app.core.auth import require_auth
from app.core.security import audit

router = APIRouter(tags=["system"])


def _gpu_info() -> dict:
    try:
        import cupy  # type: ignore
        n = int(cupy.cuda.runtime.getDeviceCount())
        if n <= 0:
            return {"available": False, "backend": "cupy", "devices": 0}
        names = []
        for i in range(n):
            try:
                names.append(cupy.cuda.runtime.getDeviceProperties(i)["name"].decode())
            except Exception:
                names.append(f"device{i}")
        return {"available": True, "backend": "cupy", "devices": n, "names": names}
    except Exception:
        return {"available": False, "backend": None, "devices": 0}


# ── /server/info ─────────────────────────────────────────────────────────────
@router.get("/server/info")
async def server_info(principal: dict = Depends(require_auth)):
    # Build defensively — a busted pack manifest / disk hiccup must never 500 this.
    pack_counts: dict[str, int] = {l: 0 for l in PACK_LAYERS}
    total_pack_bytes = 0
    try:
        for p in packs_mod.list_packs():
            pack_counts[p["layer"]] = pack_counts.get(p["layer"], 0) + 1
            total_pack_bytes += int(p.get("size_bytes_on_disk", 0) or 0)
    except Exception:  # pragma: no cover
        pass
    try:
        du = shutil.disk_usage(DATA_DIR)
        disk = {"total_bytes": du.total, "used_bytes": du.used, "free_bytes": du.free}
    except OSError:
        disk = None
    try:
        online = net_state.is_online()
    except Exception:
        online = None
    try:
        gpu = _gpu_info()
    except Exception:
        gpu = {"available": False, "backend": None, "devices": 0}
    sec_warn = None
    try:
        if (not settings.auth_enabled) and str(settings.host) not in ("127.0.0.1", "localhost", "::1"):
            sec_warn = ("AUTH IS OFF and the server is bound to a non-loopback address — anyone on the network "
                        "can use every endpoint, push CoT, manage SDR devices and read the mesh. Set ARES_AUTH=true "
                        "(and ARES_MESH_SECRET for multi-node) for any deployment that isn't localhost-only.")
    except Exception:
        pass
    try:
        from app.core import meshsec
        mesh_secret_set = bool(meshsec.secret())
    except Exception:
        mesh_secret_set = False
    return {
        "name": settings.app_name,
        "version": settings.app_version,
        "auth_enabled": settings.auth_enabled,
        "atak_enabled": settings.atak_enabled,
        "mesh_secret_set": mesh_secret_set,
        "security_warning": sec_warn,
        "network_policy": settings.network_policy,
        "online": online,
        "mode": ("offline" if online is False else "online" if online else "unknown"),
        "gpu": gpu,
        "packs": {"counts": pack_counts, "total_bytes_on_disk": total_pack_bytes},
        "data_dir": str(DATA_DIR),
        "disk": disk,
        "server_time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }


class AtakEnabled(BaseModel):
    enabled: bool


@router.post("/atak/enabled")
async def set_atak_enabled(body: AtakEnabled, principal: dict = Depends(require_auth)):
    """Master on/off for the ATAK / TAK-server integration (data packs, radio templates,
    KMZ export, CoT push). Off ⇒ those endpoints stay reachable for diagnostics but the
    web console hides the section. In-memory toggle (ARES_ATAK=false sets the default)."""
    settings.atak_enabled = bool(body.enabled)
    try:
        from app.core.security import audit
        audit("atak.enabled", enabled=settings.atak_enabled, by=principal.get("sub"))
    except Exception:
        pass
    return {"atak_enabled": settings.atak_enabled}


# ── /packs ───────────────────────────────────────────────────────────────────
@router.get("/packs")
async def list_packs(layer: Optional[str] = Query(None), principal: dict = Depends(require_auth)):
    if layer and layer not in PACK_LAYERS:
        raise HTTPException(400, f"unknown layer {layer!r}; expected one of {list(PACK_LAYERS)}")
    return {"layers": list(PACK_LAYERS), "packs": packs_mod.list_packs(layer)}


@router.get("/packs/jobs")
async def list_pack_jobs(principal: dict = Depends(require_auth)):
    return {"jobs": packs_mod.list_jobs()}


@router.get("/packs/jobs/{job_id}")
async def get_pack_job(job_id: str, principal: dict = Depends(require_auth)):
    job = packs_mod.get_job(job_id)
    if job is None:
        raise HTTPException(404, "no such job")
    return job


# Static file serving inside a pack — e.g. OSM raster tiles
# (GET /api/v1/packs/osm/active/{z}/{x}/{y}.png) and, later, quantized-mesh terrain
# (GET /api/v1/packs/terrain/active/layer.json + …/{z}/{x}/{y}.terrain). `pack_id`
# may be the literal `active` ⇒ most-recently-built pack of that layer.
@router.get("/packs/{layer}/{pack_id}/{file_path:path}")
async def pack_file(layer: str, pack_id: str, file_path: str, principal: dict = Depends(require_auth)):
    if layer not in PACK_LAYERS:
        raise HTTPException(404, f"unknown layer {layer!r}")
    p = packs_mod.pack_file_path(layer, pack_id, file_path)
    if p is None:
        raise HTTPException(404, "no such pack file")
    return FileResponse(str(p))


@router.get("/packs/{pack_id}")
async def get_pack(pack_id: str, principal: dict = Depends(require_auth)):
    pack = packs_mod.get_pack(pack_id)
    if pack is None:
        raise HTTPException(404, "no such pack")
    return pack


@router.post("/packs/{pack_id}/verify")
async def verify_pack(pack_id: str, deep: bool = Query(False, description="re-hash the pack and compare against the manifest checksum"),
                      principal: dict = Depends(require_auth)):
    res = packs_mod.verify_pack(pack_id, deep=deep)
    if res is None:
        raise HTTPException(404, "no such pack")
    return res


@router.delete("/packs/{pack_id}")
async def delete_pack(pack_id: str, principal: dict = Depends(require_auth)):
    if not packs_mod.delete_pack(pack_id):
        raise HTTPException(404, "no such pack")
    return {"status": "deleted", "id": pack_id}


class PackDownloadRequest(BaseModel):
    layers: list[str]                       # subset of PACK_LAYERS; terrain/osm/imagery/buildings have downloaders
    bbox: Optional[list[float]] = None      # [minlon, minlat, maxlon, maxlat]; null ⇒ "full planet"
    fidelity: str = "auto"                  # auto | best | <tier id>
    max_zoom: Optional[int] = None          # osm/imagery max zoom (default 12 osm / 15 imagery)
    source: Optional[str] = None            # explicit tile-server / Overpass / provider URL (recommended for large jobs)


def _kick_pack_jobs(jobs):
    """Start the in-process background tasks for queued pack jobs."""
    from app.core import pack_builder
    for j in jobs:
        if isinstance(j, dict) and j.get("status") == "queued":
            asyncio.create_task(pack_builder.run_job(j))


@router.post("/packs/download")
async def download_pack(req: PackDownloadRequest, principal: dict = Depends(require_auth)):
    try:
        job = packs_mod.start_download(layers=req.layers, bbox=req.bbox, fidelity=req.fidelity,
                                       max_zoom=req.max_zoom, source=req.source)
    except ValueError as e:
        raise HTTPException(400, str(e))
    _kick_pack_jobs([job])
    return job


# ── /regions — named state/country/region → bbox, and "download all data for it" ─────
@router.get("/regions")
async def list_regions(q: Optional[str] = Query(None, description="search (name / code / country)"),
                       limit: int = Query(40, ge=1, le=400)):
    """The catalogue of named admin regions (US states, European/other countries, and the giant
    countries split into sub-regions). With `q`, a fuzzy search; without it, the head of the list."""
    from app.core import regions
    return {"regions": regions.search(q, limit=limit) if q else regions.all_regions()[:limit],
            "total": len(regions.CATALOG)}


@router.get("/regions/at")
async def region_at(lat: float = Query(..., ge=-90, le=90), lon: float = Query(..., ge=-180, le=180)):
    """The smallest catalogued region whose bbox contains the point — for the map's right-click
    "download mapping data for this region"."""
    from app.core import regions
    r = regions.region_at(lat, lon)
    if r is None:
        raise HTTPException(404, "no catalogued region contains that point")
    return r


@router.get("/regions/{code}/cells")
async def list_region_cells(code: str):
    """The 0.5° sub-cells covering a parent region's bbox. Each cell is a z17-friendly download
    unit (~150–800 MB at z17); the parent itself stays selectable for "do a larger area at once"."""
    from app.core import regions
    parent = regions.get_region(code)
    if parent is None or parent.get("cell"):
        raise HTTPException(404, f"no such parent region {code!r}")
    return {"parent": parent, "cells": regions.cells_for(code), "cell_deg": regions.CELL_DEG}


class RegionDownloadRequest(BaseModel):
    layers: list[str] = ["terrain", "imagery", "buildings", "osm", "clutter"]   # imagery + DTED + clutter, by default
    max_zoom: Optional[int] = None                                              # imagery/osm street zoom (default 15 imagery / 12 osm)
    source: Optional[str] = None                                                # explicit tile-server / Overpass URL


class BboxDownloadRequest(BaseModel):
    bbox: list[float]                                                            # [w, s, e, n]
    layers: list[str] = ["terrain", "imagery", "buildings", "osm", "clutter"]
    max_zoom: Optional[int] = None
    source: Optional[str] = None
    name: Optional[str] = None                                                   # optional human label


# NOTE: the /regions/by-bbox/* routes MUST be declared before /regions/{code}/* — FastAPI matches
# in declaration order, and "by-bbox" would otherwise be swallowed by the {code} path param.
@router.post("/regions/by-bbox/estimate")
async def estimate_bbox(req: BboxDownloadRequest, principal: dict = Depends(require_auth)):
    """Like ``/regions/{code}/estimate`` but takes a freeform bbox. Drives the "Draw on map"
    download flow in the Layer Manager — the rectangle a user drew there isn't in the region
    catalogue, so it bypasses ``get_region`` and goes straight to the pack-size estimator."""
    if len(req.bbox) != 4:
        raise HTTPException(400, "bbox must be [w, s, e, n]")
    from app.core import pack_builder
    est = pack_builder.estimate_bytes(req.layers, req.bbox, req.max_zoom)
    region = {"code": "(custom-bbox)", "name": req.name or "Drawn area",
              "country": "(custom)", "bbox": list(req.bbox)}
    return {"region": region, **est, "layers": req.layers, "max_zoom": req.max_zoom}


@router.post("/regions/by-bbox/download")
async def download_bbox(req: BboxDownloadRequest, principal: dict = Depends(require_auth)):
    """Like ``/regions/{code}/download`` but takes a freeform bbox — one pack job per layer."""
    if len(req.bbox) != 4:
        raise HTTPException(400, "bbox must be [w, s, e, n]")
    bbox = list(req.bbox)
    jobs = []
    for layer in req.layers:
        try:
            job = packs_mod.start_download(layers=[layer], bbox=bbox, fidelity="best",
                                           max_zoom=req.max_zoom, source=req.source)
            job["region"] = {"code": "(custom-bbox)", "name": req.name or "Drawn area"}
            jobs.append(job)
        except ValueError as e:
            jobs.append({"layer": layer, "status": "error", "detail": str(e)})
    _kick_pack_jobs(jobs)
    audit("packs.bbox_download", bbox=bbox, layers=req.layers)
    region = {"code": "(custom-bbox)", "name": req.name or "Drawn area",
              "country": "(custom)", "bbox": bbox}
    return {"region": region, "jobs": jobs,
            "note": "drawn-bbox pack — same persistent library as named-region downloads"}


@router.post("/regions/{code}/estimate")
async def estimate_region(code: str, req: RegionDownloadRequest, principal: dict = Depends(require_auth)):
    """Estimate the download size, per layer, for a region pack request — *without* fetching
    anything. Drives the "Get download estimate" button in the Layer Manager so the user sees
    each item's size before committing to the download."""
    from app.core import regions, pack_builder
    region = regions.get_region(code)
    if region is None:
        raise HTTPException(404, f"no such region {code!r}")
    est = pack_builder.estimate_bytes(req.layers, region["bbox"], req.max_zoom)
    return {"region": region, **est, "layers": req.layers, "max_zoom": req.max_zoom}


@router.post("/regions/{code}/download")
async def download_region(code: str, req: RegionDownloadRequest, principal: dict = Depends(require_auth)):
    """Stage the offline data packs (imagery / DTED terrain / clutter / OSM buildings) for a named
    region into the persistent pack library. Runs in the background; one job per buildable layer —
    poll GET /packs/jobs. Imagery at street zoom over a big region is large; the region split keeps
    each pack practical (a whole-country box would be impractical — pick a sub-region for those)."""
    from app.core import regions
    region = regions.get_region(code)
    if region is None:
        raise HTTPException(404, f"no such region {code!r}")
    bbox = region["bbox"]
    jobs = []
    for layer in req.layers:
        try:
            job = packs_mod.start_download(layers=[layer], bbox=bbox, fidelity="best",
                                           max_zoom=req.max_zoom, source=req.source)
            job["region"] = {"code": region["code"], "name": region["name"]}
            jobs.append(job)
        except ValueError as e:
            jobs.append({"layer": layer, "status": "error", "detail": str(e)})
    _kick_pack_jobs(jobs)
    audit("packs.region_download", region=code, layers=req.layers)
    return {"region": region, "jobs": jobs,
            "note": "data lands in the persistent pack library (survives sessions); re-fetch later with POST /packs/{id}/update — manual only, never automatic"}


@router.post("/packs/{pack_id}/update")
async def update_pack(pack_id: str, max_zoom: Optional[int] = Query(None), source: Optional[str] = Query(None),
                      principal: dict = Depends(require_auth)):
    """Manually re-fetch a fresher version of an installed pack — re-runs the download for the same
    layer + bbox. There is no automatic/background refresh; this endpoint is the only way to update.
    The new pack is added to the library; delete the stale one when you're satisfied (DELETE /packs/{id})."""
    pack = packs_mod.get_pack(pack_id)
    if pack is None:
        raise HTTPException(404, "no such pack")
    layer = pack.get("layer")
    bbox = pack.get("bbox")
    try:
        job = packs_mod.start_download(layers=[layer], bbox=bbox, fidelity="best",
                                       max_zoom=max_zoom or pack.get("max_zoom"), source=source or pack.get("source"))
    except ValueError as e:
        raise HTTPException(400, str(e))
    _kick_pack_jobs([job])
    audit("packs.update", pack=pack_id, layer=layer)
    return {"updating": pack_id, "layer": layer, "bbox": bbox, "job": job}


# ── /terrain/heightmap — feeds the 3D globe's CustomHeightmapTerrainProvider ──
# GET /api/v1/terrain/heightmap/{pack_id}?west=&south=&east=&north=&w=&h=
# → little-endian int16 height grid (row-major, row 0 = north) for that tile rect.
# pack_id may be the literal `active`. The globe's callback only requests tiles
# that overlap the pack's bbox, so no all-globe tile storm.
@router.get("/terrain/heightmap/{pack_id}")
async def terrain_heightmap(
    pack_id: str,
    west: float = Query(...), south: float = Query(...), east: float = Query(...), north: float = Query(...),
    w: int = Query(65, ge=2, le=257), h: int = Query(65, ge=2, le=257),
    grow: bool = Query(True, description="run the provider chain — fetch missing SRTM cells online & cache them when connected"),
    principal: dict = Depends(require_auth),
):
    from app.core import terrain_tiles
    if grow:
        data, status = await terrain_tiles.heightmap_bytes_grown(pack_id, west, south, east, north, w, h)
    else:
        data, status = terrain_tiles.heightmap_bytes_or_none(pack_id, west, south, east, north, w, h), {"source": "pack"}
    if data is None:
        raise HTTPException(404, "no terrain pack")
    return Response(content=data, media_type="application/octet-stream",
                    headers={"Cache-Control": "public, max-age=86400",
                             "X-Ares-Terrain-Source": str(status.get("source", "pack"))})


# ── /net — graceful-degradation state ────────────────────────────────────────
@router.get("/net/status")
async def net_status(principal: dict = Depends(require_auth)):
    try:
        return net_state.status()
    except Exception:  # pragma: no cover
        return {"online": None, "network_policy": settings.network_policy, "last_known": {}, "overrides": {}}


class OverrideBody(BaseModel):
    data: dict


@router.put("/net/override/{kind}")
async def set_net_override(kind: str, body: OverrideBody, principal: dict = Depends(require_auth)):
    net_state.set_override(kind, body.data)
    return {"status": "ok", "kind": kind, "override": net_state.get_override(kind)}


@router.delete("/net/override/{kind}")
async def clear_net_override(kind: str, principal: dict = Depends(require_auth)):
    net_state.set_override(kind, None)
    return {"status": "cleared", "kind": kind}
