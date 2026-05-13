"""
Ares — passive emitter geolocation from Lines of Bearing (Workstream C / B).

The single Cut/Fix solver behind ``POST /api/v1/geolocate/fix`` (web app, mobile
app, ATAK plugin) and the live SDR/DF pipeline.

Pipeline: observations (az, RSSI, freq, position, confidence, device id, …)
→ group by frequency-within-tolerance + device identity
→ **maximum-likelihood (IRLS Gauss-Newton) bearing-only triangulation** — minimise
  Σ (Δθ_i / σ_i)² over emitter position; per-LoB σ from the reported confidence
  (and the receiving array's HPBW when known)
→ **error ellipse from the estimate covariance** (Jᵀ W J)⁻¹ — so it stretches along
  bad-geometry directions like a real DF system's; CEP / 95 % radii, **GDOP** and
  residual-RMS are reported
→ classify each group: ``lob`` (1) | ``cut`` (2) | ``fix`` (3+)
→ emit a GeoJSON FeatureCollection (bearing wedges, the CEP and 95 % error
  ellipses, suspected-emitter points) plus structured per-group results.

Also provides :class:`EmitterTrack` — the constant-velocity EKF the live SDR/DF
manager uses to smooth a stream of independent fixes into a track.

The terrain-aware per-bearing range cap is *not* recomputed here — callers pass
each observation's ``estimated_distance_m`` (from ``/lob/range_estimate`` or an
RSSI model); this module triangulates and renders.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Optional

import numpy as np

EARTH_R = 6_371_000.0
M_PER_DEG_LAT = 111_320.0
FREQ_TOLERANCE_HZ = 25_000.0

# extra path loss above FSPL at the 1 km reference distance, and path-loss
# exponent n, per environment (mirrors ENVIRONMENT_PRESETS in the web UI)
_ENV = {
    "open":        (5,  2.5),
    "rural":       (10, 2.8),
    "suburban":    (18, 3.2),
    "urban":       (28, 3.8),
    "forest":      (25, 3.5),
    "mountainous": (20, 3.0),
}


def _mpd_lon(lat_deg: float) -> float:
    return M_PER_DEG_LAT * math.cos(math.radians(lat_deg))


# ─────────────────────────────────────────────────────────────────────────────
# geometry
# ─────────────────────────────────────────────────────────────────────────────
def initial_bearing(lat1: float, lon1: float, lat2: float, lon2: float) -> Optional[float]:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    y = math.sin(dl) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    if y == 0 and x == 0:
        return None
    return (math.degrees(math.atan2(y, x)) + 360.0) % 360.0


# ── Relative LOB ⇄ Absolute LOB (the DF compass) ─────────────────────────────
# Relative LOB  — the line of bearing to the energy as it passes over the physical
#                 antenna elements; 0° = the *front* of the DF antenna.
# Absolute LOB  — the Relative LOB with the antenna's heading applied so it's
#                 plottable on a map:   Absolute LOB = (0° + heading) + Relative LOB.
# Clock position — the Relative LOB expressed on a 12-hour clock face off the front.
COMPASS_MODES = {
    "absolute": {"label": "Absolute LOB (true north — plottable on a map)",
                 "desc": "the Relative LOB with the antenna heading applied: (0° + heading) + Relative LOB"},
    "relative": {"label": "Relative LOB (degrees off the antenna front)",
                 "desc": "the LOB to the energy over the physical antenna elements; 0° = the front of the DF antenna"},
    "clock":    {"label": "Clock position (off the antenna front)",
                 "desc": "the Relative LOB on a 12-hour clock face: 12 = ahead, 3 = right, 6 = behind, 9 = left"},
}


def deg_to_clock(rel_deg: float) -> str:
    """A Relative LOB (0° = straight ahead off the antenna front) → a clock face.
    12 o'clock = ahead, 3 = right, 6 = behind, 9 = left; rounded to the nearest hour."""
    h = round((float(rel_deg) % 360.0) / 30.0) % 12
    return f"{12 if h == 0 else h} o'clock"


def relative_to_absolute(relative_lob_deg: float, antenna_heading_deg: float = 0.0) -> float:
    """Absolute LOB (deg from true north) = (0° + heading) + Relative LOB  (mod 360)."""
    return (0.0 + float(antenna_heading_deg or 0.0) + float(relative_lob_deg)) % 360.0


