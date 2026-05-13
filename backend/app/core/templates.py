"""
Ares — radio template store (Workstream C).

A *template* is a JSON document describing a radio's RF parameters (the
SOOTHSAYER/CloudRF "template" concept). Operators load templates in the ATAK
plugin / web app, place a marker, and a coverage calculation is run from the
template. Templates live in ``data/atak_templates/<id>.json`` and are also
side-loadable (the ATAK plugin watches ``atak/ARES/templates/``).

Schema (Ares-native; a superset of the common CloudRF fields)::

    {
      "id": "dmr-handheld-700",
      "name": "DMR handheld @ 700 MHz",
      "icon_b64": null,                 # optional ~48x48 PNG, base64
      "transmitter": {
        "frequency_hz": 700e6, "power_dbm": 33.0,
        "height_m": 1.8, "altitude_m": 0.0, "bandwidth_hz": 12500.0,
        "noise_dbm": -120.0
      },
      "antenna": { "type": "dipole_half_wave", "gain_dbi": 2.15,
                   "azimuth_deg": [0], "tilt_deg": 0.0, "polarization": "vertical" },
      "receiver": { "height_m": 1.8, "gain_dbi": 2.15, "sensitivity_dbm": -110.0 },
      "model": { "propagation_model": "itm", "diffraction_model": "deygout",
                 "radius_km": 5.0, "min_signal_dbm": -110.0 },
      "environment": { "clutter_height_m": 0.0, "use_buildings": false }
    }

``to_coverage_request(template, lat, lon)`` flattens this to the body expected
by ``POST /api/v1/simulate/coverage``.
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any, Optional

from app.config import DATA_DIR

TEMPLATES_DIR = DATA_DIR / "atak_templates"
TEMPLATES_DIR.mkdir(parents=True, exist_ok=True)

_ID_RE = re.compile(r"[^a-z0-9_-]+")


def _safe_id(raw: str) -> str:
    s = _ID_RE.sub("-", (raw or "").strip().lower()).strip("-")
    return s or f"tmpl-{int(time.time())}"


# ── built-in seeds (a few common radios) ─────────────────────────────────────
_SEEDS: list[dict] = [
    {
        "id": "dmr-handheld-700", "name": "DMR handheld @ 700 MHz",
        "transmitter": {"frequency_hz": 700e6, "power_dbm": 33.0, "height_m": 1.8,
                        "altitude_m": 0.0, "bandwidth_hz": 12500.0, "noise_dbm": -120.0},
        "antenna": {"type": "dipole_half_wave", "gain_dbi": 2.15, "azimuth_deg": [0],
                    "tilt_deg": 0.0, "polarization": "vertical"},
        "receiver": {"height_m": 1.8, "gain_dbi": 2.15, "sensitivity_dbm": -110.0},
        "model": {"propagation_model": "itm", "diffraction_model": "deygout",
                  "radius_km": 5.0, "min_signal_dbm": -110.0},
        "environment": {"clutter_height_m": 0.0, "use_buildings": False},
    },
    {
        "id": "vhf-manpack-50w", "name": "VHF manpack 50 W @ 30–88 MHz",
        "transmitter": {"frequency_hz": 50e6, "power_dbm": 47.0, "height_m": 2.0,
                        "altitude_m": 0.0, "bandwidth_hz": 25000.0, "noise_dbm": -118.0},
        "antenna": {"type": "monopole_quarter_wave", "gain_dbi": 0.0, "azimuth_deg": [0],
                    "tilt_deg": 0.0, "polarization": "vertical"},
        "receiver": {"height_m": 2.0, "gain_dbi": 0.0, "sensitivity_dbm": -110.0},
        "model": {"propagation_model": "itm", "diffraction_model": "deygout",
                  "radius_km": 30.0, "min_signal_dbm": -110.0},
        "environment": {"clutter_height_m": 0.0, "use_buildings": False},
    },
    {
        "id": "wifi-ap-2g4-sector", "name": "2.4 GHz AP, 90° sector",
        "transmitter": {"frequency_hz": 2400e6, "power_dbm": 30.0, "height_m": 10.0,
                        "altitude_m": 0.0, "bandwidth_hz": 20e6, "noise_dbm": -95.0},
        "antenna": {"type": "sector_90", "gain_dbi": 14.0, "azimuth_deg": [0, 120, 240],
                    "tilt_deg": -2.0, "polarization": "vertical"},
        "receiver": {"height_m": 1.5, "gain_dbi": 2.0, "sensitivity_dbm": -85.0},
        "model": {"propagation_model": "itm", "diffraction_model": "bullington",
                  "radius_km": 3.0, "min_signal_dbm": -85.0},
        "environment": {"clutter_height_m": 0.0, "use_buildings": True},
    },
]


def _seed_if_empty() -> None:
    if any(TEMPLATES_DIR.glob("*.json")):
        return
    for t in _SEEDS:
        save_template(t, seed=True)


# ── CRUD ─────────────────────────────────────────────────────────────────────
def _path(template_id: str) -> Path:
    return TEMPLATES_DIR / f"{_safe_id(template_id)}.json"


def list_templates() -> list[dict]:
    _seed_if_empty()
    out = []
    for f in sorted(TEMPLATES_DIR.glob("*.json")):
        try:
            out.append(json.loads(f.read_text()))
        except Exception:
            pass
    return out


def get_template(template_id: str) -> Optional[dict]:
    p = _path(template_id)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except Exception:
        return None


def save_template(template: dict, *, seed: bool = False) -> dict:
    t = dict(template)
    t["id"] = _safe_id(t.get("id") or t.get("name") or "")
    t.setdefault("name", t["id"])
    if not seed:
        t["updated"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    _path(t["id"]).write_text(json.dumps(t, indent=2))
    return t


def delete_template(template_id: str) -> bool:
    p = _path(template_id)
    if p.exists():
        p.unlink()
        return True
    return False


# ── mapping → /simulate/coverage body ────────────────────────────────────────
def to_coverage_request(template: dict, lat: float, lon: float,
                        azimuth_deg: Optional[float] = None) -> dict:
    """Flatten a template + a placed location into a ``CoverageRequest`` body.

    Multi-azimuth templates: pass one ``azimuth_deg`` to render a single sector,
    or call once per azimuth and merge the layers (the plugin does the latter).
    """
    tx = template.get("transmitter", {})
    ant = template.get("antenna", {})
    rx = template.get("receiver", {})
    mdl = template.get("model", {})
    env = template.get("environment", {})
    azimuths = ant.get("azimuth_deg", [0]) or [0]
    az = azimuth_deg if azimuth_deg is not None else (azimuths[0] if isinstance(azimuths, list) else azimuths)
    return {
        "transmitter": {
            "lat": lat, "lon": lon,
            "height_m": tx.get("height_m", 1.8288),
            "altitude_m": tx.get("altitude_m", 0.0),
            "power_dbm": tx.get("power_dbm", 27.0),
            "frequency_hz": tx.get("frequency_hz", 433e6),
            "antenna": {
                "type": ant.get("type", "dipole_half_wave"),
                "gain_dbi": ant.get("gain_dbi"),
                "azimuth_deg": az,
                "tilt_deg": ant.get("tilt_deg", 0.0),
                "polarization": ant.get("polarization", "vertical"),
                "frequency_hz": tx.get("frequency_hz", 433e6),
            },
        },
        "receiver": {
            "height_m": rx.get("height_m", 1.8288),
            "gain_dbi": rx.get("gain_dbi", 0.0),
            "sensitivity_dbm": rx.get("sensitivity_dbm", -110.0),
        },
        "propagation_model": mdl.get("propagation_model", "itm"),
        "diffraction_model": mdl.get("diffraction_model", "deygout"),
        "radius_km": mdl.get("radius_km", 5.0),
        "min_signal_dbm": mdl.get("min_signal_dbm", -110.0),
        "include_buildings": bool(env.get("use_buildings", False)),
        "clutter_height_m": env.get("clutter_height_m", 0.0),
        "_template_id": template.get("id"),
    }
