// SPDX-License-Identifier: MIT OR Apache-2.0
// Copyright (c) 2026 Ares

/**
 * Line of Bearing (LoB) geolocation utilities.
 * Provides FSPL-based distance estimation, great-circle bearing intersection,
 * grouping logic, and Circular Area of Probability (CAP) ellipse generation.
 */

import { computePatternBeamwidths } from '../../utils/polarPatterns.js'

/**
 * Environment presets for LoB distance estimation.
 * extra_db = additional path loss beyond free space (accounts for terrain,
 * clutter, multipath, and typical real-world obstructions for that environment).
 */
export const ENVIRONMENT_PRESETS = [
  { id: 'open',        label: 'Open / LOS',       extra_db:  5, n: 2.5 },
  { id: 'rural',       label: 'Rural',             extra_db: 10, n: 2.8 },
  { id: 'suburban',    label: 'Suburban',          extra_db: 18, n: 3.2 },
  { id: 'urban',       label: 'Urban',             extra_db: 28, n: 3.8 },
  { id: 'forest',      label: 'Forest / Jungle',   extra_db: 25, n: 3.5 },
  { id: 'mountainous', label: 'Mountainous',       extra_db: 20, n: 3.0 },
]

/**
 * Estimate emitter range from RSSI using a log-distance path loss model.
 *
 * PL(d) = FSPL(1 km, f) + extra_db + 10·n·log10(d_km)
 *
 * extra_db = near-field offset above free space at the 1 km reference distance.
 * n        = path loss exponent (2.0 = free space; 3–4 = built-up environments).
 *
 * Using n > 2 (environment-specific) instead of pure FSPL (n = 2) prevents the
 * systematic over-estimation of range that occurs when free-space slope is applied
 * to real-world cluttered environments.
 */
export function estimateDistance(rssi_dbm, freq_hz, tx_power_dbm = 30, opts = {}) {
  const { environment = 'suburban', clutter_height_m = 0 } = opts
  const preset = ENVIRONMENT_PRESETS.find(e => e.id === environment) ?? ENVIRONMENT_PRESETS[2]

  const path_loss_db = tx_power_dbm - rssi_dbm
  const freq_mhz = freq_hz / 1e6
  if (freq_mhz <= 0 || !isFinite(path_loss_db)) return 2000

  // Clutter adds ~0.4 dB per metre of obstruction height (simplified ITU-R P.833)
  const clutter_db = Math.max(0, clutter_height_m) * 0.4
  const effective_loss = path_loss_db - preset.extra_db - clutter_db

  // Solve PL(d) = FSPL(1 km) + 10·n·log10(d_km) for d_km
  const fspl_1km = 32.45 + 20 * Math.log10(freq_mhz)
  const d_km = Math.pow(10, (effective_loss - fspl_1km) / (10 * preset.n))
  return Math.max(100, Math.min(d_km * 1000, 150_000))
}

/** Initial bearing (°true N, [0, 360)) along the great circle from (lat1,lon1)
 *  to (lat2,lon2). Returns NaN if the two points coincide. */
export function initialBearing(lat1, lon1, lat2, lon2) {
  const φ1 = (lat1 * Math.PI) / 180
  const φ2 = (lat2 * Math.PI) / 180
  const Δλ = ((lon2 - lon1) * Math.PI) / 180
  const y = Math.sin(Δλ) * Math.cos(φ2)
  const x = Math.cos(φ1) * Math.sin(φ2) - Math.sin(φ1) * Math.cos(φ2) * Math.cos(Δλ)
  if (y === 0 && x === 0) return NaN
  return ((Math.atan2(y, x) * 180) / Math.PI + 360) % 360
}

/** Compute destination point (Vincenty simplified) given start, bearing (°true N), distance (m).
 *  Returns [lat, lon]. */
export function destinationPoint(lat, lon, bearing_deg, dist_m) {
  const R = 6_371_000
  const d = dist_m / R
  const θ = (bearing_deg * Math.PI) / 180
  const φ1 = (lat * Math.PI) / 180
  const λ1 = (lon * Math.PI) / 180
  const φ2 = Math.asin(
    Math.sin(φ1) * Math.cos(d) + Math.cos(φ1) * Math.sin(d) * Math.cos(θ),
  )
  const λ2 =
    λ1 +
    Math.atan2(
      Math.sin(θ) * Math.sin(d) * Math.cos(φ1),
      Math.cos(d) - Math.sin(φ1) * Math.sin(φ2),
    )
  return [(φ2 * 180) / Math.PI, (((λ2 * 180) / Math.PI) + 540) % 360 - 180]
}

