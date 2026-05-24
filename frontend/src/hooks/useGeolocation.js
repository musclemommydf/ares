// SPDX-License-Identifier: MIT OR Apache-2.0
// Copyright (c) 2026 Ares

import { useState, useCallback, useEffect, useMemo, useRef } from 'react'
import {
  groupLoBsByFrequency, lobGroupKey, computeGroupIntersections, computeCentroid,
  computeCAPEllipse, computeLoBRenderDistance, destinationPoint, DEFAULT_LOB_ALGORITHM,
} from '../components/Geolocation/LoBUtils'
import { useDfAlerts } from '../store/dfAlerts'

/**
 * Direction-finding / line-of-bearing state for the "geolocation" mode:
 *   - lobs / capGroups / lobAlgorithm  — persisted in the session
 *   - the transient "pick a point / azimuth target on the map" sub-modes
 *   - the handlers that mutate the above
 *   - lobGroups   — LoBs grouped by frequency (Cut/Fix grouping)
 *   - lobFeatures — GeoJSON features (bearing wedges, Cut/Fix centroids, CAP/CEP
 *                   ellipses); App merges these with the live SDR/DF features and
 *                   feeds the union to the 2-D map and the 3-D globe.
 *
 * @param {object|null} _s          the saved session (hydrates initial state)
 * @param {Function}    onActivate  called when a map-picked observer should switch the app to geolocation mode
 */
