// SPDX-License-Identifier: MIT OR Apache-2.0
// Copyright (c) 2026 Ares

import { useState, useCallback } from 'react'
import { getTerrainProfile } from '../api/client'

/**
 * The "draw a line on the map → see a terrain cross-section" feature.
 *
 * Prefers locally-loaded terrain grids when they cover most of the path; otherwise
 * falls back to the backend terrain endpoint, sampled per pair of consecutive
 * vertices and stitched together. `onReady` is invoked once a profile is available
 * (App wires it to switch the bottom panel to the Terrain Profile tab).
 *
 * @param {object}   ul       the useUserLayers() instance (for local terrain grids)
 * @param {Function} onReady  called with no args when standaloneProfile becomes available
 */
export function useStandaloneTerrainProfile(ul, onReady) {
  const [terrainLineMode, setTerrainLineMode] = useState(false)
  const [standaloneProfile, setStandaloneProfile] = useState(null)
  const [standaloneProfileLoading, setStandaloneProfileLoading] = useState(false)
  const [standaloneProfileError, setStandaloneProfileError] = useState('')

  const handleTerrainLineComplete = useCallback(async (path) => {
    setTerrainLineMode(false)
    if (!path || path.length < 2) return
    setStandaloneProfileError('')
    setStandaloneProfile(null)

    // Prefer locally-loaded terrain grids if any cover the path
    if (ul.hasTerrain()) {
      const sampled = ul.sampleTerrainAlongPath(path, 512)
      if (sampled) {
        const validCount = sampled.elevations_m.filter(v => Number.isFinite(v)).length
        if (validCount / sampled.elevations_m.length > 0.5) {
          setStandaloneProfile({
            distances_m: sampled.distances_m,
            elevations_m: sampled.elevations_m.map(v => Number.isFinite(v) ? v : 0),
            path,
            source: 'local',
            totalM: sampled.totalM,
          })
          onReady?.()
          return
        }
      }
    }

    // Fall back to backend terrain endpoint per pair of consecutive vertices
    setStandaloneProfileLoading(true)
    try {
      const numPoints = Math.max(100, Math.min(512, 100 * (path.length - 1)))
      const pointsPerSeg = Math.ceil(numPoints / (path.length - 1))
      const allDist = []
      const allElev = []
      let cumulative = 0
      for (let i = 0; i < path.length - 1; i++) {
        const a = path[i], b = path[i + 1]
        const seg = await getTerrainProfile(a[0], a[1], b[0], b[1], pointsPerSeg)
        const segDist = seg.distances_m || seg.profile?.distances_m
        const segElev = seg.elevations_m || seg.profile?.elevations_m
        if (!segDist || !segElev) continue
        const start = i === 0 ? 0 : 1  // skip first point of subsequent segments to avoid duplication
        for (let k = start; k < segDist.length; k++) {
          allDist.push(cumulative + segDist[k])
          allElev.push(segElev[k])
        }
        cumulative += segDist[segDist.length - 1]
      }
      if (allDist.length === 0) throw new Error('Empty profile')
      setStandaloneProfile({
        distances_m: allDist, elevations_m: allElev, path,
        source: 'backend', totalM: cumulative,
      })
      onReady?.()
    } catch (e) {
      setStandaloneProfileError('Profile failed: ' + (e?.message || e))
    } finally {
      setStandaloneProfileLoading(false)
    }
  }, [ul, onReady])

  return {
    terrainLineMode, setTerrainLineMode,
    standaloneProfile, setStandaloneProfile, standaloneProfileLoading, standaloneProfileError,
    handleTerrainLineComplete,
  }
}
