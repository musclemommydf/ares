"""
Ares Mission Package (`.ares-mission` zip) — single-file bundle of an
operational state. Modelled on RFEye Site mission files + ATAK Data Packages.

Contents:
  manifest.json         — { version, created, name, description }
  scan_list.json        — tasking queue entries
  watchlist.json        — alert watchlist (freqs + tolerances + labels)
  geofences.geojson     — FeatureCollection of alert geofences
  antennas.json         — antenna profiles + per-element calibration
  drawings.geojson      — user-drawn map features
  templates/            — saved radio templates (one .json per template)
  packs/                — optional pack manifests (no payload — just identifiers
                          + bbox; pack files stay in the per-pack library)
  notes.md              — operator notes / SOPs

Round-trip: `export_mission_package(...)` writes a zip; `import_mission_package`
restores all of the above into Ares's existing state stores. Idempotent.
"""

from __future__ import annotations

import io
import json
import time
import zipfile
from pathlib import Path
from typing import Optional


MISSION_VERSION = "1.0"


def export_mission_package(
    *,
    name: str = "ares-mission",
    description: str = "",
    scan_list: Optional[list[dict]] = None,
    watchlist: Optional[list[dict]] = None,
    geofences: Optional[dict] = None,
    antennas: Optional[list[dict]] = None,
    drawings: Optional[dict] = None,
    templates: Optional[list[dict]] = None,
    packs: Optional[list[dict]] = None,
    notes: str = "",
) -> bytes:
    """Build the zip in-memory. Returns the raw bytes (caller writes them)."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        manifest = {
            "version": MISSION_VERSION,
            "created": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "name": name,
            "description": description,
        }
        z.writestr("manifest.json", json.dumps(manifest, indent=2))
        z.writestr("scan_list.json", json.dumps(scan_list or [], indent=2))
        z.writestr("watchlist.json", json.dumps(watchlist or [], indent=2))
        z.writestr("geofences.geojson", json.dumps(geofences or {"type": "FeatureCollection", "features": []}, indent=2))
        z.writestr("antennas.json", json.dumps(antennas or [], indent=2))
        z.writestr("drawings.geojson", json.dumps(drawings or {"type": "FeatureCollection", "features": []}, indent=2))
        z.writestr("packs.json", json.dumps(packs or [], indent=2))
        if notes:
            z.writestr("notes.md", notes)
        for tmpl in (templates or []):
            tid = str(tmpl.get("id") or tmpl.get("name") or "unnamed").replace("/", "_")
            z.writestr(f"templates/{tid}.json", json.dumps(tmpl, indent=2))
    return buf.getvalue()


def import_mission_package(zip_bytes: bytes) -> dict:
    """Parse a mission-package zip and return a dict of restored entries
    keyed by the file's stem. Caller wires the entries into the live stores."""
    out: dict = {
        "manifest": {}, "scan_list": [], "watchlist": [],
        "geofences": {"type": "FeatureCollection", "features": []},
        "antennas": [], "drawings": {"type": "FeatureCollection", "features": []},
        "packs": [], "templates": [], "notes": "",
    }
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as z:
        names = set(z.namelist())
        def load_json(n, default):
            return json.loads(z.read(n)) if n in names else default
        out["manifest"]  = load_json("manifest.json", {})
        out["scan_list"] = load_json("scan_list.json", [])
        out["watchlist"] = load_json("watchlist.json", [])
        out["geofences"] = load_json("geofences.geojson", out["geofences"])
        out["antennas"]  = load_json("antennas.json", [])
        out["drawings"]  = load_json("drawings.geojson", out["drawings"])
        out["packs"]     = load_json("packs.json", [])
        if "notes.md" in names:
            out["notes"] = z.read("notes.md").decode("utf-8", errors="replace")
        for n in names:
            if n.startswith("templates/") and n.endswith(".json"):
                try: out["templates"].append(json.loads(z.read(n)))
                except Exception: pass
    return out