/**
 * Find intersection of two bearing lines using flat-earth ENU projection.
 * Valid for separations < ~300 km.
 * Returns [lat, lon] or null when lines are parallel or intersection is behind both observers.
 */
export function intersectBearings(lat1, lon1, az1_deg, lat2, lon2, az2_deg) {
  const toRad = d => (d * Math.PI) / 180
  const midLat = (lat1 + lat2) / 2
  const mpdLat = 111_320
  const mpdLon = 111_320 * Math.cos(toRad(midLat))

  const az1 = toRad(az1_deg)
  const az2 = toRad(az2_deg)
  // Unit direction vectors [east, north]
  const d1 = [Math.sin(az1), Math.cos(az1)]
  const d2 = [Math.sin(az2), Math.cos(az2)]

  // Offset P2 from P1 in metres (ENU)
  const dx = (lon2 - lon1) * mpdLon
  const dy = (lat2 - lat1) * mpdLat

  // Solve: P1 + t1·d1 = P2 + t2·d2
  const det = d1[0] * (-d2[1]) - d1[1] * (-d2[0])
  if (Math.abs(det) < 1e-8) return null // parallel

  const t1 = (dx * (-d2[1]) - dy * (-d2[0])) / det
  const t2 = (d1[0] * dy - d1[1] * dx) / det
  if (t1 < -200 || t2 < -200) return null // behind observers (200 m tolerance)

  return [
    lat1 + (t1 * d1[1]) / mpdLat,
    lon1 + (t1 * d1[0]) / mpdLon,
  ]
}

/** 25 kHz tolerance — LoBs within this are considered to be observing the same emitter. */
const FREQ_TOLERANCE_HZ = 25_000

/**
 * Stable string key for a LoB group — used for per-group CAP visibility tracking.
 * Groups are keyed by frequency + device identity (if set).
 */
export function lobGroupKey(group) {
  const dev = group.device_id ? `_${group.device_type || 'dev'}:${group.device_id}` : ''
  return `${group.frequency_hz}${dev}`
}

/**
 * Group LoBs by frequency (within tolerance) AND device identity.
 * LoBs with the same non-empty device_id AND matching frequency → same group.
 * LoBs with no device_id AND matching frequency → same group (legacy behaviour).
 * Returns [{frequency_hz, device_id, device_type, lobs}].
 */
export function groupLoBsByFrequency(lobs) {
  const groups = []
  const assigned = new Set()
  for (let i = 0; i < lobs.length; i++) {
    if (assigned.has(i)) continue
    const group = [lobs[i]]
    assigned.add(i)
    const devA = lobs[i].device_id || ''
    for (let j = i + 1; j < lobs.length; j++) {
      if (assigned.has(j)) continue
      const freqMatch = Math.abs(lobs[i].frequency_hz - lobs[j].frequency_hz) <= FREQ_TOLERANCE_HZ
      const devB = lobs[j].device_id || ''
      const deviceMatch = devA === devB  // empty matches empty; specific IDs must match exactly
      if (freqMatch && deviceMatch) {
        group.push(lobs[j])
        assigned.add(j)
      }
    }
    groups.push({
      frequency_hz: lobs[i].frequency_hz,
      device_id: lobs[i].device_id || '',
      device_type: lobs[i].device_type || '',
      lobs: group,
    })
  }
  return groups
}

/** Compute all pairwise intersections for a LoB group. Returns [{lat, lon, weight}]. */
export function computeGroupIntersections(group) {
  const { lobs } = group
  const intersections = []
  for (let i = 0; i < lobs.length; i++) {
    for (let j = i + 1; j < lobs.length; j++) {
      const pt = intersectBearings(
        lobs[i].lat, lobs[i].lon, lobs[i].azimuth_deg,
        lobs[j].lat, lobs[j].lon, lobs[j].azimuth_deg,
      )
      if (pt) {
        intersections.push({
          lat: pt[0],
          lon: pt[1],
          weight: (lobs[i].confidence_pct + lobs[j].confidence_pct) / 200,
        })
      }
    }
  }
  return intersections
}

/** Confidence-weighted centroid of intersection cloud. Returns {lat, lon} or null. */
export function computeCentroid(intersections) {
  if (intersections.length === 0) return null
  const totalW = intersections.reduce((s, p) => s + p.weight, 0)
  if (totalW === 0) return null
  return {
    lat: intersections.reduce((s, p) => s + p.lat * p.weight, 0) / totalW,
    lon: intersections.reduce((s, p) => s + p.lon * p.weight, 0) / totalW,
  }
}

