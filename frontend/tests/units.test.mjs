// SPDX-License-Identifier: MIT OR Apache-2.0
// Copyright (c) 2026 Ares

/**
 * Node-native unit tests for the frontend's pure helper modules — runnable with
 * zero extra dependencies:  `node --test frontend/tests/`  (Node ≥ 18).
 *
 * Covers the bits that have no DOM/React: the polar antenna patterns
 * (`utils/polarPatterns.js`) and the Line-of-Bearing geolocation maths
 * (`components/Geolocation/LoBUtils.js`). Component-level tests (rendering, the
 * map panels, the DF/Chat panels) would want jsdom + a test runner like Vitest —
 * that's a follow-up; these guard the maths the UI depends on.
 */
import test from 'node:test'
import assert from 'node:assert/strict'

import {
  polarPatternGainDb, computePatternBeamwidths, POLAR_PATTERNS,
} from '../src/utils/polarPatterns.js'
import {
  estimateDistance, initialBearing, destinationPoint, intersectBearings, groupLoBsByFrequency,
} from '../src/components/Geolocation/LoBUtils.js'

// ── polar patterns ───────────────────────────────────────────────────────────
test('omni pattern is flat 0 dB everywhere', () => {
  for (const a of [0, 45, 90, 180, 270]) assert.equal(polarPatternGainDb('omni', a), 0)
})

test('directional patterns peak at boresight, fall off the bore', () => {
  for (const id of ['cardioid', 'sector_90', 'yagi_9', 'parabolic_medium']) {
    assert.ok(POLAR_PATTERNS[id], `pattern ${id} exists`)
    const peak = polarPatternGainDb(id, 0)
    assert.equal(peak, 0, `${id}: 0 dB rel at boresight`)
    assert.ok(polarPatternGainDb(id, 90) < -1, `${id}: well down at 90° off bore`)
    assert.ok(polarPatternGainDb(id, 180) <= polarPatternGainDb(id, 0), `${id}: rear ≤ front`)
  }
})

test('cardioid has a deep null behind boresight', () => {
  assert.ok(polarPatternGainDb('cardioid', 180) < -25, 'cardioid 180° is a deep null')
})

test('beamwidths: omni undefined; a 90° sector ≈ 90°; a long Yagi narrower than a short one', () => {
  assert.equal(computePatternBeamwidths('omni').hpbw3, null)
  const s90 = computePatternBeamwidths('sector_90').hpbw3
  assert.ok(Math.abs(s90 - 90) < 25, `sector_90 HPBW ≈ 90 (got ${s90})`)
  const y3 = computePatternBeamwidths('yagi_3').hpbw3
  const y15 = computePatternBeamwidths('yagi_15').hpbw3
  assert.ok(y15 < y3, `yagi_15 (${y15}°) narrower than yagi_3 (${y3}°)`)
})

// ── LoB geolocation maths ────────────────────────────────────────────────────
test('estimateDistance: stronger RSSI ⇒ closer; clamps to a sane range', () => {
  const f = 433.92e6, p = 30
  const near = estimateDistance(-50, f, p, { environment: 'suburban' })
  const far = estimateDistance(-90, f, p, { environment: 'suburban' })
  assert.ok(near < far, `${near} m (−50 dBm) < ${far} m (−90 dBm)`)
  assert.ok(near >= 50 && far <= 200_000, 'within [50 m, 200 km]')
  // a worse environment ⇒ shorter inferred range for the same RSSI
  assert.ok(estimateDistance(-80, f, p, { environment: 'urban' }) < estimateDistance(-80, f, p, { environment: 'open' }))
})

test('initialBearing / destinationPoint round-trip', () => {
  const a = { lat: 51.5, lon: -0.12 }
  for (const [b, lon] of [[80, -0.12], [200, -0.05], [355, 0.02]]) {
    const [dlat, dlon] = destinationPoint(a.lat, a.lon, b, 5000)
    const back = initialBearing(a.lat, a.lon, dlat, dlon)
    assert.ok(Math.abs(((back - b + 540) % 360) - 180) < 1.0, `bearing round-trips (${b}° → ${back?.toFixed(2)}°)`)
    void lon
  }
})

test('intersectBearings: two rays toward a common point cross near it', () => {
  const emitter = { lat: 51.50, lon: -0.12 }
  const o1 = { lat: 51.55, lon: -0.20 }, o2 = { lat: 51.45, lon: -0.05 }
  const az1 = initialBearing(o1.lat, o1.lon, emitter.lat, emitter.lon)
  const az2 = initialBearing(o2.lat, o2.lon, emitter.lat, emitter.lon)
  const x = intersectBearings(o1.lat, o1.lon, az1, o2.lat, o2.lon, az2)
  assert.ok(x, 'intersection found')
  const err = Math.hypot((x[0] - emitter.lat) * 111320, (x[1] - emitter.lon) * 111320 * Math.cos(emitter.lat * Math.PI / 180))
  assert.ok(err < 50, `intersection within 50 m of the emitter (got ${err.toFixed(0)} m)`)
  // parallel rays ⇒ no intersection
  assert.equal(intersectBearings(51.5, -0.2, 90, 51.6, -0.2, 90), null)
})

test('groupLoBsByFrequency: same freq + same device id group together; different freq splits', () => {
  const lobs = [
    { id: 'a', frequency_hz: 433.92e6, device_id: 'X' },
    { id: 'b', frequency_hz: 433.925e6, device_id: 'X' },   // within tolerance
    { id: 'c', frequency_hz: 446.0e6, device_id: 'X' },     // far away → its own group
    { id: 'd', frequency_hz: 433.92e6, device_id: 'Y' },    // different device id
  ]
  const groups = groupLoBsByFrequency(lobs)
  // a+b group; c alone; d alone (or grouped with a/b depending on the impl) — assert a&b are together
  const g = groups.find(gr => gr.lobs.some(l => l.id === 'a'))
  assert.ok(g.lobs.some(l => l.id === 'b'), 'a and b (within freq tolerance) are in the same group')
  assert.ok(!g.lobs.some(l => l.id === 'c'), 'c (446 MHz) is not in the 433.9 MHz group')
})
