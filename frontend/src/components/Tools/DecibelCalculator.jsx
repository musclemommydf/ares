// SPDX-License-Identifier: MIT OR Apache-2.0
// Copyright (c) 2026 Ares

/**
 * Decibel / Power Calculator
 * Bidirectional conversion between dBm, dBW, dBµW, W, mW, µW, kW, MW
 * with a logarithmic reference graph.
 */
import { useState, useId } from 'react'
import { X } from 'lucide-react'

// ── Unit definitions ──────────────────────────────────────────────────────────
const UNITS = [
  { key: 'dBm',  label: 'dBm',  desc: 'Decibel-milliwatts' },
  { key: 'dBW',  label: 'dBW',  desc: 'Decibel-watts' },
  { key: 'dBuW', label: 'dBµW', desc: 'Decibel-microwatts' },
  { key: 'W',    label: 'W',    desc: 'Watts' },
  { key: 'mW',   label: 'mW',   desc: 'Milliwatts' },
  { key: 'uW',   label: 'µW',   desc: 'Microwatts' },
  { key: 'kW',   label: 'kW',   desc: 'Kilowatts' },
  { key: 'MW',   label: 'MW',   desc: 'Megawatts' },
]

// Convert any unit value to dBm (canonical)
function toDbm(val, unit) {
  if (!isFinite(val)) return NaN
  switch (unit) {
    case 'dBm':  return val
    case 'dBW':  return val + 30
    case 'dBuW': return val - 30
    case 'W':    return val > 0 ? 10 * Math.log10(val) + 30 : NaN
    case 'mW':   return val > 0 ? 10 * Math.log10(val)      : NaN
    case 'uW':   return val > 0 ? 10 * Math.log10(val) - 30 : NaN
    case 'kW':   return val > 0 ? 10 * Math.log10(val) + 60 : NaN
    case 'MW':   return val > 0 ? 10 * Math.log10(val) + 90 : NaN
    default:     return NaN
  }
}

// Convert dBm to any unit
function fromDbm(dbm, unit) {
  if (!isFinite(dbm)) return NaN
  switch (unit) {
    case 'dBm':  return dbm
    case 'dBW':  return dbm - 30
    case 'dBuW': return dbm + 30
    case 'W':    return Math.pow(10, (dbm - 30) / 10)
    case 'mW':   return Math.pow(10, dbm / 10)
    case 'uW':   return Math.pow(10, (dbm + 30) / 10)
    case 'kW':   return Math.pow(10, (dbm - 60) / 10)
    case 'MW':   return Math.pow(10, (dbm - 90) / 10)
    default:     return NaN
  }
}

// Format a converted value for display
function fmt(val, unit) {
  if (!isFinite(val)) return '—'
  if (['dBm', 'dBW', 'dBuW'].includes(unit)) {
    return val.toFixed(3)
  }
  // Linear power — pick sensible precision
  const abs = Math.abs(val)
  if (abs === 0) return '0'
  if (abs >= 1e6)  return val.toExponential(3)
  if (abs >= 1000) return val.toPrecision(5)
  if (abs >= 1)    return val.toPrecision(5)
  if (abs >= 1e-3) return val.toPrecision(4)
  if (abs >= 1e-6) return val.toPrecision(4)
  return val.toExponential(3)
}

// ── Log-scale reference graph ─────────────────────────────────────────────────
// Axis runs linearly in dBm (which is already a log scale in watts).

const G_MIN = -170
const G_MAX  =  90
const G_RANGE = G_MAX - G_MIN   // 260 dBm span

const BAR_X = 14     // SVG units
const BAR_W = 572
const BAR_Y = 72
const BAR_H = 18

const REFS = [
  { dbm: -174, top: true,  label: 'Noise density' },
  { dbm: -120, top: false, label: 'Receiver MDS' },
  { dbm: -100, top: true,  label: 'RX sensitivity' },
  { dbm:  -60, top: false, label: 'Strong RX' },
  { dbm:    0, top: true,  label: '1 mW' },
  { dbm:   20, top: false, label: '100 mW' },
  { dbm:   30, top: true,  label: '1 W' },
  { dbm:   40, top: false, label: '10 W' },
  { dbm:   50, top: true,  label: '100 W' },
  { dbm:   60, top: false, label: '1 kW' },
  { dbm:   80, top: true,  label: '100 kW' },
]

