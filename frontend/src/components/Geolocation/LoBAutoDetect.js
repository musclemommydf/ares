// SPDX-License-Identifier: MIT OR Apache-2.0
// Copyright (c) 2026 Ares

/**
 * Auto-detection of LoB environment type and clutter height from real-world data.
 *
 * Sources:
 *   1. OpenStreetMap Overpass API — landuse, natural, leisure, place tags
 *   2. Backend /terrain/elevation — absolute elevation for mountainous detection
 *
 * Returns { environment, clutter_height_m } matching ENVIRONMENT_PRESETS ids.
 * Falls back gracefully on network errors or offline use.
 */
import { getElevation, getLoBRangeEstimate } from '../../api/client'

const OVERPASS_URL = 'https://overpass-api.de/api/interpreter'
const OVERPASS_TIMEOUT_MS = 12_000

/**
 * Rules mapping OSM tags → environment preset + typical clutter height.
 * Higher score = more specific (wins over lower-score matches).
 */
const RULES = [
  // ── Landuse ─────────────────────────────────────────────────────────────────
  { tag: 'landuse', value: 'forest',       env: 'forest',      clutter: 18, score: 10 },
  { tag: 'landuse', value: 'commercial',   env: 'urban',       clutter: 20, score: 10 },
  { tag: 'landuse', value: 'industrial',   env: 'urban',       clutter: 15, score: 10 },
  { tag: 'landuse', value: 'retail',       env: 'urban',       clutter: 20, score: 10 },
  { tag: 'landuse', value: 'construction', env: 'urban',       clutter: 10, score:  9 },
  { tag: 'landuse', value: 'residential',  env: 'suburban',    clutter:  8, score:  9 },
  { tag: 'landuse', value: 'allotments',   env: 'rural',       clutter:  2, score:  8 },
  { tag: 'landuse', value: 'farmland',     env: 'rural',       clutter:  1, score:  8 },
  { tag: 'landuse', value: 'farmyard',     env: 'rural',       clutter:  3, score:  8 },
  { tag: 'landuse', value: 'meadow',       env: 'rural',       clutter:  1, score:  8 },
  { tag: 'landuse', value: 'grass',        env: 'rural',       clutter:  0, score:  8 },
  { tag: 'landuse', value: 'village_green',env: 'rural',       clutter:  0, score:  8 },
  { tag: 'landuse', value: 'military',     env: 'open',        clutter:  2, score:  7 },
  // ── Natural ──────────────────────────────────────────────────────────────────
  { tag: 'natural', value: 'wood',         env: 'forest',      clutter: 20, score: 10 },
  { tag: 'natural', value: 'scrub',        env: 'forest',      clutter:  5, score:  8 },
  { tag: 'natural', value: 'heath',        env: 'rural',       clutter:  2, score:  8 },
  { tag: 'natural', value: 'grassland',    env: 'rural',       clutter:  0, score:  8 },
  { tag: 'natural', value: 'wetland',      env: 'rural',       clutter:  1, score:  7 },
  { tag: 'natural', value: 'beach',        env: 'open',        clutter:  0, score:  9 },
  { tag: 'natural', value: 'sand',         env: 'open',        clutter:  0, score:  9 },
  { tag: 'natural', value: 'water',        env: 'open',        clutter:  0, score:  9 },
  { tag: 'natural', value: 'glacier',      env: 'open',        clutter:  0, score:  9 },
  { tag: 'natural', value: 'bare_rock',    env: 'open',        clutter:  0, score:  8 },
  // ── Leisure ──────────────────────────────────────────────────────────────────
  { tag: 'leisure', value: 'park',         env: 'rural',       clutter:  5, score:  7 },
  { tag: 'leisure', value: 'nature_reserve',env:'rural',       clutter:  3, score:  7 },
  { tag: 'leisure', value: 'golf_course',  env: 'open',        clutter:  1, score:  7 },
  { tag: 'leisure', value: 'marina',       env: 'open',        clutter:  2, score:  7 },
  // ── Place (lower score — large administrative areas, less specific) ──────────
  { tag: 'place',   value: 'city',         env: 'urban',       clutter: 20, score:  5 },
  { tag: 'place',   value: 'town',         env: 'suburban',    clutter:  8, score:  5 },
  { tag: 'place',   value: 'suburb',       env: 'suburban',    clutter:  8, score:  5 },
  { tag: 'place',   value: 'quarter',      env: 'suburban',    clutter:  8, score:  5 },
  { tag: 'place',   value: 'village',      env: 'rural',       clutter:  3, score:  4 },
  { tag: 'place',   value: 'hamlet',       env: 'rural',       clutter:  2, score:  4 },
  { tag: 'place',   value: 'isolated_dwelling', env: 'rural',  clutter:  1, score:  4 },
]

