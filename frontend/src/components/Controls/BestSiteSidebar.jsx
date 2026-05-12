import { X, Plus } from 'lucide-react'

/**
 * The sidebar control for the Best-Site tab: the list of candidate sites (each
 * showing its covered-area once a result is in), an "Add from TX" button, and the
 * ranking once Best-Site has run. App owns the candidate list + the TX it offsets
 * the new candidate from.
 */
export default function BestSiteSidebar({ candidates, result, onRemove, onAddFromTx }) {
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
          {result?.sites && (() => {
            const s = result.sites.find(s => Math.abs(s.lat - c.lat) < 0.0001)
            return s ? <span style={{ fontSize: 10, color: '#06d6a0' }}>{s.covered_area_km2} km²</span> : null
          })()}
          <button className="btn btn-ghost" style={{ padding: '1px 4px', color: '#ef4444' }} onClick={() => onRemove(i)}>
            <X size={11} />
          </button>
        </div>
      ))}
      <button className="btn btn-secondary" style={{ width: '100%', gap: 6, fontSize: 11, marginTop: 4 }} onClick={onAddFromTx}>
        <Plus size={12} /> Add from TX
      </button>
      {result?.sites && (
        <div style={{ marginTop: 8 }}>
          <div style={{ fontSize: 11, fontWeight: 600, color: '#8b949e', marginBottom: 4 }}>RANKING</div>
          {result.sites.map((s, i) => (
            <div key={i} style={{
              display: 'flex', justifyContent: 'space-between',
              fontSize: 11, padding: '2px 0', borderBottom: '1px solid #21262d',
            }}>
              <span style={{ color: i === 0 ? '#06d6a0' : '#c9d1d9' }}>{i + 1}. {s.label}</span>
              <span style={{ color: '#8b949e' }}>{s.covered_area_km2} km² · {s.avg_signal_dbm} dBm</span>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}