def calibrate_heading(known_true_bearing_deg: float, measured_relative_lob_deg: float) -> float:
    """Solve the antenna heading from a calibration shot at a target whose *true*
    bearing is known: from Absolute = heading + Relative ⇒ heading = (true − relative) mod 360."""
    return (float(known_true_bearing_deg) - float(measured_relative_lob_deg)) % 360.0


def resolve_true_bearing(azimuth_deg: float, azimuth_reference: str = "absolute",
                         antenna_heading_deg: float = 0.0) -> float:
    """Normalise a DF azimuth to an *Absolute* LOB (deg from true north):
      - reference "absolute"          : already true — returned as-is (mod 360)
      - reference "relative" | "clock": `azimuth_deg` is a Relative LOB off the antenna
                                        front ⇒ Absolute = (0 + heading) + relative."""
    ref = (azimuth_reference or "absolute").lower()
    if ref in ("absolute", "true"):
        return float(azimuth_deg) % 360.0
    return relative_to_absolute(azimuth_deg, antenna_heading_deg)


def bearing_views(absolute_lob_deg: float, antenna_heading_deg: float = 0.0) -> dict:
    """All three representations of an Absolute LOB, for display."""
    rel = (float(absolute_lob_deg) - float(antenna_heading_deg or 0.0)) % 360.0
    return {"absolute_deg": round(float(absolute_lob_deg) % 360.0, 1),
            "relative_deg": round(rel, 1), "clock": deg_to_clock(rel)}


def destination_point(lat: float, lon: float, bearing_deg: float, dist_m: float) -> tuple[float, float]:
    d = dist_m / EARTH_R
    th = math.radians(bearing_deg)
    p1, l1 = math.radians(lat), math.radians(lon)
    p2 = math.asin(math.sin(p1) * math.cos(d) + math.cos(p1) * math.sin(d) * math.cos(th))
    l2 = l1 + math.atan2(math.sin(th) * math.sin(d) * math.cos(p1),
                         math.cos(d) - math.sin(p1) * math.sin(p2))
    return math.degrees(p2), (math.degrees(l2) + 540.0) % 360.0 - 180.0


def intersect_bearings(lat1, lon1, az1_deg, lat2, lon2, az2_deg) -> Optional[tuple[float, float]]:
    """ENU flat-earth intersection of two bearing rays; valid for separations < ~300 km."""
    mid_lat = (lat1 + lat2) / 2.0
    mpd_lon = _mpd_lon(mid_lat)
    a1, a2 = math.radians(az1_deg), math.radians(az2_deg)
    d1 = (math.sin(a1), math.cos(a1))  # (east, north)
    d2 = (math.sin(a2), math.cos(a2))
    dx = (lon2 - lon1) * mpd_lon
    dy = (lat2 - lat1) * M_PER_DEG_LAT
    det = d1[0] * (-d2[1]) - d1[1] * (-d2[0])
    if abs(det) < 1e-8:
        return None  # parallel
    t1 = (dx * (-d2[1]) - dy * (-d2[0])) / det
    t2 = (d1[0] * dy - d1[1] * dx) / det
    if t1 < -200 or t2 < -200:  # behind both observers (200 m tolerance)
        return None
    return (lat1 + (t1 * d1[1]) / M_PER_DEG_LAT, lon1 + (t1 * d1[0]) / mpd_lon)


def estimate_distance_m(rssi_dbm: float, freq_hz: float, tx_power_dbm: float = 30.0,
                        environment: str = "suburban", clutter_height_m: float = 0.0) -> float:
    """Log-distance path-loss range estimate (FSPL @1 km + env offset + 10·n·log10(d_km))."""
    extra_db, n = _ENV.get(environment, _ENV["suburban"])
    freq_mhz = freq_hz / 1e6
    pl = tx_power_dbm - rssi_dbm
    if freq_mhz <= 0 or not math.isfinite(pl):
        return 2000.0
    clutter_db = max(0.0, clutter_height_m) * 0.4
    eff = pl - extra_db - clutter_db
    fspl_1km = 32.45 + 20 * math.log10(freq_mhz)
    d_km = 10 ** ((eff - fspl_1km) / (10 * n))
    return max(100.0, min(d_km * 1000.0, 150_000.0))


