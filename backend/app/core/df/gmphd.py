"""
Gaussian-Mixture PHD (Probability Hypothesis Density) filter — single-file,
constant-velocity model.

Tracks an unknown, time-varying number of targets without explicit data
association. Better than per-track Kalman filtering when:
  - Detections are noisy / clutter-heavy.
  - Targets are born / die unpredictably.
  - Per-scan detections include false alarms.

References:
  Vo & Ma, "The Gaussian Mixture Probability Hypothesis Density Filter", IEEE
  Trans. Signal Processing, 2006.
  Stone Soup's `pf/gmphd_predictor.py` and `gmphd_updater.py` (open-source).

Conventions identical to `tracker.EmitterTracker`:
  - State = [x, y, vx, vy] in local ENU around `origin` (lat/lon).
  - Measurements are bearings from known observers (lat, lon, az_deg, σ_az).
  - Optional position-only measurements (already-fused fixes) are also supported.
"""

from __future__ import annotations

import math
import time
import uuid
from dataclasses import dataclass, field
from typing import Optional

import numpy as np


EARTH_R = 6_378_137.0


def _enu_scale(lat0: float) -> tuple[float, float]:
    mlat = (math.pi * EARTH_R) / 180.0
    mlon = mlat * max(0.01, math.cos(math.radians(lat0)))
    return mlat, mlon


@dataclass
class GMComponent:
    w: float                    # weight
    m: np.ndarray               # mean (4,)
    P: np.ndarray               # covariance (4,4)
    label: str = field(default_factory=lambda: str(uuid.uuid4()))
    born_t: float = field(default_factory=time.time)
    last_update_t: float = field(default_factory=time.time)