/** Generate an ellipse as a closed GeoJSON Polygon.
 *  semiMajorM / semiMinorM in metres; rotDeg = major-axis bearing from north. */
function generateEllipseGeoJSON(centerLat, centerLon, semiMajorM, semiMinorM, rotDeg) {
  const N = 72
  const rot = (rotDeg * Math.PI) / 180
  const mpdLat = 111_320
  const mpdLon = 111_320 * Math.cos((centerLat * Math.PI) / 180)
  const coords = []
  for (let i = 0; i <= N; i++) {
    const θ = (2 * Math.PI * i) / N
    const xL = semiMajorM * Math.cos(θ)
    const yL = semiMinorM * Math.sin(θ)
    // Rotate into ENU (east = x, north = y)
    const xe = xL * Math.sin(rot) + yL * Math.cos(rot)
    const yn = xL * Math.cos(rot) - yL * Math.sin(rot)
    coords.push([centerLon + xe / mpdLon, centerLat + yn / mpdLat])
  }
  return {
    type: 'Feature',
    geometry: { type: 'Polygon', coordinates: [coords] },
    properties: {
      type: 'cap_ellipse',
      semiMajorM: Math.round(semiMajorM),
      semiMinorM: Math.round(semiMinorM),
    },
  }
}

/**
 * Resolve a receiver's effective full -3 dB beamwidth (degrees) from a
 * receiver_accuracy config. Returns null when the receiver provides no
 * directional information (e.g. omni pattern).
 */
export function effectiveRxHPBW(receiverAccuracy) {
  const cfg = receiverAccuracy
  if (!cfg) return null
  switch (cfg.mode) {
    case 'pattern': {
      const { hpbw3 } = computePatternBeamwidths(cfg.pattern_id)
      return isFinite(hpbw3) && hpbw3 > 0 ? hpbw3 : null
    }
    case 'gain': {
      const g = parseFloat(cfg.gain_dbi)
      if (!isFinite(g)) return null
      const gLin = Math.pow(10, g / 10)
      if (gLin <= 0) return null
      // Approximate circular HPBW from directivity: θ ≈ √(41253 / G_linear)
      const hpbw = Math.sqrt(41253 / gLin)
      return isFinite(hpbw) && hpbw > 0 ? hpbw : null
    }
    case 'manual':
    default: {
      const deg = parseFloat(cfg.hpbw_deg)
      return isFinite(deg) && deg > 0 ? deg : null
    }
  }
}

/**
 * Compute CAP ellipse GeoJSON for a LoB group and its pre-computed intersections.
 *
 * Semi-major axis = RMS spread of intersection cloud + lateral spread from
 * angular uncertainty (mean confidence + receiver beamwidth combined in
 * quadrature). Semi-minor axis ≈ 45 % of semi-major (elongated along mean bearing).
 * Returns GeoJSON Feature or null.
 */
