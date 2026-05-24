// SPDX-License-Identifier: MIT OR Apache-2.0
// Copyright (c) 2026 Ares

/**
 * DF alerts — audible + desktop-notification alerts when new bearings or fixes
 * arrive from the DF function.
 *
 * Designed for at-position operators: glance away from the screen, get a sound
 * + a desktop toast when a new line-of-bearing lands. Distinct sounds per event
 * so an operator can tell "new LoB" from "new high-confidence emitter fix"
 * without looking. Synthesised via Web Audio (no audio assets to ship).
 *
 * Event types:
 *   newLoB       — a new line-of-bearing observation added
 *   newCut       — a frequency group reached 2 LoBs (first cut intersection)
 *   newFix       — a frequency group reached ≥3 LoBs (high-confidence fix)
 *   newEmitter   — a fix-derived centroid materialised
 */
import { create } from 'zustand'

export const ALERT_EVENTS = ['newLoB', 'newCut', 'newFix', 'newEmitter']

export const ALERT_EVENT_LABEL = {
  newLoB:     'New bearing',
  newCut:     'New cut (2 bearings)',
  newFix:     'New fix (≥3 bearings)',
  newEmitter: 'New suspected emitter',
}

// Sound recipes — { freqHz, durationMs, pulses, sweepToHz }. The Web Audio
// player below interprets these into oscillator schedules.
const SOUND_RECIPES = {
  newLoB:     { freqHz: 440, durationMs: 80,  pulses: 1 },
  newCut:     { freqHz: 660, durationMs: 70,  pulses: 2, gapMs: 60 },
  newFix:     { freqHz: 440, durationMs: 220, pulses: 1, sweepToHz: 880 },
  newEmitter: { freqHz: 880, durationMs: 90,  pulses: 4, gapMs: 50 },
}

let _audioCtx = null
function getAudioContext() {
  if (_audioCtx) return _audioCtx
  try {
    const Ctx = window.AudioContext || window.webkitAudioContext
    if (!Ctx) return null
    _audioCtx = new Ctx()
    return _audioCtx
  } catch { return null }
}

// Play a recipe — returns immediately; the schedule is queued on the audio
// context. Volume is 0..1; gain is shaped with a 6 ms attack + 30 ms decay so
// it doesn't click at start/stop. The Web Audio API requires a user gesture
// before the first sound on most browsers; the store's `arm()` action triggers
// that during the settings interaction.
function playRecipe(recipe, volume) {
  const ctx = getAudioContext()
  if (!ctx) return
  if (ctx.state === 'suspended') { try { ctx.resume() } catch {} }
  const { freqHz, durationMs, pulses = 1, gapMs = 0, sweepToHz } = recipe
  const dur = durationMs / 1000
  const gap = gapMs / 1000
  const v = Math.max(0, Math.min(1, volume))
  let t = ctx.currentTime + 0.01
  for (let i = 0; i < pulses; i++) {
    const osc = ctx.createOscillator()
    const gain = ctx.createGain()
    osc.type = 'sine'
    osc.frequency.setValueAtTime(freqHz, t)
    if (sweepToHz) osc.frequency.exponentialRampToValueAtTime(sweepToHz, t + dur)
    gain.gain.setValueAtTime(0.0001, t)
    gain.gain.exponentialRampToValueAtTime(v * 0.5, t + 0.006)             // attack
    gain.gain.exponentialRampToValueAtTime(0.0001, t + dur)                // decay
    osc.connect(gain).connect(ctx.destination)
    osc.start(t); osc.stop(t + dur + 0.02)
    t += dur + gap
  }
}

function notify(title, body) {
  try {
    if (!('Notification' in window)) return
    if (Notification.permission !== 'granted') return
    new Notification(title, { body, icon: '/favicon.ico', tag: 'ares-df-alert' })
  } catch { /* noop */ }
}

// Point-in-polygon for {lat, lon} against a GeoJSON ring [[lon,lat], ...].
function pointInRing(lat, lon, ring) {
  if (!Array.isArray(ring)) return false
  let inside = false
  for (let i = 0, j = ring.length - 1; i < ring.length; j = i++) {
    const [xi, yi] = ring[i] || []
    const [xj, yj] = ring[j] || []
    if (typeof xi !== 'number' || typeof yi !== 'number') continue
    const intersect = ((yi > lat) !== (yj > lat)) &&
      (lon < (xj - xi) * (lat - yi) / (yj - yi + 1e-12) + xi)
    if (intersect) inside = !inside
  }
  return inside
}

