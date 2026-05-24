// SPDX-License-Identifier: MIT OR Apache-2.0
// Copyright (c) 2026 Ares

import { MapPin } from 'lucide-react'

/**
 * The sidebar control for the Multipoint tab: click / clear the TX candidate
 * locations (each is tested against a fixed receiver), with the running count.
 * App owns the TX-points list + the draw mode.
 */
export default function MultipointSidebar({ drawMode, txPoints, onToggleDraw, onClear }) {
  return (
    <div style={{ borderTop: '1px solid #21262d', padding: '8px 12px' }}>
      <div style={{ fontSize: 11, fontWeight: 600, color: '#8b949e', marginBottom: 6 }}>MULTIPOINT ANALYSIS</div>
      <div style={{ fontSize: 11, color: '#444d56', marginBottom: 8 }}>
        Click multiple TX candidate locations. Each is tested against a fixed receiver.
      </div>
      <div style={{ display: 'flex', gap: 6, marginBottom: 8 }}>
        <button className={`btn ${drawMode === 'multipoint' ? 'btn-primary' : 'btn-secondary'}`} style={{ flex: 1, fontSize: 11, gap: 4 }} onClick={onToggleDraw}>
          <MapPin size={11} />
          {drawMode === 'multipoint' ? 'Clicking… (right-click to finish)' : 'Click TX Points'}
        </button>
        {txPoints.length > 0 && (
          <button className="btn btn-ghost" style={{ fontSize: 11, color: '#ef4444' }} onClick={onClear}>Clear</button>
        )}
      </div>
      {txPoints.length > 0 && (
        <div style={{ fontSize: 11, color: '#8b949e', marginBottom: 6 }}>{txPoints.length} TX points</div>
      )}
    </div>
  )
}
