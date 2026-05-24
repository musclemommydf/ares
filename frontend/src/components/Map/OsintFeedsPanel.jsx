// SPDX-License-Identifier: MIT OR Apache-2.0
// Copyright (c) 2026 Ares

/**
 * OsintFeedsPanel — import live OSINT mapping feeds as toggleable map layers.
 *
 * Each feed is fetched + normalised to GeoJSON server-side (filtered: source query
 * → bbox clip → hard cap), then rendered through the normal user-layer system, so
 * turning it off is just `ul.removeLayer`. Keyed sources (NASA FIRMS / ACLED /
 * aisstream) show a config form until the operator pastes their own API key.
 *
 * "On" state is derived from whether a layer with id `osint:<feedId>` exists —
 * so the toggle, the standard Layer Manager row, and a refresh all stay in sync.
 */
import { useEffect, useMemo, useRef, useState } from 'react'
import { getOsintFeeds, fetchOsintFeed, addOsintFeed, deleteOsintFeed, setOsintFeedConfig } from '../../api/client'

const inputStyle = {
  marginTop: 2, background: '#0d1117', border: '1px solid #30363d', borderRadius: 4,
  color: '#e6edf3', padding: '3px 6px', fontSize: 11, outline: 'none', boxSizing: 'border-box',
}

const CATEGORY_LABEL = { conflict: 'Conflict', events: 'Events', tracks: 'Tracks', custom: 'Custom', other: 'OSINT' }

