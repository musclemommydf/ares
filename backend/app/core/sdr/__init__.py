# SPDX-License-Identifier: MIT OR Apache-2.0
# Copyright (c) 2026 Ares

"""SDR / direction-finding integration (Workstream D).

Connect physical DF radios (KrakenSDR, Epiq Matchstiq X40, or any external DF
pipeline pushing JSON-lines) → server-side LoB aggregator + geolocation solver
→ live CoT (to ATAK / TAK Server) + WebSocket events (to the web / desktop
globe) + optional auto-coverage simulation from each new fix.
"""
from .manager import SDRManager, SDRDevice, sdr_manager
from .adapters import KrakenSdrAdapter, GenericJsonLinesAdapter, MatchstiqX40Adapter

__all__ = ["SDRManager", "SDRDevice", "sdr_manager",
           "KrakenSdrAdapter", "GenericJsonLinesAdapter", "MatchstiqX40Adapter"]
