# SPDX-License-Identifier: MIT OR Apache-2.0
# Copyright (c) 2026 Ares

"""
Parity + speedup checks for the Rust-oxidised hot loops (Track D, D4).

Run from `backend/`:   python -m tests.test_native_parity

Asserts the Rust ports (ares_native) are numerically identical to the pure-Python
originals they replace, then reports the speedup:
  1. diffraction — all five knife-edge models, over random terrain profiles, vs
     the diffraction.py functions (the fallback + ground truth).
  2. ITM _hzns — horizon angles/distances vs IrregularTerrainModel._hzns_core.

If the ares_native wheel isn't built (HAS_NATIVE False), the parity checks are
skipped (the pure-Python path is always correct) — build it with
`scripts/build-native.sh`.
"""
from __future__ import annotations

import random
import sys
import time

sys.path.insert(0, ".")

from app.core import native
from app.core.propagation import diffraction as D
from app.core.propagation.itm_its import IrregularTerrainModel as ITM
from app.core.sdr import dvb_fec, dvb_inner_fec

_TOL = 1e-6

_PY_MODELS = {
    "single_knife_edge": D.single_knife_edge_db,
    "epstein_peterson": D.epstein_peterson_db,
    "bullington": D.bullington_db,
    "giovanelli": D.giovanelli_db,
    "deygout": D.deygout_db,
}


def _profile(seed: int, n: int = 60):
    rng = random.Random(seed)
    xi = 30.0
    elev = []
    base = rng.uniform(0, 200)
    for i in range(n):
        base += rng.uniform(-15, 15)
        hill = 120.0 * (1.0 if rng.random() < 0.15 else 0.0) * rng.random()
        elev.append(max(0.0, base + hill))
    dist = [i * xi for i in range(n)]
    return elev, dist, xi


def test_diffraction_parity():
    if not native.HAS_NATIVE:
        return ("diffraction parity", True, "SKIPPED — ares_native not built")
    worst = 0.0
    worst_where = ""
    for seed in range(40):
        elev, dist, _ = _profile(seed)
        for model, pyfn in _PY_MODELS.items():
            py = pyfn(elev, dist, 10.0, 10.0, 900e6)
            rs = native.diffraction_db(model, elev, dist, 10.0, 10.0, 900e6)
            d = abs(py - rs)
            if d > worst:
                worst, worst_where = d, f"{model}@seed{seed} py={py:.6f} rs={rs:.6f}"
    ok = worst <= _TOL
    return ("diffraction parity (5 models × 40)", ok,
            f"max |Δ| = {worst:.2e} dB" + (f"  ({worst_where})" if not ok else ""))


def test_itm_hzns_parity():
    if not native.HAS_NATIVE:
        return ("ITM _hzns parity", True, "SKIPPED — ares_native not built")
    worst = 0.0
    for seed in range(40):
        elev, _, xi = _profile(seed, n=80)
        np_ = len(elev) - 1
        pfl = [float(np_), xi] + [float(e) for e in elev]
        dist = np_ * xi
        for hg0, hg1, gme in ((10.0, 2.0, 1.5e-7), (30.0, 10.0, 2.0e-7)):
            py = ITM._hzns_core(pfl, hg0, hg1, gme, dist)
            rs = native.itm_hzns(pfl, hg0, hg1, gme, dist)
            worst = max(worst, max(abs(a - b) for a, b in zip(py, rs)))
    ok = worst <= _TOL
    return ("ITM _hzns parity (40×2)", ok, f"max |Δ| = {worst:.2e}")


def test_speedup():
    if not native.HAS_NATIVE:
        return ("speedup", True, "SKIPPED — ares_native not built")
    elev, dist, xi = _profile(7, n=120)
    iters = 3000

    def bench(fn):
        fn()
        t0 = time.perf_counter()
        for _ in range(iters):
            fn()
        return time.perf_counter() - t0

    py_t = bench(lambda: D.deygout_db(elev, dist, 10.0, 10.0, 900e6))
    rs_t = bench(lambda: native.diffraction_db("deygout", elev, dist, 10.0, 10.0, 900e6))
    speed = py_t / rs_t if rs_t else 0.0
    np_ = len(elev) - 1
    pfl = [float(np_), xi] + [float(e) for e in elev]
    pyh = bench(lambda: ITM._hzns_core(pfl, 10.0, 2.0, 1.5e-7, np_ * xi))
    rsh = bench(lambda: native.itm_hzns(pfl, 10.0, 2.0, 1.5e-7, np_ * xi))
    speed_h = pyh / rsh if rsh else 0.0
    return ("speedup (info)", True,
            f"deygout {speed:.1f}× ({py_t/iters*1e3:.3f}→{rs_t/iters*1e3:.3f} ms), "
            f"_hzns {speed_h:.1f}×")


