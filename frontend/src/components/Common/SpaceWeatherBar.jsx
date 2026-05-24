// SPDX-License-Identifier: MIT OR Apache-2.0
// Copyright (c) 2026 Ares

/**
 * Space weather status bar — shows at top of map area.
 */
export default function SpaceWeatherBar({ data }) {
  if (!data) return null

  const kp = data.solar_flux_f107
  const kpVal = data.kp_index
  const storm = data.storm_class
  const blackout = data.radio_blackout
  const hfStatus = data.hf_propagation || ''

  const kpColor = kpVal >= 5 ? 'red' : kpVal >= 3 ? 'yellow' : 'green'
  const blackoutColor = blackout !== 'None' ? 'red' : 'green'

  return (
    <div className="sw-bar">
      <div className="sw-item">
        <div className={`sw-dot ${kpColor}`} />
        <span>Kp: <strong style={{ color: 'var(--text-primary)' }}>{kpVal?.toFixed(1)}</strong></span>
      </div>

      <div className="sw-item">
        <span>F10.7: <strong style={{ color: 'var(--text-primary)' }}>{kp?.toFixed(0)}</strong></span>
      </div>

      {storm !== 'None' && (
        <div className="sw-item">
          <div className="sw-dot red" />
          <span style={{ color: 'var(--accent-amber)' }}>Storm {storm}</span>
        </div>
      )}

      {blackout !== 'None' && (
        <div className="sw-item">
          <div className="sw-dot red" />
          <span style={{ color: 'var(--accent-red)' }}>Blackout {blackout}</span>
        </div>
      )}

      <div className="sw-item" style={{ marginLeft: 4 }}>
        <span style={{ color: 'var(--text-muted)' }}>{hfStatus.slice(0, 60)}</span>
      </div>

      {data.vhf_sporadic_e_likely && (
        <div className="sw-item" style={{ marginLeft: 'auto' }}>
          <div className="sw-dot green" />
          <span style={{ color: 'var(--accent-green)', fontSize: 10 }}>Sporadic-E possible</span>
        </div>
      )}
    </div>
  )
}
