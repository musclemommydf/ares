// SPDX-License-Identifier: MIT OR Apache-2.0
// Copyright (c) 2026 Ares

/**
 * Renders the result of whichever analysis the active tab ran — Best Site, Route, Multipoint,
 * MANET, Best Server, BSA Polygon. This lives in the bottom "Results" tab; the left-sidebar
 * panels for these tabs hold only the inputs (add candidate / draw / set %), not the results.
 */

const card = { background: '#0d1117', border: '1px solid #21262d', borderRadius: 6, padding: '10px 12px' }
const hdr = { fontSize: 10, fontWeight: 700, color: '#8b949e', letterSpacing: 0.6, textTransform: 'uppercase', marginBottom: 6 }
const row = { display: 'flex', justifyContent: 'space-between', gap: 10, fontSize: 11, padding: '3px 0', borderBottom: '1px solid #161b22', whiteSpace: 'nowrap' }
const empty = (msg) => (
  <div style={{ padding: 20, color: '#6e7681', fontSize: 12, textAlign: 'center' }}>{msg}</div>
)
const Stat = ({ value, label, color }) => (
  <div><div style={{ fontSize: 18, fontWeight: 700, color: color || '#e6edf3' }}>{value}</div><div style={{ fontSize: 10, color: '#8b949e' }}>{label}</div></div>
)

function RankedSites({ sites, title, fmt }) {
  if (!sites?.length) return null
  return (
    <div style={card}>
      <div style={hdr}>{title}</div>
      {sites.map((s, i) => (
        <div key={i} style={row}>
          <span style={{ color: i === 0 ? '#06d6a0' : '#c9d1d9' }}>{i === 0 ? '★ ' : `${i + 1}. `}{s.label || `${s.lat?.toFixed(4)}, ${s.lon?.toFixed(4)}`}</span>
          <span style={{ color: '#8b949e' }}>{fmt(s)}</span>
        </div>
      ))}
    </div>
  )
}

function WaypointTable({ geojson, kind }) {
  const pts = (geojson?.features || []).filter(f => f.geometry?.type === 'Point')
  if (!pts.length) return empty(`Run the ${kind} analysis to see per-point results.`)
  return (
    <div style={card}>
      <div style={hdr}>{kind} — {pts.length} point{pts.length > 1 ? 's' : ''}</div>
      <div style={{ maxHeight: 220, overflowY: 'auto' }}>
        {pts.map((f, i) => {
          const p = f.properties || {}
          const [lon, lat] = f.geometry.coordinates
          const dbm = p.signal_dbm ?? p.received_signal_dbm
          const covered = p.covered ?? (dbm != null ? dbm >= (p.min_signal_dbm ?? -120) : undefined)
          return (
            <div key={i} style={row}>
              <span style={{ color: covered === false ? '#ef4444' : '#c9d1d9' }}>{p.label || p.name || `#${i + 1}`} <span style={{ color: '#484f58' }}>{lat?.toFixed(4)}, {lon?.toFixed(4)}</span></span>
              <span style={{ color: '#8b949e' }}>
                {dbm != null ? `${Number(dbm).toFixed(1)} dBm` : '—'}{p.path_loss_db != null ? ` · PL ${Number(p.path_loss_db).toFixed(0)} dB` : ''}{p.distance_m != null ? ` · ${(p.distance_m / 1000).toFixed(1)} km` : ''}
              </span>
            </div>
          )
        })}
      </div>
    </div>
  )
}

export default function AnalysisResults({
  activeTab, bestSiteResult, bestSiteCandidates = [], routeResult, multipointResult,
  manetResult, bestServerResult, bsaPolygonResult,
}) {
  const wrap = (children) => <div style={{ padding: 12, display: 'flex', flexDirection: 'column', gap: 10, overflowY: 'auto', height: '100%' }}>{children}</div>

  if (activeTab === 'best_site') {
    if (!bestSiteResult?.sites?.length) return empty(`Add candidate sites (click the map) — ${bestSiteCandidates.length} so far — then click Run.`)
    return wrap(<RankedSites sites={bestSiteResult.sites} title="Best site — ranking" fmt={s => `${s.covered_area_km2} km² · ${s.avg_signal_dbm} dBm`} />)
  }

  if (activeTab === 'best_site_polygon') {
    if (!bsaPolygonResult?.sites?.length) return empty('Draw a polygon, set the sample density, then click Run.')
    return wrap(<>
      <div style={{ fontSize: 11, color: '#8b949e' }}>Best of {bsaPolygonResult.num_candidates} candidate site(s) inside the polygon:</div>
      <RankedSites sites={bsaPolygonResult.sites.slice(0, 8)} title="BSA polygon — best sites" fmt={s => `${s.covered_area_km2} km²${s.avg_signal_dbm != null ? ` · ${s.avg_signal_dbm} dBm` : ''}`} />
    </>)
  }

  if (activeTab === 'best_server') {
    if (!bestServerResult?.sites?.length) return empty('Set a query point on the map (right-click → set RX, or click in best-server mode), then click Run.')
    return wrap(<RankedSites sites={bestServerResult.sites} title="Best server — ranked" fmt={s => `${s.signal_dbm} dBm${s.distance_m != null ? ` · ${(s.distance_m / 1000).toFixed(1)} km` : ''}`} />)
  }

  if (activeTab === 'manet') {
    const feats = manetResult?.features || []
    if (!feats.length) return empty('Place ≥ 2 MANET nodes on the map, then click Run.')
    const links = feats.filter(f => f.geometry?.type === 'LineString')
    const connected = links.filter(f => f.properties?.connected).length
    return wrap(<>
      <div style={{ ...card, display: 'flex', gap: 22, flexWrap: 'wrap' }}>
        <Stat value={connected} label="Connected links" color="#06d6a0" />
        <Stat value={links.length - connected} label="Disconnected" color="#ef4444" />
        <Stat value={links.length ? `${Math.round(connected / links.length * 100)}%` : '—'} label="Connectivity" color="#00b4d8" />
        <Stat value={feats.filter(f => f.geometry?.type === 'Point').length} label="Nodes" />
      </div>
      <div style={card}>
        <div style={hdr}>Links (by signal)</div>
        <div style={{ maxHeight: 220, overflowY: 'auto' }}>
          {links.slice().sort((a, b) => (b.properties?.signal_dbm ?? -999) - (a.properties?.signal_dbm ?? -999)).map((f, i) => (
            <div key={i} style={row}>
              <span style={{ color: f.properties?.connected ? '#06d6a0' : '#ef4444' }}>{f.properties?.connected ? '✓' : '✗'} {f.properties?.node_a} → {f.properties?.node_b}</span>
              <span style={{ color: '#8b949e' }}>{f.properties?.signal_dbm} dBm{f.properties?.distance_m != null ? ` · ${(f.properties.distance_m / 1000).toFixed(1)} km` : ''}</span>
            </div>
          ))}
        </div>
      </div>
    </>)
  }

  if (activeTab === 'route') return wrap(<WaypointTable geojson={routeResult?.geojson} kind="Route" />)
  if (activeTab === 'multipoint') return wrap(<WaypointTable geojson={multipointResult?.geojson} kind="Multipoint" />)

  return empty('No analysis result yet — pick an analysis tab and click Run Simulation.')
}
