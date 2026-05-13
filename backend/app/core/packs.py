"""
Ares — offline data packs (Workstream A.2).

A *pack* is a directory under ``data/packs/<layer>/<pack_id>/`` containing data
files plus a ``manifest.json``. Layers: terrain, osm, buildings, clutter, imagery.

This module owns the on-disk layout, the manifest schema, and CRUD over packs.
Actually *downloading* data into a pack (terrain region packs, "full planet",
OSM tiles, AO imagery caches, …) is wired in a later phase — ``start_download``
currently records a job and returns a not-yet-implemented status so the API
shape is stable.

Manifest schema (``manifest.json``)::

    {
      "id": "terrain-srtm30-alps",
      "layer": "terrain",
      "name": "SRTM 30m — Alps",
      "source": "srtm30",          # provider / fidelity tier id
      "format": "quantized-mesh",  # quantized-mesh | mbtiles | geopackage | geotiff | hgt | ...
      "bbox": [minlon, minlat, maxlon, maxlat],
      "resolution_m": 30,          # for raster/terrain layers
      "zoom_min": 0, "zoom_max": 14,   # for tiled layers
      "size_bytes": 12345678,
      "checksum": "sha256:...",    # optional, of a packaged archive
      "build_date": "2026-05-10T12:00:00Z",
      "ares_pack_version": 1
    }
"""
from __future__ import annotations

import json
import logging
import shutil
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional

from app.config import PACKS_DIR, PACK_LAYERS

log = logging.getLogger(__name__)

PACK_VERSION = 1
MANIFEST_NAME = "manifest.json"


# ─────────────────────────────────────────────────────────────────────────────
@dataclass
class PackManifest:
    id: str
    layer: str
    name: str = ""
    source: str = ""
    format: str = ""
    bbox: Optional[list[float]] = None  # [minlon, minlat, maxlon, maxlat]
    resolution_m: Optional[float] = None
    zoom_min: Optional[int] = None
    zoom_max: Optional[int] = None
    size_bytes: int = 0
    checksum: Optional[str] = None
    build_date: str = ""
    ares_pack_version: int = PACK_VERSION
    extra: dict[str, Any] = field(default_factory=dict)

    @staticmethod
    def from_dict(d: dict) -> "PackManifest":
        known = {f for f in PackManifest.__dataclass_fields__}  # type: ignore[attr-defined]
        extra = {k: v for k, v in d.items() if k not in known}
        base = {k: v for k, v in d.items() if k in known and k != "extra"}
        return PackManifest(extra=extra, **base)

    def to_dict(self) -> dict:
        d = asdict(self)
        extra = d.pop("extra", {}) or {}
        return {**d, **extra}


def _layer_dir(layer: str) -> Path:
    if layer not in PACK_LAYERS:
        raise ValueError(f"unknown pack layer {layer!r}; expected one of {PACK_LAYERS}")
    p = PACKS_DIR / layer
    p.mkdir(parents=True, exist_ok=True)
    return p


def _dir_size(path: Path) -> int:
    total = 0
    try:
        for f in path.rglob("*"):
            try:
                if f.is_file():
                    total += f.stat().st_size
            except OSError:
                continue
    except OSError:
        pass
    return total


# ─────────────────────────────────────────────────────────────────────────────
# CRUD
# ─────────────────────────────────────────────────────────────────────────────
def list_packs(layer: Optional[str] = None) -> list[dict]:
    layers = [layer] if layer else list(PACK_LAYERS)
    out: list[dict] = []
    for lyr in layers:
        for manifest_path in sorted(_layer_dir(lyr).glob(f"*/{MANIFEST_NAME}")):
            try:
                m = PackManifest.from_dict(json.loads(manifest_path.read_text()))
                d = m.to_dict()
                d["path"] = str(manifest_path.parent)
                d["size_bytes_on_disk"] = _dir_size(manifest_path.parent)
                out.append(d)
            except Exception:
                log.exception("bad manifest at %s", manifest_path)
    return out