def _ellipse_polygon(center_lat, center_lon, semi_major_m, semi_minor_m, rot_deg, n=72) -> list[list[float]]:
    rot = math.radians(rot_deg)
    mpd_lon = _mpd_lon(center_lat)
    coords = []
    for i in range(n + 1):
        th = 2 * math.pi * i / n
        xl = semi_major_m * math.cos(th)
        yl = semi_minor_m * math.sin(th)
        xe = xl * math.sin(rot) + yl * math.cos(rot)
        yn = xl * math.cos(rot) - yl * math.sin(rot)
        coords.append([center_lon + xe / mpd_lon, center_lat + yn / M_PER_DEG_LAT])
    return coords


# ─────────────────────────────────────────────────────────────────────────────
# data model
# ─────────────────────────────────────────────────────────────────────────────
@dataclass
class LoB:
    lat: float
    lon: float
    azimuth_deg: float
    frequency_hz: float
    rssi_dbm: float = -80.0
    tx_power_dbm: float = 30.0
    confidence_pct: float = 80.0
    observer_height_m: float = 1.5
    environment: str = "suburban"
    clutter_height_m: float = 0.0
    device_type: str = ""
    device_id: str = ""
    time: str = ""
    estimated_distance_m: float = 0.0  # terrain-aware cap if provided; else derived from RSSI
    id: Optional[str] = None

    def __post_init__(self):
        if not self.estimated_distance_m or self.estimated_distance_m <= 0:
            self.estimated_distance_m = estimate_distance_m(
                self.rssi_dbm, self.frequency_hz, self.tx_power_dbm,
                self.environment, self.clutter_height_m,
            )


def _group(lobs: list[LoB]) -> list[dict]:
    groups: list[dict] = []
    assigned: set[int] = set()
    for i, a in enumerate(lobs):
        if i in assigned:
            continue
        members = [a]
        assigned.add(i)
        dev_a = a.device_id or ""
        for j in range(i + 1, len(lobs)):
            if j in assigned:
                continue
            b = lobs[j]
            if abs(a.frequency_hz - b.frequency_hz) <= FREQ_TOLERANCE_HZ and (b.device_id or "") == dev_a:
                members.append(b)
                assigned.add(j)
        groups.append({
            "frequency_hz": a.frequency_hz,
            "device_id": dev_a,
            "device_type": a.device_type or "",
            "lobs": members,
        })
    return groups


def _pairwise_intersections(members: list["LoB"]) -> list[dict]:
    """All pairwise ENU intersections of the member bearing rays — used only as a
    robust initial guess for the ML solver and to visualise a 2-LoB *cut*."""
    out = []
    for i in range(len(members)):
        for j in range(i + 1, len(members)):
            pt = intersect_bearings(members[i].lat, members[i].lon, members[i].azimuth_deg,
                                    members[j].lat, members[j].lon, members[j].azimuth_deg)
            if pt:
                out.append({"lat": pt[0], "lon": pt[1],
                            "weight": (members[i].confidence_pct + members[j].confidence_pct) / 200.0})
    return out


def _centroid(intersections: list[dict]) -> Optional[dict]:
    if not intersections:
        return None
    tw = sum(p["weight"] for p in intersections)
    if tw == 0:
        return None
    return {"lat": sum(p["lat"] * p["weight"] for p in intersections) / tw,
            "lon": sum(p["lon"] * p["weight"] for p in intersections) / tw}


def _wrap_pi(a: float) -> float:
    return (a + math.pi) % (2.0 * math.pi) - math.pi


def lob_sigma_deg(lob: "LoB", rx_hpbw_deg: Optional[float] = None) -> float:
    """1-σ bearing uncertainty (deg) for one LoB: a small instrument floor + a
    confidence-driven term + (if a receiving-array HPBW is known) a realised-DF
    term that grows when the DoA estimate didn't peak sharply."""
    conf = max(0.0, min(100.0, lob.confidence_pct if lob.confidence_pct is not None else 80.0))
    peaking = 1.0 - conf / 100.0                # 0 = razor-sharp peak, 1 = mush
    sigma = 1.5 + 11.0 * peaking
    if rx_hpbw_deg and rx_hpbw_deg > 0:
        sigma = math.hypot(sigma, 0.5 * rx_hpbw_deg * peaking * peaking)
    return max(0.5, sigma)


