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

try:
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
