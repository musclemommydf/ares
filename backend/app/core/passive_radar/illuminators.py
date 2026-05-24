# SPDX-License-Identifier: MIT OR Apache-2.0
# Copyright (c) 2026 Ares

"""
Illuminators-of-opportunity catalog for passive radar.

The catalog ranks broadcast services by suitability:
  - DAB / DAB+    — wideband (1.5 MHz), continuous, ITU-R BS.1660 references.
  - DVB-T / DVB-T2 — 7.6 MHz channels, ETSI EN 300 744 OFDM, excellent for PBR.
  - FM           — narrow (0.2 MHz) but everywhere; modest range resolution.
  - LTE / 5G NR  — high duty cycle, good resolution; needs the right cell.

Lookup by approximate location (returns regional muxes/channels). Real-world
deployments should consult a local frequency database (CONELRAD, FMSCAN,
ofcom, FCC etc.); this is a seed catalog that's enough to bootstrap a
demonstration in common European/US locations.
"""

from __future__ import annotations


# Lightweight seed — region-keyed presets. Extend via the API or a CSV import.
CATALOG: dict[str, list[dict]] = {
    "europe_dab": [
        {"name": "DAB Block 12B", "freq_hz": 225_648_000, "bw_hz": 1_536_000, "mode": "DAB"},
        {"name": "DAB Block 11B", "freq_hz": 218_640_000, "bw_hz": 1_536_000, "mode": "DAB"},
        {"name": "DAB Block 10D", "freq_hz": 213_360_000, "bw_hz": 1_536_000, "mode": "DAB"},
    ],
    "europe_dvbt": [
        {"name": "DVB-T UHF 23", "freq_hz": 490_000_000, "bw_hz": 8_000_000, "mode": "DVB-T"},
        {"name": "DVB-T UHF 26", "freq_hz": 514_000_000, "bw_hz": 8_000_000, "mode": "DVB-T"},
        {"name": "DVB-T UHF 30", "freq_hz": 546_000_000, "bw_hz": 8_000_000, "mode": "DVB-T"},
    ],
    "europe_fm": [
        {"name": "BBC R1 (UK)", "freq_hz":  98_500_000, "bw_hz": 200_000, "mode": "FM"},
        {"name": "Generic FM 100.3", "freq_hz": 100_300_000, "bw_hz": 200_000, "mode": "FM"},
    ],
    "us_atsc": [
        {"name": "ATSC UHF 14", "freq_hz": 473_000_000, "bw_hz": 6_000_000, "mode": "ATSC"},
        {"name": "ATSC UHF 20", "freq_hz": 509_000_000, "bw_hz": 6_000_000, "mode": "ATSC"},
        {"name": "ATSC UHF 30", "freq_hz": 569_000_000, "bw_hz": 6_000_000, "mode": "ATSC"},
    ],
    "us_fm": [
        {"name": "FM 88.5 (NPR-ish)", "freq_hz":  88_500_000, "bw_hz": 200_000, "mode": "FM"},
        {"name": "FM 96.3",           "freq_hz":  96_300_000, "bw_hz": 200_000, "mode": "FM"},
        {"name": "FM 104.5",          "freq_hz": 104_500_000, "bw_hz": 200_000, "mode": "FM"},
    ],
}


def list_regions() -> list[str]:
    return sorted(CATALOG.keys())


def list_illuminators(region: str | None = None) -> list[dict]:
    if region:
        return list(CATALOG.get(region, []))
    out = []
    for k, items in CATALOG.items():
        for it in items:
            out.append({**it, "region": k})
    return out


def best_for(freq_hz: float, region: str | None = None) -> dict | None:
    """Pick the catalog entry nearest in frequency to the given target frequency."""
    items = list_illuminators(region)
    if not items:
        return None
    return min(items, key=lambda it: abs(it["freq_hz"] - freq_hz))