# ─────────────────────────────────────────────────────────────────────────────
# Maximum-likelihood (IRLS Gauss-Newton) bearing-only triangulation, with the
# full estimate covariance → a geometry-correct error ellipse and GDOP.  This
# replaces the old "confidence-weighted centroid of pairwise intersections +
# fixed-aspect ellipse" — the ellipse now stretches along bad-geometry directions
# exactly as a real DF system's does, and CEP/95% radii come from the covariance.
# ─────────────────────────────────────────────────────────────────────────────
def error_ellipse_from_cov(cov: "np.ndarray", conf: float = 0.95) -> tuple[float, float, float]:
    """(semi_major_m, semi_minor_m, rotation_deg) of the `conf`-probability error
    ellipse for a 2-D Gaussian with the given ENU position covariance (m²).
    `rotation_deg` is the *bearing* (from north, clockwise) of the major axis."""
    vals, vecs = np.linalg.eigh(np.asarray(cov, dtype=float))   # ascending eigenvalues
    s = math.sqrt(max(1e-9, -2.0 * math.log(max(1e-9, 1.0 - conf))))
    lam_min, lam_maj = max(float(vals[0]), 0.0), max(float(vals[1]), 0.0)
    semi_major = s * math.sqrt(lam_maj)
    semi_minor = s * math.sqrt(lam_min)
    vmaj = vecs[:, 1]                                           # [east, north]
    rot_deg = math.degrees(math.atan2(float(vmaj[0]), float(vmaj[1]))) % 180.0
    return semi_major, semi_minor, rot_deg


def cep_from_cov(cov: "np.ndarray") -> float:
    """Circular Error Probable (50 % radius, m) from an ENU position covariance —
    the standard σ-based approximation CEP ≈ 0.5887·(σ_major + σ_minor)."""
    vals = np.linalg.eigvalsh(np.asarray(cov, dtype=float))
    smin, smaj = math.sqrt(max(float(vals[0]), 0.0)), math.sqrt(max(float(vals[1]), 0.0))
    return 0.5887 * (smaj + smin)


