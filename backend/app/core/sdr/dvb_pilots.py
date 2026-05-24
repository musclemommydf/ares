# SPDX-License-Identifier: MIT OR Apache-2.0
# Copyright (c) 2026 Ares

"""
dvb_pilots.py — DVB-T frame structure: pilot/TPS removal → data cells (EN 300 744 §4.5/4.6).

A DVB-T OFDM symbol carries K = 1705 (2K) / 6817 (8K) active carriers, of which a
fixed pattern are scattered pilots, continual pilots and TPS carriers; the rest —
exactly 1512 (2K) / 6048 (8K) — are data cells. To soft-decode a real DVB-T signal
the receiver must (a) pick the K active carriers out of the FFT, (b) equalise using
the known pilots, and (c) extract the data cells in mapper order before the symbol
de-interleaver. This module does all three.

Carrier tables (continual pilots Table 7, TPS carriers Table 8) and the scattered-
pilot rule k = 3·(l mod 4) + 12p are transcribed from ETSI EN 300 744 V1.6.1; the
self-test asserts the resulting data-cell count is exactly 1512/6048 for every
scattered-pilot phase — a wrong index would change that count.

Self-test: ``python -m app.core.sdr.dvb_pilots``.
"""
from __future__ import annotations

import numpy as np

_KMAX = {"2k": 1704, "8k": 6816}
_NDATA = {"2k": 1512, "8k": 6048}
_FFT = {"2k": 2048, "8k": 8192}

# Continual pilot carrier indices (EN 300 744 Table 7).
CONTINUAL_2K = [
    0, 48, 54, 87, 141, 156, 192, 201, 255, 279, 282, 333, 432, 450, 483, 525, 531,
    618, 636, 714, 759, 765, 780, 804, 873, 888, 918, 939, 942, 969, 984, 1050, 1101,
    1107, 1110, 1137, 1140, 1146, 1206, 1269, 1323, 1377, 1491, 1683, 1704,
]
CONTINUAL_8K = CONTINUAL_2K[:] + [
    1752, 1758, 1791, 1845, 1860, 1896, 1905, 1959, 1983, 1986, 2037, 2136, 2154,
    2187, 2229, 2235, 2322, 2340, 2418, 2463, 2469, 2484, 2508, 2577, 2592, 2622,
    2643, 2646, 2673, 2688, 2754, 2805, 2811, 2814, 2841, 2844, 2850, 2910, 2973,
    3027, 3081, 3195, 3387, 3408, 3456, 3462, 3495, 3549, 3564, 3600, 3609, 3663,
    3687, 3690, 3741, 3840, 3858, 3891, 3933, 3939, 4026, 4044, 4122, 4167, 4173,
    4188, 4212, 4281, 4296, 4326, 4347, 4350, 4377, 4392, 4458, 4509, 4515, 4518,
    4545, 4548, 4554, 4614, 4677, 4731, 4785, 4899, 5091, 5112, 5160, 5166, 5199,
    5253, 5268, 5304, 5313, 5367, 5391, 5394, 5445, 5544, 5562, 5595, 5637, 5643,
    5730, 5748, 5826, 5871, 5877, 5892, 5916, 5985, 6000, 6030, 6051, 6054, 6081,
    6096, 6162, 6213, 6219, 6222, 6249, 6252, 6258, 6318, 6381, 6435, 6489, 6603,
    6795, 6816,
]

# TPS carrier indices (EN 300 744 Table 8).
TPS_2K = [34, 50, 209, 346, 413, 569, 595, 688, 790, 901, 1073, 1219, 1262, 1286, 1469, 1594, 1687]
TPS_8K = TPS_2K[:] + [
    1738, 1754, 1913, 2050, 2117, 2273, 2299, 2392, 2494, 2605, 2777, 2923, 2966,
    2990, 3173, 3298, 3391, 3442, 3458, 3617, 3754, 3821, 3977, 4003, 4096, 4198,
    4309, 4481, 4627, 4670, 4694, 4877, 5002, 5095, 5146, 5162, 5321, 5458, 5525,
    5681, 5707, 5800, 5902, 6013, 6185, 6331, 6374, 6398, 6581, 6706, 6799,
]

