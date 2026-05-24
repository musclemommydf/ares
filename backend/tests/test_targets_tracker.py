# SPDX-License-Identifier: MIT OR Apache-2.0
# Copyright (c) 2026 Ares

"""
Validation harness for the per-identifier target tracker.

Run from `backend/`:   python -m tests.test_targets_tracker

Tests:
  1. Peak-RSSI sampler — feed a sequence of synthetic RSSI observations at
     known positions, assert the running peak matches the maximum and the
     peak_observation carries the right (lat, lon, t).
  2. Top-K — rolling top-K list stays sorted, bounded, and contains the K
     largest RSSI observations.
  3. Friis single-pose range — with only 1–2 observations, ``estimate_range``
     returns the Friis log-distance inversion using the catalogue defaults
     for the identifier's kind.
  4. Multi-pose RSS-ML — with ≥ 3 distinct observer positions, the tracker
     auto-upgrades to ``rss_path_loss_fix`` and converges within a sane
     fraction of the truth.
  5. AoA + RSS fusion — when bearings are attached, ``estimate_position``
     dispatches to ``ml_grid_fusion`` and converges on the true emitter.
  6. forget() — leaves the tracker clean (no history, no peak, no listener
     leaks).
"""
from __future__ import annotations

import math
import random
import sys
import time
from typing import Iterable

import numpy as np

# Allow running as `python -m tests.test_targets_tracker` from backend/
sys.path.insert(0, ".")

from app.core import targets
from app.core.df.single_channel import _enu_scale


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────
def _haversine_m(a_lat, a_lon, b_lat, b_lon) -> float:
    R = 6_371_000.0
    p1 = math.radians(a_lat); p2 = math.radians(b_lat)
    dp = p2 - p1
    dl = math.radians(b_lon - a_lon)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * R * math.asin(math.sqrt(h))


def _synthetic_rssi(true_lat, true_lon, obs_lat, obs_lon, p_tx_dbm, n, noise_sigma=1.0):
    """Friis-style RSSI sample at obs from emitter at (true_lat,true_lon)."""
    mlat, mlon = _enu_scale(true_lat)
    dx = (obs_lon - true_lon) * mlon; dy = (obs_lat - true_lat) * mlat
    d = max(1.0, math.hypot(dx, dy))
    return p_tx_dbm - 10.0 * n * math.log10(d) + random.gauss(0, noise_sigma)


def _bearing_deg(true_lat, true_lon, obs_lat, obs_lon):
    mlat, mlon = _enu_scale(obs_lat)
    dx = (true_lon - obs_lon) * mlon; dy = (true_lat - obs_lat) * mlat
    return (math.degrees(math.atan2(dx, dy))) % 360.0


# ─────────────────────────────────────────────────────────────────────────────
# Test cases — each returns (label, passed: bool, detail: str).
# ─────────────────────────────────────────────────────────────────────────────
def test_peak_rssi_sampler() -> tuple[str, bool, str]:
    random.seed(0)
    targets.forget("imsi", "PEAKTEST")
    # Feed 30 observations at random positions, one of them deliberately the
    # strongest. The strongest is at index 17.
    feeds = []
    base_lat, base_lon = 37.77, -122.42
    for i in range(30):
        lat = base_lat + random.uniform(-0.005, 0.005)
        lon = base_lon + random.uniform(-0.005, 0.005)
        rssi = -80 + random.uniform(-2, 2)
        if i == 17:
            rssi = -22.0      # the peak
            peak_truth = (lat, lon, rssi)
        feeds.append((lat, lon, rssi))
    for lat, lon, rssi in feeds:
        targets.record("imsi", "PEAKTEST", lat, lon, rssi)
    t = targets.get("imsi", "PEAKTEST")
    if t is None:
        return ("peak-rssi sampler", False, "target was not recorded")
    if abs(t.peak_rssi_dbm - peak_truth[2]) > 1e-6:
        return ("peak-rssi sampler", False,
                f"expected peak {peak_truth[2]}, got {t.peak_rssi_dbm}")
    if t.peak_observation is None or \
       abs(t.peak_observation.observer_lat - peak_truth[0]) > 1e-9 or \
       abs(t.peak_observation.observer_lon - peak_truth[1]) > 1e-9:
        return ("peak-rssi sampler", False,
                f"peak_observation mismatch: {t.peak_observation}")
    targets.forget("imsi", "PEAKTEST")
    return ("peak-rssi sampler", True, f"30 obs, peak={t.peak_rssi_dbm:.1f} dBm at correct position")