def ml_fix(members: list["LoB"], rx_hpbw_deg: Optional[float] = None, max_iter: int = 20) -> Optional[dict]:
    """Maximum-likelihood emitter position from ≥2 LoBs by iteratively-reweighted
    Gauss-Newton on the angular residuals (minimise Σ (Δθ_i / σ_i)²).  Returns a
    dict with the lat/lon estimate, the 2×2 ENU position covariance, the position
    σ (m), geometric GDOP (m per rad of bearing error), and the residual RMS (deg).
    None if fewer than 2 LoBs."""
    n = len(members)
    if n < 2:
        return None
    lat0 = sum(l.lat for l in members) / n
    lon0 = sum(l.lon for l in members) / n
    mpd_lon = _mpd_lon(lat0)
    obs = [((l.lon - lon0) * mpd_lon, (l.lat - lat0) * M_PER_DEG_LAT,
            math.radians(l.azimuth_deg), math.radians(lob_sigma_deg(l, rx_hpbw_deg))) for l in members]
    # initial guess: centroid of pairwise intersections, else project the
    # highest-confidence LoB out to its RSSI-derived range
    ints = _pairwise_intersections(members)
    if ints:
        c = _centroid(ints)
        x, y = (c["lon"] - lon0) * mpd_lon, (c["lat"] - lat0) * M_PER_DEG_LAT
    else:
        l = max(members, key=lambda l: (l.confidence_pct or 0.0))
        x = (l.lon - lon0) * mpd_lon + math.sin(math.radians(l.azimuth_deg)) * l.estimated_distance_m
        y = (l.lat - lat0) * M_PER_DEG_LAT + math.cos(math.radians(l.azimuth_deg)) * l.estimated_distance_m
    chi2 = 0.0
    m_used = 0
    for _ in range(max_iter):
        JtWJ = np.zeros((2, 2))
        JtWr = np.zeros(2)
        chi2 = 0.0
        m_used = 0
        for (ox, oy, th, sig) in obs:
            dx, dy = x - ox, y - oy
            d2 = dx * dx + dy * dy
            if d2 < 1.0:
                continue
            beta = math.atan2(dx, dy)              # predicted bearing (east, north → from N)
            r = _wrap_pi(th - beta)
            jx, jy = dy / d2, -dx / d2             # ∂β/∂(x,y) — model Jacobian (Gauss-Newton: p ← p + (JᵀWJ)⁻¹JᵀWr)
            w = 1.0 / (sig * sig)
            JtWJ[0, 0] += jx * jx * w; JtWJ[0, 1] += jx * jy * w
            JtWJ[1, 0] += jx * jy * w; JtWJ[1, 1] += jy * jy * w
            JtWr[0] += jx * r * w; JtWr[1] += jy * r * w
            chi2 += r * r * w
            m_used += 1
        if m_used < 2:
            return None
        try:
            step = np.linalg.solve(JtWJ, JtWr)
        except np.linalg.LinAlgError:
            break
        x += float(step[0]); y += float(step[1])
        if abs(step[0]) + abs(step[1]) < 0.5:
            break
    # covariance, residuals, GDOP
    try:
        cov0 = np.linalg.inv(JtWJ)
    except np.linalg.LinAlgError:
        cov0 = np.array([[1e10, 0.0], [0.0, 1e10]])
    dof = max(1, m_used - 2)
    scale = max(1.0, chi2 / dof) if m_used > 2 else 1.0     # inflate if the fit is poor
    cov = cov0 * scale
    # geometry-only Jacobian (unweighted) → GDOP, and the plain residual RMS
    JtJ = np.zeros((2, 2))
    sq_res = 0.0
    for (ox, oy, th, sig) in obs:
        dx, dy = x - ox, y - oy
        d2 = dx * dx + dy * dy
        if d2 < 1.0:
            continue
        beta = math.atan2(dx, dy)
        rr = _wrap_pi(th - beta)
        sq_res += rr * rr
        jx, jy = dy / d2, -dx / d2
        JtJ[0, 0] += jx * jx; JtJ[0, 1] += jx * jy
        JtJ[1, 0] += jx * jy; JtJ[1, 1] += jy * jy
    try:
        gdop = math.sqrt(max(0.0, float(np.trace(np.linalg.inv(JtJ)))))   # metres per radian
    except np.linalg.LinAlgError:
        gdop = float("inf")
    residual_rms_deg = math.degrees(math.sqrt(sq_res / max(1, m_used)))
    emitter_lat = lat0 + y / M_PER_DEG_LAT
    emitter_lon = lon0 + x / mpd_lon
    pos_sigma_m = math.sqrt(max(0.0, float(np.trace(cov))))
    return {
        "lat": emitter_lat, "lon": emitter_lon,
        "covariance_enu": [[float(cov[0, 0]), float(cov[0, 1])], [float(cov[1, 0]), float(cov[1, 1])]],
        "position_sigma_m": pos_sigma_m, "gdop": gdop, "residual_rms_deg": residual_rms_deg,
        "n_lobs": n, "_lat0": lat0, "_lon0": lon0, "_mpd_lon": mpd_lon, "_cov": cov,
    }


