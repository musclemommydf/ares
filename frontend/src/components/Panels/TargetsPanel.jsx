// SPDX-License-Identifier: MIT OR Apache-2.0
// Copyright (c) 2026 Ares

import { useEffect, useMemo, useRef, useState } from 'react'
import { Crosshair, RefreshCw, Send, Trash2, Filter, Radio, Sigma } from 'lucide-react'
import {
  listTargets, getTarget, getTargetRange, recomputeTargetFix, forgetTarget, getTargetKinds,
} from '../../api/client'

const card = { background: '#0d1117', border: '1px solid #21262d', borderRadius: 8, padding: 10, marginBottom: 10 }
const th   = { textAlign: 'left', fontSize: 10, color: '#8b949e', fontWeight: 600, padding: '4px 6px', whiteSpace: 'nowrap' }
const td   = { fontSize: 11, color: '#c9d1d9', padding: '4px 6px', borderTop: '1px solid #161b22', whiteSpace: 'nowrap' }
const inp  = { background: '#0d1117', color: '#c9d1d9', border: '1px solid #21262d', borderRadius: 4, padding: '3px 6px', fontSize: 11 }
const mono = { fontFamily: 'ui-monospace, SFMono-Regular, Menlo, monospace' }

function fmtMs(t) { if (!t) return '—'; const d = new Date(t * 1000); return d.toLocaleTimeString() }
function fmtDist(m) { if (m == null) return '—'; if (m < 1000) return `${m.toFixed(0)} m`; return `${(m / 1000).toFixed(2)} km` }
function fmtRssi(d) { return (d == null) ? '—' : `${d.toFixed(1)} dBm` }

// Tiny RSSI-vs-time sparkline that doesn't require a chart lib.
function RssiSpark({ history, width = 320, height = 70 }) {
  const ref = useRef(null)
  useEffect(() => {
    const c = ref.current; if (!c) return
    const w = c.width = width, h = c.height = height
    const ctx = c.getContext('2d')
    ctx.fillStyle = '#000'; ctx.fillRect(0, 0, w, h)
    const pts = history.filter(o => o.rssi_dbm != null).map(o => [o.t, o.rssi_dbm])
    if (pts.length < 2) {
      ctx.fillStyle = '#6e7681'; ctx.font = '10px monospace'
      ctx.fillText('not enough RSSI samples', 6, h / 2)
      return
    }
    const t0 = pts[0][0], t1 = pts[pts.length - 1][0]
    const lo = Math.min(...pts.map(p => p[1])) - 2
    const hi = Math.max(...pts.map(p => p[1])) + 2
    const X = t => 4 + ((t - t0) / Math.max(1e-3, (t1 - t0))) * (w - 8)
    const Y = r => h - 4 - ((r - lo) / Math.max(1e-3, (hi - lo))) * (h - 8)
    // axes / labels
    ctx.strokeStyle = '#21262d'; ctx.beginPath()
    ctx.moveTo(0, Y(lo)); ctx.lineTo(w, Y(lo)); ctx.moveTo(0, Y(hi)); ctx.lineTo(w, Y(hi)); ctx.stroke()
    ctx.fillStyle = '#6e7681'; ctx.font = '9px monospace'
    ctx.fillText(`${hi.toFixed(0)} dBm`, 4, Y(hi) + 9)
    ctx.fillText(`${lo.toFixed(0)} dBm`, 4, Y(lo) - 2)
    // trace
    ctx.strokeStyle = '#22d3ee'; ctx.lineWidth = 1.5; ctx.beginPath()
    pts.forEach((p, i) => { const x = X(p[0]), y = Y(p[1]); i === 0 ? ctx.moveTo(x, y) : ctx.lineTo(x, y) })
    ctx.stroke()
    // peak marker
    const peak = pts.reduce((a, b) => (b[1] > a[1] ? b : a))
    ctx.fillStyle = '#f59e0b'
    ctx.beginPath(); ctx.arc(X(peak[0]), Y(peak[1]), 3, 0, Math.PI * 2); ctx.fill()
  }, [history, width, height])
  return <canvas ref={ref} style={{ width: '100%', height, display: 'block' }} />
}

