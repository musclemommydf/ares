// SPDX-License-Identifier: MIT OR Apache-2.0
// Copyright (c) 2026 Ares

/**
 * 3D Coverage View
 * Plotly-based 3D visualisation with terrain surface, coverage volume,
 * building blocks, and beam cone from the TX position.
 */
import { useMemo } from 'react'
import Plot from 'react-plotly.js'
import { computePatternBeamwidths } from '../../utils/polarPatterns'

// Map signal dBm to an RGB colour string (mirrors signalToColor in client.js)
function dbmToRgb(dbm) {
  const norm = Math.max(0, Math.min(1, (dbm + 120) / 80))
  if (norm < 0.25) {
    const t = norm / 0.25
    return `rgb(${Math.round(255 * t)},0,${Math.round(128 + 127 * t)})`
  } else if (norm < 0.5) {
    const t = (norm - 0.25) / 0.25
    return `rgb(255,${Math.round(165 * t)},0)`
  } else if (norm < 0.75) {
    const t = (norm - 0.5) / 0.25
    return `rgb(255,${Math.round(165 + 90 * t)},0)`
  }
  const t = (norm - 0.75) / 0.25
  return `rgb(${Math.round(255 - 255 * t)},255,0)`
}

// Generate a beam cone as a set of line traces
function buildBeamCone(tx, beamHeightMin, beamHeightMax, beamWidthDeg, azimuthDeg, tiltDeg) {
  if (!tx || tx.lat == null || tx.lon == null) return []
  const numRays = 24
  const halfBw = (beamWidthDeg ?? 360) / 2
  const isOmni = !beamWidthDeg || beamWidthDeg >= 360
  const txLat = tx.lat, txLon = tx.lon
  const txElev = (tx.altitude_m ?? 0) + (tx.height_m ?? 30)
  // Range to trace: use beam height range to determine slant distance
  const tiltRad = tiltDeg * Math.PI / 180
  const horizRange = (beamHeightMax - beamHeightMin) > 0
    ? (beamHeightMax - beamHeightMin) / Math.max(0.01, Math.abs(Math.sin(tiltRad)) || 0.1)
    : 5000  // metres
  const rangeDeg = Math.min(horizRange / 111320, 0.15)  // cap at ~16 km

  const traces = []
  for (let i = 0; i < numRays; i++) {
    const rayAz = isOmni
      ? (i / numRays) * 360
      : azimuthDeg - halfBw + (i / Math.max(1, numRays - 1)) * (beamWidthDeg ?? 360)
    const azRad = rayAz * Math.PI / 180
    const endLat = txLat + rangeDeg * Math.cos(azRad)
    const endLon = txLon + rangeDeg * Math.sin(azRad) / Math.max(0.001, Math.cos(txLat * Math.PI / 180))
    const endElev = txElev + (beamHeightMax - beamHeightMin) / 2

    traces.push({
      type: 'scatter3d',
      x: [txLon, endLon],
      y: [txLat, endLat],
      z: [txElev, endElev],
      mode: 'lines',
      line: { color: 'rgba(0,180,216,0.4)', width: 1.5 },
      showlegend: false,
      hoverinfo: 'none',
    })
  }
  return traces
}

