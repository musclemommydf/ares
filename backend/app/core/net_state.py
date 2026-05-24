# SPDX-License-Identifier: MIT OR Apache-2.0
# Copyright (c) 2026 Ares

"""
Ares — network state & graceful degradation (Workstream A.3).

When the box has internet, services behave as today (live NOAA SWPC space
weather, weather APIs, Overpass building queries). When it doesn't (air-gapped /
field), those calls fail — this module remembers the last good value, hands it
back with a ``stale`` flag, and lets the operator enter a manual override. It
also owns the cheap cached online/offline probe used by ``/server/info``.

Persisted to ``data/.net_cache.json`` so a freshly-restarted offline box still
has the last values it saw.
"""
from __future__ import annotations

import json
import socket
import time
from pathlib import Path
from typing import Any, Optional

from app.config import settings, DATA_DIR

_CACHE_FILE = DATA_DIR / ".net_cache.json"
_ONLINE_TTL_S = 30.0
_PROBE_HOSTS = (("services.swpc.noaa.gov", 443), ("1.1.1.1", 53))

_state: dict[str, Any] = {"online": None, "online_ts": 0.0, "last_known": {}, "overrides": {}}


def _load() -> None:
    if _CACHE_FILE.exists():
        try:
            d = json.loads(_CACHE_FILE.read_text())
            _state["last_known"] = d.get("last_known", {})
            _state["overrides"] = d.get("overrides", {})
        except Exception:
            pass


def _save() -> None:
    try:
        _CACHE_FILE.write_text(json.dumps(
            {"last_known": _state["last_known"], "overrides": _state["overrides"]}, indent=2))
    except OSError:
        pass


_load()


# ── online probe ─────────────────────────────────────────────────────────────
def is_online(force: bool = False) -> Optional[bool]:
    if settings.network_policy == "offline_only":
        return False
    if settings.network_policy == "online_only":
        return True
    now = time.time()
    if not force and _state["online"] is not None and now - _state["online_ts"] < _ONLINE_TTL_S:
        return _state["online"]
    ok = False
    for host, port in _PROBE_HOSTS:
        try:
            with socket.create_connection((host, port), timeout=1.0):
                ok = True
                break
        except OSError:
            continue
    _state["online"] = ok
    _state["online_ts"] = now
    return ok


# ── last-known values ────────────────────────────────────────────────────────
def record(kind: str, data: Any) -> None:
    _state["last_known"][kind] = {"data": data, "ts": time.time(),
                                  "iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    _save()


def last_known(kind: str) -> Optional[dict]:
    return _state["last_known"].get(kind)


def set_override(kind: str, data: Optional[Any]) -> None:
    if data is None:
        _state["overrides"].pop(kind, None)
    else:
        _state["overrides"][kind] = {"data": data, "iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    _save()


def get_override(kind: str) -> Optional[dict]:
    return _state["overrides"].get(kind)


async def fetch_or_degrade(kind: str, fetcher) -> dict:
    """Run ``await fetcher()`` (returns JSON-able data). On success cache it and
    return ``{"data": ..., "source": "live", "stale": False}``. On failure return
    the operator override if set, else the last-known value (``stale``), else raise.
    """
    ov = get_override(kind)
    if ov is not None:
        # an explicit operator override always wins (e.g. exercise / known conditions)
        return {"data": ov["data"], "source": "override", "stale": False, "as_of": ov["iso"]}
    try:
        data = await fetcher()
        record(kind, data)
        return {"data": data, "source": "live", "stale": False,
                "as_of": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    except Exception as e:
        lk = last_known(kind)
        if lk is not None:
            return {"data": lk["data"], "source": "cache", "stale": True, "as_of": lk["iso"],
                    "error": f"{type(e).__name__}: {e}"}
        raise


def status() -> dict:
    return {
        "online": is_online(),
        "network_policy": settings.network_policy,
        "last_known": {k: {"as_of": v.get("iso"), "ts": v.get("ts")} for k, v in _state["last_known"].items()},
        "overrides": {k: v.get("iso") for k, v in _state["overrides"].items()},
    }
