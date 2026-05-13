/**
 * "Download mapping data for a state / country / region" — sits in the Layer Manager. Look up a
 * named region (or have it pre-selected from a right-click on the map), tick which layers you want
 * (imagery / DTED terrain / clutter / OSM tiles / buildings), and stage the offline data packs into
 * the persistent library. The data lives in the server's pack directory and survives sessions; the
 * "Update" button is the *only* way to re-fetch a fresher version — there is no auto/background refresh.
 */
import { useEffect, useRef, useState } from 'react'
import { searchRegions, downloadRegionData, estimateRegionDownload, listDataPacks, updateDataPack, deleteDataPack, listPackJobs, listRegionCells, estimateBboxDownload, downloadBboxData } from '../../api/client'

const CUSTOM_BBOX_CODE = '(custom-bbox)'

const LAYER_OPTS = [
  ['imagery', 'Imagery (satellite/aerial XYZ tiles)'],
  ['terrain', 'DTED terrain (SRTM ~30 m .hgt)'],
  ['clutter', 'Clutter / land-cover'],
  ['osm', 'OSM base-map tiles'],
  ['buildings', 'OSM building footprints'],
]
const ZOOMS = [12, 13, 14, 15, 16, 17, 18]
const card = { background: '#0d1117', border: '1px solid #21262d', borderRadius: 6 }
const fmtBytes = (b) => !b ? '–' : b > 1e9 ? `${(b / 1e9).toFixed(2)} GB` : b > 1e6 ? `${(b / 1e6).toFixed(1)} MB` : `${Math.max(1, Math.round(b / 1e3))} kB`

