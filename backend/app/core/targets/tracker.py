# SPDX-License-Identifier: MIT OR Apache-2.0
# Copyright (c) 2026 Ares

"""
targets/tracker.py — per-identifier observation store.

Each "target" is a (kind, value) pair where ``kind`` is one of
``imsi`` · ``tmsi`` · ``imei`` · ``rnti`` · ``mac`` · ``ble`` · ``ssid`` ·
``icao`` · ``dmr_rid`` · ``p25_rid`` · ``uas_serial`` · ``ais_mmsi`` ·
``callsign`` · ``other`` and ``value`` is the human-readable identifier
string. Observations land via ``record(...)`` carrying ``observer_lat`` /
``observer_lon`` / ``rssi_dbm`` and optionally ``bearing_deg`` / ``frequency_hz`` /
arbitrary metadata. The tracker keeps a per-target ring buffer (default
length 10 000), running peak RSSI + its observation, and lazily-computed
range and position estimates.

The range and position estimators delegate to functions already in
``app.core.df.single_channel`` so this module stays small and we don't
duplicate any DSP. Concretely:

  * 1–2 observations  → Friis log-distance inversion from peak RSSI and a
    catalogue-derived ``P_tx`` / ``n``. Single value, wide CEP.
  * ≥ 3 distinct observer positions → ``rss_path_loss_fix(...)`` for a
    joint ML position + ``P_tx`` + ``n`` + covariance ellipse.
  * Any AoA observations attached → ``ml_grid_fusion(...)`` mixes
    AoA + RSS + Doppler + TDOA into one likelihood with a heat-map.

The tracker also exposes a tiny pub-sub for the WebSocket: callers can
``register_listener(fn)`` to receive each tracker mutation as a dict.
"""
from __future__ import annotations

import math
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Optional

import numpy as np

# Lazy imports of the heavier solvers — only when needed, so importing this
# module is cheap.
def _single_channel():
    from app.core.df import single_channel
    return single_channel


# ─────────────────────────────────────────────────────────────────────────────
# Catalogue of identifier kinds and per-kind defaults for the Friis fallback
# (P_tx in dBm and path-loss exponent n). These are conservative averages used
# only when we have *one* observation and need to make some range guess.
# ─────────────────────────────────────────────────────────────────────────────
IDENTIFIER_KINDS: dict[str, dict] = {
    "imsi":       {"label": "IMSI",       "family": "cellular", "p_tx_dbm": 33.0, "n": 3.0},
    "tmsi":       {"label": "TMSI",       "family": "cellular", "p_tx_dbm": 33.0, "n": 3.0},
    "imei":       {"label": "IMEI",       "family": "cellular", "p_tx_dbm": 33.0, "n": 3.0},
    "rnti":       {"label": "LTE RNTI",   "family": "cellular", "p_tx_dbm": 23.0, "n": 3.0},
    "guti":       {"label": "5G GUTI",    "family": "cellular", "p_tx_dbm": 23.0, "n": 3.0},
    "nr_cell":    {"label": "5G NR cell", "family": "cellular_infra", "p_tx_dbm": 49.0, "n": 3.5},
    "lte_cell":   {"label": "LTE eNB",    "family": "cellular_infra", "p_tx_dbm": 46.0, "n": 3.5},
    "gsm_cell":   {"label": "GSM BTS",    "family": "cellular_infra", "p_tx_dbm": 43.0, "n": 3.3},
    "mac":        {"label": "WiFi MAC",   "family": "wifi", "p_tx_dbm": 20.0, "n": 2.7},
    "bssid":      {"label": "WiFi BSSID", "family": "wifi", "p_tx_dbm": 20.0, "n": 2.7},
    "ssid":       {"label": "WiFi SSID",  "family": "wifi", "p_tx_dbm": 20.0, "n": 2.7},
    "ble":        {"label": "BLE MAC",    "family": "ble",  "p_tx_dbm":  4.0, "n": 2.0},
    "icao":       {"label": "ICAO",       "family": "aviation", "p_tx_dbm": 50.0, "n": 2.2},
    "callsign":   {"label": "Callsign",   "family": "aviation", "p_tx_dbm": 50.0, "n": 2.2},
    "ais_mmsi":   {"label": "AIS MMSI",   "family": "maritime", "p_tx_dbm": 41.0, "n": 2.0},
    "dmr_rid":    {"label": "DMR RID",    "family": "ptt", "p_tx_dbm": 37.0, "n": 3.0},
    "p25_rid":    {"label": "P25 RID",    "family": "ptt", "p_tx_dbm": 37.0, "n": 3.0},
    "nxdn_rid":   {"label": "NXDN RID",   "family": "ptt", "p_tx_dbm": 37.0, "n": 3.0},
    "tetra_issi": {"label": "TETRA ISSI", "family": "ptt", "p_tx_dbm": 37.0, "n": 3.0},
    "uas_serial": {"label": "UAS Serial", "family": "uas", "p_tx_dbm": 20.0, "n": 2.5},
    "uas_op":     {"label": "UAS Operator ID", "family": "uas", "p_tx_dbm": 20.0, "n": 2.5},
    "other":      {"label": "Other ID",   "family": "other", "p_tx_dbm": 25.0, "n": 3.0},
}


