# SPDX-License-Identifier: MIT OR Apache-2.0
# Copyright (c) 2026 Ares

"""
dvb_inner_fec.py — DVB-T inner forward error correction.

The stages between the demodulated soft bits and the RS(204,188) outer code
(EN 300 744 §4.3): the punctured rate-1/2 convolutional code (K=7, generators
G1=171₈, G2=133₈) decoded by a soft-decision Viterbi, then the Forney
convolutional byte de-interleaver (I=12 branches, depth M=17).

Full receive chain (this module + dvb_fec):
    soft bits → depuncture → Viterbi → bytes → conv-deinterleave
              → RS(204,188) decode → energy-dispersal derandomise → MPEG-TS

Everything is round-trip self-tested (encode → [add errors] → decode):
``python -m app.core.sdr.dvb_inner_fec``.
"""
from __future__ import annotations

from collections import deque
from typing import Optional

import numpy as np

# ── convolutional code: K=7, 64 states, generators 171₈ / 133₈ ───────────────
_K = 7
_NSTATES = 1 << (_K - 1)          # 64
_G = (0o171, 0o133)


def _parity(x: int) -> int:
    return bin(x).count("1") & 1


# Precompute the trellis: for state s (6-bit) and input u → (next_state, (out0,out1)).
_NEXT = [[0, 0] for _ in range(_NSTATES)]
_OUT = [[(0, 0), (0, 0)] for _ in range(_NSTATES)]
for s in range(_NSTATES):
    for u in (0, 1):
        reg = (u << (_K - 1)) | s
        o = tuple(_parity(reg & g) for g in _G)
        _NEXT[s][u] = reg >> 1
        _OUT[s][u] = o


def conv_encode(bits) -> list[int]:
    """Rate-1/2 mother-code encode (zero-terminated). Returns the 2N+12 output bits."""
    state = 0
    out: list[int] = []
    seq = list(bits) + [0] * (_K - 1)        # flush to terminate the trellis at state 0
    for u in seq:
        o0, o1 = _OUT[state][u]
        out.append(o0); out.append(o1)
        state = _NEXT[state][u]
    return out


# ── puncturing (EN 300 744 Table 4); patterns are [X-row, Y-row] over a period ─
_PUNCTURE = {
    "1/2": ([1], [1]),
    "2/3": ([1, 0], [1, 1]),
    "3/4": ([1, 0, 1], [1, 1, 0]),
    "5/6": ([1, 0, 1, 0, 1], [1, 1, 0, 1, 0]),
    "7/8": ([1, 0, 0, 0, 1, 0, 1], [1, 1, 1, 1, 0, 1, 0]),
}


