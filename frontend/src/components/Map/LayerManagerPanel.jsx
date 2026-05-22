/**
 * Unified manager for all user-added map content:
 *   - Imported KML / KMZ / GeoJSON / GPX layers
 *   - Imagery overlays (with min/max zoom level)
 *   - Tile sources (XYZ / WMS)
 *   - DTED / HGT / ASCII terrain grids
 *   - Drawn features
 * Plus session save / load / reset.
 */
import { useMemo, useRef, useState } from 'react'
import RegionDownloadPanel from './RegionDownloadPanel'
import OsintFeedsPanel from './OsintFeedsPanel'

const KIND_LABELS = {
  geojson: 'Vector',
  image:   'Imagery',
  tiles:   'Tile source',
  terrain: 'Terrain',
}

const KIND_COLORS = {
  geojson: '#06d6a0',
  image:   '#3b82f6',
  tiles:   '#a78bfa',
  terrain: '#f59e0b',
}

const ALL_KINDS = ['geojson', 'image', 'tiles', 'terrain', 'drawings']

function fmtBounds(b) {
  if (!b) return ''
  const [[s, w], [n, e]] = b
  return `${s.toFixed(3)}, ${w.toFixed(3)} → ${n.toFixed(3)}, ${e.toFixed(3)}`
}

function fmtMeters(m) {
  if (m == null || !Number.isFinite(m)) return '–'
  return m >= 1000 ? `${(m / 1000).toFixed(2)} km` : `${m.toFixed(0)} m`
}

