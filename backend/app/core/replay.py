# SPDX-License-Identifier: MIT OR Apache-2.0
# Copyright (c) 2026 Ares

"""
SigMF replay — load a `.sigmf-data` file and drive the in-process DSP / DF
pipeline as if it were live.

Workflow:
    1. Operator captures IQ in the field with a real driver.
    2. Each capture is paired with a `.sigmf-meta` JSON (see app.core.sigmf).
    3. Back at base, operator opens "Replay" → picks a capture → presses ▶.
    4. We open both files, stream IQ in fixed-size blocks at the original
       sample rate (or faster/slower scaled), and feed the same channelizer
       / DSP path the live pipeline uses.

This makes captures fully re-processable with different algorithms (e.g. try
MUSIC with 4 sources vs 2, or run a fresh modulation classifier on an old
event without going back to the field).
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import time
from pathlib import Path
from typing import AsyncIterator, Optional

import numpy as np

log = logging.getLogger(__name__)


_DTYPE_MAP: dict[str, np.dtype] = {
    "cf32_le": np.dtype("<c8"),                # complex64 little-endian
    "cf64_le": np.dtype("<c16"),
    "ci16_le": np.dtype("<i2"),                # interleaved int16 → caller scales
    "ci8":     np.dtype("i1"),
    "rf32_le": np.dtype("<f4"),                # real-valued float32 (rare)
}


def _coerce_iq(raw: np.ndarray, datatype: str) -> np.ndarray:
    """Convert raw bytes-as-array into complex64."""
    dt = _DTYPE_MAP.get(datatype, np.dtype("<c8"))
    if dt.kind == "c":
        return raw.astype(np.complex64)
    if dt.kind == "i":
        # Interleaved IQ as signed int → /max
        s = raw.astype(np.float32) / float(np.iinfo(dt).max)
        return s[0::2] + 1j * s[1::2]
    return raw.astype(np.complex64)


def open_sigmf(meta_path: str | Path) -> dict:
    """Return { meta, data_path, datatype, sample_rate_hz, n_samples }.
    Does not load samples — that's lazy via `iter_blocks()`."""
    mp = Path(meta_path)
    if not mp.exists():
        raise FileNotFoundError(meta_path)
    meta = json.loads(mp.read_text())
    g = meta.get("global", {})
    datatype = g.get("core:datatype", "cf32_le")
    sample_rate = float(g.get("core:sample_rate", 0))
    if mp.suffix == ".sigmf-meta":
        data_path = mp.with_suffix(".sigmf-data")
    else:
        # double-extension layout (foo.iq.sigmf-meta → foo.iq)
        data_path = mp.with_name(mp.name[: -len(".sigmf-meta")])
    if not data_path.exists():
        # Some tools save companion as `.cf32` or `.bin`
        for ext in (".cf32", ".bin", ".raw", ".iq"):
            cand = mp.with_suffix(ext)
            if cand.exists():
                data_path = cand; break
    if not data_path.exists():
        raise FileNotFoundError(f"no matching IQ data file for {meta_path}")
    dt = _DTYPE_MAP.get(datatype, np.dtype("<c8"))
    n_bytes = data_path.stat().st_size
    n_samples = n_bytes // (dt.itemsize * (2 if dt.kind != "c" else 1))
    return {
        "meta_path": str(mp), "data_path": str(data_path),
        "meta": meta, "datatype": datatype,
        "sample_rate_hz": sample_rate, "n_samples": int(n_samples),
    }


def iter_blocks(meta_path: str | Path, block_size: int = 65536) -> AsyncIterator[np.ndarray]:
    """Async generator that yields complex64 IQ blocks. Wraps `open_sigmf` +
    `np.fromfile` for laziness. Optionally pauses to honour real-time playback
    rates via `pace()` below."""
    info = open_sigmf(meta_path)
    datatype = info["datatype"]; dt = _DTYPE_MAP.get(datatype, np.dtype("<c8"))
    data_path = info["data_path"]

    async def _gen():
        with open(data_path, "rb") as f:
            n_per_block = block_size
            element_bytes = dt.itemsize * (2 if dt.kind != "c" else 1)
            chunk_bytes = n_per_block * element_bytes
            while True:
                raw = f.read(chunk_bytes)
                if not raw:
                    return
                arr = np.frombuffer(raw, dtype=dt)
                iq = _coerce_iq(arr, datatype)
                yield iq

    return _gen()


def pace(iq: np.ndarray, sample_rate_hz: float, speed: float = 1.0) -> float:
    """Sleep duration needed to play a block in real time at `speed`× rate."""
    if sample_rate_hz <= 0 or speed <= 0:
        return 0.0
    return (iq.size / sample_rate_hz) / speed


def annotate_meta(meta_path: str | Path, annotation: dict) -> dict:
    """Append an annotation (operator note) into the SigMF meta. The format
    follows the standard: { sample_start, sample_count, comment, freq_lower_edge,
    freq_upper_edge, … }."""
    mp = Path(meta_path)
    meta = json.loads(mp.read_text())
    meta.setdefault("annotations", []).append(annotation)
    mp.write_text(json.dumps(meta, indent=2))
    return meta


def list_recordings(root: Optional[str | Path] = None) -> list[dict]:
    """List candidate SigMF captures under `data/recordings/` and `data/iq/`."""
    bases: list[Path] = []
    if root:
        bases.append(Path(root))
    else:
        env = os.environ.get("ARES_DATA_DIR", "data")
        for sub in ("recordings", "iq"):
            bases.append(Path(env) / sub)
    out = []
    seen: set[str] = set()
    for base in bases:
        if not base.exists():
            continue
        for p in sorted(base.rglob("*.sigmf-meta")):
            try:
                info = open_sigmf(p)
                key = str(info["data_path"])
                if key in seen: continue
                seen.add(key)
                g = info["meta"].get("global", {})
                out.append({
                    "meta_path": info["meta_path"], "data_path": info["data_path"],
                    "sample_rate_hz": info["sample_rate_hz"], "datatype": info["datatype"],
                    "n_samples": info["n_samples"],
                    "datetime": g.get("core:datetime"),
                    "hw": g.get("core:hw"),
                    "capture_freq_hz": (info["meta"].get("captures") or [{}])[0].get("core:frequency"),
                })
            except Exception:
                continue
    return out
