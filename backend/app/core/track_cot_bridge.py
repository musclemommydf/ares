# SPDX-License-Identifier: MIT OR Apache-2.0
# Copyright (c) 2026 Ares

"""
Continuous track-to-CoT publisher.

Existing `app.core.cot` publishes a CoT event on every fresh LoB / fix. This
module fills the gap for the new emitter trackers (Kalman per-track + GM-PHD):
periodically iterate every active track and publish a fresh CoT so other
ATAK users see the track *persist* and move, even when the tracker doesn't
emit a fresh fix.

Heartbeat is configurable. Each track is published with:
  uid     = "ares-track-{track_id}"
  type    = "a-u-G-U-C-I"  (unknown ground, unidentified)
  cep_m   = track's CEP (1.1774 σ_pos)
  course  = atan2(vx, vy) deg if speed > 1 m/s, else absent
  speed   = √(vx²+vy²) m/s
  stale   = stale_s into the future

Run as `asyncio.create_task(track_cot_bridge.run())` from the application
lifespan; cancel on shutdown.
"""

from __future__ import annotations

import asyncio
import logging
import math
import xml.etree.ElementTree as ET
from typing import Callable, Optional

from . import cot as cot_mod

log = logging.getLogger(__name__)


# Track sources we can pull from — pluggable. Each must implement:
#   `serialise() → list[dict]` returning [{ id, lat, lon, cep_m, velocity_mps?, frequency_hz?, … }]
SOURCES: list[Callable[[], list[dict]]] = []


def register_source(callable_returning_tracks: Callable[[], list[dict]]) -> None:
    """Register an additional tracker source (e.g. GM-PHD instance, custom fuser)."""
    if callable_returning_tracks not in SOURCES:
        SOURCES.append(callable_returning_tracks)


def unregister_source(callable_returning_tracks: Callable[[], list[dict]]) -> None:
    if callable_returning_tracks in SOURCES:
        SOURCES.remove(callable_returning_tracks)


def track_to_cot(tr: dict, stale_s: float = 30.0) -> bytes:
    """Build a CoT event for one track."""
    uid = f"ares-track-{tr.get('id', 'unknown')}"
    lat = float(tr["lat"]); lon = float(tr["lon"])
    cep = float(tr.get("cep_m") or 250.0)
    vx = float((tr.get("velocity_mps") or {}).get("vx", 0.0))
    vy = float((tr.get("velocity_mps") or {}).get("vy", 0.0))
    speed = math.sqrt(vx * vx + vy * vy)
    root = cot_mod._event(uid, "a-u-G-U-C-I", lat, lon, ce_m=cep, stale_s=stale_s)
    detail = ET.SubElement(root, "detail")
    freq = tr.get("frequency_hz") or 0
    cs_extra = f" {freq/1e6:.3f}MHz" if freq else ""
    ET.SubElement(detail, "contact", {"callsign": f"Ares Track{cs_extra}"})
    if speed > 0.5:
        course = (math.degrees(math.atan2(vx, vy)) + 360) % 360
        ET.SubElement(detail, "track", {"speed": f"{speed:.2f}", "course": f"{course:.2f}"})
    ET.SubElement(detail, "remarks").text = (
        f"Ares track · conf={tr.get('confidence', tr.get('weight', 0)):.2f}"
        f" · obs={tr.get('n_obs', '?')} · CEP {cep:.0f} m"
    )
    ET.SubElement(detail, "takv", {"platform": "Ares", "device": "ares-server", "version": "1.1"})
    ET.SubElement(detail, "color", {"argb": "-65536"})           # red
    ET.SubElement(detail, "__group", {"name": "Red", "role": "Team Member"})
    return b'<?xml version="1.0" encoding="UTF-8"?>' + ET.tostring(root)


async def publish_once() -> int:
    """Pull all tracks from registered sources and publish CoT. Returns count."""
    n = 0
    for src in SOURCES:
        try:
            tracks = src() or []
        except Exception:
            log.exception("track source failed")
            continue
        for tr in tracks:
            try:
                payload = track_to_cot(tr)
                await cot_mod._send_all(payload)
                n += 1
            except Exception:
                log.exception("track CoT publish failed for %s", tr.get("id"))
    return n


async def run(interval_s: float = 2.0) -> None:
    """Heartbeat loop. Cancellable."""
    while True:
        try:
            await publish_once()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("track_cot_bridge tick failed")
        await asyncio.sleep(interval_s)


# Auto-register the bundled trackers so the bridge "just works" once started.
def _register_default_sources() -> None:
    try:
        from .df.tracker import get_tracker
        register_source(lambda: get_tracker().serialise())
    except Exception:
        log.warning("Kalman tracker not registered with track_cot_bridge", exc_info=True)
    try:
        from .df.gmphd import get_gmphd
        register_source(lambda: get_gmphd().serialise())
    except Exception:
        log.warning("GM-PHD tracker not registered with track_cot_bridge", exc_info=True)


_register_default_sources()
