// SPDX-License-Identifier: MIT OR Apache-2.0
// Copyright (c) 2026 Ares

export default function CoverageLegend({ minDbm = -120, maxDbm = 0 }) {
  const levels = [
    { label: `≥ −60 dBm`, color: '#06d6a0', desc: 'Excellent' },
    { label: '−75 dBm', color: '#84cc16', desc: 'Good' },
    { label: '−90 dBm', color: '#f59e0b', desc: 'Fair' },
    { label: '−100 dBm', color: '#ef4444', desc: 'Poor' },
    { label: '< −110 dBm', color: '#6b7280', desc: 'Very Poor' },
  ]

  return (
    <div className="legend">
      <div className="legend-title">Signal Strength</div>
      {levels.map(l => (
        <div key={l.label} className="legend-item">
          <div className="legend-swatch" style={{ background: l.color }} />
          <span>{l.desc}</span>
          <span style={{ marginLeft: 'auto', color: 'var(--text-muted)', fontSize: 10 }}>
            {l.label}
          </span>
        </div>
      ))}
    </div>
  )
}