function DetailCard({ target, onSendToMap, onForget }) {
  const [full, setFull] = useState(null)
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState('')
  useEffect(() => {
    let alive = true
    getTarget(target.kind, target.value, { include_history: true })
      .then(d => alive && setFull(d)).catch(e => alive && setErr(String(e?.message || e)))
    return () => { alive = false }
  }, [target.kind, target.value])

  const recompute = async () => {
    setBusy(true); setErr('')
    try {
      await recomputeTargetFix(target.kind, target.value)
      const d = await getTarget(target.kind, target.value, { include_history: true })
      setFull(d)
    } catch (e) { setErr(String(e?.response?.data?.detail || e?.message || e)) }
    finally { setBusy(false) }
  }

  const send = () => {
    if (!full?.position) return
    onSendToMap?.({
      lat: full.position.lat, lon: full.position.lon,
      label: `${target.label || target.kind.toUpperCase()}: ${target.value} (CEP ${Math.round(full.position.cep_m || 0)} m)`,
      method_id: full.position.method, method_name: `target/${target.kind}`,
      cep_m: full.position.cep_m, raw: full,
    })
  }

  if (!full) return <div style={{ fontSize: 11, color: '#6e7681', padding: 8 }}>{err || 'Loading…'}</div>
  const hist = full.history || []
  return (
    <div style={{ ...card, background: '#0a0e13', marginTop: 6 }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 6 }}>
        <Sigma size={14} color="#22d3ee" />
        <b style={{ color: '#e6edf3' }}>{full.label || full.kind.toUpperCase()}</b>
        <code style={{ fontSize: 11, color: '#c9d1d9', ...mono }}>{full.value}</code>
        <span style={{ flex: 1 }} />
        <button className="btn btn-ghost" style={{ fontSize: 10, padding: '3px 8px' }} disabled={busy} onClick={recompute}>
          {busy ? <RefreshCw size={11} className="spin" /> : <RefreshCw size={11} />} Recompute fix
        </button>
        {full.position && (
          <button className="btn btn-primary" style={{ fontSize: 10, padding: '3px 8px' }} onClick={send}>
            <Send size={11} /> Send to map
          </button>
        )}
        <button className="btn btn-ghost" style={{ fontSize: 10, padding: '3px 8px' }}
                 onClick={() => onForget?.(target.kind, target.value)}>
          <Trash2 size={11} /> Forget
        </button>
      </div>
      <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 8, marginBottom: 6 }}>
        <div>
          <div style={{ fontSize: 10, color: '#8b949e' }}>Peak RSSI</div>
          <div style={{ fontSize: 13, color: '#f59e0b' }}>{fmtRssi(full.peak_rssi_dbm)}</div>
          {full.peak_observation && (
            <div style={{ fontSize: 10, color: '#6e7681' }}>
              @ {full.peak_observation.lat?.toFixed(5)}, {full.peak_observation.lon?.toFixed(5)} · {fmtMs(full.peak_observation.t)}
            </div>
          )}
        </div>
        <div>
          <div style={{ fontSize: 10, color: '#8b949e' }}>Range estimate</div>
          <div style={{ fontSize: 13, color: '#22d3ee' }}>{fmtDist(full.range_m_estimate)}{full.range_uncertainty_m ? ` ± ${fmtDist(full.range_uncertainty_m)}` : ''}</div>
          <div style={{ fontSize: 10, color: '#6e7681' }}>{full.range_method || '—'}</div>
        </div>
        <div>
          <div style={{ fontSize: 10, color: '#8b949e' }}>Observations</div>
          <div style={{ fontSize: 13, color: '#c9d1d9' }}>{full.n_obs} samples · top-K {full.rolling_top_k?.length || 0}</div>
          <div style={{ fontSize: 10, color: '#6e7681' }}>first {fmtMs(full.first_seen_t)} · last {fmtMs(full.last_seen_t)}</div>
        </div>
        <div>
          <div style={{ fontSize: 10, color: '#8b949e' }}>Position estimate</div>
          {full.position
            ? <>
                <div style={{ fontSize: 13, color: '#06d6a0' }}>{full.position.lat.toFixed(5)}, {full.position.lon.toFixed(5)}</div>
                <div style={{ fontSize: 10, color: '#6e7681' }}>CEP {fmtDist(full.position.cep_m)} · {full.position.method}</div>
              </>
            : <div style={{ fontSize: 11, color: '#6e7681' }}>not computable yet — need ≥ 3 distinct observer positions or any AoA</div>}
        </div>
      </div>
      <div style={{ fontSize: 10, color: '#8b949e', marginBottom: 2 }}>RSSI vs time</div>
      <RssiSpark history={hist} />
      {Object.keys(full.metadata || {}).length > 0 && (
        <details style={{ marginTop: 6 }}>
          <summary style={{ fontSize: 10, color: '#8b949e', cursor: 'pointer' }}>metadata</summary>
          <pre style={{ fontSize: 9, color: '#c9d1d9', background: '#0d1117', padding: 6, marginTop: 4, overflow: 'auto' }}>
            {JSON.stringify(full.metadata, null, 2)}
          </pre>
        </details>
      )}
    </div>
  )
}