# ─────────────────────────────────────────────────────────────────────────────
# Data classes
# ─────────────────────────────────────────────────────────────────────────────
@dataclass
class Observation:
    """One RSSI / AoA / Doppler / TDOA sample of a target."""
    t: float
    observer_lat: float
    observer_lon: float
    rssi_dbm: Optional[float] = None
    bearing_deg: Optional[float] = None        # AoA observation, if present
    sigma_deg: Optional[float] = None
    frequency_hz: Optional[float] = None
    doppler_hz: Optional[float] = None         # ('doppler' obs, single-pose)
    v_mps: Optional[float] = None              # observer speed at t
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class Target:
    kind: str
    value: str
    first_seen_t: float
    last_seen_t: float
    n_obs: int = 0
    peak_rssi_dbm: Optional[float] = None
    peak_observation: Optional[Observation] = None
    rolling_top_k: list[Observation] = field(default_factory=list)
    range_m_estimate: Optional[float] = None
    range_uncertainty_m: Optional[float] = None
    range_method: Optional[str] = None         # 'friis_single' | 'rss_log_distance_ml' | 'ml_grid'
    position_lat: Optional[float] = None
    position_lon: Optional[float] = None
    position_cep_m: Optional[float] = None
    position_method: Optional[str] = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def label(self) -> str:
        return IDENTIFIER_KINDS.get(self.kind, {}).get("label", self.kind.upper())

    def to_dict(self, *, include_history: bool = False, history: Optional[list[Observation]] = None) -> dict:
        out: dict[str, Any] = {
            "kind": self.kind, "value": self.value, "label": self.label,
            "first_seen_t": self.first_seen_t, "last_seen_t": self.last_seen_t,
            "n_obs": int(self.n_obs),
            "peak_rssi_dbm": self.peak_rssi_dbm,
            "peak_observation": _obs_to_dict(self.peak_observation) if self.peak_observation else None,
            "rolling_top_k": [_obs_to_dict(o) for o in self.rolling_top_k],
            "range_m_estimate": self.range_m_estimate,
            "range_uncertainty_m": self.range_uncertainty_m,
            "range_method": self.range_method,
            "position": ({"lat": self.position_lat, "lon": self.position_lon,
                            "cep_m": self.position_cep_m, "method": self.position_method}
                           if self.position_lat is not None else None),
            "metadata": dict(self.metadata),
        }
        if include_history and history is not None:
            out["history"] = [_obs_to_dict(o) for o in history]
        return out


def _obs_to_dict(o: Optional[Observation]) -> Optional[dict]:
    if o is None:
        return None
    return {
        "t": o.t, "lat": o.observer_lat, "lon": o.observer_lon,
        "rssi_dbm": o.rssi_dbm, "bearing_deg": o.bearing_deg, "sigma_deg": o.sigma_deg,
        "frequency_hz": o.frequency_hz, "doppler_hz": o.doppler_hz, "v_mps": o.v_mps,
        "metadata": dict(o.metadata or {}),
    }


