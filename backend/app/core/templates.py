# SPDX-License-Identifier: MIT OR Apache-2.0
# Copyright (c) 2026 Ares

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


# ── built-in seeds — common tactical / civilian / SAR / EW radios ───────────
# Grouped roughly by band. Numbers are sensible defaults a user can edit per mission
# (power is at-radio dBm, not EIRP; gains are typical for the antenna class; sensitivities
# match commercial datasheets within ±5 dB). All use ITM + Deygout diffraction by default;
# the sub-GHz HF entry uses ITM with longer radius for groundwave / NVIS planning.
_SEEDS: list[dict] = [
    # ── HF (3–30 MHz) ───────────────────────────────────────────────────────
    {
        "id": "hf-manpack-ssb", "name": "HF manpack SSB · 20 W @ 5 MHz",
        "transmitter": {"frequency_hz": 5e6, "power_dbm": 43.0, "height_m": 2.0,
                        "altitude_m": 0.0, "bandwidth_hz": 2700.0, "noise_dbm": -118.0},
        "antenna": {"type": "whip_quarter_wave", "gain_dbi": 0.0, "azimuth_deg": [0],
                    "tilt_deg": 0.0, "polarization": "vertical"},
        "receiver": {"height_m": 2.0, "gain_dbi": 0.0, "sensitivity_dbm": -112.0},
        "model": {"propagation_model": "itm", "diffraction_model": "deygout",
                  "radius_km": 100.0, "min_signal_dbm": -110.0},
        "environment": {"clutter_height_m": 0.0, "use_buildings": False},
    },

    # ── VHF low-band (30–88 MHz) — tactical squad / platoon ─────────────────
    {
        "id": "vhf-manpack-50w", "name": "VHF low-band manpack · 50 W @ 50 MHz",
        "transmitter": {"frequency_hz": 50e6, "power_dbm": 47.0, "height_m": 2.0,
                        "altitude_m": 0.0, "bandwidth_hz": 25000.0, "noise_dbm": -118.0},
        "antenna": {"type": "monopole_quarter_wave", "gain_dbi": 0.0, "azimuth_deg": [0],
                    "tilt_deg": 0.0, "polarization": "vertical"},
        "receiver": {"height_m": 2.0, "gain_dbi": 0.0, "sensitivity_dbm": -110.0},
        "model": {"propagation_model": "itm", "diffraction_model": "deygout",
                  "radius_km": 30.0, "min_signal_dbm": -110.0},
        "environment": {"clutter_height_m": 0.0, "use_buildings": False},
    },

    # ── VHF aviation / marine (118–162 MHz) ─────────────────────────────────
    {
        "id": "vhf-aviation-am", "name": "VHF aviation AM · 10 W @ 122 MHz (aircraft, FL100)",
        "transmitter": {"frequency_hz": 122e6, "power_dbm": 40.0, "height_m": 3000.0,
                        "altitude_m": 3000.0, "bandwidth_hz": 25000.0, "noise_dbm": -116.0},
        "antenna": {"type": "monopole_quarter_wave", "gain_dbi": 0.0, "azimuth_deg": [0],
                    "tilt_deg": 0.0, "polarization": "vertical"},
        "receiver": {"height_m": 2.0, "gain_dbi": 0.0, "sensitivity_dbm": -107.0},
        "model": {"propagation_model": "itm", "diffraction_model": "deygout",
                  "radius_km": 200.0, "min_signal_dbm": -105.0},
        "environment": {"clutter_height_m": 0.0, "use_buildings": False},
    },
    {
        "id": "vhf-marine-ch16", "name": "VHF marine FM · 25 W @ 156.8 MHz (Ch16)",
        "transmitter": {"frequency_hz": 156.8e6, "power_dbm": 44.0, "height_m": 8.0,
                        "altitude_m": 0.0, "bandwidth_hz": 25000.0, "noise_dbm": -116.0},
        "antenna": {"type": "dipole_half_wave", "gain_dbi": 3.0, "azimuth_deg": [0],
                    "tilt_deg": 0.0, "polarization": "vertical"},
        "receiver": {"height_m": 2.0, "gain_dbi": 0.0, "sensitivity_dbm": -113.0},
        "model": {"propagation_model": "itm", "diffraction_model": "deygout",
                  "radius_km": 40.0, "min_signal_dbm": -110.0},
        "environment": {"clutter_height_m": 0.0, "use_buildings": False},
    },
    {
        "id": "ais-marine-162", "name": "AIS marine transponder · 12.5 W @ 162 MHz",
        "transmitter": {"frequency_hz": 162e6, "power_dbm": 41.0, "height_m": 10.0,
                        "altitude_m": 0.0, "bandwidth_hz": 25000.0, "noise_dbm": -116.0},
        "antenna": {"type": "dipole_half_wave", "gain_dbi": 3.0, "azimuth_deg": [0],
                    "tilt_deg": 0.0, "polarization": "vertical"},
        "receiver": {"height_m": 5.0, "gain_dbi": 3.0, "sensitivity_dbm": -107.0},
        "model": {"propagation_model": "itm", "diffraction_model": "deygout",
                  "radius_km": 60.0, "min_signal_dbm": -107.0},
        "environment": {"clutter_height_m": 0.0, "use_buildings": False},
    },

    # ── VHF / UHF land-mobile radio ─────────────────────────────────────────
    {
        "id": "vhf-lmr-handheld", "name": "VHF LMR handheld · 5 W @ 155 MHz",
        "transmitter": {"frequency_hz": 155e6, "power_dbm": 37.0, "height_m": 1.6,
                        "altitude_m": 0.0, "bandwidth_hz": 12500.0, "noise_dbm": -118.0},
        "antenna": {"type": "whip_quarter_wave", "gain_dbi": 0.0, "azimuth_deg": [0],
                    "tilt_deg": 0.0, "polarization": "vertical"},
        "receiver": {"height_m": 1.6, "gain_dbi": 0.0, "sensitivity_dbm": -118.0},
        "model": {"propagation_model": "itm", "diffraction_model": "deygout",
                  "radius_km": 10.0, "min_signal_dbm": -115.0},
        "environment": {"clutter_height_m": 0.0, "use_buildings": False},
    },
    {
        "id": "uhf-lmr-handheld", "name": "UHF LMR handheld · 4 W @ 450 MHz",
        "transmitter": {"frequency_hz": 450e6, "power_dbm": 36.0, "height_m": 1.6,
                        "altitude_m": 0.0, "bandwidth_hz": 12500.0, "noise_dbm": -118.0},
        "antenna": {"type": "whip_quarter_wave", "gain_dbi": 0.0, "azimuth_deg": [0],
                    "tilt_deg": 0.0, "polarization": "vertical"},
        "receiver": {"height_m": 1.6, "gain_dbi": 0.0, "sensitivity_dbm": -118.0},
        "model": {"propagation_model": "itm", "diffraction_model": "deygout",
                  "radius_km": 8.0, "min_signal_dbm": -115.0},
        "environment": {"clutter_height_m": 0.0, "use_buildings": False},
    },
    {
        "id": "dmr-handheld-700", "name": "DMR handheld · 2 W @ 700 MHz",
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
        "id": "p25-portable-800", "name": "P25 portable · 3 W @ 800 MHz",
        "transmitter": {"frequency_hz": 851e6, "power_dbm": 35.0, "height_m": 1.6,
                        "altitude_m": 0.0, "bandwidth_hz": 12500.0, "noise_dbm": -120.0},
        "antenna": {"type": "dipole_half_wave", "gain_dbi": 2.15, "azimuth_deg": [0],
                    "tilt_deg": 0.0, "polarization": "vertical"},
        "receiver": {"height_m": 1.6, "gain_dbi": 2.15, "sensitivity_dbm": -116.0},
        "model": {"propagation_model": "itm", "diffraction_model": "deygout",
                  "radius_km": 6.0, "min_signal_dbm": -110.0},
        "environment": {"clutter_height_m": 0.0, "use_buildings": False},
    },
    {
        "id": "tetra-portable-410", "name": "TETRA portable · 1 W @ 410 MHz",
        "transmitter": {"frequency_hz": 410e6, "power_dbm": 30.0, "height_m": 1.6,
                        "altitude_m": 0.0, "bandwidth_hz": 25000.0, "noise_dbm": -118.0},
        "antenna": {"type": "dipole_half_wave", "gain_dbi": 2.15, "azimuth_deg": [0],
                    "tilt_deg": 0.0, "polarization": "vertical"},
        "receiver": {"height_m": 1.6, "gain_dbi": 2.15, "sensitivity_dbm": -112.0},
        "model": {"propagation_model": "itm", "diffraction_model": "deygout",
                  "radius_km": 5.0, "min_signal_dbm": -108.0},
        "environment": {"clutter_height_m": 0.0, "use_buildings": False},
    },
    {
        "id": "tetra-base-sector120", "name": "TETRA base · 25 W @ 410 MHz, 3× 120° sectors",
        "transmitter": {"frequency_hz": 410e6, "power_dbm": 44.0, "height_m": 30.0,
                        "altitude_m": 0.0, "bandwidth_hz": 25000.0, "noise_dbm": -118.0},
        "antenna": {"type": "sector_120", "gain_dbi": 12.0, "azimuth_deg": [0, 120, 240],
                    "tilt_deg": -3.0, "polarization": "vertical"},
        "receiver": {"height_m": 1.6, "gain_dbi": 2.15, "sensitivity_dbm": -112.0},
        "model": {"propagation_model": "itm", "diffraction_model": "deygout",
                  "radius_km": 18.0, "min_signal_dbm": -108.0},
        "environment": {"clutter_height_m": 0.0, "use_buildings": True},
    },

    # ── Cellular / LTE ──────────────────────────────────────────────────────
    {
        "id": "lte-b12-sector", "name": "LTE band 12 (700 MHz) eNodeB · 20 W/sector, 3× 90°",
        "transmitter": {"frequency_hz": 740e6, "power_dbm": 43.0, "height_m": 30.0,
                        "altitude_m": 0.0, "bandwidth_hz": 10e6, "noise_dbm": -104.0},
        "antenna": {"type": "sector_90", "gain_dbi": 15.0, "azimuth_deg": [0, 120, 240],
                    "tilt_deg": -4.0, "polarization": "vertical"},
        "receiver": {"height_m": 1.5, "gain_dbi": 0.0, "sensitivity_dbm": -100.0},
        "model": {"propagation_model": "itm", "diffraction_model": "deygout",
                  "radius_km": 35.0, "min_signal_dbm": -110.0},
        "environment": {"clutter_height_m": 0.0, "use_buildings": True},
    },

    # ── Wi-Fi / ISM ─────────────────────────────────────────────────────────
    {
        "id": "wifi-ap-2g4-sector", "name": "Wi-Fi 2.4 GHz AP · 1 W EIRP, 3× 120° sectors",
        "transmitter": {"frequency_hz": 2400e6, "power_dbm": 30.0, "height_m": 10.0,
                        "altitude_m": 0.0, "bandwidth_hz": 20e6, "noise_dbm": -95.0},
        "antenna": {"type": "sector_120", "gain_dbi": 14.0, "azimuth_deg": [0, 120, 240],
                    "tilt_deg": -2.0, "polarization": "vertical"},
        "receiver": {"height_m": 1.5, "gain_dbi": 2.0, "sensitivity_dbm": -85.0},
        "model": {"propagation_model": "itm", "diffraction_model": "bullington",
                  "radius_km": 3.0, "min_signal_dbm": -85.0},
        "environment": {"clutter_height_m": 0.0, "use_buildings": True},
    },
    {
        "id": "wifi-ptp-5g-dish", "name": "Wi-Fi 5 GHz PtP link · 30 dBm + 25 dBi dish",
        "transmitter": {"frequency_hz": 5800e6, "power_dbm": 30.0, "height_m": 15.0,
                        "altitude_m": 0.0, "bandwidth_hz": 40e6, "noise_dbm": -92.0},
        "antenna": {"type": "parabolic_dish", "gain_dbi": 25.0, "azimuth_deg": [0],
                    "tilt_deg": 0.0, "polarization": "vertical"},
        "receiver": {"height_m": 15.0, "gain_dbi": 25.0, "sensitivity_dbm": -85.0},
        "model": {"propagation_model": "itm", "diffraction_model": "deygout",
                  "radius_km": 20.0, "min_signal_dbm": -80.0},
        "environment": {"clutter_height_m": 0.0, "use_buildings": False},
    },
    {
        "id": "lora-915-iot", "name": "LoRa gateway · 14 dBm @ 915 MHz, omni 3 dBi",
        "transmitter": {"frequency_hz": 915e6, "power_dbm": 14.0, "height_m": 6.0,
                        "altitude_m": 0.0, "bandwidth_hz": 125000.0, "noise_dbm": -119.0},
        "antenna": {"type": "dipole_half_wave", "gain_dbi": 3.0, "azimuth_deg": [0],
                    "tilt_deg": 0.0, "polarization": "vertical"},
        "receiver": {"height_m": 1.5, "gain_dbi": 2.0, "sensitivity_dbm": -137.0},
        "model": {"propagation_model": "itm", "diffraction_model": "deygout",
                  "radius_km": 12.0, "min_signal_dbm": -130.0},
        "environment": {"clutter_height_m": 0.0, "use_buildings": True},
    },

    # ── UAS / Drone ─────────────────────────────────────────────────────────
    {
        "id": "uas-c2-2g4", "name": "UAS C2 link · 100 mW EIRP @ 2.4 GHz (airborne, 120 m AGL)",
        "transmitter": {"frequency_hz": 2440e6, "power_dbm": 20.0, "height_m": 120.0,
                        "altitude_m": 120.0, "bandwidth_hz": 10e6, "noise_dbm": -95.0},
        "antenna": {"type": "patch", "gain_dbi": 5.0, "azimuth_deg": [0],
                    "tilt_deg": -90.0, "polarization": "vertical"},
        "receiver": {"height_m": 1.5, "gain_dbi": 5.0, "sensitivity_dbm": -90.0},
        "model": {"propagation_model": "itm", "diffraction_model": "deygout",
                  "radius_km": 8.0, "min_signal_dbm": -90.0},
        "environment": {"clutter_height_m": 0.0, "use_buildings": False},
    },
    {
        "id": "uas-video-5g8", "name": "UAS video downlink · 500 mW @ 5.8 GHz (airborne)",
        "transmitter": {"frequency_hz": 5800e6, "power_dbm": 27.0, "height_m": 120.0,
                        "altitude_m": 120.0, "bandwidth_hz": 20e6, "noise_dbm": -92.0},
        "antenna": {"type": "patch", "gain_dbi": 6.0, "azimuth_deg": [0],
                    "tilt_deg": -90.0, "polarization": "vertical"},
        "receiver": {"height_m": 1.5, "gain_dbi": 8.0, "sensitivity_dbm": -85.0},
        "model": {"propagation_model": "itm", "diffraction_model": "deygout",
                  "radius_km": 6.0, "min_signal_dbm": -85.0},
        "environment": {"clutter_height_m": 0.0, "use_buildings": False},
    },

    # ── Aviation / surveillance ─────────────────────────────────────────────
    {
        "id": "adsb-aircraft-1090", "name": "ADS-B aircraft transponder · 25 W @ 1090 MHz (FL100)",
        "transmitter": {"frequency_hz": 1090e6, "power_dbm": 44.0, "height_m": 3000.0,
                        "altitude_m": 3000.0, "bandwidth_hz": 2.6e6, "noise_dbm": -100.0},
        "antenna": {"type": "monopole_quarter_wave", "gain_dbi": 0.0, "azimuth_deg": [0],
                    "tilt_deg": 0.0, "polarization": "vertical"},
        "receiver": {"height_m": 5.0, "gain_dbi": 3.0, "sensitivity_dbm": -90.0},
        "model": {"propagation_model": "itm", "diffraction_model": "deygout",
                  "radius_km": 300.0, "min_signal_dbm": -100.0},
        "environment": {"clutter_height_m": 0.0, "use_buildings": False},
    },

    # ── Backhaul / satcom ───────────────────────────────────────────────────
    {
        "id": "mw-backhaul-11g", "name": "Microwave backhaul · 1 W @ 11 GHz, 1.2 m dish",
        "transmitter": {"frequency_hz": 11.5e9, "power_dbm": 30.0, "height_m": 30.0,
                        "altitude_m": 0.0, "bandwidth_hz": 28e6, "noise_dbm": -100.0},
        "antenna": {"type": "parabolic_dish", "gain_dbi": 38.0, "azimuth_deg": [0],
                    "tilt_deg": 0.0, "polarization": "vertical"},
        "receiver": {"height_m": 30.0, "gain_dbi": 38.0, "sensitivity_dbm": -85.0},
        "model": {"propagation_model": "itm", "diffraction_model": "deygout",
                  "radius_km": 40.0, "min_signal_dbm": -80.0},
        "environment": {"clutter_height_m": 0.0, "use_buildings": False},
    },
    {
        "id": "iridium-handheld-1g6", "name": "Iridium satphone uplink · 7 W @ 1622 MHz",
        "transmitter": {"frequency_hz": 1622e6, "power_dbm": 38.0, "height_m": 1.6,
                        "altitude_m": 0.0, "bandwidth_hz": 31500.0, "noise_dbm": -118.0},
        "antenna": {"type": "patch", "gain_dbi": 3.0, "azimuth_deg": [0],
                    "tilt_deg": 90.0, "polarization": "circular"},
        "receiver": {"height_m": 780000.0, "gain_dbi": 3.0, "sensitivity_dbm": -120.0},
        "model": {"propagation_model": "itm", "diffraction_model": "deygout",
                  "radius_km": 2.0, "min_signal_dbm": -120.0},
        "environment": {"clutter_height_m": 0.0, "use_buildings": False},
    },
]


def _seed_if_empty() -> None:
    """Seed any built-in templates whose ids aren't already on disk. User-created and
    user-edited templates are never overwritten — only missing ids are filled in. This
    keeps existing installs in sync as the built-in list grows.

    Note: a built-in template the user has intentionally deleted will be re-created on
    next startup. If that becomes annoying we'll add a seeded-version marker."""
    on_disk = {p.stem for p in TEMPLATES_DIR.glob("*.json")}
    for t in _SEEDS:
        if _safe_id(t["id"]) not in on_disk:
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