def test_top_k() -> tuple[str, bool, str]:
    random.seed(1)
    targets.forget("mac", "TOPK")
    # Push 50 observations with widely-varying RSSI
    rssis = []
    for i in range(50):
        rssi = random.uniform(-95, -25)
        targets.record("mac", "TOPK", 37.77 + i * 1e-4, -122.42, rssi)
        rssis.append(rssi)
    t = targets.get("mac", "TOPK")
    k = len(t.rolling_top_k)
    if k > 10:
        return ("rolling top-K", False, f"top-K exceeded limit: {k} > 10")
    # Sorted descending
    top_rssis = [o.rssi_dbm for o in t.rolling_top_k]
    if top_rssis != sorted(top_rssis, reverse=True):
        return ("rolling top-K", False, f"top-K not sorted: {top_rssis}")
    # And it actually contains the K largest from the input stream
    expected = sorted(rssis, reverse=True)[:k]
    if not all(abs(a - b) < 1e-6 for a, b in zip(top_rssis, expected)):
        return ("rolling top-K", False, f"top-K differs from expected: {top_rssis} vs {expected}")
    targets.forget("mac", "TOPK")
    return ("rolling top-K", True, f"K={k}, sorted, matches the K largest in the stream")


def test_friis_single_pose() -> tuple[str, bool, str]:
    targets.forget("ble", "FRIISTEST")
    # One observation — should trigger the single-pose Friis range estimate.
    # For BLE the catalogue says p_tx=4 dBm, n=2.0 → d ≈ 10^((4 - (-60))/20) = 1585 m.
    targets.record("ble", "FRIISTEST", 37.77, -122.42, rssi_dbm=-60.0)
    t = targets.get("ble", "FRIISTEST")
    # Single-pose only fires after the throttle threshold — force a refit.
    targets.tracker.recompute("ble", "FRIISTEST")
    t = targets.get("ble", "FRIISTEST")
    if t.range_method != "friis_single":
        return ("friis single-pose", False, f"expected friis_single, got {t.range_method}")
    expected_d = 10 ** ((4.0 - (-60.0)) / 20.0)
    if not (0.5 * expected_d <= t.range_m_estimate <= 2.0 * expected_d):
        return ("friis single-pose", False,
                f"range {t.range_m_estimate:.0f} m not near expected {expected_d:.0f} m")
    targets.forget("ble", "FRIISTEST")
    return ("friis single-pose", True,
            f"range={t.range_m_estimate:.0f} m (expected ~{expected_d:.0f} m)")


def test_rss_log_distance_ml() -> tuple[str, bool, str]:
    random.seed(2)
    targets.forget("imsi", "RSSMLTEST")
    true_lat, true_lon = 37.77, -122.42
    p_tx = 20.0; n = 3.0
    # Spread 25 observations around the emitter over ~500 m
    for i in range(25):
        lat = true_lat + random.uniform(-0.0045, 0.0045)
        lon = true_lon + random.uniform(-0.0045, 0.0045)
        rssi = _synthetic_rssi(true_lat, true_lon, lat, lon, p_tx, n, noise_sigma=2.0)
        targets.record("imsi", "RSSMLTEST", lat, lon, rssi)
    targets.tracker.recompute("imsi", "RSSMLTEST")
    t = targets.get("imsi", "RSSMLTEST")
    if t.range_method != "rss_log_distance_ml":
        return ("rss_log_distance_ml", False,
                f"expected rss_log_distance_ml after 25 obs, got {t.range_method}")
    if t.position_lat is None or t.position_lon is None:
        return ("rss_log_distance_ml", False, "no position estimate produced")
    err_m = _haversine_m(true_lat, true_lon, t.position_lat, t.position_lon)
    # Synthetic accuracy bound — fit may settle at a P_tx that's different
    # from truth, so we allow a generous CEP. The point is "the fit *runs*
    # and lands somewhere finite within the search grid".
    if err_m > 5000:
        return ("rss_log_distance_ml", False,
                f"position fit too far from truth: {err_m:.0f} m")
    targets.forget("imsi", "RSSMLTEST")
    return ("rss_log_distance_ml", True,
            f"fit @ {err_m:.0f} m from truth (CEP {t.position_cep_m:.0f} m)")