export default function LayerManagerPanel({ ul, openFileDialog, drawCtrlRef, regionPreselect, onConsumeRegionPreselect,
                                            incomingBbox, onConsumeBbox, onRequestDrawBbox,
                                            // Full-app state save/load (duplicated from File menu) — Save opens the
                                            // section-selector dialog so the user can choose what to include.
                                            onOpenSaveStateDialog, onLoadFullState }) {
  const [kindFilter, setKindFilter] = useState(new Set(ALL_KINDS))
  const [tileFormOpen, setTileFormOpen] = useState(false)
  const [osintOpen, setOsintOpen] = useState(false)
  const [tileForm, setTileForm] = useState({
    name: '', url: '', type: 'xyz', minZoom: 0, maxZoom: 18,
    attribution: '', wmsLayers: '',
  })
  const sessionInputRef = useRef(null)
  // Multi-select for bulk Show / Hide / Delete. Keys: layer.id or `drawn:${f.id}`.
  const [selected, setSelected] = useState(() => new Set())
  const toggleSelected = (key) => {
    setSelected((prev) => {
      const next = new Set(prev)
      if (next.has(key)) next.delete(key); else next.add(key)
      return next
    })
  }
  const clearSelection = () => setSelected(new Set())
  const bulkSetVisible = (visible) => {
    selected.forEach((key) => {
      if (key.startsWith('drawn:')) return                  // drawn features have no per-feature visibility yet
      ul.setLayerProperty(key, { visible })
    })
  }
  const bulkDelete = () => {
    const n = selected.size
    if (!n) return
    if (!confirm(`Delete ${n} selected item${n > 1 ? 's' : ''}?`)) return
    selected.forEach((key) => {
      if (key.startsWith('drawn:')) ul.removeDrawnFeature(key.slice(6))
      else ul.removeLayer(key)
    })
    clearSelection()
  }

  const groupedByZoom = useMemo(() => {
    // For imagery & tile sources, group by their visible zoom range
    const groups = {}
    for (const l of ul.layers) {
      if (l.kind !== 'image' && l.kind !== 'tiles') continue
      const min = l.minZoom ?? 0, max = l.maxZoom ?? 22
      // Bucket: snap to integer ranges
      const key = `${min}–${max}`
      if (!groups[key]) groups[key] = { min, max, items: [] }
      groups[key].items.push(l)
    }
    return Object.values(groups).sort((a, b) => a.min - b.min || a.max - b.max)
  }, [ul.layers])

  const filteredLayers = ul.layers.filter(l => kindFilter.has(l.kind))
  const showDrawings = kindFilter.has('drawings')

  const toggleKind = (k) => {
    const next = new Set(kindFilter)
    if (next.has(k)) next.delete(k); else next.add(k)
    setKindFilter(next)
  }

  const submitTileForm = () => {
    const f = tileForm
    if (!f.url) return
    ul.addTileLayer({
      name: f.name || (f.type === 'wms' ? 'WMS layer' : 'XYZ tiles'),
      url: f.url,
      type: f.type,
      minZoom: parseInt(f.minZoom, 10) || 0,
      maxZoom: parseInt(f.maxZoom, 10) || 18,
      attribution: f.attribution,
      wmsLayers: f.wmsLayers,
      visible: true,
    })
    setTileFormOpen(false)
    setTileForm({ name: '', url: '', type: 'xyz', minZoom: 0, maxZoom: 18, attribution: '', wmsLayers: '' })
  }

  const exportSession = () => {
    const session = ul.exportSession()
    const blob = new Blob([JSON.stringify(session)], { type: 'application/json' })
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = `ares-layers-${new Date().toISOString().slice(0, 19).replace(/[:T]/g, '-')}.areslayers.json`
    document.body.appendChild(a); a.click(); a.remove()
    URL.revokeObjectURL(url)
  }

  const importSession = (file) => {
    const r = new FileReader()
    r.onload = () => {
      try {
        const session = JSON.parse(r.result)
        ul.importSession(session)
      } catch (e) {
        alert('Failed to load layer session: ' + (e?.message || e))
      }
    }
    r.readAsText(file)
  }

  const totalLayers = ul.layers.length + ul.drawnFeatures.length

  return (
    <div style={{ height: '100%', display: 'flex', flexDirection: 'column', overflow: 'hidden' }}>
      {/* ── Toolbar ─────────────────────────────────────────────────────── */}
      <div style={{
        display: 'flex', alignItems: 'center', gap: 6, flexWrap: 'wrap',
        padding: '8px 10px', borderBottom: '1px solid #21262d', flexShrink: 0,
      }}>
        <button className="btn btn-primary" style={{ fontSize: 11, padding: '4px 10px' }}
          onClick={openFileDialog}>
          📂 Load file…
        </button>
        <button className="btn btn-ghost" style={{ fontSize: 11, padding: '4px 10px' }}
          onClick={() => setTileFormOpen(o => !o)}>
          🌐 Add tile source
        </button>
        <button className={`btn ${osintOpen ? 'btn-primary' : 'btn-ghost'}`} style={{ fontSize: 11, padding: '4px 10px' }}
          title="Import live OSINT mapping feeds (DeepState, GDELT, ADS-B, FIRMS, ACLED, AIS, …) as toggleable layers"
          onClick={() => setOsintOpen(o => !o)}>
          🛰 OSINT feeds
        </button>
        <div style={{ width: 1, height: 18, background: '#30363d', margin: '0 2px' }} />
        <button className="btn btn-ghost" style={{ fontSize: 11, padding: '4px 10px' }}
          title="Save just the layers in this panel — KMZ / GeoJSON / imagery / tiles / DTED / drawings."
          onClick={exportSession}>
          💾 Save session
        </button>
        <button className="btn btn-ghost" style={{ fontSize: 11, padding: '4px 10px' }}
          title="Load a previously-saved layer-only session."
          onClick={() => sessionInputRef.current?.click()}>
          📥 Load session
        </button>
        <input ref={sessionInputRef} type="file" accept=".json,.areslayers.json"
          style={{ display: 'none' }}
          onChange={e => { if (e.target.files?.[0]) importSession(e.target.files[0]); e.target.value = '' }} />
        {(onOpenSaveStateDialog || onLoadFullState) && (
          <>
            <div style={{ width: 1, height: 18, background: '#30363d', margin: '0 2px' }} />
            {onOpenSaveStateDialog && (
              <button className="btn btn-ghost" style={{ fontSize: 11, padding: '4px 10px' }}
                title="Save full app state (emitters, LoBs, analyses, layers, …) — choose which sections to include."
                onClick={onOpenSaveStateDialog}>
                💾 Save state…
              </button>
            )}
            {onLoadFullState && (
              <button className="btn btn-ghost" style={{ fontSize: 11, padding: '4px 10px' }}
                title="Load a previously-saved full app state."
                onClick={onLoadFullState}>
                📥 Load state…
              </button>
            )}
          </>
        )}
        <div style={{ flex: 1 }} />
        {totalLayers > 0 && (
          <button className="btn btn-ghost" style={{ fontSize: 11, padding: '4px 10px', color: '#fca5a5' }}
            onClick={() => {
              if (confirm(`Remove all ${totalLayers} item${totalLayers > 1 ? 's' : ''}?`)) {
                ul.clearAll(); ul.clearDrawn()
              }
            }}>
            🗑 Clear all
          </button>
        )}
      </div>

      {/* Download offline mapping data for a state / country / region → the persistent pack library */}
      <RegionDownloadPanel preselect={regionPreselect} onConsumePreselect={onConsumeRegionPreselect}
                            incomingBbox={incomingBbox} onConsumeBbox={onConsumeBbox}
                            onRequestDrawBbox={onRequestDrawBbox} />

      {/* Live OSINT feeds → toggleable map layers (collapsed by default) */}
      {osintOpen && <OsintFeedsPanel ul={ul} />}

      {/* Tile source form */}
      {tileFormOpen && (
        <div style={{
          padding: '10px 14px', borderBottom: '1px solid #21262d', flexShrink: 0,
          background: '#0d1117',
        }}>
          <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 8, alignItems: 'end' }}>
            <label style={{ fontSize: 10, color: '#8b949e' }}>Name
              <input value={tileForm.name}
                onChange={e => setTileForm(p => ({ ...p, name: e.target.value }))}
                style={inputStyle} placeholder="OpenStreetMap" />
            </label>
            <label style={{ fontSize: 10, color: '#8b949e' }}>Type
              <select value={tileForm.type}
                onChange={e => setTileForm(p => ({ ...p, type: e.target.value }))}
                style={inputStyle}>
                <option value="xyz">XYZ tiles</option>
                <option value="wms">WMS</option>
              </select>
            </label>
          </div>
          <label style={{ fontSize: 10, color: '#8b949e', display: 'block', marginTop: 6 }}>
            URL template ({tileForm.type === 'xyz' ? 'use {z}/{x}/{y}' : 'WMS GetMap endpoint'})
            <input value={tileForm.url}
              onChange={e => setTileForm(p => ({ ...p, url: e.target.value }))}
              style={inputStyle}
              placeholder={tileForm.type === 'xyz'
                ? 'https://tile.openstreetmap.org/{z}/{x}/{y}.png'
                : 'https://example.com/wms?'} />
          </label>
          {tileForm.type === 'wms' && (
            <label style={{ fontSize: 10, color: '#8b949e', display: 'block', marginTop: 6 }}>
              WMS layers
              <input value={tileForm.wmsLayers}
                onChange={e => setTileForm(p => ({ ...p, wmsLayers: e.target.value }))}
                style={inputStyle} placeholder="layer1,layer2" />
            </label>
          )}
          <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr 2fr', gap: 8, marginTop: 6 }}>
            <label style={{ fontSize: 10, color: '#8b949e' }}>Min zoom
              <input type="number" value={tileForm.minZoom}
                onChange={e => setTileForm(p => ({ ...p, minZoom: e.target.value }))}
                style={inputStyle} />
            </label>
            <label style={{ fontSize: 10, color: '#8b949e' }}>Max zoom
              <input type="number" value={tileForm.maxZoom}
                onChange={e => setTileForm(p => ({ ...p, maxZoom: e.target.value }))}
                style={inputStyle} />
            </label>
            <label style={{ fontSize: 10, color: '#8b949e' }}>Attribution
              <input value={tileForm.attribution}
                onChange={e => setTileForm(p => ({ ...p, attribution: e.target.value }))}
                style={inputStyle} placeholder="© Source" />
            </label>
          </div>
          <div style={{ display: 'flex', gap: 6, marginTop: 10 }}>
            <button className="btn btn-primary" style={{ fontSize: 11, padding: '4px 10px' }}
              disabled={!tileForm.url} onClick={submitTileForm}>Add</button>
            <button className="btn btn-ghost" style={{ fontSize: 11, padding: '4px 10px' }}
              onClick={() => setTileFormOpen(false)}>Cancel</button>
          </div>
        </div>
      )}

      {/* ── Bulk-actions bar (only when items are selected) ─────────────── */}
      {selected.size > 0 && (
        <div style={{
          display: 'flex', alignItems: 'center', gap: 6, padding: '6px 10px',
          borderBottom: '1px solid #21262d', flexShrink: 0,
          background: '#0d2438',
        }}>
          <span style={{ fontSize: 11, color: '#c9d1d9', fontWeight: 600 }}>
            {selected.size} selected
          </span>
          <div style={{ flex: 1 }} />
          <button className="btn btn-ghost" style={{ fontSize: 11, padding: '4px 10px' }}
            onClick={() => bulkSetVisible(true)}>👁 Show</button>
          <button className="btn btn-ghost" style={{ fontSize: 11, padding: '4px 10px' }}
            onClick={() => bulkSetVisible(false)}>🚫 Hide</button>
          <button className="btn btn-ghost" style={{ fontSize: 11, padding: '4px 10px', color: '#fca5a5' }}
            onClick={bulkDelete}>🗑 Delete</button>
          <button className="btn btn-ghost" style={{ fontSize: 11, padding: '4px 6px', color: '#8b949e' }}
            onClick={clearSelection}>✕</button>
        </div>
      )}

      {/* ── Filter chips ────────────────────────────────────────────────── */}
      <div style={{
        display: 'flex', alignItems: 'center', gap: 6, flexWrap: 'wrap',
        padding: '6px 10px', borderBottom: '1px solid #21262d', flexShrink: 0,
        fontSize: 11,
      }}>
        <span style={{ color: '#8b949e' }}>Show:</span>
        {ALL_KINDS.map(k => {
          const label = k === 'drawings' ? 'Drawings' : KIND_LABELS[k]
          const color = k === 'drawings' ? '#a855f7' : KIND_COLORS[k]
          const on = kindFilter.has(k)
          return (
            <button key={k}
              className={`btn ${on ? 'btn-primary' : 'btn-ghost'}`}
              style={{ fontSize: 10, padding: '2px 8px',
                       borderColor: on ? color : '#30363d',
                       color: on ? '#fff' : color }}
              onClick={() => toggleKind(k)}>
              {label}
            </button>
          )
        })}
      </div>

      {/* ── Body ────────────────────────────────────────────────────────── */}
      <div style={{ flex: 1, overflowY: 'auto', padding: '8px 10px' }}>
        {totalLayers === 0 && (
          <div style={{
            display: 'flex', flexDirection: 'column', alignItems: 'center',
            color: '#484f58', textAlign: 'center', padding: '40px 20px',
            fontSize: 12, gap: 4,
          }}>
            <div style={{ fontSize: 30, marginBottom: 6 }}>📂</div>
            No layers yet. Drag KML / KMZ / GeoJSON / GPX / GeoTIFF / DTED files
            onto the map, or click <strong>Load file…</strong>.
          </div>
        )}

        {/* Imagery & tile sources grouped by zoom range */}
        {(kindFilter.has('image') || kindFilter.has('tiles')) && groupedByZoom.length > 0 && (
          <Section title={`Imagery & tile sources by zoom (${groupedByZoom.reduce((n, g) => n + g.items.length, 0)})`}>
            {groupedByZoom.map(g => (
              <div key={`${g.min}-${g.max}`} style={{ marginBottom: 6 }}>
                <div style={{
                  fontSize: 10, color: '#8b949e', fontWeight: 700, letterSpacing: 0.5,
                  padding: '2px 4px', textTransform: 'uppercase',
                }}>
                  Zoom {g.min}–{g.max} · {g.items.length} layer{g.items.length > 1 ? 's' : ''}
                </div>
                {g.items.filter(l => kindFilter.has(l.kind)).map(l => (
                  <LayerRow key={l.id} layer={l} ul={ul} selected={selected.has(l.id)} onToggleSelect={() => toggleSelected(l.id)} />
                ))}
              </div>
            ))}
          </Section>
        )}

        {/* Vector layers */}
        {kindFilter.has('geojson') && filteredLayers.some(l => l.kind === 'geojson') && (
          <Section title={`Vector layers (${filteredLayers.filter(l => l.kind === 'geojson').length})`}>
            {filteredLayers.filter(l => l.kind === 'geojson').map(l =>
              <LayerRow key={l.id} layer={l} ul={ul} selected={selected.has(l.id)} onToggleSelect={() => toggleSelected(l.id)} />
            )}
          </Section>
        )}

        {/* Terrain grids */}
        {kindFilter.has('terrain') && filteredLayers.some(l => l.kind === 'terrain') && (
          <Section title={`Terrain grids (${filteredLayers.filter(l => l.kind === 'terrain').length})`}>
            {filteredLayers.filter(l => l.kind === 'terrain').map(l =>
              <LayerRow key={l.id} layer={l} ul={ul} selected={selected.has(l.id)} onToggleSelect={() => toggleSelected(l.id)} />
            )}
          </Section>
        )}

        {/* Drawn features */}
        {showDrawings && ul.drawnFeatures.length > 0 && (
          <Section title={`Drawings (${ul.drawnFeatures.length})`}>
            {ul.drawnFeatures.map(f => {
              const key = `drawn:${f.id}`
              const isSelected = selected.has(key)
              return (
                <div key={f.id} style={{
                  display: 'flex', alignItems: 'center', gap: 8,
                  padding: '6px 8px', borderBottom: '1px solid #161b22',
                  background: isSelected ? '#0d2438' : 'transparent',
                  borderRadius: isSelected ? 4 : 0,
                }}>
                  <input type="checkbox" checked={isSelected} onChange={() => toggleSelected(key)}
                    title="Select for bulk action"
                    style={{ cursor: 'pointer', accentColor: '#1f6feb' }} />
                  <span style={{
                    fontSize: 9, color: '#a855f7', fontWeight: 700,
                    background: '#2d1b3d', padding: '2px 6px', borderRadius: 4,
                    minWidth: 60, textAlign: 'center',
                  }}>{f.kind}</span>
                  <button className="btn btn-ghost"
                    style={{ flex: 1, textAlign: 'left', padding: '2px 6px', fontSize: 11,
                             color: '#c9d1d9', overflow: 'hidden', whiteSpace: 'nowrap',
                             textOverflow: 'ellipsis' }}
                    onClick={() => ul.focusDrawnFeature(f.id)}>
                    {f.meta?.name || f.id}
                  </button>
                  <button className="btn btn-ghost"
                    style={{ padding: '2px 5px', fontSize: 11, color: '#fca5a5' }}
                    onClick={() => ul.removeDrawnFeature(f.id)}>×</button>
                </div>
              )
            })}
          </Section>
        )}
      </div>
    </div>
  )
}

