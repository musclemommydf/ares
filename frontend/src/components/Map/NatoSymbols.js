// SPDX-License-Identifier: MIT OR Apache-2.0
// Copyright (c) 2026 Ares

/**
 * NATO / MIL-STD-2525C / APP-6 symbology helpers.
 *
 * Provides:
 *   - A curated catalogue of common SIDC (Symbol Identification Code) presets
 *     covering ATAK / Ranger Handbook–level operational graphics.
 *   - `makeSidcIcon(sidc, opts)` → Leaflet divIcon rendered via milsymbol.
 *   - `applyAffiliation(sidc, aff)` → swaps the affiliation digit.
 *
 * SIDC convention used in this catalogue: every preset has '*' in position 2
 * (the affiliation digit) which is replaced at render time by F/H/N/U.
 * SIDCs are 12 characters (MIL-STD-2525C minimum); milsymbol accepts and
 * renders these directly.
 */
import L from 'leaflet'
import ms from 'milsymbol'

// milsymbol 3.x ships its default export as an object with a `Symbol`
// constructor (alongside helpers like setColorMode / addSymbolPart). Some
// bundlers/CDNs ESM-default-interop a different shape, so we look in both
// likely places — the named `ms.Symbol` first, and the default itself if it
// looks like a constructor.
const MilSymbol = (ms && ms.Symbol) || ms

// SIDC affiliation digit (position 2): F = friend, H = hostile, N = neutral,
// U = unknown, P = pending, A = assumed friend, S = suspect
export const AFFILIATIONS = [
  { id: 'F', label: 'Friend',  color: '#3b82f6' },
  { id: 'H', label: 'Hostile', color: '#ef4444' },
  { id: 'N', label: 'Neutral', color: '#22c55e' },
  { id: 'U', label: 'Unknown', color: '#facc15' },
]

// Curated catalogue. SIDCs verified against milsymbol 3.0.4 — every entry
// here renders a recognizable glyph (not the fallback "?" frame).
export const SYMBOL_CATALOGUE = {
  Units: [
    { sidc: 'S*GPUCI-----', name: 'Infantry' },
    { sidc: 'S*GPUCIM----', name: 'Mech. Infantry' },
    { sidc: 'S*GPUCIA----', name: 'Airborne Inf.' },
    { sidc: 'S*GPUCIS----', name: 'Sniper Team' },
    { sidc: 'S*GPUCR-----', name: 'Recon' },
    { sidc: 'S*GPUCAA----', name: 'Air Defence' },
    { sidc: 'S*GPUCF-----', name: 'Field Artillery' },
    { sidc: 'S*GPUCFR----', name: 'Rocket Arty' },
    { sidc: 'S*GPUCFM----', name: 'Mortar' },
    { sidc: 'S*GPUCE-----', name: 'Engineer' },
    { sidc: 'S*GPUUS-----', name: 'Signal' },
    { sidc: 'S*GPUCM-----', name: 'Med. Corps' },
    { sidc: 'S*GPUUL-----', name: 'Mil. Police' },
    { sidc: 'S*GPUUE-----', name: 'EOD' },
    { sidc: 'S*FPGS------', name: 'Spec. Forces' },
    { sidc: 'S*GPUH------', name: 'HQ' },
  ],
  Equipment: [
    { sidc: 'S*GPEVAT----', name: 'Tank' },
    { sidc: 'S*GPEVAA----', name: 'APC' },
    { sidc: 'S*GPEVUR----', name: 'Recon Vehicle' },
    { sidc: 'S*GPEWA-----', name: 'Anti-tank' },
    { sidc: 'S*GPEVUL----', name: 'Truck' },
    { sidc: 'S*APMHA-----', name: 'Attack Helicopter' },
    { sidc: 'S*APMFF-----', name: 'Fighter' },
    { sidc: 'S*APMFB-----', name: 'Bomber' },
    { sidc: 'S*SPCL------', name: 'Naval Surface' },
  ],
  Tactical: [
    // Tactical graphics — points
    { sidc: 'G*GPGPP-----', name: 'Point of Interest' },
    { sidc: 'G*GPGPRD----', name: 'Drop Point' },
    { sidc: 'G*GPGPRP----', name: 'Pickup Zone' },
    { sidc: 'G*GPGPPS----', name: 'Cache (Resupply)' },
    { sidc: 'G*GPGPRC----', name: 'Rendezvous' },
    { sidc: 'G*GPGPRS----', name: 'Reference Point' },
    { sidc: 'G*GPGPH-----', name: 'Helo Point' },
    { sidc: 'G*GPAPP-----', name: 'Air Control Pt.' },
    { sidc: 'G*GPGPPC----', name: 'Contact Point' },
    { sidc: 'G*GPAPC-----', name: 'Coord. Point' },
    { sidc: 'G*GPGPPK----', name: 'Check Point' },
  ],
  'Combat Service': [
    { sidc: 'S*GPUSX-----', name: 'Maintenance' },
    { sidc: 'S*GPUST-----', name: 'Transportation' },
    { sidc: 'S*GPUSS-----', name: 'Supply' },
    { sidc: 'S*GPUSMT----', name: 'Med. Treatment' },
    { sidc: 'S*GPUSMV----', name: 'CASEVAC' },
    { sidc: 'S*GPUSAS----', name: 'Refuel Point' },
  ],
  CBRN: [
    { sidc: 'S*GPUUABR---', name: 'CBRN Defence' },
    { sidc: 'E*NZ--------', name: 'Contam. Zone' },
    { sidc: 'E*NAB-------', name: 'Biological' },
    { sidc: 'E*NAC-------', name: 'Chemical' },
    { sidc: 'O*VN--------', name: 'Nuclear' },
    { sidc: 'O*VR--------', name: 'Radiological' },
  ],
  'Ranger Handbook': [
    { sidc: 'G*GPGPP-----', name: 'OBJ (Objective)' },
    { sidc: 'G*GPGPRP----', name: 'PZ' },
    { sidc: 'G*GPGPRD----', name: 'LZ' },
    { sidc: 'G*GPGPPK----', name: 'CP (Check Point)' },
    { sidc: 'G*GPGPPR----', name: 'Rally Point' },
    { sidc: 'G*GPGPPE----', name: 'Release Point' },
    { sidc: 'G*GPGPRS----', name: 'OP / LP' },
    { sidc: 'G*GPAPP-----', name: '9-Line CAS (ACP)' },
    { sidc: 'G*GPGPH-----', name: 'HLZ' },
  ],
  IADS: [
    { sidc: 'S*GPUCAA----', name: 'Air Defence Unit' },
    { sidc: 'S*GPUCAAM---', name: 'SAM Site' },
    { sidc: 'S*GPUCAAG---', name: 'AAA Site' },
    { sidc: 'S*GPEWMS----', name: 'SAM Launcher' },
    { sidc: 'S*GPEWAH----', name: 'AAA Gun' },
    { sidc: 'S*GPUUMR----', name: 'Surveillance Radar' },
    { sidc: 'S*GPUUMSE---', name: 'EW / Jammer' },
    { sidc: 'S*GPUUS-----', name: 'Signal unit' },
    { sidc: 'S*GPUH------', name: 'AD Sector HQ' },
    { sidc: 'S*APMFF-----', name: 'Interceptor' },
    { sidc: 'S*APMFA-----', name: 'AEW&C' },
  ],
  'Special Operations': [
    { sidc: 'S*FPGS------', name: 'Special Forces' },
    { sidc: 'S*FPGR------', name: 'Ranger' },
    { sidc: 'S*FPGP------', name: 'PSYOP' },
    { sidc: 'S*FPGC------', name: 'Civil Affairs' },
    { sidc: 'S*FPN-------', name: 'Naval SOF' },
    { sidc: 'S*FPNS------', name: 'SEAL' },
    { sidc: 'S*FPNB------', name: 'Special Boat Team' },
    { sidc: 'S*FPNU------', name: 'SDV Team' },
    { sidc: 'S*FPA-------', name: 'SOF Air' },
    { sidc: 'S*FPAH------', name: 'SOF Helo' },
    { sidc: 'S*GPUCR-----', name: 'Recon / SR Team' },
    { sidc: 'S*GPUCIS----', name: 'Sniper Team' },
  ],
}