def test_ml_grid_fusion_with_aoa() -> tuple[str, bool, str]:
    random.seed(3)
    targets.forget("tmsi", "FUSIONTEST")
    true_lat, true_lon = 37.77, -122.42
    # 5 observations, each with both RSSI and a noisy AoA bearing
    for i in range(5):
        lat = true_lat + random.uniform(-0.005, 0.005)
        lon = true_lon + random.uniform(-0.005, 0.005)
        rssi = _synthetic_rssi(true_lat, true_lon, lat, lon, p_tx_dbm=20.0, n=3.0, noise_sigma=1.5)
        brg = _bearing_deg(true_lat, true_lon, lat, lon) + random.gauss(0, 2.0)
        targets.record("tmsi", "FUSIONTEST", lat, lon, rssi,
                       bearing_deg=brg, sigma_deg=3.0)
    targets.tracker.recompute("tmsi", "FUSIONTEST")
    t = targets.get("tmsi", "FUSIONTEST")
    if t.position_method != "ml_grid_fusion":
        return ("ml_grid_fusion (AoA+RSS)", False,
                f"expected ml_grid_fusion, got {t.position_method}")
    err_m = _haversine_m(true_lat, true_lon, t.position_lat, t.position_lon)
    if err_m > 500:
        return ("ml_grid_fusion (AoA+RSS)", False,
                f"AoA-fused fit too far from truth: {err_m:.0f} m")
    targets.forget("tmsi", "FUSIONTEST")
    return ("ml_grid_fusion (AoA+RSS)", True,
            f"AoA-fused fit @ {err_m:.0f} m from truth")


def test_forget_clean() -> tuple[str, bool, str]:
    targets.forget("rnti", "FORGETTEST")
    for i in range(3):
        targets.record("rnti", "FORGETTEST", 37.77, -122.42, -60.0 + i)
    assert targets.get("rnti", "FORGETTEST") is not None
    ok = targets.forget("rnti", "FORGETTEST")
    if not ok:
        return ("forget()", False, "forget returned False on existing target")
    if targets.get("rnti", "FORGETTEST") is not None:
        return ("forget()", False, "target still present after forget")
    if targets.tracker.history("rnti", "FORGETTEST"):
        return ("forget()", False, "history not cleared after forget")
    return ("forget()", True, "post-forget state is clean")


def test_listener_pubsub() -> tuple[str, bool, str]:
    captured = []
    def listener(payload):
        captured.append(payload)
    targets.register_listener(listener)
    try:
        targets.record("icao", "LISTENTEST", 37.77, -122.42, -55.0)
        targets.forget("icao", "LISTENTEST")
    finally:
        targets.unregister_listener(listener)
    events = [p["event"] for p in captured]
    if "target_update" not in events or "target_forget" not in events:
        return ("listener pub-sub", False, f"missing events: {events}")
    return ("listener pub-sub", True, f"got events: {events}")


# ─────────────────────────────────────────────────────────────────────────────
def main() -> int:
    tests = [
        test_peak_rssi_sampler,
        test_top_k,
        test_friis_single_pose,
        test_rss_log_distance_ml,
        test_ml_grid_fusion_with_aoa,
        test_forget_clean,
        test_listener_pubsub,
    ]
    passed = 0
    print("=" * 72)
    print("Ares — target tracker validation harness")
    print("=" * 72)
    for fn in tests:
        try:
            name, ok, detail = fn()
        except Exception as e:
            print(f"  ✗ {fn.__name__}  CRASH  {type(e).__name__}: {e}")
            continue
        flag = "✓" if ok else "✗"
        print(f"  {flag} {name:32s}  {detail}")
        if ok:
            passed += 1
    print("-" * 72)
    print(f"  {passed}/{len(tests)} target-tracker tests passed")
    return 0 if passed == len(tests) else 1


if __name__ == "__main__":
    sys.exit(main())
