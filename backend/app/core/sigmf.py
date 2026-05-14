"""
SigMF metadata writer + reader.

SigMF (Signal Metadata Format, https://sigmf.org) is the de-facto interop
format for IQ captures — supported by PySDR, SDRAngel, Inspectrum, GNU Radio,
and most off-line analysis tools. Pairing every Ares IQ capture with a
``.sigmf-meta`` makes those captures load anywhere.

Spec version 1.0.0 ("v1.0.0"). Fields we populate:
  global:
    core:datatype        — "cf32_le" (complex64 little-endian)
    core:sample_rate     — Hz
    core:hw              — driver name + serial
    core:author          — Ares
    core:version         — SigMF spec version
    core:datetime        — capture start ISO-8601
    core:geolocation     — GeoJSON Point with [lon, lat, alt_m]
    core:extensions      — ["antenna:1.0.0"]
    antenna:gain         — dBi
    antenna:type         — UCA/ULA/etc.
    antenna:array        — element positions
  captures:
    [{ core:sample_start: 0, core:frequency: Hz, core:datetime: ISO-8601 }]
  annotations:
    operator-supplied or auto-generated (e.g. detected emitters)
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


SIGMF_VERSION = "1.0.0"


def make_sigmf_meta(
    iq_path: str | Path,
    *,
    sample_rate_hz: float,
    center_freq_hz: float,
    datatype: str = "cf32_le",
    hardware: str = "ares",
    author: str = "Ares",
    geolocation: Optional[dict] = None,           # {"lat":..., "lon":..., "altitude_m":...}
    antenna: Optional[dict] = None,               # {"gain_dbi": ..., "type": "uca", "array": ...}
    annotations: Optional[list[dict]] = None,
    extra_global: Optional[dict] = None,
    capture_start_iso: Optional[str] = None,
) -> dict:
    """Build a SigMF meta dict for an IQ file."""
    glob: dict = {
        "core:datatype":     datatype,
        "core:sample_rate":  float(sample_rate_hz),
        "core:hw":           hardware,
        "core:author":       author,
        "core:version":      SIGMF_VERSION,
        "core:datetime":     capture_start_iso or datetime.now(timezone.utc).isoformat(),
    }
    if geolocation:
        glob["core:geolocation"] = {
            "type": "Point",
            "coordinates": [
                float(geolocation.get("lon", 0.0)),
                float(geolocation.get("lat", 0.0)),
                float(geolocation.get("altitude_m", 0.0)),
            ],
        }
    if antenna:
        glob["core:extensions"] = [{"name": "antenna", "version": "1.0.0", "optional": False}]
        if "gain_dbi"  in antenna: glob["antenna:gain"]   = float(antenna["gain_dbi"])
        if "type"      in antenna: glob["antenna:type"]   = antenna["type"]
        if "array"     in antenna: glob["antenna:array"]  = antenna["array"]
    if extra_global:
        glob.update(extra_global)
    return {
        "global": glob,
        "captures": [{
            "core:sample_start": 0,
            "core:frequency":    float(center_freq_hz),
            "core:datetime":     capture_start_iso or datetime.now(timezone.utc).isoformat(),
        }],
        "annotations": list(annotations or []),
    }


def write_sigmf_meta(iq_path: str | Path, meta: dict) -> Path:
    """Write `iq_path`.sigmf-meta beside the IQ file. SigMF requires the IQ
    file to use the `.sigmf-data` extension; we accept either the data path
    or the *meta* path and normalise."""
    p = Path(iq_path)
    if p.suffix == ".sigmf-meta":
        meta_path = p
    elif p.suffix == ".sigmf-data":
        meta_path = p.with_suffix(".sigmf-meta")
    else:
        # Arbitrary IQ extension — emit beside it.
        meta_path = p.with_suffix(p.suffix + ".sigmf-meta")
    meta_path.write_text(json.dumps(meta, indent=2))
    return meta_path


def read_sigmf_meta(meta_path: str | Path) -> dict:
    return json.loads(Path(meta_path).read_text())
