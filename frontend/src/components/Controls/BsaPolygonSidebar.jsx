import { Hexagon } from 'lucide-react'

/**
 * The sidebar control for the Best-Site-Polygon tab: draw / clear the polygon, the
 * grid-sample-density slider, and the top candidate sites once it has run. App owns
 * the polygon coords, the sample-density %, the result and the draw mode.
 */
export default function BsaPolygonSidebar({ drawMode, polygonCoords, coveragePct, result, onToggleDraw, onClearPolygon, onSetCoveragePct }) {
  return (
    <div style={{ borderTop: '1px solid #21262d', padding: '8px 12px' }}>
      <div style={{ fontSize: 11, fontWeight: 600, color: '#8b949e', marginBottom: 6 }}>BEST SITE — POLYGON</div>
      <div style={{ fontSize: 11, color: '#444d56', marginBottom: 8 }}>
        Draw a polygon. Grid-sample TX locations within it and find the best.
      </div>
      <div style={{ display: 'flex', gap: 6, marginBottom: 8 }}>
        <button className={`btn ${drawMode === 'polygon' ? 'btn-primary' : 'btn-secondary'}`} style={{ flex: 1, fontSize: 11, gap: 4 }} onClick={onToggleDraw}>
          <Hexagon size={11} />
          {drawMode === 'polygon' ? 'Click to close polygon' : 'Draw Polygon'}
        </button>
        {polygonCoords.length > 0 && (
          <button className="btn btn-ghost" style={{ fontSize: 11, color: '#ef4444' }} onClick={onClearPolygon}>Clear</button>
        )}
      </div>
      {polygonCoords.length > 0 && (
        <div style={{ fontSize: 11, color: '#8b949e', marginBottom: 8 }}>{polygonCoords.length} vertices</div>
      )}
      <div style={{ marginBottom: 8 }}>
        <label style={{ fontSize: 11, color: '#8b949e', display: 'block', marginBottom: 4 }}>Sample Density: {coveragePct}%</label>
        <input type="range" min={5} max={100} step={5} value={coveragePct} onChange={e => onSetCoveragePct(Number(e.target.value))} style={{ width: '100%' }} />
      </div>
      {result?.sites && (
        <div style={{ padding: 8, background: '#0d1117', borderRadius: 4, border: '1px solid #21262d' }}>
          <div style={{ fontSize: 11, color: '#8b949e', marginBottom: 4 }}>Best of {result.num_candidates} candidates:</div>
          {result.sites.slice(0, 3).map((s, i) => (
            <div key={i} style={{
              fontSize: 11, padding: '2px 0', borderBottom: '1px solid #21262d',
              display: 'flex', justifyContent: 'space-between',
            }}>
              <span style={{ color: i === 0 ? '#06d6a0' : '#c9d1d9' }}>{i + 1}. {s.lat?.toFixed(4)}, {s.lon?.toFixed(4)}</span>
              <span style={{ color: '#8b949e' }}>{s.covered_area_km2} km²</span>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}
