// SPDX-License-Identifier: MIT OR Apache-2.0
// Copyright (c) 2026 Ares

/**
 * The "model = auto" resolver: given a transmitter + propagation config (and the
 * current receiver and active tab for context), pick a concrete propagation model
 * — terrain-aware ITM by default, P.528 for airborne air-to-ground links, P.1546
 * for long-range HF/VHF, COST-231 Hata in the cellular bands, two-ray / FSPL up high,
 * radar for the radar tab. App calls `makeResolveModelFast(rx, activeTab)` once per
 * render to get a `resolveModelFast(tx, propagation)` it can apply to any TX/prop pair.
 */
export function makeResolveModelFast(rx, activeTab) {
  return function resolveModelFast(txConfig, propConfig) {
    if (propConfig.model !== 'auto') return propConfig.model
    const freqMhz = txConfig.frequency_hz / 1e6
    const radius = propConfig.radius_km
    const isAirborne = txConfig.height_m > 30 || txConfig.altitude_m > 150 || rx.altitude_m > 150
    if (activeTab === 'radar') return 'radar'
    if (isAirborne && freqMhz >= 100 && freqMhz <= 15500) return 'itu_p528'
    if (freqMhz < 30) return radius > 200 ? 'itu_p1546' : 'itm'
    if (freqMhz < 1500) return 'itm'
    if (freqMhz < 2000) return 'cost231_hata'
    if (freqMhz >= 6000) return radius > 5 ? 'two_ray' : 'fspl'
    if (freqMhz >= 3000) return 'fspl'
    return 'itm'
  }
}
