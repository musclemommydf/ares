# SPDX-License-Identifier: MIT OR Apache-2.0
# Copyright (c) 2026 Ares

"""
Track-and-archive — every confirmed track gets its bearing history + any
IQ-capture pointers recorded into `data/df_state/track_archive/{id}.json`.

Append-only per track. The Mission Package exporter pulls these in so a
.ares-mission file is self-contained for off-line replay.

Format (one JSON file per track):
{
  "track_id": "...",
  "created_t": <epoch>,
  "updated_t": <epoch>,
  "frequency_hz": ...,
  "observations": [
    { "t": ..., "lat": ..., "lon": ..., "azimuth_deg": ..., "rssi_dbm": ..., "device_id": ... }
  ],
  "positions": [
    { "t": ..., "lat": ..., "lon": ..., "cep_m": ..., "confidence": ..., "v_mps": [vx, vy] }
  ],
  "iq_captures": [ { "t": ..., "sigmf_path": "..." } ],
  "audio_captures": [ { "t": ..., "wav_path": "...", "meta_path": "..." } ],
  "modulation_history": [ { "t": ..., "label": "FM", "confidence": 0.85 } ]
}
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Optional


def _archive_dir() -> Path:
    p = Path(os.environ.get("ARES_DATA_DIR", "data")) / "df_state" / "track_archive"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _path(track_id: str) -> Path:
    safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in str(track_id))[:64]
    return _archive_dir() / f"{safe or 'track'}.json"


def _load(track_id: str) -> dict:
    p = _path(track_id)
    if p.exists():
        try: return json.loads(p.read_text())
        except Exception: pass
    return {
        "track_id": track_id, "created_t": time.time(), "updated_t": time.time(),
        "frequency_hz": 0, "observations": [], "positions": [],
        "iq_captures": [], "audio_captures": [], "modulation_history": [],
    }


def _save(track_id: str, data: dict) -> None:
    data["updated_t"] = time.time()
    _path(track_id).write_text(json.dumps(data, indent=2))


def record_observation(track_id: str, *, t: float, lat: float, lon: float,
                        azimuth_deg: float, rssi_dbm: Optional[float] = None,
                        device_id: str = "", frequency_hz: float = 0) -> None:
    d = _load(track_id)
    d["observations"].append({"t": t, "lat": lat, "lon": lon, "azimuth_deg": azimuth_deg,
                                "rssi_dbm": rssi_dbm, "device_id": device_id})
    if frequency_hz: d["frequency_hz"] = frequency_hz
    _save(track_id, d)


def record_position(track_id: str, *, t: float, lat: float, lon: float,
                     cep_m: float, confidence: float = 0.0,
                     velocity_mps: Optional[dict] = None,
                     frequency_hz: float = 0) -> None:
    d = _load(track_id)
    d["positions"].append({"t": t, "lat": lat, "lon": lon, "cep_m": cep_m,
                            "confidence": confidence,
                            "v_mps": [(velocity_mps or {}).get("vx", 0.0),
                                       (velocity_mps or {}).get("vy", 0.0)]})
    if frequency_hz: d["frequency_hz"] = frequency_hz
    _save(track_id, d)


def attach_iq(track_id: str, sigmf_path: str, t: Optional[float] = None) -> None:
    d = _load(track_id)
    d["iq_captures"].append({"t": t or time.time(), "sigmf_path": str(sigmf_path)})
    _save(track_id, d)


def attach_audio(track_id: str, wav_path: str, meta_path: str, t: Optional[float] = None) -> None:
    d = _load(track_id)
    d["audio_captures"].append({"t": t or time.time(), "wav_path": str(wav_path),
                                  "meta_path": str(meta_path)})
    _save(track_id, d)


def attach_modulation(track_id: str, label: str, confidence: float = 0.0,
                       t: Optional[float] = None) -> None:
    d = _load(track_id)
    d["modulation_history"].append({"t": t or time.time(), "label": label,
                                      "confidence": float(confidence)})
    _save(track_id, d)


def get_archive(track_id: str) -> Optional[dict]:
    p = _path(track_id)
    return json.loads(p.read_text()) if p.exists() else None


def list_tracks() -> list[dict]:
    out = []
    for p in sorted(_archive_dir().glob("*.json")):
        try:
            d = json.loads(p.read_text())
            out.append({"track_id": d["track_id"],
                          "n_observations": len(d.get("observations", [])),
                          "n_positions": len(d.get("positions", [])),
                          "frequency_hz": d.get("frequency_hz", 0),
                          "updated_t": d.get("updated_t", 0)})
        except Exception:
            pass
    return out


def remove_track(track_id: str) -> bool:
    p = _path(track_id)
    if p.exists():
        p.unlink(); return True
    return False
