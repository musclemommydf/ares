// SPDX-License-Identifier: MIT OR Apache-2.0
// Copyright (c) 2026 Ares

/**
 * DfAlertsSettings — UI for the DF-alerts store ([useDfAlerts]).
 *
 * Compact panel exposing the master toggle, volume slider, per-event toggles
 * (new LoB / cut / fix / emitter), desktop-notification permission button, and
 * a "test tone" so the operator can verify before the action gets busy.
 */
import { useEffect, useState } from 'react'
import { useDfAlerts, ALERT_EVENTS, ALERT_EVENT_LABEL } from '../../store/dfAlerts'

export default function DfAlertsSettings() {
  const enabled = useDfAlerts((s) => s.enabled)
  const setEnabled = useDfAlerts((s) => s.setEnabled)
  const sound = useDfAlerts((s) => s.sound)
  const setSound = useDfAlerts((s) => s.setSound)
  const desktop = useDfAlerts((s) => s.desktop)
  const setDesktop = useDfAlerts((s) => s.setDesktop)
  const volume = useDfAlerts((s) => s.volume)
  const setVolume = useDfAlerts((s) => s.setVolume)
  const perEvent = useDfAlerts((s) => s.perEvent)
  const togglePerEvent = useDfAlerts((s) => s.togglePerEvent)
  const testTone = useDfAlerts((s) => s.testTone)
  const requestDesktopPermission = useDfAlerts((s) => s.requestDesktopPermission)

  const [permission, setPermission] = useState(
    typeof Notification !== 'undefined' ? Notification.permission : 'denied'
  )
  useEffect(() => {
    if (typeof Notification === 'undefined') return
    setPermission(Notification.permission)
  }, [enabled, desktop])

  const requestPerm = async () => {
    const p = await requestDesktopPermission()
    setPermission(p)
  }

  return (
    <div style={{ background: '#0d1117', border: '1px solid #21262d', borderRadius: 8, padding: '10px 12px' }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 8 }}>
        <label style={{ display: 'flex', alignItems: 'center', gap: 8, cursor: 'pointer' }}>
          <input type="checkbox" checked={enabled} onChange={(e) => setEnabled(e.target.checked)}
            style={{ cursor: 'pointer', accentColor: '#06d6a0' }} />
          <span style={{ fontWeight: 700, color: '#e6edf3', fontSize: 12 }}>DF alerts</span>
        </label>
        <div style={{ flex: 1 }} />
        <button className="btn btn-ghost" style={{ fontSize: 10, padding: '2px 8px' }}
          onClick={() => testTone('newLoB')}>Test tone</button>
      </div>

      <div style={{ opacity: enabled ? 1 : 0.45, pointerEvents: enabled ? 'auto' : 'none' }}>
        <div style={{ display: 'flex', gap: 10, alignItems: 'center', marginBottom: 8 }}>
          <label style={{ display: 'flex', alignItems: 'center', gap: 6, fontSize: 11, color: '#c9d1d9' }}>
            <input type="checkbox" checked={sound} onChange={(e) => setSound(e.target.checked)}
              style={{ accentColor: '#06d6a0' }} /> Sound
          </label>
          <label style={{ display: 'flex', alignItems: 'center', gap: 6, fontSize: 11, color: '#c9d1d9' }}>
            <input type="checkbox" checked={desktop} onChange={(e) => setDesktop(e.target.checked)}
              style={{ accentColor: '#06d6a0' }} /> Desktop notify
          </label>
        </div>

        <div style={{ marginBottom: 8 }}>
          <div style={{ fontSize: 10, color: '#8b949e', marginBottom: 2 }}>
            Volume — {Math.round(volume * 100)}%
          </div>
          <input type="range" min={0} max={1} step={0.05} value={volume}
            onChange={(e) => setVolume(Number(e.target.value))}
            style={{ width: '100%', accentColor: '#06d6a0' }} />
        </div>

        {desktop && permission !== 'granted' && (
          <div style={{ display: 'flex', alignItems: 'center', gap: 6, marginBottom: 8,
                        padding: '6px 8px', background: '#21262d', borderRadius: 4, fontSize: 11 }}>
            <span style={{ color: '#facc15' }}>⚠</span>
            <span style={{ color: '#c9d1d9' }}>
              Desktop notifications {permission === 'denied' ? 'are blocked' : 'need permission'}
            </span>
            <div style={{ flex: 1 }} />
            <button className="btn btn-primary" style={{ fontSize: 10, padding: '2px 8px' }}
              onClick={requestPerm}>Allow</button>
          </div>
        )}

        <div style={{ fontSize: 10, fontWeight: 700, color: '#8b949e', marginBottom: 4, letterSpacing: 0.6, textTransform: 'uppercase' }}>
          Alert on
        </div>
        <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 4 }}>
          {ALERT_EVENTS.map((ev) => (
            <label key={ev} style={{ display: 'flex', alignItems: 'center', gap: 6, fontSize: 11, color: '#c9d1d9', cursor: 'pointer' }}>
              <input type="checkbox" checked={!!perEvent[ev]} onChange={() => togglePerEvent(ev)}
                style={{ accentColor: '#06d6a0' }} />
              <span>{ALERT_EVENT_LABEL[ev]}</span>
              <button className="btn btn-ghost" style={{ marginLeft: 'auto', fontSize: 9, padding: '0 4px', color: '#484f58' }}
                onClick={(e) => { e.preventDefault(); testTone(ev) }}
                title="Preview tone">▶</button>
            </label>
          ))}
        </div>

        <AlertFilters />
      </div>
    </div>
  )
}

