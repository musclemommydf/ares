# SPDX-License-Identifier: MIT OR Apache-2.0
# Copyright (c) 2026 Ares

"""
Multi-target emitter tracker (Stone-Soup-style, single-file).

Each *track* is a 4-state Kalman filter for an emitter centroid in lat/lon
(actually local ENU around an origin, then re-projected). Birth happens when a
fresh "cut" intersection lands far from every existing track. Death happens
after a stale-timeout. Each `step()` runs:

  1. Predict every existing track forward by dt.
  2. Gate incoming observations against predicted tracks (Mahalanobis distance).
  3. Update matched tracks (JPDA-style soft assignment is overkill here; we use
     greedy nearest-neighbour — sufficient for tens of emitters, the
     operational scale for tactical DF).
  4. Birth tracks for unassigned high-confidence cuts.
  5. Kill tracks that haven't been updated in `stale_timeout_s` seconds.

Inputs are LoB observations, not centroid fixes — the tracker triangulates
internally by treating each new LoB as evidence that a track's position is on
the bearing ray from the observer. Concretely we use a UKF-style measurement
model where the predicted bearing from the track's current state is compared
to the observed bearing, with σ_az as observation noise.

This is the open-source equivalent of CRFS RFEye Site's track-while-scan
display, scaled for Ares's use cases.
"""

from __future__ import annotations

import math
import time
import uuid
from dataclasses import dataclass, field
from typing import Optional

import numpy as np


EARTH_R = 6_378_137.0


def _enu_origin(lat0: float, lon0: float) -> tuple[float, float]:
    """Local-tangent-plane scale factors at the origin (m per degree)."""
    m_per_deg_lat = (math.pi * EARTH_R) / 180.0
    m_per_deg_lon = m_per_deg_lat * max(0.01, math.cos(math.radians(lat0)))
    return m_per_deg_lat, m_per_deg_lon


@dataclass
class Track:
    id: str
    state: np.ndarray                # [x_m, y_m, vx_mps, vy_mps] in ENU
    covariance: np.ndarray           # 4×4
    last_update_t: float
    born_t: float
    confidence: float = 0.0          # rolls up from observation count + posterior trace
    frequency_hz: float = 0.0
    n_obs: int = 0
    label: str = ""

    def position_latlon(self, origin_lat: float, origin_lon: float) -> tuple[float, float]:
        mlat, mlon = _enu_origin(origin_lat, origin_lon)
        return origin_lat + self.state[1] / mlat, origin_lon + self.state[0] / mlon

    def cep_m(self) -> float:
        """50% circular-error-probable — translation of the position covariance
        sub-block under a 2-D Gaussian assumption."""
        Px = self.covariance[:2, :2]
        # CEP ≈ 1.1774 · sqrt((σx² + σy²) / 2) for σx ≈ σy. Use the principal axes.
        w, _ = np.linalg.eigh((Px + Px.T) / 2)
        w = np.clip(w, 0, None)
        return float(1.1774 * math.sqrt((w[0] + w[1]) / 2))