function pointInGeofences(lat, lon, geofences) {
  if (!geofences?.features) return false
  for (const f of geofences.features) {
    const g = f.geometry; if (!g) continue
    if (g.type === 'Polygon' && pointInRing(lat, lon, g.coordinates?.[0])) return true
    if (g.type === 'MultiPolygon') {
      for (const poly of g.coordinates || []) if (pointInRing(lat, lon, poly?.[0])) return true
    }
  }
  return false
}

function freqMatchesWatchlist(freqHz, watchlist) {
  if (!Array.isArray(watchlist) || !watchlist.length) return true   // empty list = no filter
  for (const w of watchlist) {
    const tgt = Number(w.frequency_hz)
    const tol = Number(w.tolerance_hz ?? 5_000)
    if (Math.abs(freqHz - tgt) <= tol) return true
  }
  return false
}

export const useDfAlerts = create((set, get) => ({
  enabled: false,
  volume: 0.6,                                                            // 0..1
  sound: true,
  desktop: true,                                                          // requires Notification permission
  perEvent: { newLoB: true, newCut: true, newFix: true, newEmitter: true },
  // Filters — all optional. When empty/null they're treated as no-op.
  geofences: { type: 'FeatureCollection', features: [] },     // GeoJSON; alert only inside any feature
  geofenceMode: 'off',                                         // 'off' | 'inside' | 'outside'
  watchlist: [],                                               // [{ frequency_hz, tolerance_hz, label }]
  minSnrDb: -200,                                              // -200 = effectively off
  setGeofences: (gj) => set({ geofences: gj || { type: 'FeatureCollection', features: [] } }),
  setGeofenceMode: (m) => set({ geofenceMode: ['off','inside','outside'].includes(m) ? m : 'off' }),
  setWatchlist: (w) => set({ watchlist: Array.isArray(w) ? w : [] }),
  setMinSnrDb: (v) => set({ minSnrDb: Number.isFinite(v) ? v : -200 }),

  setEnabled: (v) => set({ enabled: !!v }),
  setVolume:  (v) => set({ volume: Math.max(0, Math.min(1, v)) }),
  setSound:   (v) => set({ sound: !!v }),
  setDesktop: (v) => set({ desktop: !!v }),
  togglePerEvent: (key) => set((s) => ({ perEvent: { ...s.perEvent, [key]: !s.perEvent[key] } })),
  // Test-tone helper for the settings panel — also primes the audio context
  // by happening inside a user click.
  testTone: (event = 'newLoB') => {
    const recipe = SOUND_RECIPES[event] || SOUND_RECIPES.newLoB
    playRecipe(recipe, get().volume)
  },
  requestDesktopPermission: async () => {
    if (!('Notification' in window)) return 'denied'
    if (Notification.permission === 'granted') return 'granted'
    try { return await Notification.requestPermission() } catch { return 'denied' }
  },

  /** Fire an alert. `event` is one of ALERT_EVENTS; `body` is the toast body.
   *  Optional `context = { lat, lon, frequency_hz, snr_db }` enables filters
   *  (geofence inside/outside, frequency watchlist, minimum SNR). When no
   *  context is supplied the filters are not applied (caller fires unconditionally). */
  fire: (event, body = '', context = null) => {
    const s = get()
    if (!s.enabled) return
    if (s.perEvent[event] === false) return
    if (context) {
      if (typeof context.snr_db === 'number' && context.snr_db < s.minSnrDb) return
      if (typeof context.frequency_hz === 'number' && !freqMatchesWatchlist(context.frequency_hz, s.watchlist)) return
      if (s.geofenceMode !== 'off' && typeof context.lat === 'number' && typeof context.lon === 'number') {
        const inside = pointInGeofences(context.lat, context.lon, s.geofences)
        if (s.geofenceMode === 'inside' && !inside) return
        if (s.geofenceMode === 'outside' && inside) return
      }
    }
    if (s.sound) playRecipe(SOUND_RECIPES[event] || SOUND_RECIPES.newLoB, s.volume)
    if (s.desktop) notify(ALERT_EVENT_LABEL[event] || 'DF alert', body)
  },
}))
