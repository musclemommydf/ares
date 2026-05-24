# SPDX-License-Identifier: MIT OR Apache-2.0
# Copyright (c) 2026 Ares

"""
OSINT feed import (Workstream A.4).

Pull live open-source-intelligence mapping data — DeepState Map, GDELT, live
ADS-B aircraft, NASA FIRMS fires, ACLED conflict events, ship AIS, LiveUAMap,
Signal Cockpit, or any GeoJSON/KML/GeoRSS/GPX URL — normalise each to GeoJSON,
filter it (source query → bbox clip → hard cap) so the browser never gets a
firehose, cache it for offline use, and render it as a normal toggleable map layer.
"""
from .feeds import (
    list_feeds, fetch_feed, get_cached, add_custom_feed, remove_feed,
    set_config, BUILTIN_FEEDS,
)

__all__ = [
    "list_feeds", "fetch_feed", "get_cached", "add_custom_feed", "remove_feed",
    "set_config", "BUILTIN_FEEDS",
]
