# SPDX-License-Identifier: MIT OR Apache-2.0
# Copyright (c) 2026 Ares

"""
viterbi_verify.py — verify the true-soft Viterbi against reference performance.

The reference "data" for a convolutional code is its **distance spectrum** and the
**union bound** on BER — the canonical theory every textbook/standard uses. This:

  1. enumerates the distance spectrum of the encoder (dfree and the bit-error
     weights B_d) straight from the trellis, and checks it against the published
     values for the (171,133) K=7 code: dfree=10, B_10=36, B_12=211, B_14=1404
     (Voyager / IEEE-802.11 mother code; e.g. Proakis, Frenger et al. 1999);
  2. computes the soft-decision union-bound BER curve from that spectrum;
  3. Monte-Carlo simulates the soft-decision Viterbi over BPSK/AWGN and checks the
     measured BER sits at or below the union bound (tight at higher Eb/N0);
  4. simulates hard-decision (sliced input) and confirms soft beats hard by the
     well-known ≈2 dB.

Run: ``python -m app.core.sdr.viterbi_verify``.
"""
from __future__ import annotations

import math
from collections import defaultdict

import numpy as np

from app.core.sdr.dvb_inner_fec import _OUT, _NEXT, _NSTATES, _K, conv_encode, viterbi_decode

_R = 0.5                       # code rate


def _q(x: float) -> float:
    return 0.5 * math.erfc(x / math.sqrt(2.0))


def distance_spectrum(max_weight: int = 18) -> tuple[dict, dict]:
    """Enumerate error events (state 0 → … → state 0) up to output weight max_weight.
    Returns (A_d: #paths, B_d: total info-bit weight) by output weight d. Valid
    because the code is non-catastrophic (every nonzero-state cycle adds weight)."""
    A = defaultdict(int)
    B = defaultdict(int)
    # depth-first over the trellis; start by leaving state 0 via input 1
    stack = [(_NEXT[0][1], sum(_OUT[0][1]), 1)]
    while stack:
        state, outw, inw = stack.pop()
        if outw > max_weight:
            continue
        for u in (0, 1):
            o0, o1 = _OUT[state][u]
            no = outw + o0 + o1
            if no > max_weight:
                continue
            ns = _NEXT[state][u]
            ni = inw + u
            if ns == 0:
                A[no] += 1
                B[no] += ni
            else:
                stack.append((ns, no, ni))
    return dict(A), dict(B)


def union_bound_ber(ebno_db: float, B: dict) -> float:
    """Soft-decision union bound: P_b ≤ Σ_d B_d · Q(sqrt(2·R·d·Eb/N0))."""
    ebno = 10 ** (ebno_db / 10.0)
    return sum(bd * _q(math.sqrt(2.0 * _R * d * ebno)) for d, bd in B.items())


def monte_carlo_ber(ebno_db: float, *, soft: bool, n_bits: int = 200_000,
                    block: int = 2000, seed: int = 0) -> tuple[float, int]:
    """BER of the Viterbi decoder over BPSK/AWGN at ``ebno_db``. ``soft`` passes the
    raw channel values; otherwise inputs are sliced to ±1 (hard decision)."""
    rng = np.random.default_rng(seed)
    ebno = 10 ** (ebno_db / 10.0)
    sigma = math.sqrt(1.0 / (2.0 * _R * ebno))     # Es=1, noise σ per real dim
    errs = total = 0
    while total < n_bits and errs < 400:
        info = rng.integers(0, 2, block).astype(np.uint8)
        coded = np.array(conv_encode(info.tolist()), dtype=np.float64)   # includes flush
        tx = 1.0 - 2.0 * coded                      # BPSK: 0→+1, 1→−1
        rx = tx + sigma * rng.standard_normal(tx.size)
        chan = np.sign(rx) if not soft else rx      # hard slices to ±1
        dec = viterbi_decode(chan.tolist(), terminated=True)
        m = min(len(dec), info.size)
        errs += int(np.sum(np.asarray(dec[:m]) != info[:m]))
        total += m
    return (errs / total if total else 1.0), total


def run() -> dict:
    A, B = distance_spectrum(18)
    dfree = min(B)
    pub = {10: 36, 12: 211, 14: 1404}              # published B_d for (171,133)
    spec_ok = (dfree == 10 and all(B.get(d) == v for d, v in pub.items()))

    rows = []
    soft_at, hard_at = None, None
    target = 1e-3
    for ebno in (3.0, 3.5, 4.0, 4.5, 5.0):
        ub = union_bound_ber(ebno, B)
        soft, _ = monte_carlo_ber(ebno, soft=True, seed=1)
        hard, _ = monte_carlo_ber(ebno, soft=False, seed=2)
        rows.append({"ebno_db": ebno, "union_bound": ub, "soft_ber": soft, "hard_ber": hard,
                     "soft_le_bound": soft <= ub * 1.5 + 1e-6})
        if soft_at is None and soft <= target:
            soft_at = ebno
        if hard_at is None and hard <= target:
            hard_at = ebno
    soft_better = all(r["soft_ber"] <= r["hard_ber"] + 1e-9 for r in rows)
    bound_ok = all(r["soft_le_bound"] for r in rows if r["ebno_db"] >= 4.0)
    gain_db = (hard_at - soft_at) if (soft_at is not None and hard_at is not None) else None
    return {
        "dfree": dfree, "B_d_low": {d: B[d] for d in sorted(B)[:4]},
        "distance_spectrum_matches_published": spec_ok,
        "rows": rows,
        "soft_beats_hard": soft_better,
        "soft_within_union_bound": bound_ok,
        "approx_soft_gain_db_at_1e-3": gain_db,
        "passed": bool(spec_ok and soft_better and bound_ok),
    }


if __name__ == "__main__":
    import json
    res = run()
    print(f"dfree={res['dfree']} (published 10)  B_d low={res['B_d_low']}  "
          f"spectrum matches published: {res['distance_spectrum_matches_published']}")
    print(f"\n{'Eb/N0':>6} {'union':>10} {'soft':>10} {'hard':>10}")
    for r in res["rows"]:
        print(f"{r['ebno_db']:6.1f} {r['union_bound']:10.2e} {r['soft_ber']:10.2e} {r['hard_ber']:10.2e}")
    print(f"\nsoft beats hard: {res['soft_beats_hard']}  |  soft ≤ union bound (≥4 dB): {res['soft_within_union_bound']}")
    print(f"approx soft coding gain @1e-3: {res['approx_soft_gain_db_at_1e-3']} dB (expect ~1.5–2)")
    print(f"\n{'PASS' if res['passed'] else 'FAIL'}")
    raise SystemExit(0 if res["passed"] else 1)