# ─────────────────────────────────────────────────────────────────────────────
# Range / position estimators
# ─────────────────────────────────────────────────────────────────────────────
def _friis_range_from_peak(rssi_peak_dbm: float, kind: str) -> tuple[float, float]:
    """Single-observation Friis log-distance range estimate.

    d ≈ d₀ · 10^((P_tx − P_peak) / (10·n)) — purely a back-of-envelope
    answer when we have only one position. Returns (range_m, sigma_m).
    """
    defaults = IDENTIFIER_KINDS.get(kind, IDENTIFIER_KINDS["other"])
    p_tx = float(defaults["p_tx_dbm"]); n = float(defaults["n"])
    d0 = 1.0
    delta = max(0.0, p_tx - rssi_peak_dbm)
    d = d0 * (10 ** (delta / (10.0 * n)))
    # Uncertainty: 6 dB σ on RSSI propagates to a multiplicative factor of
    # 10^(σ/10n) on range. We report ±1σ above and below as 'uncertainty_m'.
    sigma_db = 6.0
    factor = 10 ** (sigma_db / (10.0 * n))
    sigma_m = 0.5 * (d * factor - d / factor)
    return float(d), float(sigma_m)


def estimate_range(t: "Target", history: list["Observation"]) -> dict:
    """Compute a range estimate from a target's observation history.

    Returns a dict suitable to stash on the Target. Picks the right method
    based on what's available:
      * <3 distinct observer positions → Friis single-pose
      * ≥3 distinct positions with RSSI → rss_path_loss_fix
    """
    if not t.peak_observation or t.peak_rssi_dbm is None:
        return {"range_m_estimate": None, "range_uncertainty_m": None, "range_method": None}
    pts = {(round(o.observer_lat, 4), round(o.observer_lon, 4))
           for o in history if o.rssi_dbm is not None}
    if len(pts) >= 3:
        try:
            sc = _single_channel()
            obs = [{"lat": o.observer_lat, "lon": o.observer_lon,
                       "rssi_dbm": o.rssi_dbm}
                     for o in history if o.rssi_dbm is not None]
            r = sc.rss_path_loss_fix(obs, grid_m=50.0, grid_span_m=20_000.0)
            if r.get("ok"):
                est = r["estimate"]; unc = r["uncertainty"]
                lat0, lon0 = est["lat"], est["lon"]
                # Use peak observer's position as the range origin so the
                # reported "range" is "how far from the operator's peak
                # sighting is the emitter?"
                pk = t.peak_observation
                # haversine
                R = 6_371_000.0
                phi1 = math.radians(pk.observer_lat); phi2 = math.radians(lat0)
                dphi = phi2 - phi1
                dlam = math.radians(lon0 - pk.observer_lon)
                a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlam / 2) ** 2
                d = 2 * R * math.asin(math.sqrt(a))
                return {"range_m_estimate": float(d),
                        "range_uncertainty_m": float(unc.get("cep_m") or 0.0),
                        "range_method": "rss_log_distance_ml"}
        except Exception:
            pass
    # Fall back to single-pose Friis
    d, sigma = _friis_range_from_peak(t.peak_rssi_dbm, t.kind)
    return {"range_m_estimate": d, "range_uncertainty_m": sigma, "range_method": "friis_single"}


def estimate_position(t: "Target", history: list["Observation"]) -> dict:
    """Compute an emitter position estimate. Prefers ml_grid_fusion when AoA
    observations are present; falls back to rss_path_loss_fix; returns
    nothing if the history is too sparse.
    """
    has_aoa = any(o.bearing_deg is not None for o in history)
    has_rss = any(o.rssi_dbm is not None for o in history)
    n_distinct = len({(round(o.observer_lat, 4), round(o.observer_lon, 4)) for o in history})
    try:
        sc = _single_channel()
        if has_aoa:
            obs = []
            for o in history:
                if o.bearing_deg is not None:
                    obs.append({"kind": "aoa", "lat": o.observer_lat, "lon": o.observer_lon,
                                 "bearing_deg": o.bearing_deg,
                                 "sigma_deg": o.sigma_deg or 3.0})
                if o.rssi_dbm is not None and has_rss:
                    obs.append({"kind": "rss", "lat": o.observer_lat, "lon": o.observer_lon,
                                 "rssi_dbm": o.rssi_dbm})
            if obs:
                r = sc.ml_grid_fusion(obs, grid_span_m=20_000.0, grid_step_m=50.0,
                                       sigma_aoa_deg=3.0, sigma_rss_db=6.0)
                if r.get("ok"):
                    est = r["estimate"]; unc = r["uncertainty"]
                    return {"position_lat": float(est["lat"]),
                              "position_lon": float(est["lon"]),
                              "position_cep_m": float(unc.get("cep_m") or 0.0),
                              "position_method": "ml_grid_fusion",
                              "heatmap": r.get("heatmap")}
        if has_rss and n_distinct >= 3:
            obs = [{"lat": o.observer_lat, "lon": o.observer_lon, "rssi_dbm": o.rssi_dbm}
                     for o in history if o.rssi_dbm is not None]
            r = sc.rss_path_loss_fix(obs, grid_m=50.0, grid_span_m=20_000.0)
            if r.get("ok"):
                est = r["estimate"]; unc = r["uncertainty"]
                return {"position_lat": float(est["lat"]),
                          "position_lon": float(est["lon"]),
                          "position_cep_m": float(unc.get("cep_m") or 0.0),
                          "position_method": "rss_log_distance_ml"}
    except Exception:
        pass
    return {"position_lat": None, "position_lon": None,
              "position_cep_m": None, "position_method": None}


