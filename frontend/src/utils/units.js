// SPDX-License-Identifier: MIT OR Apache-2.0
// Copyright (c) 2026 Ares

/**
 * Unit conversion and coordinate format utilities.
 * Supports: metric/imperial distances, lat-lon / MGRS / UTM coordinates.
 */
import { forward, toPoint } from 'mgrs'

// ── Distance ─────────────────────────────────────────────────────────────────

export function formatDistance(m, unit = 'metric') {
  if (unit === 'imperial') {
    const ft = m * 3.28084
    if (ft >= 5280) return `${(ft / 5280).toFixed(2)} mi`
    return `${ft.toFixed(0)} ft`
  }
  if (m >= 1000) return `${(m / 1000).toFixed(1)} km`
  return `${m.toFixed(0)} m`
}

export function formatAltitude(m, unit = 'metric') {
  if (unit === 'imperial') return `${(m * 3.28084).toFixed(0)} ft`
  return `${m.toFixed(0)} m`
}

export function formatSignal(dbm) {
  return `${dbm.toFixed(1)} dBm`
}

export function metersToDisplay(m, unit = 'metric') {
  return unit === 'imperial' ? m * 3.28084 : m
}

export function displayToMeters(val, unit = 'metric') {
  return unit === 'imperial' ? val / 3.28084 : val
}

export function distanceLabel(unit = 'metric') {
  return unit === 'imperial' ? 'ft / mi' : 'm / km'
}

export function altitudeUnit(unit = 'metric') {
  return unit === 'imperial' ? 'ft' : 'm'
}

export function heightLabel(unit = 'metric') {
  return unit === 'imperial' ? 'ft AGL' : 'm AGL'
}

// ── Coordinates ───────────────────────────────────────────────────────────────

/**
 * Format lat/lon according to selected coordinate system.
 * @param {number} lat
 * @param {number} lon
 * @param {'latlon'|'latlon_dms'|'mgrs'|'utm'} system
 * @returns {string}
 */
export function formatCoordinate(lat, lon, system = 'latlon') {
  switch (system) {
    case 'latlon_dms': return toDMS(lat, lon)
    case 'mgrs':       return toMGRS(lat, lon)
    case 'utm':        return toUTM(lat, lon)
    default:           return `${lat.toFixed(6)}, ${lon.toFixed(6)}`
  }
}

export function coordSystemLabel(system = 'latlon') {
  const labels = {
    latlon:     'Lat / Lon (DD)',
    latlon_dms: 'Lat / Lon (DMS)',
    mgrs:       'MGRS',
    utm:        'UTM',
  }
  return labels[system] || system
}

// ── Lat/Lon DMS ───────────────────────────────────────────────────────────────

function toDMS(lat, lon) {
  const fmt = (deg, pos, neg) => {
    const abs = Math.abs(deg)
    const d = Math.floor(abs)
    const m = Math.floor((abs - d) * 60)
    const s = ((abs - d) * 60 - m) * 60
    return `${d}°${m}'${s.toFixed(1)}"${deg >= 0 ? pos : neg}`
  }
  return `${fmt(lat, 'N', 'S')} ${fmt(lon, 'E', 'W')}`
}

// ── Lat/Lon DDM (Decimal Minutes) ────────────────────────────────────────────

export function toDDM(lat, lon) {
  const fmt = (deg, pos, neg) => {
    const abs = Math.abs(deg)
    const d = Math.floor(abs)
    const m = (abs - d) * 60
    return `${d}°${m.toFixed(4)}'${deg >= 0 ? pos : neg}`
  }
  return `${fmt(lat, 'N', 'S')} ${fmt(lon, 'E', 'W')}`
}

// ── Maidenhead Grid Locator (6-character) ───────────────────────────────────