export function computeCAPEllipse(group, intersections, algorithm = null) {
  const centroid = computeCentroid(intersections)
  if (!centroid) return null

  const { lobs } = group
  const mpdLat = 111_320
  const mpdLon = 111_320 * Math.cos((centroid.lat * Math.PI) / 180)

  // Radial distance of each pairwise intersection from the centroid.
  const dists = intersections.map(p => {
    const dx = (p.lon - centroid.lon) * mpdLon
    const dy = (p.lat - centroid.lat) * mpdLat
    return Math.sqrt(dx * dx + dy * dy)
  })

  // Reject outliers: pairs of near-parallel bearings can intersect arbitrarily
  // far away and would otherwise dominate the RMS. Keep only intersections
  // within ~3× the median distance — robust against a small number of huge outliers.
  const median = (() => {
    if (dists.length === 0) return 0
    const sorted = [...dists].sort((a, b) => a - b)
    const mid = Math.floor(sorted.length / 2)
    return sorted.length % 2 === 0 ? (sorted[mid - 1] + sorted[mid]) / 2 : sorted[mid]
  })()
  const cleanDists = dists.filter(d => d <= 3 * median + 50)
  const rmsClean =
    cleanDists.length > 0
      ? Math.sqrt(cleanDists.reduce((s, d) => s + d * d, 0) / cleanDists.length)
      : 0

  // Per-LoB angular variance (deg²) from operator reading error + realized
  // receiver direction-finding resolution. Confidence drives BOTH terms:
  // the receiver-beamwidth contribution falls off as (1 − conf)², so a
  // confidently-peaked LoB effectively bypasses the receiver beamwidth floor.
  const rxHPBW = effectiveRxHPBW(algorithm?.receiver_accuracy)
  const halfHPBW = rxHPBW != null ? rxHPBW / 2 : 0
  const avgDist = lobs.reduce((s, l) => s + l.estimated_distance_m, 0) / lobs.length
  const perLobVar = lobs.map(l => {
    const conf = Math.max(0, Math.min(100, l.confidence_pct ?? 80))
    const peakingFactor = 1 - conf / 100
    const confErr = Math.max(0.5, peakingFactor * 10)        // operator reading σ (deg)
    const rxErr = halfHPBW * peakingFactor * peakingFactor    // realized DF σ (deg)
    return confErr * confErr + rxErr * rxErr
  })
  // Inverse-variance-weighted centroid angular σ (handles mixed-confidence LoBs).
  const invVarSum = perLobVar.reduce((s, v) => s + 1 / Math.max(1e-6, v), 0)
  const centroidAngularDeg = invVarSum > 0 ? Math.sqrt(1 / invVarSum) : 0
  const lateralSE = avgDist * Math.tan((centroidAngularDeg * Math.PI) / 180)

  // Centroid SE from observed (cleaned) scatter — meaningful for N ≥ 3.
  const N = Math.max(1, lobs.length)
  const sqrtN = Math.sqrt(N)
  const observedSE = N >= 3 ? rmsClean / sqrtN : 0

  // Combine model and observation in quadrature. With confidence-driven angular
  // variance, well-pointed LoBs collapse the model term naturally — no heuristic
  // 1/N² needed.
  const semiMajor = Math.max(
    Math.sqrt(lateralSE * lateralSE + observedSE * observedSE),
    25,
  )
  const semiMinor = Math.max(semiMajor * 0.45, 15)

  // Major axis aligned with mean bearing
  const meanAz = lobs.reduce((s, l) => s + l.azimuth_deg, 0) / lobs.length

  return generateEllipseGeoJSON(centroid.lat, centroid.lon, semiMajor, semiMinor, meanAz)
}

// ── LoB length-rendering algorithms ───────────────────────────────────────────
//
// Three operator-selectable algorithms for how long each bearing line is drawn:
//
//   estimated    — current behaviour: use lob.estimated_distance_m (RSSI-derived
//                  FSPL or terrain-aware ITM range estimate).
//   intersection — extend each line until it meets another LoB in the same
//                  frequency/device group; if it never crosses one, draw it long.
//   step         — map RSSI to length via exponential interpolation between a
//                  weak-signal anchor (long range), an optional middle anchor,
//                  and a strong-signal anchor (short range).
//   fixed        — every LoB drawn at one operator-set length.
//

/** Length used for an "intersection" LoB that never crosses another (~1000 km). */
export const INTERSECTION_FALLBACK_M = 1_000_000

export const DEFAULT_LOB_ALGORITHM = {
  type: 'step',
  terrain_aware: false,  // when true, override algorithm with each LoB's terrain-derived estimated_distance_m
  // Receiver direction-finding accuracy → contributes to CAP angular uncertainty.
  // Wider receiver beam = larger CAP. mode=='manual' uses hpbw_deg directly;
  // 'pattern' resolves HPBW from a polar-pattern id; 'gain' converts dBi → HPBW
  // assuming a circular beam (θ ≈ √(41253 / 10^(dBi/10))).
  receiver_accuracy: {
    mode: 'pattern',           // 'manual' | 'pattern' | 'gain'
    hpbw_deg: 30,
    pattern_id: 'cardioid',
    gain_dbi: 6,
  },
  step: {
    min_rssi_dbm: -120,
    min_rssi_distance_m: 50_000,   // weakest signal → longest line
    min_rssi_distance_unit: 'km',
    max_rssi_dbm: -30,
    max_rssi_distance_m: 500,      // strongest signal → shortest line
    max_rssi_distance_unit: 'km',
    middle_enabled: false,
    middle_rssi_dbm: -75,
    middle_distance_m: 5_000,
    middle_distance_unit: 'km',
    interpolation: 'exponential',  // 'exponential' | 'linear'
  },
  fixed: {
    length_m: 10_000,
    length_unit: 'km',
  },
}

/**
 * Step algorithm: map RSSI → distance using piecewise exponential interpolation
 * between operator-set anchors.
 *
 * Two-anchor mode uses (min_rssi, min_distance) ↔ (max_rssi, max_distance).
 * Three-anchor mode (middle_enabled) splits into two segments at the middle anchor.
 *
 * Within each segment d(r) = d1 · exp(k · (r − r1)),
 * where k = ln(d2 / d1) / (r2 − r1). RSSI outside the configured bounds is
 * clamped, so a stronger-than-max signal still draws at min_distance, and a
 * weaker-than-min signal still draws at max_distance.
 */