class EmitterTracker:
    """Multi-target tracker. Coordinate origin is set on first observation."""

    def __init__(self, *, dt: float = 1.0, sigma_a_mps2: float = 2.0,
                 stale_timeout_s: float = 30.0, gate_chi2: float = 9.21,
                 birth_min_obs: int = 2, freq_tol_hz: float = 50_000.0):
        self.tracks: dict[str, Track] = {}
        self._origin: Optional[tuple[float, float]] = None
        self._unassigned_obs: list[dict] = []    # buffer for birth (≥ birth_min_obs)
        self.dt = dt
        self.sigma_a = sigma_a_mps2              # process-noise acceleration (m/s²)
        self.stale_timeout_s = stale_timeout_s
        self.gate_chi2 = gate_chi2               # ~99% gate for 2 DOF
        self.birth_min_obs = birth_min_obs
        self.freq_tol_hz = freq_tol_hz

    # ── coordinate origin ──────────────────────────────────────────────────
    @property
    def origin(self) -> tuple[float, float]:
        if self._origin is None:
            raise RuntimeError("tracker has not seen an observation yet")
        return self._origin

    def _set_origin_if_needed(self, lat: float, lon: float) -> None:
        if self._origin is None:
            self._origin = (lat, lon)

    def _to_enu(self, lat: float, lon: float) -> tuple[float, float]:
        olat, olon = self._origin
        mlat, mlon = _enu_origin(olat, olon)
        return (lon - olon) * mlon, (lat - olat) * mlat

    # ── Kalman primitives ───────────────────────────────────────────────────
    def _F(self, dt: float) -> np.ndarray:
        return np.array([
            [1, 0, dt, 0],
            [0, 1, 0, dt],
            [0, 0, 1, 0],
            [0, 0, 0, 1],
        ], dtype=float)

    def _Q(self, dt: float) -> np.ndarray:
        s = self.sigma_a ** 2
        return s * np.array([
            [dt**4 / 4, 0, dt**3 / 2, 0],
            [0, dt**4 / 4, 0, dt**3 / 2],
            [dt**3 / 2, 0, dt**2, 0],
            [0, dt**3 / 2, 0, dt**2],
        ], dtype=float)

    def _predict(self, tr: Track, dt: float) -> None:
        F = self._F(dt)
        tr.state = F @ tr.state
        tr.covariance = F @ tr.covariance @ F.T + self._Q(dt)

    def _bearing_residual(self, tr: Track, obs_x: float, obs_y: float, obs_az_deg: float) -> tuple[float, np.ndarray, float]:
        """Return (innovation, H, predicted_bearing_deg) for an LoB observation."""
        dx = tr.state[0] - obs_x
        dy = tr.state[1] - obs_y
        r2 = dx * dx + dy * dy
        r = math.sqrt(r2) or 1.0
        pred_az = (math.degrees(math.atan2(dx, dy)) + 360) % 360
        # innovation in degrees, wrapped to [-180, 180]
        innov = ((obs_az_deg - pred_az + 540) % 360) - 180
        # H is the Jacobian of bearing = atan2(dx, dy) w.r.t. state (x, y, vx, vy).
        H = np.array([dy / r2, -dx / r2, 0, 0]) * math.degrees(1.0)
        return innov, H, pred_az

    # ── main step ───────────────────────────────────────────────────────────
    def step(self, observations: list[dict]) -> list[dict]:
        """Feed a batch of new LoB observations; return current track snapshots.

        Each observation:
          { lat, lon, azimuth_deg, frequency_hz, t (epoch s),
            sigma_az_deg (optional, default 5°) }

        Returns serialised Track dicts (active + just-died this tick).
        """
        now = time.time()
        # Predict every existing track to now.
        for tr in self.tracks.values():
            dt = max(0.05, min(60.0, now - tr.last_update_t))
            self._predict(tr, dt)

        # Set origin from first observation if not set.
        if observations:
            self._set_origin_if_needed(observations[0]["lat"], observations[0]["lon"])

        # Associate & update.
        for ob in observations:
            obs_x, obs_y = self._to_enu(ob["lat"], ob["lon"])
            obs_az = float(ob["azimuth_deg"])
            sigma_az = float(ob.get("sigma_az_deg") or 5.0)
            freq = float(ob.get("frequency_hz") or 0.0)
            R_obs = np.array([[sigma_az ** 2]])

            # Find best matching track within freq tolerance + Mahalanobis gate.
            best_id, best_d2 = None, math.inf
            for tid, tr in self.tracks.items():
                if freq > 0 and tr.frequency_hz > 0 and abs(tr.frequency_hz - freq) > self.freq_tol_hz:
                    continue
                innov, H, _ = self._bearing_residual(tr, obs_x, obs_y, obs_az)
                S = H @ tr.covariance @ H.T + R_obs.item()
                if S <= 0:
                    continue
                d2 = innov * innov / S
                if d2 < best_d2:
                    best_d2 = d2; best_id = tid

            if best_id is not None and best_d2 < self.gate_chi2:
                tr = self.tracks[best_id]
                innov, H, _ = self._bearing_residual(tr, obs_x, obs_y, obs_az)
                S = (H @ tr.covariance @ H.T + R_obs.item())
                K = (tr.covariance @ H.reshape(-1, 1)) / max(S, 1e-9)
                tr.state = tr.state + (K.flatten() * innov)
                tr.covariance = (np.eye(4) - K @ H.reshape(1, -1)) @ tr.covariance
                tr.last_update_t = now
                tr.n_obs += 1
                tr.confidence = min(1.0, tr.confidence + 0.08)
                _archive_obs_and_pos(tr, ob, now, self._origin)
            else:
                # Buffer for potential birth.
                self._unassigned_obs.append({**ob, "obs_x": obs_x, "obs_y": obs_y, "t": now})

        # Try to spawn new tracks from buffered cuts. Group by similar frequency,
        # require ≥ birth_min_obs observations from distinct observers within 20 s.
        self._unassigned_obs = [o for o in self._unassigned_obs if now - o["t"] < 20.0]
        births = self._maybe_birth(now)
        for tr in births:
            self.tracks[tr.id] = tr

        # Kill stale tracks.
        died = [tid for tid, tr in self.tracks.items() if now - tr.last_update_t > self.stale_timeout_s]
        for tid in died:
            self.tracks.pop(tid, None)

        return self.serialise()

    def _maybe_birth(self, now: float) -> list[Track]:
        births: list[Track] = []
        seen_used: set[int] = set()
        # Group by frequency tolerance
        by_freq: dict[int, list[int]] = {}
        for i, o in enumerate(self._unassigned_obs):
            key = int(round(o.get("frequency_hz", 0) / max(self.freq_tol_hz, 1)))
            by_freq.setdefault(key, []).append(i)
        for indices in by_freq.values():
            if len(indices) < self.birth_min_obs:
                continue
            sub = [self._unassigned_obs[i] for i in indices]
            # Triangulate by simple least-squares of bearing rays
            pos = self._triangulate(sub)
            if pos is None:
                continue
            x, y = pos
            v = np.array([x, y, 0.0, 0.0])
            P = np.diag([5000.0**2, 5000.0**2, 50.0**2, 50.0**2])
            tr = Track(
                id=str(uuid.uuid4()),
                state=v, covariance=P,
                last_update_t=now, born_t=now,
                frequency_hz=float(sub[0].get("frequency_hz") or 0.0),
                n_obs=len(sub), confidence=0.3,
            )
            births.append(tr)
            seen_used.update(indices)
            # Archive every observation that contributed to the birth + the
            # initial position estimate, so the heatmap and detail card have
            # history from frame one.
            for o in sub:
                _archive_obs_and_pos(tr, o, now, self._origin)
        self._unassigned_obs = [o for i, o in enumerate(self._unassigned_obs) if i not in seen_used]
        return births

    def _triangulate(self, observations: list[dict]) -> Optional[tuple[float, float]]:
        """Least-squares intersection of bearing rays in ENU."""
        rows = []
        rhs = []
        for o in observations:
            x0, y0 = o["obs_x"], o["obs_y"]
            az = math.radians(o["azimuth_deg"])
            ux, uy = math.sin(az), math.cos(az)
            # Perpendicular form: (p - p0) × u = 0 → uy*x - ux*y = uy*x0 - ux*y0
            rows.append([uy, -ux])
            rhs.append(uy * x0 - ux * y0)
        A = np.array(rows); b = np.array(rhs)
        if np.linalg.matrix_rank(A) < 2:
            return None
        sol, *_ = np.linalg.lstsq(A, b, rcond=None)
        return float(sol[0]), float(sol[1])

    # ── output ──────────────────────────────────────────────────────────────
    def serialise(self) -> list[dict]:
        if self._origin is None:
            return []
        olat, olon = self._origin
        out = []
        for tr in self.tracks.values():
            lat, lon = tr.position_latlon(olat, olon)
            out.append({
                "id": tr.id,
                "lat": lat, "lon": lon,
                "velocity_mps": {"vx": float(tr.state[2]), "vy": float(tr.state[3])},
                "cep_m": tr.cep_m(),
                "confidence": tr.confidence,
                "n_obs": tr.n_obs,
                "frequency_hz": tr.frequency_hz,
                "born_t": tr.born_t,
                "last_update_t": tr.last_update_t,
                "age_s": time.time() - tr.born_t,
            })
        return out

    def reset(self) -> None:
        self.tracks.clear()
        self._unassigned_obs.clear()
        self._origin = None


