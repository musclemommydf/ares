// SPDX-License-Identifier: MIT OR Apache-2.0
// Copyright (c) 2026 Ares

import { useState, useEffect } from 'react'
import { getTerrainGrid } from '../api/client'

/**
 * Fetches the elevation grid for the 3-D View tab — the real terrain (SRTM, or a covering
 * offline pack) in a square around the transmitter. Re-fetched whenever that tab is active
 * and the transmitter moves, or its resolution changes. Returns the grid (or null) and a
 * loading flag, which App hands to <ThreeDView>. When no terrain source is reachable the
 * backend returns an all-zero grid (flat) — ThreeDView flags that so it isn't mistaken for
 * "the terrain is just flat here".
 *
 * @param {object} tx          the primary transmitter ({ lat, lon, ... })
 * @param {object} propagation the propagation config (uses radius_km and terrain_resolution)
 * @param {string} bottomTab   the active bottom-panel tab; only fetches when it's '3d'
 */
export function useTerrainGrid(tx, propagation, bottomTab) {
  const [terrainGrid, setTerrainGrid] = useState(null)
  const [terrainGridLoading, setTerrainGridLoading] = useState(false)
  const res = propagation.terrain_resolution

  useEffect(() => {
    if (bottomTab !== '3d') return
    setTerrainGridLoading(true)
    // a square around the TX, capped so the request stays light; 48×48 samples for a
    // recognisable surface (≈ radius/24 spacing — finer than the radial-sweep thinning)
    const r = Math.max(2, Math.min(propagation.radius_km ?? 20, 40))
    getTerrainGrid(tx.lat, tx.lon, r, 48, res)
      .then(g => setTerrainGrid(g))
      .catch(() => setTerrainGrid(null))
      .finally(() => setTerrainGridLoading(false))
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [bottomTab, tx.lat, tx.lon, res])

  return { terrainGrid, terrainGridLoading }
}
