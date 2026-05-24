// SPDX-License-Identifier: MIT OR Apache-2.0
// Copyright (c) 2026 Ares

/**
 * Track history — recorded position-over-time tracks (Co-Opt mover replay,
 * drone telemetry, SDR-fed GPS feeds, etc.). Designed as a thin store that
 * any data source can `addPoint(trackId, lat, lon, ts?)` to; the UI handles
 * persistence, on-map rendering, and playback scrubbing.
 *
 * Data shape:
 *   tracks: Record<trackId, {
 *     id: string, name: string, color: string,
 *     points: { lat, lon, t }[],           // t = epoch ms
 *     recording: boolean,
 *     visible: boolean,
 *   }>
 *
 * Playback is held in `playback`: { trackId, t } — when t is set, the renderer
 * draws the polyline up to t and a "now" marker at the interpolated position.
 */
import { create } from 'zustand'

let SEQ = 1
const nextId = () => `tr_${Date.now().toString(36)}_${(SEQ++).toString(36)}`

const PALETTE = ['#06d6a0', '#00b4d8', '#f59e0b', '#ef4444', '#a855f7', '#22c55e', '#facc15']

export const useTrackHistory = create((set, get) => ({
  tracks: {},
  playback: null,           // { trackId, t } — current scrub time
  // Create an empty track. Returns its id.
  createTrack: (name = '') => {
    const id = nextId()
    const i = Object.keys(get().tracks).length
    set((s) => ({
      tracks: { ...s.tracks, [id]: {
        id, name: name || `Track ${i + 1}`,
        color: PALETTE[i % PALETTE.length],
        points: [], recording: true, visible: true,
      } },
    }))
    return id
  },
  addPoint: (trackId, lat, lon, t = Date.now()) => set((s) => {
    const tr = s.tracks[trackId]; if (!tr || !tr.recording) return {}
    // Drop duplicate consecutive points (same lat/lon to ~5dp) — they bloat the
    // track without telling us anything new.
    const last = tr.points[tr.points.length - 1]
    if (last && Math.abs(last.lat - lat) < 1e-5 && Math.abs(last.lon - lon) < 1e-5) return {}
    return { tracks: { ...s.tracks, [trackId]: { ...tr, points: [...tr.points, { lat, lon, t }] } } }
  }),
  stopRecording: (trackId) => set((s) => {
    const tr = s.tracks[trackId]; if (!tr) return {}
    return { tracks: { ...s.tracks, [trackId]: { ...tr, recording: false } } }
  }),
  startRecording: (trackId) => set((s) => {
    const tr = s.tracks[trackId]; if (!tr) return {}
    return { tracks: { ...s.tracks, [trackId]: { ...tr, recording: true } } }
  }),
  setVisible: (trackId, visible) => set((s) => {
    const tr = s.tracks[trackId]; if (!tr) return {}
    return { tracks: { ...s.tracks, [trackId]: { ...tr, visible } } }
  }),
  renameTrack: (trackId, name) => set((s) => {
    const tr = s.tracks[trackId]; if (!tr) return {}
    return { tracks: { ...s.tracks, [trackId]: { ...tr, name } } }
  }),
  removeTrack: (trackId) => set((s) => {
    const next = { ...s.tracks }; delete next[trackId]
    const pb = s.playback?.trackId === trackId ? null : s.playback
    return { tracks: next, playback: pb }
  }),
  clearAll: () => set({ tracks: {}, playback: null }),
  // Playback: scrub through a track's timeline; t is epoch ms. Renderers
  // interpolate between the two surrounding points for the "now" marker.
  startPlayback: (trackId) => set((s) => {
    const tr = s.tracks[trackId]; if (!tr || tr.points.length === 0) return {}
    return { playback: { trackId, t: tr.points[0].t } }
  }),
  scrubTo: (t) => set((s) => s.playback ? { playback: { ...s.playback, t } } : {}),
  stopPlayback: () => set({ playback: null }),
}))

// Sample the position at a given timestamp via linear interpolation along the
// track's segments. Returns null if `t` is outside the track's time range.
export function trackPositionAt(track, t) {
  const pts = track.points
  if (!pts.length) return null
  if (t <= pts[0].t) return { lat: pts[0].lat, lon: pts[0].lon }
  if (t >= pts[pts.length - 1].t) return { lat: pts[pts.length - 1].lat, lon: pts[pts.length - 1].lon }
  for (let i = 1; i < pts.length; i++) {
    if (pts[i].t >= t) {
      const a = pts[i - 1], b = pts[i]
      const span = b.t - a.t || 1
      const f = (t - a.t) / span
      return { lat: a.lat + (b.lat - a.lat) * f, lon: a.lon + (b.lon - a.lon) * f }
    }
  }
  return null
}
