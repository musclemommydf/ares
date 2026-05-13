/**
 * Sidebar input for the Best-Server tab: the query point (which the tool tests against your TX
 * sites — the extra-TX list). The *result* — the ranked servers — is rendered in the bottom
 * Results tab (see <AnalysisResults>), not here.
 */
export default function BestServerSidebar({ query, onClearQuery }) {
  return (
    <div style={{ borderTop: '1px solid #21262d', padding: '8px 12px' }}>
      <div style={{ fontSize: 11, fontWeight: 600, color: '#8b949e', marginBottom: 6 }}>BEST SERVER TOOL</div>
      <div style={{ fontSize: 11, color: '#444d56', marginBottom: 8 }}>
        Click a query point — the tool finds which of your TX sites serves it best.
        Uses the extra TX list as candidate sites.
      </div>
      {query ? (
        <div style={{ fontSize: 11, color: '#06d6a0', marginBottom: 4 }}>
          Query: {query.lat.toFixed(4)}, {query.lon.toFixed(4)}
          <button className="btn btn-ghost" style={{ marginLeft: 8, fontSize: 10, padding: '1px 4px', color: '#ef4444' }} onClick={onClearQuery}>×</button>
        </div>
      ) : (
        <div style={{ fontSize: 11, color: '#444d56', marginBottom: 4 }}>Click the map to set the query point.</div>
      )}
      <div style={{ fontSize: 10, color: '#484f58' }}>Run Simulation → ranking shows in the bottom <strong>Results</strong> tab.</div>
    </div>
  )
}
