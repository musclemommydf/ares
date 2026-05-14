"""
Moving-platform compensation for an AoA measurement.

When the DF array is mounted on a vehicle / aircraft / boat / drone, the body
rotates as the platform turns. The array's *measured* bearing (relative to
the array body) must be rotated by the platform heading to recover a true-
north bearing before plotting/fusing.

Two inputs:
  - relative_bearing_deg : θ as measured by MUSIC / Capon / interferometer
                            (degrees off the array's +y reference)
  - platform_heading_deg : direction the platform's nose is pointing, in true
                            degrees (typically from GPS course-made-good or a
                            magnetic compass corrected for declination).

Output: true bearing = (platform_heading + relative_bearing) mod 360°.

Also exposes a velocity-vector helper for when the GPS yields course +
speed and the operator wants a quick sanity check ("am I moving fast enough
that the AoA can change appreciably during one capture?").
"""

from __future__ import annotations

import math


def to_true_bearing(relative_bearing_deg: float, platform_heading_deg: float) -> float:
    """Rotate a body-frame bearing into the true-north frame."""
    return ((float(platform_heading_deg) + float(relative_bearing_deg)) + 360.0) % 360.0


def to_body_bearing(true_bearing_deg: float, platform_heading_deg: float) -> float:
    """Inverse: true → body-frame bearing (for verifying calibration)."""
    return ((float(true_bearing_deg) - float(platform_heading_deg)) + 360.0) % 360.0


def smear_warning(speed_mps: float, capture_duration_s: float, max_acceptable_deg: float = 1.0) -> dict:
    """Heuristic: is the platform turning so fast that a single capture window
    smears the AoA more than `max_acceptable_deg`? Returns an advisory."""
    # Rough proxy: at 50 m/s ground speed for 0.1 s, a 1° heading change happens
    # when angular velocity ≈ 10 deg/s. Caller actually has GPS so use real ω
    # when available; this is a "shorten your capture" hint based on linear speed.
    if speed_mps <= 0 or capture_duration_s <= 0:
        return {"smear_deg_estimate": 0.0, "ok": True}
    # Worst-case smear at small target range: angular subtense = (v · t / r).
    # Use r=1km as a conservative anchor (≈ 0.057 deg/m at 1km).
    smear_deg = (speed_mps * capture_duration_s) * (180.0 / math.pi) / 1000.0
    return {
        "smear_deg_estimate": smear_deg,
        "ok": smear_deg <= max_acceptable_deg,
        "advice": ("shorten capture or stop the platform" if smear_deg > max_acceptable_deg else "ok"),
    }


def rotate_array_geometry(positions_m, heading_deg: float):
    """Rotate an (M, 2) array of element positions by the platform heading.
    Useful when you want to perform fusion across multiple moving snapshots
    of the same emitter and don't want to apply the rotation in the bearing
    domain. East-north preserved; heading is true-north degrees clockwise."""
    import numpy as np
    h = math.radians(float(heading_deg))
    # Rotation by +heading puts the body's +y (forward) at +heading in earth.
    # The array's body-east becomes (sin h, cos h)·east + (cos h, −sin h)·north.
    rot = np.array([[math.cos(h), math.sin(h)],
                    [-math.sin(h), math.cos(h)]])
    return (rot @ np.asarray(positions_m, dtype=float).T).T
