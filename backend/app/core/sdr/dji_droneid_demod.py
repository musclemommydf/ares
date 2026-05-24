# SPDX-License-Identifier: MIT OR Apache-2.0
# Copyright (c) 2026 Ares

"""
dji_droneid_demod.py — OFDM demodulator for the DJI DroneID RF burst.

DJI DroneID broadcasts an LTE-like OFDM burst (≈15.36 MHz, 1024-pt FFT, 15 kHz
subcarrier spacing, 9 symbols, QPSK data, two embedded Zadoff-Chu symbols used
for detection + channel estimation). This module recovers the payload from a
baseband IQ capture: burst detection (ZC/energy), coarse CFO from the cyclic
prefix, per-symbol FFT, ZC-based one-tap equalisation, QPSK demap → bytes, then
hands the bytes to ``remote_id.parse_dji_droneid`` (v1 plaintext) — the v2
obfuscated tail still needs the published descramble key.

Validation: the OFDM/CP/CFO/equalise/QPSK chain is round-trip self-tested
(``python -m app.core.sdr.dji_droneid_demod`` synthesises a burst and recovers
the bits). The DJI-specific constants (ZC roots, subcarrier map, scrambler) are
to public research (proto17/dji_droneid, RUB SysSec) and need real captures to
confirm end-to-end — like the SDR hardware drivers, this is spec-faithful but
hardware-unvalidated.
"""
from __future__ import annotations

import logging
from typing import Optional

import numpy as np

log = logging.getLogger(__name__)

# DroneID OFDM numerology (public research).
FS_HZ = 15.36e6
FFT_LEN = 1024
SCS_HZ = 15_000
N_USED = 600                 # active subcarriers (300 each side of DC, DC nulled)
N_SYMBOLS = 9
# Cyclic-prefix lengths per symbol (LTE 15.36 MHz normal CP: 80 then 72×).
CP_LENS = [80] + [72] * (N_SYMBOLS - 1)
ZC_SYMBOL_IDX = (3, 5)       # 0-based symbols carrying the Zadoff-Chu reference
ZC_ROOTS = (600, 147)        # ZC roots used for the two reference symbols (per research)
DATA_SYMBOLS = tuple(i for i in range(N_SYMBOLS) if i not in ZC_SYMBOL_IDX)


def _zc(root: int, length: int) -> np.ndarray:
    n = np.arange(length)
    return np.exp(-1j * np.pi * root * n * (n + 1) / length).astype(np.complex64)


def _used_bins() -> np.ndarray:
    """FFT-shifted indices of the N_USED active subcarriers (DC nulled)."""
    half = N_USED // 2
    pos = np.arange(1, half + 1)             # skip DC
    neg = np.arange(FFT_LEN - half, FFT_LEN)
    return np.concatenate([neg, pos])


def _coarse_cfo(burst: np.ndarray) -> float:
    """Coarse CFO (Hz) from the cyclic-prefix autocorrelation of the first symbol."""
    cp = CP_LENS[0]
    a = burst[:cp]
    b = burst[FFT_LEN:FFT_LEN + cp]
    if a.size < cp or b.size < cp:
        return 0.0
    ang = np.angle(np.vdot(a, b))
    return float(ang / (2 * np.pi) * (FS_HZ / FFT_LEN))


