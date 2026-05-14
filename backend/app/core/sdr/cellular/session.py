"""
Base class for cellular decoder sessions. Each concrete decoder
(GSM / LTE / NR / UMTS / WiFi / BLE) inherits from this and implements
``_start_impl`` / ``_stop_impl``. Decoded events are pushed onto
``self.events`` (a thread-safe deque, max 1000 entries) and a sequence
number tracker so consumers can resume.
"""
from __future__ import annotations

import threading
import time
from collections import deque
from typing import Any, Optional


class CellularSession:
    """Abstract base."""

    KIND: str = "abstract"

    def __init__(self, sid: str, device: Optional[dict] = None,
                  center_hz: Optional[float] = None,
                  bandwidth_hz: Optional[float] = None,
                  **extra: Any):
        self.sid = sid
        self.device = device or {"id": "synthetic"}
        self.center_hz = float(center_hz) if center_hz is not None else None
        self.bandwidth_hz = float(bandwidth_hz) if bandwidth_hz is not None else None
        self.extra = dict(extra)
        self.started_t: Optional[float] = None
        self.stopped_t: Optional[float] = None
        self.error: Optional[str] = None
        self._running = False
        self._lock = threading.RLock()
        self.events: deque[dict] = deque(maxlen=2_000)
        self._seq = 0

    def start(self) -> None:
        if self._running:
            return
        self.started_t = time.time()
        try:
            self._start_impl()
            self._running = True
        except Exception as e:
            self.error = f"{type(e).__name__}: {e}"
            self.stopped_t = time.time()

    def stop(self) -> None:
        if not self._running:
            return
        try:
            self._stop_impl()
        finally:
            self._running = False
            self.stopped_t = time.time()

    # ── subclass hooks ──────────────────────────────────────────────────
    def _start_impl(self) -> None:
        raise NotImplementedError

    def _stop_impl(self) -> None:
        raise NotImplementedError

    # ── event plumbing ──────────────────────────────────────────────────
    def emit(self, payload: dict) -> None:
        """Push one decoded event onto the queue. Also forwards to
        targets.tracker if the payload carries an identifier."""
        with self._lock:
            self._seq += 1
            ev = {"seq": self._seq, "t": time.time(), "kind": self.KIND, **payload}
            self.events.append(ev)
        # Route to targets tracker if the event carries an identifier + observer-position
        try:
            from app.core import targets
            id_kind = ev.get("identifier_kind")
            id_value = ev.get("identifier_value")
            obs_lat = ev.get("observer_lat")
            obs_lon = ev.get("observer_lon")
            if id_kind and id_value and obs_lat is not None and obs_lon is not None:
                targets.record(
                    kind=id_kind, value=id_value,
                    observer_lat=obs_lat, observer_lon=obs_lon,
                    rssi_dbm=ev.get("rssi_dbm"),
                    frequency_hz=ev.get("frequency_hz") or self.center_hz,
                    bearing_deg=ev.get("bearing_deg"),
                    sigma_deg=ev.get("sigma_deg"),
                    t=ev.get("t"),
                    metadata={k: v for k, v in ev.items()
                                if k not in ("seq", "kind", "identifier_kind",
                                              "identifier_value", "observer_lat",
                                              "observer_lon", "rssi_dbm",
                                              "frequency_hz", "bearing_deg",
                                              "sigma_deg", "t")},
                )
        except Exception:
            pass

    def recent_events(self, since_seq: int = 0, limit: int = 200) -> list[dict]:
        with self._lock:
            return [e for e in list(self.events) if e["seq"] > since_seq][-limit:]

    def status(self) -> dict:
        return {
            "sid": self.sid,
            "kind": self.KIND,
            "device_id": (self.device or {}).get("id"),
            "center_hz": self.center_hz,
            "bandwidth_hz": self.bandwidth_hz,
            "running": bool(self._running),
            "started_t": self.started_t,
            "stopped_t": self.stopped_t,
            "error": self.error,
            "n_events": len(self.events),
            "last_event_t": (self.events[-1]["t"] if self.events else None),
            "extra": dict(self.extra),
        }
