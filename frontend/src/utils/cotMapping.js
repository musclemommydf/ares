// SPDX-License-Identifier: MIT OR Apache-2.0
// Copyright (c) 2026 Ares

/**
 * ATAK / CoT (Cursor on Target) type code → MIL-STD-2525 SIDC mapping.
 *
 * SIDCs are emitted in the 15-char 2525B/C "warfighting" format (here trimmed to the
 * 12-char minimum); milsymbol auto-detects 2525B/C vs 2525D so this feeds it directly.
 *
 * CoT type codes (per the CoT spec) look like:
 *   a-f-G-U-C-I        atom, friendly, Ground, Unit, Combat, Infantry
 *   a-h-G-E-V-A-T      atom, hostile,  Ground, Equipment, Vehicle, Armor, Tank
 *   a-n-A-M-F-Q-A      atom, neutral,  Air,    Military, Fixed-wing, ...
 *
 * Structure:
 *   pos 1: scheme        ('a' = atom; only scheme we map)
 *   pos 2: affiliation   (f/h/n/u/p/s/j/k)
 *   pos 3+: hierarchy    (battle dimension, then sub-types)
 *
 * MIL-STD-2525C SIDC (12-char minimum) layout:
 *   pos 1:    coding scheme (S=warfighting, G=tactical graphic, ...)
 *   pos 2:    affiliation (F/H/N/U/P/S/J/K)
 *   pos 3:    battle dimension (P=land, A=air, S=sea-surface, U=subsurface, ...)
 *   pos 4:    status (P=present, A=anticipated)
 *   pos 5-10: function ID (6 chars)
 *   pos 11-12: padding/modifier
 */

// CoT affiliation → SIDC affiliation digit
const AFF_MAP = {
  f: 'F', // Friend
  a: 'A', // Assumed Friend  (CoT 'a-a-...'; was previously unmapped → those markers fell back to a plain dot)
  h: 'H', // Hostile
  n: 'N', // Neutral
  u: 'U', // Unknown
  p: 'P', // Pending
  s: 'S', // Suspect
  j: 'J', // Joker
  k: 'K', // Faker
  o: 'O', // None Specified
  x: 'O', // Other → None Specified (closest 2525 standard-identity)
}

// CoT dimension → SIDC battle-dimension digit
const DIM_MAP = {
  G: 'G', // Ground (Land in SIDC uses 'G' for tactical or 'P' historically)
  A: 'A', // Air
  S: 'S', // Sea Surface
  U: 'U', // Subsurface
  P: 'P', // Space
  F: 'F', // SOF (Special Operations Forces)
  X: 'X', // Other
}

/**
 * Parse a CoT type into its segments.
 * Returns null if the input is not a recognized CoT type.
 */
export function parseCotType(type) {
  if (!type || typeof type !== 'string') return null
  const segs = type.split('-').filter(Boolean)
  if (segs.length < 3) return null
  if (segs[0] !== 'a') return null   // only "atom" scheme is mappable
  const aff = segs[1]?.toLowerCase()
  const dim = segs[2]?.toUpperCase()
  if (!AFF_MAP[aff] || !DIM_MAP[dim]) return null
  return {
    scheme: segs[0],
    affiliation: aff,
    dimension: dim,
    rest: segs.slice(3).map(s => s.toUpperCase()), // sub-type chars
  }
}

/**
 * Convert a CoT type string to a 12-character MIL-STD-2525C SIDC.
 * Returns null if the CoT type cannot be mapped.
 *
 * The conversion preserves the hierarchy: CoT's per-segment chars after the
 * dimension are concatenated into the SIDC function code (positions 5-10),
 * padded with '-' to 6 chars and trimmed if longer.
 *
 * Example:
 *   a-f-G-U-C-I       → SFGPUCI-----  (Friendly Ground Combat Infantry)
 *   a-h-G-E-V-A-T     → SHGPEVAT----  (Hostile Ground Equipment Vehicle Armor Tank)
 *   a-n-A-M-F         → SNAPMF------
 */
export function cotTypeToSidc(type) {
  const parsed = parseCotType(type)
  if (!parsed) return null
  const aff = AFF_MAP[parsed.affiliation]
  const dim = DIM_MAP[parsed.dimension]
  // Concatenate sub-type chars; each CoT segment after the dimension is a
  // single hierarchy character. Take the first 6, pad with '-'.
  const fnChars = parsed.rest.join('').slice(0, 6).padEnd(6, '-')
  // SIDC layout: S + aff + dim + status (P) + function(6) + modifier(2) = 12 chars
  return `S${aff}${dim}P${fnChars}--`
}

/**
 * Inspect a feature's properties (as produced by @tmcw/togeojson) for a CoT
 * type code coming from ATAK exports. ATAK stores the type in different
 * places depending on the export tool:
 *   - ExtendedData/Data name="type"     → properties.type
 *   - ExtendedData/SimpleData name="type"
 *   - <atom:type> Placemark child       (not parsed by togeojson)
 *   - the description HTML (rare)
 *
 * Returns the raw CoT type string if found, otherwise null.
 */
export function findCotType(properties = {}) {
  const candidates = [
    properties.type,
    properties.cot_type,
    properties.cotType,
    properties['atom:type'],
  ]
  for (const c of candidates) {
    if (typeof c === 'string' && /^a-[a-zA-Z]/.test(c.trim())) return c.trim()
  }
  // Some ATAK exports stash CoT type inside an HTML description block
  const desc = properties.description
  if (typeof desc === 'string') {
    const m = desc.match(/\b(a-[a-zA-Z](?:-[A-Za-z]+)+)\b/)
    if (m) return m[1]
  }
  return null
}

/**
 * Map an ATAK CoT type to a SIDC if possible. Returns the SIDC string
 * (12 characters) ready to feed into milsymbol, or null.
 */
export function cotToSidc(type) {
  return cotTypeToSidc(type)
}
