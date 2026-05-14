"""
Per-channel calibration for coherent DF arrays.

For coherent multi-channel SDRs (KrakenSDR, ANTSDR e200 multi-input, USRP
X4xx, etc.) every receive chain has its own amplitude/phase response. Without
correction, even a perfect array geometry will mis-direct. Calibration is
captured once with a known reference (broadband noise from a hat-coupled
splitter, or a known beacon at a known true bearing) and applied to every IQ
frame before covariance estimation.

Two correction modes:
  - 'noise_coupling'  → from a coherent reference injected to all elements
                        (Kraken's HeIMDALL DAQ uses this). Solves R_meas = D R_ref D^H
                        where D is the diagonal complex gain vector.
  - 'reference_beacon'→ from a single known DoA. Solves a per-element complex
                        gain that drives the measured response to the steering
                        vector for that DoA.

Both produce a length-M complex gain vector `d` such that
    iq_calibrated = iq / d[:, None]
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

import numpy as np

from .arrays import ArrayGeometry, steering_vector


def calibrate_from_noise_coupling(iq_ref: np.ndarray, ref_channel: int = 0) -> np.ndarray:
    """Calibrate from a coherent noise source coupled to all elements. Returns
    a length-M complex gain vector. `iq_ref` is (M, N)."""
    iq_ref = np.asarray(iq_ref, dtype=complex)
    M, N = iq_ref.shape
    # Estimate per-channel response relative to ref_channel via inner products.
    ref = iq_ref[ref_channel]
    d = np.zeros(M, dtype=complex)
    norm = np.vdot(ref, ref).real
    for m in range(M):
        d[m] = np.vdot(ref, iq_ref[m]) / max(norm, 1e-12)
    d[ref_channel] = 1.0 + 0j
    return d


def calibrate_from_beacon(iq: np.ndarray, geom: ArrayGeometry, freq_hz: float,
                          true_az_deg: float, elevation_deg: float = 0.0) -> np.ndarray:
    """Calibrate from a beacon at known true bearing. Returns per-element
    complex gain vector that aligns the measured covariance's principal
    eigenvector with the ideal steering vector."""
    iq = np.asarray(iq, dtype=complex)
    M, _ = iq.shape
    R = (iq @ iq.conj().T) / iq.shape[1]
    R = (R + R.conj().T) / 2
    w, V = np.linalg.eigh(R)
    v_measured = V[:, np.argmax(w)]                              # principal eigvec
    v_ideal = steering_vector(geom, freq_hz, true_az_deg, elevation_deg)
    # Per-element complex gain that maps measured → ideal (element-wise division).
    d = v_measured / v_ideal
    # Normalise so the first element is 1.0 (arbitrary global phase).
    d = d / d[0] if abs(d[0]) > 1e-12 else d
    return d


def apply_gain(iq: np.ndarray, d: Optional[np.ndarray]) -> np.ndarray:
    """Divide each channel's IQ stream by its complex gain (no-op when d is None)."""
    if d is None:
        return iq
    iq = np.asarray(iq, dtype=complex)
    d = np.asarray(d, dtype=complex).reshape(-1)
    if d.shape[0] != iq.shape[0]:
        raise ValueError(f"gain vector length {d.shape[0]} != channels {iq.shape[0]}")
    return iq / d[:, None]


def save_calibration(path: str | Path, device_id: str, d: np.ndarray,
                     metadata: Optional[dict] = None) -> Path:
    """Persist a calibration to disk as JSON (complex gain split into amp+phase)."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "device_id": device_id,
        "n_channels": int(len(d)),
        "amplitude": [float(np.abs(x)) for x in d],
        "phase_deg": [float(np.degrees(np.angle(x))) for x in d],
        **(metadata or {}),
    }
    p.write_text(json.dumps(payload, indent=2))
    return p


def load_calibration(path: str | Path) -> np.ndarray:
    """Load a previously-saved calibration. Returns the M complex gain vector."""
    raw = json.loads(Path(path).read_text())
    amps = np.array(raw["amplitude"], dtype=float)
    phs = np.radians(np.array(raw["phase_deg"], dtype=float))
    return amps * np.exp(1j * phs)
