# SPDX-License-Identifier: MIT OR Apache-2.0
# Copyright (c) 2026 Ares

"""
dvb_s2_pl.py — DVB-S2 physical-layer framing primitives (EN 302 307-1 §5.5).

What a receiver needs to find DVB-S2 FECFRAMEs in a symbol stream and learn their
config, before the FEC (dvb_s2_fec):

  * **SOF** — the 26-symbol π/2-BPSK Start-Of-Frame (0x18D2E82); correlate to locate
    each PLFRAME.
  * **PLS** — the 64-symbol Physical-Layer Signalling code: MODCOD (5 b) + TYPE (frame
    size + pilots), bi-orthogonally (64,7)-coded and scrambled. Decoded by correlating
    against all candidate codewords → constellation + code rate + frame size + pilots.
  * **PL scrambler** — the complex Gold sequence (two degree-18 m-sequences) that
    randomises the XFECFRAME; descramble = multiply by exp(−j·Rn·π/2).
  * **constellations + soft-demap** — DVB-S2 *and* DVB-S2X: QPSK/8PSK/8APSK/16APSK
    (incl. 8+8)/32APSK (incl. 4+12+16, 4+8+4+16)/64APSK (incl. 8+16+20+20)/128APSK/256APSK,
    with the §5.4 Gray label maps + rate-dependent ring radii (dvb_s2_const_tables, from
    gr-dtv), a max-log per-bit LLR demapper, and the §5.3.3 column bit-interleaver (S2 + S2X
    permutation tables) with its inverse.

``decode_dvbs2_plframe`` chains all of it — SOF-sync → PLS → derotate + descramble →
de-pilot → soft-demap (+ de-interleave) → BCH+LDPC (dvb_s2_fec). It blind-decodes the
DVB-S2 MODCODs (Table 12); DVB-S2X formats (extended code rates + APSK) decode via an
explicit ``fmt=`` config, since S2X PLHEADER signalling (extended PLS codewords + VL-SNR
SOF) isn't blind-detected here. ``decodable_formats(frame)`` lists what the engine handles.
Verified by round-trip self-test (no live RF hardware here).

Self-test: ``python -m app.core.sdr.dvb_s2_pl``.
"""
from __future__ import annotations

from typing import Optional

import numpy as np

_SQRT1_2 = 1.0 / np.sqrt(2.0)
_SOF_HEX = 0x18D2E82                       # 26-bit SOF, MSB first
_PLS_SCRAMBLE = "0111000110011101100000111100100101010011010000100010110111111010"  # 64 bits

# (32,6) generator matrix G (Figure 13b), 6 rows × 32 bits.
_G32 = [
    "01010101010101010101010101010101",
    "00110011001100110011001100110011",
    "00001111000011110000111100001111",
    "00000000111111110000000011111111",
    "00000000000000001111111111111111",
    "11111111111111111111111111111111",
]

# MODCOD value → (modulation, code_rate) (Table 12). 0 = dummy PLFRAME.
MODCOD_TABLE = {
    1: ("qpsk", "1/4"), 2: ("qpsk", "1/3"), 3: ("qpsk", "2/5"), 4: ("qpsk", "1/2"),
    5: ("qpsk", "3/5"), 6: ("qpsk", "2/3"), 7: ("qpsk", "3/4"), 8: ("qpsk", "4/5"),
    9: ("qpsk", "5/6"), 10: ("qpsk", "8/9"), 11: ("qpsk", "9/10"),
    12: ("8psk", "3/5"), 13: ("8psk", "2/3"), 14: ("8psk", "3/4"), 15: ("8psk", "5/6"),
    16: ("8psk", "8/9"), 17: ("8psk", "9/10"),
    18: ("16apsk", "2/3"), 19: ("16apsk", "3/4"), 20: ("16apsk", "4/5"), 21: ("16apsk", "5/6"),
    22: ("16apsk", "8/9"), 23: ("16apsk", "9/10"),
    24: ("32apsk", "3/4"), 25: ("32apsk", "4/5"), 26: ("32apsk", "5/6"), 27: ("32apsk", "8/9"),
    28: ("32apsk", "9/10"),
}


