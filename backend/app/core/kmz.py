"""
Ares — KMZ export of coverage layers (Workstream C, §C.1).

Turns a coverage GeoJSON (a ``Point`` FeatureCollection with ``signal_dbm`` /
``covered`` properties, as produced by ``/api/v1/simulate/coverage``) into a KMZ
containing a ``GroundOverlay`` PNG — the format ATAK / WinTAK / Google Earth
import directly ("Image Overlay File") and the ATAK plugin sends to contacts.

Rasterisation mirrors the web map's heatmap: each coverage point is splatted as a
small coloured disc onto an RGBA grid sized to the layer's bounding box; pixels
outside coverage stay transparent. Good enough for situational use; a smoother
interpolated raster can replace it later.
"""
from __future__ import annotations

import io
import math
import zipfile
from typing import Iterable, Optional

import numpy as np

try:
    from PIL import Image
except Exception:  # pragma: no cover
    Image = None  # type: ignore


# colour ramp — matches frontend/src/api/client.js signalToColor()
def _signal_rgba(dbm: float, min_dbm: float = -120.0) -> tuple[int, int, int, int]:
    norm = max(0.0, min(1.0, (dbm - min_dbm) / (0.0 - min_dbm)))
    if norm > 0.8:
        return (6, 214, 160, 200)
    if norm > 0.6:
        return (132, 204, 22, 200)
    if norm > 0.4:
        return (245, 158, 11, 200)
    if norm > 0.2:
        return (239, 68, 68, 200)
    return (100, 100, 100, 80)


def _bounds(features: list[dict]) -> Optional[tuple[float, float, float, float]]:
    lons, lats = [], []
    for f in features:
        g = f.get("geometry") or {}
        if g.get("type") == "Point":
            c = g.get("coordinates") or []
            if len(c) >= 2:
                lons.append(c[0]); lats.append(c[1])
    if not lons:
        return None
    return (min(lons), min(lats), max(lons), max(lats))  # west, south, east, north


def rasterize_coverage(geojson: dict, *, min_signal_dbm: float = -120.0,
                       max_px: int = 2048, dot_radius_px: int = 3) -> Optional[tuple[bytes, tuple[float, float, float, float]]]:
    """Return ``(png_bytes, (west, south, east, north))`` or ``None`` if empty / PIL missing."""
    if Image is None:
        return None
    feats = [f for f in (geojson.get("features") or [])
             if (f.get("geometry") or {}).get("type") == "Point" and f.get("properties", {}).get("covered") is not False]
    if not feats:
        return None
    b = _bounds(feats)
    if not b:
        return None
    west, south, east, north = b
    # pad a touch so edge dots aren't clipped
    dlon = max(east - west, 1e-6); dlat = max(north - south, 1e-6)
    west -= dlon * 0.02; east += dlon * 0.02; south -= dlat * 0.02; north += dlat * 0.02
    dlon = east - west; dlat = north - south

    # size the raster, keeping aspect ratio of the (cos-lat-corrected) box
    mid_lat = (north + south) / 2.0
    aspect = (dlon * math.cos(math.radians(mid_lat))) / dlat if dlat else 1.0
    if aspect >= 1:
        w = max_px; h = max(1, int(round(max_px / aspect)))
    else:
        h = max_px; w = max(1, int(round(max_px * aspect)))

    arr = np.zeros((h, w, 4), dtype=np.uint8)
    r = max(1, int(dot_radius_px))
    yy, xx = np.ogrid[-r:r + 1, -r:r + 1]
    disc = (xx * xx + yy * yy) <= r * r
    for f in feats:
        lon, lat = f["geometry"]["coordinates"][:2]
        px = int((lon - west) / dlon * (w - 1))
        py = int((north - lat) / dlat * (h - 1))  # north at top
        x0, x1 = max(0, px - r), min(w, px + r + 1)
        y0, y1 = max(0, py - r), min(h, py + r + 1)
        if x0 >= x1 or y0 >= y1:
            continue
        sub = disc[(y0 - (py - r)):(y1 - (py - r)), (x0 - (px - r)):(x1 - (px - r))]
        rgba = _signal_rgba(f.get("properties", {}).get("signal_dbm", min_signal_dbm), min_signal_dbm)
        for ch in range(4):
            block = arr[y0:y1, x0:x1, ch]
            block[sub] = rgba[ch]

    buf = io.BytesIO()
    Image.fromarray(arr, mode="RGBA").save(buf, format="PNG", optimize=True)
    return buf.getvalue(), (west, south, east, north)


_KML = """<?xml version="1.0" encoding="UTF-8"?>
<kml xmlns="http://www.opengis.net/kml/2.2">
  <Document>
    <name>{name}</name>
    <GroundOverlay>
      <name>{name}</name>
      <description>Ares coverage layer</description>
      <Icon><href>overlay.png</href></Icon>
      <LatLonBox>
        <north>{north}</north><south>{south}</south><east>{east}</east><west>{west}</west>
      </LatLonBox>
    </GroundOverlay>
  </Document>
</kml>
"""


def coverage_geojson_to_kmz(geojson: dict, name: str = "Ares coverage",
                            min_signal_dbm: float = -120.0) -> Optional[bytes]:
    raster = rasterize_coverage(geojson, min_signal_dbm=min_signal_dbm)
    if raster is None:
        return None
    png, (west, south, east, north) = raster
    kml = _KML.format(name=name, north=north, south=south, east=east, west=west)
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("doc.kml", kml)
        z.writestr("overlay.png", png)
    return out.getvalue()