export default function TargetsPanel({ onSendToMap }) {
  const [targets, setTargets] = useState([])
  const [kinds, setKinds] = useState([])
  const [kindFilter, setKindFilter] = useState('')
  const [minObs, setMinObs] = useState(1)
  const [sortKey, setSortKey] = useState('peak')
  const [expanded, setExpanded] = useState(null)        // 'kind/value'
  const [err, setErr] = useState('')

  const refresh = async () => {
    try {
      const d = await listTargets({ kind: kindFilter || undefined, min_obs: minObs })
      setTargets(d.targets || []); setErr('')
    } catch (e) { setErr(String(e?.response?.data?.detail || e?.message || e)) }
  }
  useEffect(() => {
    let alive = true
    getTargetKinds().then(d => alive && setKinds(d.kinds || [])).catch(() => {})
    refresh()
    const h = setInterval(() => { if (!document.hidden) refresh() }, 4000)   // pause while hidden
    return () => { alive = false; clearInterval(h) }
  }, [kindFilter, minObs])    // eslint-disable-line

  const sorted = useMemo(() => {
    const arr = [...targets]
    if (sortKey === 'peak') arr.sort((a, b) => (b.peak_rssi_dbm ?? -1e9) - (a.peak_rssi_dbm ?? -1e9))
    else if (sortKey === 'recent') arr.sort((a, b) => (b.last_seen_t || 0) - (a.last_seen_t || 0))
    else if (sortKey === 'obs') arr.sort((a, b) => b.n_obs - a.n_obs)
    return arr
  }, [targets, sortKey])

  const onForget = async (kind, value) => {
    try { await forgetTarget(kind, value); setExpanded(null); refresh() }
    catch (e) { setErr(String(e?.message || e)) }
  }

  return (
    <div style={{ height: '100%', overflowY: 'auto', padding: 12 }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 8 }}>
        <Radio size={16} color="#22d3ee" />
        <b style={{ color: '#e6edf3' }}>Targets — per-identifier peak-RSSI + range tracker</b>
      </div>
      <div style={{ fontSize: 11, color: '#8b949e', marginBottom: 10 }}>
        Devices tracked by their stable identifier — IMSI / TMSI / IMEI / RNTI / MAC / BLE BD_ADDR / ICAO / DMR-RID / UAS serial.
        Every observation (RSSI from any decoder, AoA from any DF source) lands here keyed by ID. Peak RSSI is sampled live;
        range / position upgrade automatically from Friis single-shot → multi-pose RSS-ML → AoA-fused ML grid as observations accumulate.
      </div>

      {/* Filter row */}
      <div style={{ ...card, padding: 8 }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap' }}>
          <Filter size={12} color="#8b949e" />
          <label style={{ fontSize: 11, color: '#8b949e' }}>kind
            <select value={kindFilter} onChange={e => setKindFilter(e.target.value)} style={{ ...inp, marginLeft: 6 }}>
              <option value="">all</option>
              {kinds.map(k => <option key={k.id} value={k.id}>{k.label}</option>)}
            </select>
          </label>
          <label style={{ fontSize: 11, color: '#8b949e' }}>min obs
            <input type="number" value={minObs} min={1} max={100}
                   onChange={e => setMinObs(Math.max(1, Number(e.target.value) || 1))}
                   style={{ ...inp, marginLeft: 6, width: 50 }} />
          </label>
          <label style={{ fontSize: 11, color: '#8b949e' }}>sort
            <select value={sortKey} onChange={e => setSortKey(e.target.value)} style={{ ...inp, marginLeft: 6 }}>
              <option value="peak">peak RSSI ↓</option>
              <option value="recent">last seen ↓</option>
              <option value="obs">N obs ↓</option>
            </select>
          </label>
          <button className="btn btn-ghost" style={{ fontSize: 10, padding: '3px 8px' }} onClick={refresh}>
            <RefreshCw size={11} /> Refresh
          </button>
          <span style={{ flex: 1 }} />
          <span style={{ fontSize: 10, color: '#6e7681' }}>{sorted.length} target(s)</span>
        </div>
        {err && <div style={{ fontSize: 10, color: '#f0883e', marginTop: 4 }}>{err}</div>}
      </div>

      {sorted.length === 0 ? (
        <div style={{ ...card, padding: 14, textAlign: 'center', color: '#6e7681', fontSize: 12 }}>
          No targets tracked yet. Targets land here automatically as decoders run — start a cellular / WiFi / BLE session from
          the SDR console, or push observations via <code>POST /api/v1/targets/{`{kind}`}/{`{value}`}/observe</code>.
        </div>
      ) : (
        <div style={{ ...card, padding: 0, overflow: 'hidden' }}>
          <table style={{ width: '100%', borderCollapse: 'collapse' }}>
            <thead><tr style={{ position: 'sticky', top: 0, background: '#0d1117' }}>
              <th style={th}>Kind</th><th style={th}>Identifier</th><th style={th}>Peak RSSI</th>
              <th style={th}>Range</th><th style={th}>N obs</th>
              <th style={th}>Last seen</th><th style={th}>Position</th><th style={th}></th>
            </tr></thead>
            <tbody>
              {sorted.map(t => {
                const key = `${t.kind}/${t.value}`
                const isOpen = expanded === key
                return (
                  <>
                    <tr key={key} onClick={() => setExpanded(isOpen ? null : key)} style={{ cursor: 'pointer' }}>
                      <td style={td}>{t.label}</td>
                      <td style={{ ...td, ...mono, color: '#c9d1d9' }}>{t.value}</td>
                      <td style={{ ...td, color: '#f59e0b' }}>{fmtRssi(t.peak_rssi_dbm)}</td>
                      <td style={td}>{fmtDist(t.range_m_estimate)}</td>
                      <td style={td}>{t.n_obs}</td>
                      <td style={td}>{fmtMs(t.last_seen_t)}</td>
                      <td style={td}>{t.position
                          ? <span style={{ color: '#06d6a0' }}>{t.position.lat.toFixed(4)}, {t.position.lon.toFixed(4)}</span>
                          : <span style={{ color: '#6e7681' }}>—</span>}</td>
                      <td style={td}><span style={{ color: '#58a6ff', fontSize: 10 }}>{isOpen ? '▾' : '▸'}</span></td>
                    </tr>
                    {isOpen && (
                      <tr><td colSpan={8} style={{ padding: 6, background: '#0a0e13' }}>
                        <DetailCard target={t} onSendToMap={onSendToMap} onForget={onForget} />
                      </td></tr>
                    )}
                  </>
                )
              })}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}