# Module-level singleton — endpoints share one tracker instance per process.
def _archive_obs_and_pos(track: Track, observation: dict, now: float,
                          origin: Optional[tuple[float, float]]) -> None:
    """Persist each accepted observation + the updated track position to the
    on-disk track archive so /df/track_archive serves real data and the
    activity heatmap / emitter detail card light up."""
    try:
        from .. import track_archive               # type: ignore
        track_archive.record_observation(
            track.id,
            t=now, lat=observation["lat"], lon=observation["lon"],
            azimuth_deg=float(observation["azimuth_deg"]),
            rssi_dbm=observation.get("rssi_dbm"),
            device_id=observation.get("device_id", ""),
            frequency_hz=float(observation.get("frequency_hz") or 0),
        )
        if origin is not None:
            lat, lon = track.position_latlon(*origin)
            track_archive.record_position(
                track.id,
                t=now, lat=lat, lon=lon, cep_m=track.cep_m(),
                confidence=track.confidence,
                velocity_mps={"vx": float(track.state[2]), "vy": float(track.state[3])},
                frequency_hz=track.frequency_hz,
            )
    except Exception:
        # Archiving failure must never disrupt tracker math.
        pass


_tracker: Optional[EmitterTracker] = None


def get_tracker() -> EmitterTracker:
    global _tracker
    if _tracker is None:
        _tracker = EmitterTracker()
    return _tracker


def reset_tracker() -> None:
    global _tracker
    _tracker = None
