"""
core/targets — per-identifier observation store and peak-RSSI / range-finding
machinery. The tracker keys every observation by a (kind, value) pair like
('imsi', '310410123456789'), ('mac', 'aa:bb:cc:dd:ee:ff'), ('icao', 'A1B2C3'),
('uas_serial', '1581FXYZ'), etc., independent of frequency.

Public API lives in `tracker.py` (re-exported here for convenience):

    from app.core.targets import record, get, list_targets, forget

Every cellular / WiFi / BLE / DF feeder calls ``record(...)`` with whatever
identifier + RSSI + observer-position it has. The tracker maintains running
peaks, a top-K of best observations, and a range/position estimate that
auto-upgrades from single-pose Friis to multi-pose RSS-ML to AoA-fused ML
grid as enough observations accumulate.
"""
from __future__ import annotations

from .tracker import (
    Target, Observation, TargetTracker, tracker,
    IDENTIFIER_KINDS,
    record, get, list_targets, query, forget, snapshot,
    estimate_range, estimate_position,
    register_listener, unregister_listener,
)

__all__ = [
    "Target", "Observation", "TargetTracker", "tracker",
    "IDENTIFIER_KINDS",
    "record", "get", "list_targets", "query", "forget", "snapshot",
    "estimate_range", "estimate_position",
    "register_listener", "unregister_listener",
]
