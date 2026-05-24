// SPDX-License-Identifier: MIT OR Apache-2.0
// Copyright (c) 2026 Ares

/**
 * HF 24h Skywave / NVIS Chart
 * Shows a Plotly heatmap of predicted signal quality vs frequency (2-30 MHz)
 * vs UTC hour (0-23), using a simplified MUF model.
 *
 * MUF model:
 *   - Baseline MUF varies with solar flux proxy (F10.7-like)
 *   - Diurnal variation: MUF peaks around local noon (12h UTC offset by lon)
 *   - MUF(hour) = MUF_noon * diurnal_factor(hour)
 *   - Signal is "good" if freq <= MUF and freq >= LUF
 *   - NVIS is constrained to short ranges (< 500 km), uses F-layer at night,
 *     D-layer absorption during day
 */
import { useMemo } from 'react'
import Plot from 'react-plotly.js'

/**
 * Simplified daily MUF model.
 * lat: latitude of TX (affects ionospheric tilt)
 * lon: for local noon estimation
 * month: 1-12 for seasonal variation
 * Returns MUF(hour) array [0..23]
 */
function computeDailyMuf(lat = 52, lon = 0, month = 6) {
  // Solar noon offset in UTC hours from longitude
  const noon_utc = 12 - lon / 15.0

  // Seasonal factor: summer in N hemisphere → higher MUF
  const season_amp = Math.cos((month - 6) * Math.PI / 6) * 0.15  // ±15%
  // Latitude factor: equatorial > polar
  const lat_factor = 1.0 - Math.abs(lat) / 120.0

  // Baseline MUF at solar noon (MHz) — typical mid-latitude
  const muf_noon = 25.0 * lat_factor * (1.0 + season_amp)

  // MUF at midnight is lower due to F-layer recombination
  const muf_midnight = muf_noon * 0.45

  const mufs = []
  for (let h = 0; h < 24; h++) {
    // Hours from local noon (wrapped)
    const delta = ((h - noon_utc + 12) % 24) - 12  // -12 to +12
    // Cosine diurnal pattern
    const factor = Math.cos(delta * Math.PI / 14)   // peak at noon, valley at midnight
    const clamped = Math.max(0, factor)
    const muf = muf_midnight + (muf_noon - muf_midnight) * clamped
    mufs.push(Math.max(3.0, muf))
  }
  return mufs
}

/**
 * Compute LUF per hour.
 * LUF is dominated by D-layer absorption (active 06-18 local time).
 */
function computeDailyLuf(lat = 52, lon = 0) {
  const noon_utc = 12 - lon / 15.0
  const lufs = []
  for (let h = 0; h < 24; h++) {
    const delta = ((h - noon_utc + 12) % 24) - 12
    // D-layer absorption peaks near noon
    const day_frac = Math.max(0, Math.cos(delta * Math.PI / 8))  // active ±8h around noon
    const luf = 2.0 + day_frac * 4.0   // 2 MHz at night, ~6 MHz at noon
    lufs.push(luf)
  }
  return lufs
}

/**
 * Signal quality matrix: freqs (rows) x hours (cols)
 * Returns values 0-10 (0=no signal, 10=excellent)
 */
function computeSignalMatrix(freqs, mufs, lufs) {
  return freqs.map(f => {
    return mufs.map((muf, h) => {
      const luf = lufs[h]
      if (f > muf) return 0        // above MUF — refracted out
      if (f < luf) return 0        // below LUF — D-layer absorption
      // Quality: distance from LUF and MUF boundaries
      const muf_margin = (muf - f) / muf       // 0 near MUF, 1 far below
      const luf_margin = (f - luf) / (muf - luf + 0.01)  // 0 near LUF, 1 far above
      const quality = Math.min(muf_margin, luf_margin) * 2  // 0-1
      return Math.min(10, Math.round(quality * 12))
    })
  })
}