_CONTINUAL = {"2k": CONTINUAL_2K, "8k": CONTINUAL_8K}
_TPS = {"2k": TPS_2K, "8k": TPS_8K}


def scattered_indices(symbol_index: int, mode: str) -> np.ndarray:
    """Scattered-pilot carrier indices for OFDM symbol l: k = 3·(l mod 4) + 12p."""
    kmax = _KMAX[mode]
    start = 3 * (symbol_index % 4)
    return np.arange(start, kmax + 1, 12)


def pilot_tps_mask(symbol_index: int, mode: str) -> np.ndarray:
    """Boolean mask over the K active carriers: True where the carrier is a scattered
    pilot, continual pilot or TPS carrier (i.e. NOT a data cell) for this symbol."""
    kmax = _KMAX[mode]
    mask = np.zeros(kmax + 1, dtype=bool)
    mask[scattered_indices(symbol_index, mode)] = True
    mask[_CONTINUAL[mode]] = True
    mask[_TPS[mode]] = True
    return mask


def data_carrier_indices(symbol_index: int, mode: str) -> np.ndarray:
    """The data-cell carrier indices for OFDM symbol l (everything that isn't a
    pilot/TPS). Length is exactly 1512 (2K) / 6048 (8K)."""
    return np.nonzero(~pilot_tps_mask(symbol_index, mode))[0]


# ── reference (pilot) PRBS — EN 300 744 §4.5.2 (X^11 + X^2 + 1, init all ones) ──
def prbs_wk(mode: str) -> np.ndarray:
    """w_k for k = 0..Kmax: a new bit per active carrier from the 11-bit PRBS."""
    kmax = _KMAX[mode]
    reg = [1] * 11
    out = np.empty(kmax + 1, dtype=np.uint8)
    for k in range(kmax + 1):
        out[k] = reg[10]                          # output is the last stage
        fb = reg[10] ^ reg[2]                     # X^11 + X^2 + 1
        reg = [fb] + reg[:10]
    return out


def pilot_values(mode: str) -> np.ndarray:
    """Expected (real) pilot amplitude per carrier: (4/3)·(1 − 2·w_k) = ±4/3."""
    return (4.0 / 3.0) * (1.0 - 2.0 * prbs_wk(mode).astype(np.float64))