export function useGeolocation(_s, onActivate) {
  const [lobs, setLobs] = useState(() => _s?.lobs?.map(l => ({ device_type: '', device_id: '', ...l })) ?? [])
  const [capGroups, setCapGroups] = useState(() => _s?.capGroups ?? {})  // { [groupKey]: boolean } missing = default true
  const [lobAlgorithm, setLobAlgorithm] = useState(() => {
    if (!_s?.lobAlgorithm) return DEFAULT_LOB_ALGORITHM
    const merged = {
      ...DEFAULT_LOB_ALGORITHM,
      ..._s.lobAlgorithm,
      step: { ...DEFAULT_LOB_ALGORITHM.step, ...(_s.lobAlgorithm.step || {}) },
      fixed: { ...DEFAULT_LOB_ALGORITHM.fixed, ...(_s.lobAlgorithm.fixed || {}) },
      receiver_accuracy: {
        ...DEFAULT_LOB_ALGORITHM.receiver_accuracy,
        ...(_s.lobAlgorithm.receiver_accuracy || {}),
      },
    }
    // Migrate legacy 'estimated' algorithm → 'step' + terrain-aware modifier
    if (merged.type === 'estimated') {
      merged.type = 'step'
      merged.terrain_aware = true
    }
    return merged
  })
  const [lobPickingMode, setLobPickingMode] = useState(false)
  const [pendingLobLocation, setPendingLobLocation] = useState(null)
  const [lobAzimuthPickingMode, setLobAzimuthPickingMode] = useState(false)
  const [pendingLobAzimuthTarget, setPendingLobAzimuthTarget] = useState(null)
  const [editLobRequestId, setEditLobRequestId] = useState(null)

  const lobGroups = useMemo(() => groupLoBsByFrequency(lobs), [lobs])

  // GeoJSON of the DF picture (bearing wedges, Cut/Fix centroids, CAP/CEP ellipses).
  const lobFeatures = useMemo(() => {
    const features = []
    for (const lob of lobs) {
      const grp = lobGroups.find((g) => g.lobs.some((l) => l.id === lob.id || l === lob))
      const peers = grp ? grp.lobs : [lob]
      let dist = 1000
      try { dist = computeLoBRenderDistance(lob, peers, lobAlgorithm) || 1000 } catch { /* keep default */ }
      let end
      try { end = destinationPoint(lob.lat, lob.lon, lob.azimuth_deg, dist) } catch { end = null }
      if (!end) continue
      features.push({ type: 'Feature', geometry: { type: 'LineString', coordinates: [[lob.lon, lob.lat], [end[1], end[0]]] },
        properties: { glx: 'lob', color: lob.color || '#f59e0b', azimuth_deg: lob.azimuth_deg, frequency_hz: lob.frequency_hz, lob_id: lob.id } })
    }
    for (const g of lobGroups) {
      if (g.lobs.length < 2) continue
      const kind = g.lobs.length >= 3 ? 'fix' : 'cut'
      let ints = []
      try { ints = computeGroupIntersections(g) } catch { /* skip */ }
      const centroid = (() => { try { return computeCentroid(ints) } catch { return null } })()
      if (capGroups[lobGroupKey(g)] !== false) {
        const ell = (() => { try { return computeCAPEllipse(g, ints, lobAlgorithm) } catch { return null } })()
        if (ell) features.push({ ...ell, properties: { ...(ell.properties || {}), glx: 'cap', kind, frequency_hz: g.frequency_hz } })
      }
      if (centroid) features.push({ type: 'Feature', geometry: { type: 'Point', coordinates: [centroid.lon, centroid.lat] },
        properties: { glx: 'emitter', kind, frequency_hz: g.frequency_hz, device_id: g.device_id || '', n_lobs: g.lobs.length } })
    }
    return features
  }, [lobs, lobGroups, capGroups, lobAlgorithm])

  const fireAlert = useDfAlerts((s) => s.fire)
  const handleAddLoB = useCallback((lob) => {
    setLobs(prev => [...prev, lob])
    try {
      const fHz = Number(lob.frequency_hz)
      const fMHz = Number.isFinite(fHz) && fHz > 0 ? `${(fHz / 1e6).toFixed(3)} MHz` : '—'
      // Pass context so the alert filters (geofence inside/outside, watchlist, min SNR) can apply.
      fireAlert('newLoB', `${fMHz} · ${Number(lob.azimuth_deg ?? 0).toFixed(1)}°`, {
        lat: lob.lat, lon: lob.lon,
        frequency_hz: fHz,
        snr_db: lob.snr_db,
      })
    } catch { /* ignore alert errors */ }
  }, [fireAlert])

  // Group-transition alerts: 1→2 LoBs in a freq group ⇒ first cut, 2→3 ⇒ first
  // high-confidence fix. Tracked by group key so re-adds of the same group
  // don't re-fire. Also fires `newEmitter` when a centroid first becomes
  // computable for a group (effectively coincident with newFix but treated as
  // a separate event so operators can mute the cut/fix beeps and only hear
  // confirmed emitter pings).
  const groupStateRef = useRef(new Map())   // key → { count, hadEmitter }
  useEffect(() => {
    const prev = groupStateRef.current
    const next = new Map()
    for (const g of lobGroups) {
      const key = lobGroupKey(g)
      const count = g.lobs.length
      const had = prev.get(key) || { count: 0, hadEmitter: false }
      next.set(key, { count, hadEmitter: had.hadEmitter || count >= 2 })
      const fMHz = `${(g.frequency_hz / 1e6).toFixed(3)} MHz`
      if (had.count < 2 && count >= 2) fireAlert('newCut', `${fMHz} · ${count} bearings`)
      if (had.count < 3 && count >= 3) {
        fireAlert('newFix', `${fMHz} · ${count} bearings`)
        if (!had.hadEmitter) fireAlert('newEmitter', `${fMHz}`)
      }
    }
    groupStateRef.current = next
  }, [lobGroups, fireAlert])


  const handleRemoveLoB = useCallback((id) => { setLobs(prev => prev.filter(l => l.id !== id)) }, [])
  const handleUpdateLoB = useCallback((updated) => { setLobs(prev => prev.map(l => l.id === updated.id ? updated : l)) }, [])
  const handleToggleCAP = useCallback((groupKey) => {
    setCapGroups(prev => ({ ...prev, [groupKey]: prev[groupKey] === false ? true : false }))
  }, [])
  const handleAddLoBObserver = useCallback((lat, lon) => {
    onActivate?.()
    setPendingLobLocation({ lat, lon })
    setLobPickingMode(false)
  }, [onActivate])
  const handleAddLoBAzimuthTarget = useCallback((lat, lon) => {
    setPendingLobAzimuthTarget({ lat, lon })
    setLobAzimuthPickingMode(false)
  }, [])

  return {
    lobs, setLobs, capGroups, setCapGroups, lobAlgorithm, setLobAlgorithm,
    lobPickingMode, setLobPickingMode, pendingLobLocation, setPendingLobLocation,
    lobAzimuthPickingMode, setLobAzimuthPickingMode, pendingLobAzimuthTarget, setPendingLobAzimuthTarget,
    editLobRequestId, setEditLobRequestId, lobGroups, lobFeatures,
    handleAddLoB, handleRemoveLoB, handleUpdateLoB, handleToggleCAP,
    handleAddLoBObserver, handleAddLoBAzimuthTarget,
  }
}
