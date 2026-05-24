// SPDX-License-Identifier: MIT OR Apache-2.0
// Copyright (c) 2026 Ares

/**
 * Results panel — shows coverage stats, link budget, and space weather.
 */
import { dbmToQuality, formatDistance } from '../../api/client'

function StatCard({ label, value, unit, color }) {
  return (
    <div className="stat-card">
      <div className="stat-label">{label}</div>
      <div className="stat-value" style={color ? { color } : {}}>
        {value}
        {unit && <span className="stat-unit"> {unit}</span>}
      </div>
    </div>
  )
}

function SignalBars({ dbm }) {
  const q = dbmToQuality(dbm)
  return (
    <div className="signal-meter">
      <div className="signal-bars">
        {[1, 2, 3, 4, 5].map(i => (
          <div
            key={i}
            className={`signal-bar ${i <= q.bars ? 'active excellent' : ''}`}
            style={i <= q.bars ? { background: q.color } : {}}
          />
        ))}
      </div>
      <span style={{ fontSize: 12, color: q.color, fontWeight: 600 }}>{q.label}</span>
    </div>
  )
}

function LinkBudgetTable({ budget }) {
  if (!budget) return null
  const rows = [
    { label: 'TX Power', value: budget.tx_power_dbm?.toFixed(1), unit: 'dBm', positive: true },
    { label: 'TX Antenna Gain', value: `+${budget.tx_antenna_gain_dbi?.toFixed(1)}`, unit: 'dBi', positive: true },
    { label: 'EIRP', value: budget.eirp_dbm?.toFixed(1), unit: 'dBm', highlight: true },
    { label: 'Path Loss', value: `-${budget.path_loss_db?.toFixed(1)}`, unit: 'dB', negative: true },
    { label: 'Atmospheric Loss', value: `-${budget.atmospheric_loss_db?.toFixed(2)}`, unit: 'dB', negative: true },
    { label: 'Rain Loss', value: `-${budget.rain_loss_db?.toFixed(2)}`, unit: 'dB', negative: true },
    { label: 'RX Antenna Gain', value: `+${budget.rx_antenna_gain_dbi?.toFixed(1)}`, unit: 'dBi', positive: true },
    { label: 'Received Signal', value: budget.received_power_dbm?.toFixed(1), unit: 'dBm', highlight: true },
    { label: 'RX Sensitivity', value: budget.rx_sensitivity_dbm?.toFixed(0), unit: 'dBm' },
    { label: 'Link Margin', value: budget.link_margin_db?.toFixed(1), unit: 'dB',
      positive: budget.is_viable, negative: !budget.is_viable },
  ]

  return (
    <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 12 }}>
      <tbody>
        {rows.map((r, i) => (
          <tr key={i} style={{
            background: r.highlight ? 'rgba(0,180,216,0.06)' : 'transparent',
            borderBottom: r.highlight ? '1px solid var(--border-muted)' : undefined,
          }}>
            <td style={{ padding: '4px 12px', color: 'var(--text-secondary)' }}>{r.label}</td>
            <td style={{
              padding: '4px 12px',
              textAlign: 'right',
              fontWeight: r.highlight ? 600 : 400,
              fontFamily: 'monospace',
              color: r.positive ? 'var(--accent-green)' :
                     r.negative ? 'var(--accent-red)' :
                     r.highlight ? 'var(--accent-blue)' :
                     'var(--text-primary)',
            }}>
              {r.value} <span style={{ color: 'var(--text-muted)' }}>{r.unit}</span>
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  )
}

function LinkBudgetSection({ budget, isViable }) {
  if (!budget) return null
  return (
    <div style={{ borderTop: '1px solid var(--border-muted)', marginTop: 8, paddingTop: 8 }}>
      <div style={{ padding: '0 14px 6px', fontSize: 11, fontWeight: 600, letterSpacing: 0.4,
                    textTransform: 'uppercase', color: 'var(--text-muted)' }}>
        Link Budget
      </div>
      <LinkBudgetTable budget={budget} />
      {isViable !== undefined && (
        <div style={{ padding: '8px 12px' }}>
          <div className={`alert ${budget.is_viable ? 'alert-success' : 'alert-warning'}`}>
            {budget.is_viable
              ? '✓ Link is viable'
              : '✗ Link margin insufficient — consider increasing power, gain, or reducing distance'}
          </div>
        </div>
      )}
    </div>
  )
}

export default function ResultsPanel({
  metadata, p2pResult, warnings, spaceWeather, activeTab
}) {
  // Coverage results (also covers the radar tab — it shares the coverage path)
  if ((activeTab === 'coverage' || activeTab === 'radar') && metadata) {
    return (
      <div>
        <div className="stats-grid">
          <StatCard
            label="Max Range"
            value={metadata.max_range_km?.toFixed(1)}
            unit="km"
            color="var(--accent-blue)"
          />
          <StatCard
            label="Avg Signal"
            value={metadata.avg_signal_dbm?.toFixed(0)}
            unit="dBm"
            color={dbmToQuality(metadata.avg_signal_dbm || -100).color}
          />
          <StatCard
            label="Coverage"
            value={metadata.covered_area_km2?.toFixed(0)}
            unit="km²"
            color="var(--accent-green)"
          />
        </div>

        <div style={{ padding: '0 14px 8px', display: 'flex', gap: 8, alignItems: 'center' }}>
          <span style={{ fontSize: 11, color: 'var(--text-muted)' }}>
            Signal quality:
          </span>
          <SignalBars dbm={metadata.avg_signal_dbm || -100} />
        </div>

        {warnings?.length > 0 && (
          <div style={{ padding: '0 14px 8px' }}>
            {warnings.slice(0, 3).map((w, i) => (
              <div key={i} className="alert alert-warning" style={{ marginBottom: 4 }}>
                {w}
              </div>
            ))}
          </div>
        )}

        <div style={{ padding: '0 14px 4px', fontSize: 11, color: 'var(--text-muted)' }}>
          Computed in {metadata.computation_time_s?.toFixed(2)}s
          {metadata.gpu_used && <span style={{ color: 'var(--accent-purple)', marginLeft: 6 }}>GPU</span>}
          {' · '}{(metadata.num_points || 0).toLocaleString()} points
        </div>

        <LinkBudgetSection budget={metadata.link_budget} isViable={metadata.link_budget?.is_viable} />
      </div>
    )
  }

  // P2P results
  if (activeTab === 'p2p' && p2pResult) {
    const mode = p2pResult.propagation_mode
    return (
      <div>
        <div className="stats-grid">
          <StatCard
            label="Path Loss"
            value={p2pResult.path_loss_db?.toFixed(1)}
            unit="dB"
            color="var(--accent-amber)"
          />
          <StatCard
            label="Rx Signal"
            value={p2pResult.received_signal_dbm?.toFixed(1)}
            unit="dBm"
            color={dbmToQuality(p2pResult.received_signal_dbm || -100).color}
          />
          <StatCard
            label="Mode"
            value={mode?.charAt(0).toUpperCase() + mode?.slice(1) || '—'}
            color="var(--accent-blue)"
          />
        </div>

        {warnings?.length > 0 && (
          <div style={{ padding: '0 14px 8px' }}>
            {warnings.slice(0, 3).map((w, i) => (
              <div key={i} className="alert alert-warning" style={{ marginBottom: 4 }}>
                {w}
              </div>
            ))}
          </div>
        )}

        <LinkBudgetSection budget={p2pResult.link_budget} isViable={p2pResult.link_budget?.is_viable} />
      </div>
    )
  }

  return (
    <div style={{ padding: 16, color: 'var(--text-muted)', fontSize: 12, textAlign: 'center' }}>
      <p>Configure transmitter settings and click <strong style={{ color: 'var(--text-primary)' }}>Run Simulation</strong></p>
      <p style={{ marginTop: 4 }}>Results will appear here.</p>
    </div>
  )
}