/** Classify a list of OSM elements (from Overpass is_in) into an environment. */
function classifyElements(elements) {
  let best = null
  let bestScore = -1
  for (const el of elements) {
    const tags = el.tags || {}
    for (const rule of RULES) {
      if (tags[rule.tag] === rule.value && rule.score > bestScore) {
        best = { environment: rule.env, clutter_height_m: rule.clutter }
        bestScore = rule.score
      }
    }
  }
  return best  // null if nothing matched
}

/**
 * Fetch OSM land-use data for a point via Overpass API.
 * Returns array of OSM elements, or null on failure.
 */
async function fetchOSMLandUse(lat, lon) {
  const query = `[out:json][timeout:10];is_in(${lat},${lon});out tags;`
  const controller = new AbortController()
  const tid = setTimeout(() => controller.abort(), OVERPASS_TIMEOUT_MS)
  try {
    const resp = await fetch(OVERPASS_URL, {
      method: 'POST',
      headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
      body: `data=${encodeURIComponent(query)}`,
      signal: controller.signal,
    })
    clearTimeout(tid)
    if (!resp.ok) return null
    const json = await resp.json()
    return json.elements ?? null
  } catch {
    clearTimeout(tid)
    return null
  }
}

/**
 * Auto-detect environment type and clutter height for a lat/lon using
 * OSM land use (Overpass) + terrain elevation (backend SRTM).
 *
 * @param {number} lat
 * @param {number} lon
 * @returns {Promise<{ environment: string, clutter_height_m: number, source: string } | null>}
 *   Returns null only if both data sources fail completely.
 *   source: 'osm' | 'elevation' | 'fallback'
 */
export async function autoDetectEnvironment(lat, lon) {
  // Run both queries in parallel
  const [elementsResult, elevResult] = await Promise.allSettled([
    fetchOSMLandUse(lat, lon),
    getElevation(lat, lon),
  ])

  const elements = elementsResult.status === 'fulfilled' ? elementsResult.value : null
  const elevation_m = elevResult.status === 'fulfilled'
    ? (elevResult.value?.elevation_m ?? 0)
    : 0

  // Classify from OSM land use
  const osmClass = elements ? classifyElements(elements) : null

  // Elevation override: high terrain → mountainous
  // Only override if OSM says open/rural/unclassified (not urban/forest)
  if (elevation_m > 600) {
    const env = osmClass?.environment
    if (!env || env === 'open' || env === 'rural') {
      return { environment: 'mountainous', clutter_height_m: 0, source: 'elevation' }
    }
  }

  if (osmClass) {
    return { ...osmClass, source: 'osm' }
  }

  // Both sources failed — caller handles the fallback display
  return null
}

/**
 * Terrain-aware LoB range estimation via the backend propagation engine.
 * Runs a single radial simulation from the observer location in the bearing
 * direction and returns where the signal crosses the observed RSSI.
 *
 * @param {object} params
 * @returns {Promise<{ estimated_distance_m: number, confidence: string, propagation_mode: string } | null>}
 */
export async function estimateDistanceFromTerrain(params) {
  try {
    const result = await getLoBRangeEstimate({
      observer_lat: params.observer_lat,
      observer_lon: params.observer_lon,
      observer_height_m: params.observer_height_m ?? 1.5,
      azimuth_deg: params.azimuth_deg,
      frequency_hz: params.frequency_hz,
      tx_power_dbm: params.tx_power_dbm,
      observed_rssi_dbm: params.observed_rssi_dbm,
      propagation_model: 'itm',
      diffraction_model: params.diffraction_model ?? 'deygout',
      clutter_height_m: params.clutter_height_m ?? 0,
      terrain_resolution: params.terrain_resolution ?? 'srtm1',
      context: params.context ?? 2,
      max_range_km: Math.min(150, params.max_range_km ?? 150),
      num_points: params.num_points ?? 300,
    })
    if (result?.status === 'ok' && result.estimated_distance_m > 0) {
      return {
        estimated_distance_m: result.estimated_distance_m,
        confidence: result.confidence,
        propagation_mode: result.propagation_mode,
      }
    }
    return null
  } catch {
    return null
  }
}