export default function HfSkywaveChart({ txLat = 52, txLon = 0 }) {
  const now = new Date()
  const month = now.getUTCMonth() + 1

  const { freqs, hours, z, mufs, lufs } = useMemo(() => {
    const freqs = []
    for (let f = 2; f <= 30; f += 0.5) freqs.push(f)
    const hours = Array.from({ length: 24 }, (_, i) => i)
    const mufs = computeDailyMuf(txLat, txLon, month)
    const lufs = computeDailyLuf(txLat, txLon)
    const z = computeSignalMatrix(freqs, mufs, lufs)
    return { freqs, hours, z, mufs, lufs }
  }, [txLat, txLon, month])

  const traces = [
    {
      type: 'heatmap',
      x: hours,
      y: freqs,
      z,
      colorscale: [
        [0.0,  'rgba(20,20,40,0.9)'],
        [0.01, 'rgba(239,68,68,0.8)'],
        [0.3,  'rgba(245,158,11,0.9)'],
        [0.6,  'rgba(132,204,22,0.95)'],
        [1.0,  'rgba(6,214,160,1.0)'],
      ],
      zmin: 0, zmax: 10,
      showscale: true,
      colorbar: {
        title: { text: 'Signal Quality', font: { color: '#8b949e', size: 10 } },
        tickfont: { color: '#8b949e', size: 9 },
        len: 0.8,
        thickness: 10,
        x: 1.01,
      },
      hovertemplate:
        'Hour: %{x}h UTC<br>Freq: %{y} MHz<br>Quality: %{z}/10<extra></extra>',
    },
    // MUF overlay
    {
      type: 'scatter',
      x: hours,
      y: mufs,
      mode: 'lines',
      line: { color: '#06d6a0', width: 2, dash: 'solid' },
      name: 'MUF',
      hovertemplate: 'MUF: %{y:.1f} MHz<extra></extra>',
    },
    // LUF overlay
    {
      type: 'scatter',
      x: hours,
      y: lufs,
      mode: 'lines',
      line: { color: '#ef4444', width: 1.5, dash: 'dash' },
      name: 'LUF',
      hovertemplate: 'LUF: %{y:.1f} MHz<extra></extra>',
    },
  ]

  const layout = {
    paper_bgcolor: 'transparent',
    plot_bgcolor: 'rgba(13,17,23,0.6)',
    font: { color: '#8b949e', size: 11, family: 'Inter, system-ui, sans-serif' },
    margin: { l: 52, r: 60, t: 28, b: 40 },
    title: {
      text: `HF Propagation Window — ${now.toLocaleDateString('en', { month: 'short', year: 'numeric' })} · Lat ${txLat.toFixed(1)}°`,
      font: { size: 12, color: '#c9d1d9' },
      x: 0.02,
    },
    xaxis: {
      title: { text: 'UTC Hour', font: { size: 11 } },
      tickvals: [0, 3, 6, 9, 12, 15, 18, 21, 23],
      ticktext: ['00:00', '03:00', '06:00', '09:00', '12:00', '15:00', '18:00', '21:00', '23:00'],
      gridcolor: '#21262d',
      tickfont: { size: 9 },
    },
    yaxis: {
      title: { text: 'Frequency (MHz)', font: { size: 11 } },
      gridcolor: '#21262d',
      tickfont: { size: 10 },
      range: [2, 30],
    },
    legend: {
      x: 0.01, y: 0.99,
      bgcolor: 'rgba(22,27,34,0.85)',
      bordercolor: '#30363d',
      borderwidth: 1,
      font: { size: 10 },
    },
    hovermode: 'closest',
    hoverlabel: {
      bgcolor: '#161b22',
      bordercolor: '#30363d',
      font: { color: '#e6edf3', size: 11 },
    },
    annotations: [
      {
        x: 0.5, y: 1.0,
        xref: 'paper', yref: 'paper',
        text: 'Green = MUF boundary · Red dashed = LUF · Dark = no propagation',
        showarrow: false,
        font: { size: 9, color: '#444d56' },
        xanchor: 'center', yanchor: 'bottom',
      },
    ],
  }

  const config = {
    displayModeBar: true,
    displaylogo: false,
    modeBarButtonsToRemove: ['sendDataToCloud'],
    toImageButtonOptions: {
      format: 'png', filename: 'hf-skywave-chart',
      height: 400, width: 700, scale: 2,
    },
    responsive: true,
  }

  return (
    <div style={{ height: '100%', width: '100%' }}>
      <Plot
        data={traces}
        layout={layout}
        config={config}
        style={{ width: '100%', height: '100%' }}
        useResizeHandler
      />
    </div>
  )
}