def puncture(mother_bits, code_rate: str) -> list[int]:
    """Drop the bits the puncturing pattern marks 0. Input = rate-1/2 [X0,Y0,X1,Y1,…]."""
    px, py = _PUNCTURE[code_rate]
    per = len(px)
    out = []
    for i in range(0, len(mother_bits) - 1, 2):
        k = (i // 2) % per
        if px[k]:
            out.append(mother_bits[i])
        if py[k]:
            out.append(mother_bits[i + 1])
    return out


def depuncture(soft, code_rate: str):
    """Re-insert erasures (0.0 = neutral soft value) where puncturing removed bits,
    rebuilding the rate-1/2 mother soft stream [X0,Y0,X1,Y1,…]."""
    px, py = _PUNCTURE[code_rate]
    per = len(px)
    out = []
    idx = 0
    n = len(soft)
    k = 0
    while idx < n or (len(out) % 2):     # keep going until X,Y pairs are balanced
        kx = k % per
        out.append(soft[idx] if (px[kx] and idx < n) else 0.0); idx += px[kx] if (px[kx] and idx < n) else 0
        out.append(soft[idx] if (py[kx] and idx < n) else 0.0); idx += py[kx] if (py[kx] and idx < n) else 0
        k += 1
        if idx >= n:
            break
    return out


# ── soft-decision Viterbi (numpy-vectorised ACS) ─────────────────────────────
# Predecessors of each next-state: every state has exactly 2 incoming branches.
_PRED = np.zeros((_NSTATES, 2), dtype=np.int64)        # predecessor state
_PRED_IN = np.zeros((_NSTATES, 2), dtype=np.uint8)     # input bit on that branch
_PRED_OUTIDX = np.zeros((_NSTATES, 2), dtype=np.int64)  # 2-bit output index (o0<<1|o1)
_filled = [0] * _NSTATES
for s in range(_NSTATES):
    for u in (0, 1):
        ns = _NEXT[s][u]
        o0, o1 = _OUT[s][u]
        slot = _filled[ns]; _filled[ns] += 1
        _PRED[ns, slot] = s
        _PRED_IN[ns, slot] = u
        _PRED_OUTIDX[ns, slot] = (o0 << 1) | o1
# symbol for each 2-bit output index, mapped 0→+1, 1→−1
_SYM0 = np.array([1 - 2 * ((i >> 1) & 1) for i in range(4)], dtype=np.float64)
_SYM1 = np.array([1 - 2 * (i & 1) for i in range(4)], dtype=np.float64)


def viterbi_decode(soft_pairs, terminated: bool = True) -> list[int]:
    """Soft-decision Viterbi (K=7). Rust fast path (D4) — a scalar trellis that
    beats the per-timestep numpy ACS on small (64-state) arrays — with the numpy
    implementation (viterbi_decode_py) as the fallback + parity ground truth."""
    from app.core import native
    if native.HAS_NATIVE:
        try:
            return native.viterbi_decode(soft_pairs, terminated)
        except Exception:
            pass
    return viterbi_decode_py(soft_pairs, terminated)


def viterbi_decode_py(soft_pairs, terminated: bool = True) -> list[int]:
    """Soft-decision Viterbi over the K=7 trellis. ``soft_pairs`` is a flat sequence
    of soft values (length 2·T): +ve ⇒ bit 0, −ve ⇒ bit 1, 0 ⇒ erasure. Vectorised
    add-compare-select across the 64 states. Returns the ML information bits."""
    sp = np.asarray(soft_pairs, dtype=np.float64)
    T = sp.size // 2
    if T == 0:
        return []
    r = sp[:2 * T].reshape(T, 2)
    NEG = -1e18
    pm = np.full(_NSTATES, NEG); pm[0] = 0.0
    tb = np.zeros((T, _NSTATES), dtype=np.uint8)
    prev = np.zeros((T, _NSTATES), dtype=np.int64)
    idx = np.arange(_NSTATES)
    for t in range(T):
        r0, r1 = r[t, 0], r[t, 1]
        bm = r0 * _SYM0 + r1 * _SYM1            # (4,) branch metric per output index
        cand = pm[_PRED] + bm[_PRED_OUTIDX]     # (64,2)
        choose = np.argmax(cand, axis=1)
        pm = cand[idx, choose]
        tb[t] = _PRED_IN[idx, choose]
        prev[t] = _PRED[idx, choose]
    end = 0 if terminated else int(np.argmax(pm))
    bits = np.zeros(T, dtype=np.uint8)
    s = end
    for t in range(T - 1, -1, -1):
        bits[t] = tb[t, s]; s = int(prev[t, s])
    out = bits.tolist()
    return out[:-(_K - 1)] if terminated else out


# ── Forney convolutional byte de-interleaver (I=12, M=17) ────────────────────
_I = 12
_M = 17


class ConvDeinterleaver:
    """Inverse of the DVB-T transmit interleaver: branch j delays (I-1-j)·M bytes.
    Commutator advances one branch per byte (branch = k mod I)."""

    def __init__(self):
        self.fifos = [deque([0] * ((_I - 1 - j) * _M), maxlen=(_I - 1 - j) * _M + 1)
                      for j in range(_I)]
        self.k = 0

    def push(self, byte: int) -> int:
        j = self.k % _I
        self.k += 1
        f = self.fifos[j]
        if (_I - 1 - j) * _M == 0:
            return byte                      # branch 11: no delay
        f.append(byte)
        return f.popleft()


class ConvInterleaver:
    """Transmit interleaver (for the self-test): branch j delays j·M bytes."""

    def __init__(self):
        self.fifos = [deque([0] * (j * _M), maxlen=j * _M + 1) for j in range(_I)]
        self.k = 0

    def push(self, byte: int) -> int:
        j = self.k % _I
        self.k += 1
        if j == 0:
            return byte
        f = self.fifos[j]
        f.append(byte)
        return f.popleft()


def deinterleave(data: bytes) -> bytes:
    di = ConvDeinterleaver()
    return bytes(di.push(b) for b in data)


def interleave(data: bytes) -> bytes:
    ci = ConvInterleaver()
    return bytes(ci.push(b) for b in data)


_END_TO_END_DELAY = _I * (_I - 1) * _M     # 2244 bytes (matched Forney pair, I·(I−1)·M)


# ── full inner-chain decode ──────────────────────────────────────────────────
def decode_dvbt(soft_bits, code_rate: str = "2/3") -> tuple[Optional[bytes], dict]:
    """soft bits → depuncture → Viterbi → bytes → deinterleave → RS+derandomise → TS.
    Returns (ts_bytes | None, stats)."""
    from . import dvb_fec
    mother = depuncture(list(soft_bits), code_rate)
    bits = viterbi_decode(mother, terminated=False)
    if not bits:
        return None, {"stage": "viterbi", "ok": False}
    nbytes = len(bits) // 8
    byts = bytes(int("".join(str(b) for b in bits[i * 8:(i + 1) * 8]), 2) for i in range(nbytes))
    deint = deinterleave(byts)
    # find the 204-aligned RS sync after the interleaver's 187-byte transient
    for off in range(_END_TO_END_DELAY, min(_END_TO_END_DELAY + 408, len(deint))):
        if deint[off] in (0x47, 0xB8) and all(
                deint[off + p * 204] in (0x47, 0xB8)
                for p in range(1, 4) if off + p * 204 < len(deint)):
            usable = ((len(deint) - off) // 204) * 204
            if usable >= 204 * 4:
                ts, stats = dvb_fec.correct_ts_packets(deint[off:off + usable])
                stats["code_rate"] = code_rate
                return ts, stats
    return None, {"stage": "rs_sync", "ok": False, "code_rate": code_rate}


if __name__ == "__main__":
    from app.core.sdr import dvb_fec
    import random
    random.seed(1)
    fails = 0

    # 1) Viterbi round-trip (rate 1/2) with bit errors below capacity
    info = [random.randint(0, 1) for _ in range(2000)]
    coded = conv_encode(info)
    soft = [1.0 if b == 0 else -1.0 for b in coded]
    for _ in range(40):                       # flip ~2% of bits
        soft[random.randrange(len(soft))] *= -1
    dec = viterbi_decode(soft, terminated=True)
    ber = sum(a != b for a, b in zip(dec, info)) / len(info)
    print(f"Viterbi 1/2 (2% errors): BER={ber:.4f}")
    fails += ber > 1e-6

    # 2) punctured 2/3 round-trip, clean
    coded = conv_encode(info)
    punc = puncture(coded, "2/3")
    soft = [1.0 if b == 0 else -1.0 for b in punc]
    dep = depuncture(soft, "2/3")
    dec = viterbi_decode(dep, terminated=True)
    ber = sum(a != b for a, b in zip(dec, info)) / len(info)
    print(f"Viterbi 2/3 (clean): BER={ber:.4f}")
    fails += ber > 1e-6

    # 3) interleaver round-trip
    payload = bytes(random.randrange(256) for _ in range(204 * 20))
    rt = deinterleave(interleave(payload))
    ok_il = rt[_END_TO_END_DELAY:] == payload[:len(payload) - _END_TO_END_DELAY]
    print(f"conv interleaver round-trip ({_END_TO_END_DELAY}-byte delay): {'PASS' if ok_il else 'FAIL'}")
    fails += not ok_il

    # 4) full DVB-T chain: TS → randomise → RS → interleave → conv+puncture
    #    → soft → depuncture → Viterbi → deinterleave → RS+derandomise → TS
    pkts = 16
    ts = bytearray()
    for p in range(pkts):
        ts += bytes([0x47]) + bytes(random.randrange(256) for _ in range(187))
    rand = dvb_fec.derandomise(bytes(ts))             # energy dispersal (self-inverse)
    rs = b"".join(dvb_fec.rs_encode(rand[i:i + 188]) for i in range(0, len(rand), 188))
    il = interleave(rs)
    coded = conv_encode(list(_bytes_to_bits := [int(b) for byte in il for b in f"{byte:08b}"]))
    punc = puncture(coded, "2/3")
    soft = [1.0 if b == 0 else -1.0 for b in punc]
    ts_out, stats = decode_dvbt(soft, "2/3")
    ok_chain = ts_out is not None and ts[:188 * 4] in ts_out  # first packets recovered
    print(f"full DVB-T chain: {'PASS' if ok_chain else 'FAIL'} (stats={stats})")
    fails += not ok_chain

    print(f"\n{'ALL PASS' if fails == 0 else str(fails) + ' FAILED'}")
    raise SystemExit(0 if fails == 0 else 1)
