#!/usr/bin/env python3
# SPDX-License-Identifier: MIT OR Apache-2.0
# Copyright (c) 2026 Ares

"""
Pre-download SRTM terrain tiles for offline use.

Downloads tiles from AWS S3 (elevation-tiles-prod/skadi) into the terrain
cache directory.  Tiles are stored atomically so a crash mid-download never
leaves a corrupt file, and existing good tiles are never deleted.

Usage:
    python preload_terrain.py                   # UK default area
    python preload_terrain.py --lat 48 56 --lon -5 3   # custom bounding box
    python preload_terrain.py --list            # show which tiles are cached
"""
import argparse
import asyncio
import gzip
import sys
from pathlib import Path
import aiohttp

# Resolve project paths without importing the full app
SCRIPT_DIR = Path(__file__).resolve().parent
BACKEND_DIR = SCRIPT_DIR.parent
DATA_DIR = BACKEND_DIR / "data"
TERRAIN_DIR = DATA_DIR / "terrain" / "srtm1"

SRTM1_PIXELS = 3601
SRTM3_PIXELS = 1201
MIN_VALID_BYTES = SRTM3_PIXELS * SRTM3_PIXELS * 2   # ~2.9 MB — reject anything smaller

AWS_BASE = "https://s3.amazonaws.com/elevation-tiles-prod/skadi"

# Default area: UK + surrounding waters at ~100 km simulation margin
# Covers lat 49–59 N, lon 6 W to 3 E  (10 × 9 = 90 tiles, ~2.3 GB SRTM1)
# Reduced default: lat 50–58 N, lon 5 W to 2 E  (8 × 7 = 56 tiles, ~1.4 GB)
DEFAULT_LAT = (50, 58)   # inclusive range of SW tile corners
DEFAULT_LON = (-5, 2)


def tile_name(lat: int, lon: int) -> str:
    lc = "N" if lat >= 0 else "S"
    oc = "E" if lon >= 0 else "W"
    return f"{lc}{abs(lat):02d}{oc}{abs(lon):03d}.hgt"


def tile_url(lat: int, lon: int) -> str:
    lc = "N" if lat >= 0 else "S"
    oc = "E" if lon >= 0 else "W"
    lat_band = f"{lc}{abs(lat):02d}"
    fname = f"{lc}{abs(lat):02d}{oc}{abs(lon):03d}.hgt.gz"
    return f"{AWS_BASE}/{lat_band}/{fname}"


def list_cached(lat_range, lon_range):
    tiles = [(lat, lon)
             for lat in range(lat_range[0], lat_range[1] + 1)
             for lon in range(lon_range[0], lon_range[1] + 1)]
    cached = 0
    for lat, lon in tiles:
        path = TERRAIN_DIR / tile_name(lat, lon)
        status = "cached" if path.exists() else "missing"
        if path.exists():
            cached += 1
        print(f"  {tile_name(lat, lon):20s} {status}")
    print(f"\n{cached}/{len(tiles)} tiles cached")


async def download_tile(session, lat: int, lon: int, semaphore: asyncio.Semaphore) -> bool:
    name = tile_name(lat, lon)
    dest = TERRAIN_DIR / name
    tmp  = dest.with_suffix('.tmp')

    if dest.exists():
        return True  # already cached — never overwrite a good tile

    url = tile_url(lat, lon)
    async with semaphore:
        try:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=60)) as resp:
                if resp.status != 200:
                    return False
                raw = await resp.read()
                content = gzip.decompress(raw)

            if len(content) < MIN_VALID_BYTES:
                return False

            # Atomic write
            tmp.write_bytes(content)
            tmp.replace(dest)
            return True
        except Exception as e:
            tmp.unlink(missing_ok=True)
            return False


async def run(lat_range, lon_range, concurrency: int):
    TERRAIN_DIR.mkdir(parents=True, exist_ok=True)

    tiles = [(lat, lon)
             for lat in range(lat_range[0], lat_range[1] + 1)
             for lon in range(lon_range[0], lon_range[1] + 1)]

    already = sum(1 for lat, lon in tiles if (TERRAIN_DIR / tile_name(lat, lon)).exists())
    needed  = len(tiles) - already

    if needed == 0:
        print(f"All {len(tiles)} tiles already cached.")
        return

    size_gb = needed * 25 / 1024
    print(f"Preloading {needed} tile(s)  (~{size_gb:.1f} GB — may take a while)")
    print(f"Cache: {TERRAIN_DIR}")

    semaphore = asyncio.Semaphore(concurrency)
    ok = fail = 0

    async with aiohttp.ClientSession() as session:
        tasks = [download_tile(session, lat, lon, semaphore) for lat, lon in tiles
                 if not (TERRAIN_DIR / tile_name(lat, lon)).exists()]
        total = len(tasks)
        for i, coro in enumerate(asyncio.as_completed(tasks), 1):
            result = await coro
            if result:
                ok += 1
            else:
                fail += 1
            done = ok + fail + already
            bar = '#' * int(30 * done / len(tiles))
            print(f"\r  [{bar:<30}] {done}/{len(tiles)}  ok={ok}  fail={fail}", end='', flush=True)

    print(f"\nDone: {ok} downloaded, {fail} failed, {already} already cached")
    if fail:
        print("  (failed tiles will be retried on next simulation — app still works offline with cached tiles)")


def main():
    parser = argparse.ArgumentParser(description="Pre-download SRTM terrain tiles")
    parser.add_argument("--lat", nargs=2, type=int, metavar=("MIN", "MAX"),
                        default=list(DEFAULT_LAT),
                        help=f"Latitude range (SW tile corners). Default: {DEFAULT_LAT}")
    parser.add_argument("--lon", nargs=2, type=int, metavar=("MIN", "MAX"),
                        default=list(DEFAULT_LON),
                        help=f"Longitude range (SW tile corners). Default: {DEFAULT_LON}")
    parser.add_argument("--concurrency", type=int, default=4,
                        help="Parallel downloads (default: 4)")
    parser.add_argument("--list", action="store_true",
                        help="List cache status without downloading")
    args = parser.parse_args()

    lat_range = tuple(sorted(args.lat))
    lon_range = tuple(sorted(args.lon))

    if args.list:
        list_cached(lat_range, lon_range)
        return

    asyncio.run(run(lat_range, lon_range, args.concurrency))


if __name__ == "__main__":
    main()
