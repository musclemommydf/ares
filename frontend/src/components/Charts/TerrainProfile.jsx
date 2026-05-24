// SPDX-License-Identifier: MIT OR Apache-2.0
// Copyright (c) 2026 Ares

/**
 * Terrain Profile Chart
 * Shows elevation cross-section with LOS line, Fresnel zones, and signal strength.
 * Uses Plotly for interactive rendering.
 * Includes HF 24h skywave chart tab when using HF/NVIS models.
 */
import { useState } from 'react'
import Plot from 'react-plotly.js'
import { formatDistance } from '../../api/client'
import HfSkywaveChart from '../Tools/HfSkywaveChart'

export default function TerrainProfile({
  profile, txHeight = 30, rxHeight = 1.5, frequencyHz = 433e6,
  propagationModel = '', waveType = 'auto',
  txLat = 52, txLon = 0,
  standalone = false,  // when true, hide LOS / Fresnel / TX / RX traces
}) {
  const [chartTab, setChartTab] = useState('terrain')

  const isHf = (frequencyHz / 1e6) >= 2 && (frequencyHz / 1e6) <= 30
  const isHfModel = propagationModel === 'nvis_hf' || waveType === 'skywave'
  const showHfTab = isHf || isHfModel

  if (!profile?.distances_m?.length) {
    return (
      <div style={{ height: '100%', display: 'flex', flexDirection: 'column' }}>
        {showHfTab && (
          <div style={{ display: 'flex', borderBottom: '1px solid #21262d', padding: '0 8px' }}>
            <button
              className={`tab ${chartTab === 'terrain' ? 'active' : ''}`}
              style={{ fontSize: 11, padding: '4px 10px' }}
              onClick={() => setChartTab('terrain')}
            >Terrain Profile</button>
            <button
              className={`tab ${chartTab === 'hf24h' ? 'active' : ''}`}
              style={{ fontSize: 11, padding: '4px 10px' }}
              onClick={() => setChartTab('hf24h')}
            >HF 24h Chart</button>
          </div>
        )}
        {chartTab === 'hf24h' && showHfTab ? (
          <div style={{ flex: 1 }}>
            <HfSkywaveChart txLat={txLat} txLon={txLon} />
          </div>
        ) : (
          <div style={{
            flex: 1, display: 'flex', alignItems: 'center', justifyContent: 'center',
            color: 'var(--text-muted)', fontSize: 13,
          }}>
            Run a point-to-point simulation to see the terrain profile
          </div>
        )}
      </div>
    )
  }

  const { distances_m, elevations_m, los_heights_m, fresnel_radii_m } = profile

  // Convert to km for display
  const dist_km = distances_m.map(d => d / 1000)
  const total_km = dist_km[dist_km.length - 1]

  // F1 upper and lower boundaries (only meaningful when LOS is provided)
  const hasLos = !standalone && Array.isArray(los_heights_m) && Array.isArray(fresnel_radii_m)
  const f1_upper = hasLos ? los_heights_m.map((h, i) => h + fresnel_radii_m[i]) : []
  const f1_lower = hasLos ? los_heights_m.map((h, i) => h - fresnel_radii_m[i]) : []

  const blocked = hasLos && elevations_m.some((e, i) => e > los_heights_m[i])

  // Baseline placed just below the lowest point so Plotly autoscale shows
  // the actual elevation band, not the band from zero up.
  const finiteElevsForBase = elevations_m.filter(Number.isFinite)
  const baseValForFill = finiteElevsForBase.length
    ? Math.min(...finiteElevsForBase) - Math.max(20, (Math.max(...finiteElevsForBase) - Math.min(...finiteElevsForBase)) * 0.10)
    : 0
  const baselineY = elevations_m.map(() => baseValForFill)

  const traces = [
    // Hidden baseline (sets the floor for the terrain fill)
    {
      x: dist_km,
      y: baselineY,
      mode: 'lines',
      line: { color: 'transparent', width: 0 },
      hoverinfo: 'skip',
      showlegend: false,
    },
    // Terrain fill — fills from this trace down to the previous (baseline)
    {
      x: dist_km,
      y: elevations_m,
      fill: 'tonexty',
      fillcolor: 'rgba(55,65,81,0.8)',
      line: { color: '#4b5563', width: 1.5 },
      name: 'Terrain',
      mode: 'lines',
      hovertemplate: '%{y:.0f} m<extra>Terrain</extra>',
    },
  ]

  if (hasLos) {
    traces.push(
      // Fresnel zone 1 fill
      {
        x: [...dist_km, ...dist_km.slice().reverse()],
        y: [...f1_upper, ...f1_lower.slice().reverse()],
        fill: 'toself',
        fillcolor: blocked ? 'rgba(239,68,68,0.1)' : 'rgba(0,180,216,0.07)',
        line: { color: 'transparent' },
        name: 'Fresnel Zone 1',
        mode: 'lines',
        hoverinfo: 'skip',
      },
      {
        x: dist_km, y: f1_upper,
        line: { color: 'rgba(0,180,216,0.3)', width: 1, dash: 'dot' },
        name: 'F1 boundary', mode: 'lines', hoverinfo: 'skip',
      },
      {
        x: dist_km, y: f1_lower,
        line: { color: 'rgba(0,180,216,0.3)', width: 1, dash: 'dot' },
        showlegend: false, mode: 'lines', hoverinfo: 'skip',
      },
      {
        x: dist_km, y: los_heights_m,
        line: { color: blocked ? '#ef4444' : '#06d6a0', width: 2, dash: blocked ? 'dash' : 'solid' },
        name: `LOS ${blocked ? '(BLOCKED)' : '(Clear)'}`,
        mode: 'lines', hovertemplate: '%{y:.0f} m<extra>LOS</extra>',
      },
      {
        x: [0], y: [(elevations_m[0] || 0) + txHeight], mode: 'markers',
        marker: { symbol: 'triangle-up', size: 10, color: '#00b4d8' },
        name: 'TX', hovertemplate: 'TX: %{y:.0f} m<extra></extra>',
      },
      {
        x: [total_km], y: [(elevations_m[elevations_m.length - 1] || 0) + rxHeight], mode: 'markers',
        marker: { symbol: 'circle', size: 8, color: '#a855f7' },
        name: 'RX', hovertemplate: 'RX: %{y:.0f} m<extra></extra>',
      },
    )
  }

  // Compute y-range from actual data so the chart doesn't include y=0 just
  // because the terrain trace uses fill: 'tozeroy'. Plotly's autoscale alone
  // would extend down to zero and squash the visible elevation band.
  const finiteElevs = elevations_m.filter(Number.isFinite)
  const yPool = finiteElevs.length ? [...finiteElevs] : [0]
  if (hasLos) {
    los_heights_m.forEach(v => Number.isFinite(v) && yPool.push(v))
    f1_upper.forEach(v => Number.isFinite(v) && yPool.push(v))
    f1_lower.forEach(v => Number.isFinite(v) && yPool.push(v))
    yPool.push((finiteElevs[0] || 0) + txHeight)
    yPool.push((finiteElevs[finiteElevs.length - 1] || 0) + rxHeight)
  }
  const yMin = Math.min(...yPool)
  const yMax = Math.max(...yPool)
  const yPad = Math.max(20, (yMax - yMin) * 0.10)

  const layout = {
    paper_bgcolor: 'transparent',
    plot_bgcolor: 'rgba(13,17,23,0.6)',
    font: { color: '#8b949e', size: 11, family: 'Inter, system-ui, sans-serif' },
    margin: { l: 48, r: 12, t: 8, b: 36 },
    legend: {
      x: 0.01, y: 0.99,
      bgcolor: 'rgba(22,27,34,0.85)',
      bordercolor: '#30363d',
      borderwidth: 1,
      font: { size: 10 },
    },
    xaxis: {
      title: { text: 'Distance (km)', font: { size: 11 } },
      gridcolor: '#21262d',
      zerolinecolor: '#30363d',
      tickfont: { size: 10 },
      range: [0, total_km],
    },
    yaxis: {
      title: { text: 'Elevation (m ASL)', font: { size: 11 } },
      gridcolor: '#21262d',
      zerolinecolor: '#30363d',
      tickfont: { size: 10 },
      range: [yMin - yPad, yMax + yPad],
      autorange: false,
    },
    hovermode: 'x unified',
    hoverlabel: {
      bgcolor: '#161b22',
      bordercolor: '#30363d',
      font: { color: '#e6edf3', size: 11 },
    },
  }

  const config = {
    displayModeBar: true,
    displaylogo: false,
    modeBarButtonsToRemove: ['sendDataToCloud'],
    toImageButtonOptions: {
      format: 'png',
      filename: 'terrain-profile',
      height: 400, width: 900, scale: 2,
    },
    responsive: true,
  }

  return (
    <div style={{ height: '100%', width: '100%', display: 'flex', flexDirection: 'column' }}>
      {showHfTab && (
        <div style={{ display: 'flex', borderBottom: '1px solid #21262d', padding: '0 8px', flexShrink: 0 }}>
          <button
            className={`tab ${chartTab === 'terrain' ? 'active' : ''}`}
            style={{ fontSize: 11, padding: '4px 10px' }}
            onClick={() => setChartTab('terrain')}
          >Terrain Profile</button>
          <button
            className={`tab ${chartTab === 'hf24h' ? 'active' : ''}`}
            style={{ fontSize: 11, padding: '4px 10px' }}
            onClick={() => setChartTab('hf24h')}
          >HF 24h Chart</button>
        </div>
      )}

      {chartTab === 'hf24h' && showHfTab ? (
        <div style={{ flex: 1 }}>
          <HfSkywaveChart txLat={txLat} txLon={txLon} />
        </div>
      ) : (
        <div style={{ flex: 1, position: 'relative' }}>
          {blocked && (
            <div className="alert alert-warning" style={{
              position: 'absolute', top: 4, right: 8, zIndex: 10,
              padding: '3px 8px', margin: 0,
            }}>
              ⚠ Fresnel zone obstructed
            </div>
          )}
          <Plot
            data={traces}
            layout={layout}
            config={config}
            style={{ width: '100%', height: '100%' }}
            useResizeHandler
          />
        </div>
      )}
    </div>
  )
}
