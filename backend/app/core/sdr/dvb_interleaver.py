# SPDX-License-Identifier: MIT OR Apache-2.0
# Copyright (c) 2026 Ares

"""
dvb_interleaver.py — DVB-T inner bit + symbol interleaver (EN 300 744 §4.3.4).

The layer between the QAM mapper and the convolutional code. On receive it must be
undone, in order, before the Viterbi decoder — otherwise the soft bits are scrambled
and the inner decoder can't lock. Implemented exactly to the standard:

  * **Bit-wise interleaver** (§4.3.4.1): demux the coded stream into v sub-streams
    (v = 2 QPSK / 4 16-QAM / 6 64-QAM), each block-interleaved over 126 bits with
    He(w) = (w + {0,63,105,42,21,84}[e]) mod 126, then re-grouped into v-bit cells
    (I0 = MSB).
  * **Symbol interleaver** (§4.3.4.2): permute the 1512 (2K) / 6048 (8K) cells of an
    OFDM symbol by H(q), generated from the R'i register (right-shift + feedback
    2K: bits 0,3 / 8K: bits 0,1,4,6) and the bit-permutation tables 3a/3b, with the
    even/odd-symbol alternation.

Soft-value friendly: every step is a reordering, so it applies to LLRs identically.
Round-trip self-tested: ``python -m app.core.sdr.dvb_interleaver``.
"""
from __future__ import annotations

import numpy as np

# Bit-interleaver permutation offsets He(w) = (w + offset_e) mod 126.
_BIT_OFFSETS = {0: 0, 1: 63, 2: 105, 3: 42, 4: 21, 5: 84}
_BIT_BLOCK = 126

# Symbol-interleaver per-mode parameters.
_MODE = {
    "2k": {"Mmax": 2048, "Nmax": 1512, "Nr": 11,
           # table 3a: R'i bit position -> Ri bit position
           "perm": {9: 0, 8: 7, 7: 5, 6: 1, 5: 8, 4: 2, 3: 6, 2: 9, 1: 3, 0: 4},
           "taps": (0, 3)},
    "8k": {"Mmax": 8192, "Nmax": 6048, "Nr": 13,
           # table 3b
           "perm": {11: 5, 10: 11, 9: 3, 8: 0, 7: 10, 6: 8, 5: 6, 4: 9, 3: 2, 2: 4, 1: 1, 0: 7},
           "taps": (0, 1, 4, 6)},
}


def v_for_modulation(mod: str) -> int:
    return {"qpsk": 2, "16qam": 4, "64qam": 6}.get(mod.lower(), 2)


