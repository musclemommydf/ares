// SPDX-License-Identifier: MIT OR Apache-2.0
// Copyright (c) 2026 Ares

/**
 * EmitterDetailCard — focused view of a single tracked emitter.
 *
 * Pulls the track-archive entry, summarises bearings + position history, lists
 * attached IQ + audio captures, and renders a small bearing-history sparkline.
 *
 * Designed for the right pane of the DF tab — operator clicks a track marker
 * on the map, this card surfaces "what do we know about this contact?" in
 * one glance. Mirrors the per-target detail cards of CRFS RFEye Site and
 * R&S DDF / PR100 spectrum-monitor consoles.
 */
import { useEffect, useMemo, useRef, useState } from 'react'
import api from '../../api/client'

const fmtTime = (epoch) => {
  if (!epoch) return '—'
  try { return new Date(epoch * 1000).toISOString().replace('T', ' ').slice(0, 19) + 'Z' }
  catch { return String(epoch) }
}

export default function EmitterDetailCard({ trackId, onClose }) {
  const [archive, setArchive] = useState(null)
  const [err, setErr] = useState('')
  const sparkRef = useRef(null)

  useEffect(() => {
    if (!trackId) return
    setErr('')
    api.get(`/df/track_archive/${encodeURIComponent(trackId)}`)
      .then(r => setArchive(r.data))
      .catch(e => setErr(String(e?.response?.data?.detail || e?.message || e)))
  }, [trackId])

  const stats = useMemo(() => {
    if (!archive) return null
    const obs = archive.observations || []
    const pos = archive.positions || []
    const bearings = obs.map(o => o.azimuth_deg).filter(Number.isFinite)
    const meanBearing = bearings.length
      ? ((Math.atan2(
          bearings.reduce((s, b) => s + Math.sin(b * Math.PI / 180), 0) / bearings.length,
          bearings.reduce((s, b) => s + Math.cos(b * Math.PI / 180), 0) / bearings.length,
        ) * 180 / Math.PI) + 360) % 360
      : null
    return {
      n_obs: obs.length, n_pos: pos.length,
      mean_bearing: meanBearing,
      latest_pos: pos.length ? pos[pos.length - 1] : null,
      first_t: obs.length ? obs[0].t : (pos[0]?.t || archive.created_t),
      last_t: archive.updated_t,
    }
  }, [archive])

  // Bearing sparkline
  useEffect(() => {
    const c = sparkRef.current; if (!c || !archive) return
    const w = 240, h = 60; c.width = w; c.height = h
    const ctx = c.getContext('2d')
    ctx.fillStyle = '#0d1117'; ctx.fillRect(0, 0, w, h)
    const obs = archive.observations || []
    if (obs.length < 2) return
    const t0 = obs[0].t, t1 = obs[obs.length - 1].t || t0 + 1
    const span = Math.max(1, t1 - t0)
    ctx.strokeStyle = '#06d6a0'; ctx.lineWidth = 1.5; ctx.beginPath()
    obs.forEach((o, i) => {
      const x = (o.t - t0) / span * (w - 4) + 2
      const y = h - ((o.azimuth_deg ?? 0) / 360) * (h - 4) - 2
      i === 0 ? ctx.moveTo(x, y) : ctx.lineTo(x, y)
    })
    ctx.stroke()
    // axis ticks (90°, 180°, 270°)
    ctx.strokeStyle = '#1c2530'; ctx.lineWidth = 0.5
    for (const deg of [90, 180, 270]) {
      const y = h - (deg / 360) * (h - 4) - 2
      ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(w, y); ctx.stroke()
    }
    ctx.fillStyle = '#5b6b7a'; ctx.font = '8px system-ui,sans-serif'
    ctx.fillText('360°', 2, 9); ctx.fillText('0°', 2, h - 2)
  }, [archive])

  if (!trackId) return null
  return (
    <div style={{ background: '#0d1117', border: '1px solid #21262d', borderRadius: 8, padding: 10, color: '#e6edf3', fontSize: 12 }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 6 }}>
        <div style={{ fontWeight: 700 }}>Emitter detail</div>
        <code style={{ color: '#8b949e', fontSize: 10 }}>{trackId.slice(0, 12)}</code>
        <div style={{ flex: 1 }} />
        {onClose && <button className="btn btn-ghost" style={{ padding: '2px 8px', fontSize: 11, color: '#fca5a5' }} onClick={onClose}>×</button>}
      </div>
      {err && <div style={{ fontSize: 11, color: '#f85149' }}>{err}</div>}
      {archive && stats && (
        <>
          <table style={{ borderCollapse: 'collapse', fontSize: 11, width: '100%' }}>
            <tbody>
              <Row k="Frequency" v={archive.frequency_hz ? `${(archive.frequency_hz / 1e6).toFixed(3)} MHz` : '—'} />
              <Row k="Observations" v={stats.n_obs} />
              <Row k="Positions" v={stats.n_pos} />
              <Row k="Mean bearing" v={stats.mean_bearing != null ? `${stats.mean_bearing.toFixed(1)}°` : '—'} />
              <Row k="First seen" v={fmtTime(stats.first_t)} />
              <Row k="Last seen" v={fmtTime(stats.last_t)} />
              {stats.latest_pos && <>
                <Row k="Lat / lon" v={`${stats.latest_pos.lat.toFixed(5)}, ${stats.latest_pos.lon.toFixed(5)}`} />
                <Row k="CEP" v={`${(stats.latest_pos.cep_m || 0).toFixed(0)} m`} />
              </>}
              {(archive.modulation_history?.length > 0) && (
                <Row k="Modulation" v={archive.modulation_history[archive.modulation_history.length - 1].label +
                                      ` (${(archive.modulation_history[archive.modulation_history.length - 1].confidence * 100).toFixed(0)}%)`} />
              )}
            </tbody>
          </table>
          <div style={{ marginTop: 6, fontSize: 10, color: '#8b949e' }}>Bearing history</div>
          <canvas ref={sparkRef} style={{ display: 'block', borderRadius: 4 }} />
          {(archive.iq_captures?.length > 0 || archive.audio_captures?.length > 0) && (
            <div style={{ marginTop: 8, borderTop: '1px solid #21262d', paddingTop: 6 }}>
              <div style={{ fontSize: 10, color: '#8b949e', marginBottom: 4 }}>Attached captures</div>
              {(archive.iq_captures || []).map((c, i) => (
                <div key={`iq-${i}`} style={{ fontSize: 10, color: '#c9d1d9' }}>📡 IQ · {c.sigmf_path.split('/').pop()}</div>
              ))}
              {(archive.audio_captures || []).map((c, i) => (
                <div key={`au-${i}`} style={{ fontSize: 10, color: '#c9d1d9' }}>🔊 Audio · {c.wav_path.split('/').pop()}</div>
              ))}
            </div>
          )}
        </>
      )}
    </div>
  )
}

function Row({ k, v }) {
  return (
    <tr>
      <td style={{ color: '#8b949e', padding: '2px 6px 2px 0', whiteSpace: 'nowrap', fontSize: 10 }}>{k}</td>
      <td style={{ color: '#e6edf3', padding: '2px 0' }}>{v}</td>
    </tr>
  )
}
