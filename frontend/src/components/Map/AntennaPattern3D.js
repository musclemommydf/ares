/**
 * AntennaPattern3D — render the 3-dB beamwidth + first nulls of a DF antenna
 * as a translucent Cesium entity attached to an observer position.
 *
 * Two modes:
 *   - "omni"          → full ring (UCA / vertical monopole; no preferred axis)
 *   - "directional"   → main-lobe ellipse along the antenna heading, with a
 *                       front/back distinction (nulls on the sides).
 *
 * The shape is *visualisation-only*: it's a rough cardioid / circular lobe to
 * give the operator a quick sense of "which direction is this DF rig
 * pointing?", not a literal radiation pattern. Real arrays' patterns can be
 * loaded from a per-antenna .json (the antenna database files) and we can
 * later render those exactly the same way with sampled radii — the helper
 * below accepts either a `hpbw_deg` (cardioid model) or an explicit
 * `pattern_db: [{az, gain_db}]` array of samples.
 */
import * as Cesium from 'cesium'

const DEG = Math.PI / 180

function cardioidRadiusM(hpbwDeg, azFromHeadingDeg, lobeRadiusM = 800) {
  // Simple cardioid: r(θ) = R · (0.5 + 0.5·cos(θ))^n; n chosen so half-power = hpbw/2.
  // For an HPBW of θ° we solve (0.5 + 0.5·cos(θ/2))^n = 0.5  →  n = log2(0.5+0.5·cos(θ/2))^-1.
  const halfRad = (hpbwDeg / 2) * DEG
  const denom = Math.log2(Math.max(1e-6, 0.5 + 0.5 * Math.cos(halfRad)))
  const n = -1 / (denom || -1)
  const c = Math.max(0, 0.5 + 0.5 * Math.cos(azFromHeadingDeg * DEG))
  return lobeRadiusM * Math.pow(c, n)
}

/** Build a Cesium polygon hierarchy for an antenna lobe at (lat, lon).
 *  Returns an entity options object: `viewer.entities.add(addAntennaLobe({...}))`. */
export function buildAntennaLobeEntity({
  lat, lon,
  headingDeg = 0,
  hpbwDeg = 60,
  pattern = null,                 // optional [{az_deg, gain_db}] override
  lobeRadiusM = 800,
  color = '#a78bfa',
  altitudeM = 5,
  omni = false,
  label = '',
}) {
  const positions = []
  for (let a = 0; a < 360; a += 4) {
    let r
    if (omni) {
      r = lobeRadiusM
    } else if (pattern && pattern.length) {
      // Linear-interpolate the sampled pattern (relative to heading)
      const rel = ((a - headingDeg) + 360) % 360
      const i = Math.floor(rel)
      const a0 = pattern[i % pattern.length]
      const a1 = pattern[(i + 1) % pattern.length]
      const g = (a0?.gain_db ?? 0 + (a1?.gain_db ?? 0)) / 2
      const lin = Math.pow(10, g / 10)
      r = lobeRadiusM * Math.sqrt(lin)
    } else {
      r = cardioidRadiusM(hpbwDeg, ((a - headingDeg) + 540) % 360 - 180, lobeRadiusM)
    }
    if (r <= 0.1) continue
    // Project on the ellipsoid: bearing = a (true), distance = r
    const dest = destinationPoint(lat, lon, a, r)
    positions.push(Cesium.Cartesian3.fromDegrees(dest.lon, dest.lat, altitudeM))
  }
  // Close ring
  if (positions.length > 2) positions.push(positions[0])
  const c = Cesium.Color.fromCssColorString(color).withAlpha(0.25)
  return {
    name: label || 'antenna lobe',
    polygon: {
      hierarchy: new Cesium.PolygonHierarchy(positions),
      material: c,
      outline: true,
      outlineColor: Cesium.Color.fromCssColorString(color).withAlpha(0.8),
      perPositionHeight: true,
    },
  }
}

function destinationPoint(lat, lon, bearingDeg, distanceM) {
  const R = 6371000
  const d = distanceM / R
  const θ = bearingDeg * DEG
  const φ1 = lat * DEG, λ1 = lon * DEG
  const φ2 = Math.asin(Math.sin(φ1) * Math.cos(d) + Math.cos(φ1) * Math.sin(d) * Math.cos(θ))
  const λ2 = λ1 + Math.atan2(Math.sin(θ) * Math.sin(d) * Math.cos(φ1), Math.cos(d) - Math.sin(φ1) * Math.sin(φ2))
  return { lat: φ2 / DEG, lon: ((λ2 / DEG) + 540) % 360 - 180 }
}
