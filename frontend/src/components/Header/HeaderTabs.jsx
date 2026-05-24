// SPDX-License-Identifier: MIT OR Apache-2.0
// Copyright (c) 2026 Ares

import { Radio, Crosshair, Route, MapPin, Network, Hexagon, Scan } from 'lucide-react'
import AppIcon from '../Common/AppIcon'

const DIV = { width: 1, height: 20, background: '#30363d', margin: '0 4px', flexShrink: 0 }
const PRIMARY_TABS = [
  { id: 'coverage', label: 'Coverage' }, { id: 'p2p', label: 'P2P' },
  { id: 'best_site', label: 'Best Site' }, { id: 'radar', label: 'Radar' },
]
const TOOL_TABS = [
  { id: 'route', label: 'Route', Icon: Route }, { id: 'multipoint', label: 'Multipoint', Icon: MapPin },
  { id: 'manet', label: 'MANET', Icon: Network }, { id: 'best_server', label: 'Best Server', Icon: Radio },
  { id: 'best_site_polygon', label: 'BSA Polygon', Icon: Hexagon }, { id: 'ray_trace', label: '3D Ray', Icon: Scan },
]

/**
 * The left/centre of the header: logo · mode tabs (Propagation / Geolocation) · then,
 * in propagation mode, the analysis tabs (Coverage / P2P / Best Site / Radar) + the
 * icon-only tool tabs (Route / Multipoint / MANET / Best Server / BSA Polygon / 3D Ray);
 * in geolocation mode, the LoB count. `onSelectTab` should also set the radar model when
 * the radar tab is picked (App wires that).
 */
export default function HeaderTabs({ mainMode, activeTab, lobCount, lobGroupCount, onSelectMode, onSelectTab }) {
  return (
    <>
      <div className="app-logo" style={{ flexShrink: 0, display: 'inline-flex', alignItems: 'center', gap: 6 }}>
        <AppIcon size={24} /> Ares
        <span title="Alpha — APIs and UX may change between releases"
              style={{ fontSize: 9, fontWeight: 700, letterSpacing: 0.6, textTransform: 'uppercase',
                       padding: '1px 5px', borderRadius: 3, color: '#f0883e',
                       background: 'rgba(240,136,62,0.12)', border: '1px solid rgba(240,136,62,0.45)' }}>
          alpha v5.2
        </span>
      </div>
      <div style={DIV} />

      {/* Mode tabs */}
      <div style={{ display: 'flex', gap: 2, alignItems: 'center', flexShrink: 0 }}>
        <button
          className={`tab ${mainMode === 'propagation' ? 'active' : ''}`}
          style={{ padding: '4px 10px', fontSize: 12, fontWeight: 600, whiteSpace: 'nowrap' }}
          onClick={() => onSelectMode('propagation')}
        >
          <Radio size={11} style={{ marginRight: 4, display: 'inline', verticalAlign: 'text-bottom' }} />
          Propagation
        </button>
        <button
          className={`tab ${mainMode === 'geolocation' ? 'active' : ''}`}
          style={{ padding: '4px 10px', fontSize: 12, fontWeight: 600, whiteSpace: 'nowrap',
                   color: mainMode === 'geolocation' ? '#a78bfa' : undefined }}
          onClick={() => onSelectMode('geolocation')}
        >
          <Crosshair size={11} style={{ marginRight: 4, display: 'inline', verticalAlign: 'text-bottom' }} />
          Geolocation
        </button>
      </div>

      <div style={DIV} />

      {/* Primary analysis tabs + tool tabs — propagation only */}
      {mainMode === 'propagation' && (
        <div style={{ display: 'flex', gap: 2, alignItems: 'center', flexShrink: 0, minWidth: 0 }}>
          <div className="tabs" style={{ borderBottom: 'none', padding: 0, flexWrap: 'nowrap' }}>
            {PRIMARY_TABS.map(t => (
              <button key={t.id} className={`tab ${activeTab === t.id ? 'active' : ''}`}
                      style={{ whiteSpace: 'nowrap', fontSize: 11 }} onClick={() => onSelectTab(t.id)}>{t.label}</button>
            ))}
          </div>
          <div style={{ ...DIV, margin: '0 2px' }} />
          <div className="tabs" style={{ borderBottom: 'none', padding: 0, flexWrap: 'nowrap' }}>
            {TOOL_TABS.map(t => (
              <button key={t.id} className={`tab ${activeTab === t.id ? 'active' : ''}`}
                      title={t.label} style={{ padding: '4px 7px' }} onClick={() => onSelectTab(t.id)}>
                <t.Icon size={13} />
              </button>
            ))}
          </div>
        </div>
      )}

      {/* Geolocation mode label */}
      {mainMode === 'geolocation' && (
        <span style={{ fontSize: 11, color: '#a78bfa', fontWeight: 600, flexShrink: 0 }}>
          {lobCount} LoB{lobCount !== 1 ? 's' : ''}{lobCount > 0 && ` · ${lobGroupCount} group${lobGroupCount !== 1 ? 's' : ''}`}
        </span>
      )}
    </>
  )
}
