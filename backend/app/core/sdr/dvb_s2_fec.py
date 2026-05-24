# SPDX-License-Identifier: MIT OR Apache-2.0
# Copyright (c) 2026 Ares

"""
dvb_s2_fec.py — DVB-S2 inner LDPC + outer BCH FEC (EN 302 307-1 §5.3).

The FEC that DVB-T's chain doesn't cover. DVB-S2 concatenates an outer t-error BCH
with an inner IRA-LDPC, for two FECFRAME sizes — normal (nldpc = 64800) and short
(nldpc = 16200):

  * **BCH** — systematic binary BCH; g(x) = product of the first t generator
    polynomials (Table 6a normal / 6b short). Encode = polynomial remainder; decode =
    GF(2^16) (normal) / GF(2^14) (short) syndromes → Berlekamp-Massey → Chien.
  * **LDPC** — irregular repeat-accumulate. Encode accumulates each info bit at the
    parity addresses from the Annex B (normal) / C (short) table — offset by m·q per
    §5.3.2 — then a dual-diagonal accumulate. Decode builds H = [A | B(dual-diagonal)]
    from the same table and runs a normalised min-sum belief-propagation decoder.

Every DVB-S2 and DVB-S2X normal/short code rate is tabulated (35 normal + 16 short; see
dvb_s2_ldpc_tables.py, the Annex B/C accumulator addresses cross-checked against the ETSI
PDF + gr-dtv). S2X rates of equal value but different tables (e.g. 26/45 vs 104/180) are
kept as distinct keys. The BCH reuses the same GF(2^16)/GF(2^14) polynomials across S2 and
S2X — only the per-rate (kbch, t) differ. The engine is frame- and rate-agnostic.

Self-test (``python -m app.core.sdr.dvb_s2_fec``) round-trips message → BCH → LDPC →
BPSK/AWGN → min-sum LDPC → BCH → message for both frame sizes.
"""
from __future__ import annotations

from functools import lru_cache
from typing import Optional

import numpy as np

from . import dvb_s2_ldpc_tables as _T

NLDPC = _T.NLDPC                              # {'normal': 64800, 'short': 16200}

# ── per-(frame, rate) coding parameters (Table 5a normal / 5b short) ──────────
# kbch (BCH uncoded block) and the BCH t-error correction. Nbch = kldpc; the BCH
# parity = kldpc - kbch = t·m bits (m = 16 normal, 14 short).
_KBCH = {
    # normal FECFRAME (Table 5a + EN 302 307-2). Keyed by literal rate name; S2X rates
    # of equal value (e.g. 26/45 vs 104/180) are distinct codes.
    "normal": {
        "1/4": 16008, "1/3": 21408, "2/5": 25728, "1/2": 32208, "3/5": 38688,
        "2/3": 43040, "3/4": 48408, "4/5": 51648, "5/6": 53840, "8/9": 57472,
        "9/10": 58192, "2/9": 14208, "13/45": 18528, "9/20": 28968, "11/20": 35448,
        "13/18": 46608, "7/9": 50208, "18/30": 38688, "20/30": 43008, "22/30": 47328,
        "23/36": 41208, "25/36": 44808, "26/45": 37248, "28/45": 40128,
        "90/180": 32208, "96/180": 34368, "100/180": 35808, "104/180": 37248,
        "116/180": 41568, "124/180": 44448, "128/180": 45888, "132/180": 47328,
        "135/180": 48408, "140/180": 50208, "154/180": 55248},
    "short": {
        "1/4": 3072, "1/3": 5232, "2/5": 6312, "1/2": 7032, "3/5": 9552,
        "2/3": 10632, "3/4": 11712, "4/5": 12432, "5/6": 13152, "8/9": 14232,
        "4/15": 4152, "8/15": 8472, "11/45": 3792, "14/45": 4872, "26/45": 9192,
        "32/45": 11352},
}
# BCH t: short is always 12; normal is 12 except a few rates (Table 5a).
_BCH_T = {
    "normal": {"2/3": 10, "5/6": 10, "8/9": 8, "9/10": 8},   # else 12
    "short":  {},                                            # all 12
}