export default function OsintFeedsPanel({ ul }) {
  const [feeds, setFeeds] = useState([])
  const [ui, setUi] = useState({})            // feedId → { params, bbox, max, autoMin, status, busy, config }
  const [addOpen, setAddOpen] = useState(false)
  const [custom, setCustom] = useState({ name: '', url: '', format: 'auto', color: '#06d6a0' })
  const [err, setErr] = useState(null)
  const autoRef = useRef({})                  // feedId → interval id
  const uiRef = useRef(ui); uiRef.current = ui

  const activeIds = useMemo(() => new Set((ul.layers || []).map(l => l.id)), [ul.layers])
  const isOn = (id) => activeIds.has('osint:' + id)

  const loadFeeds = () => getOsintFeeds().then(r => {
    const list = r.feeds || []
    setFeeds(list)
    setUi(prev => {
      const next = { ...prev }
      for (const f of list) {
        if (next[f.id]) continue
        const params = {}
        for (const p of (f.params || [])) params[p.key] = p.default
        next[f.id] = { params, bbox: !!(f.big || f.wants_bbox), max: 2000, autoMin: 0,
                       status: null, busy: false, config: {} }
      }
      return next
    })
  }).catch(e => setErr(String(e?.message || e)))

  useEffect(() => { loadFeeds() }, [])              // eslint-disable-line
  useEffect(() => () => { Object.values(autoRef.current).forEach(clearInterval) }, [])

  const patchUi = (id, patch) => setUi(prev => ({ ...prev, [id]: { ...prev[id], ...patch } }))
  const patchParam = (id, key, val) => setUi(prev => ({ ...prev, [id]: { ...prev[id], params: { ...prev[id].params, [key]: val } } }))
  const patchConfig = (id, key, val) => setUi(prev => ({ ...prev, [id]: { ...prev[id], config: { ...prev[id].config, [key]: val } } }))

  const mapBbox = () => {
    try {
      const b = ul?._mapRef?.current?.getBounds?.()
      if (b) return [b.getWest(), b.getSouth(), b.getEast(), b.getNorth()]
    } catch { /* no map yet */ }
    return undefined
  }

  const buildBody = (feed) => {
    const u = uiRef.current[feed.id] || {}
    const body = { params: u.params || {}, max_features: Number(u.max) || 2000 }
    if (u.bbox) { const bb = mapBbox(); if (bb) body.bbox = bb }
    return body
  }

  const enable = async (feed) => {
    setErr(null); patchUi(feed.id, { busy: true })
    try {
      const res = await fetchOsintFeed(feed.id, buildBody(feed))
      if (res.source === 'unavailable') {
        patchUi(feed.id, { busy: false, status: res })       // show note; don't add an empty layer
        return
      }
      ul.removeLayer('osint:' + feed.id)
      ul.addGeoJSONLayer(res.geojson, {
        id: 'osint:' + feed.id, name: 'OSINT · ' + feed.name, color: feed.color,
        sourceFormat: 'osint', visible: true, fit: true,
      })
      patchUi(feed.id, { busy: false, status: res })
    } catch (e) {
      patchUi(feed.id, { busy: false, status: { source: 'error', error: String(e?.response?.data?.detail || e?.message || e) } })
    }
  }

  const refresh = async (feed) => {
    const u = uiRef.current[feed.id] || {}
    if (!isOn(feed.id) && !u.autoMin) return
    await enable(feed)
  }

  const setAuto = (feed, minutes) => {
    patchUi(feed.id, { autoMin: minutes })
    clearInterval(autoRef.current[feed.id]); delete autoRef.current[feed.id]
    if (minutes > 0) autoRef.current[feed.id] = setInterval(() => { if (!document.hidden) refresh(feed) }, minutes * 60000)
  }

  const toggle = (feed) => {
    if (isOn(feed.id)) {
      ul.removeLayer('osint:' + feed.id)
      clearInterval(autoRef.current[feed.id]); delete autoRef.current[feed.id]
      patchUi(feed.id, { autoMin: 0 })
    } else {
      enable(feed)
    }
  }

  const saveConfig = async (feed) => {
    const u = uiRef.current[feed.id] || {}
    try {
      await setOsintFeedConfig(feed.id, u.config || {})
      await loadFeeds()
      setErr(`✓ ${feed.name} configured`)
    } catch (e) { setErr(String(e?.response?.data?.detail || e?.message || e)) }
  }

  const submitCustom = async () => {
    if (!custom.url.trim()) { setErr('a feed URL is required'); return }
    try {
      await addOsintFeed({ ...custom, name: custom.name || custom.url })
      setCustom({ name: '', url: '', format: 'auto', color: '#06d6a0' })
      setAddOpen(false)
      await loadFeeds()
    } catch (e) { setErr(String(e?.response?.data?.detail || e?.message || e)) }
  }

  const removeCustom = async (feed) => {
    if (isOn(feed.id)) ul.removeLayer('osint:' + feed.id)
    try { await deleteOsintFeed(feed.id); await loadFeeds() }
    catch (e) { setErr(String(e?.message || e)) }
  }

  return (
    <div style={{ padding: '8px 14px', borderBottom: '1px solid #21262d', background: '#0b0f14' }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 6 }}>
        <span style={{ fontSize: 11, fontWeight: 700, color: '#8b949e', letterSpacing: 0.6, textTransform: 'uppercase' }}>
          🛰 OSINT feeds
        </span>
        <span style={{ fontSize: 10, color: '#484f58' }}>live overlays — toggle on/off; data is cached for offline</span>
        <div style={{ flex: 1 }} />
        <button className="btn btn-ghost" style={{ fontSize: 10, padding: '2px 8px' }} onClick={loadFeeds}>↻</button>
        <button className="btn btn-ghost" style={{ fontSize: 10, padding: '2px 8px' }} onClick={() => setAddOpen(o => !o)}>+ Custom</button>
      </div>

      {addOpen && (
        <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 6, marginBottom: 8,
                      padding: 8, border: '1px solid #21262d', borderRadius: 6 }}>
          <input style={inputStyle} placeholder="Name" value={custom.name} onChange={e => setCustom(c => ({ ...c, name: e.target.value }))} />
          <select style={inputStyle} value={custom.format} onChange={e => setCustom(c => ({ ...c, format: e.target.value }))}>
            <option value="auto">auto-detect</option><option value="geojson">GeoJSON</option>
            <option value="kml">KML</option><option value="georss">GeoRSS</option><option value="gpx">GPX</option>
          </select>
          <input style={{ ...inputStyle, gridColumn: '1 / -1' }} placeholder="https://…/feed.geojson (or KML/GeoRSS/GPX)"
                 value={custom.url} onChange={e => setCustom(c => ({ ...c, url: e.target.value }))} />
          <div style={{ gridColumn: '1 / -1', display: 'flex', gap: 6 }}>
            <button className="btn btn-primary" style={{ fontSize: 11, padding: '3px 10px' }} onClick={submitCustom}>Add feed</button>
            <button className="btn btn-ghost" style={{ fontSize: 11, padding: '3px 10px' }} onClick={() => setAddOpen(false)}>Cancel</button>
          </div>
        </div>
      )}

      <div style={{ display: 'flex', flexDirection: 'column', gap: 5 }}>
        {feeds.map(feed => {
          const u = ui[feed.id] || { params: {}, bbox: false, max: 2000, autoMin: 0 }
          const on = isOn(feed.id)
          const needsKey = feed.requires_config && !feed.configured
          const st = u.status
          return (
            <div key={feed.id} style={{ border: '1px solid #21262d', borderRadius: 6, padding: '6px 8px',
                                        borderLeft: `3px solid ${feed.color}` }}>
              <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                <label style={{ display: 'inline-flex', alignItems: 'center', gap: 5, cursor: needsKey ? 'not-allowed' : 'pointer', flex: 1, minWidth: 0 }}
                       title={needsKey ? 'needs an API key — configure below' : ''}>
                  <input type="checkbox" checked={on} disabled={needsKey || u.busy} onChange={() => toggle(feed)} style={{ accentColor: '#1f6feb' }} />
                  <span style={{ fontSize: 12, color: '#e6edf3', fontWeight: 600, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{feed.name}</span>
                  <span style={{ fontSize: 9, color: '#8b949e', background: '#161b22', padding: '1px 5px', borderRadius: 3 }}>{CATEGORY_LABEL[feed.category] || 'OSINT'}</span>
                  {feed.best_effort && <span style={{ fontSize: 9, color: '#d29922' }} title="best-effort scrape — may break if the site changes">⚠ scrape</span>}
                </label>
                {on && <button className="btn btn-ghost" style={{ fontSize: 10, padding: '2px 7px' }} disabled={u.busy} onClick={() => enable(feed)}>{u.busy ? '…' : '↻'}</button>}
                {feed.custom && <button className="btn btn-ghost" style={{ fontSize: 11, padding: '2px 6px', color: '#fca5a5' }} onClick={() => removeCustom(feed)}>×</button>}
              </div>
              <div style={{ fontSize: 10, color: '#6e7681', marginTop: 2 }}>
                {feed.description} <span style={{ color: '#484f58' }}>· {feed.attribution}</span>
              </div>

              {/* status / truncation / errors */}
              {st && (
                <div style={{ fontSize: 10, marginTop: 3, color: st.source === 'live' ? '#3fb950' : st.source === 'cache' ? '#d29922' : '#f0883e' }}>
                  {st.source === 'live' && `✓ ${st.count} shown${st.truncated ? ` of ${st.total} — zoom in / tighten filter` : ''} · ${st.as_of?.slice(11, 16)}Z`}
                  {st.source === 'cache' && `cached ${st.count ?? ''} (${st.as_of || 'offline'})${st.error ? ' — ' + st.error : ''}`}
                  {(st.source === 'unavailable' || st.source === 'error') && `unavailable — ${st.error || 'no data'}`}
                  {st.signup && <span style={{ color: '#6e7681' }}> · {st.signup}</span>}
                </div>
              )}

              {/* config form for keyed sources */}
              {needsKey && (
                <div style={{ marginTop: 5, display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 5 }}>
                  {(feed.config_fields || []).map(f => (
                    <input key={f} style={inputStyle} type={(f === 'api_key' || f === 'password') ? 'password' : 'text'} placeholder={f}
                           value={(u.config || {})[f] || ''} onChange={e => patchConfig(feed.id, f, e.target.value)} />
                  ))}
                  <button className="btn btn-primary" style={{ fontSize: 10, padding: '3px 8px', gridColumn: '1 / -1', justifySelf: 'start' }}
                          onClick={() => saveConfig(feed)}>Save key</button>
                  {feed.signup && <div style={{ gridColumn: '1 / -1', fontSize: 9, color: '#6e7681' }}>Get a key: {feed.signup}</div>}
                </div>
              )}

              {/* filters (shown for configured feeds) */}
              {!needsKey && (
                <div style={{ marginTop: 5, display: 'flex', flexWrap: 'wrap', gap: 8, alignItems: 'center', fontSize: 10, color: '#8b949e' }}>
                  {(feed.params || []).map(p => (
                    <label key={p.key} style={{ display: 'inline-flex', alignItems: 'center', gap: 3 }}>
                      {p.label}
                      {p.type === 'select'
                        ? <select style={{ ...inputStyle, marginTop: 0 }} value={u.params[p.key] ?? p.default} onChange={e => patchParam(feed.id, p.key, e.target.value)}>
                            {(p.options || []).map(o => <option key={o} value={o}>{o}</option>)}
                          </select>
                        : p.type === 'bool'
                        ? <input type="checkbox" checked={!!u.params[p.key]} onChange={e => patchParam(feed.id, p.key, e.target.checked)} />
                        : <input type={p.type === 'number' ? 'number' : 'text'} style={{ ...inputStyle, marginTop: 0, width: p.type === 'number' ? 56 : 110 }}
                                 min={p.min} max={p.max} value={u.params[p.key] ?? p.default} onChange={e => patchParam(feed.id, p.key, p.type === 'number' ? Number(e.target.value) : e.target.value)} />}
                    </label>
                  ))}
                  <label style={{ display: 'inline-flex', alignItems: 'center', gap: 3 }} title="Only load features inside the current map view">
                    <input type="checkbox" checked={!!u.bbox} onChange={e => patchUi(feed.id, { bbox: e.target.checked })} /> map view
                  </label>
                  <label style={{ display: 'inline-flex', alignItems: 'center', gap: 3 }} title="Hard cap on features sent to the map">
                    max <input type="number" style={{ ...inputStyle, marginTop: 0, width: 64 }} value={u.max} onChange={e => patchUi(feed.id, { max: e.target.value })} />
                  </label>
                  <label style={{ display: 'inline-flex', alignItems: 'center', gap: 3 }} title="Auto-refresh interval (0 = off)">
                    auto <input type="number" min={0} max={120} style={{ ...inputStyle, marginTop: 0, width: 48 }} value={u.autoMin} onChange={e => setAuto(feed, Number(e.target.value) || 0)} /> min
                  </label>
                </div>
              )}
            </div>
          )
        })}
      </div>

      {err && <div style={{ fontSize: 10, marginTop: 6, color: err.startsWith('✓') ? '#3fb950' : '#f85149' }}>{err}</div>}
    </div>
  )
}
