/**
 * Saved Results — a localStorage-backed catalog of simulation snapshots, lives
 * inside the Layer Manager. Each entry stores the result GeoJSON *and* the input
 * params (tab / TX / propagation), so loading reproduces the calculation: it
 * restores those settings and re-renders the result on the map.
 *
 * Storage key stays 'ares-archive' (legacy 'rf-sim-archive' migrated on read) so
 * results saved by the old Archive panel carry over.
 */
import { useState, useEffect, useCallback } from 'react'
import { Save, FolderOpen, Trash2, Download, ChevronDown, ChevronRight } from 'lucide-react'

const STORE_KEY = 'ares-archive'
const LEGACY_KEY = 'rf-sim-archive'

function loadStore() {
  try {
    let raw = localStorage.getItem(STORE_KEY)
    if (!raw) {
      const legacy = localStorage.getItem(LEGACY_KEY)
      if (legacy) {
        localStorage.setItem(STORE_KEY, legacy)
        localStorage.removeItem(LEGACY_KEY)
        raw = legacy
      }
    }
    return raw ? JSON.parse(raw) : []
  } catch {
    return []
  }
}

function persist(entries) {
  try {
    localStorage.setItem(STORE_KEY, JSON.stringify(entries))
  } catch {
    console.error('Saved results: localStorage write failed — storage may be full')
  }
}

const typeColor = (type) => {
  if (type === 'p2p')       return '#a855f7'
  if (type === 'best_site') return '#f59e0b'
  if (type === 'manet')     return '#06d6a0'
  if (type === 'route')     return '#00b4d8'
  if (type === 'ray_trace') return '#ef4444'
  return '#8b949e'
}

