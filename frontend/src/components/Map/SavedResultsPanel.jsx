// SPDX-License-Identifier: MIT OR Apache-2.0
// Copyright (c) 2026 Ares

/**
 * Saved Results — a durable, server-side catalog of simulation snapshots, shown
 * inside the Layer Manager. Each entry stores the result GeoJSON, the input
 * params (tab / TX / propagation), and the Results-panel state, so loading
 * reproduces the calculation: it restores those settings and re-renders it.
 *
 * Backed by the backend SQLite store (/results) — shared across browsers/devices
 * and surviving restart. On first run it migrates any entries from the legacy
 * browser-localStorage Archive ('ares-archive') to the server, then clears them.
 */
import { useState, useEffect, useCallback } from 'react'
import { Save, FolderOpen, Trash2, Download, ChevronDown, ChevronRight, RefreshCw } from 'lucide-react'
import { listSavedResults, getSavedResult, saveSavedResult, deleteSavedResult } from '../../api/client'

const LEGACY_KEYS = ['ares-archive', 'rf-sim-archive']
const MIGRATED_FLAG = 'ares-results-migrated'

const typeColor = (type) => {
  if (type === 'p2p')       return '#a855f7'
  if (type === 'best_site') return '#f59e0b'
  if (type === 'manet')     return '#06d6a0'
  if (type === 'route')     return '#00b4d8'
  if (type === 'ray_trace') return '#ef4444'
  return '#8b949e'
}

// One-time: push any legacy localStorage Archive entries to the server, then clear.
async function migrateLegacy() {
  if (localStorage.getItem(MIGRATED_FLAG)) return 0
  let migrated = 0
  for (const key of LEGACY_KEYS) {
    let raw
    try { raw = localStorage.getItem(key) } catch { raw = null }
    if (!raw) continue
    let entries
    try { entries = JSON.parse(raw) } catch { entries = [] }
    for (const e of entries || []) {
      try {
        await saveSavedResult({
          id: e.id, name: e.name || 'Imported', project: e.network || e.project || 'Default',
          type: e.type || 'coverage', params: e.params || {}, results: e.results || {},
          geojson: e.geojson || null,
          created: e.timestamp ? Date.parse(e.timestamp) / 1000 : undefined,
        })
        migrated++
      } catch { /* skip a bad entry, keep going */ }
    }
    try { localStorage.removeItem(key) } catch { /* ignore */ }
  }
  try { localStorage.setItem(MIGRATED_FLAG, '1') } catch { /* ignore */ }
  return migrated
}

