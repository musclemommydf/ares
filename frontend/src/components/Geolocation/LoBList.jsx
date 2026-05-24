// SPDX-License-Identifier: MIT OR Apache-2.0
// Copyright (c) 2026 Ares

import { Pencil, Trash2 } from 'lucide-react'
import { ENVIRONMENT_PRESETS } from './LoBUtils'
import { DEVICE_TYPES } from './GeoLocationPanel'

export default function LoBList({
  lobs,
  onEditLoB,
  onRemoveLoB,
  editingLobId = null,
  pendingTerrainIds = new Set(),
  emptyHint = 'No bearings recorded yet',
}) {
  if (!lobs || lobs.length === 0) {
    return <div style={{ fontSize: 11, color: '#484f58' }}>{emptyHint}</div>
  }
  return (
    <>
      {lobs.map(lob => (
        <div
          key={lob.id}
          style={{
            background: editingLobId === lob.id ? '#1a1040' : '#0d1117',
            border: editingLobId === lob.id ? '1px solid #a78bfa50' : `1px solid ${lob.color}30`,
            borderLeft: `3px solid ${editingLobId === lob.id ? '#a78bfa' : lob.color}`,
            borderRadius: 4, padding: '6px 8px', marginBottom: 4,
          }}
        >
          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start' }}>
            <div style={{ flex: 1, minWidth: 0 }}>
              <div style={{ fontSize: 11, fontWeight: 600, color: lob.color, marginBottom: 2 }}>
                {lob.label} · {(lob.frequency_hz / 1e6).toFixed(3)} MHz
              </div>
              <div style={{ fontSize: 10, color: '#8b949e' }}>
                Az {lob.azimuth_deg.toFixed(1)}° · {lob.rssi_dbm} dBm · {lob.confidence_pct}% conf
              </div>
              <div style={{ fontSize: 10, color: '#444d56' }}>
                {lob.lat.toFixed(5)}, {lob.lon.toFixed(5)} ·{' '}
                {lob.tx_power_dbm !== null
                  ? (lob.estimated_distance_m >= 1000
                      ? `~${(lob.estimated_distance_m / 1000).toFixed(1)} km`
                      : `~${Math.round(lob.estimated_distance_m)} m`)
                  : 'dist. unknown'}
                {lob.tx_power_dbm !== null && lob.distance_method === 'terrain' ? ' (terrain)' : lob.tx_power_dbm !== null ? ' (FSPL)' : ''}
                {' · '}{ENVIRONMENT_PRESETS.find(e => e.id === (lob.environment || 'suburban'))?.label ?? 'Suburban'}
                {lob.clutter_height_m > 0 ? ` / ${lob.clutter_height_m}m clutter` : ''}
                {lob.observer_height_m > 0 ? ` / obs ${lob.observer_height_m}m AGL` : ''}
              </div>
              {pendingTerrainIds.has(lob.id) && (
                <div style={{ fontSize: 9, color: '#f59e0b', marginTop: 1 }}>⟳ terrain calc…</div>
              )}
              {lob.device_id && (
                <div style={{ fontSize: 10, color: '#a78bfa' }}>
                  {DEVICE_TYPES.find(t => t.value === lob.device_type)?.label || 'ID'}: {lob.device_id}
                </div>
              )}
              {lob.time && (
                <div style={{ fontSize: 9, color: '#444d56' }}>{lob.time}</div>
              )}
            </div>
            <div style={{ display: 'flex', gap: 2, flexShrink: 0 }}>
              {onEditLoB && (
                <button
                  className="btn btn-ghost"
                  style={{ padding: '2px 4px', color: editingLobId === lob.id ? '#a78bfa' : '#8b949e' }}
                  onClick={() => onEditLoB(lob)}
                  title="Edit LoB"
                >
                  <Pencil size={11} />
                </button>
              )}
              <button
                className="btn btn-ghost"
                style={{ padding: '2px 4px', color: '#ef4444' }}
                onClick={() => onRemoveLoB(lob.id)}
                title="Remove LoB"
              >
                <Trash2 size={11} />
              </button>
            </div>
          </div>
        </div>
      ))}
    </>
  )
}
