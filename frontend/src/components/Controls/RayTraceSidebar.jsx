// SPDX-License-Identifier: MIT OR Apache-2.0
// Copyright (c) 2026 Ares

/**
 * The sidebar info panel for the 3D-Ray tab — describes the ray-trace and shows the
 * current TX position & frequency it'll use. No interactive controls here; the header
 * Run button does the work.
 */
export default function RayTraceSidebar({ tx }) {
  return (
    <div style={{ borderTop: '1px solid #21262d', padding: '8px 12px' }}>
      <div style={{ fontSize: 11, fontWeight: 600, color: '#8b949e', marginBottom: 6 }}>3D RAY TRACING</div>
      <div style={{ fontSize: 11, color: '#444d56', marginBottom: 8 }}>
        Traces rays from TX, finds terrain intersections, computes Fresnel reflection and
        single-bounce contributions. Uses current TX position and frequency.
      </div>
      <div style={{ fontSize: 11, color: '#8b949e', marginBottom: 6 }}>
        TX: {tx.lat.toFixed(4)}, {tx.lon.toFixed(4)} · f: {(tx.frequency_hz / 1e6).toFixed(1)} MHz
      </div>
    </div>
  )
}
