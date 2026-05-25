# SPDX-License-Identifier: MIT OR Apache-2.0
# Copyright (c) 2026 Ares

"""
afsk1200.py — Bell-202 AFSK1200 + HDLC modem for APRS (the RF front-end).

Turns FM-discriminator audio (or IQ) into AX.25 frames — the layer below
decoders/aprs.py, which then parses the APRS info field. APRS is 1200-baud
Bell-202 AFSK (mark 1200 Hz / space 2200 Hz) carrying NRZI-coded, bit-stuffed,
HDLC-framed AX.25 with an X.25 FCS.

Chain (demod):
    audio → mark/space correlator (numpy) → bit-sync DPLL → NRZI decode
          → HDLC flag-split + de-stuff → FCS check → AX.25 frame bytes

A matching modulator (`modulate`) builds APRS audio from frames — used by the
round-trip self-test and available for the SDR-as-NIC / TX path.

Receive-only in normal use; transmit is gated upstream (lawful-use policy). The
per-sample bit-sync/HDLC loops are pure Python but run per-capture (not per
packet), so they're left un-oxidised; the correlator is numpy.
"""
from __future__ import annotations

import numpy as np

MARK = 1200.0
SPACE = 2200.0
BAUD = 1200.0
_FLAG = [0, 1, 1, 1, 1, 1, 1, 0]   # HDLC flag 0x7E


# ── AX.25 FCS (CRC-16 X.25) ──────────────────────────────────────────────────
def fcs(data: bytes) -> int:
    crc = 0xFFFF
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = (crc >> 1) ^ 0x8408 if (crc & 1) else (crc >> 1)
    return crc ^ 0xFFFF


# ── modulator (frames → audio) ───────────────────────────────────────────────
def _hdlc_encode_bits(frame: bytes) -> list[int]:
    """frame (AX.25, no FCS) → flag + bit-stuffed [data‖FCS] + flag, LSB-first."""
    f = fcs(frame)
    payload = frame + bytes([f & 0xFF, (f >> 8) & 0xFF])
    raw: list[int] = []
    for byte in payload:
        for i in range(8):
            raw.append((byte >> i) & 1)        # LSB first
    stuffed: list[int] = []
    ones = 0
    for b in raw:
        stuffed.append(b)
        if b:
            ones += 1
            if ones == 5:
                stuffed.append(0)              # bit-stuff
                ones = 0
        else:
            ones = 0
    return _FLAG + stuffed + _FLAG


def _nrzi_encode(bits: list[int]) -> list[int]:
    out = []
    level = 1
    for b in bits:
        if b == 0:
            level ^= 1                          # 0 ⇒ transition
        out.append(level)
    return out


def modulate(frames: list[bytes], fs: int = 48000, preamble_flags: int = 32) -> np.ndarray:
    """frames → phase-continuous AFSK1200 audio (float32, ±1)."""
    bits = list(_FLAG) * preamble_flags
    for fr in frames:
        bits += _hdlc_encode_bits(fr)
    levels = _nrzi_encode(bits)
    spb = fs / BAUD
    total = int(round(len(levels) * spb))
    idx = np.clip(np.floor(np.arange(total) / spb).astype(int), 0, len(levels) - 1)
    lvl = np.asarray(levels)[idx]
    freq = np.where(lvl == 1, MARK, SPACE)
    phase = 2 * np.pi * np.cumsum(freq) / fs
    return np.cos(phase).astype(np.float32)


# ── demodulator (audio → frames) ─────────────────────────────────────────────
def _discriminator(audio: np.ndarray, fs: int) -> np.ndarray:
    """Mark-minus-space correlator: >0 ≈ mark (NRZI level 1), <0 ≈ space."""
    n = audio.size
    t = np.arange(n) / fs
    k = np.ones(int(round(fs / BAUD)))
    def mag(f):
        c = np.convolve(audio * np.cos(2 * np.pi * f * t), k, "same")
        s = np.convolve(audio * np.sin(2 * np.pi * f * t), k, "same")
        return np.hypot(c, s)
    return mag(MARK) - mag(SPACE)


def _sample_levels(level_sig: np.ndarray, fs: int) -> list[int]:
    """Mid-bit sampler with transition resync (a simple DPLL). One level per bit."""
    spb = fs / BAUD
    inc = 1.0 / spb
    levels: list[int] = []
    phase = 0.0
    sampled = False
    last = int(level_sig[0])
    for v in level_sig:
        cur = int(v)
        if cur != last:                         # transition ⇒ bit boundary, resync
            phase = 0.0
            sampled = False
        last = cur
        phase += inc
        if (not sampled) and phase >= 0.5:      # sample at bit centre
            levels.append(cur)
            sampled = True
        if phase >= 1.0:
            phase -= 1.0
            sampled = False
    return levels


def _nrzi_decode(levels: list[int]) -> list[int]:
    return [1 if levels[i] == levels[i - 1] else 0 for i in range(1, len(levels))]


def _deframe(bits: list[int]) -> list[bytes]:
    """HDLC: split on flags, de-stuff each segment, pack LSB-first, FCS-check."""
    bs = "".join("1" if b else "0" for b in bits)
    frames: list[bytes] = []
    flag = "01111110"
    pos = []
    i = bs.find(flag)
    while i != -1:
        pos.append(i)
        i = bs.find(flag, i + 8)
    for a, b in zip(pos, pos[1:]):
        seg = bs[a + 8:b]
        dbits: list[int] = []
        ones = 0
        for ch in seg:
            if ch == "1":
                dbits.append(1)
                ones += 1
            else:
                if ones == 5:                   # de-stuff: drop the inserted 0
                    ones = 0
                    continue
                dbits.append(0)
                ones = 0
        nbytes = len(dbits) // 8
        if nbytes < 3:
            continue
        data = bytes(sum(dbits[j * 8 + k] << k for k in range(8)) for j in range(nbytes))
        frame, rx = data[:-2], data[-2] | (data[-1] << 8)
        if fcs(frame) == rx:
            frames.append(frame)
    return frames


def demod(audio, fs: int = 48000) -> list[bytes]:
    """AFSK1200 audio (FM-discriminator output) → list of valid AX.25 frames."""
    x = np.asarray(audio, dtype=np.float64)
    if x.size < int(fs / BAUD) * 8:
        return []
    x = x - x.mean()                            # DC block
    level_sig = (_discriminator(x, fs) > 0).astype(np.int8)
    return _deframe(_nrzi_decode(_sample_levels(level_sig, fs)))


def fm_demod(iq, fs: int) -> np.ndarray:
    """Quadrature FM discriminator: complex IQ → real audio (instantaneous freq)."""
    z = np.asarray(iq, dtype=np.complex64)
    return np.angle(z[1:] * np.conj(z[:-1])).astype(np.float64)


def demod_iq(iq, fs: int) -> list[bytes]:
    """FM-modulated IQ at the APRS channel → AX.25 frames."""
    return demod(fm_demod(iq, fs), fs)