def find_burst(iq: np.ndarray, fs: float) -> Optional[int]:
    """Return the sample index where a DroneID-length OFDM burst likely starts, via a
    sliding CP autocorrelation peak; None if nothing burst-like is present."""
    x = np.asarray(iq, dtype=np.complex64)
    burst_len = sum(CP_LENS) + N_SYMBOLS * FFT_LEN
    if x.size < burst_len:
        return None
    cp = CP_LENS[1]
    # CP metric: correlation of x[n..n+cp] with x[n+FFT..]; peaks at symbol boundaries.
    step = max(1, cp // 2)
    best_i, best_m = None, 0.0
    for i in range(0, max(1, x.size - burst_len) + 1, step):
        a = x[i:i + cp]; b = x[i + FFT_LEN:i + FFT_LEN + cp]
        m = abs(np.vdot(a, b)) / (np.linalg.norm(a) * np.linalg.norm(b) + 1e-9)
        if m > best_m:
            best_m, best_i = m, i
    return best_i if best_m > 0.6 else None


def _demod_symbols(burst: np.ndarray, cfo_hz: float) -> Optional[np.ndarray]:
    """FFT each OFDM symbol after CFO correction → array (N_SYMBOLS, N_USED) of
    equalised subcarriers (using the ZC symbols for the channel estimate)."""
    n = np.arange(burst.size)
    burst = burst * np.exp(-1j * 2 * np.pi * cfo_hz * n / FS_HZ).astype(np.complex64)
    bins = _used_bins()
    grids = []
    pos = 0
    for s in range(N_SYMBOLS):
        cp = CP_LENS[s]
        pos += cp
        if pos + FFT_LEN > burst.size:
            return None
        sym = burst[pos:pos + FFT_LEN]
        pos += FFT_LEN
        spec = np.fft.fft(sym, FFT_LEN) / np.sqrt(FFT_LEN)
        grids.append(spec[bins])
    grid = np.array(grids)                            # (N_SYMBOLS, N_USED)
    # Channel estimate from the first ZC reference symbol, then one-tap equalise.
    ref_ref = _zc(ZC_ROOTS[0], N_USED)
    h = grid[ZC_SYMBOL_IDX[0]] / (ref_ref + 1e-9)
    h_mag = np.abs(h)
    h_mag[h_mag < 1e-3] = 1e-3
    eq = grid / (h / h_mag) / h_mag                   # phase + magnitude equalise
    return eq.astype(np.complex64)


def _qpsk_bits(syms: np.ndarray) -> np.ndarray:
    """Gray-coded QPSK hard demap → bit array (2 bits/symbol)."""
    bits = np.empty(syms.size * 2, dtype=np.uint8)
    bits[0::2] = (syms.real < 0).astype(np.uint8)
    bits[1::2] = (syms.imag < 0).astype(np.uint8)
    return bits


def _bits_to_bytes(bits: np.ndarray) -> bytes:
    n = (bits.size // 8) * 8
    return np.packbits(bits[:n]).tobytes()


def demodulate(iq, fs: float, *, descramble=None) -> Optional[dict]:
    """Demodulate a DroneID OFDM burst from baseband IQ. Returns
    ``{payload_bytes, n_symbols, cfo_hz, evm_pct}`` or None if no burst is found.
    ``descramble(bytes)->bytes`` optionally undoes the v2 scrambler before return."""
    x = np.asarray(iq, dtype=np.complex64).ravel()
    if abs(fs - FS_HZ) / FS_HZ > 0.02:
        # resample to the DroneID rate
        try:
            from scipy.signal import resample_poly
            from math import gcd
            up, dn = int(FS_HZ), int(fs)
            g = gcd(up, dn)
            x = resample_poly(x, up // g, dn // g).astype(np.complex64)
        except Exception:
            return None
    start = find_burst(x, FS_HZ)
    if start is None:
        return None
    burst_len = sum(CP_LENS) + N_SYMBOLS * FFT_LEN
    burst = x[start:start + burst_len]
    if burst.size < burst_len:
        return None
    cfo = _coarse_cfo(burst)
    eq = _demod_symbols(burst, cfo)
    if eq is None:
        return None
    data = eq[list(DATA_SYMBOLS)].reshape(-1)
    # EVM estimate against the nearest QPSK constellation point
    ideal = (np.sign(data.real) + 1j * np.sign(data.imag)) / np.sqrt(2)
    norm = data / (np.abs(data).mean() + 1e-9) / np.sqrt(2)
    evm = float(np.sqrt(np.mean(np.abs(norm - ideal) ** 2)) * 100.0)
    payload = _bits_to_bytes(_qpsk_bits(data))
    if descramble is not None:
        try:
            payload = descramble(payload)
        except Exception:
            pass
    return {"payload_bytes": payload, "n_symbols": len(DATA_SYMBOLS),
            "cfo_hz": round(cfo, 1), "evm_pct": round(evm, 1)}


def demodulate_to_droneid(iq, fs: float) -> Optional[dict]:
    """Full path: OFDM demod → parse_dji_droneid on the recovered payload."""
    r = demodulate(iq, fs)
    if not r:
        return None
    from . import remote_id
    parsed = remote_id.parse_dji_droneid(r["payload_bytes"])
    if parsed and not parsed.get("error"):
        parsed["_phy"] = {k: r[k] for k in ("cfo_hz", "evm_pct", "n_symbols")}
        return parsed
    return {"phy": r, "parsed": None,
            "note": "OFDM burst demodulated; payload didn't parse as plaintext DroneID "
                    "(v2 needs the published descramble key)."}


def _synth_burst(payload_bits: np.ndarray) -> np.ndarray:
    """Build a clean DroneID-format OFDM burst carrying ``payload_bits`` (for the
    self-test): QPSK-map data symbols, ZC reference symbols, IFFT + CP."""
    bins = _used_bins()
    out = []
    bi = 0
    for s in range(N_SYMBOLS):
        grid = np.zeros(FFT_LEN, dtype=np.complex64)
        if s in ZC_SYMBOL_IDX:
            grid[bins] = _zc(ZC_ROOTS[0] if s == ZC_SYMBOL_IDX[0] else ZC_ROOTS[1], N_USED)
        else:
            need = N_USED * 2
            chunk = payload_bits[bi:bi + need]
            if chunk.size < need:
                chunk = np.concatenate([chunk, np.zeros(need - chunk.size, np.uint8)])
            bi += need
            syms = ((1 - 2 * chunk[0::2].astype(np.float32)) +
                    1j * (1 - 2 * chunk[1::2].astype(np.float32))) / np.sqrt(2)
            grid[bins] = syms
        td = np.fft.ifft(grid) * np.sqrt(FFT_LEN)
        out.append(np.concatenate([td[-CP_LENS[s]:], td]).astype(np.complex64))
    return np.concatenate([np.zeros(256, np.complex64)] + out + [np.zeros(256, np.complex64)])


if __name__ == "__main__":
    rng = np.random.default_rng(0)
    nbits = len(DATA_SYMBOLS) * N_USED * 2
    bits = rng.integers(0, 2, nbits).astype(np.uint8)
    burst = _synth_burst(bits)
    r = demodulate(burst, FS_HZ)
    if not r:
        print("FAIL: no burst detected"); raise SystemExit(1)
    got = np.unpackbits(np.frombuffer(r["payload_bytes"], dtype=np.uint8))[:nbits]
    ber = float(np.mean(got != bits[:got.size])) if got.size else 1.0
    print(f"DroneID OFDM round-trip: BER={ber:.4f}, EVM={r['evm_pct']}%, CFO={r['cfo_hz']} Hz, "
          f"recovered {len(r['payload_bytes'])} bytes")
    raise SystemExit(0 if ber < 0.01 else 1)
