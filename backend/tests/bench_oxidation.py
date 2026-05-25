# SPDX-License-Identifier: MIT OR Apache-2.0
# Copyright (c) 2026 Ares

"""
Baseline benchmarks for the D4 oxidation candidates (Track D).

Run from `backend/`:   python -m tests.bench_oxidation

This establishes the *pure-Python/numpy* baseline timings for the hot paths the
D4 plan names. They're the measuring stick for the "port to Rust only when it
dominates wall-clock" triggers in ROADMAP.md — re-run after a candidate is ported
to ares_native to confirm the speedup justified the move. Not a pass/fail test
(it prints timings), so it's intentionally not wired into the CI gate.

Candidates ↔ kernels benchmarked here:
  - IQ pipeline / channelizer      → sample covariance over multi-channel IQ
  - MUSIC inner loop               → Hermitian eigendecomposition
  - multi-VFO channelizer / PSD    → an FFT bank
  - ITM inner loop                 → a scalar per-point transcendental loop
"""
from __future__ import annotations

import sys
import time

import numpy as np

sys.path.insert(0, ".")

from app.core import native


def _bench(fn, iters: int) -> float:
    fn()  # warm up
    t0 = time.perf_counter()
    for _ in range(iters):
        fn()
    return (time.perf_counter() - t0) / iters * 1e3  # ms/op


def main() -> int:
    rng = np.random.default_rng(0)
    n_ch, n_samp = 8, 4096
    iq = (rng.standard_normal((n_ch, n_samp)) + 1j * rng.standard_normal((n_ch, n_samp))).astype(np.complex64)
    cov = (iq @ iq.conj().T) / n_samp

    def k_cov():
        return (iq @ iq.conj().T) / n_samp

    def k_eig():
        return np.linalg.eigh(cov)

    def k_fft_bank():
        return [np.fft.fft(iq[c]) for c in range(n_ch)]

    def k_itm_scalar():
        # scalar loop ≈ ITM per-point path before vectorisation — where Rust helps
        acc = 0.0
        for i in range(20000):
            acc += np.sqrt(i + 1.0) * np.log10(i + 2.0)
        return acc

    print("=" * 72)
    print("Ares — D4 oxidation baselines (pure Python / numpy)")
    print("=" * 72)
    print(f"  native extension loaded: {native.HAS_NATIVE}"
          + (f" (v{native.native_version()})" if native.HAS_NATIVE else " (pure-Python fallback)"))
    # parity check of the reference kernel across both paths
    sample = list(range(1000))
    print(f"  sum_squares parity: {native.sum_squares(sample) == float(sum(x * x for x in sample))}")
    print("-" * 72)
    rows = [
        ("covariance  (8ch×4096 IQ)   [IQ pipeline]", _bench(k_cov, 200)),
        ("eigh        (8×8 Hermitian) [MUSIC]", _bench(k_eig, 500)),
        ("fft-bank    (8×4096)        [channelizer]", _bench(k_fft_bank, 200)),
        ("scalar loop (20k transcend) [ITM inner]", _bench(k_itm_scalar, 20)),
    ]
    for name, ms in rows:
        print(f"  {name:44s} {ms:8.3f} ms/op")
    print("-" * 72)
    print("  Trigger: port a kernel to ares_native only when it dominates the")
    print("  per-frame/per-pixel wall-clock under load (see ROADMAP.md D4 table).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