const AXIS_TICKS = [-160, -140, -120, -100, -80, -60, -40, -20, 0, 20, 40, 60, 80]

function dBmToSvgX(dbm) {
  return BAR_X + ((dbm - G_MIN) / G_RANGE) * BAR_W
}

function LogGraph({ dbm }) {
  const gradId = useId()
  const cursorX = isFinite(dbm) ? Math.max(BAR_X, Math.min(BAR_X + BAR_W, dBmToSvgX(dbm))) : null
  const cursorInRange = isFinite(dbm) && dbm >= G_MIN && dbm <= G_MAX

  return (
    <svg
      viewBox={`0 0 600 130`}
      width="100%"
      style={{ display: 'block', overflow: 'visible' }}
      aria-label="Logarithmic power scale"
    >
      <defs>
        <linearGradient id={gradId} x1="0%" y1="0%" x2="100%" y2="0%">
          <stop offset="0%"   stopColor="#1e1b4b" />
          <stop offset="12%"  stopColor="#1d4ed8" />
          <stop offset="22%"  stopColor="#0284c7" />
          <stop offset="35%"  stopColor="#06b6d4" />
          <stop offset="48%"  stopColor="#16a34a" />
          <stop offset="58%"  stopColor="#65a30d" />
          <stop offset="68%"  stopColor="#eab308" />
          <stop offset="78%"  stopColor="#f97316" />
          <stop offset="88%"  stopColor="#dc2626" />
          <stop offset="100%" stopColor="#7f1d1d" />
        </linearGradient>
      </defs>

      {/* Gradient bar */}
      <rect x={BAR_X} y={BAR_Y} width={BAR_W} height={BAR_H}
            fill={`url(#${gradId})`} rx="3" />

      {/* Reference markers */}
      {REFS.map((r) => {
        const x = dBmToSvgX(r.dbm)
        if (x < BAR_X || x > BAR_X + BAR_W) return null
        const tickY1 = r.top ? BAR_Y - 2  : BAR_Y + BAR_H + 2
        const tickY2 = r.top ? BAR_Y - 14 : BAR_Y + BAR_H + 14
        const labelY = r.top ? BAR_Y - 17 : BAR_Y + BAR_H + 24
        const anchor = x < BAR_X + 60 ? 'start' : x > BAR_X + BAR_W - 60 ? 'end' : 'middle'
        return (
          <g key={r.dbm}>
            <line x1={x} y1={tickY1} x2={x} y2={tickY2}
                  stroke="#6b7280" strokeWidth="1" strokeDasharray="2 2" />
            <text x={x} y={labelY} textAnchor={anchor}
                  fill="#6b7280" fontSize="8.5" fontFamily="sans-serif">
              {r.label}
            </text>
          </g>
        )
      })}

      {/* Axis ticks + labels */}
      {AXIS_TICKS.map((t) => {
        const x = dBmToSvgX(t)
        return (
          <g key={t}>
            <line x1={x} y1={BAR_Y + BAR_H} x2={x} y2={BAR_Y + BAR_H + 4}
                  stroke="#374151" strokeWidth="1" />
            <text x={x} y={BAR_Y + BAR_H + 14} textAnchor="middle"
                  fill="#4b5563" fontSize="9" fontFamily="sans-serif">
              {t}
            </text>
          </g>
        )
      })}

      {/* Axis label */}
      <text x={BAR_X + BAR_W / 2} y={BAR_Y + BAR_H + 26}
            textAnchor="middle" fill="#374151" fontSize="9" fontFamily="sans-serif">
        dBm
      </text>

      {/* Bar border */}
      <rect x={BAR_X} y={BAR_Y} width={BAR_W} height={BAR_H}
            fill="none" stroke="#374151" strokeWidth="0.75" rx="3" />

      {/* Current value cursor */}
      {cursorInRange && (
        <g>
          <line x1={cursorX} y1={BAR_Y - 4} x2={cursorX} y2={BAR_Y + BAR_H + 4}
                stroke="#fff" strokeWidth="2" />
          {/* Arrowhead above */}
          <polygon
            points={`${cursorX},${BAR_Y - 4} ${cursorX - 4},${BAR_Y - 10} ${cursorX + 4},${BAR_Y - 10}`}
            fill="#fff"
          />
          <text
            x={Math.max(BAR_X + 4, Math.min(BAR_X + BAR_W - 4, cursorX))}
            y={BAR_Y - 13}
            textAnchor={cursorX < BAR_X + 60 ? 'start' : cursorX > BAR_X + BAR_W - 60 ? 'end' : 'middle'}
            fill="#fff" fontSize="9.5" fontWeight="bold" fontFamily="sans-serif"
          >
            {dbm.toFixed(1)} dBm
          </text>
        </g>
      )}

      {/* Out-of-range indicator */}
      {isFinite(dbm) && !cursorInRange && (
        <text x={BAR_X + BAR_W / 2} y={BAR_Y + BAR_H / 2 + 4}
              textAnchor="middle" fill="rgba(255,255,255,0.5)" fontSize="10" fontFamily="sans-serif">
          {dbm < G_MIN ? `← ${dbm.toFixed(1)} dBm (below graph range)` : `${dbm.toFixed(1)} dBm (above graph range) →`}
        </text>
      )}
    </svg>
  )
}