def decodable_formats(frame: str = "normal") -> list[tuple[str, str]]:
    """Every (modulation, code_rate) this receiver can fully decode for the given
    FECFRAME size — constellation (dvb_s2_const_tables) ∩ LDPC+BCH (dvb_s2_fec). Spans
    DVB-S2 and the DVB-S2X extensions (8/16/32/64/128/256-APSK + the S2X code rates).
    NB: a format being *decodable with a known config* is separate from *blind PLS
    detection* — decode_pls covers the DVB-S2 MODCODs; S2X PLHEADER signalling (extended
    PLS codewords + VL-SNR SOF) is not blind-detected, so S2X needs ``fmt=`` supplied."""
    from . import dvb_s2_fec
    rates = set(dvb_s2_fec.available_rates(frame))
    out = []
    for mod, rad in _RADII.items():
        for r in sorted(rates):
            if r in rad or None in rad:
                out.append((mod, r))
    return out


def _pi2_bpsk(bits, start: int = 0) -> np.ndarray:
    """π/2-BPSK modulate a header bit sequence at absolute header position ``start``."""
    out = np.empty(len(bits), dtype=np.complex64)
    for m, b in enumerate(bits):
        s = (1.0 - 2.0 * b) * _SQRT1_2
        pos = start + m
        out[m] = (s + 1j * s) if (pos % 2 == 0) else (-s + 1j * s)
    return out


def sof_symbols() -> np.ndarray:
    bits = [(_SOF_HEX >> (25 - i)) & 1 for i in range(26)]
    return _pi2_bpsk(bits, 0)


def _pls_bits(modcod: int, short: bool, pilots: bool) -> list[int]:
    b = [(modcod >> (4 - i)) & 1 for i in range(5)] + [1 if short else 0]   # b1..b6
    y32 = [0] * 32
    for i in range(6):
        if b[i]:
            row = _G32[i]
            for j in range(32):
                y32[j] ^= int(row[j])
    b7 = 1 if pilots else 0
    y64 = []
    for j in range(32):
        y64.append(y32[j]); y64.append(y32[j] ^ b7)        # repeat, complemented if b7
    scr = [int(c) for c in _PLS_SCRAMBLE]
    return [y64[i] ^ scr[i] for i in range(64)]


def pls_symbols(modcod: int, short: bool, pilots: bool) -> np.ndarray:
    return _pi2_bpsk(_pls_bits(modcod, short, pilots), start=26)            # after the 26-symbol SOF


# ── SOF correlation + PLS decode ─────────────────────────────────────────────
def find_sof(symbols: np.ndarray, threshold: float = 0.6) -> Optional[int]:
    """Index of the best SOF correlation peak in ``symbols`` (normalised |corr| over
    the 26 SOF symbols), or None below threshold."""
    x = np.asarray(symbols, dtype=np.complex64)
    sof = sof_symbols()
    if x.size < sof.size:
        return None
    best_i, best_m = None, 0.0
    sof_n = np.linalg.norm(sof)
    for i in range(x.size - sof.size + 1):
        seg = x[i:i + sof.size]
        m = abs(np.vdot(sof, seg)) / (sof_n * (np.linalg.norm(seg) + 1e-12))
        if m > best_m:
            best_m, best_i = m, i
    return best_i if best_m >= threshold else None


def decode_pls(pls_rx: np.ndarray, phase: float = 0.0) -> Optional[dict]:
    """Decode the 64 PLS symbols (the slot after the SOF) → config. COHERENT
    correlation (real part): the frame-size bit negates the whole codeword, so
    |corr| can't distinguish it — the caller must phase-correct first (the SOF
    gives the reference); ``phase`` derotates the input if not already done."""
    x = np.asarray(pls_rx, dtype=np.complex64)[:64]
    if x.size < 64:
        return None
    if phase:
        x = x * np.exp(-1j * phase)
    best, best_m = None, -1e30
    for modcod in MODCOD_TABLE:
        for short in (False, True):
            for pilots in (False, True):
                ref = pls_symbols(modcod, short, pilots)
                m = np.vdot(ref, x).real            # coherent — preserves the sign
                if m > best_m:
                    best_m, best = m, (modcod, short, pilots)
    if best is None:
        return None
    modcod, short, pilots = best
    mod, rate = MODCOD_TABLE[modcod]
    return {"modcod": modcod, "modulation": mod, "code_rate": rate,
            "frame": "short" if short else "normal", "pilots": pilots,
            "corr": round(float(best_m), 2)}


