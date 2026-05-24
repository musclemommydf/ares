# SPDX-License-Identifier: MIT OR Apache-2.0
# Copyright (c) 2026 Ares

"""
Ares — serve offline terrain packs to the 3D globe as a Cesium *heightmap*
terrain provider (Workstream A / B).

Instead of pre-generating a quantized-mesh tileset, we expose a per-tile
heightmap endpoint: the Cesium globe uses `CustomHeightmapTerrainProvider` whose
callback fetches a w×h grid of int16 heights for the requested tile rectangle.
The grid is sampled on the fly from the SRTM `.hgt` files inside a terrain pack
(``data/packs/terrain/<id>/N45E006.hgt`` …) using the same elevation
interpolation the propagation engine uses. No new dependencies, no binary
quantized-mesh wrangling, and it works fully offline.

Row order: row 0 = the **north** edge (Cesium heightmap convention), column 0 =
**west**. Heights are clamped to int16 metres. If a grid point falls in a
1°×1° cell that isn't in the pack, that point is 0 (flat).
"""
from __future__ import annotations

import logging
import math
import struct
from functools import lru_cache
from pathlib import Path
from typing import Optional

import numpy as np

from app.config import PACKS_DIR
from app.core import packs as packs_mod
from app.core.propagation.terrain import TerrainTile

log = logging.getLogger(__name__)

_HGT_PIXELS = 3601   # SRTM 1-arc-second (~30 m); the pack builder downloads this resolution


def _terrain_pack_dirs(pack_id: str) -> list[Path]:
    """The pack dir(s) to sample from. ``active`` (the default the globe sends) ⇒
    *all* installed terrain packs, so a cell from any of them — including the
    auto-grown ``terrain-auto`` — is usable. A specific id ⇒ just that one."""
    if pack_id == "active":
        return [Path(p["path"]) for p in packs_mod.list_packs("terrain")]
    d = PACKS_DIR / "terrain" / pack_id
    return [d] if d.is_dir() else []


@lru_cache(maxsize=128)
def _load_hgt(pack_dir_str: str, lat_int: int, lon_int: int) -> Optional[TerrainTile]:
    """Load (and cache) the SRTM `.hgt` for the 1°×1° cell whose SW corner is
    (lat_int, lon_int) from a terrain pack, or None if it isn't in the pack."""
    name = TerrainTile(lat_int, lon_int, "srtm1").filename   # e.g. "N45E006.hgt"
    path = Path(pack_dir_str) / name
    if not path.is_file():
        return None
    tile = TerrainTile(lat_int, lon_int, "srtm1")
    return tile if tile.load_hgt(path) else None


def _load_cell(pack_dirs: list[str], lat_int: int, lon_int: int) -> Optional[TerrainTile]:
    for pdir in pack_dirs:
        t = _load_hgt(pdir, lat_int, lon_int)
        if t is not None:
            return t
    return None


def cells_for_rect(west: float, south: float, east: float, north: float) -> list[tuple[int, int]]:
    """The 1°×1° SRTM cells (SW corner ints) a lon/lat rectangle touches."""
    out = []
    for la in range(math.floor(south), math.floor(north) + 1):
        for lo in range(math.floor(west), math.floor(east) + 1):
            out.append((la, lo))
    return out


def sample_heightmap_rect(pack_id: str, west: float, south: float, east: float, north: float,
                          w: int = 65, h: int = 65) -> Optional[bytes]:
    """Sample a w×h int16 height grid (little-endian, row-major, row 0 = north)
    over the rectangle, from the installed terrain pack(s). Returns ``None`` if no
    terrain packs are installed at all (the caller then leaves that tile flat).
    Grid points in a cell that no pack has come back 0 (flat)."""
    pack_dirs = [str(p) for p in _terrain_pack_dirs(pack_id)]
    if not pack_dirs:
        return None
    w = max(2, min(257, int(w)))
    h = max(2, min(257, int(h)))
    grid = np.zeros((h, w), dtype=np.int16)
    dlon = (east - west) / (w - 1)
    dlat = (north - south) / (h - 1)
    for i in range(h):
        lat = north - i * dlat
        lat_int = math.floor(lat)
        for j in range(w):
            lon = west + j * dlon
            lon_int = math.floor(lon)
            tile = _load_cell(pack_dirs, lat_int, lon_int)
            if tile is None:
                continue
            try:
                e = tile.get_elevation(lat, lon)
            except Exception:
                e = 0.0
            if e != e or e is None:  # NaN
                e = 0.0
            grid[i, j] = int(max(-12000, min(12000, round(e))))
    # little-endian int16; Cesium reads `new Int16Array(arrayBuffer)` (native LE on x86)
    return grid.astype("<i2").tobytes()


# convenience for the API layer
def heightmap_bytes_or_none(pack_id: str, west: float, south: float, east: float, north: float,
                            w: int = 65, h: int = 65) -> Optional[bytes]:
    try:
        return sample_heightmap_rect(pack_id, west, south, east, north, w, h)
    except Exception:  # pragma: no cover - defensive
        log.exception("heightmap sampling failed")
        return None


async def heightmap_bytes_grown(pack_id: str, west: float, south: float, east: float, north: float,
                                w: int = 65, h: int = 65) -> tuple[Optional[bytes], dict]:
    """Like :func:`heightmap_bytes_or_none` but first runs the terrain provider
    chain (local pack → online fetch → cache into ``terrain-auto``) so a connected
    box grows its offline terrain pack as new areas are viewed. Returns
    ``(bytes_or_None, status_dict)`` — the status goes into a response header."""
    status: dict = {"source": "unknown"}
    try:
        from app.core import providers
        status = await providers.ensure_terrain_tiles(cells_for_rect(west, south, east, north))
        # invalidate the per-cell cache for cells we may have just downloaded
        if status.get("fetched"):
            _load_hgt.cache_clear()
    except Exception:  # pragma: no cover - the provider chain is best-effort
        log.exception("terrain provider chain failed")
    return heightmap_bytes_or_none(pack_id, west, south, east, north, w, h), status