class GmPhdTracker:
    """GM-PHD with bearing-only measurements (EKF linearisation of the
    measurement equation; sufficient for tactical AOA tracking)."""

    def __init__(self, *,
                 prob_survival: float = 0.97,
                 prob_detection: float = 0.85,
                 clutter_rate: float = 1.0,            # expected false alarms / scan / steradian
                 birth_intensity: float = 0.05,         # poisson intensity for new tracks
                 prune_threshold: float = 1e-3,
                 merge_threshold: float = 4.0,          # Mahalanobis
                 max_components: int = 64,
                 stale_timeout_s: float = 30.0,
                 sigma_a: float = 2.0):
        self.components: list[GMComponent] = []
        self._origin: Optional[tuple[float, float]] = None
        self.ps = prob_survival
        self.pd = prob_detection
        self.clutter = clutter_rate
        self.birth_i = birth_intensity
        self.prune_t = prune_threshold
        self.merge_t = merge_threshold
        self.max_n = max_components
        self.stale = stale_timeout_s
        self.sigma_a = sigma_a
        self._t_prev: Optional[float] = None

    # ── coordinate plumbing ────────────────────────────────────────────────
    def _set_origin(self, lat: float, lon: float) -> None:
        if self._origin is None:
            self._origin = (lat, lon)

    def _enu(self, lat, lon):
        olat, olon = self._origin
        mlat, mlon = _enu_scale(olat)
        return (lon - olon) * mlon, (lat - olat) * mlat

    def _latlon(self, x, y):
        olat, olon = self._origin
        mlat, mlon = _enu_scale(olat)
        return olat + y / mlat, olon + x / mlon

    # ── Kalman primitives ──────────────────────────────────────────────────
    def _F(self, dt):
        return np.array([[1, 0, dt, 0], [0, 1, 0, dt], [0, 0, 1, 0], [0, 0, 0, 1]], dtype=float)

    def _Q(self, dt):
        s = self.sigma_a ** 2
        return s * np.array([[dt**4/4, 0, dt**3/2, 0],
                             [0, dt**4/4, 0, dt**3/2],
                             [dt**3/2, 0, dt**2, 0],
                             [0, dt**3/2, 0, dt**2]], dtype=float)

    def _predict_step(self, dt: float) -> None:
        F = self._F(dt); Q = self._Q(dt)
        for c in self.components:
            c.m = F @ c.m
            c.P = F @ c.P @ F.T + Q
            c.w *= self.ps

    # ── update with bearing observations ──────────────────────────────────
    def _update_bearings(self, observations: list[dict], now: float) -> None:
        """Each obs = { lat, lon, azimuth_deg, sigma_az_deg (deg), frequency_hz?}.
        The EKF update linearises h(x) = atan2(x - x_obs, y - y_obs)."""
        if not observations:
            # Missed-detection branch: just down-weight by (1-pd)
            for c in self.components:
                c.w *= (1.0 - self.pd)
            return

        # Pre-compute updated components per observation
        new_components: list[GMComponent] = []
        # Surviving (un-updated) part scaled by (1-pd):
        for c in self.components:
            cc = GMComponent(w=c.w * (1.0 - self.pd), m=c.m.copy(), P=c.P.copy(),
                              label=c.label, born_t=c.born_t, last_update_t=c.last_update_t)
            new_components.append(cc)

        for ob in observations:
            ox, oy = self._enu(ob["lat"], ob["lon"])
            z = math.radians(float(ob["azimuth_deg"]))
            R = math.radians(float(ob.get("sigma_az_deg", 5.0))) ** 2
            # Sum of likelihoods for normaliser
            likelihoods = []
            updated_comps = []
            for c in self.components:
                dx = c.m[0] - ox; dy = c.m[1] - oy
                r2 = dx * dx + dy * dy
                r = math.sqrt(r2) or 1.0
                h_x = math.atan2(dx, dy)
                # Innovation in radians, wrapped
                y_inn = ((z - h_x) + 3 * math.pi) % (2 * math.pi) - math.pi
                # Jacobian H = ∂h/∂x = (dy/r², -dx/r², 0, 0)
                H = np.array([dy / r2, -dx / r2, 0, 0])
                S = float(H @ c.P @ H + R)
                if S <= 0:
                    continue
                K = (c.P @ H) / S
                m_upd = c.m + K * y_inn
                P_upd = (np.eye(4) - np.outer(K, H)) @ c.P
                P_upd = (P_upd + P_upd.T) / 2
                lik = math.exp(-0.5 * y_inn * y_inn / S) / math.sqrt(2 * math.pi * S)
                w_upd = self.pd * c.w * lik
                likelihoods.append(lik)
                updated_comps.append(GMComponent(w=w_upd, m=m_upd, P=P_upd, label=c.label,
                                                  born_t=c.born_t, last_update_t=now))
            denom = self.clutter + sum(c.w for c in updated_comps)
            if denom > 0:
                for c in updated_comps:
                    c.w /= denom
                    new_components.append(c)

        # Add birth components (broad, low-weight) from each measurement
        for ob in observations:
            ox, oy = self._enu(ob["lat"], ob["lon"])
            az = math.radians(float(ob["azimuth_deg"]))
            # Plant a birth component a bit down the bearing ray at moderate range
            r_birth = 5000.0
            mx = ox + r_birth * math.sin(az); my = oy + r_birth * math.cos(az)
            cov = np.diag([r_birth ** 2, r_birth ** 2, 50.0 ** 2, 50.0 ** 2])
            new_components.append(GMComponent(w=self.birth_i, m=np.array([mx, my, 0.0, 0.0]),
                                                P=cov, last_update_t=now))

        self.components = new_components

    # ── pruning / merging ──────────────────────────────────────────────────
    def _prune_and_merge(self) -> None:
        # Prune
        keep = [c for c in self.components if c.w >= self.prune_t]
        keep.sort(key=lambda c: -c.w)
        merged: list[GMComponent] = []
        used = [False] * len(keep)
        for i, ci in enumerate(keep):
            if used[i]:
                continue
            cluster = [ci]; used[i] = True
            for j in range(i + 1, len(keep)):
                if used[j]:
                    continue
                cj = keep[j]
                d = ci.m - cj.m
                try:
                    Pinv = np.linalg.inv((ci.P + cj.P) / 2)
                except np.linalg.LinAlgError:
                    continue
                if float(d @ Pinv @ d) < self.merge_t:
                    cluster.append(cj); used[j] = True
            if len(cluster) == 1:
                merged.append(ci); continue
            W = sum(c.w for c in cluster)
            m = sum(c.w * c.m for c in cluster) / W
            P = np.zeros((4, 4))
            for c in cluster:
                dm = (c.m - m).reshape(-1, 1)
                P += c.w * (c.P + dm @ dm.T)
            P /= W
            merged.append(GMComponent(w=W, m=m, P=P, label=cluster[0].label,
                                       born_t=min(c.born_t for c in cluster),
                                       last_update_t=max(c.last_update_t for c in cluster)))
        merged.sort(key=lambda c: -c.w)
        self.components = merged[: self.max_n]

    # ── public API ─────────────────────────────────────────────────────────
    def step(self, observations: list[dict]) -> list[dict]:
        if observations:
            self._set_origin(observations[0]["lat"], observations[0]["lon"])
        elif self._origin is None:
            return []
        now = time.time()
        dt = (now - self._t_prev) if self._t_prev else 1.0
        self._t_prev = now
        self._predict_step(max(0.05, min(60.0, dt)))
        self._update_bearings(observations or [], now)
        self._prune_and_merge()
        # Drop stale components
        self.components = [c for c in self.components if (now - c.last_update_t) <= self.stale]
        return self.serialise()

    def serialise(self) -> list[dict]:
        if self._origin is None:
            return []
        out = []
        for c in self.components:
            if c.w < 0.4:           # report only confirmed components
                continue
            lat, lon = self._latlon(c.m[0], c.m[1])
            cov2 = c.P[:2, :2]
            w_eig, _ = np.linalg.eigh((cov2 + cov2.T) / 2)
            w_eig = np.clip(w_eig, 0, None)
            cep = float(1.1774 * math.sqrt((w_eig[0] + w_eig[1]) / 2))
            out.append({
                "id": c.label, "lat": lat, "lon": lon,
                "velocity_mps": {"vx": float(c.m[2]), "vy": float(c.m[3])},
                "weight": float(c.w), "cep_m": cep,
                "born_t": c.born_t, "last_update_t": c.last_update_t,
            })
        return out

    def reset(self) -> None:
        self.components.clear()
        self._origin = None
        self._t_prev = None


_gmphd: Optional[GmPhdTracker] = None


def get_gmphd() -> GmPhdTracker:
    global _gmphd
    if _gmphd is None:
        _gmphd = GmPhdTracker()
    return _gmphd


def reset_gmphd() -> None:
    global _gmphd
    _gmphd = None