def _bch_t(frame: str, rate: str) -> int:
    return _BCH_T[frame].get(rate, 12)


# ── BCH generator polynomials, as bit masks (LSB = x^0) ──────────────────────
# Table 6a — normal FECFRAME (degree 16, GF(2^16)).
_BCH_NORMAL_POLYS = [
    [0, 2, 3, 5, 16], [0, 1, 4, 5, 6, 8, 16], [0, 2, 3, 4, 5, 7, 8, 9, 10, 11, 16],
    [0, 2, 4, 6, 9, 11, 12, 14, 16], [0, 1, 2, 3, 5, 8, 9, 10, 11, 12, 16],
    [0, 2, 4, 5, 7, 8, 9, 10, 12, 13, 14, 15, 16], [0, 2, 5, 6, 8, 9, 10, 11, 13, 15, 16],
    [0, 1, 2, 5, 6, 8, 9, 12, 13, 14, 16], [0, 5, 7, 9, 10, 11, 16],
    [0, 1, 2, 5, 7, 8, 10, 12, 13, 14, 16], [0, 2, 3, 5, 9, 11, 12, 13, 16],
    [0, 1, 5, 6, 7, 9, 11, 12, 16],
]
# Table 6b — short FECFRAME (degree 14, GF(2^14)).
_BCH_SHORT_POLYS = [
    [0, 1, 3, 5, 14], [0, 6, 8, 11, 14], [0, 1, 2, 6, 9, 10, 14],
    [0, 4, 7, 8, 10, 12, 14], [0, 2, 4, 6, 8, 9, 11, 13, 14], [0, 3, 7, 8, 9, 13, 14],
    [0, 2, 5, 6, 7, 10, 11, 13, 14], [0, 5, 8, 9, 10, 11, 14], [0, 1, 2, 3, 9, 10, 14],
    [0, 3, 6, 9, 11, 12, 14], [0, 4, 11, 12, 14], [0, 1, 2, 3, 5, 6, 7, 8, 10, 13, 14],
]
# field: m and primitive polynomial (= g1, the minimal poly of the primitive element).
_FIELD = {
    "normal": (16, (1 << 16) | (1 << 5) | (1 << 3) | (1 << 2) | 1, _BCH_NORMAL_POLYS),
    "short":  (14, (1 << 14) | (1 << 5) | (1 << 3) | (1 << 1) | 1, _BCH_SHORT_POLYS),
}


# ── GF(2) polynomial helpers ─────────────────────────────────────────────────
def _poly_from_terms(terms) -> int:
    v = 0
    for t in terms:
        v ^= (1 << t)
    return v


def _gf2_polymul(a: int, b: int) -> int:
    r = 0
    while b:
        if b & 1:
            r ^= a
        b >>= 1
        a <<= 1
    return r


def _deg(p: int) -> int:
    return p.bit_length() - 1


# ── GF(2^m) arithmetic context (one per frame, cached) ───────────────────────
class _GF:
    def __init__(self, m: int, prim: int):
        self.m = m
        self.N2 = (1 << m) - 1
        exp = np.zeros(2 * self.N2, dtype=np.int64)
        log = np.zeros(1 << m, dtype=np.int64)
        x = 1
        for i in range(self.N2):
            exp[i] = x
            log[x] = i
            x <<= 1
            if x & (1 << m):
                x ^= prim
        exp[self.N2:] = exp[:self.N2]
        self.EXP = exp
        self.LOG = log

    def mul(self, a: int, b: int) -> int:
        return 0 if (a == 0 or b == 0) else int(self.EXP[self.LOG[a] + self.LOG[b]])

    def inv(self, a: int) -> int:
        return int(self.EXP[self.N2 - self.LOG[a]])


@lru_cache(maxsize=None)
def _gf(frame: str) -> _GF:
    m, prim, _ = _FIELD[frame]
    return _GF(m, prim)


@lru_cache(maxsize=None)
def _bch_generator(frame: str, t: int) -> int:
    polys = _FIELD[frame][2]
    g = 1
    for i in range(t):
        g = _gf2_polymul(g, _poly_from_terms(polys[i]))
    return g                                  # degree m·t


