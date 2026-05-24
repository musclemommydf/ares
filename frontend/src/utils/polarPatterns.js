// SPDX-License-Identifier: MIT OR Apache-2.0
// Copyright (c) 2026 Ares

/**
 * Polar (azimuth-plane) radiation patterns — JS mirror of
 * backend/app/core/propagation/polar_patterns.py.
 *
 * Used for live UI preview (pattern dropdown, derived -3 dB / -6 dB
 * beamwidth readout, and a small polar plot).  The simulator on the
 * backend evaluates the same definitions, so what the UI shows matches
 * what the splat does.
 */

const FLOOR_DB = -40.0

export const POLAR_PATTERNS = {
  omni: {
    kind: 'card', a: 1.0, b: 0.0,
    label: 'Omnidirectional', category: 'Omni',
    description: 'Uniform 360° coverage. No directionality.',
  },
  subcardioid: {
    kind: 'card', a: 0.7, b: 0.3,
    label: 'Sub-cardioid', category: 'Cardioid family',
    description: 'Wide front lobe, gentle rear attenuation; no deep null.',
  },
  cardioid: {
    kind: 'card', a: 0.5, b: 0.5,
    label: 'Cardioid', category: 'Cardioid family',
    description: 'Heart-shaped pattern with deep null directly behind boresight.',
  },
  supercardioid: {
    kind: 'card', a: 0.37, b: 0.63,
    label: 'Super-cardioid', category: 'Cardioid family',
    description: 'Tighter forward beam than cardioid; small rear lobe, deepest rejection at ~127°.',
  },
  hypercardioid: {
    kind: 'card', a: 0.25, b: 0.75,
    label: 'Hyper-cardioid', category: 'Cardioid family',
    description: 'Even tighter front beam; larger rear lobe (~-6 dB), max rejection at ~109°.',
  },
  figure_8: {
    kind: 'card', a: 0.0, b: 1.0,
    label: 'Figure-8 / Bidirectional', category: 'Cardioid family',
    description: 'Equal main and rear lobes, deep nulls at ±90° (free-space dipole).',
  },
  sector_60: {
    kind: 'lobes',
    lobes: [[0, 0, 60], [-25, 180, 120]],
    label: 'Sector 60°', category: 'Sector',
    description: '60° HPBW main lobe with ~25 dB front-to-back ratio.',
  },
  sector_90: {
    kind: 'lobes',
    lobes: [[0, 0, 90], [-25, 180, 150]],
    label: 'Sector 90°', category: 'Sector',
    description: '90° HPBW main lobe with ~25 dB front-to-back ratio.',
  },
  sector_120: {
    kind: 'lobes',
    lobes: [[0, 0, 120], [-22, 180, 180]],
    label: 'Sector 120°', category: 'Sector',
    description: '120° HPBW main lobe — typical 3-sector cellular cell.',
  },
  yagi_3: {
    kind: 'lobes',
    lobes: [[0, 0, 46], [-18, 180, 60]],
    label: 'Yagi 3-element', category: 'Directional',
    description: 'Compact Yagi, ~46° HPBW, modest rear lobe.',
  },
  yagi_5: {
    kind: 'lobes',
    lobes: [[0, 0, 36], [-20, 180, 55]],
    label: 'Yagi 5-element', category: 'Directional',
    description: 'Common Yagi, ~36° HPBW.',
  },
  yagi_9: {
    kind: 'lobes',
    lobes: [[0, 0, 27], [-22, 180, 50]],
    label: 'Yagi 9-element', category: 'Directional',
    description: 'High-gain Yagi, ~27° HPBW.',
  },
  yagi_15: {
    kind: 'lobes',
    lobes: [[0, 0, 21], [-25, 180, 45]],
    label: 'Yagi 15-element', category: 'Directional',
    description: 'Long-boom Yagi, ~21° HPBW.',
  },
  log_periodic: {
    kind: 'lobes',
    lobes: [[0, 0, 60], [-20, 180, 120]],
    label: 'Log-Periodic', category: 'Directional',
    description: 'Broadband directional, ~60° HPBW across decade bandwidth.',
  },
  horn: {
    kind: 'lobes',
    lobes: [[0, 0, 30], [-25, 180, 50]],
    label: 'Horn', category: 'Aperture',
    description: 'Pyramidal horn, ~30° HPBW, controlled side lobes.',
  },
  parabolic_narrow: {
    kind: 'lobes',
    lobes: [[0, 0, 5], [-22, 12, 8], [-22, -12, 8], [-30, 180, 60]],
    label: 'Parabolic dish (narrow)', category: 'Aperture',
    description: 'Highly directional, ~5° HPBW, first side lobe at ±12°.',
  },
  parabolic_medium: {
    kind: 'lobes',
    lobes: [[0, 0, 12], [-22, 25, 18], [-22, -25, 18], [-30, 180, 60]],
    label: 'Parabolic dish (medium)', category: 'Aperture',
    description: 'Mid-size dish, ~12° HPBW, first side lobe at ±25°.',
  },
  patch: {
    kind: 'lobes',
    lobes: [[0, 0, 80], [-20, 180, 200]],
    label: 'Patch / Microstrip', category: 'Planar',
    description: 'Hemispherical patch, ~80° HPBW, low rear radiation.',
  },
  helical: {
    kind: 'lobes',
    lobes: [[0, 0, 52], [-15, 180, 80]],
    label: 'Helical (axial mode)', category: 'Circular pol',
    description: 'Axial-mode helix, ~52° HPBW, circularly polarised.',
  },
  phased_array: {
    kind: 'lobes',
    lobes: [[0, 0, 12], [-18, 30, 25], [-18, -30, 25], [-25, 180, 80]],
    label: 'Phased array', category: 'Array',
    description: 'Steered array, ~12° HPBW, grating-lobe-like side lobes.',
  },
}

