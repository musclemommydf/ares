// SPDX-License-Identifier: MIT OR Apache-2.0
// Copyright (c) 2026 Ares

/**
 * Auto session persistence — loads (and migrates) the saved UI session from
 * localStorage. App.jsx reads this once at module load to hydrate useState
 * initial values, and writes back to SESSION_KEY in a debounced effect.
 */
export const SESSION_KEY = 'ares_session'
const LEGACY_SESSION_KEY = 'mv_session'

// Migrate old beam_mode/beam_width_deg sessions onto the new polar_pattern field
// so users who already had a custom beamwidth still get a directional pattern.
function migrateAntenna(ant) {
  if (!ant) return ant
  if (ant.polar_pattern !== undefined) return ant
  let polar_pattern = 'omni'
  if (ant.beam_mode === 'custom' && typeof ant.beam_width_deg === 'number') {
    const bw = ant.beam_width_deg
    if (bw <= 30) polar_pattern = 'parabolic_medium'
    else if (bw <= 50) polar_pattern = 'horn'
    else if (bw <= 75) polar_pattern = 'sector_60'
    else if (bw <= 105) polar_pattern = 'sector_90'
    else if (bw <= 135) polar_pattern = 'sector_120'
    else polar_pattern = 'cardioid'
  }
  const { beam_mode: _bm, beam_width_deg: _bw, ...rest } = ant
  return { ...rest, polar_pattern, polar_peak_gain_dbi: ant.polar_peak_gain_dbi ?? null }
}

export function loadSession() {
  let s
  try {
    let raw = localStorage.getItem(SESSION_KEY)
    if (!raw) {
      const legacy = localStorage.getItem(LEGACY_SESSION_KEY)
      if (legacy) {
        localStorage.setItem(SESSION_KEY, legacy)
        localStorage.removeItem(LEGACY_SESSION_KEY)
        raw = legacy
      }
    }
    if (!raw) return null
    s = JSON.parse(raw)
  } catch {
    return null
  }
  if (s?.primaryTransmitter?.antenna) {
    s.primaryTransmitter.antenna = migrateAntenna(s.primaryTransmitter.antenna)
  }
  if (Array.isArray(s?.extraTransmitters)) {
    s.extraTransmitters = s.extraTransmitters.map(t => (t?.antenna ? { ...t, antenna: migrateAntenna(t.antenna) } : t))
  }
  return s
}