def get_pack(pack_id: str) -> Optional[dict]:
    for p in list_packs():
        if p["id"] == pack_id:
            return p
    return None


def latest_pack(layer: str) -> Optional[dict]:
    """Most-recently-built pack of a layer (by build_date, then dir mtime). Used by
    the ``…/packs/<layer>/active/…`` static alias the Cesium globe references."""
    ps = list_packs(layer)
    if not ps:
        return None
    def _key(p: dict):
        return (p.get("build_date") or "", Path(p["path"]).stat().st_mtime)
    return sorted(ps, key=_key)[-1]


def pack_file_path(layer: str, pack_id: str, rel: str) -> Optional[Path]:
    """Resolve ``data/packs/<layer>/<pack_id>/<rel>`` with a path-traversal guard.
    ``pack_id`` may be the literal ``active`` to pick :func:`latest_pack`."""
    if layer not in PACK_LAYERS:
        return None
    if pack_id == "active":
        lp = latest_pack(layer)
        if lp is None:
            return None
        pack_id = lp["id"]
    root = (_layer_dir(layer) / pack_id).resolve()
    if not root.is_dir():
        return None
    target = (root / rel).resolve()
    try:
        target.relative_to(root)  # raises if rel escapes the pack dir
    except ValueError:
        return None
    return target if target.is_file() else None


def register_pack(manifest: PackManifest) -> dict:
    """Create (or update) a pack directory + manifest. Caller has already put
    the data files in place under ``data/packs/<layer>/<id>/`` (or will)."""
    d = _layer_dir(manifest.layer) / manifest.id
    d.mkdir(parents=True, exist_ok=True)
    if not manifest.build_date:
        manifest.build_date = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    manifest.size_bytes = manifest.size_bytes or _dir_size(d)
    (d / MANIFEST_NAME).write_text(json.dumps(manifest.to_dict(), indent=2))
    log.info("registered pack %s (%s)", manifest.id, manifest.layer)
    res = manifest.to_dict()
    res["path"] = str(d)
    return res