# ─────────────────────────────────────────────────────────────────────────────
# The tracker
# ─────────────────────────────────────────────────────────────────────────────
_HISTORY_MAX = 10_000
_TOP_K = 10


class TargetTracker:
    """Thread-safe singleton that stores observations keyed by (kind, value)."""

    def __init__(self):
        self._lock = threading.RLock()
        self._targets: dict[tuple[str, str], Target] = {}
        self._history: dict[tuple[str, str], deque[Observation]] = {}
        self._listeners: set[Callable[[dict], None]] = set()
        # Throttle the heavyweight estimators — they re-fit on every Nth obs
        # or every refresh_interval_s seconds, whichever comes first.
        self._refit_every_n = 10
        self._refit_interval_s = 5.0
        self._last_refit_t: dict[tuple[str, str], float] = {}
        self._last_refit_n: dict[tuple[str, str], int] = {}

    # ── core ────────────────────────────────────────────────────────────
    def record(self, kind: str, value: str, observer_lat: float, observer_lon: float,
                rssi_dbm: Optional[float] = None, *,
                bearing_deg: Optional[float] = None, sigma_deg: Optional[float] = None,
                frequency_hz: Optional[float] = None,
                doppler_hz: Optional[float] = None, v_mps: Optional[float] = None,
                t: Optional[float] = None,
                metadata: Optional[dict[str, Any]] = None) -> Target:
        """Add one observation and update derived state."""
        if not kind or not value:
            raise ValueError("kind and value are required")
        kind = kind.lower()
        value = value.strip()
        t = float(t if t is not None else time.time())
        obs = Observation(t=t, observer_lat=float(observer_lat), observer_lon=float(observer_lon),
                           rssi_dbm=(None if rssi_dbm is None else float(rssi_dbm)),
                           bearing_deg=(None if bearing_deg is None else float(bearing_deg) % 360.0),
                           sigma_deg=(None if sigma_deg is None else float(sigma_deg)),
                           frequency_hz=(None if frequency_hz is None else float(frequency_hz)),
                           doppler_hz=(None if doppler_hz is None else float(doppler_hz)),
                           v_mps=(None if v_mps is None else float(v_mps)),
                           metadata=dict(metadata or {}))
        key = (kind, value)
        with self._lock:
            tgt = self._targets.get(key)
            hist = self._history.setdefault(key, deque(maxlen=_HISTORY_MAX))
            if tgt is None:
                tgt = Target(kind=kind, value=value, first_seen_t=t, last_seen_t=t)
                self._targets[key] = tgt
            tgt.n_obs += 1
            tgt.last_seen_t = t
            if obs.metadata:
                # merge metadata: most-recent wins on conflict but old keys preserved
                tgt.metadata.update(obs.metadata)
            hist.append(obs)
            # Peak-RSSI sampler
            if obs.rssi_dbm is not None:
                if tgt.peak_rssi_dbm is None or obs.rssi_dbm > tgt.peak_rssi_dbm:
                    tgt.peak_rssi_dbm = obs.rssi_dbm
                    tgt.peak_observation = obs
                # Top-K (sorted desc; max length K)
                tgt.rolling_top_k.append(obs)
                tgt.rolling_top_k.sort(key=lambda o: -(o.rssi_dbm or -1e9))
                tgt.rolling_top_k = tgt.rolling_top_k[:_TOP_K]
            self._maybe_refit(key, tgt, list(hist))
            snap = tgt.to_dict(include_history=False)
        self._notify({"event": "target_update", "target": snap})
        return tgt

    def _maybe_refit(self, key: tuple[str, str], tgt: "Target", history: list[Observation]):
        """Lazily recompute range/position when enough new obs have arrived."""
        last_t = self._last_refit_t.get(key, 0.0)
        last_n = self._last_refit_n.get(key, 0)
        now = tgt.last_seen_t
        if tgt.n_obs - last_n < self._refit_every_n and (now - last_t) < self._refit_interval_s:
            return
        self._last_refit_t[key] = now
        self._last_refit_n[key] = tgt.n_obs
        try:
            r = estimate_range(tgt, history)
            for k, v in r.items():
                setattr(tgt, k, v)
            p = estimate_position(tgt, history)
            for k in ("position_lat", "position_lon", "position_cep_m", "position_method"):
                setattr(tgt, k, p.get(k))
        except Exception:
            pass

    # ── queries ─────────────────────────────────────────────────────────
    def get(self, kind: str, value: str) -> Optional[Target]:
        with self._lock:
            return self._targets.get((kind.lower(), value.strip()))

    def history(self, kind: str, value: str) -> list[Observation]:
        with self._lock:
            return list(self._history.get((kind.lower(), value.strip()), ()))

    def list(self) -> list[Target]:
        with self._lock:
            return list(self._targets.values())

    def query(self, *, kind: Optional[str] = None, since_t: Optional[float] = None,
               min_obs: int = 1, family: Optional[str] = None) -> list[Target]:
        with self._lock:
            out = []
            for tgt in self._targets.values():
                if kind and tgt.kind != kind:
                    continue
                if since_t is not None and tgt.last_seen_t < since_t:
                    continue
                if tgt.n_obs < min_obs:
                    continue
                if family and IDENTIFIER_KINDS.get(tgt.kind, {}).get("family") != family:
                    continue
                out.append(tgt)
            return out

    def forget(self, kind: str, value: str) -> bool:
        key = (kind.lower(), value.strip())
        with self._lock:
            existed = key in self._targets
            self._targets.pop(key, None)
            self._history.pop(key, None)
            self._last_refit_t.pop(key, None)
            self._last_refit_n.pop(key, None)
        if existed:
            self._notify({"event": "target_forget", "kind": kind, "value": value})
        return existed

    def snapshot(self) -> list[dict]:
        with self._lock:
            return [t.to_dict() for t in self._targets.values()]

    def recompute(self, kind: str, value: str) -> Optional[Target]:
        """Force a refit (used by /targets/{kind}/{value}/fix)."""
        key = (kind.lower(), value.strip())
        with self._lock:
            tgt = self._targets.get(key)
            if tgt is None:
                return None
            self._last_refit_t[key] = 0.0
            self._last_refit_n[key] = 0
            self._maybe_refit(key, tgt, list(self._history.get(key, ())))
            return tgt

    # ── listeners ───────────────────────────────────────────────────────
    def register_listener(self, fn: Callable[[dict], None]) -> None:
        with self._lock:
            self._listeners.add(fn)

    def unregister_listener(self, fn: Callable[[dict], None]) -> None:
        with self._lock:
            self._listeners.discard(fn)

    def _notify(self, payload: dict) -> None:
        with self._lock:
            listeners = list(self._listeners)
        for fn in listeners:
            try:
                fn(payload)
            except Exception:
                pass


# ─────────────────────────────────────────────────────────────────────────────
# Module-level singleton + convenience functions (the public API)
# ─────────────────────────────────────────────────────────────────────────────
tracker = TargetTracker()


def record(kind: str, value: str, observer_lat: float, observer_lon: float,
            rssi_dbm: Optional[float] = None, **kwargs) -> Target:
    return tracker.record(kind, value, observer_lat, observer_lon, rssi_dbm=rssi_dbm, **kwargs)


def get(kind: str, value: str) -> Optional[Target]:
    return tracker.get(kind, value)


def list_targets() -> list[Target]:
    return tracker.list()


def query(**kwargs) -> list[Target]:
    return tracker.query(**kwargs)


def forget(kind: str, value: str) -> bool:
    return tracker.forget(kind, value)


def snapshot() -> list[dict]:
    return tracker.snapshot()


def register_listener(fn: Callable[[dict], None]) -> None:
    tracker.register_listener(fn)


def unregister_listener(fn: Callable[[dict], None]) -> None:
    tracker.unregister_listener(fn)