export default function RegionDownloadPanel({ preselect, onConsumePreselect,
                                              incomingBbox, onConsumeBbox, onRequestDrawBbox }) {
  const [open, setOpen] = useState(false)
  const [q, setQ] = useState('')
  const [results, setResults] = useState([])
  const [sel, setSel] = useState(null)            // selected region {code, name, bbox, country}
  const [layers, setLayers] = useState(['imagery', 'terrain', 'clutter', 'buildings'])
  const [maxZoom, setMaxZoom] = useState(15)
  const [busy, setBusy] = useState(false)
  const [msg, setMsg] = useState('')
  const [estimate, setEstimate] = useState(null)   // null = no estimate yet; once set, the button switches from "Get estimate" → "Download"
  const [packs, setPacks] = useState([])
  const [jobs, setJobs] = useState([])
  const [cellsOpen, setCellsOpen] = useState(false)  // "pick a 0.5° sub-cell" drawer after a parent is selected
  const [cells, setCells] = useState(null)           // {parent, cells, cell_deg} | null
  const [cellsLoading, setCellsLoading] = useState(false)
  const debRef = useRef(null)

  const refreshLibrary = () => {
    listDataPacks().then(d => setPacks(d.packs || [])).catch(() => {})
    listPackJobs().then(d => setJobs((d.jobs || []).filter(j => ['queued', 'running'].includes(j.status)))).catch(() => {})
  }
  useEffect(() => { if (open) refreshLibrary() }, [open])
  useEffect(() => {
    if (!open) return
    const t = setInterval(refreshLibrary, 5000)
    return () => clearInterval(t)
  }, [open])

  // pre-selected from a right-click on the map → open + select that region
  useEffect(() => {
    if (preselect?.code) {
      setOpen(true); setSel(preselect); setQ(preselect.name || ''); setResults([preselect])
      onConsumePreselect?.()
    }
  }, [preselect, onConsumePreselect])

  // a bbox drawn on the map (via "Draw on map") arrives here → become the current selection
  // (treated as a custom-bbox: estimate/download go through /regions/by-bbox/* instead of /regions/{code}/*)
  useEffect(() => {
    if (incomingBbox && incomingBbox.length === 4) {
      const [w, s, e, n] = incomingBbox
      setOpen(true)
      setSel({
        code: CUSTOM_BBOX_CODE,
        name: `Drawn area · ${w.toFixed(3)},${s.toFixed(3)} → ${e.toFixed(3)},${n.toFixed(3)}`,
        country: '(custom)',
        bbox: [w, s, e, n],
      })
      setEstimate(null)
      onConsumeBbox?.()
    }
  }, [incomingBbox, onConsumeBbox])

  useEffect(() => {
    if (!open) return
    if (debRef.current) clearTimeout(debRef.current)
    debRef.current = setTimeout(() => {
      searchRegions(q, 60).then(d => setResults(d.regions || [])).catch(() => setResults([]))
    }, 220)
    return () => { if (debRef.current) clearTimeout(debRef.current) }
  }, [q, open])

  // Changing the region invalidates the estimate outright (different bbox); changing layers or
  // max-zoom leaves the estimate visible but marks it stale (see `estimateStale`) so the user can
  // click "↺ re-estimate" to refresh in place rather than going through a second "Get estimate".
  const toggleLayer = (l) => setLayers(prev => prev.includes(l) ? prev.filter(x => x !== l) : [...prev, l])
  const setSelInvalidating = (r) => { setEstimate(null); setSel(r); setCellsOpen(false); setCells(null) }

  // Fetch the 0.5° sub-cells for a parent region (only when the user expands the drawer; the
  // server computes them on demand so we don't carry tens of thousands in memory by default).
  const loadCells = async (code) => {
    if (!code) return
    setCellsLoading(true)
    try { setCells(await listRegionCells(code)) }
    catch { setCells(null) }
    finally { setCellsLoading(false) }
  }
  const expandCells = async () => {
    setCellsOpen(true)
    if (!cells && sel?.code && !sel?.cell) await loadCells(sel.code)
  }
  const pickCell = (cell) => {
    setEstimate(null); setSel(cell); setCellsOpen(false)
  }
  const estimateStale = !!estimate && (
    estimate.max_zoom !== maxZoom ||
    (estimate.layers || []).slice().sort().join(',') !== layers.slice().sort().join(',')
  )

  const isCustomBbox = sel?.code === CUSTOM_BBOX_CODE

  const doEstimate = async () => {
    if (!sel?.code || !layers.length) return
    setBusy(true); setMsg('')
    try {
      const r = isCustomBbox
        ? await estimateBboxDownload(sel.bbox, { layers, max_zoom: maxZoom, name: sel.name })
        : await estimateRegionDownload(sel.code, { layers, max_zoom: maxZoom })
      setEstimate(r)
      setMsg(`Estimate for ${sel.name}: ~${fmtBytes(r.total_bytes)} total — review per-item below, then click Download.`)
    } catch (e) {
      setMsg('Estimate failed: ' + (e?.response?.data?.detail || e?.message || e))
    } finally { setBusy(false) }
  }

  const doDownload = async () => {
    if (!sel?.code || !layers.length) return
    setBusy(true); setMsg('')
    try {
      const r = isCustomBbox
        ? await downloadBboxData(sel.bbox, { layers, max_zoom: maxZoom, name: sel.name })
        : await downloadRegionData(sel.code, { layers, max_zoom: maxZoom })
      const n = (r.jobs || []).filter(j => j.status === 'queued').length
      const skipped = (r.jobs || []).filter(j => j.status !== 'queued')
      setMsg(`Staging ${n} pack job(s) for ${sel.name} — runs on the server; check the library below.` +
             (skipped.length ? ` (${skipped.map(j => `${j.layer}: ${j.detail || j.status}`).join('; ')})` : ''))
      setEstimate(null)              // next time the user wants to download something else, start with a fresh estimate
      refreshLibrary()
    } catch (e) {
      setMsg('Download failed: ' + (e?.response?.data?.detail || e?.message || e))
    } finally { setBusy(false) }
  }
  const doUpdate = async (id) => { try { await updateDataPack(id); setMsg(`Re-fetching ${id}…`); refreshLibrary() } catch (e) { setMsg('Update failed: ' + (e?.response?.data?.detail || e?.message || e)) } }
  const doDelete = async (id) => { if (!confirm(`Delete pack ${id} from the library?`)) return; try { await deleteDataPack(id); refreshLibrary() } catch {} }

  return (
    <div style={{ ...card, margin: '8px 10px 0', padding: '8px 10px' }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 8, cursor: 'pointer' }} onClick={() => setOpen(o => !o)}>
        <span style={{ fontSize: 11, fontWeight: 700, color: '#8b949e', letterSpacing: 0.5 }}>{open ? '▾' : '▸'} OFFLINE MAPPING DATA — DOWNLOAD A REGION</span>
        {jobs.length > 0 && <span style={{ fontSize: 10, color: '#f0883e' }}>● {jobs.length} job(s) running</span>}
        <span style={{ marginLeft: 'auto', fontSize: 10, color: '#484f58' }}>{packs.length} pack(s) in library</span>
      </div>

      {open && (
        <div style={{ marginTop: 8, display: 'flex', flexDirection: 'column', gap: 8 }}>
          <div style={{ fontSize: 10, color: '#6e7681', lineHeight: 1.5 }}>
            Pick a state / country / region (or right-click the map to grab the 0.5° cell over that point — the z17-friendly download unit). After selecting a parent you can drill into its 0.5° cells; the parent itself stays selectable for "do a larger area at once" at lower zooms. Packs land in the server's persistent library and survive sessions. <strong>Update is manual only</strong> — nothing auto-refreshes.
          </div>

          {/* search + draw-bbox shortcut */}
          <div style={{ display: 'flex', gap: 6, alignItems: 'center' }}>
            <input value={q} onChange={e => setQ(e.target.value)} placeholder="search a state / country / region — e.g. California, France, Russia, Texas…"
                   style={{ flex: 1, fontSize: 11, padding: '4px 6px', background: '#161b22', border: '1px solid #30363d', borderRadius: 4, color: '#e6edf3', boxSizing: 'border-box' }} />
            {onRequestDrawBbox && (
              <button className="btn btn-ghost" onClick={onRequestDrawBbox}
                      title="Close this dialog and draw a rectangle on the map — the bbox becomes a custom selection you can estimate and download."
                      style={{ fontSize: 10, padding: '3px 8px', whiteSpace: 'nowrap', flexShrink: 0 }}>
                ▭ Draw on map
              </button>
            )}
          </div>
          {!sel && (
            <div style={{ ...card, maxHeight: 160, overflowY: 'auto' }}>
              {results.length === 0 && <div style={{ fontSize: 11, color: '#6e7681', padding: 8 }}>No matches.</div>}
              {results.map(r => (
                <button key={r.code} onClick={() => setSelInvalidating(r)} className="btn btn-ghost"
                        style={{ display: 'flex', justifyContent: 'space-between', width: '100%', fontSize: 11, padding: '3px 8px', borderRadius: 0, borderBottom: '1px solid #161b22', textAlign: 'left' }}>
                  <span style={{ color: '#c9d1d9' }}>{r.name} <span style={{ color: '#484f58' }}>· {r.code}</span></span>
                  <span style={{ color: '#6e7681' }}>{r.group}</span>
                </button>
              ))}
            </div>
          )}

          {sel && (
            <div style={{ ...card, padding: 8 }}>
              <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 6 }}>
                <b style={{ fontSize: 12, color: '#e6edf3' }}>{sel.name}</b>
                <span style={{ fontSize: 10, color: '#6e7681' }}>{sel.code} · bbox {sel.bbox?.map(v => v.toFixed(1)).join(', ')}</span>
                {sel.parent && (
                  <span style={{ fontSize: 10, color: '#484f58' }}>· in {sel.parent}</span>
                )}
                <button className="btn btn-ghost" style={{ marginLeft: 'auto', fontSize: 10, padding: '1px 6px' }} onClick={() => setSelInvalidating(null)}>change</button>
              </div>

              {/* When a *parent* (state/country) is selected, offer to narrow to a 0.5° cell
                  — that's the z17-friendly download unit. When a cell is selected, offer to
                  jump back up to its parent (for the "do a larger area at once" case).
                  Drawn-bbox selections have no parent/cell context — neither affordance applies. */}
              {isCustomBbox ? null : !sel.cell ? (
                <div style={{ marginBottom: 6 }}>
                  <button className="btn btn-ghost" onClick={expandCells}
                          style={{ fontSize: 10, padding: '2px 6px', color: '#8b949e' }}>
                    {cellsOpen ? '▾' : '▸'} pick a 0.5° sub-cell within {sel.name} — recommended for z17
                  </button>
                  {cellsOpen && (
                    <div style={{ ...card, marginTop: 4, maxHeight: 180, overflowY: 'auto' }}>
                      {cellsLoading && <div style={{ fontSize: 10, color: '#6e7681', padding: 6 }}>Loading cells…</div>}
                      {!cellsLoading && cells && cells.cells?.length === 0 && (
                        <div style={{ fontSize: 10, color: '#6e7681', padding: 6 }}>
                          This region is already smaller than one 0.5° cell.
                        </div>
                      )}
                      {!cellsLoading && cells && cells.cells?.length > 0 && (
                        <>
                          <div style={{ fontSize: 9, color: '#6e7681', padding: '4px 8px', borderBottom: '1px solid #21262d' }}>
                            {cells.cells.length} cells · click to pick one (z17 imagery ≈ 150–800 MB per cell)
                          </div>
                          {cells.cells.map(c => (
                            <button key={c.code} onClick={() => pickCell(c)} className="btn btn-ghost"
                                    style={{ display: 'flex', justifyContent: 'space-between', width: '100%',
                                             fontSize: 10, padding: '2px 8px', borderRadius: 0,
                                             borderBottom: '1px solid #161b22', textAlign: 'left' }}>
                              <span style={{ color: '#c9d1d9' }}>{c.name}</span>
                              <span style={{ color: '#484f58' }}>{c.code}</span>
                            </button>
                          ))}
                        </>
                      )}
                    </div>
                  )}
                </div>
              ) : sel.parent_code && (
                <div style={{ marginBottom: 6 }}>
                  <button className="btn btn-ghost"
                          onClick={() => setSelInvalidating({ code: sel.parent_code, name: sel.parent, bbox: sel.parent_bbox })}
                          style={{ fontSize: 10, padding: '2px 6px', color: '#8b949e' }}>
                    ↑ use whole {sel.parent} instead (larger area, lower zooms only)
                  </button>
                </div>
              )}
              <div style={{ display: 'flex', flexDirection: 'column', gap: 3, marginBottom: 6 }}>
                {LAYER_OPTS.map(([l, label]) => (
                  <label key={l} style={{ fontSize: 11, color: '#c9d1d9', display: 'flex', alignItems: 'center', gap: 6, cursor: 'pointer' }}>
                    <input type="checkbox" checked={layers.includes(l)} onChange={() => toggleLayer(l)} /> {label}
                  </label>
                ))}
              </div>
              {(layers.includes('imagery') || layers.includes('osm')) && (
                <label style={{ fontSize: 11, color: '#8b949e', display: 'flex', alignItems: 'center', gap: 6, marginBottom: 6 }}>
                  Imagery max zoom:
                  <select value={maxZoom} onChange={e => setMaxZoom(Number(e.target.value))} style={{ fontSize: 11 }}>
                    {ZOOMS.map(z => <option key={z} value={z}>z{z}{z >= 17 ? ' (very large!)' : z >= 16 ? ' (large)' : ''}</option>)}
                  </select>
                  <span style={{ fontSize: 10, color: '#6e7681' }}>higher = more detail & much bigger download</span>
                </label>
              )}
              {/* per-layer download estimate (shown once the user clicks "Get download estimate") */}
              {estimate && (
                <div style={{ ...card, padding: 6, marginBottom: 6, background: '#161b22',
                              opacity: estimateStale ? 0.6 : 1 }}>
                  <div style={{ fontSize: 10, fontWeight: 700, color: '#8b949e', letterSpacing: 0.5, marginBottom: 4,
                                display: 'flex', alignItems: 'center', gap: 6 }}>
                    <span>ESTIMATE — review before downloading</span>
                    {estimateStale && <span style={{ color: '#f0883e', fontWeight: 600 }}>· settings changed — click ↺ re-estimate</span>}
                  </div>
                  {LAYER_OPTS.filter(([l]) => layers.includes(l)).map(([l, label]) => {
                    const e = estimate.per_layer?.[l]
                    if (!e) return null
                    const exceeds = !!e.exceeds_cap
                    return (
                      <div key={l} style={{ display: 'flex', alignItems: 'baseline', gap: 6, fontSize: 11, padding: '2px 0', borderBottom: '1px solid #21262d' }}>
                        <span style={{ minWidth: 70, color: '#c9d1d9', fontWeight: 600 }}>{l}</span>
                        <span style={{ color: exceeds ? '#ef4444' : '#3fb950', minWidth: 80, textAlign: 'right' }}>{fmtBytes(e.bytes)}</span>
                        <span style={{ color: '#6e7681', fontSize: 10 }}>· {e.tiles?.toLocaleString()} {l === 'buildings' ? 'cells' : 'tiles'}</span>
                        {e.note && <span style={{ color: exceeds ? '#f0883e' : '#484f58', fontSize: 10, marginLeft: 4 }}>· {e.note}</span>}
                      </div>
                    )
                  })}
                  <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: 11, paddingTop: 4, marginTop: 2, borderTop: '1px solid #30363d' }}>
                    <span style={{ color: '#8b949e' }}>Total</span>
                    <strong style={{ color: '#e6edf3' }}>{fmtBytes(estimate.total_bytes)}</strong>
                  </div>
                </div>
              )}

              {/* the button is "Get download estimate" until an estimate is in; then it becomes "Download <total>" */}
              {!estimate ? (
                <button className="btn btn-primary" disabled={busy || !layers.length} onClick={doEstimate} style={{ fontSize: 11, padding: '4px 12px' }}>
                  {busy ? 'Estimating…' : `Get download estimate · ${layers.length} layer${layers.length === 1 ? '' : 's'}`}
                </button>
              ) : (
                <div style={{ display: 'flex', gap: 6, alignItems: 'center', flexWrap: 'wrap' }}>
                  <button className="btn btn-primary" disabled={busy || !layers.length || estimateStale} onClick={doDownload}
                          title={estimateStale ? 'Settings changed — re-estimate before downloading' : undefined}
                          style={{ fontSize: 11, padding: '4px 12px', background: '#1f6feb', borderColor: '#1f6feb' }}>
                    {busy ? 'Staging…' : `⬇ Download · ${fmtBytes(estimate.total_bytes)}`}
                  </button>
                  <button className="btn btn-ghost" disabled={busy || !layers.length} onClick={doEstimate} style={{ fontSize: 11, padding: '4px 8px' }}>
                    {busy ? 'Estimating…' : '↺ re-estimate'}
                  </button>
                </div>
              )}
            </div>
          )}

          {msg && <div style={{ fontSize: 10, color: msg.startsWith('Download failed') || msg.startsWith('Update failed') ? '#f0883e' : '#3fb950' }}>{msg}</div>}

          {/* library */}
          <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
            <span style={{ fontSize: 10, fontWeight: 700, color: '#8b949e', letterSpacing: 0.5 }}>INSTALLED PACKS (LIBRARY)</span>
            <button className="btn btn-ghost" style={{ fontSize: 10, padding: '1px 6px' }} onClick={refreshLibrary}>↻</button>
            {jobs.map(j => <span key={j.id} style={{ fontSize: 9, color: '#f0883e' }}>{j.layer}: {j.status}{j.progress != null ? ` ${Math.round(j.progress * 100)}%` : ''}</span>)}
          </div>
          <div style={{ ...card, maxHeight: 180, overflowY: 'auto' }}>
            {packs.length === 0 && <div style={{ fontSize: 11, color: '#6e7681', padding: 8 }}>No packs installed yet.</div>}
            {packs.map(p => (
              <div key={p.id} style={{ display: 'flex', alignItems: 'center', gap: 8, fontSize: 10, padding: '4px 8px', borderBottom: '1px solid #161b22' }}>
                <span style={{ color: '#8b949e', minWidth: 64, fontWeight: 600 }}>{p.layer}</span>
                <span style={{ color: '#c9d1d9', flex: 1, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }} title={p.name}>{p.name || p.id}</span>
                <span style={{ color: '#6e7681' }}>{fmtBytes(p.size_bytes_on_disk)}</span>
                <button className="btn btn-ghost" style={{ fontSize: 9, padding: '1px 6px' }} title="Re-fetch a fresher version (manual)" onClick={() => doUpdate(p.id)}>Update</button>
                <button className="btn btn-ghost" style={{ fontSize: 9, padding: '1px 6px', color: '#fca5a5' }} onClick={() => doDelete(p.id)}>×</button>
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  )
}