export default function ThreeDView({
  terrainGrid, loading, coverageGeoJSON, buildingGeoJSON, tx, minSignalDbm = -120,
}) {
  const beamHeightMin = tx?.antenna?.beam_height_min_m ?? 2
  const beamHeightMax = tx?.antenna?.beam_height_max_m ?? 50
  // Derived -3 dB beamwidth from the polar pattern (null = omni → no cone).
  const polarPatternId = tx?.antenna?.polar_pattern ?? 'omni'
  const { hpbw3 } = computePatternBeamwidths(polarPatternId)
  const beamWidthDeg = hpbw3

  const traces = useMemo(() => {
    try {
      return buildTraces()
    } catch (e) {
      // never let a malformed grid / geojson crash the whole app
      // eslint-disable-next-line no-console
      console.error('[ThreeDView] trace build failed', e)
      try { return buildBeamCone(tx, beamHeightMin, beamHeightMax, beamWidthDeg, tx?.antenna?.azimuth_deg ?? 0, tx?.antenna?.tilt_deg ?? 0) }
      catch { return [] }
    }

    function buildTraces() {
    const out = []

    // ── Terrain surface ────────────────────────────────────────────────────
    const elevIs2D = Array.isArray(terrainGrid?.elevations) && Array.isArray(terrainGrid.elevations[0])
    if (terrainGrid?.lats?.length && terrainGrid?.lons?.length && elevIs2D) {
      out.push({
        type: 'surface',
        x: terrainGrid.lons,
        y: terrainGrid.lats,
        z: terrainGrid.elevations,
        colorscale: [
          [0, '#1a3a1a'], [0.2, '#2d5a27'], [0.4, '#5a8a3d'],
          [0.6, '#8b7355'], [0.8, '#9e9e9e'], [1, '#ffffff'],
        ],
        showscale: false,
        opacity: 0.85,
        hovertemplate: 'Lon: %{x:.4f}<br>Lat: %{y:.4f}<br>Elev: %{z:.0f} m<extra></extra>',
        name: 'Terrain',
        contours: { z: { show: false } },
        lighting: { ambient: 0.7, diffuse: 0.8, roughness: 0.5 },
      })
    }

    // ── Coverage scatter3d ─────────────────────────────────────────────────
    if (coverageGeoJSON?.features?.length) {
      const lons = [], lats = [], zs = [], colors = [], texts = []
      const terrainElev = elevIs2D
        ? (lat, lon) => {
            if (!terrainGrid.lats.length) return 0
            const yi = Math.round((lat - terrainGrid.lats[0]) /
              (terrainGrid.lats[terrainGrid.lats.length - 1] - terrainGrid.lats[0] + 1e-9) *
              (terrainGrid.lats.length - 1))
            const xi = Math.round((lon - terrainGrid.lons[0]) /
              (terrainGrid.lons[terrainGrid.lons.length - 1] - terrainGrid.lons[0] + 1e-9) *
              (terrainGrid.lons.length - 1))
            const row = terrainGrid.elevations[Math.max(0, Math.min(terrainGrid.lats.length - 1, yi))]
            return row?.[Math.max(0, Math.min(terrainGrid.lons.length - 1, xi))] ?? 0
          }
        : () => 0

      for (const f of coverageGeoJSON.features) {
        if (!f.properties?.covered) continue
        const c = f.geometry?.coordinates
        if (!Array.isArray(c) || c.length < 2) continue
        const [lon, lat] = c
        const dbm = Number(f.properties.signal_dbm)
        if (!Number.isFinite(lon) || !Number.isFinite(lat) || !Number.isFinite(dbm)) continue
        const baseElev = terrainElev(lat, lon)
        // Place coverage dot at terrain height + midpoint of beam height range
        const midH = (beamHeightMin + beamHeightMax) / 2
        lons.push(lon)
        lats.push(lat)
        zs.push(baseElev + midH)
        colors.push(dbm)
        texts.push(`${dbm.toFixed(1)} dBm`)
      }

      if (lons.length) {
        out.push({
          type: 'scatter3d',
          x: lons, y: lats, z: zs,
          mode: 'markers',
          marker: {
            size: 3,
            color: colors,
            colorscale: [
              [0, 'rgb(148,0,211)'], [0.2, 'rgb(255,0,0)'],
              [0.5, 'rgb(255,165,0)'], [0.8, 'rgb(255,255,0)'], [1, 'rgb(0,255,0)'],
            ],
            cmin: minSignalDbm,
            cmax: minSignalDbm + 80,
            colorbar: {
              title: 'dBm', thickness: 12, len: 0.5,
              tickfont: { size: 10, color: '#8b949e' },
              titlefont: { size: 11, color: '#8b949e' },
            },
            opacity: 0.75,
          },
          text: texts,
          hovertemplate: '%{text}<br>Lon: %{x:.4f}, Lat: %{y:.4f}, Alt: %{z:.0f}m<extra></extra>',
          name: 'Coverage',
        })
      }
    }

    // ── Building blocks ────────────────────────────────────────────────────
    if (buildingGeoJSON?.features?.length) {
      for (const f of buildingGeoJSON.features) {
        if (f.geometry?.type !== 'Polygon') continue
        const ring = f.geometry.coordinates?.[0]
        if (!Array.isArray(ring) || ring.length < 3) continue
        const h = f.properties?.height_m ?? 10
        // Draw each wall as a surface using two elevation levels
        const xs = ring.map(c => c[0])
        const ys = ring.map(c => c[1])
        const baseElev = elevIs2D
          ? (terrainGrid.elevations[Math.floor(terrainGrid.elevations.length / 2)]?.[
              Math.floor(terrainGrid.lons.length / 2)] ?? 0)
          : 0
        out.push({
          type: 'scatter3d',
          x: [...xs, xs[0]],
          y: [...ys, ys[0]],
          z: Array(xs.length + 1).fill(baseElev + h),
          mode: 'lines',
          line: { color: 'rgba(245,158,11,0.7)', width: 2 },
          showlegend: false,
          hoverinfo: 'none',
        })
      }
    }

    // ── TX marker ─────────────────────────────────────────────────────────
    if (tx?.lat != null) {
      const txElev = elevIs2D
        ? (terrainGrid.elevations[Math.floor(terrainGrid.elevations.length / 2)]?.[
            Math.floor(terrainGrid.lons.length / 2)] ?? 0)
        : 0
      out.push({
        type: 'scatter3d',
        x: [tx.lon], y: [tx.lat],
        z: [txElev + (tx.height_m ?? 30)],
        mode: 'markers+text',
        marker: { size: 8, color: '#00b4d8', symbol: 'diamond' },
        text: ['TX'],
        textposition: 'top center',
        textfont: { color: '#00b4d8', size: 11 },
        showlegend: false,
        hovertemplate: `TX<br>Lat: ${tx.lat?.toFixed(4)}, Lon: ${tx.lon?.toFixed(4)}<br>Height: ${tx.height_m}m AGL<extra></extra>`,
      })

      // Beam height range indicator (vertical bar at TX)
      out.push({
        type: 'scatter3d',
        x: [tx.lon, tx.lon],
        y: [tx.lat, tx.lat],
        z: [txElev + beamHeightMin, txElev + beamHeightMax],
        mode: 'lines',
        line: { color: 'rgba(0,180,216,0.9)', width: 4 },
        showlegend: false,
        hovertemplate: `Beam height: ${beamHeightMin}–${beamHeightMax} m AGL<extra></extra>`,
      })
    }

    // ── Beam cone traces ───────────────────────────────────────────────────
    const coneTraces = buildBeamCone(
      tx, beamHeightMin, beamHeightMax,
      beamWidthDeg, tx?.antenna?.azimuth_deg ?? 0, tx?.antenna?.tilt_deg ?? 0
    )
    out.push(...coneTraces)

    return out
    }
  }, [terrainGrid, coverageGeoJSON, buildingGeoJSON, tx, minSignalDbm, beamHeightMin, beamHeightMax, beamWidthDeg])

  if (loading) {
    return (
      <div style={{
        height: '100%', display: 'flex', alignItems: 'center', justifyContent: 'center',
        color: 'var(--text-muted)', fontSize: 13,
      }}>
        <div className="spinner" style={{ width: 20, height: 20, marginRight: 10 }} />
        Loading terrain grid…
      </div>
    )
  }

  if (!terrainGrid && !coverageGeoJSON) {
    return (
      <div style={{
        height: '100%', display: 'flex', alignItems: 'center', justifyContent: 'center',
        color: 'var(--text-muted)', fontSize: 13, flexDirection: 'column', gap: 8,
      }}>
        <div>Run a coverage simulation, then switch to this tab.</div>
        <div style={{ fontSize: 11, color: '#444d56' }}>
          The terrain grid is fetched automatically when this tab is opened.
        </div>
      </div>
    )
  }

  return (
    <div style={{ height: '100%', display: 'flex', flexDirection: 'column' }}>
      <div style={{
        display: 'flex', alignItems: 'center', gap: 12, padding: '4px 12px',
        fontSize: 11, color: '#8b949e', borderBottom: '1px solid #21262d', flexShrink: 0,
      }}>
        <span>
          Beam height:&nbsp;
          <strong style={{ color: '#00b4d8' }}>
            {beamHeightMin}–{beamHeightMax} m AGL
          </strong>
        </span>
        {coverageGeoJSON?.features?.length && (
          <span>Coverage points: {coverageGeoJSON.features.filter(f => f.properties?.covered).length}</span>
        )}
        {buildingGeoJSON?.features?.length && (
          <span>Buildings: {buildingGeoJSON.features.length}</span>
        )}
        {terrainGrid?.flat
          ? <span style={{ color: '#f0883e' }}>⚠ terrain data unavailable here — showing a flat plane (needs an SRTM source or an offline terrain pack)</span>
          : terrainGrid?.lats?.length
            ? <span style={{ color: '#444d56' }}>terrain: {terrainGrid.lats.length}×{terrainGrid.lons?.length || 0}{terrainGrid.resolution ? ` · ${terrainGrid.resolution.toUpperCase()}` : ''} around the emitter</span>
            : null}
        <span style={{ marginLeft: 'auto', color: '#444d56' }}>
          Left-drag: rotate · Scroll: zoom · Right-drag: pan · Drag top edge of panel to resize
        </span>
      </div>
      <div style={{ flex: 1, minHeight: 0 }}>
        <Plot
          data={traces}
          layout={{
            paper_bgcolor: '#0d1117',
            plot_bgcolor: '#0d1117',
            margin: { l: 10, r: 10, t: 10, b: 10 },
            scene: {
              bgcolor: '#0d1117',
              xaxis: {
                title: { text: 'Longitude', font: { color: '#8b949e', size: 11 } },
                gridcolor: '#21262d', zerolinecolor: '#30363d',
                tickfont: { color: '#8b949e', size: 9 },
              },
              yaxis: {
                title: { text: 'Latitude', font: { color: '#8b949e', size: 11 } },
                gridcolor: '#21262d', zerolinecolor: '#30363d',
                tickfont: { color: '#8b949e', size: 9 },
              },
              zaxis: {
                title: { text: 'Elevation (m)', font: { color: '#8b949e', size: 11 } },
                gridcolor: '#21262d', zerolinecolor: '#30363d',
                tickfont: { color: '#8b949e', size: 9 },
              },
              camera: {
                eye: { x: 1.2, y: -1.2, z: 0.8 },
                center: { x: 0, y: 0, z: -0.1 },
                up: { x: 0, y: 0, z: 1 },
              },
              aspectmode: 'manual',
              aspectratio: { x: 1.5, y: 1.5, z: 0.4 },
              dragmode: 'turntable',
            },
            legend: {
              font: { color: '#8b949e', size: 11 },
              bgcolor: 'rgba(13,17,23,0.85)',
              bordercolor: '#30363d', borderwidth: 1,
              x: 0, y: 1,
            },
            showlegend: true,
            uirevision: 'stable',
          }}
          config={{
            responsive: true,
            displayModeBar: true,
            modeBarButtonsToRemove: ['sendDataToCloud', 'toImage'],
            modeBarButtonsToAdd: [],
            displaylogo: false,
            scrollZoom: true,
          }}
          style={{ width: '100%', height: '100%' }}
          useResizeHandler
        />
      </div>
    </div>
  )
}