export default function SavedResultsPanel({ currentGeojson, currentParams, onLoad }) {
  const [entries, setEntries] = useState([])
  const [saveName, setSaveName] = useState('')
  const [saveProject, setSaveProject] = useState('')
  const [expandedId, setExpandedId] = useState(null)

  useEffect(() => { setEntries(loadStore()) }, [])

  const handleSave = useCallback(() => {
    if (!saveName.trim() || !currentGeojson) return
    const entry = {
      id:        Date.now().toString(),
      name:      saveName.trim(),
      network:   saveProject.trim() || 'Default',
      type:      currentParams?.type || 'coverage',
      timestamp: new Date().toISOString(),
      params:    currentParams || {},
      geojson:   currentGeojson || null,
      metadata:  { point_count: currentGeojson?.features?.length || 0 },
    }
    const updated = [...entries, entry]
    setEntries(updated); persist(updated)
    setSaveName('')
  }, [saveName, saveProject, currentGeojson, currentParams, entries])

  const handleDelete = useCallback((id) => {
    const updated = entries.filter(e => e.id !== id)
    setEntries(updated); persist(updated)
  }, [entries])

  const handleExport = useCallback((entry) => {
    if (!entry.geojson) return
    const blob = new Blob([JSON.stringify(entry.geojson, null, 2)], { type: 'application/geo+json' })
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = `${entry.name.replace(/\s+/g, '_')}_${entry.id}.geojson`
    a.click()
    URL.revokeObjectURL(url)
  }, [])

  // Group by project / network
  const groups = {}
  for (const e of entries) {
    const net = e.network || 'Default'
    if (!groups[net]) groups[net] = []
    groups[net].push(e)
  }

  return (
    <div style={{ padding: '10px 14px', borderBottom: '1px solid #21262d', background: '#0d1117', flexShrink: 0 }}>
      <div style={{ fontSize: 10, fontWeight: 700, color: '#8b949e', marginBottom: 8, letterSpacing: 0.6 }}>
        SAVE CURRENT RESULT
      </div>
      {!currentGeojson && (
        <div style={{ fontSize: 11, color: '#444d56', marginBottom: 6 }}>
          Run a simulation first to save its result.
        </div>
      )}
      <div style={{ display: 'flex', gap: 6, marginBottom: 4 }}>
        <input
          value={saveName}
          onChange={e => setSaveName(e.target.value)}
          onKeyDown={e => e.key === 'Enter' && handleSave()}
          placeholder="Name (required)"
          style={inputStyle} />
        <input
          value={saveProject}
          onChange={e => setSaveProject(e.target.value)}
          placeholder="Project"
          style={{ ...inputStyle, flex: '0 0 110px' }} />
        <button className="btn btn-primary"
          style={{ padding: '4px 12px', fontSize: 12, gap: 4, flexShrink: 0 }}
          disabled={!saveName.trim() || !currentGeojson}
          onClick={handleSave}>
          <Save size={12} /> Save
        </button>
      </div>

      {entries.length === 0 ? (
        <div style={{ fontSize: 11, color: '#444d56', padding: '8px 0 2px' }}>
          No saved results yet.
        </div>
      ) : (
        <div style={{ maxHeight: 220, overflowY: 'auto', marginTop: 8 }}>
          {Object.entries(groups).map(([net, netEntries]) => (
            <div key={net} style={{ marginBottom: 8 }}>
              <div style={{
                fontSize: 10, fontWeight: 700, color: '#8b949e', textTransform: 'uppercase',
                letterSpacing: 1, marginBottom: 4, padding: '2px 0', borderBottom: '1px solid #21262d',
              }}>{net}</div>
              {netEntries.map(entry => (
                <div key={entry.id} style={{ background: '#11161d', border: '1px solid #21262d', borderRadius: 6, marginBottom: 4, overflow: 'hidden' }}>
                  <div style={{ display: 'flex', alignItems: 'center', gap: 8, padding: '6px 8px', cursor: 'pointer' }}
                       onClick={() => setExpandedId(expandedId === entry.id ? null : entry.id)}>
                    {expandedId === entry.id ? <ChevronDown size={12} color="#8b949e" /> : <ChevronRight size={12} color="#8b949e" />}
                    <span style={{ width: 6, height: 6, borderRadius: '50%', flexShrink: 0, background: typeColor(entry.type) }} />
                    <span style={{ fontSize: 12, color: '#e6edf3', flex: 1, overflow: 'hidden', whiteSpace: 'nowrap', textOverflow: 'ellipsis' }}>{entry.name}</span>
                    <span style={{ fontSize: 10, color: typeColor(entry.type), background: typeColor(entry.type) + '22', padding: '1px 5px', borderRadius: 3 }}>{entry.type}</span>
                  </div>
                  {expandedId === entry.id && (
                    <div style={{ padding: '4px 10px 8px 26px', borderTop: '1px solid #21262d' }}>
                      <div style={{ fontSize: 11, color: '#8b949e', marginBottom: 6 }}>
                        {entry.metadata?.point_count || 0} features · {new Date(entry.timestamp).toLocaleString()}
                      </div>
                      <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap' }}>
                        <button className="btn btn-secondary" style={{ fontSize: 11, gap: 4, padding: '3px 8px' }}
                          title="Restore this result's settings (tab / TX / propagation) and re-render it on the map"
                          onClick={() => onLoad?.(entry)}>
                          <FolderOpen size={11} /> Load
                        </button>
                        <button className="btn btn-ghost" style={{ fontSize: 11, gap: 4, padding: '3px 8px' }}
                          disabled={!entry.geojson} onClick={() => handleExport(entry)}>
                          <Download size={11} /> GeoJSON
                        </button>
                        <button className="btn btn-ghost" style={{ fontSize: 11, gap: 4, padding: '3px 8px', color: '#ef4444' }}
                          onClick={() => handleDelete(entry.id)}>
                          <Trash2 size={11} /> Delete
                        </button>
                      </div>
                    </div>
                  )}
                </div>
              ))}
            </div>
          ))}
        </div>
      )}
    </div>
  )
}

const inputStyle = {
  flex: 1, padding: '4px 8px', fontSize: 12, background: '#161b22',
  border: '1px solid #30363d', borderRadius: 4, color: '#e6edf3', outline: 'none',
  minWidth: 0, boxSizing: 'border-box',
}