# ── BCH encode / decode ──────────────────────────────────────────────────────
def bch_encode(msg_bits: np.ndarray, kbch: int, t: int, frame: str = "normal") -> np.ndarray:
    """Systematic binary BCH: append the degree-(m·t) remainder of x^(n-k)·m(x)/g(x)."""
    g = _bch_generator(frame, t)
    nparity = _deg(g)
    reg = 0
    msg = np.asarray(msg_bits, dtype=np.uint8)
    for bit in np.concatenate([msg, np.zeros(nparity, dtype=np.uint8)]):
        reg = (reg << 1) | int(bit)
        if reg >> nparity:
            reg ^= g
    parity = reg & ((1 << nparity) - 1)
    pbits = np.array([(parity >> (nparity - 1 - i)) & 1 for i in range(nparity)], dtype=np.uint8)
    return np.concatenate([msg, pbits])


def bch_decode(code_bits: np.ndarray, kbch: int, t: int,
               frame: str = "normal") -> tuple[np.ndarray, int]:
    """Correct up to t errors. Returns (message_bits, n_corrected | -1 if uncorrectable)."""
    gf = _gf(frame)
    EXP, N2 = gf.EXP, gf.N2
    n = len(code_bits)
    c = np.asarray(code_bits, dtype=np.uint8).copy()

    def syndromes():
        idx = np.nonzero(c)[0]
        if idx.size == 0:
            return [0] * (2 * t)
        pows = (n - 1 - idx).astype(np.int64) % N2          # locator exponents
        out = []
        for j in range(1, 2 * t + 1):
            e = (j * pows) % N2
            out.append(int(np.bitwise_xor.reduce(EXP[e])))
        return out

    synd = syndromes()
    if not any(synd):
        return c[:kbch], 0
    # Berlekamp-Massey (binary BCH)
    L, m_ = 0, 1
    Lam = [1] + [0] * (2 * t)
    B = [1] + [0] * (2 * t)
    b = 1
    for r in range(2 * t):
        delta = synd[r]
        for i in range(1, L + 1):
            delta ^= gf.mul(Lam[i], synd[r - i])
        if delta == 0:
            m_ += 1
        elif 2 * L <= r:
            T = Lam[:]
            coef = gf.mul(delta, gf.inv(b))
            for i in range(2 * t + 1 - m_):
                Lam[i + m_] ^= gf.mul(coef, B[i])
            L = r + 1 - L; B = T; b = delta; m_ = 1
        else:
            coef = gf.mul(delta, gf.inv(b))
            for i in range(2 * t + 1 - m_):
                Lam[i + m_] ^= gf.mul(coef, B[i])
            m_ += 1
    # Chien search (vectorised over all n positions): error at bit p has locator
    # X_p = α^(n-1-p); an error ⇔ Λ(X_p^-1) = 0. e = exponent of α^-(n-1-p).
    e = (N2 - ((n - 1 - np.arange(n, dtype=np.int64)) % N2)) % N2
    val = np.zeros(n, dtype=np.int64)
    for d in range(L + 1):
        if Lam[d]:
            val ^= EXP[(gf.LOG[Lam[d]] + (e * d) % N2)]
    errs = np.nonzero(val == 0)[0]
    if errs.size != L:
        return c[:kbch], -1
    c[errs] ^= 1
    if any(syndromes()):
        return c[:kbch], -1
    return c[:kbch], int(errs.size)


# ── LDPC: params, H construction, encode, min-sum decode ─────────────────────
def available_rates(frame: str = "normal") -> list[str]:
    """Code rates with tabulated LDPC accumulator addresses for this FECFRAME size."""
    table = _T.LDPC_NORMAL if frame == "normal" else _T.LDPC_SHORT
    return sorted(table)


def _ldpc_params(rate: str, frame: str):
    """Return (kldpc, q, table) for the (frame, rate)."""
    table = _T.LDPC_NORMAL if frame == "normal" else _T.LDPC_SHORT
    if rate not in table:
        raise ValueError(f"DVB-S2 {frame} rate {rate} not tabulated (have: {sorted(table)})")
    kldpc, q, rows = table[rate]
    return kldpc, q, rows