# ── bit-wise interleaver ─────────────────────────────────────────────────────
def _demux_indices(v: int):
    """e(di), do(di) for the non-hierarchical demultiplexer (EN 300 744 §4.3.4.1)."""
    n = _BIT_BLOCK * v
    half = v // 2
    di = np.arange(n)
    e = ((di % v) // half) + 2 * (di % half)
    do = di // v
    return e, do


def bit_interleave(x):
    """TX bit interleaver on one 126·v block of bits/soft values. Returns the 126
    v-bit cells as an array shaped (126, v) (column 0 = I0 = MSB)."""
    x = np.asarray(x)
    n = x.size
    v = n // _BIT_BLOCK
    e, do = _demux_indices(v)
    b = np.empty((v, _BIT_BLOCK), dtype=x.dtype)
    b[e, do] = x                                   # demux: b[e][do] = x[di]
    a = np.empty_like(b)
    for ee in range(v):
        off = _BIT_OFFSETS[ee]
        a[ee] = b[ee][(np.arange(_BIT_BLOCK) + off) % _BIT_BLOCK]   # a[e][w]=b[e][He(w)]
    return a.T                                     # cells[w] = (a0,w … a(v-1),w)


def bit_deinterleave(cells):
    """RX bit de-interleaver: inverse of :func:`bit_interleave`. ``cells`` is (126, v)
    of soft values (column 0 = I0/MSB). Returns the 126·v de-interleaved soft stream."""
    cells = np.asarray(cells)
    v = cells.shape[1]
    a = cells.T                                    # a[e][w]
    b = np.empty_like(a)
    for ee in range(v):
        off = _BIT_OFFSETS[ee]
        b[ee] = a[ee][(np.arange(_BIT_BLOCK) - off) % _BIT_BLOCK]   # b[e][k]=a[e][He^{-1}(k)]
    e, do = _demux_indices(v)
    return b[e, do]                                # remux: x[di]=b[e(di)][do(di)]


# ── symbol interleaver ───────────────────────────────────────────────────────
_H_CACHE: dict[str, np.ndarray] = {}


def build_H(mode: str) -> np.ndarray:
    """The H(q) permutation of 0..Nmax-1 (EN 300 744 §4.3.4.2)."""
    mode = mode.lower()
    if mode in _H_CACHE:
        return _H_CACHE[mode]
    p = _MODE[mode]
    Mmax, Nmax, Nr, perm, taps = p["Mmax"], p["Nmax"], p["Nr"], p["perm"], p["taps"]
    top = Nr - 2                                   # MSB position of R'i (0-indexed)
    H = np.empty(Nmax, dtype=np.int64)
    q = 0
    reg = 0
    for i in range(Mmax):
        if i <= 1:
            reg = 0
        elif i == 2:
            reg = 1
        else:
            fb = 0
            for t in taps:
                fb ^= (reg >> t) & 1
            reg = (reg >> 1) | (fb << top)         # R'i[k]=R'i-1[k+1]; R'i[top]=feedback
        Ri = 0
        for src, dst in perm.items():
            if (reg >> src) & 1:
                Ri |= (1 << dst)
        Hq = (i & 1) * (1 << (Nr - 1)) + Ri
        if Hq < Nmax:
            H[q] = Hq
            q += 1
    if q != Nmax:                                  # sanity: H must be a full permutation
        raise RuntimeError(f"DVB-T {mode} H(q) produced {q} entries, expected {Nmax}")
    _H_CACHE[mode] = H
    return H


def symbol_deinterleave(Y, sym_index: int, mode: str):
    """Recover Y' (mapper order) from received cells Y (carrier order) for the OFDM
    symbol at ``sym_index`` (even/odd alternation per the standard)."""
    H = build_H(mode)
    Y = np.asarray(Y)
    if sym_index % 2 == 0:
        return Y[H]                                # even: Y'[q] = Y[H(q)]
    Hinv = np.argsort(H)
    return Y[Hinv]                                 # odd:  Y'[q] = Y[H^{-1}(q)]


def symbol_interleave(Yp, sym_index: int, mode: str):
    """TX symbol interleaver (inverse of :func:`symbol_deinterleave`), for the self-test."""
    H = build_H(mode)
    Yp = np.asarray(Yp)
    out = np.empty_like(Yp)
    if sym_index % 2 == 0:
        out[H] = Yp                                # even: Y[H(q)] = Y'[q]
    else:
        out[:] = Yp[H]                             # odd:  Y[q] = Y'[H(q)]
    return out


# ── full inner de-interleave (receive): cells → soft coded-bit stream ────────
def inner_deinterleave(cells_soft, sym_index: int, v: int, mode: str):
    """One OFDM symbol of soft cells → de-interleaved soft coded bits.
    ``cells_soft`` is (Nmax, v): Nmax data cells in carrier order, v soft bits each
    (column 0 = I0/MSB). Applies symbol de-interleave then per-126-group bit
    de-interleave, returning Nmax·v soft values ready for depuncture+Viterbi."""
    cells_soft = np.asarray(cells_soft)
    Nmax = _MODE[mode.lower()]["Nmax"]
    # symbol de-interleave reorders the cells (apply the permutation to row indices)
    H = build_H(mode)
    order = H if sym_index % 2 == 0 else np.argsort(H)
    cells = cells_soft[order]                      # now in mapper (Y') order
    out = np.empty(Nmax * v, dtype=cells.dtype)
    for g in range(Nmax // _BIT_BLOCK):
        block = cells[g * _BIT_BLOCK:(g + 1) * _BIT_BLOCK]   # (126, v)
        out[g * _BIT_BLOCK * v:(g + 1) * _BIT_BLOCK * v] = bit_deinterleave(block)
    return out


def inner_interleave(soft_bits, sym_index: int, v: int, mode: str):
    """TX inverse of :func:`inner_deinterleave` (for the round-trip test): soft coded
    bits → (Nmax, v) cells in carrier order."""
    soft_bits = np.asarray(soft_bits)
    Nmax = _MODE[mode.lower()]["Nmax"]
    cells = np.empty((Nmax, v), dtype=soft_bits.dtype)
    for g in range(Nmax // _BIT_BLOCK):
        blk = soft_bits[g * _BIT_BLOCK * v:(g + 1) * _BIT_BLOCK * v]
        cells[g * _BIT_BLOCK:(g + 1) * _BIT_BLOCK] = bit_interleave(blk)
    return symbol_interleave(cells, sym_index, mode)


def decode_dvbt_full(cells_by_symbol, *, code_rate: str = "2/3",
                     modulation: str = "qpsk", mode: str = "8k"):
    """Complete DVB-T inner+outer receive chain from per-OFDM-symbol soft data cells:
    symbol+bit de-interleave (this module) → depuncture → Viterbi → outer conv
    de-interleave → RS(204,188) → derandomise → MPEG-TS (dvb_inner_fec / dvb_fec).

    ``cells_by_symbol`` is a sequence of (Nmax, v) soft-cell arrays, one per OFDM
    symbol, in transmission order (symbol index 0,1,2,… sets the even/odd parity)."""
    from . import dvb_inner_fec
    v = v_for_modulation(modulation)
    soft = []
    for sym_index, cells in enumerate(cells_by_symbol):
        soft.append(inner_deinterleave(cells, sym_index, v, mode))
    soft_bits = np.concatenate(soft) if soft else np.zeros(0)
    return dvb_inner_fec.decode_dvbt(soft_bits.tolist(), code_rate)


if __name__ == "__main__":
    rng = np.random.default_rng(0)
    fails = 0

    # 1) bit interleaver round-trip for each modulation
    for mod, v in (("qpsk", 2), ("16qam", 4), ("64qam", 6)):
        x = rng.integers(0, 2, _BIT_BLOCK * v)
        rt = bit_deinterleave(bit_interleave(x))
        ok = np.array_equal(rt, x)
        print(f"bit interleaver {mod} (v={v}) round-trip: {'PASS' if ok else 'FAIL'}")
        fails += not ok

    # 2) symbol interleaver: H is a valid permutation + round-trip, both parities
    for mode in ("2k", "8k"):
        H = build_H(mode)
        Nmax = _MODE[mode]["Nmax"]
        perm_ok = (len(np.unique(H)) == Nmax and H.min() == 0 and H.max() == Nmax - 1)
        Y = rng.standard_normal(Nmax)
        rt_even = symbol_deinterleave(symbol_interleave(Y, 0, mode), 0, mode)
        rt_odd = symbol_deinterleave(symbol_interleave(Y, 1, mode), 1, mode)
        ok = perm_ok and np.allclose(rt_even, Y) and np.allclose(rt_odd, Y)
        print(f"symbol interleaver {mode}: permutation valid={perm_ok}, "
              f"round-trip even/odd: {'PASS' if ok else 'FAIL'}")
        fails += not ok

    # 3) full inner interleave→deinterleave round-trip on soft values
    for mode, mod, v in (("2k", "qpsk", 2), ("2k", "16qam", 4), ("8k", "64qam", 6)):
        Nmax = _MODE[mode]["Nmax"]
        soft = rng.standard_normal(Nmax * v)
        for sym in (0, 1):
            rt = inner_deinterleave(inner_interleave(soft, sym, v, mode), sym, v, mode)
            ok = np.allclose(rt, soft)
            print(f"full inner chain {mode}/{mod} sym{'even' if sym==0 else 'odd'}: "
                  f"{'PASS' if ok else 'FAIL'}")
            fails += not ok

    # 4) END-TO-END DVB-T: TS → randomise → RS → outer-interleave → conv+puncture
    #    → bit+symbol interleave → [perfect soft channel] → full de-interleave +
    #    inner/outer FEC → TS. Proves the interleaver model composes with the chain.
    from app.core.sdr import dvb_fec, dvb_inner_fec
    mode, mod, v, rate = "2k", "qpsk", 2, "2/3"
    Nmax = _MODE[mode]["Nmax"]
    pkts = 40
    ts = bytearray()
    for _ in range(pkts):
        ts += bytes([0x47]) + bytes(rng.integers(0, 256, 187).tolist())
    rand = dvb_fec.derandomise(bytes(ts))
    rs = b"".join(dvb_fec.rs_encode(rand[i:i + 188]) for i in range(0, len(rand), 188))
    il = dvb_inner_fec.interleave(rs)
    coded = dvb_inner_fec.conv_encode([int(b) for byte in il for b in f"{byte:08b}"])
    punc = dvb_inner_fec.puncture(coded, rate)
    soft = [1.0 if b == 0 else -1.0 for b in punc]                 # perfect channel → ±1
    cell = Nmax * v
    cells_by_symbol = []
    for s in range(0, len(soft), cell):
        block = soft[s:s + cell]
        if len(block) < cell:
            block = block + [0.0] * (cell - len(block))            # pad final symbol
        cells_by_symbol.append(inner_interleave(np.array(block), len(cells_by_symbol), v, mode))
    ts_out, stats = decode_dvbt_full(cells_by_symbol, code_rate=rate, modulation=mod, mode=mode)
    ok_e2e = ts_out is not None and ts[:188 * 4] in ts_out
    print(f"END-TO-END DVB-T (interleavers + inner+outer FEC): {'PASS' if ok_e2e else 'FAIL'} "
          f"(stats={stats})")
    fails += not ok_e2e

    print(f"\n{'ALL PASS' if fails == 0 else str(fails)+' FAILED'}")
    raise SystemExit(0 if fails == 0 else 1)
