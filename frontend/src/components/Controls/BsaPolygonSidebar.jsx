import { Hexagon } from 'lucide-react'

/**
 * Sidebar inputs for the Best-Site-Polygon tab: draw / clear the polygon, and the grid-sample-
 * density slider. The *result* — the top candidate sites — is rendered in the bottom Results tab
 * (see <AnalysisResults>), not here.
 */
export default function BsaPolygonSidebar({ drawMode, polygonCoords, coveragePct, onToggleDraw, onClearPolygon, onSetCoveragePct }) {
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
      <div style={{ fontSize: 10, color: '#484f58' }}>Run Simulation → top candidates show in the bottom <strong>Results</strong> tab.</div>
    </div>
  )
}
