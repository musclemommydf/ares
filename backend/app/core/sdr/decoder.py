# SPDX-License-Identifier: MIT OR Apache-2.0
# Copyright (c) 2026 Ares

"""
sdr/decoder.py — drive an installed external digital decoder from the live baseband.

The trunked digital voice vocoders (AMBE for DMR/P25/NXDN/dPMR/ProVoice, codec2 for
M17) are licensed and can't be vendored — so for the digital modes Ares pipes the
**FM-discriminated baseband** into the operator's installed decoder over stdin and
reads its output back:

  * voice decoders (dsd-fme, m17-demod) write synthesized PCM to stdout → streamed
    to the browser like the analog audio;
  * data decoders (multimon-ng for POCSAG/FLEX) write decoded text lines to stdout →
    surfaced as decode messages.

A `DigitalDecoder` owns the subprocess and two lock-free deques the adapter/WS drain.
"""
from __future__ import annotations

import collections
import logging
import shutil
import subprocess
import threading

log = logging.getLogger(__name__)


def _which(*names: str):
    for n in names:
        p = shutil.which(n)
        if p:
            return p
    return None


# multimon-ng demodulator sets per data mode
_MMNG = {
    "pocsag": ["-a", "POCSAG512", "-a", "POCSAG1200", "-a", "POCSAG2400"],
    "flex":   ["-a", "FLEX", "-a", "FLEX_NEXT"],
}
# digital voice modes dsd-fme handles (auto-detect frame type with -fa)
_DSD_MODES = {"dmr", "dpmr", "p25p1", "nxdn48", "nxdn96", "dstar", "ysf", "provoice"}


def spec_for(mode: str) -> dict | None:
    """Resolve a mode → an invocable decoder spec, or None if none is installed.
    ``in_rate`` is the discriminator-feed rate the decoder expects on stdin."""
    mode = (mode or "").lower()
    if mode == "m17":
        b = _which("m17-demod")
        if b:
            return {"decoder": "m17-demod", "argv": [b, "-q"], "in_rate": 48000, "out": "audio", "out_rate": 8000}
        b = _which("dsd-fme", "dsd_fme")
        if b:
            return {"decoder": "dsd-fme", "argv": [b, "-i", "-", "-o", "-", "-fa"], "in_rate": 48000, "out": "audio", "out_rate": 8000}
        return None
    if mode in _DSD_MODES or mode in ("p25p2", "tetra"):
        # dsd-fme covers DMR/dPMR/P25p1/NXDN/D-STAR/YSF/ProVoice (auto). P25p2/TETRA
        # need op25/tetra-rx — try dsd-fme anyway (it handles some), else give up.
        b = _which("dsd-fme", "dsd_fme")
        if b and mode not in ("p25p2", "tetra"):
            return {"decoder": "dsd-fme", "argv": [b, "-i", "-", "-o", "-", "-fa"], "in_rate": 48000, "out": "audio", "out_rate": 8000}
        return None
    if mode in _MMNG:
        b = _which("multimon-ng")
        if b:
            return {"decoder": "multimon-ng", "argv": [b, "-t", "raw", "-c", *_MMNG[mode], "-f", "alpha", "-q", "-"],
                    "in_rate": 22050, "out": "text"}
        return None
    return None


def decoders_for(mode: str) -> list:
    """The decoder programs that would decode `mode` (for an actionable error)."""
    try:
        from . import dsp
        m = next((x for x in dsp.AUDIO_MODES if x["id"] == (mode or "").lower()), None)
        return [d for d in (m or {}).get("decoders", []) if d != "builtin"]
    except Exception:
        return []


class DigitalDecoder:
    def __init__(self, spec: dict):
        self.spec = spec
        self.decoder = spec["decoder"]
        self.in_rate = int(spec["in_rate"])
        self.out_kind = spec["out"]              # "audio" | "text"
        self.out_rate = int(spec.get("out_rate", 8000))
        self._proc = None
        self._stop = False
        self._audio = collections.deque(maxlen=256)   # decoded PCM (bytes)
        self._text = collections.deque(maxlen=512)    # decoded lines (str)

    def start(self) -> "DigitalDecoder":
        self._proc = subprocess.Popen(self.spec["argv"], stdin=subprocess.PIPE,
                                      stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0)
        target = self._read_audio if self.out_kind == "audio" else self._read_text
        threading.Thread(target=target, daemon=True).start()
        log.info("digital decoder started: %s (%s, in %d Hz)", self.decoder, " ".join(self.spec["argv"]), self.in_rate)
        return self

    def feed(self, pcm_bytes: bytes) -> None:
        p = self._proc
        if p and p.stdin and not self._stop:
            try:
                p.stdin.write(pcm_bytes); p.stdin.flush()
            except (BrokenPipeError, ValueError, OSError):
                pass

    def _read_audio(self) -> None:
        p = self._proc
        while not self._stop and p and p.stdout:
            b = p.stdout.read(4096)
            if not b:
                break
            self._audio.append(b)

    def _read_text(self) -> None:
        p = self._proc
        while not self._stop and p and p.stdout:
            line = p.stdout.readline()
            if not line:
                break
            s = line.decode("utf-8", "replace").rstrip()
            if s:
                self._text.append(s)

    def audio_chunks(self) -> list:
        out = []
        while self._audio:
            out.append(self._audio.popleft())
        return out

    def text_lines(self) -> list:
        out = []
        while self._text:
            out.append(self._text.popleft())
        return out

    def stop(self) -> None:
        self._stop = True
        p = self._proc
        self._proc = None
        if not p:
            return
        for fn in (lambda: p.stdin and p.stdin.close(), p.terminate):
            try:
                fn()
            except Exception:
                pass
        try:
            p.wait(timeout=1.0)
        except Exception:
            try:
                p.kill()
            except Exception:
                pass
