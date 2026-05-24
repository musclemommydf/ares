// SPDX-License-Identifier: MIT OR Apache-2.0
// Copyright (c) 2026 Ares

/**
 * EmitterAnalyticsPanel — wraps ActivityHeatmap + EmitterDetailCard with a
 * shared track selector. Mounted as the "Activity" tab of the bottom panel.
 *
 * Both children read from `/df/track_archive`. We do the track listing here so
 * one fetch feeds both views.
 */
import { useEffect, useState } from 'react'
import api from '../../api/client'
import ActivityHeatmap from '../Tools/ActivityHeatmap'
import EmitterDetailCard from '../Tools/EmitterDetailCard'

export default function EmitterAnalyticsPanel() {
  const [tracks, setTracks] = useState([])
  const [selected, setSelected] = useState(null)
  const [err, setErr] = useState('')

  useEffect(() => {
    let stopped = false
    const tick = () => {
      api.get('/df/track_archive')
        .then((r) => { if (!stopped) { setTracks(r.data?.tracks || []); setErr('') } })
        .catch((e) => { if (!stopped) setErr(String(e?.response?.data?.detail || e?.message || e)) })
    }
    tick()
    const h = setInterval(() => { if (!document.hidden) tick() }, 5000)   // pause while hidden
    return () => { stopped = true; clearInterval(h) }
  }, [])

  return (
    <div style={{ height: '100%', display: 'flex', minHeight: 0, gap: 8, padding: 8,
                  flexWrap: 'wrap', overflowY: 'auto' }}>
      <div style={{ flex: '1 1 320px', minWidth: 280, maxWidth: 520 }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 6 }}>
          <span style={{ fontSize: 11, color: '#8b949e' }}>Track:</span>
          <select value={selected || ''} onChange={(e) => setSelected(e.target.value || null)}
            style={{ flex: 1, background: '#0d1117', border: '1px solid #30363d',
                     borderRadius: 4, color: '#e6edf3', fontSize: 11, padding: '3px 6px' }}>
            <option value="">— Pick a tracked emitter —</option>
            {tracks.map((t) => (
              <option key={t.track_id} value={t.track_id}>
                {t.track_id.slice(0, 12)} · {(t.frequency_hz / 1e6).toFixed(3)} MHz · {t.n_positions} pos
              </option>
            ))}
          </select>
        </div>
        {err && <div style={{ fontSize: 10, color: '#f85149', marginBottom: 6 }}>{err}</div>}
        <ActivityHeatmap trackId={selected} />
      </div>
      <div style={{ flex: '1 1 320px', minWidth: 280, maxWidth: 520 }}>
        {selected
          ? <EmitterDetailCard trackId={selected} onClose={() => setSelected(null)} />
          : <div style={{ background: '#0d1117', border: '1px solid #21262d', borderRadius: 8,
                          padding: 16, fontSize: 12, color: '#484f58', textAlign: 'center' }}>
              Pick an emitter from the dropdown to see its detail card.
            </div>}
      </div>
    </div>
  )
}