# ─────────────────────────────────────────────────────────────────────────────
# EKF emitter track — used by the live SDR/DF pipeline to smooth a stream of
# independent fixes (constant-velocity model in ENU about a fixed origin).
# ─────────────────────────────────────────────────────────────────────────────
class EmitterTrack:
    """Constant-velocity extended Kalman filter on (x, y, vx, vy) in ENU metres
    about a fixed lat/lon origin. `update(fix)` ingests an `ml_fix()` result
    (position + its covariance as the measurement noise R). `state()` returns the
    current smoothed lat/lon plus a 1-σ position from the filter covariance."""

    def __init__(self, lat0: float, lon0: float, accel_psd: float = 1.0) -> None:
        self.lat0, self.lon0, self.mpd_lon = lat0, lon0, _mpd_lon(lat0)
        self.q = float(accel_psd)                      # process-noise PSD (m²/s³)
        self.x = np.zeros(4)
        self.P = np.diag([1e8, 1e8, 1e4, 1e4]).astype(float)
        self.t = None
        self._init = False

    def _to_xy(self, lat: float, lon: float) -> tuple[float, float]:
        return (lon - self.lon0) * self.mpd_lon, (lat - self.lat0) * M_PER_DEG_LAT

    def _to_ll(self, x: float, y: float) -> tuple[float, float]:
        return self.lat0 + y / M_PER_DEG_LAT, self.lon0 + x / self.mpd_lon

    def predict(self, t: float) -> None:
        if self.t is None or not self._init:
            self.t = t
            return
        dt = max(0.0, float(t) - self.t)
        self.t = t
        if dt == 0.0:
            return
        F = np.array([[1, 0, dt, 0], [0, 1, 0, dt], [0, 0, 1, 0], [0, 0, 0, 1]], dtype=float)
        q = self.q
        Q = q * np.array([
            [dt**3 / 3, 0, dt**2 / 2, 0],
            [0, dt**3 / 3, 0, dt**2 / 2],
            [dt**2 / 2, 0, dt, 0],
            [0, dt**2 / 2, 0, dt],
        ], dtype=float)
        self.x = F @ self.x
        self.P = F @ self.P @ F.T + Q

    def update(self, fix: dict, t: Optional[float] = None) -> None:
        if t is not None:
            self.predict(t)
        zx, zy = self._to_xy(fix["lat"], fix["lon"])
        R = np.asarray(fix.get("_cov", fix.get("covariance_enu", [[1e6, 0], [0, 1e6]])), dtype=float)
        if not self._init:
            self.x = np.array([zx, zy, 0.0, 0.0])
            self.P = np.diag([R[0, 0], R[1, 1], 1e4, 1e4]).astype(float)
            self._init = True
            return
        H = np.array([[1, 0, 0, 0], [0, 1, 0, 0]], dtype=float)
        y_innov = np.array([zx, zy]) - H @ self.x
        S = H @ self.P @ H.T + R
        try:
            K = self.P @ H.T @ np.linalg.inv(S)
        except np.linalg.LinAlgError:
            return
        self.x = self.x + K @ y_innov
        self.P = (np.eye(4) - K @ H) @ self.P

    def state(self) -> dict:
        lat, lon = self._to_ll(float(self.x[0]), float(self.x[1]))
        covpos = self.P[:2, :2]
        return {
            "lat": lat, "lon": lon,
            "speed_mps": math.hypot(float(self.x[2]), float(self.x[3])),
            "heading_deg": (math.degrees(math.atan2(float(self.x[2]), float(self.x[3]))) % 360.0),
            "position_sigma_m": math.sqrt(max(0.0, float(np.trace(covpos)))),
            "covariance_enu": [[float(covpos[0, 0]), float(covpos[0, 1])], [float(covpos[1, 0]), float(covpos[1, 1])]],
            "initialised": self._init,
        }