def verify_pack(pack_id: str, deep: bool = False) -> Optional[dict]:
    """Integrity / version check for one pack (P5 hardening). ``deep`` re-hashes
    the whole pack dir and compares against ``checksum`` when the manifest has one.
    Returns ``None`` if there's no such pack."""
    pack = get_pack(pack_id)
    if pack is None:
        return None
    root = Path(pack["path"])
    issues: list[str] = []
    files = [f for f in root.rglob("*") if f.is_file() and f.name != MANIFEST_NAME]
    if not files:
        issues.append("pack contains no data files")
    pv = int(pack.get("ares_pack_version", 0) or 0)
    if pv > PACK_VERSION:
        issues.append(f"pack version {pv} is newer than this server supports ({PACK_VERSION}) — upgrade Ares")
    elif pv < PACK_VERSION:
        issues.append(f"pack version {pv} predates the current format ({PACK_VERSION}) — re-download to upgrade")
    declared = int(pack.get("size_bytes", 0) or 0)
    on_disk = _dir_size(root)
    if declared and abs(declared - on_disk) > max(4096, declared // 100):
        issues.append(f"size mismatch: manifest says {declared} B, on disk {on_disk} B (incomplete download?)")
    checksum_ok: Optional[bool] = None
    if deep and pack.get("checksum"):
        import hashlib
        algo, _, want = str(pack["checksum"]).partition(":")
        try:
            h = hashlib.new(algo or "sha256")
            for f in sorted(files):
                h.update(f.relative_to(root).as_posix().encode())
                h.update(f.read_bytes())
            checksum_ok = (h.hexdigest() == want)
            if not checksum_ok:
                issues.append("checksum mismatch")
        except Exception as e:  # pragma: no cover
            issues.append(f"checksum could not be computed: {e}")
    return {
        "id": pack_id, "layer": pack["layer"], "ok": not issues, "issues": issues,
        "pack_version": pv, "current_pack_version": PACK_VERSION,
        "file_count": len(files), "size_bytes_on_disk": on_disk, "size_bytes_declared": declared,
        "checksum_ok": checksum_ok,
    }


def buildings_near(lat: float, lon: float, radius_m: float) -> Optional[dict]:
    """Footprints from any installed ``buildings`` pack covering (lat, lon), as a
    GeoJSON FeatureCollection clipped to ``radius_m``. ``None`` if no pack covers
    the point. Used as the offline fallback for ``GET /terrain/buildings``."""
    import json as _json
    import math as _math
    out: list[dict] = []
    cos_lat = max(0.05, _math.cos(_math.radians(lat)))
    seen = False
    for p in list_packs("buildings"):
        bb = p.get("bbox")
        if not (isinstance(bb, (list, tuple)) and len(bb) == 4):
            continue
        if not (bb[0] <= lon <= bb[2] and bb[1] <= lat <= bb[3]):
            continue
        fpath = pack_file_path("buildings", p["id"], p.get("file") or "buildings.geojson")
        if fpath is None:
            continue
        try:
            fc = _json.loads(Path(fpath).read_text())
        except Exception:
            continue
        seen = True
        for f in fc.get("features", []):
            try:
                ring = f["geometry"]["coordinates"][0]
                clon = sum(c[0] for c in ring) / len(ring)
                clat = sum(c[1] for c in ring) / len(ring)
                dx = (clon - lon) * 111320.0 * cos_lat
                dy = (clat - lat) * 110540.0
                if dx * dx + dy * dy <= radius_m * radius_m:
                    out.append(f)
            except Exception:
                continue
    if not seen:
        return None
    return {"type": "FeatureCollection", "features": out}


def delete_pack(pack_id: str) -> bool:
    for lyr in PACK_LAYERS:
        d = PACKS_DIR / lyr / pack_id
        if (d / MANIFEST_NAME).exists():
            shutil.rmtree(d, ignore_errors=True)
            log.info("deleted pack %s", pack_id)
            return True
    return False


# ─────────────────────────────────────────────────────────────────────────────
# Download jobs — API surface only for now (data fetch lands in a later phase)
# ─────────────────────────────────────────────────────────────────────────────
_JOBS: dict[str, dict] = {}


# layers with a working downloader (see app.core.pack_builder)
_BUILDABLE = {"terrain", "osm", "imagery", "buildings", "clutter"}


def start_download(*, layers: list[str], bbox: Optional[list[float]],
                   fidelity: str = "auto", max_zoom: Optional[int] = None,
                   source: Optional[str] = None) -> dict:
    bad = [l for l in layers if l not in PACK_LAYERS]
    if bad:
        raise ValueError(f"unknown layer(s) {bad}; expected subset of {PACK_LAYERS}")
    job_id = uuid.uuid4().hex[:12]
    buildable = [l for l in layers if l in _BUILDABLE]
    job = {
        "job_id": job_id,
        # queued → running → done | error  (not_implemented if no buildable layer requested)
        "status": "queued" if buildable else "not_implemented",
        "layers": layers,
        "buildable_layers": buildable,
        "bbox": bbox,  # None ⇒ "full planet" (clipped to SRTM land coverage for terrain)
        "fidelity": fidelity,
        "max_zoom": max_zoom,
        "source": source,
        "progress": 0.0,
        "created": time.time(),
        "detail": (
            f"queued: {buildable}" if buildable else
            "no buildable layer requested — terrain / osm / imagery / buildings / clutter "
            "all have downloaders. See docs/BUILD_PLAN.md §A.2."
        ),
    }
    _JOBS[job_id] = job
    log.info("pack download requested: layers=%s bbox=%s buildable=%s", layers, bbox, buildable)
    return job


def get_job(job_id: str) -> Optional[dict]:
    return _JOBS.get(job_id)


def list_jobs() -> list[dict]:
    return list(_JOBS.values())