def test_rs_parity():
    if not native.HAS_NATIVE:
        return ("RS(204,188) parity", True, "SKIPPED — ares_native not built")
    import random
    rng = random.Random(0)
    mism = 0
    for _ in range(300):
        data = bytes(rng.randrange(256) for _ in range(188))
        code = bytearray(dvb_fec.rs_encode(data))
        for pos in rng.sample(range(204), rng.randint(0, 8)):
            code[pos] ^= rng.randint(1, 255)
        c = bytes(code)
        if dvb_fec._rs_decode_py(c) != native.rs_decode_204(c):
            mism += 1
    # uncorrectable (> t): both must agree on (None, -1)
    code = bytearray(dvb_fec.rs_encode(bytes(range(188))))
    for pos in range(9):
        code[pos] ^= 0xFF
    over = dvb_fec._rs_decode_py(bytes(code)) == native.rs_decode_204(bytes(code))
    ok = mism == 0 and over
    return ("RS(204,188) parity (300 + >t)", ok, f"mismatches={mism}, >t agrees={over}")


def test_derandomise_parity():
    if not native.HAS_NATIVE:
        return ("derandomise parity", True, "SKIPPED — ares_native not built")
    import random
    rng = random.Random(1)
    ok = True
    for npkt in (1, 8, 9, 20):
        pkts = bytes(rng.randrange(256) for _ in range(npkt * 188))
        if native.dvb_derandomise(pkts) != dvb_fec._derandomise_py(pkts):
            ok = False
    return ("derandomise parity (1/8/9/20 pkts)", ok, "byte-identical" if ok else "MISMATCH")


def test_viterbi_parity():
    if not native.HAS_NATIVE:
        return ("Viterbi parity", True, "SKIPPED — ares_native not built")
    import random
    rng = random.Random(2)
    mism = 0
    for _ in range(20):
        info = [rng.randint(0, 1) for _ in range(500)]
        soft = [1.0 if b == 0 else -1.0 for b in dvb_inner_fec.conv_encode(info)]
        for _ in range(rng.randint(0, 15)):
            soft[rng.randrange(len(soft))] *= -1
        for term in (True, False):
            if dvb_inner_fec.viterbi_decode_py(soft, term) != native.viterbi_decode(soft, term):
                mism += 1
    return ("Viterbi parity (20×2)", mism == 0, f"mismatches={mism}")


def test_dvb_speedup():
    if not native.HAS_NATIVE:
        return ("DVB speedup", True, "SKIPPED — ares_native not built")
    import random
    rng = random.Random(3)
    code = bytearray(dvb_fec.rs_encode(bytes(rng.randrange(256) for _ in range(188))))
    for pos in rng.sample(range(204), 8):
        code[pos] ^= rng.randint(1, 255)
    code = bytes(code)
    soft = [1.0 if b == 0 else -1.0 for b in dvb_inner_fec.conv_encode([rng.randint(0, 1) for _ in range(1000)])]

    def bench(fn, it):
        fn()
        t0 = time.perf_counter()
        for _ in range(it):
            fn()
        return (time.perf_counter() - t0) / it * 1e3

    rs_py = bench(lambda: dvb_fec._rs_decode_py(code), 400)
    rs_rs = bench(lambda: native.rs_decode_204(code), 400)
    v_py = bench(lambda: dvb_inner_fec.viterbi_decode_py(soft, True), 30)
    v_rs = bench(lambda: native.viterbi_decode(soft, True), 30)
    return ("DVB speedup (info)", True,
            f"RS {rs_py/rs_rs:.1f}× ({rs_py:.3f}→{rs_rs:.3f} ms), "
            f"Viterbi {v_py/v_rs:.1f}× ({v_py:.2f}→{v_rs:.2f} ms)")


def main() -> int:
    tests = [test_diffraction_parity, test_itm_hzns_parity,
             test_rs_parity, test_derandomise_parity, test_viterbi_parity,
             test_speedup, test_dvb_speedup]
    passed = 0
    print("=" * 72)
    print(f"Ares — native oxidation parity (HAS_NATIVE={native.HAS_NATIVE})")
    print("=" * 72)
    for fn in tests:
        try:
            name, ok, detail = fn()
        except Exception as e:
            print(f"  ✗ {fn.__name__}  CRASH  {type(e).__name__}: {e}")
            continue
        flag = "✓" if ok else "✗"
        print(f"  {flag} {name:34s}  {detail}")
        if ok:
            passed += 1
    print("-" * 72)
    print(f"  {passed}/{len(tests)} parity checks passed")
    return 0 if passed == len(tests) else 1


if __name__ == "__main__":
    sys.exit(main())