/** Existing antenna_type → polar_pattern fallback. */
export const ANTENNA_TYPE_TO_POLAR_PATTERN = {
  isotropic: 'omni',
  omnidirectional: 'omni',
  omni_5dbi: 'omni',
  omni_9dbi: 'omni',
  dipole_half_wave: 'omni',
  dipole_full_wave: 'omni',
  dipole_quarter_wave: 'omni',
  whip_quarter_wave: 'omni',
  ground_plane: 'omni',
  collinear_2el: 'omni',
  collinear_4el: 'omni',
  loop: 'figure_8',
  crossed_dipole: 'omni',
  yagi_3el: 'yagi_3',
  yagi_5el: 'yagi_5',
  yagi_9el: 'yagi_9',
  yagi_15el: 'yagi_15',
  log_periodic: 'log_periodic',
  sector_60: 'sector_60',
  sector_90: 'sector_90',
  sector_120: 'sector_120',
  patch: 'patch',
  horn: 'horn',
  parabolic_dish: 'parabolic_medium',
  helical: 'helical',
  phased_array: 'phased_array',
  custom: 'omni',
}

/** Relative gain (dB, peak = 0) at azimuth offset (deg) for a pattern id. */
export function polarPatternGainDb(patternId, azOffsetDeg) {
  const p = POLAR_PATTERNS[patternId]
  if (!p) return 0.0
  let th = ((azOffsetDeg + 180) % 360 + 360) % 360 - 180

  if (p.kind === 'card') {
    const peakAmp = Math.abs(p.a + p.b)
    if (peakAmp <= 1e-12) return FLOOR_DB
    const amp = Math.abs(p.a + p.b * Math.cos(th * Math.PI / 180))
    if (amp <= 1e-12) return FLOOR_DB
    return Math.max(FLOOR_DB, 20 * Math.log10(amp / peakAmp))
  }

  let g = FLOOR_DB
  for (const [peakDb, centerDeg, hpbwDeg] of p.lobes) {
    if (hpbwDeg <= 0) continue
    let delta = Math.abs(th - centerDeg)
    if (delta > 180) delta = 360 - delta
    const contribution = peakDb - 12 * (delta / hpbwDeg) ** 2
    if (contribution > g) g = contribution
  }
  return Math.max(FLOOR_DB, g)
}

/** Numerically derive (-3 dB, -6 dB) full beamwidths in degrees. Returns {hpbw3, hpbw6}; either may be null for omni. */
export function computePatternBeamwidths(patternId) {
  if (!POLAR_PATTERNS[patternId]) return { hpbw3: null, hpbw6: null }
  const samples = []
  for (let deg = 0; deg <= 180; deg += 0.5) {
    samples.push([deg, polarPatternGainDb(patternId, deg)])
  }
  const h3 = firstCrossing(samples, -3)
  const h6 = firstCrossing(samples, -6)
  return {
    hpbw3: h3 === null ? null : Math.round(h3 * 2 * 10) / 10,
    hpbw6: h6 === null ? null : Math.round(h6 * 2 * 10) / 10,
  }
}

function firstCrossing(samples, threshold) {
  let [prevDeg, prevG] = samples[0]
  if (prevG <= threshold) return prevDeg
  for (let i = 1; i < samples.length; i++) {
    const [deg, g] = samples[i]
    if (g <= threshold) {
      if (prevG !== g) {
        const frac = (prevG - threshold) / (prevG - g)
        return prevDeg + frac * (deg - prevDeg)
      }
      return deg
    }
    prevDeg = deg; prevG = g
  }
  return null
}

/** Ordered list of pattern ids grouped by category, for select dropdowns. */
export function patternsByCategory() {
  const groups = {}
  for (const [id, meta] of Object.entries(POLAR_PATTERNS)) {
    const cat = meta.category || 'Other'
    if (!groups[cat]) groups[cat] = []
    groups[cat].push({ id, ...meta })
  }
  return groups
}