// ── Main component ─────────────────────────────────────────────────────────────
export default function DecibelCalculator({ onClose, embedded = false }) {
  const [rawValue, setRawValue] = useState('0')
  const [activeUnit, setActiveUnit] = useState('dBm')

  const parsed = parseFloat(rawValue)
  const dbm = isFinite(parsed) ? toDbm(parsed, activeUnit) : NaN

  const handleUnitSwitch = (unit) => {
    // Convert the current dBm to the new unit and pre-fill
    if (isFinite(dbm)) {
      const converted = fromDbm(dbm, unit)
      if (isFinite(converted)) {
        setRawValue(fmt(converted, unit))
      } else {
        setRawValue('')
      }
    } else {
      setRawValue('')
    }
    setActiveUnit(unit)
  }

  return (
    <div
      style={embedded
        ? { width: '100%', height: '100%', display: 'flex', flexDirection: 'column' }
        : { position: 'fixed', inset: 0, zIndex: 9999, background: 'rgba(0,0,0,0.65)',
            display: 'flex', alignItems: 'center', justifyContent: 'center' }}
      onClick={embedded ? undefined : onClose}
    >
      <div
        style={embedded
          ? { background: 'transparent', flex: 1, minHeight: 0, display: 'flex', flexDirection: 'column' }
          : { background: '#161b22', border: '1px solid #30363d', borderRadius: 10,
              width: 660, maxWidth: '95vw', maxHeight: '90vh',
              display: 'flex', flexDirection: 'column', boxShadow: '0 16px 48px rgba(0,0,0,0.7)' }}
        onClick={embedded ? undefined : (e => e.stopPropagation())}
      >
        {/* Header (modal only — the bottom-panel tab provides its own label) */}
        {!embedded && (
        <div style={{
          display: 'flex', alignItems: 'center', justifyContent: 'space-between',
          padding: '14px 18px 10px', borderBottom: '1px solid #21262d',
          flexShrink: 0,
        }}>
          <div>
            <div style={{ fontWeight: 700, fontSize: 14, color: '#e6edf3' }}>
              Decibel / Power Calculator
            </div>
            <div style={{ fontSize: 11, color: '#484f58', marginTop: 2 }}>
              Enter a value in any unit — all equivalents update instantly
            </div>
          </div>
          <button className="btn btn-ghost" onClick={onClose}><X size={14} /></button>
        </div>
        )}

        <div style={{ overflow: 'auto', padding: '16px 18px', flex: 1 }}>

          {/* Input row */}
          <div style={{ display: 'flex', gap: 8, marginBottom: 18, alignItems: 'center' }}>
            <input
              type="number"
              value={rawValue}
              onChange={e => setRawValue(e.target.value)}
              step="any"
              autoFocus
              style={{
                flex: 1, background: '#0d1117', border: '1px solid #30363d',
                borderRadius: 6, color: '#e6edf3', fontSize: 15,
                padding: '8px 12px', fontFamily: 'monospace',
              }}
              placeholder="Enter value…"
            />
            <select
              value={activeUnit}
              onChange={e => handleUnitSwitch(e.target.value)}
              style={{
                background: '#0d1117', border: '1px solid #30363d', borderRadius: 6,
                color: '#00b4d8', fontSize: 13, padding: '8px 10px',
                cursor: 'pointer', fontWeight: 700,
              }}
            >
              {UNITS.map(u => (
                <option key={u.key} value={u.key}>{u.label}</option>
              ))}
            </select>
          </div>

          {/* Conversion table */}
          <table style={{ width: '100%', borderCollapse: 'collapse', marginBottom: 20 }}>
            <thead>
              <tr>
                <th style={{ textAlign: 'left', fontSize: 10, color: '#484f58', fontWeight: 600,
                             padding: '4px 8px', letterSpacing: 0.7, borderBottom: '1px solid #21262d' }}>
                  UNIT
                </th>
                <th style={{ textAlign: 'left', fontSize: 10, color: '#484f58', fontWeight: 600,
                             padding: '4px 8px', letterSpacing: 0.7, borderBottom: '1px solid #21262d' }}>
                  DESCRIPTION
                </th>
                <th style={{ textAlign: 'right', fontSize: 10, color: '#484f58', fontWeight: 600,
                             padding: '4px 8px', letterSpacing: 0.7, borderBottom: '1px solid #21262d' }}>
                  VALUE
                </th>
                <th style={{ width: 70, borderBottom: '1px solid #21262d' }} />
              </tr>
            </thead>
            <tbody>
              {UNITS.map((u) => {
                const val = isFinite(dbm) ? fromDbm(dbm, u.key) : NaN
                const isActive = u.key === activeUnit
                return (
                  <tr
                    key={u.key}
                    style={{
                      background: isActive ? '#1c2128' : 'transparent',
                      borderBottom: '1px solid #21262d',
                    }}
                  >
                    <td style={{ padding: '7px 8px' }}>
                      <span style={{
                        fontFamily: 'monospace', fontSize: 13, fontWeight: 700,
                        color: isActive ? '#00b4d8' : '#c9d1d9',
                      }}>
                        {u.label}
                      </span>
                    </td>
                    <td style={{ padding: '7px 8px', fontSize: 11, color: '#484f58' }}>
                      {u.desc}
                    </td>
                    <td style={{ padding: '7px 8px', textAlign: 'right' }}>
                      <span style={{
                        fontFamily: 'monospace', fontSize: 13,
                        color: isActive ? '#e6edf3' : '#8b949e',
                      }}>
                        {isFinite(val) ? fmt(val, u.key) : '—'}
                      </span>
                      {' '}
                      <span style={{ fontSize: 10, color: '#484f58' }}>{u.label}</span>
                    </td>
                    <td style={{ padding: '4px 8px', textAlign: 'right' }}>
                      {!isActive && (
                        <button
                          style={{
                            background: 'none', border: '1px solid #30363d', borderRadius: 4,
                            color: '#8b949e', fontSize: 10, padding: '2px 7px',
                            cursor: 'pointer', transition: 'all 100ms',
                          }}
                          onMouseEnter={e => { e.currentTarget.style.borderColor = '#00b4d8'; e.currentTarget.style.color = '#00b4d8' }}
                          onMouseLeave={e => { e.currentTarget.style.borderColor = '#30363d'; e.currentTarget.style.color = '#8b949e' }}
                          onClick={() => handleUnitSwitch(u.key)}
                          title={`Switch input to ${u.label}`}
                        >
                          edit
                        </button>
                      )}
                    </td>
                  </tr>
                )
              })}
            </tbody>
          </table>

          {/* Logarithmic graph */}
          <div style={{
            background: '#0d1117', border: '1px solid #21262d', borderRadius: 8,
            padding: '14px 10px 6px',
          }}>
            <div style={{ fontSize: 11, fontWeight: 700, color: '#8b949e',
                          marginBottom: 8, letterSpacing: 0.8, textTransform: 'uppercase' }}>
              Power Scale (dBm = log scale)
            </div>
            <LogGraph dbm={dbm} />
          </div>

        </div>
      </div>
    </div>
  )
}
