"""
Array geometry helpers for DF / DoA.

Every estimator in `algorithms` needs (1) an (M, 2) array of element positions
in metres and (2) a steering-vector generator that, given a wavelength and a
target azimuth (and optionally elevation), returns the M-element complex phase
response.

Coordinate convention: x = east, y = north, both in metres relative to the
array centroid. Azimuth is degrees clockwise from north (true bearing).
Elevation is degrees above the horizon (0 = horizontal). The plane-wave
steering model is the standard far-field response:

    a_m(θ, φ) = exp( j · 2π/λ · (x_m·sin(θ)·cos(φ) + y_m·cos(θ)·cos(φ)) )

That puts boresight (θ=0, the +y / north direction) at zero phase across the
array, which matches every operator's intuition of "true bearing 0 = north."
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np


C_LIGHT = 299_792_458.0


@dataclass
class ArrayGeometry:
    """Element positions in metres, x=east, y=north. M elements ⇒ (M, 2)."""
    positions: np.ndarray
    label: str = "array"

    @property
    def n(self) -> int:
        return self.positions.shape[0]

    @classmethod
    def uca(cls, n: int, radius_m: float, label: str = "UCA") -> "ArrayGeometry":
        """Uniform circular array of `n` elements at `radius_m`. Element 0 sits at +y (north).
        This matches the KrakenSDR documentation convention and most academic DF papers."""
        angles = np.arange(n) * (2 * np.pi / n)
        x = radius_m * np.sin(angles)            # +x = east
        y = radius_m * np.cos(angles)            # +y = north (element 0 at +y)
        return cls(np.column_stack([x, y]), f"{label}(n={n}, r={radius_m:g}m)")

    @classmethod
    def ula(cls, n: int, spacing_m: float, axis_deg: float = 90.0, label: str = "ULA") -> "ArrayGeometry":
        """Uniform linear array of `n` elements with given inter-element spacing.
        `axis_deg` is the azimuth of the array's long axis (default 90° = east-west)."""
        ax = math.radians(axis_deg)
        idx = np.arange(n) - (n - 1) / 2.0
        x = idx * spacing_m * math.sin(ax)
        y = idx * spacing_m * math.cos(ax)
        return cls(np.column_stack([x, y]), f"{label}(n={n}, d={spacing_m:g}m, axis={axis_deg:g}°)")

    @classmethod
    def custom(cls, positions: np.ndarray, label: str = "custom") -> "ArrayGeometry":
        positions = np.asarray(positions, dtype=float)
        if positions.ndim != 2 or positions.shape[1] != 2:
            raise ValueError("positions must be (M, 2) — x_east, y_north in metres")
        # Centre on the array centroid so steering-vector phases are array-relative.
        return cls(positions - positions.mean(axis=0, keepdims=True), label)

    def to_dict(self) -> dict:
        return {"label": self.label, "n": self.n, "positions_m": self.positions.tolist()}


def steering_vector(geom: ArrayGeometry, freq_hz: float, azimuth_deg: float,
                    elevation_deg: float = 0.0) -> np.ndarray:
    """Plane-wave response of the array to a single source at (az, el).
    Returns an (M,) complex vector. Wavelength derived from `freq_hz`."""
    lam = C_LIGHT / freq_hz
    k = 2 * math.pi / lam
    az = math.radians(azimuth_deg)
    el = math.radians(elevation_deg)
    cos_el = math.cos(el)
    # Direction-of-arrival unit vector projected onto the array plane (x=east, y=north).
    dx = math.sin(az) * cos_el
    dy = math.cos(az) * cos_el
    return np.exp(1j * k * (geom.positions[:, 0] * dx + geom.positions[:, 1] * dy))


def steering_matrix(geom: ArrayGeometry, freq_hz: float,
                    az_deg: np.ndarray, elevation_deg: float = 0.0) -> np.ndarray:
    """Stack of steering vectors for a vector of azimuths. Returns (M, len(az))."""
    lam = C_LIGHT / freq_hz
    k = 2 * math.pi / lam
    az = np.radians(np.asarray(az_deg, dtype=float))
    cos_el = math.cos(math.radians(elevation_deg))
    dx = np.sin(az) * cos_el                                 # shape (K,)
    dy = np.cos(az) * cos_el                                 # shape (K,)
    # (M, K)
    phases = k * (geom.positions[:, 0:1] * dx[None, :] + geom.positions[:, 1:2] * dy[None, :])
    return np.exp(1j * phases)
