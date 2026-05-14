/**
 * TrackHistoryPanel — manage recorded movement tracks (Co-Opt movers, GPS
 * feeds, drone telemetry, etc.) plus a time scrubber for replay.
 *
 * The store ([useTrackHistory]) holds the data; this component is the UI. The
 * actual on-map rendering lives in MapView's track effect.
 */
import { useEffect, useState } from 'react'
import { useTrackHistory } from '../../store/trackHistory'

const fmtTime = (t) => {
  try { return new Date(t).toISOString().replace('T', ' ').slice(11, 19) } catch { return '—' }
}
const fmtDuration = (ms) => {
  if (!ms || ms < 0) return '—'
  const s = Math.round(ms / 1000)
  if (s < 60) return `${s}s`
  const m = Math.floor(s / 60), rem = s % 60
  if (m < 60) return `${m}m ${rem}s`
  return `${Math.floor(m / 60)}h ${m % 60}m`
}

export default function TrackHistoryPanel() {
  const tracks = useTrackHistory((s) => s.tracks)
  const playback = useTrackHistory((s) => s.playback)
  const startRecording = useTrackHistory((s) => s.startRecording)
  const stopRecording = useTrackHistory((s) => s.stopRecording)
  const setVisible = useTrackHistory((s) => s.setVisible)
  const renameTrack = useTrackHistory((s) => s.renameTrack)
  const removeTrack = useTrackHistory((s) => s.removeTrack)
  const startPlayback = useTrackHistory((s) => s.startPlayback)
  const scrubTo = useTrackHistory((s) => s.scrubTo)
  const stopPlayback = useTrackHistory((s) => s.stopPlayback)

  const list = Object.values(tracks)
  // Auto-play: when a playback is started, advance the scrubber on a timer
  // until it hits the end. The user can pause by removing the playback.
  const [playing, setPlaying] = useState(false)
  useEffect(() => {
    if (!playback || !playing) return
    const tr = tracks[playback.trackId]; if (!tr || tr.points.length < 2) return
    const end = tr.points[tr.points.length - 1].t
    const id = setInterval(() => {
      const cur = useTrackHistory.getState().playback
      if (!cur) { setPlaying(false); return }
      const next = cur.t + 1000             // step 1s/tick; tweak to taste
      if (next >= end) { scrubTo(end); setPlaying(false); return }
      scrubTo(next)
    }, 100)                                  // 10× realtime by default
    return () => clearInterval(id)
  }, [playback, playing, tracks, scrubTo])

  return (
    <div style={{ height: '100%', display: 'flex', flexDirection: 'column' }}>
      <div style={{ padding: '8px 10px', borderBottom: '1px solid #21262d',
                    fontSize: 11, color: '#8b949e' }}>
        Recorded movement tracks. Feed them via the API (Co-Opt movers, drone
        telemetry) or use the SDR panel's "Record GPS" button.
      </div>
      {list.length === 0 && (
        <div style={{ padding: '24px 16px', color: '#484f58', fontSize: 12, textAlign: 'center' }}>
          No tracks yet.
        </div>
      )}
      <div style={{ flex: 1, overflowY: 'auto' }}>
        {list.map((tr) => {
          const t0 = tr.points[0]?.t, t1 = tr.points[tr.points.length - 1]?.t
          const isPlayingThis = playback?.trackId === tr.id
          return (
            <div key={tr.id} style={{ padding: '8px 10px', borderBottom: '1px solid #161b22' }}>
              <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 4 }}>
                <input type="checkbox" checked={tr.visible} onChange={(e) => setVisible(tr.id, e.target.checked)}
                  style={{ cursor: 'pointer' }} />
                <span style={{ width: 10, height: 10, borderRadius: '50%', background: tr.color, flexShrink: 0 }} />
                <input value={tr.name} onChange={(e) => renameTrack(tr.id, e.target.value)}
                  style={{ flex: 1, background: 'transparent', border: 'none', color: '#e6edf3',
                           fontSize: 12, fontWeight: 600, outline: 'none' }} />
                <button className="btn btn-ghost" style={{ padding: '2px 6px', fontSize: 11, color: '#fca5a5' }}
                  onClick={() => removeTrack(tr.id)}>×</button>
              </div>
              <div style={{ fontSize: 10, color: '#8b949e' }}>
                {tr.points.length} pts · {fmtDuration((t1 ?? 0) - (t0 ?? 0))}
                {tr.recording && <span style={{ color: '#ef4444', marginLeft: 6 }}>● REC</span>}
              </div>
              <div style={{ display: 'flex', gap: 4, marginTop: 6 }}>
                {tr.recording ? (
                  <button className="btn btn-ghost" style={{ flex: 1, fontSize: 10, padding: '3px 6px' }}
                    onClick={() => stopRecording(tr.id)}>⏸ Stop rec</button>
                ) : (
                  <button className="btn btn-ghost" style={{ flex: 1, fontSize: 10, padding: '3px 6px' }}
                    onClick={() => startRecording(tr.id)}>● Record</button>
                )}
                <button className="btn btn-ghost" style={{ flex: 1, fontSize: 10, padding: '3px 6px',
                                                            color: isPlayingThis ? '#06d6a0' : undefined }}
                  disabled={tr.points.length < 2}
                  onClick={() => { if (isPlayingThis) { stopPlayback(); setPlaying(false) }
                                    else { startPlayback(tr.id); setPlaying(true) } }}>
                  {isPlayingThis ? '⏹ Stop replay' : '▶ Replay'}
                </button>
              </div>
              {isPlayingThis && t0 != null && t1 != null && t1 > t0 && (
                <div style={{ marginTop: 6 }}>
                  <input type="range" min={t0} max={t1}
                    value={playback?.t ?? t0}
                    onChange={(e) => { scrubTo(Number(e.target.value)); setPlaying(false) }}
                    style={{ width: '100%' }} />
                  <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: 9, color: '#484f58' }}>
                    <span>{fmtTime(t0)}</span>
                    <span>{fmtTime(playback?.t ?? t0)}</span>
                    <span>{fmtTime(t1)}</span>
                  </div>
                </div>
              )}
            </div>
          )
        })}
      </div>
    </div>
  )
}
