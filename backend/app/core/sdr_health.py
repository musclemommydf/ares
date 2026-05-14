"""
SDR health monitor.

Per-device health snapshot:
  - temperature (°C), reported by the driver if available
  - ADC clipping fraction (0..1) — share of recent samples saturating ±1.0
  - USB / Ethernet throughput (last second, MB/s) — best-effort from driver
  - GPS lock + satellite count from the node's gpsd
  - last-frame age in seconds (heartbeat)
  - dropouts in the last minute

For most SDRs (RTL-SDR, ANTSDR, USRP, Matchstiq) some fields are
unavailable — we return them as None and the UI shows "—". Drivers can
override `health()` on `SdrDriver` to report richer info.
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field
from typing import Optional

import numpy as np


@dataclass
class _DeviceState:
    last_frame_t: float = 0.0
    clipped_samples: int = 0
    total_samples: int = 0
    dropouts_last_min: deque = field(default_factory=lambda: deque(maxlen=600))
    last_throughput_bytes: int = 0
    last_throughput_t: float = field(default_factory=time.time)


_STATES: dict[str, _DeviceState] = {}


def record_frame(device_id: str, iq: np.ndarray, n_dropped_since_last: int = 0) -> None:
    """Update health stats from a fresh IQ frame. Call from each driver's read_iq path."""
    s = _STATES.setdefault(device_id, _DeviceState())
    now = time.time()
    s.last_frame_t = now
    a = np.abs(iq)
    s.clipped_samples += int((a >= 0.97).sum())
    s.total_samples += int(a.size)
    if s.total_samples > 1_000_000:
        # Decay so old clipping doesn't dominate.
        s.clipped_samples = int(s.clipped_samples * 0.5)
        s.total_samples = int(s.total_samples * 0.5)
    s.last_throughput_bytes += int(iq.nbytes)
    if n_dropped_since_last:
        s.dropouts_last_min.append((now, int(n_dropped_since_last)))


def record_dropout(device_id: str, n: int = 1) -> None:
    _STATES.setdefault(device_id, _DeviceState()).dropouts_last_min.append((time.time(), int(n)))


def status(device_id: str, *, driver_self_report: Optional[dict] = None) -> dict:
    """Return a one-shot snapshot for `device_id`. Optionally fold in fields
    the driver knows (temperature, gain headroom etc.)."""
    s = _STATES.get(device_id)
    now = time.time()
    out: dict = {"device_id": device_id, "ok": True, "last_frame_age_s": None,
                  "clip_fraction": None, "dropouts_last_minute": 0,
                  "throughput_mbps": None, "temperature_c": None,
                  "gps": _gps_status_compact(),
                  "time_sync": None}
    if s is not None:
        if s.last_frame_t:
            out["last_frame_age_s"] = round(now - s.last_frame_t, 2)
            out["ok"] = out["ok"] and (now - s.last_frame_t) < 5.0
        if s.total_samples > 0:
            out["clip_fraction"] = round(s.clipped_samples / s.total_samples, 4)
            out["ok"] = out["ok"] and out["clip_fraction"] < 0.05
        # 1-minute dropout count
        cutoff = now - 60.0
        recent = [n for (t, n) in s.dropouts_last_min if t >= cutoff]
        out["dropouts_last_minute"] = sum(recent)
        if out["dropouts_last_minute"] > 5:
            out["ok"] = False
        # Throughput (instantaneous)
        if now - s.last_throughput_t >= 1.0:
            out["throughput_mbps"] = round((s.last_throughput_bytes / 1_000_000) /
                                           (now - s.last_throughput_t), 2)
            s.last_throughput_bytes = 0
            s.last_throughput_t = now
    if driver_self_report:
        out.update({k: v for k, v in driver_self_report.items() if v is not None})
    # Cross-link to time_sync
    try:
        from . import time_sync
        out["time_sync"] = time_sync.status().get("preferred")
    except Exception:
        pass
    return out


def status_all() -> list[dict]:
    return [status(dev) for dev in sorted(_STATES.keys())]


def _gps_status_compact() -> dict:
    """One-line GPS summary (delegates to time_sync._gpsd_status() if available)."""
    try:
        from . import time_sync
        g = time_sync._gpsd_status()                            # noqa: SLF001
        return {"lock": bool(g.get("lock")),
                  "satellites": g.get("satellites"),
                  "tdop": g.get("tdop")}
    except Exception:
        return {"lock": None}


def reset(device_id: Optional[str] = None) -> None:
    if device_id:
        _STATES.pop(device_id, None)
    else:
        _STATES.clear()