// Sub-panel for the filters added in the "Tier 1 — geofence / watchlist / SNR" pass.
function AlertFilters() {
  const minSnrDb = useDfAlerts((s) => s.minSnrDb)
  const setMinSnrDb = useDfAlerts((s) => s.setMinSnrDb)
  const watchlist = useDfAlerts((s) => s.watchlist)
  const setWatchlist = useDfAlerts((s) => s.setWatchlist)
  const geofenceMode = useDfAlerts((s) => s.geofenceMode)
  const setGeofenceMode = useDfAlerts((s) => s.setGeofenceMode)
  const geofences = useDfAlerts((s) => s.geofences)
  const [draftFreq, setDraftFreq] = useState('')
  const [draftTol, setDraftTol] = useState('5000')

  const addEntry = () => {
    const f = parseFloat(draftFreq)
    const t = parseFloat(draftTol) || 5000
    if (!Number.isFinite(f) || f <= 0) return
    setWatchlist([...watchlist, { frequency_hz: f, tolerance_hz: t, label: '' }])
    setDraftFreq(''); setDraftTol('5000')
  }
  const remove = (i) => setWatchlist(watchlist.filter((_, j) => j !== i))

  return (
    <div style={{ marginTop: 10, borderTop: '1px solid #30363d', paddingTop: 8 }}>
      <div style={{ fontSize: 10, fontWeight: 700, color: '#8b949e', marginBottom: 4, letterSpacing: 0.6, textTransform: 'uppercase' }}>
        Filters
      </div>
      <div style={{ display: 'flex', gap: 8, alignItems: 'center', marginBottom: 6 }}>
        <span style={{ fontSize: 11, color: '#c9d1d9' }}>Geofence:</span>
        {['off', 'inside', 'outside'].map((m) => (
          <button key={m} onClick={() => setGeofenceMode(m)}
            className={`btn ${geofenceMode === m ? 'btn-primary' : 'btn-ghost'}`}
            style={{ fontSize: 10, padding: '2px 8px' }}>{m}</button>
        ))}
        <span style={{ fontSize: 10, color: '#484f58' }}>{geofences?.features?.length || 0} fence{(geofences?.features?.length || 0) === 1 ? '' : 's'}</span>
      </div>
      <div style={{ display: 'flex', gap: 6, alignItems: 'center', marginBottom: 6 }}>
        <span style={{ fontSize: 11, color: '#c9d1d9' }}>Min SNR (dB):</span>
        <input type="number" value={minSnrDb} onChange={(e) => setMinSnrDb(Number(e.target.value))}
          style={{ width: 70, background: '#0d1117', border: '1px solid #30363d', borderRadius: 4, color: '#e6edf3', padding: '2px 4px', fontSize: 11 }} />
        <span style={{ fontSize: 10, color: '#484f58' }}>(-200 = off)</span>
      </div>
      <div style={{ fontSize: 11, color: '#c9d1d9', marginBottom: 4 }}>Watchlist (Hz)</div>
      <div style={{ display: 'flex', gap: 4, alignItems: 'center', marginBottom: 6 }}>
        <input type="number" placeholder="freq Hz" value={draftFreq} onChange={(e) => setDraftFreq(e.target.value)}
          style={{ width: 110, background: '#0d1117', border: '1px solid #30363d', borderRadius: 4, color: '#e6edf3', padding: '2px 4px', fontSize: 11 }} />
        <input type="number" placeholder="± Hz" value={draftTol} onChange={(e) => setDraftTol(e.target.value)}
          style={{ width: 70, background: '#0d1117', border: '1px solid #30363d', borderRadius: 4, color: '#e6edf3', padding: '2px 4px', fontSize: 11 }} />
        <button className="btn btn-primary" style={{ fontSize: 10, padding: '2px 8px' }} onClick={addEntry}>+ Add</button>
      </div>
      {watchlist.length > 0 && (
        <div style={{ maxHeight: 96, overflowY: 'auto', background: '#0d1117', border: '1px solid #21262d', borderRadius: 4, padding: 4 }}>
          {watchlist.map((w, i) => (
            <div key={i} style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', fontSize: 10, color: '#c9d1d9', padding: '2px 4px' }}>
              <span>{(w.frequency_hz / 1e6).toFixed(3)} MHz ± {w.tolerance_hz} Hz</span>
              <button className="btn btn-ghost" style={{ padding: '0 4px', color: '#fca5a5' }} onClick={() => remove(i)}>×</button>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}