// Build a flat search index for fuzzy lookup
export const SYMBOL_INDEX = Object.entries(SYMBOL_CATALOGUE).flatMap(
  ([category, items]) => items.map(it => ({ ...it, category }))
)

// Replace the affiliation digit (index 1) in a SIDC.
// Works for warfighting (S), tactical graphic (G), emergency mgmt (E),
// and operations (O) schemes — affiliation always lives at position 2.
export function applyAffiliation(sidc, aff = 'F') {
  if (!sidc || sidc.length < 2) return sidc
  return sidc[0] + (aff || 'F') + sidc.slice(2)
}

// Cache rendered SVGs to keep things snappy when the user scrolls a long list
const _renderCache = new Map()

function renderSymbolSVG(sidc, opts = {}) {
  const key = `${sidc}|${opts.size || 30}|${opts.label || ''}|${opts.uniqueDesignation || ''}`
  if (_renderCache.has(key)) return _renderCache.get(key)
  try {
    const sym = new MilSymbol(sidc, {
      size: opts.size || 30,
      uniqueDesignation: opts.uniqueDesignation || '',
      additionalInformation: opts.label || '',
      infoFields: true,
    })
    const svg = sym.asSVG()
    const anchor = sym.getAnchor()
    const dim = sym.getSize()
    const out = { svg, anchor, dim }
    _renderCache.set(key, out)
    return out
  } catch (e) {
    // milsymbol throws on invalid SIDC — fall back to a plain question-mark pin
    const out = {
      svg: `<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24"><circle cx="12" cy="12" r="10" fill="#f59e0b"/><text x="12" y="17" font-size="14" text-anchor="middle" fill="#000">?</text></svg>`,
      anchor: { x: 12, y: 12 },
      dim: { width: 24, height: 24 },
    }
    _renderCache.set(key, out)
    return out
  }
}

export function makeSidcIcon(sidc, opts = {}) {
  const { svg, anchor, dim } = renderSymbolSVG(sidc, opts)
  return L.divIcon({
    className: 'mv-mil-symbol',
    html: svg,
    iconSize: [Math.round(dim.width), Math.round(dim.height)],
    iconAnchor: [Math.round(anchor.x), Math.round(anchor.y)],
  })
}

// Render an SVG snippet for use in toolbar buttons / palette cells
export function renderSymbolPreview(sidc, sizePx = 28) {
  const { svg } = renderSymbolSVG(sidc, { size: sizePx })
  return svg
}