export function toMaidenhead(lat, lon, precision = 6) {
  const aLat = lat + 90
  const aLon = lon + 180
  const A = 'A'.charCodeAt(0)
  let s = ''
  // Field (20° lon × 10° lat)
  s += String.fromCharCode(A + Math.floor(aLon / 20))
  s += String.fromCharCode(A + Math.floor(aLat / 10))
  // Square (2° × 1°)
  s += Math.floor((aLon % 20) / 2)
  s += Math.floor(aLat % 10)
  // Subsquare (5' × 2.5')
  if (precision >= 6) {
    const lonRem = (aLon - Math.floor(aLon / 2) * 2) * 60  // minutes
    const latRem = (aLat - Math.floor(aLat)) * 60
    s += String.fromCharCode('a'.charCodeAt(0) + Math.floor(lonRem / 5))
    s += String.fromCharCode('a'.charCodeAt(0) + Math.floor(latRem / 2.5))
  }
  return s
}

// ── Plus Code (Open Location Code) — 10-char short form ─────────────────────
// Reference: https://github.com/google/open-location-code

const OLC_ALPHABET = '23456789CFGHJMPQRVWX'
const OLC_BASE = OLC_ALPHABET.length
const OLC_LAT_MAX = 90
const OLC_LON_MAX = 180

export function toPlusCode(lat, lon, codeLength = 10) {
  const clippedLat = Math.max(-OLC_LAT_MAX, Math.min(OLC_LAT_MAX - 1e-12, lat))
  let normLon = lon
  while (normLon < -OLC_LON_MAX) normLon += 360
  while (normLon >= OLC_LON_MAX) normLon -= 360

  let latVal = clippedLat + OLC_LAT_MAX
  let lonVal = normLon + OLC_LON_MAX
  let latPlace = OLC_LAT_MAX  // halves each pair: 90 → 4.5 → 0.225 → ...
  let lonPlace = OLC_LON_MAX  // 180 → 9 → 0.45 → ...
  let code = ''
  for (let pair = 0; pair < 5; pair++) {
    const latDigit = Math.floor((latVal / latPlace) * OLC_BASE)
    const lonDigit = Math.floor((lonVal / lonPlace) * OLC_BASE)
    const latIdx = Math.max(0, Math.min(OLC_BASE - 1, latDigit))
    const lonIdx = Math.max(0, Math.min(OLC_BASE - 1, lonDigit))
    code += OLC_ALPHABET[latIdx]
    code += OLC_ALPHABET[lonIdx]
    latVal -= (latIdx / OLC_BASE) * latPlace
    lonVal -= (lonIdx / OLC_BASE) * lonPlace
    latPlace /= OLC_BASE
    lonPlace /= OLC_BASE
    if (pair === 3) code += '+'
  }
  return code.slice(0, codeLength + 1)
}

// ── GARS (Global Area Reference System) ─────────────────────────────────────
// 5-character GARS like "354LV" — 30 arc-minute cells.
export function toGARS(lat, lon) {
  const lonCell = Math.floor((lon + 180) * 2) + 1   // 1..720
  const latCell = Math.floor((lat + 90) * 2)          // 0..359
  const lonStr = String(lonCell).padStart(3, '0')
  const ALPHA = 'ABCDEFGHJKLMNPQRSTUVWXYZ'  // skip I, O
  const latStr = ALPHA[Math.floor(latCell / ALPHA.length)] + ALPHA[latCell % ALPHA.length]
  return `${lonStr}${latStr}`
}

// ── MGRS ─────────────────────────────────────────────────────────────────────

function toMGRS(lat, lon) {
  try {
    return forward([lon, lat], 5)
  } catch {
    return `${lat.toFixed(5)}, ${lon.toFixed(5)}`
  }
}

/** MGRS at a chosen precision (digits per axis, 0–5). 0 = 100 km square (GZD + 100 km ID),
 *  1 = 10 km, 2 = 1 km (4-digit), 3 = 100 m, 4 = 10 m, 5 = 1 m. */