const inputStyle = {
  width: '100%', marginTop: 3, background: '#0d1117',
  border: '1px solid #30363d', borderRadius: 4, color: '#e6edf3',
  padding: '4px 6px', fontSize: 11, outline: 'none', boxSizing: 'border-box',
}

function Section({ title, children }) {
  return (
    <div style={{ marginBottom: 14 }}>
      <div style={{
        fontSize: 10, fontWeight: 700, color: '#8b949e', letterSpacing: 0.7,
        textTransform: 'uppercase', padding: '4px 0', borderBottom: '1px solid #30363d',
        marginBottom: 4,
      }}>{title}</div>
      {children}
    </div>
  )
}

function LayerRow({ layer, ul, selected = false, onToggleSelect }) {
  const [expanded, setExpanded] = useState(false)
  const color = KIND_COLORS[layer.kind] || '#8b949e'
  const sub = []
  if (layer.featureCount != null) sub.push(`${layer.featureCount} features`)
  if (layer.bounds) sub.push(fmtBounds(layer.bounds))
  if (layer.kind === 'terrain') {
    sub.push(`${layer.cols}×${layer.rows}`)
    if (layer.minElev != null && layer.maxElev != null) {
      sub.push(`${layer.minElev.toFixed(0)}–${layer.maxElev.toFixed(0)} m`)
    }
  }
  if (layer.kind === 'tiles' && layer.url) sub.push(layer.url.replace(/^https?:\/\//, '').slice(0, 40))

  return (
    <div style={{
      borderBottom: '1px solid #161b22',
      background: expanded ? '#11161d' : 'transparent',
      borderRadius: expanded ? 6 : 0,
      marginBottom: expanded ? 4 : 0,
      padding: expanded ? '4px 6px' : 0,
    }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 8, padding: '4px 6px',
                    background: selected ? '#0d2438' : 'transparent', borderRadius: selected ? 4 : 0 }}>
        <input type="checkbox" checked={selected} onChange={onToggleSelect}
          title="Select for bulk action"
          style={{ cursor: 'pointer', accentColor: '#1f6feb' }} />
        <input type="checkbox" checked={layer.visible}
          onChange={() => ul.setLayerProperty(layer.id, { visible: !layer.visible })}
          title="Show on map"
          style={{ cursor: 'pointer' }} />
        <span style={{
          fontSize: 9, color, fontWeight: 700,
          background: '#0d1117', padding: '2px 6px', borderRadius: 4,
          minWidth: 60, textAlign: 'center', border: `1px solid ${color}55`,
        }}>{KIND_LABELS[layer.kind]}</span>
        <button className="btn btn-ghost"
          style={{ flex: 1, textAlign: 'left', padding: '2px 6px', fontSize: 11,
                   color: '#c9d1d9', overflow: 'hidden', whiteSpace: 'nowrap',
                   textOverflow: 'ellipsis' }}
          onClick={() => ul.focusLayer(layer.id)}
          title={layer.name}>
          {layer.name}
          {sub.length > 0 && (
            <span style={{ color: '#484f58', fontSize: 10, marginLeft: 6 }}>
              {sub.join(' · ')}
            </span>
          )}
        </button>
        <button className="btn btn-ghost"
          style={{ padding: '2px 5px', fontSize: 11, color: '#8b949e' }}
          onClick={() => setExpanded(e => !e)}
          title={expanded ? 'Collapse' : 'Settings'}>
          {expanded ? '▾' : '⋯'}
        </button>
        <button className="btn btn-ghost"
          style={{ padding: '2px 5px', fontSize: 11, color: '#fca5a5' }}
          onClick={() => ul.removeLayer(layer.id)}
          title="Delete">×</button>
      </div>

      {expanded && (
        <div style={{ padding: '4px 10px 10px', display: 'grid', gap: 6, fontSize: 10 }}>
          <label style={{ color: '#8b949e' }}>Name
            <input value={layer.name}
              onChange={e => ul.renameLayer(layer.id, e.target.value)}
              style={inputStyle} />
          </label>

          <label style={{ color: '#8b949e' }}>
            Opacity {Math.round((layer.opacity ?? 1) * 100)}%
            <input type="range" min="0" max="1" step="0.05"
              value={layer.opacity ?? 1}
              onChange={e => ul.setLayerProperty(layer.id, { opacity: parseFloat(e.target.value) })}
              style={{ width: '100%' }} />
          </label>

          {(layer.kind === 'image' || layer.kind === 'tiles') && (
            <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 8 }}>
              <label style={{ color: '#8b949e' }}>Min zoom
                <input type="number" min="0" max="22"
                  value={layer.minZoom ?? 0}
                  onChange={e => ul.setLayerProperty(layer.id, { minZoom: parseInt(e.target.value, 10) })}
                  style={inputStyle} />
              </label>
              <label style={{ color: '#8b949e' }}>Max zoom
                <input type="number" min="0" max="22"
                  value={layer.maxZoom ?? 22}
                  onChange={e => ul.setLayerProperty(layer.id, { maxZoom: parseInt(e.target.value, 10) })}
                  style={inputStyle} />
              </label>
            </div>
          )}

          {layer.kind === 'geojson' && (
            <label style={{ color: '#8b949e' }}>Stroke colour
              <input type="color" value={layer.color || '#06d6a0'}
                onChange={e => ul.setLayerProperty(layer.id, { color: e.target.value })}
                style={{ ...inputStyle, height: 30, padding: 0 }} />
            </label>
          )}
        </div>
      )}
    </div>
  )
}
