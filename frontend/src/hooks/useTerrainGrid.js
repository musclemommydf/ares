import { useState, useEffect } from 'react'
import { getTerrainGrid } from '../api/client'

/**
 * Fetches the coarse elevation grid for the 3-D View tab — re-fetched whenever
 * that tab is active and the transmitter moves. Returns the grid (or null) and a
 * loading flag, which App hands to <ThreeDView>.
 *
 * @param {object} tx          the primary transmitter ({ lat, lon, ... })
 * @param {object} propagation the propagation config (uses radius_km, capped at 30 km)
 * @param {string} bottomTab   the active bottom-panel tab; only fetches when it's '3d'
 */
export function useTerrainGrid(tx, propagation, bottomTab) {
  const [terrainGrid, setTerrainGrid] = useState(null)
  const [terrainGridLoading, setTerrainGridLoading] = useState(false)

  useEffect(() => {
    if (bottomTab !== '3d') return
    setTerrainGridLoading(true)
    const r = Math.min(propagation.radius_km ?? 50, 30)
    getTerrainGrid(tx.lat, tx.lon, r, 30)
      .then(g => setTerrainGrid(g))
      .catch(() => setTerrainGrid(null))
      .finally(() => setTerrainGridLoading(false))
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [bottomTab, tx.lat, tx.lon])

  return { terrainGrid, terrainGridLoading }
}
