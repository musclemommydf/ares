// SPDX-License-Identifier: MIT OR Apache-2.0
// Copyright (c) 2026 Ares

/**
 * Coordinate converter — type any location and see it rendered in every major
 * coordinate system. Auto-detects input format (DD, DMS, DDM, MGRS, UTM).
 */
import { useMemo, useState } from 'react'
import {
  formatCoordinate,
  parseCoordinateInput,
  autoParseCoordinate,
  toDDM,
} from '../../utils/units'

function copyToClipboard(text) {
  if (navigator.clipboard?.writeText) {
    navigator.clipboard.writeText(text).catch(() => {})
  }
}

function parseAny(input) {
  if (!input?.trim()) return null
  // Try the existing auto-parser first (covers DD, DMS, MGRS, UTM)
  const auto = autoParseCoordinate(input)
  if (auto) return auto
  // DDM: "51°30.435'N 0°7.65'W" or "51 30.435 N, 0 7.65 W"
  const norm = input.replace(/[°'"]/g, ' ').replace(/\s+/g, ' ').trim()
  const ddm = /^(\d+)\s+(\d+(?:\.\d+)?)\s*([NS])\s*[,\s]\s*(\d+)\s+(\d+(?:\.\d+)?)\s*([EW])$/i.exec(norm)
  if (ddm) {
    const lat = (parseInt(ddm[1], 10) + parseFloat(ddm[2]) / 60) * (ddm[3].toUpperCase() === 'S' ? -1 : 1)
    const lon = (parseInt(ddm[4], 10) + parseFloat(ddm[5]) / 60) * (ddm[6].toUpperCase() === 'W' ? -1 : 1)
    if (lat >= -90 && lat <= 90 && lon >= -180 && lon <= 180) return { lat, lon }
  }
  return null
}

export default function CoordinateConverter({ open, onClose, onUseLocation, leafletMap }) {
  const [input, setInput] = useState('')
  const parsed = useMemo(() => parseAny(input), [input])

  const rows = parsed ? [
    { label: 'Decimal degrees',  value: `${parsed.lat.toFixed(6)}, ${parsed.lon.toFixed(6)}` },
    { label: 'Lat/Lon DMS',      value: formatCoordinate(parsed.lat, parsed.lon, 'latlon_dms') },
    { label: 'Lat/Lon DDM',      value: toDDM(parsed.lat, parsed.lon) },
    { label: 'MGRS',             value: formatCoordinate(parsed.lat, parsed.lon, 'mgrs') },
    { label: 'UTM',              value: formatCoordinate(parsed.lat, parsed.lon, 'utm') },
  ] : []

  if (!open) return null

  return (
    <div
      style={{
        position: 'absolute', top: '110%', right: 0, marginTop: 2,
        background: '#161b22', border: '1px solid #30363d', borderRadius: 8,
        padding: '12px 14px', minWidth: 360, maxWidth: 440, zIndex: 9999,
        boxShadow: '0 6px 20px rgba(0,0,0,0.7)',
      }}
      onClick={e => e.stopPropagation()}
    >
      <div style={{
        fontSize: 11, fontWeight: 700, color: '#8b949e', marginBottom: 8,
        textTransform: 'uppercase', letterSpacing: 0.8,
        display: 'flex', alignItems: 'center', justifyContent: 'space-between',
      }}>
        <span>Coordinate Converter</span>
        <button className="btn btn-ghost"
          style={{ padding: '0 6px', fontSize: 14, color: '#8b949e' }}
          onClick={onClose}>×</button>
      </div>
      <input
        autoFocus
        value={input}
        onChange={e => setInput(e.target.value)}
        placeholder="DD, DMS, DDM, MGRS, UTM…"
        style={{
          width: '100%', background: '#0d1117', border: '1px solid #30363d',
          borderRadius: 4, color: '#e6edf3', fontSize: 12, padding: '6px 8px',
          outline: 'none', boxSizing: 'border-box', marginBottom: 8,
        }}
      />
      {!parsed && input.trim() && (
        <div style={{ fontSize: 10, color: '#fca5a5', marginBottom: 6 }}>
          Could not interpret this coordinate
        </div>
      )}
      {parsed && (
        <>
          <div style={{
            display: 'grid', gridTemplateColumns: '90px 1fr auto',
            gap: '4px 8px', fontSize: 11, alignItems: 'center',
          }}>
            {rows.map(r => (
              <ConverterRow key={r.label} label={r.label} value={r.value} />
            ))}
          </div>
          <div style={{ display: 'flex', gap: 6, marginTop: 10 }}>
            <button className="btn btn-primary"
              style={{ flex: 1, fontSize: 11, padding: '4px 8px' }}
              onClick={() => {
                if (leafletMap) leafletMap.setView([parsed.lat, parsed.lon],
                  Math.max(leafletMap.getZoom(), 12))
              }}>
              Centre map
            </button>
            {onUseLocation && (
              <button className="btn btn-ghost"
                style={{ flex: 1, fontSize: 11, padding: '4px 8px' }}
                onClick={() => onUseLocation(parsed)}>
                Save location
              </button>
            )}
          </div>
        </>
      )}
      <div style={{ fontSize: 9, color: '#484f58', padding: '8px 0 0', lineHeight: 1.4 }}>
        Examples: <code>51.5074, -0.1278</code> · <code>51 30 26 N 0 7 39 W</code> · <code>30U 699721E 5710158N</code> · <code>30UXC1234567890</code>
      </div>
    </div>
  )
}

function ConverterRow({ label, value }) {
  const [copied, setCopied] = useState(false)
  return (
    <>
      <span style={{ color: '#8b949e' }}>{label}</span>
      <span style={{
        color: '#e6edf3', fontFamily: 'ui-monospace, monospace',
        background: '#0d1117', padding: '3px 6px', borderRadius: 3,
        overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap',
      }} title={value}>{value}</span>
      <button className="btn btn-ghost"
        style={{ padding: '2px 6px', fontSize: 10, color: copied ? '#06d6a0' : '#8b949e', flexShrink: 0 }}
        onClick={() => { copyToClipboard(value); setCopied(true); setTimeout(() => setCopied(false), 1200) }}
        title="Copy">
        {copied ? '✓' : '⧉'}
      </button>
    </>
  )
}
