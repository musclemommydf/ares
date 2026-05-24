# SPDX-License-Identifier: MIT OR Apache-2.0
# Copyright (c) 2026 Ares

"""
dvb_tps.py — DVB-T Transmission Parameter Signalling decode (EN 300 744 §4.6).

The TPS carriers signal the transmission config (constellation, code rate, guard,
mode, cell id) so a receiver doesn't have to brute-force it. One TPS bit is sent
per OFDM symbol, DBPSK along the time axis on every TPS carrier (all TPS carriers
in a symbol carry the same bit). A frame is 68 symbols:

  s0 init · s1..s16 sync · s17..s22 length · s23,s24 frame# · s25,s26 constellation
  · s27..s29 hierarchy · s30..s32 code-rate HP · s33..s35 code-rate LP
  · s36,s37 guard · s38,s39 mode · s40..s47 cell id · s48..s53 (0) · s54..s67 BCH

Differential decode is absolute-phase- and pilot-PRBS-independent: comparing a TPS
carrier to its value in the previous symbol yields s_l directly (the per-carrier
PRBS sign and any common channel phase cancel in the product). We then find the
sync word to frame-align and read the fields.

Self-test: ``python -m app.core.sdr.dvb_tps``.
"""
from __future__ import annotations

from typing import Optional

import numpy as np

from app.core.sdr import dvb_pilots

_SYNC_A = "0011010111101110"   # TPS blocks 1 & 3
_SYNC_B = "1100101000010001"   # TPS blocks 2 & 4 (inverse)
_CONSTELLATION = {"00": "qpsk", "01": "16qam", "10": "64qam"}
_CODE_RATE = {"000": "1/2", "001": "2/3", "010": "3/4", "011": "5/6", "100": "7/8"}
_GUARD = {"00": "1/32", "01": "1/16", "10": "1/8", "11": "1/4"}
_MODE = {"00": "2k", "01": "8k", "10": "4k"}


def _tps_carriers(X_by_symbol, mode: str) -> np.ndarray:
    """Equalised TPS-carrier values per symbol → array (n_symbols, n_tps)."""
    tps_idx = np.asarray(dvb_pilots._TPS[mode])
    rows = []
    for l, X_row in enumerate(X_by_symbol):
        carr = dvb_pilots.active_from_fft(np.asarray(X_row), mode)
        carr = dvb_pilots.equalize(carr, l, mode)
        rows.append(carr[tps_idx])
    return np.array(rows)


def _differential_bits(tps: np.ndarray) -> list[int]:
    """s_l for l=1..L-1 from consecutive-symbol DBPSK comparison (majority over carriers)."""
    bits = []
    for l in range(1, tps.shape[0]):
        corr = np.sum((tps[l] * np.conj(tps[l - 1])).real)
        bits.append(0 if corr >= 0 else 1)
    return bits


def _match(bits: list[int], j0: int, pattern: str) -> bool:
    if j0 + len(pattern) > len(bits):
        return False
    return all(bits[j0 + i] == int(c) for i, c in enumerate(pattern))


def decode_tps(X_by_symbol, mode: str) -> Optional[dict]:
    """Decode the TPS from per-symbol FFT rows. Returns the config dict (constellation,
    code_rate, guard, mode, cell_id, frame, sync) or None if no sync word is found
    (need ~the first ~40 symbols of a frame after sync)."""
    tps = _tps_carriers(X_by_symbol, mode)
    if tps.shape[0] < 40:
        return None
    bits = _differential_bits(tps)                 # bits[j] = s_{j+1}
    for j0 in range(len(bits) - 16):
        for sync_name, sync in (("A", _SYNC_A), ("B", _SYNC_B)):
            if not _match(bits, j0, sync):
                continue
            # need s25..s39 (indices j0+24 .. j0+38) present
            if j0 + 38 >= len(bits):
                continue

            def field(a, b):
                return "".join(str(bits[j0 + i]) for i in range(a - 1, b))   # s_a..s_b (1-indexed)
            const = _CONSTELLATION.get(field(25, 26))
            rate_hp = _CODE_RATE.get(field(30, 32))
            guard = _GUARD.get(field(36, 37))
            tmode = _MODE.get(field(38, 39))
            if const is None or rate_hp is None:
                continue
            cell = None
            if j0 + 46 < len(bits):
                cell = int(field(40, 47), 2)
            return {"constellation": const, "code_rate": rate_hp, "guard": guard,
                    "mode": tmode, "cell_id": cell, "sync_block": sync_name,
                    "frame_offset": j0}
    return None


if __name__ == "__main__":
    fails = 0
    for mode, const_bits, rate_bits, guard_bits, mode_bits, want in (
        ("2k", "10", "010", "01", "00", ("64qam", "3/4", "1/16", "2k")),    # 64-QAM 3/4 GI 1/16 2K
        ("8k", "00", "000", "11", "01", ("qpsk", "1/2", "1/4", "8k")),       # QPSK 1/2 GI 1/4 8K
        ("8k", "01", "100", "10", "01", ("16qam", "7/8", "1/8", "8k")),      # 16-QAM 7/8 GI 1/8 8K
    ):
        # Build a 68-symbol TPS bit word s1..s67 for this config (block 1 → sync A).
        s = [0] * 68                                 # s[0..67] = s0..s67
        sync = _SYNC_A
        for i, c in enumerate(sync):
            s[1 + i] = int(c)
        for i, c in enumerate(const_bits):  s[25 + i] = int(c)
        for i, c in enumerate(rate_bits):   s[30 + i] = int(c)
        for i, c in enumerate(rate_bits):   s[33 + i] = int(c)   # LP = HP (non-hierarchical)
        for i, c in enumerate(guard_bits):  s[36 + i] = int(c)
        for i, c in enumerate(mode_bits):   s[38 + i] = int(c)
        cell_val = 0xA5
        for i in range(8):                  s[40 + i] = (cell_val >> (7 - i)) & 1
        # Synthesise TPS carriers: per-carrier base sign (from PRBS) × cumulative DBPSK.
        wk = dvb_pilots.prbs_wk(mode)
        tps_idx = np.asarray(dvb_pilots._TPS[mode])
        base = (1.0 - 2.0 * wk[tps_idx]).astype(np.float64)
        X_syms = []
        cum = 1.0
        for l in range(68):
            if l >= 1:
                cum *= (1.0 if s[l] == 0 else -1.0)
            carr = dvb_pilots.pilot_values(mode).astype(np.complex64)   # pilots for equaliser
            carr[tps_idx] = (base * cum).astype(np.complex64)
            X_syms.append(dvb_pilots.fft_from_active(carr, mode))
        got = decode_tps(X_syms, mode)
        ok = got and (got["constellation"], got["code_rate"], got["guard"], got["mode"]) == want and got["cell_id"] == cell_val
        print(f"{mode} TPS decode → {got and (got['constellation'], got['code_rate'], got['guard'], got['mode'], hex(got['cell_id']))} "
              f"(want {want} + cell 0xa5): {'PASS' if ok else 'FAIL'}")
        fails += not ok
    print(f"\n{'ALL PASS' if fails == 0 else str(fails)+' FAILED'}")
    raise SystemExit(0 if fails == 0 else 1)