def ldpc_encode(info_bits: np.ndarray, rate: str = "2/3", frame: str = "normal") -> np.ndarray:
    """IRA-LDPC systematic encode: accumulate, then dual-diagonal (§5.3.2)."""
    kldpc, q, table = _ldpc_params(rate, frame)
    n = NLDPC[frame]
    m = n - kldpc
    info = np.asarray(info_bits, dtype=np.uint8)
    p = np.zeros(m, dtype=np.uint8)
    for t in range(kldpc):
        if info[t]:
            off = t % 360
            for a in table[t // 360]:
                p[(a + off * q) % m] ^= 1
    for i in range(1, m):                        # dual-diagonal accumulate
        p[i] ^= p[i - 1]
    return np.concatenate([info, p])


@lru_cache(maxsize=None)
def _build_H(rate: str, frame: str):
    """Variable/check adjacency of H = [A | B] for the IRA code (B = dual diagonal).

    Returns (edges_c, edges_v, segs, kldpc, m) precomputed for the min-sum decoder."""
    kldpc, q, table = _ldpc_params(rate, frame)
    m = NLDPC[frame] - kldpc
    chk_to_var = [[] for _ in range(m)]
    for t in range(kldpc):
        off = t % 360
        for a in table[t // 360]:
            chk_to_var[(a + off * q) % m].append(t)
    for j in range(m):                           # dual-diagonal parity part
        chk_to_var[j].append(kldpc + j)
        if j >= 1:
            chk_to_var[j].append(kldpc + j - 1)
    edges_c, edges_v = [], []
    for j, vs in enumerate(chk_to_var):
        for v in vs:
            edges_c.append(j); edges_v.append(v)
    edges_c = np.asarray(edges_c); edges_v = np.asarray(edges_v)
    order = np.argsort(edges_c, kind="stable")
    ec = edges_c[order]
    seg_start = [0] + list(np.nonzero(np.diff(ec))[0] + 1) + [len(ec)]
    segs = [(seg_start[i], seg_start[i + 1]) for i in range(len(seg_start) - 1)]
    return edges_v, order, segs, kldpc, m


def _check_adjacency(rate: str, frame: str):
    """check→variable lists (used by the self-test to verify H·c = 0)."""
    kldpc, q, table = _ldpc_params(rate, frame)
    m = NLDPC[frame] - kldpc
    chk_to_var = [[] for _ in range(m)]
    for t in range(kldpc):
        off = t % 360
        for a in table[t // 360]:
            chk_to_var[(a + off * q) % m].append(t)
    for j in range(m):
        chk_to_var[j].append(kldpc + j)
        if j >= 1:
            chk_to_var[j].append(kldpc + j - 1)
    return chk_to_var


def ldpc_decode(llr: np.ndarray, rate: str = "2/3", frame: str = "normal",
                max_iter: int = 50) -> np.ndarray:
    """Normalised min-sum BP decode. ``llr`` is the per-bit channel LLR (sign = bit,
    +ve ⇒ 0). Returns the hard-decided codeword bits."""
    edges_v, order, segs, kldpc, m = _build_H(rate, frame)
    llr = np.asarray(llr, dtype=np.float64)
    msg_vc = llr[edges_v].copy()
    alpha = 0.75                                 # min-sum normalisation
    msg_cv = np.zeros_like(msg_vc)
    hard = (llr < 0).astype(np.uint8)
    for _ in range(max_iter):
        vals = msg_vc[order]
        gsign = np.where(vals >= 0, 1.0, -1.0)
        agv = np.abs(vals)
        cv_sorted = np.empty_like(vals)
        for a, b in segs:
            gs = gsign[a:b]; ga = agv[a:b]
            sgn_all = np.prod(gs)
            si = np.argsort(ga)
            min1 = ga[si[0]]
            min2 = ga[si[1]] if si.size > 1 else min1
            mag = np.where(np.arange(b - a) == si[0], min2, min1)
            cv_sorted[a:b] = alpha * (sgn_all * gs) * mag
        msg_cv[order] = cv_sorted
        tot = llr.copy()
        np.add.at(tot, edges_v, msg_cv)
        msg_vc = tot[edges_v] - msg_cv
        hard = (tot < 0).astype(np.uint8)
        hc = hard[edges_v][order]
        synd_ok = True
        for a, b in segs:
            if int(hc[a:b].sum()) & 1:
                synd_ok = False; break
        if synd_ok:
            break
    return hard


def decode_fecframe(llr: np.ndarray, rate: str = "2/3", frame: str = "normal",
                    max_iter: int = 50) -> tuple[Optional[np.ndarray], dict]:
    """LDPC min-sum + BCH on a FECFRAME's soft bits → BBFRAME bits (or None)."""
    kldpc, _, _ = _ldpc_params(rate, frame)
    kbch = _KBCH[frame][rate]
    t = _bch_t(frame, rate)
    cw = ldpc_decode(llr, rate, frame, max_iter=max_iter)
    info = cw[:kldpc]                            # = BCH codeword (Nbch bits)
    msg, nerr = bch_decode(info, kbch, t, frame)
    if nerr < 0:
        return None, {"frame": frame, "ldpc": "done", "bch": "uncorrectable"}
    return msg, {"frame": frame, "rate": rate, "bch_corrected": nerr, "kbch": kbch}


if __name__ == "__main__":
    import math
    rng = np.random.default_rng(0)
    fails = 0

    # 1) BCH round-trip per frame: inject up to t errors, recover.
    for frame, rate in (("short", "2/3"), ("normal", "3/4"), ("normal", "8/9")):
        kbch = _KBCH[frame][rate]; t = _bch_t(frame, rate)
        for nerr in (0, t, t + 1):
            msg = rng.integers(0, 2, kbch).astype(np.uint8)
            cw = bch_encode(msg, kbch, t, frame)
            bad = cw.copy()
            for pos in rng.choice(len(cw), nerr, replace=False):
                bad[pos] ^= 1
            dec, got = bch_decode(bad, kbch, t, frame)
            ok = (np.array_equal(dec, msg) and nerr <= t) or (got == -1 and nerr > t)
            print(f"BCH {frame} {rate} t={t}, {nerr} errors → corrected={got}: "
                  f"{'PASS' if ok else 'FAIL'}")
            fails += not ok

    # 2) FECFRAME round-trip (message → BCH → LDPC → AWGN → LDPC → BCH → message).
    #    Min-sum sits ~1–2 dB off sum-product → test with margin. Normal frame is big,
    #    so test a representative subset of rates per frame.
    for frame, rates, ebno_db, mi in (
        ("short",  ("3/5", "2/3", "3/4"), 5.0, 50),
        ("normal", ("1/2", "3/4", "8/9"), 4.5, 40),
    ):
        for rate in rates:
            kldpc, _, _ = _ldpc_params(rate, frame)
            kbch = _KBCH[frame][rate]; t = _bch_t(frame, rate)
            info = rng.integers(0, 2, kldpc).astype(np.uint8)
            cw = ldpc_encode(info, rate, frame)
            valid = all(np.bitwise_xor.reduce(cw[vs]) == 0 for vs in _check_adjacency(rate, frame))
            r_lin = eval(rate)
            sigma = math.sqrt(1.0 / (2.0 * r_lin * 10 ** (ebno_db / 10)))
            msg = rng.integers(0, 2, kbch).astype(np.uint8)
            fec = ldpc_encode(bch_encode(msg, kbch, t, frame), rate, frame)
            rx = (1.0 - 2.0 * fec.astype(np.float64)) + sigma * rng.standard_normal(fec.size)
            out, dinfo = decode_fecframe(2.0 * rx / (sigma ** 2), rate, frame, max_iter=mi)
            rt = out is not None and np.array_equal(out, msg)
            print(f"DVB-S2 {frame} {rate}: valid-codeword={valid}, round-trip @ "
                  f"{ebno_db} dB={'PASS' if rt else 'FAIL'} ({dinfo})")
            fails += (not valid) + (not rt)

    print(f"\n{'ALL PASS' if fails == 0 else str(fails) + ' FAILED'}")
    raise SystemExit(0 if fails == 0 else 1)
