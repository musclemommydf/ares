# SPDX-License-Identifier: MIT OR Apache-2.0
# Copyright (c) 2026 Ares

"""
native.py — optional Rust acceleration shim (Track D, D4).

Imports the ``ares_native`` extension (built from ``backend/native`` via
``scripts/build-native.sh``) when present, and exposes a pure-Python fallback for
every accelerated kernel so Ares always runs — built wheel or not (the project's
rule: DSP stays local/in-process, no hard Rust dependency). Hot paths get
promoted into the extension only when their D4 profiling trigger fires; callers
use the wrappers here and never import ``ares_native`` directly.
"""
from __future__ import annotations

import os

try:
    if os.getenv("ARES_NO_NATIVE", "").lower() in ("1", "true", "yes"):
        raise ImportError("native disabled via ARES_NO_NATIVE")
    import ares_native as _native  # type: ignore
    HAS_NATIVE = True
except Exception:
    _native = None
    HAS_NATIVE = False


def native_version() -> str | None:
    """Version string of the loaded extension, or None if running pure-Python."""
    return _native.version() if HAS_NATIVE else None


def sum_squares(xs) -> float:
    """Reference kernel: Σ x². Uses the Rust path when available, else Python.
    Exists to verify dispatch + numeric parity between the two paths."""
    if HAS_NATIVE:
        return _native.sum_squares(list(xs))
    return float(sum(x * x for x in xs))


# ── ported hot loops (callers fall back to pure Python when HAS_NATIVE is False) ──
def diffraction_db(model, elevations, distances, tx_height_m, rx_height_m, freq_hz) -> float:
    """Native terrain diffraction (diffraction.py port). Call only when HAS_NATIVE."""
    return _native.diffraction_db(model, list(elevations), list(distances),
                                  float(tx_height_m), float(rx_height_m), float(freq_hz))


def itm_hzns(pfl, hg0, hg1, gme, dist):
    """Native ITM horizon analysis → (the0, the1, dl0, dl1). Call only when HAS_NATIVE."""
    return _native.itm_hzns([float(x) for x in pfl], float(hg0), float(hg1), float(gme), float(dist))


def rs_decode_204(code204):
    """Native RS(204,188) decode → (data188_bytes | None, n_errors). Call only when HAS_NATIVE."""
    res, nerr = _native.rs_decode_204(list(code204))
    return (bytes(res) if res is not None else None), nerr


def dvb_derandomise(packets):
    """Native DVB energy-dispersal de-randomiser → bytes. Call only when HAS_NATIVE."""
    return bytes(_native.dvb_derandomise(list(packets)))


def viterbi_decode(soft_pairs, terminated):
    """Native DVB-T soft Viterbi → list[int] bits. Call only when HAS_NATIVE."""
    return _native.viterbi_decode([float(x) for x in soft_pairs], bool(terminated))
