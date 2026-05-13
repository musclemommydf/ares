/**
 * MANET Planning Panel
 * Place nodes on map, compute P2P between all pairs, show colored links.
 * Controlled component: nodes managed by parent (App.jsx).
 */
import { Network, Plus, Trash2, X } from 'lucide-react'

export default function ManetPanel({
  nodes = [],
  onAddNode,
  onRemoveNode,
  onUpdateNode,
  isSimulating,
}) {
  return (
    <div style={{ padding: '8px 12px', borderTop: '1px solid #21262d' }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 8 }}>
        <Network size={13} color="var(--accent-blue)" />
        <span style={{ fontSize: 11, fontWeight: 600, color: '#8b949e' }}>
          MANET NODES
        </span>
        <span style={{ fontSize: 10, color: '#444d56', flex: 1 }}>
          {nodes.length} node{nodes.length !== 1 ? 's' : ''} · {nodes.length > 1 ? `${nodes.length*(nodes.length-1)/2} links` : '—'}
        </span>
      </div>

      {nodes.length === 0 && (
        <div style={{ fontSize: 11, color: '#444d56', marginBottom: 8 }}>
          Click the map to place MANET nodes. At least 2 nodes required.
        </div>
      )}

      {nodes.map((node, i) => (
        <div key={node.id || i} style={{
          display: 'flex', alignItems: 'center', gap: 6,
          marginBottom: 4, padding: '4px 6px',
          background: '#0d1117', borderRadius: 4, border: '1px solid #21262d',
        }}>
          <div style={{
            width: 8, height: 8, borderRadius: '50%', flexShrink: 0,
            background: '#06d6a0',
          }} />
          <div style={{ flex: 1 }}>
            <input
              value={node.label || `Node ${i + 1}`}
              onChange={e => onUpdateNode?.(node.id || i, { label: e.target.value })}
              style={{
                fontSize: 11, background: 'transparent', border: 'none',
                color: '#c9d1d9', width: '100%', outline: 'none', padding: 0,
              }}
            />
            <div style={{ fontSize: 10, color: '#444d56' }}>
              {node.lat?.toFixed(4)}, {node.lon?.toFixed(4)} · h:{node.height_m ?? 10}m
            </div>
          </div>
          <input
            type="number"
            value={node.height_m ?? 10}
            min={0} max={1000} step={1}
            onChange={e => onUpdateNode?.(node.id || i, { height_m: Number(e.target.value) })}
            style={{
              width: 44, padding: '2px 4px', fontSize: 10,
              background: '#161b22', border: '1px solid #30363d',
              borderRadius: 3, color: '#8b949e', textAlign: 'right',
            }}
            title="Height (m)"
          />
          <span style={{ fontSize: 9, color: '#444d56' }}>m</span>
          <button
            className="btn btn-ghost"
            style={{ padding: '1px 4px', color: '#ef4444' }}
            onClick={() => onRemoveNode?.(node.id || i)}
          >
            <X size={11} />
          </button>
        </div>
      ))}

      <div style={{ display: 'flex', gap: 6, marginTop: 4 }}>
        <button
          className="btn btn-secondary"
          style={{ flex: 1, fontSize: 11, gap: 4 }}
          onClick={onAddNode}
        >
          <Plus size={11} /> Add Node (click map)
        </button>
        {nodes.length > 0 && (
          <button
            className="btn btn-ghost"
            style={{ fontSize: 11, gap: 4, color: '#ef4444' }}
            onClick={() => nodes.forEach(n => onRemoveNode?.(n.id || 0))}
          >
            <Trash2 size={11} /> Clear All
          </button>
        )}
      </div>

      <div style={{ fontSize: 10, color: '#484f58', marginTop: 8 }}>Run Simulation → connectivity + per-link results show in the bottom <strong>Results</strong> tab.</div>
    </div>
  )
}