# ── PL scrambler (complex Gold sequence, §5.5.4) ─────────────────────────────
def _rn_sequence(n: int, length: int) -> np.ndarray:
    """Rn(i) ∈ {0,1,2,3} for the chosen scrambling-code number n."""
    P = (1 << 18) - 1
    need = length + 131072 + n + 2
    x = np.zeros(need, dtype=np.uint8); x[0] = 1
    y = np.zeros(need, dtype=np.uint8); y[:18] = 1
    for i in range(need - 18):
        x[i + 18] = x[i + 7] ^ x[i]
        y[i + 18] = y[i + 10] ^ y[i + 7] ^ y[i + 5] ^ y[i]
    def zn(i):
        return x[(i + n) % P] ^ y[i % P]
    rn = np.empty(length, dtype=np.uint8)
    for i in range(length):
        rn[i] = (2 * zn((i + 131072) % P) + zn(i)) & 3
    return rn


def pl_descramble(symbols: np.ndarray, n: int = 0) -> np.ndarray:
    """Undo the PL scrambling: divide by exp(j·Rn·π/2) ⇒ multiply by exp(−j·Rn·π/2)."""
    x = np.asarray(symbols, dtype=np.complex64)
    rn = _rn_sequence(n, x.size)
    rot = np.exp(-1j * rn.astype(np.float64) * (np.pi / 2.0)).astype(np.complex64)
    return x * rot


def pl_scramble(symbols: np.ndarray, n: int = 0) -> np.ndarray:
    x = np.asarray(symbols, dtype=np.complex64)
    rn = _rn_sequence(n, x.size)
    rot = np.exp(1j * rn.astype(np.float64) * (np.pi / 2.0)).astype(np.complex64)
    return x * rot


from .dvb_s2_const_tables import CONST as _CONST, RADII as _RADII, PERM as _PERM

# bits/symbol per modulation, derived from the constellation cardinality (2..8).
_BITS_PER_SYM = {m: len(pts).bit_length() - 1 for m, pts in _CONST.items()}
_PI = np.pi


def _norm_unit_energy(pts: np.ndarray) -> np.ndarray:
    return (pts / np.sqrt(np.mean(np.abs(pts) ** 2))).astype(np.complex64)


def constellation(mod: str, code_rate: str = "2/3") -> np.ndarray:
    """Complex constellation indexed by the b-bit Gray label (EN 302 307-1/-2 §5.4),
    unit average energy. (ring, angle) per label from dvb_s2_const_tables.CONST; the
    rate-dependent APSK ring radii from RADII (Tables 9/10 for S2, the S2X equivalents).
    Covers QPSK/8PSK/8APSK/16APSK/8+8APSK/32APSK/64APSK/128APSK/256APSK + variants."""
    mod = mod.lower()
    if mod not in _CONST:
        raise ValueError(f"unknown modulation {mod}")
    rad = _RADII[mod]
    radius = rad.get(code_rate) or rad.get(None)
    if radius is None:
        raise ValueError(f"{mod} has no constellation for rate {code_rate} (have: "
                         f"{sorted(r for r in rad if r)})")
    pts = np.array([radius[ring] * np.exp(1j * ang) for ring, ang in _CONST[mod]])
    return _norm_unit_energy(pts)


def _interleaver_perm(mod: str, code_rate: str, frame: str = "normal") -> list[int]:
    """§5.3.3 bit-interleaver column→symbol-bit map: symbol bit k (MSB first) reads
    FECFRAME column perm[k]. From gr-dtv dvbs2_interleaver rowaddr tables (S2 + S2X);
    identity for any (mod, rate, frame) not listed. QPSK has no interleaver."""
    b = _BITS_PER_SYM[mod]
    return _PERM.get((mod, code_rate, frame), list(range(b)))


def _frame_of(n_bits: int) -> str:
    return "short" if n_bits == 16200 else "normal"