export default function SavedResultsPanel({ currentGeojson, currentParams, currentExtras, onLoad }) {
  const [entries, setEntries] = useState([])
  const [saveName, setSaveName] = useState('')
  const [saveProject, setSaveProject] = useState('')
  const [expandedId, setExpandedId] = useState(null)
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState('')

  const refresh = useCallback(async () => {
    setErr('')
    try { setEntries(await listSavedResults()) }
    catch (e) { setErr(e?.message || 'failed to load saved results') }
  }, [])

  useEffect(() => {
    (async () => {
      try { await migrateLegacy() } catch { /* ignore */ }
      await refresh()
    })()
  }, [refresh])

  const handleSave = useCallback(async () => {
    if (!saveName.trim() || !currentGeojson) return
    setBusy(true); setErr('')
    try {
      await saveSavedResult({
        name: saveName.trim(), project: saveProject.trim() || 'Default',
        type: currentParams?.type || 'coverage',
        params: currentParams || {}, results: currentExtras || {}, geojson: currentGeojson || null,
      })
      setSaveName('')
      await refresh()
    } catch (e) { setErr(e?.message || 'save failed') }
    finally { setBusy(false) }
  }, [saveName, saveProject, currentGeojson, currentParams, currentExtras, refresh])

  const handleDelete = useCallback(async (id) => {
    setBusy(true)
    try { await deleteSavedResult(id); await refresh() }
    catch (e) { setErr(e?.message || 'delete failed') }
    finally { setBusy(false) }
  }, [refresh])

  const handleLoad = useCallback(async (id) => {
    setBusy(true); setErr('')
    try { const full = await getSavedResult(id); onLoad?.(full) }
    catch (e) { setErr(e?.message || 'load failed') }
    finally { setBusy(false) }
  }, [onLoad])

  const handleExport = useCallback(async (entry) => {
    setBusy(true)
    try {
      const full = await getSavedResult(entry.id)
      if (!full?.geojson) return
      const blob = new Blob([JSON.stringify(full.geojson, null, 2)], { type: 'application/geo+json' })
      const url = URL.createObjectURL(blob)
      const a = document.createElement('a')
      a.href = url
      a.download = `${(entry.name || 'result').replace(/\s+/g, '_')}_${entry.id}.geojson`
      a.click()
      URL.revokeObjectURL(url)
    } catch (e) { setErr(e?.message || 'export failed') }
    finally { setBusy(false) }
  }, [])

  // Group by project
  const groups = {}
  for (const e of entries) {
    const net = e.project || 'Default'
    if (!groups[net]) groups[net] = []
    groups[net].push(e)
  }

  return (
    <div style={{ padding: '10px 14px', borderBottom: '1px solid #21262d', background: '#0d1117', flexShrink: 0 }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 6, marginBottom: 8 }}>
        <span style={{ fontSize: 10, fontWeight: 700, color: '#8b949e', letterSpacing: 0.6, flex: 1 }}>
          SAVE CURRENT RESULT
        </span>
        <button className="btn btn-ghost" style={{ padding: '2px 6px', fontSize: 10 }} title="Refresh"
          onClick={refresh} disabled={busy}><RefreshCw size={11} /></button>
      </div>
      {!currentGeojson && (
        <div style={{ fontSize: 11, color: '#444d56', marginBottom: 6 }}>
          Run a simulation first to save its result.
        </div>
      )}
      <div style={{ display: 'flex', gap: 6, marginBottom: 4 }}>
        <input value={saveName} onChange={e => setSaveName(e.target.value)}
          onKeyDown={e => e.key === 'Enter' && handleSave()} placeholder="Name (required)" style={inputStyle} />
        <input value={saveProject} onChange={e => setSaveProject(e.target.value)}
          placeholder="Project" style={{ ...inputStyle, flex: '0 0 110px' }} />
        <button className="btn btn-primary" style={{ padding: '4px 12px', fontSize: 12, gap: 4, flexShrink: 0 }}
          disabled={!saveName.trim() || !currentGeojson || busy} onClick={handleSave}>
          <Save size={12} /> Save
        </button>
      </div>
      {err && <div style={{ fontSize: 10, color: '#f85149', marginTop: 4 }}>{err}</div>}

      {entries.length === 0 ? (
        <div style={{ fontSize: 11, color: '#444d56', padding: '8px 0 2px' }}>No saved results yet.</div>
      ) : (
        <div style={{ maxHeight: 220, overflowY: 'auto', marginTop: 8 }}>
          {Object.entries(groups).map(([net, netEntries]) => (
            <div key={net} style={{ marginBottom: 8 }}>
              <div style={{ fontSize: 10, fontWeight: 700, color: '#8b949e', textTransform: 'uppercase',
                            letterSpacing: 1, marginBottom: 4, padding: '2px 0', borderBottom: '1px solid #21262d' }}>{net}</div>
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
                        {entry.point_count || 0} features · {new Date((entry.created || 0) * 1000).toLocaleString()}
                      </div>
                      <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap' }}>
                        <button className="btn btn-secondary" style={{ fontSize: 11, gap: 4, padding: '3px 8px' }}
                          title="Restore this result's settings (tab / TX / propagation) and re-render it on the map"
                          disabled={busy} onClick={() => handleLoad(entry.id)}>
                          <FolderOpen size={11} /> Load
                        </button>
                        <button className="btn btn-ghost" style={{ fontSize: 11, gap: 4, padding: '3px 8px' }}
                          disabled={busy} onClick={() => handleExport(entry)}>
                          <Download size={11} /> GeoJSON
                        </button>
                        <button className="btn btn-ghost" style={{ fontSize: 11, gap: 4, padding: '3px 8px', color: '#ef4444' }}
                          disabled={busy} onClick={() => handleDelete(entry.id)}>
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