# ─────────────────────────────────────────────────────────────────────────────
# entry point
# ─────────────────────────────────────────────────────────────────────────────
def solve_fix(observations: list[dict], options: Optional[dict] = None) -> dict:
    """Solve emitter fixes from a list of LoB observations.

    `observations`: list of dicts matching :class:`LoB` fields.
    `options`: ``{"rx_hpbw_deg": float|None, "lob_length_m": float|None,
                  "ellipse_conf": 0.50|0.95|...}`` (default ellipse confidence 0.95).

    Returns ``{"groups": [...], "geojson": FeatureCollection}``. Each group now
    carries the ML estimate, the ENU position covariance, GDOP, residual RMS and
    CEP/95 % radii; the rendered ``cep_ellipse`` feature is the covariance error
    ellipse (geometry-correct), and a second ``error_ellipse_95`` feature is added.
    """
    options = options or {}
    rx_hpbw = options.get("rx_hpbw_deg")
    lob_len = options.get("lob_length_m")
    conf = float(options.get("ellipse_conf", 0.95))

    lobs = [LoB(**{k: v for k, v in o.items() if k in LoB.__dataclass_fields__}) for o in observations]  # type: ignore[attr-defined]
    for idx, l in enumerate(lobs):
        if l.id is None:
            l.id = f"lob{idx}"

    features: list[dict] = []
    results: list[dict] = []

    for gi, g in enumerate(_group(lobs)):
        members: list[LoB] = g["lobs"]
        kind = "fix" if len(members) >= 3 else "cut" if len(members) == 2 else "lob"

        # bearing wedges (one LineString per LoB)
        for l in members:
            length = lob_len if (lob_len and lob_len > 0) else l.estimated_distance_m
            end_lat, end_lon = destination_point(l.lat, l.lon, l.azimuth_deg, length)
            features.append({
                "type": "Feature",
                "geometry": {"type": "LineString", "coordinates": [[l.lon, l.lat], [end_lon, end_lat]]},
                "properties": {"type": "lob", "group": gi, "lob_id": l.id, "azimuth_deg": l.azimuth_deg,
                               "rssi_dbm": l.rssi_dbm, "frequency_hz": l.frequency_hz,
                               "confidence_pct": l.confidence_pct, "length_m": round(length),
                               "sigma_deg": round(lob_sigma_deg(l, rx_hpbw), 2),
                               "device_type": l.device_type, "device_id": l.device_id},
            })

        fix = ml_fix(members, rx_hpbw_deg=rx_hpbw) if len(members) >= 2 else None
        intersections = _pairwise_intersections(members) if len(members) >= 2 else []

        ellipse_props = None
        if fix is not None:
            cov = fix["_cov"]
            smaj95, smin95, rot95 = error_ellipse_from_cov(cov, conf)
            smaj50, smin50, rot50 = error_ellipse_from_cov(cov, 0.50)
            cep_m = cep_from_cov(cov)
            centroid = {"lat": fix["lat"], "lon": fix["lon"]}
            ellipse_props = {
                "type": "cep_ellipse", "semiMajorM": round(smaj50), "semiMinorM": round(smin50),
                "centerLat": fix["lat"], "centerLon": fix["lon"], "rotDeg": round(rot50, 1),
                "cep50_m": round(cep_m), "r95_semiMajorM": round(smaj95), "r95_semiMinorM": round(smin95),
                "gdop": round(fix["gdop"], 1) if math.isfinite(fix["gdop"]) else None,
                "positionSigmaM": round(fix["position_sigma_m"]),
                "residualRmsDeg": round(fix["residual_rms_deg"], 2),
            }
            # 50% (CEP-equivalent) ellipse — kept under the legacy `cep_ellipse` tag
            features.append({
                "type": "Feature",
                "geometry": {"type": "Polygon", "coordinates": [_ellipse_polygon(fix["lat"], fix["lon"], smaj50, smin50, rot50)]},
                "properties": {**ellipse_props, "group": gi},
            })
            # 95% ellipse (new) — the "where the emitter actually is" region
            features.append({
                "type": "Feature",
                "geometry": {"type": "Polygon", "coordinates": [_ellipse_polygon(fix["lat"], fix["lon"], smaj95, smin95, rot95)]},
                "properties": {"type": "error_ellipse_95", "group": gi, "confidence": conf,
                               "semiMajorM": round(smaj95), "semiMinorM": round(smin95), "rotDeg": round(rot95, 1)},
            })
            features.append({
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [fix["lon"], fix["lat"]]},
                "properties": {"type": "suspected_emitter", "group": gi, "kind": kind,
                               "frequency_hz": g["frequency_hz"], "device_type": g["device_type"],
                               "device_id": g["device_id"], "n_lobs": len(members),
                               "method": "ml", "gdop": ellipse_props["gdop"],
                               "cep_m": round(cep_m), "position_sigma_m": round(fix["position_sigma_m"]),
                               "residual_rms_deg": ellipse_props["residualRmsDeg"],
                               "cep_semi_major_m": round(smaj50)},
            })
        else:
            centroid = None

        results.append({
            "group": gi, "kind": kind, "frequency_hz": g["frequency_hz"],
            "device_type": g["device_type"], "device_id": g["device_id"],
            "n_lobs": len(members), "n_intersections": len(intersections),
            "method": ("ml" if fix is not None else "lob"),
            "centroid": centroid,
            "cep": ellipse_props,
            "gdop": (round(fix["gdop"], 1) if (fix is not None and math.isfinite(fix["gdop"])) else None),
            "position_sigma_m": (round(fix["position_sigma_m"]) if fix is not None else None),
            "residual_rms_deg": (round(fix["residual_rms_deg"], 2) if fix is not None else None),
            "covariance_enu": (fix["covariance_enu"] if fix is not None else None),
            "lob_ids": [l.id for l in members],
        })

    return {"groups": results, "geojson": {"type": "FeatureCollection", "features": features}}