export function toMGRSAt(lat, lon, precision = 5) {
  const p = Math.max(0, Math.min(5, Math.round(precision)))
  try {
    return forward([lon, lat], p)
  } catch {
    return `${lat.toFixed(5)}, ${lon.toFixed(5)}`
  }
}

/** Pick an MGRS precision matching a feature's size in metres (use the larger
 *  full-extent axis). 1 km feature → precision 2 (4-digit grid). */
export function mgrsPrecisionForSize(sizeMeters) {
  const m = Number(sizeMeters)
  if (!isFinite(m) || m <= 0) return 5
  if (m >= 100_000) return 0
  if (m >= 10_000)  return 1
  if (m >= 1_000)   return 2
  if (m >= 100)     return 3
  if (m >= 10)      return 4
  return 5
}

// ── Coordinate input parsing ──────────────────────────────────────────────────

/**
 * Parse a coordinate string in the given system.
 * Returns { lat, lon } on success, or null on failure.
 */
export function parseCoordinateInput(value, system) {
  const s = value.trim()
  if (!s) return null

  switch (system) {
    case 'latlon': {
      // "51.5074, -0.1278" or "51.5074 -0.1278"
      const m = s.match(/^(-?\d+\.?\d*)\s*[,\s]\s*(-?\d+\.?\d*)$/)
      if (!m) return null
      const lat = parseFloat(m[1]), lon = parseFloat(m[2])
      if (lat < -90 || lat > 90 || lon < -180 || lon > 180) return null
      return { lat, lon }
    }

    case 'latlon_dms': {
      // "51°30'26.0"N, 0°07'39.0"W" or "51 30 26 N 0 07 39 W"
      const norm = s.replace(/[°'"]/g, ' ').replace(/\s+/g, ' ').trim()
      const m = norm.match(/^(\d+)\s+(\d+)\s+(\d*\.?\d*)\s*([NS])\s*[,\s]\s*(\d+)\s+(\d+)\s+(\d*\.?\d*)\s*([EW])$/i)
      if (!m) return null
      const lat = (parseFloat(m[1]) + parseFloat(m[2]) / 60 + parseFloat(m[3]) / 3600) * (m[4].toUpperCase() === 'S' ? -1 : 1)
      const lon = (parseFloat(m[5]) + parseFloat(m[6]) / 60 + parseFloat(m[7]) / 3600) * (m[8].toUpperCase() === 'W' ? -1 : 1)
      if (lat < -90 || lat > 90 || lon < -180 || lon > 180) return null
      return { lat, lon }
    }

    case 'mgrs': {
      try {
        const [lon, lat] = toPoint(s.replace(/\s+/g, ''))
        if (isNaN(lat) || isNaN(lon)) return null
        return { lat, lon }
      } catch {
        return null
      }
    }

    case 'utm': {
      // "30U 549880 4179733" or "30U 549880E 4179733N"
      const m = s.match(/^(\d{1,2})([A-Z])\s+(\d+)\s*[Ee]?\s+(\d+)\s*[Nn]?$/i)
      if (!m) return null
      const zone = parseInt(m[1])
      const band = m[2].toUpperCase()
      const easting  = parseFloat(m[3])
      const northing = parseFloat(m[4])

      // Reverse simplified UTM → lat/lon
      const k0 = 0.9996, a = 6378137.0, e2 = 0.00669437999014
      const e1 = (1 - Math.sqrt(1 - e2)) / (1 + Math.sqrt(1 - e2))
      const x = easting - 500000
      const y = 'CDEFGHJKLMNPQRSTUVWX'.indexOf(band) < 10 ? northing - 10000000 : northing
      const lonOrig = ((zone - 1) * 6 - 180 + 3) * (Math.PI / 180)
      const M = y / k0
      const mu = M / (a * (1 - e2 / 4 - 3 * e2 ** 2 / 64))
      const phi1 = mu + (3 * e1 / 2 - 27 * e1 ** 3 / 32) * Math.sin(2 * mu)
                     + (21 * e1 ** 2 / 16 - 55 * e1 ** 4 / 32) * Math.sin(4 * mu)
                     + (151 * e1 ** 3 / 96) * Math.sin(6 * mu)
      const N1 = a / Math.sqrt(1 - e2 * Math.sin(phi1) ** 2)
      const T1 = Math.tan(phi1) ** 2
      const C1 = e2 / (1 - e2) * Math.cos(phi1) ** 2
      const R1 = a * (1 - e2) / (1 - e2 * Math.sin(phi1) ** 2) ** 1.5
      const D = x / (N1 * k0)
      const lat = phi1 - (N1 * Math.tan(phi1) / R1) *
        (D ** 2 / 2 - (5 + 3 * T1 + 10 * C1 - 4 * C1 ** 2) * D ** 4 / 24)
      const lon = lonOrig + (D - (1 + 2 * T1 + C1) * D ** 3 / 6) / Math.cos(phi1)
      const latDeg = lat * 180 / Math.PI
      const lonDeg = lon * 180 / Math.PI
      if (isNaN(latDeg) || isNaN(lonDeg)) return null
      if (latDeg < -90 || latDeg > 90 || lonDeg < -180 || lonDeg > 180) return null
      return { lat: latDeg, lon: lonDeg }
    }

    default:
      return null
  }
}

/**
 * Try to parse a coordinate string without knowing the format.
 * Tries MGRS → UTM → DMS → decimal lat/lon in that order (most to least distinctive).
 * Returns { lat, lon, detectedSystem } on success, or null.
 */
export function autoParseCoordinate(value) {
  const order = ['mgrs', 'utm', 'latlon_dms', 'latlon']
  for (const system of order) {
    const result = parseCoordinateInput(value, system)
    if (result) return { ...result, detectedSystem: system }
  }
  return null
}

export function coordInputPlaceholder(system) {
  switch (system) {
    case 'latlon':     return 'e.g. 51.5074, -0.1278'
    case 'latlon_dms': return 'e.g. 51 30 26 N, 0 07 39 W'
    case 'mgrs':       return 'e.g. 30U XC 12345 67890'
    case 'utm':        return 'e.g. 30U 549880 4179733'
    default:           return 'Enter coordinates'
  }
}

// ── UTM ──────────────────────────────────────────────────────────────────────

function toUTM(lat, lon) {
  try {
    const zone = Math.floor((lon + 180) / 6) + 1
    const band = 'CDEFGHJKLMNPQRSTUVWX'[Math.floor((lat + 80) / 8)] || 'Z'
    // Simplified UTM — accurate enough for display
    const lonRad  = (lon * Math.PI) / 180
    const latRad  = (lat * Math.PI) / 180
    const k0 = 0.9996
    const a  = 6378137.0
    const e2 = 0.00669437999014
    const lonOrig = ((zone - 1) * 6 - 180 + 3) * (Math.PI / 180)
    const N = a / Math.sqrt(1 - e2 * Math.sin(latRad) ** 2)
    const T = Math.tan(latRad) ** 2
    const C = (e2 / (1 - e2)) * Math.cos(latRad) ** 2
    const A = Math.cos(latRad) * (lonRad - lonOrig)
    const M = a * (
      (1 - e2 / 4 - 3 * e2 ** 2 / 64) * latRad -
      (3 * e2 / 8 + 3 * e2 ** 2 / 32) * Math.sin(2 * latRad) +
      (15 * e2 ** 2 / 256) * Math.sin(4 * latRad)
    )
    const easting  = k0 * N * (A + (1 - T + C) * A ** 3 / 6) + 500000
    let northing = k0 * (M + N * Math.tan(latRad) * (A ** 2 / 2 + (5 - T + 9 * C) * A ** 4 / 24))
    if (lat < 0) northing += 10000000
    return `${zone}${band} ${Math.round(easting)}E ${Math.round(northing)}N`
  } catch {
    return `${lat.toFixed(5)}, ${lon.toFixed(5)}`
  }
}
