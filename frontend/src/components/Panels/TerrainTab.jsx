import TerrainProfile from '../Charts/TerrainProfile'

/**
 * The "Terrain Profile" bottom-panel tab — the standalone-profile controls (draw a
 * line on the map → terrain cross-section) over the <TerrainProfile> chart, which
 * shows either the standalone profile or the most recent point-to-point sim profile.
 */
export default function TerrainTab({
  terrainLineMode, standaloneProfile, standaloneProfileLoading, standaloneProfileError,
  onToggleLineMode, onClearStandalone,
  terrainProfile, tx, rx, propagationModel, waveType,
}) {
  return (
    <div style={{ flex: 1, minHeight: 0, display: 'flex', flexDirection: 'column' }}>
      {/* Standalone terrain profile controls */}
      <div style={{
        display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap',
        padding: '6px 12px', borderBottom: '1px solid #21262d', background: '#0d1117', flexShrink: 0,
      }}>
        <span style={{ fontSize: 11, color: '#8b949e', fontWeight: 600 }}>Standalone profile:</span>
        <button className={`btn ${terrainLineMode ? 'btn-primary' : 'btn-ghost'}`} style={{ fontSize: 11, padding: '3px 10px' }} onClick={onToggleLineMode}>
          {terrainLineMode ? '✏ Drawing… (right-click to finish)' : '✏ Draw line on map'}
        </button>
        {standaloneProfileLoading && <span style={{ fontSize: 10, color: '#06d6a0' }}>Sampling terrain…</span>}
        {standaloneProfileError && <span style={{ fontSize: 10, color: '#fca5a5' }}>{standaloneProfileError}</span>}
        {standaloneProfile && (
          <>
            <span style={{ fontSize: 10, color: '#8b949e' }}>
              {standaloneProfile.path.length} pts · {(standaloneProfile.totalM / 1000).toFixed(2)} km · src: {standaloneProfile.source}
            </span>
            <button className="btn btn-ghost" style={{ fontSize: 11, padding: '3px 8px', color: '#fca5a5' }} onClick={onClearStandalone}>Clear</button>
          </>
        )}
        <div style={{ flex: 1 }} />
        {terrainProfile && <span style={{ fontSize: 10, color: '#06d6a0' }}>● P2P sim profile loaded</span>}
      </div>
      <div style={{ flex: 1, minHeight: 0 }}>
        {standaloneProfile ? (
          <TerrainProfile
            profile={{ distances_m: standaloneProfile.distances_m, elevations_m: standaloneProfile.elevations_m }}
            standalone
            frequencyHz={0}
          />
        ) : (
          <TerrainProfile
            profile={terrainProfile}
            txHeight={tx.height_m}
            rxHeight={rx.height_m}
            frequencyHz={tx.frequency_hz}
            propagationModel={propagationModel}
            waveType={waveType}
            txLat={tx.lat}
            txLon={tx.lon}
          />
        )}
      </div>
    </div>
  )
}
