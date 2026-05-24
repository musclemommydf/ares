// SPDX-License-Identifier: MIT OR Apache-2.0
// Copyright (c) 2026 Ares

import { RADAR_TARGETS } from '../../appDefaults'

/** The "RADAR TARGET" picker shown on the Radar tab — sets the radar cross-section
 *  (rcs_m2 on the propagation config) from a list of typical targets. */
export default function RadarTargetPicker({ rcsM2, onSelectRcs }) {
  return (
    <div style={{ borderTop: '1px solid #21262d', padding: '8px 12px' }}>
      <div style={{ fontSize: 11, fontWeight: 600, color: '#8b949e', marginBottom: 8 }}>RADAR TARGET</div>
      <div style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
        {RADAR_TARGETS.map(t => (
          <button
            key={t.rcs}
            className={`btn ${(rcsM2 ?? 1) === t.rcs ? 'btn-primary' : 'btn-secondary'}`}
            style={{ fontSize: 11, textAlign: 'left', justifyContent: 'flex-start' }}
            onClick={() => onSelectRcs(t.rcs)}
          >
            {t.label}
          </button>
        ))}
      </div>
    </div>
  )
}
