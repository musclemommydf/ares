"""
Ares — provider chain (Workstream A.3): *local pack → online fetch → cache*.

When the box is offline, data comes only from installed packs. When it's online
(and ``network_policy`` allows), a missing datum is fetched from the best remote
source **and written into a pack**, so a connected box transparently *grows its
own offline pack* as new areas get used.

Implemented here for **terrain** (SRTM 1-arc-second ``.hgt`` cells). Missing cells
land in a ``terrain-auto`` pack that is updated on every fetch — its bbox is the
union of the cells it has accumulated, and it reports ``cesium_ready`` so the 3D
globe and the heightmap endpoint pick it up like any other terrain pack.
"""
from __future__ import annotations

import asyncio
import gzip
import logging
import re
import time
from pathlib import Path
from typing import Iterable

import aiohttp

from app.config import PACKS_DIR, settings
from app.core import net_state
from app.core import packs as packs_mod
from app.core.pack_builder import SRTM1_SKADI, _hgt_name

log = logging.getLogger(__name__)

AUTO_TERRAIN_ID = "terrain-auto"
_lock = asyncio.Lock()


def _hgt_name_file(lat_int: int, lon_int: int) -> str:
    return f"{_hgt_name(lat_int, lon_int)}.hgt"


def _present_in_any_pack(lat_int: int, lon_int: int, pack_dirs: list[Path]) -> bool:
    name = _hgt_name_file(lat_int, lon_int)
    return any((d / name).is_file() for d in pack_dirs)


async def ensure_terrain_tiles(cells: Iterable[tuple[int, int]]) -> dict:
    """Make sure each ``(lat_int, lon_int)`` ``.hgt`` cell is available in some
    installed terrain pack; download the missing ones into ``terrain-auto`` when
    online and ``network_policy`` permits. Returns a small status dict::

        {"source": "pack"|"online"|"flat", "fetched": N, "available": M,
         "requested": K, "online": bool}
    """
    cells = sorted({(int(la), int(lo)) for la, lo in cells})
    pack_dirs = [Path(p["path"]) for p in packs_mod.list_packs("terrain")]
    missing = [(la, lo) for (la, lo) in cells if not _present_in_any_pack(la, lo, pack_dirs)]
    have = len(cells) - len(missing)
    online = net_state.is_online()
    base = {"requested": len(cells), "available": have, "fetched": 0, "online": bool(online)}

    if not missing:
        return {**base, "source": "pack" if have else "flat"}
    if settings.network_policy == "offline_only" or online is not True:
        return {**base, "source": "pack" if have else "flat",
                "note": "offline — missing terrain cells served flat"}

    fetched = 0
    async with _lock:
        out_dir = PACKS_DIR / "terrain" / AUTO_TERRAIN_ID
        out_dir.mkdir(parents=True, exist_ok=True)
        timeout = aiohttp.ClientTimeout(total=60, connect=15)
        sem = asyncio.Semaphore(6)
        async with aiohttp.ClientSession(timeout=timeout) as sess:
            async def one(la: int, lo: int) -> None:
                nonlocal fetched
                name = _hgt_name(la, lo)
                dst = out_dir / f"{name}.hgt"
                if dst.exists():
                    fetched += 1
                    return
                url = SRTM1_SKADI.format(band=name[:3], name=name)
                async with sem:
                    try:
                        async with sess.get(url) as r:
                            if r.status == 200:
                                dst.write_bytes(gzip.decompress(await r.read()))
                                fetched += 1
                            # 403/404 ⇒ ocean cell — leave it; the sampler returns flat there
                    except Exception:
                        pass
            await asyncio.gather(*(one(la, lo) for la, lo in missing))
        if fetched:
            _refresh_auto_manifest(out_dir)
            log.info("provider chain: grew %s by %d SRTM cell(s)", AUTO_TERRAIN_ID, fetched)
    return {**base, "fetched": fetched, "available": have + fetched,
            "source": "online" if fetched else ("pack" if have else "flat")}


def _refresh_auto_manifest(out_dir: Path) -> None:
    w = s = 999.0
    e = n = -999.0
    for f in out_dir.glob("*.hgt"):
        m = re.match(r"([NS])(\d+)([EW])(\d+)$", f.stem)
        if not m:
            continue
        la = int(m.group(2)) * (1 if m.group(1) == "N" else -1)
        lo = int(m.group(4)) * (1 if m.group(3) == "E" else -1)
        w, e = min(w, lo), max(e, lo + 1)
        s, n = min(s, la), max(n, la + 1)
    if w > e:
        return
    packs_mod.register_pack(packs_mod.PackManifest(
        id=AUTO_TERRAIN_ID, layer="terrain", name="SRTM 30 m — auto-grown (online cache)",
        source="srtm30-skadi", format="hgt", bbox=[w, s, e, n], resolution_m=30,
        build_date=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        extra={"auto_grown": True, "cesium_ready": True,
               "note": "grows on demand from online SRTM fetches when the box is connected (provider chain, §A.3)"},
    ))


def terrain_pack_dirs() -> list[Path]:
    """All installed terrain-pack directories (used by the heightmap sampler so a
    cell from *any* pack — including ``terrain-auto`` — is usable)."""
    return [Path(p["path"]) for p in packs_mod.list_packs("terrain")]
