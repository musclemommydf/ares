/**
 * ActivityHeatmap — hour-of-day × day-of-week activity grid.
 *
 * Drives "they go quiet 02–06 every day" mission-planning insights. Reads
 * the track archive's positions[] timestamps and bins them.
 *
 * 7 rows (Mon..Sun in operator-local time) × 24 columns (00..23h). Cells
 * show normalised activity (0..1) as colour intensity. Tooltip shows the
 * count + the absolute hour.
 */
import { useEffect, useMemo, useState } from 'react'
import api from '../../api/client'

const DOW_LABELS = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun']

export default function ActivityHeatmap({ trackId = null, height = 200 }) {
  const [archive, setArchive] = useState(null)
  const [err, setErr] = useState('')
  const [trackList, setTrackList] = useState([])
  const [selected, setSelected] = useState(trackId)

  useEffect(() => {
    api.get('/df/track_archive').then(r => setTrackList(r.data?.tracks || [])).catch(e => setErr(String(e?.message || e)))
  }, [])
  useEffect(() => {
    if (!selected) { setArchive(null); return }
    api.get(`/df/track_archive/${encodeURIComponent(selected)}`)
      .then(r => setArchive(r.data))
      .catch(e => setErr(String(e?.response?.data?.detail || e?.message || e)))
  }, [selected])

  const grid = useMemo(() => {
    const counts = Array.from({ length: 7 }, () => new Array(24).fill(0))
    if (archive?.positions?.length) {
      for (const p of archive.positions) {
        const d = new Date(Number(p.t) * 1000)
        // JS getDay: 0=Sunday..6=Saturday. Remap to Mon=0..Sun=6 for ops display.
        const dow = (d.getDay() + 6) % 7
        const hr = d.getHours()
        counts[dow][hr] += 1
      }
    }
    let max = 0
    for (const row of counts) for (const v of row) if (v > max) max = v
    return { counts, max }
  }, [archive])

  const cellColor = (v) => {
    if (grid.max === 0 || v === 0) return '#0d1117'
    const t = v / grid.max
    // Viridis-ish ramp
    const r = Math.round(255 * (0.27 + 0.7 * t * t))
    const g = Math.round(255 * (0.10 + 0.75 * t))
    const b = Math.round(255 * (0.40 + 0.30 * (1 - t)))
    return `rgb(${r},${g},${b})`
  }

  return (
    <div style={{ background: '#0d1117', border: '1px solid #21262d', borderRadius: 8, padding: 8, color: '#e6edf3', fontSize: 12 }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 6 }}>
        <div style={{ fontWeight: 700 }}>Activity heatmap</div>
        <select value={selected || ''} onChange={(e) => setSelected(e.target.value || null)}
          style={{ background: '#0d1117', border: '1px solid #30363d', borderRadius: 4, color: '#e6edf3', fontSize: 11, padding: '2px 6px' }}>
          <option value="">— Pick a track —</option>
          {trackList.map((t) => (
            <option key={t.track_id} value={t.track_id}>
              {t.track_id.slice(0, 12)} · {(t.frequency_hz / 1e6).toFixed(3)} MHz · {t.n_positions} pos
            </option>
          ))}
        </select>
        <div style={{ flex: 1 }} />
        {grid.max > 0 && <span style={{ fontSize: 10, color: '#8b949e' }}>peak {grid.max}/hr</span>}
      </div>
      {err && <div style={{ fontSize: 10, color: '#f85149' }}>{err}</div>}
      <table style={{ borderCollapse: 'collapse', width: '100%' }}>
        <thead>
          <tr>
            <th></th>
            {Array.from({ length: 24 }, (_, h) => (
              <th key={h} style={{ fontSize: 8, color: '#8b949e', fontWeight: 400 }}>{h % 6 === 0 ? h : ''}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {DOW_LABELS.map((label, dow) => (
            <tr key={label}>
              <td style={{ fontSize: 10, color: '#8b949e', paddingRight: 4 }}>{label}</td>
              {grid.counts[dow].map((v, hr) => (
                <td key={hr} title={`${label} ${hr}:00 — ${v}`}
                  style={{ background: cellColor(v), width: `${100 / 24}%`,
                            height: Math.max(8, height / 9), border: '1px solid #0a0e13' }} />
              ))}
            </tr>
          ))}
        </tbody>
      </table>
      <div style={{ fontSize: 9, color: '#484f58', marginTop: 4 }}>
        {archive ? `${archive.positions?.length || 0} positions over ${((archive.updated_t - archive.created_t) / 3600).toFixed(1)} h` : 'select a track'}
      </div>
    </div>
  )
}
