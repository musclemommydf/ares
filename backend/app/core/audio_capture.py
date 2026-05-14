"""
Tagged audio capture — WAV + sidecar JSON for every ▶ Listen session.

When the operator demods a signal and starts listening, we open a recording
session that writes mono/stereo PCM to a `.wav` and a sidecar `.json` with:

  { "frequency_hz", "mode", "device_id", "observer": {lat, lon, callsign},
    "started_t", "stopped_t", "sample_rate_hz", "channels",
    "track_id" (when associated with an emitter track),
    "talkgroup": null,        # filled by trunked-decode plugins
    "tags": ["alpha-net"] }   # operator-supplied

Files go under `data/recordings/{YYYY}/{MM}/{DD}/`. Always sample-accurate
because we use Python's built-in `wave` for the framing — no third-party deps.
"""

from __future__ import annotations

import json
import os
import time
import wave
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import numpy as np


def _root() -> Path:
    p = Path(os.environ.get("ARES_DATA_DIR", "data")) / "recordings"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _session_paths(started_t: float, suffix: str = "") -> tuple[Path, Path]:
    t = time.gmtime(started_t)
    dirp = _root() / f"{t.tm_year:04d}" / f"{t.tm_mon:02d}" / f"{t.tm_mday:02d}"
    dirp.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%dT%H%M%SZ", t)
    base = f"audio_{stamp}{('_' + suffix) if suffix else ''}"
    return dirp / f"{base}.wav", dirp / f"{base}.json"


@dataclass
class AudioRecorder:
    wav_path: Path
    meta_path: Path
    sample_rate_hz: int
    channels: int = 1
    bit_depth: int = 16
    metadata: dict = field(default_factory=dict)
    _writer: Optional[wave.Wave_write] = None
    _started_t: float = 0.0
    _n_samples: int = 0

    def open(self) -> "AudioRecorder":
        self._writer = wave.open(str(self.wav_path), "wb")
        self._writer.setnchannels(self.channels)
        self._writer.setsampwidth(self.bit_depth // 8)
        self._writer.setframerate(int(self.sample_rate_hz))
        self._started_t = time.time()
        return self

    def write(self, samples: np.ndarray) -> None:
        """`samples` ∈ float[-1, 1] mono or (channels, N). Quantises to int16."""
        if self._writer is None:
            raise RuntimeError("recorder not opened")
        s = np.asarray(samples, dtype=np.float32)
        if s.ndim == 1 and self.channels == 1:
            x = (np.clip(s, -1.0, 1.0) * (2 ** (self.bit_depth - 1) - 1)).astype(np.int16)
            self._writer.writeframes(x.tobytes())
            self._n_samples += len(x)
        elif s.ndim == 2 and s.shape[0] == self.channels:
            # interleave channels (L, R, L, R, …)
            x = (np.clip(s, -1.0, 1.0) * (2 ** (self.bit_depth - 1) - 1)).astype(np.int16)
            self._writer.writeframes(x.T.flatten().tobytes())
            self._n_samples += s.shape[1]
        else:
            raise ValueError(f"shape {s.shape} doesn't match channels={self.channels}")

    def close(self) -> dict:
        if self._writer is None:
            return self.metadata
        self._writer.close(); self._writer = None
        stopped = time.time()
        self.metadata.setdefault("started_t", self._started_t)
        self.metadata["stopped_t"] = stopped
        self.metadata["duration_s"] = round(stopped - self._started_t, 3)
        self.metadata["sample_rate_hz"] = int(self.sample_rate_hz)
        self.metadata["channels"] = int(self.channels)
        self.metadata["bit_depth"] = int(self.bit_depth)
        self.metadata["n_samples"] = int(self._n_samples)
        self.metadata["wav"] = str(self.wav_path.name)
        self.meta_path.write_text(json.dumps(self.metadata, indent=2))
        return self.metadata


def open_session(*, sample_rate_hz: int, frequency_hz: float = 0,
                  mode: str = "nfm", device_id: str = "",
                  observer: Optional[dict] = None,
                  track_id: Optional[str] = None,
                  tags: Optional[list[str]] = None,
                  channels: int = 1) -> AudioRecorder:
    """Open a new tagged-audio recording session."""
    started = time.time()
    wav_path, meta_path = _session_paths(started, suffix=f"{int(frequency_hz/1e3)}kHz" if frequency_hz else "")
    rec = AudioRecorder(
        wav_path=wav_path, meta_path=meta_path,
        sample_rate_hz=int(sample_rate_hz), channels=channels,
        metadata={
            "frequency_hz": float(frequency_hz),
            "mode": mode,
            "device_id": device_id,
            "observer": observer or {},
            "track_id": track_id,
            "tags": list(tags or []),
            "started_t": started,
        },
    )
    return rec.open()


def list_recordings(limit: int = 200) -> list[dict]:
    """List all recordings (newest first)."""
    out = []
    for meta in sorted(_root().rglob("*.json"), reverse=True):
        try:
            d = json.loads(meta.read_text())
            d["meta_path"] = str(meta)
            d["wav_path"] = str(meta.with_suffix(".wav"))
            out.append(d)
        except Exception:
            pass
        if len(out) >= limit:
            break
    return out
