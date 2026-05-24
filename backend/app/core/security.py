# SPDX-License-Identifier: MIT OR Apache-2.0
# Copyright (c) 2026 Ares

"""
security.py — a rate-limit middleware + a lightweight audit log (security pass).

* **Rate limiting** — a per-client-IP token bucket on ``/api/v1/*`` (a generous
  default, a tighter bucket for the expensive ``/simulate/*`` and ``/packs/download``
  endpoints), returning ``429`` when exceeded. WebSocket upgrades, ``/``, ``/health``,
  ``/docs``, ``/redoc`` are exempt. In-memory; resets on restart; tune with
  ``ARES_RATE_LIMIT`` / ``ARES_RATE_LIMIT_SIM`` (requests/second; ``0`` disables).
* **Audit log** — ``audit(event, **fields)`` appends one JSON line to
  ``data/audit.log`` (size-rotated to ``audit.log.1``): logins, device add/remove/
  calibrate, mesh-peer add/remove, CoT-target / ATAK-toggle changes, coverage runs.
  Best-effort — a write failure is swallowed (never breaks a request).
"""
from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Optional

from app.config import DATA_DIR

log = logging.getLogger(__name__)

_RATE = float(os.getenv("ARES_RATE_LIMIT", "25"))        # req/s per IP (burst = 4×), 0 ⇒ off
_RATE_SIM = float(os.getenv("ARES_RATE_LIMIT_SIM", "2")) # req/s per IP for /simulate, /packs/download
_BURST_MULT = 4.0
_EXEMPT_PREFIXES = ("/", "/health", "/docs", "/redoc", "/openapi.json")
_SIM_PREFIXES = ("/api/v1/simulate/", "/api/v1/packs/download")
_AUDIT_FILE = DATA_DIR / "audit.log"
_AUDIT_MAX = 4 * 1024 * 1024                              # rotate at 4 MB

# bucket state: ip -> {"sim"|"gen": (tokens, last_ts)}
_BUCKETS: dict[str, dict] = {}


def _take(ip: str, key: str, rate: float) -> bool:
    if rate <= 0:
        return True
    cap = max(1.0, rate * _BURST_MULT)
    now = time.monotonic()
    b = _BUCKETS.setdefault(ip, {})
    tokens, last = b.get(key, (cap, now))
    tokens = min(cap, tokens + (now - last) * rate)
    if tokens < 1.0:
        b[key] = (tokens, now)
        return False
    b[key] = (tokens - 1.0, now)
    return True


async def rate_limit_middleware(request, call_next):
    path = request.url.path
    if request.scope.get("type") == "websocket" or not path.startswith("/api/v1/") or path in _EXEMPT_PREFIXES:
        return await call_next(request)
    ip = (request.client.host if request.client else "?") or "?"
    is_sim = path.startswith(_SIM_PREFIXES)
    ok = _take(ip, "sim", _RATE_SIM) if is_sim else _take(ip, "gen", _RATE)
    if not ok:
        from fastapi.responses import JSONResponse
        return JSONResponse({"detail": "rate limit exceeded — slow down"}, status_code=429,
                            headers={"Retry-After": "1"})
    return await call_next(request)


def _rotate_if_big() -> None:
    try:
        if _AUDIT_FILE.exists() and _AUDIT_FILE.stat().st_size > _AUDIT_MAX:
            bak = _AUDIT_FILE.with_suffix(".log.1")
            try:
                bak.unlink()
            except OSError:
                pass
            _AUDIT_FILE.rename(bak)
    except OSError:
        pass


def audit(event: str, **fields) -> None:
    """Append one JSON line to data/audit.log. Best-effort."""
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        _rotate_if_big()
        line = json.dumps({"t": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                           "ts": time.time(), "event": event, **fields}, default=str)
        with open(_AUDIT_FILE, "a") as f:
            f.write(line + "\n")
    except Exception:
        log.debug("audit write failed", exc_info=True)
