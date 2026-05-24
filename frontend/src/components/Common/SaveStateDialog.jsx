// SPDX-License-Identifier: MIT OR Apache-2.0
// Copyright (c) 2026 Ares

/**
 * Modal that lets the user pick which slices of app state to include in a
 * Save State export. Default is "everything checked"; the dialog returns a
 * selection map (k → boolean) when the user clicks Save. Cancel returns null.
 *
 * The slice list mirrors handleSaveState() in App.jsx — adding a new state
 * slice means adding it both there and to SECTIONS below.
 */
import { useEffect, useState } from 'react'

const SECTIONS = [
  { key: 'emitters',       label: 'Propagation emitters',  hint: 'Primary TX + extras + RX + propagation + atmosphere' },
  { key: 'lobs',           label: 'Lines of bearing & geo', hint: 'LoBs, CAP groups, LoB algorithm settings' },
  { key: 'layers',         label: 'Imported layers & drawings', hint: 'KMZ / KML / GeoJSON / GPX / imagery / tile sources / DTED + drawings' },
  { key: 'coverage',       label: 'Coverage results',      hint: 'Propagation heatmap + buildings + warnings' },
  { key: 'analyses',       label: 'Analysis results',      hint: 'Best-site, route, multipoint, MANET, best-server, BSA, P2P, terrain profile, radar' },
  { key: 'sdr',            label: 'SDR live overlay',      hint: 'SDR-derived features + auto-coverage' },
  { key: 'savedLocations', label: 'Saved locations',       hint: 'Pinned named locations' },
  { key: 'mapView',        label: 'Map view',              hint: 'Center + zoom + layer-visibility selection' },
  { key: 'ui',             label: 'UI preferences',        hint: 'Active tab, units, coord system, brightness, panel layout' },
]

export default function SaveStateDialog({ open, onSave, onCancel }) {
  // Default: every section checked. Reset when the dialog re-opens so a prior
  // Cancel doesn't leak partial selections into the next session.
  const [sel, setSel] = useState(() => Object.fromEntries(SECTIONS.map(s => [s.key, true])))
  useEffect(() => {
    if (open) setSel(Object.fromEntries(SECTIONS.map(s => [s.key, true])))
  }, [open])
  if (!open) return null

  const toggle = (key) => setSel(prev => ({ ...prev, [key]: !prev[key] }))
  const allOn = SECTIONS.every(s => sel[s.key])
  const noneOn = SECTIONS.every(s => !sel[s.key])
  const setAll = (v) => setSel(Object.fromEntries(SECTIONS.map(s => [s.key, v])))

  return (
    <div
      onClick={onCancel}
      onKeyDown={(e) => { if (e.key === 'Escape') onCancel() }}
      style={{
        position: 'fixed', inset: 0, background: 'rgba(0,0,0,0.55)', zIndex: 1000,
        display: 'flex', alignItems: 'center', justifyContent: 'center',
      }}>
      <div
        onClick={(e) => e.stopPropagation()}
        style={{
          background: '#0d1117', border: '1px solid #30363d', borderRadius: 6,
          padding: 18, width: 'min(440px, 92vw)', maxHeight: '90vh', overflowY: 'auto',
          color: '#e6edf3',
        }}>
        <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 14 }}>
          <div style={{ fontSize: 14, fontWeight: 700 }}>Save state — choose what to include</div>
          <div style={{ display: 'flex', gap: 6 }}>
            <button type="button" onClick={() => setAll(true)} disabled={allOn} style={chipBtn(allOn)}>All</button>
            <button type="button" onClick={() => setAll(false)} disabled={noneOn} style={chipBtn(noneOn)}>None</button>
          </div>
        </div>

        <div style={{ display: 'flex', flexDirection: 'column', gap: 4, marginBottom: 16 }}>
          {SECTIONS.map(s => (
            <label
              key={s.key}
              style={{
                display: 'flex', alignItems: 'flex-start', gap: 10, padding: '8px 10px',
                background: sel[s.key] ? '#161b22' : 'transparent',
                border: '1px solid', borderColor: sel[s.key] ? '#30363d' : 'transparent',
                borderRadius: 4, cursor: 'pointer',
                transition: 'background 120ms, border-color 120ms',
              }}>
              <input
                type="checkbox" checked={!!sel[s.key]} onChange={() => toggle(s.key)}
                style={{ marginTop: 2, accentColor: '#00b4d8', flexShrink: 0 }}
              />
              <div style={{ minWidth: 0 }}>
                <div style={{ fontSize: 12, fontWeight: 600, color: sel[s.key] ? '#e6edf3' : '#8b949e' }}>{s.label}</div>
                <div style={{ fontSize: 10, color: '#8b949e', marginTop: 2 }}>{s.hint}</div>
              </div>
            </label>
          ))}
        </div>

        <div style={{ display: 'flex', gap: 8, justifyContent: 'flex-end' }}>
          <button type="button" onClick={onCancel} style={{
            padding: '6px 14px', fontSize: 12, background: 'transparent', color: '#8b949e',
            border: '1px solid #30363d', borderRadius: 4, cursor: 'pointer',
          }}>Cancel</button>
          <button
            type="button"
            onClick={() => onSave(sel)}
            disabled={noneOn}
            style={{
              padding: '6px 18px', fontSize: 12, fontWeight: 600,
              background: noneOn ? '#21262d' : '#0d2438',
              color: noneOn ? '#484f58' : '#7dd3fc',
              border: `1px solid ${noneOn ? '#30363d' : '#1e3a5f'}`,
              borderRadius: 4, cursor: noneOn ? 'not-allowed' : 'pointer',
            }}>Save</button>
        </div>
      </div>
    </div>
  )
}

function chipBtn(disabled) {
  return {
    padding: '3px 9px', fontSize: 10, background: 'transparent',
    color: disabled ? '#484f58' : '#8b949e',
    border: '1px solid #30363d', borderRadius: 3,
    cursor: disabled ? 'default' : 'pointer',
  }
}