def soft_demap_to_fecframe(symbols: np.ndarray, mod: str, code_rate: str, n_bits: int) -> np.ndarray:
    """Max-log per-bit LLR demap → FECFRAME-order soft bits (LLR>0 ⇒ bit 0). Undoes the
    §5.3.3 column-write/row-read bit interleaver for 8PSK/APSK (QPSK has none)."""
    mod = mod.lower()
    cons = constellation(mod, code_rate)
    b = _BITS_PER_SYM[mod]
    y = np.asarray(symbols, dtype=np.complex64)[: n_bits // b]
    rows = n_bits // b                                  # = #symbols (= interleaver rows)
    perm = _interleaver_perm(mod, code_rate, _frame_of(n_bits))
    # per bit position p (MSB first), which labels have that bit = 1
    ones = [np.array([(lbl >> (b - 1 - p)) & 1 for lbl in range(len(cons))], bool) for p in range(b)]
    llr = np.empty(n_bits)
    d = np.abs(y[:, None] - cons[None, :]) ** 2          # (nsym, 2^b) squared distances
    scale = 4.0
    for p in range(b):
        l1 = d[:, ones[p]].min(axis=1)
        l0 = d[:, ~ones[p]].min(axis=1)
        val = (l1 - l0) * scale                          # +ve ⇒ bit 0
        if mod == "qpsk":
            llr[p::b] = val                              # no interleaver: bits 2i,2i+1
        else:
            c = perm[p]
            llr[c * rows: (c + 1) * rows] = val          # de-interleave: symbol-bit p → column c
    return llr


def modulate_fecframe(bits: np.ndarray, mod: str, code_rate: str) -> np.ndarray:
    """TX: FECFRAME bits → symbols (with the §5.3.3 bit interleaver for 8PSK/APSK)."""
    mod = mod.lower()
    cons = constellation(mod, code_rate)
    b = _BITS_PER_SYM[mod]
    bits = np.asarray(bits, dtype=np.int64)
    rows = bits.size // b
    perm = _interleaver_perm(mod, code_rate, _frame_of(bits.size))
    syms = np.empty(rows, dtype=np.complex64)
    for s in range(rows):
        if mod == "qpsk":
            label = (bits[s*b] << 1) | bits[s*b + 1]
        else:
            label = 0
            for k in range(b):                            # k=0 is MSB; reads column perm[k]
                label = (label << 1) | int(bits[perm[k]*rows + s])
        syms[s] = cons[label]
    return syms


def _depilot(region: np.ndarray, n_data: int, pilots: bool) -> np.ndarray:
    """Strip the 36-symbol pilot block inserted after every 16 slots (1440 symbols)."""
    if not pilots:
        return region[:n_data]
    out = []
    src = 0
    while len(out) < n_data:
        take = min(16 * 90, n_data - len(out))
        out.extend(region[src:src + take]); src += take
        if len(out) < n_data:
            src += 36                                  # skip the pilot block
    return np.asarray(out[:n_data], dtype=np.complex64)


def decode_dvbs2_plframe(symbols: np.ndarray, n: int = 0, fmt: Optional[dict] = None):
    """Full DVB-S2/S2X receive of one PLFRAME: SOF-sync → PLS decode → derotate + PL
    descramble → de-pilot → soft-demap (+ bit de-interleave) → BCH+LDPC → BBFRAME bits.
    Returns (bbframe_bits | None, info). Handles every constellation/code-rate/frame the
    engine supports (see decodable_formats).

    Blind PLS decode covers the DVB-S2 MODCODs (Table 12). For DVB-S2X — whose PLHEADER
    uses extended PLS codewords this doesn't blind-detect — pass the known config as
    ``fmt={'modulation','code_rate','frame','pilots'}`` to skip PLS and decode directly
    (SOF is still used for timing + phase)."""
    from . import dvb_s2_fec
    x = np.asarray(symbols, dtype=np.complex64)
    off = find_sof(x)
    if off is None:
        return None, {"reason": "no SOF"}
    sof = sof_symbols()
    phase = float(np.angle(np.vdot(sof, x[off:off + 26])))
    if fmt is not None:
        cfg = {"modulation": fmt["modulation"], "code_rate": fmt["code_rate"],
               "frame": fmt.get("frame", "normal"), "pilots": bool(fmt.get("pilots", False)),
               "source": "explicit-fmt"}
    else:
        cfg = decode_pls(x[off + 26:off + 90], phase=phase)
        if not cfg:
            return None, {"reason": "PLS decode failed"}
    n_bits = 16200 if cfg["frame"] == "short" else 64800
    if cfg["code_rate"] not in dvb_s2_fec.available_rates(cfg["frame"]):
        return None, {"reason": f"LDPC rate {cfg['code_rate']} ({cfg['frame']}) not tabulated", **cfg}
    bps = _BITS_PER_SYM[cfg["modulation"]]
    n_data = n_bits // bps
    slots = n_data // 90
    n_blocks = (slots - 1) // 16 if cfg["pilots"] else 0
    total = n_data + n_blocks * 36
    region = x[off + 90:off + 90 + total]
    region = pl_descramble(region * np.exp(-1j * phase), n)     # derotate then descramble
    data = _depilot(region, n_data, cfg["pilots"])
    llr = soft_demap_to_fecframe(data, cfg["modulation"], cfg["code_rate"], n_bits)
    out, info = dvb_s2_fec.decode_fecframe(llr, cfg["code_rate"], frame=cfg["frame"])
    return out, {**cfg, **info}


if __name__ == "__main__":
    rng = np.random.default_rng(0)
    fails = 0

    # 1) SOF correlation finds the frame start in a noisy stream
    sof = sof_symbols()
    pad_a = (rng.standard_normal(40) + 1j * rng.standard_normal(40)).astype(np.complex64) * 0.3
    pad_b = (rng.standard_normal(40) + 1j * rng.standard_normal(40)).astype(np.complex64) * 0.3
    stream = np.concatenate([pad_a, sof + 0.1 * (rng.standard_normal(26) + 1j * rng.standard_normal(26)), pad_b])
    idx = find_sof(stream)
    print(f"SOF correlation: found at {idx} (expect 40): {'PASS' if idx == 40 else 'FAIL'}")
    fails += idx != 40

    # 2) PLS decode recovers MODCOD + frame size + pilots
    for modcod, short, pilots in ((6, True, False), (19, False, True), (28, True, True)):
        rx = pls_symbols(modcod, short, pilots) + 0.05 * (rng.standard_normal(64) + 1j * rng.standard_normal(64))
        got = decode_pls(rx)
        ok = got and got["modcod"] == modcod and (got["frame"] == ("short" if short else "normal")) and got["pilots"] == pilots
        print(f"PLS decode modcod={modcod} short={short} pilots={pilots} → "
              f"{got and (got['modcod'], got['frame'], got['pilots'], got['modulation'], got['code_rate'])}: "
              f"{'PASS' if ok else 'FAIL'}")
        fails += not ok

    # 3) PL scrambler round-trip
    data = (rng.standard_normal(2000) + 1j * rng.standard_normal(2000)).astype(np.complex64)
    rt = pl_descramble(pl_scramble(data, 0), 0)
    ok = np.allclose(rt, data, atol=1e-4)
    print(f"PL scrambler (Gold) round-trip: {'PASS' if ok else 'FAIL'}")
    fails += not ok
    # Rn must be in {0,1,2,3}
    rn = _rn_sequence(0, 1000)
    fails += not set(np.unique(rn)).issubset({0, 1, 2, 3})

    # 4) Soft-demap round-trip per modulation (constellation + §5.3.3 bit interleaver
    #    inverse + LLR sign): random bits → map → AWGN → demap → hard-decide → BER≈0.
    import math
    from app.core.sdr import dvb_s2_fec as s2
    for mod, rate in (("qpsk", "2/3"), ("8psk", "3/5"), ("16apsk", "2/3"), ("32apsk", "3/4")):
        b = _BITS_PER_SYM[mod]
        nb = b * 4000
        bits = rng.integers(0, 2, nb).astype(np.uint8)
        syms = modulate_fecframe(bits, mod, rate)
        sigma = 0.03                                       # near-noiseless: demap must be exact
        rx = syms + sigma * (rng.standard_normal(syms.size) + 1j * rng.standard_normal(syms.size)).astype(np.complex64)
        llr = soft_demap_to_fecframe(rx, mod, rate, nb)
        hard = (llr < 0).astype(np.uint8)                  # LLR>0 ⇒ bit 0
        ber = np.mean(hard != bits)
        cons = constellation(mod, rate)
        ue = abs(np.mean(np.abs(cons) ** 2) - 1.0) < 1e-6
        ok = ber == 0 and len(cons) == (1 << b) and ue
        print(f"demap {mod:6} {rate}: |cons|={len(cons)} unit-energy={ue} BER={ber:.4f}: "
              f"{'PASS' if ok else 'FAIL'}")
        fails += not ok

    # 5) Full FEC + demap per modulation (no PL framing): msg → BCH → LDPC → map →
    #    AWGN → demap → LDPC+BCH → msg. Validates the demap feeds the FEC correctly.
    for mod, rate, ebno in (("qpsk", "2/3", 4.0), ("8psk", "3/5", 6.0),
                            ("16apsk", "2/3", 7.5), ("32apsk", "3/4", 9.5)):
        kbch = s2._KBCH["short"][rate]; t = s2._bch_t("short", rate)
        msg = rng.integers(0, 2, kbch).astype(np.uint8)
        cw = s2.ldpc_encode(s2.bch_encode(msg, kbch, t, "short"), rate, "short")   # 16200 bits
        syms = modulate_fecframe(cw, mod, rate)
        r_lin = eval(rate)
        sigma = math.sqrt(1.0 / (2.0 * _BITS_PER_SYM[mod] * r_lin * 10 ** (ebno / 10)))
        rx = syms + sigma * (rng.standard_normal(syms.size) + 1j * rng.standard_normal(syms.size)).astype(np.complex64)
        llr = soft_demap_to_fecframe(rx, mod, rate, 16200)
        out, info = s2.decode_fecframe(llr, rate, frame="short")
        ok = out is not None and np.array_equal(out, msg)
        print(f"FEC+demap {mod:6} {rate} @ {ebno} dB: {'PASS' if ok else 'FAIL'} ({info})")
        fails += not ok

    # 6) FULL DVB-S2 PLFRAME end-to-end (SOF-sync → PLS → descramble → de-pilot →
    #    demap → BCH+LDPC). Short QPSK 2/3 (pilots off/on) and short 8PSK 3/5.
    def _insert_pilots(xfec):
        out, src = [], 0
        while src < xfec.size:
            out.extend(xfec[src:src + 16 * 90]); src += 16 * 90
            if src < xfec.size:
                out.extend([(_SQRT1_2 + 1j * _SQRT1_2)] * 36)
        return np.asarray(out, dtype=np.complex64)

    for mod, rate, modcod, pilots, sig in (("qpsk", "2/3", 6, False, 0.18),
                                           ("qpsk", "2/3", 6, True, 0.18),
                                           ("8psk", "3/5", 12, True, 0.12)):
        kbch = s2._KBCH["short"][rate]; t = s2._bch_t("short", rate)
        msg = rng.integers(0, 2, kbch).astype(np.uint8)
        cw = s2.ldpc_encode(s2.bch_encode(msg, kbch, t, "short"), rate, "short")
        xfec = modulate_fecframe(cw, mod, rate)
        if pilots:
            xfec = _insert_pilots(xfec)
        scrambled = pl_scramble(xfec, 0)
        plheader = np.concatenate([sof_symbols(), pls_symbols(modcod, True, pilots)])
        frame = np.concatenate([(rng.standard_normal(20) + 1j * rng.standard_normal(20)).astype(np.complex64) * 0.2,
                                plheader, scrambled])
        frame = frame + sig * (rng.standard_normal(frame.size) + 1j * rng.standard_normal(frame.size)).astype(np.complex64)
        out, info = decode_dvbs2_plframe(frame, 0)
        ok = out is not None and np.array_equal(out, msg)
        print(f"full PLFRAME (short {mod} {rate}, pilots={pilots}): {'PASS' if ok else 'FAIL'} ({info})")
        fails += not ok

    # 7) DVB-S2X constellations: demap round-trip (constellation + interleaver inverse)
    #    for every S2X APSK type, near-noiseless → BER 0.
    print("--- DVB-S2X ---")
    for mod, rate in (("8apsk", "100/180"), ("16apsk", "13/18"), ("8_8apsk", "100/180"),
                      ("4_12_16apsk", "2/3"), ("4_8_4_16apsk", "128/180"), ("64apsk", "128/180"),
                      ("8_16_20_20apsk", "4/5"), ("128apsk", "135/180"), ("256apsk", "124/180")):
        b = _BITS_PER_SYM[mod]; nb = b * 6000
        bits = rng.integers(0, 2, nb).astype(np.uint8)
        syms = modulate_fecframe(bits, mod, rate)
        sig = 0.004 if b >= 8 else 0.012        # 256APSK is dense → near-noiseless
        rx = syms + sig * (rng.standard_normal(syms.size) + 1j * rng.standard_normal(syms.size)).astype(np.complex64)
        hard = (soft_demap_to_fecframe(rx, mod, rate, nb) < 0).astype(np.uint8)
        cons = constellation(mod, rate)
        ok = np.mean(hard != bits) == 0 and len(cons) == (1 << b)
        print(f"demap {mod:14} {rate:8} |cons|={len(cons):3}: {'PASS' if ok else 'FAIL'}")
        fails += not ok

    # 8) S2X full FEC + demap (normal frame, new code rates + high-order APSK).
    for mod, rate, ebno in (("16apsk", "13/18", 9.0), ("64apsk", "128/180", 14.5),
                            ("256apsk", "124/180", 18.0), ("qpsk", "13/45", 3.5)):
        kbch = s2._KBCH["normal"][rate]; t = s2._bch_t("normal", rate)
        msg = rng.integers(0, 2, kbch).astype(np.uint8)
        cw = s2.ldpc_encode(s2.bch_encode(msg, kbch, t, "normal"), rate, "normal")   # 64800 bits
        syms = modulate_fecframe(cw, mod, rate)
        sigma = math.sqrt(1.0 / (2.0 * _BITS_PER_SYM[mod] * eval(rate) * 10 ** (ebno / 10)))
        rx = syms + sigma * (rng.standard_normal(syms.size) + 1j * rng.standard_normal(syms.size)).astype(np.complex64)
        llr = soft_demap_to_fecframe(rx, mod, rate, 64800)
        out, info = s2.decode_fecframe(llr, rate, frame="normal", max_iter=40)
        ok = out is not None and np.array_equal(out, msg)
        print(f"FEC+demap {mod:10} {rate:8} (normal) @ {ebno} dB: {'PASS' if ok else 'FAIL'} ({info})")
        fails += not ok

    # 9) S2X full PLFRAME via explicit fmt= (blind PLS doesn't cover S2X): 16APSK 13/18
    #    normal, pilots on. SOF-sync → descramble → de-pilot → demap → BCH+LDPC.
    mod, rate = "16apsk", "13/18"
    kbch = s2._KBCH["normal"][rate]; t = s2._bch_t("normal", rate)
    msg = rng.integers(0, 2, kbch).astype(np.uint8)
    cw = s2.ldpc_encode(s2.bch_encode(msg, kbch, t, "normal"), rate, "normal")
    xfec = _insert_pilots(modulate_fecframe(cw, mod, rate))
    frame = np.concatenate([(rng.standard_normal(20) + 1j * rng.standard_normal(20)).astype(np.complex64) * 0.2,
                            sof_symbols(), pls_symbols(19, False, True), pl_scramble(xfec, 0)])
    frame = frame + 0.05 * (rng.standard_normal(frame.size) + 1j * rng.standard_normal(frame.size)).astype(np.complex64)
    out, info = decode_dvbs2_plframe(frame, 0, fmt={"modulation": mod, "code_rate": rate,
                                                    "frame": "normal", "pilots": True})
    ok = out is not None and np.array_equal(out, msg)
    print(f"full PLFRAME via fmt= ({mod} {rate} normal, pilots): {'PASS' if ok else 'FAIL'} ({info.get('source')})")
    fails += not ok

    print(f"\n{'ALL PASS' if fails == 0 else str(fails)+' FAILED'}")
    raise SystemExit(0 if fails == 0 else 1)
