import { Route } from 'lucide-react'

/**
 * The sidebar control for the Route tab: draw / clear the route polyline, the
 * waypoint count, and the fixed P2P receiver point (each route waypoint is tested
 * against it; defaults to the TX position). App owns the route state + draw mode.
 */
export default function RouteSidebar({ drawMode, waypoints, receiverPoint, onToggleDraw, onClearWaypoints, onClearReceiver }) {
  return (
    <div style={{ borderTop: '1px solid #21262d', padding: '8px 12px' }}>
      <div style={{ fontSize: 11, fontWeight: 600, color: '#8b949e', marginBottom: 6 }}>ROUTE ANALYSIS</div>
      <div style={{ fontSize: 11, color: '#444d56', marginBottom: 8 }}>
        Draw a polyline on the map. Each waypoint is tested against a fixed receiver.
      </div>
      <div style={{ display: 'flex', gap: 6, marginBottom: 8 }}>
        <button className={`btn ${drawMode === 'route' ? 'btn-primary' : 'btn-secondary'}`} style={{ flex: 1, fontSize: 11, gap: 4 }} onClick={onToggleDraw}>
          <Route size={11} />
          {drawMode === 'route' ? 'Drawing… (right-click to finish)' : 'Draw Route'}
        </button>
        {waypoints.length > 0 && (
          <button className="btn btn-ghost" style={{ fontSize: 11, color: '#ef4444' }} onClick={onClearWaypoints}>Clear</button>
        )}
      </div>
      {waypoints.length > 0 && (
        <div style={{ fontSize: 11, color: '#8b949e', marginBottom: 6 }}>{waypoints.length} waypoints drawn</div>
      )}
      <div style={{ fontSize: 11, color: '#8b949e', marginBottom: 4 }}>Fixed receiver (P2P target):</div>
      {receiverPoint ? (
        <div style={{ fontSize: 11, color: '#06d6a0', marginBottom: 6 }}>
          {receiverPoint.lat.toFixed(4)}, {receiverPoint.lon.toFixed(4)}
          <button className="btn btn-ghost" style={{ marginLeft: 8, fontSize: 10, padding: '1px 4px', color: '#ef4444' }} onClick={onClearReceiver}>×</button>
        </div>
      ) : (
        <div style={{ fontSize: 11, color: '#444d56', marginBottom: 6 }}>
          Click map in P2P mode to set receiver, or defaults to TX position.
        </div>
      )}
    </div>
  )
}
