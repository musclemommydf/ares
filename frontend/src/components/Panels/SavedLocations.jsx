import { Trash2 } from 'lucide-react'

/**
 * The "Saved Locations" bottom-panel tab — the list of saved places (name + lat/lon),
 * each with a fly-to (⊕) and a remove button. App owns the list and the map fly-to.
 */
export default function SavedLocations({ locations, onFlyTo, onRemove }) {
  return (
    <div style={{ padding: '12px 16px', flex: 1, minHeight: 0, overflowY: 'auto' }}>
      {locations.length === 0 ? (
        <div style={{ fontSize: 12, color: '#484f58', textAlign: 'center', marginTop: 24 }}>
          No saved locations yet. Search for a place on the map and click ★ to save it.
        </div>
      ) : (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
          {locations.map(loc => (
            <div key={loc.id} style={{
              display: 'flex', alignItems: 'center', gap: 8,
              background: '#0d1117', border: '1px solid #21262d', borderRadius: 6, padding: '7px 10px',
            }}>
              <div style={{ flex: 1, minWidth: 0 }}>
                <div style={{ fontSize: 12, color: '#e6edf3', fontWeight: 600, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{loc.name}</div>
                <div style={{ fontSize: 10, color: '#484f58' }}>{loc.lat.toFixed(5)}, {loc.lon.toFixed(5)}</div>
              </div>
              <button className="btn btn-ghost" style={{ padding: '3px 8px', fontSize: 11, flexShrink: 0 }} title="Fly to this location" onClick={() => onFlyTo(loc.lat, loc.lon)}>⊕</button>
              <button className="btn btn-ghost" style={{ padding: '3px 6px', color: '#ef4444', flexShrink: 0 }} title="Remove" onClick={() => onRemove(loc.id)}>
                <Trash2 size={12} />
              </button>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}