# ── FFT bin ↔ active carrier mapping (carriers centred on DC) ─────────────────
def active_from_fft(X_row: np.ndarray, mode: str) -> np.ndarray:
    """Pick the K active carriers (k = 0..Kmax) out of an fft_len FFT row. Carrier k
    sits at FFT bin (k − Kmax/2) mod fft_len (DVB-T centres the band on DC)."""
    kmax = _KMAX[mode]
    fft = _FFT[mode]
    bins = (np.arange(kmax + 1) - kmax // 2) % fft
    return X_row[bins]


def fft_from_active(carriers: np.ndarray, mode: str) -> np.ndarray:
    """Inverse of active_from_fft — place K carriers into an fft_len grid (for tests)."""
    kmax = _KMAX[mode]
    fft = _FFT[mode]
    X = np.zeros(fft, dtype=np.complex64)
    bins = (np.arange(kmax + 1) - kmax // 2) % fft
    X[bins] = carriers
    return X


# ── pilot-based channel equalisation ─────────────────────────────────────────
def equalize(carriers: np.ndarray, symbol_index: int, mode: str) -> np.ndarray:
    """Estimate the channel H_k at the scattered+continual pilot carriers (known
    reference values) and linearly interpolate |H| and ∠H across all carriers, then
    divide it out. Flat/ideal channel ⇒ ~identity."""
    kmax = _KMAX[mode]
    pv = pilot_values(mode)
    pilot_pos = np.unique(np.concatenate([scattered_indices(symbol_index, mode),
                                          np.asarray(_CONTINUAL[mode])]))
    H = carriers[pilot_pos] / pv[pilot_pos]       # complex channel estimate at pilots
    k = np.arange(kmax + 1)
    Hi = (np.interp(k, pilot_pos, H.real).astype(np.complex64)
          + 1j * np.interp(k, pilot_pos, H.imag).astype(np.complex64))
    Hi[np.abs(Hi) < 1e-9] = 1e-9
    return (carriers / Hi).astype(np.complex64)


def extract_data_cells(X_by_symbol, mode: str, *, equalise: bool = True, phase: int = 0):
    """Full per-symbol FFT rows → list of complex data-cell arrays (one per symbol,
    each length Nmax) in carrier order, pilots/TPS removed. ``phase`` offsets the
    scattered-pilot index l (the capture may not start frame-aligned)."""
    out = []
    for i, X_row in enumerate(X_by_symbol):
        l = i + phase
        carr = active_from_fft(np.asarray(X_row), mode)
        if equalise:
            carr = equalize(carr, l, mode)
        out.append(carr[data_carrier_indices(l, mode)])
    return out


# ── soft QAM demap → per-bit soft values (column 0 = I0/MSB, +ve ⇒ bit 0) ────
def _qam_levels(order: int) -> np.ndarray:
    m = int(round(order ** 0.5))
    return np.arange(-(m - 1), m, 2, dtype=np.float64)     # e.g. 16-QAM → [-3,-1,1,3]


def soft_demap(cells: np.ndarray, modulation: str) -> np.ndarray:
    """Complex data cells → (N, v) soft bits matching dvb_interleaver's convention
    (Gray-mapped, MSB first; +ve soft ⇒ bit 0). QPSK is exact; square-QAM uses a
    max-log per-bit metric over the Gray-coded I/Q levels."""
    cells = np.asarray(cells, dtype=np.complex64)
    mod = modulation.lower()
    if mod in ("qpsk", "4qam"):
        return np.column_stack([cells.real, cells.imag]).astype(np.float64)
    order = {"16qam": 16, "64qam": 64, "256qam": 256}.get(mod, 16)
    lv = _qam_levels(order)
    scale = float(np.sqrt((np.mean(lv ** 2)) * 2.0))
    bits_per_axis = int(round(np.log2(len(lv))))
    # Gray code for each level index, MSB first.
    gray = [(i ^ (i >> 1)) for i in range(len(lv))]
    def axis_llr(x):
        x = x * scale
        d = -((x[:, None] - lv[None, :]) ** 2)             # neg squared dist → log-likelihood
        out = np.empty((x.size, bits_per_axis))
        for b in range(bits_per_axis):
            bit = bits_per_axis - 1 - b                     # MSB first
            ones = [j for j, g in enumerate(gray) if (g >> bit) & 1]
            zeros = [j for j, g in enumerate(gray) if not ((g >> bit) & 1)]
            out[:, b] = d[:, zeros].max(axis=1) - d[:, ones].max(axis=1)   # +ve ⇒ bit 0
        return out
    li = axis_llr(cells.real); lq = axis_llr(cells.imag)
    # interleave I/Q bits as DVB-T groups them: (I0,Q0,I1,Q1,...) MSB first
    out = np.empty((cells.size, 2 * bits_per_axis))
    out[:, 0::2] = li; out[:, 1::2] = lq
    return out


def decode_dvbt_rx(X_by_symbol, *, mode: str = "8k", modulation: str = "qpsk",
                   code_rate: str = "2/3"):
    """Full live DVB-T receive from per-symbol FFT rows: try each scattered-pilot
    phase, extract+equalise data cells, soft-demap, then the interleaver + inner/outer
    FEC (dvb_interleaver.decode_dvbt_full). Returns (ts_bytes, info) or (None, info)."""
    from . import dvb_interleaver
    for phase in range(4):
        cells_c = extract_data_cells(X_by_symbol, mode, phase=phase)
        cells_by_symbol = [soft_demap(c, modulation) for c in cells_c]
        ts, stats = dvb_interleaver.decode_dvbt_full(
            cells_by_symbol, code_rate=code_rate, modulation=modulation, mode=mode)
        if ts:
            return ts, {"pilot_phase": phase, **stats}
    return None, {"reason": "no DVB-T lock across pilot phases"}


if __name__ == "__main__":
    fails = 0
    # 1) data-cell count must be exactly Ndata for every scattered-pilot phase
    for mode in ("2k", "8k"):
        for l in range(4):
            n = len(data_carrier_indices(l, mode))
            ok = n == _NDATA[mode]
            print(f"{mode} symbol l={l}: data cells = {n} (expected {_NDATA[mode]}) {'PASS' if ok else 'FAIL'}")
            fails += not ok
        # table sanity: counts + no out-of-range / duplicate indices
        for name, lst in (("continual", _CONTINUAL[mode]), ("TPS", _TPS[mode])):
            uniq = len(set(lst)) == len(lst) and min(lst) >= 0 and max(lst) <= _KMAX[mode]
            fails += not uniq
    print(f"continual 2K/8K = {len(CONTINUAL_2K)}/{len(CONTINUAL_8K)} (expect 45/177); "
          f"TPS 2K/8K = {len(TPS_2K)}/{len(TPS_8K)} (expect 17/68)")
    fails += not (len(CONTINUAL_2K) == 45 and len(CONTINUAL_8K) == 177
                  and len(TPS_2K) == 17 and len(TPS_8K) == 68)

    # 2) active-carrier mapping round-trip + data extraction recovers planted data
    rng = np.random.default_rng(0)
    for mode in ("2k", "8k"):
        # build a 4-symbol frame (covers all scattered-pilot phases l=0..3)
        X_syms, planted_syms = [], []
        for l in range(4):
            carr = pilot_values(mode).astype(np.complex64)    # pilot refs at every carrier
            didx = data_carrier_indices(l, mode)
            planted = (rng.standard_normal(didx.size) + 1j * rng.standard_normal(didx.size)).astype(np.complex64)
            carr[didx] = planted
            X_syms.append(fft_from_active(carr, mode))        # ideal channel
            planted_syms.append(planted)
        cells = extract_data_cells(X_syms, mode)             # uses list index as l
        ok = all(np.allclose(cells[l], planted_syms[l], atol=1e-3) for l in range(4))
        print(f"{mode} active-map + equalise + data-extract round-trip (4 phases): {'PASS' if ok else 'FAIL'}")
        fails += not ok

    # 3) FULL LIVE DVB-T end-to-end (QPSK): TS → FEC → interleave → QPSK map +
    #    pilot/TPS insertion → FFT grid → decode_dvbt_rx (pilot extract + equalise +
    #    de-interleave + soft Viterbi + RS) → TS.
    from app.core.sdr import dvb_fec, dvb_inner_fec, dvb_interleaver as dil
    mode, mod, v, rate = "2k", "qpsk", 2, "2/3"
    Nmax = _NDATA[mode]
    ts = bytearray()
    for _ in range(60):
        ts += bytes([0x47]) + bytes(rng.integers(0, 256, 187).tolist())
    rand = dvb_fec.derandomise(bytes(ts))
    rs = b"".join(dvb_fec.rs_encode(rand[i:i + 188]) for i in range(0, len(rand), 188))
    il = dvb_inner_fec.interleave(rs)
    coded = dvb_inner_fec.conv_encode([int(b) for byte in il for b in f"{byte:08b}"])
    punc = dvb_inner_fec.puncture(coded, rate)
    soft = np.array([1.0 if b == 0 else -1.0 for b in punc])
    cell = Nmax * v
    X_syms = []
    for i in range(0, len(soft), cell):
        block = soft[i:i + cell]
        if block.size < cell:
            block = np.concatenate([block, np.zeros(cell - block.size)])
        sym_idx = len(X_syms)
        cells = dil.inner_interleave(block, sym_idx, v, mode)        # (Nmax, 2) of ±1
        carr = pilot_values(mode).astype(np.complex64)               # pilots at every carrier
        sym = (cells[:, 0] + 1j * cells[:, 1]).astype(np.complex64)  # QPSK
        carr[data_carrier_indices(sym_idx, mode)] = sym
        X_syms.append(fft_from_active(carr, mode))
    ts_out, info = decode_dvbt_rx(X_syms, mode=mode, modulation=mod, code_rate=rate)
    ok_live = ts_out is not None and ts[:188 * 4] in ts_out
    print(f"FULL LIVE DVB-T (pilots+TPS → equalise → soft Viterbi → TS): "
          f"{'PASS' if ok_live else 'FAIL'} (info={info})")
    fails += not ok_live

    print(f"\n{'ALL PASS' if fails == 0 else str(fails)+' FAILED'}")
    raise SystemExit(0 if fails == 0 else 1)
