// SPDX-License-Identifier: MIT OR Apache-2.0
// Copyright (c) 2026 Ares

import { X, Plus } from 'lucide-react'

/**
 * Sidebar inputs for the Best-Site tab: the list of candidate sites (drop them by clicking the
 * map, or "Add from TX") and the remove buttons. The *result* — the ranking — is rendered in the
 * bottom Results tab (see <AnalysisResults>), not here.
 */
export default function BestSiteSidebar({ candidates, onRemove, onAddFromTx }) {
  return (
    <div style={{ borderTop: '1px solid #21262d', padding: '8px 12px' }}>
      <div style={{ fontSize: 11, fontWeight: 600, color: '#8b949e', marginBottom: 8 }}>CANDIDATE SITES</div>
      {candidates.length === 0 && (
        <div style={{ fontSize: 11, color: '#444d56', marginBottom: 8 }}>
          Click the map to add candidate sites. At least 2 required.
        </div>
      )}
      {candidates.map((c, i) => (
        <div key={i} style={{
          display: 'flex', alignItems: 'center', gap: 6,
          marginBottom: 4, padding: '4px 6px',
          background: '#0d1117', borderRadius: 4, border: '1px solid #21262d',
        }}>
          <div style={{ width: 8, height: 8, borderRadius: '50%', background: '#f59e0b', flexShrink: 0 }} />
          <div style={{ flex: 1, fontSize: 11, color: '#c9d1d9' }}>
            {c.label || `Site ${i + 1}`}
            <span style={{ color: '#444d56', marginLeft: 4 }}>{c.lat.toFixed(4)}, {c.lon.toFixed(4)}</span>
          </div>
          <button className="btn btn-ghost" style={{ padding: '1px 4px', color: '#ef4444' }} onClick={() => onRemove(i)}>
            <X size={11} />
          </button>
        </div>
      ))}
      <button className="btn btn-secondary" style={{ width: '100%', gap: 6, fontSize: 11, marginTop: 4 }} onClick={onAddFromTx}>
        <Plus size={12} /> Add from TX
      </button>
      <div style={{ fontSize: 10, color: '#484f58', marginTop: 6 }}>Run Simulation → ranking shows in the bottom <strong>Results</strong> tab.</div>
    </div>
  )
}