export function computeStepDistance(rssi_dbm, cfg) {
  const c = { ...DEFAULT_LOB_ALGORITHM.step, ...(cfg || {}) }
  const rMin = Math.min(c.min_rssi_dbm, c.max_rssi_dbm)
  const rMax = Math.max(c.min_rssi_dbm, c.max_rssi_dbm)
  // Pair RSSI bound with its configured distance regardless of which is "min"/"max" numerically
  const dAtRMin = c.min_rssi_dbm <= c.max_rssi_dbm ? c.min_rssi_distance_m : c.max_rssi_distance_m
  const dAtRMax = c.min_rssi_dbm <= c.max_rssi_dbm ? c.max_rssi_distance_m : c.min_rssi_distance_m

  if (!isFinite(rssi_dbm)) return dAtRMax
  if (rssi_dbm <= rMin) return dAtRMin
  if (rssi_dbm >= rMax) return dAtRMax

  let r1, d1, r2, d2
  const midOk = c.middle_enabled
    && isFinite(c.middle_rssi_dbm)
    && c.middle_rssi_dbm > rMin
    && c.middle_rssi_dbm < rMax
    && c.middle_distance_m > 0
  if (midOk && rssi_dbm < c.middle_rssi_dbm) {
    r1 = rMin; d1 = dAtRMin
    r2 = c.middle_rssi_dbm; d2 = c.middle_distance_m
  } else if (midOk) {
    r1 = c.middle_rssi_dbm; d1 = c.middle_distance_m
    r2 = rMax; d2 = dAtRMax
  } else {
    r1 = rMin; d1 = dAtRMin
    r2 = rMax; d2 = dAtRMax
  }

  if (r2 === r1) return Math.max(d1, 1)
  if (c.interpolation === 'linear') {
    return d1 + (d2 - d1) * (rssi_dbm - r1) / (r2 - r1)
  }
  if (d1 <= 0 || d2 <= 0) return Math.max(d1, 1)
  const k = Math.log(d2 / d1) / (r2 - r1)
  return d1 * Math.exp(k * (rssi_dbm - r1))
}

/**
 * Intersection algorithm: distance from this LoB's observer to the nearest
 * forward intersection with any of `peers` (LoBs in the same frequency/device
 * group). Returns null if no peer line crosses ahead.
 */
export function computeIntersectionDistance(lob, peers) {
  const toRad = d => (d * Math.PI) / 180
  const mpdLat = 111_320
  const mpdLon = 111_320 * Math.cos(toRad(lob.lat))
  const ux = Math.sin(toRad(lob.azimuth_deg))
  const uy = Math.cos(toRad(lob.azimuth_deg))

  let best = null
  for (const other of peers) {
    if (!other || other.id === lob.id) continue
    const pt = intersectBearings(
      lob.lat, lob.lon, lob.azimuth_deg,
      other.lat, other.lon, other.azimuth_deg,
    )
    if (!pt) continue
    const dx = (pt[1] - lob.lon) * mpdLon
    const dy = (pt[0] - lob.lat) * mpdLat
    // Reject intersections behind this LoB's bearing (small forward tolerance).
    if (dx * ux + dy * uy < -1) continue
    const dist = Math.sqrt(dx * dx + dy * dy)
    if (best === null || dist < best) best = dist
  }
  return best
}

/**
 * Resolve the rendered length (m) of a LoB under the selected algorithm.
 * `peers` is the list of LoBs in the same frequency/device group (used by
 * the 'intersection' algorithm; ignored by the others).
 */
export function computeLoBRenderDistance(lob, peers, algorithm) {
  const algo = algorithm || DEFAULT_LOB_ALGORITHM
  if (algo.terrain_aware && lob.estimated_distance_m && lob.estimated_distance_m > 0) {
    return lob.estimated_distance_m
  }
  switch (algo.type) {
    case 'intersection': {
      const d = computeIntersectionDistance(lob, peers || [])
      return d !== null ? d : INTERSECTION_FALLBACK_M
    }
    case 'step':
      return computeStepDistance(lob.rssi_dbm, algo.step)
    case 'fixed':
      return Math.max(1, (algo.fixed?.length_m ?? DEFAULT_LOB_ALGORITHM.fixed.length_m))
    default:
      // Legacy 'estimated' or unknown — fall back to per-LoB estimate
      return lob.estimated_distance_m
  }
}
