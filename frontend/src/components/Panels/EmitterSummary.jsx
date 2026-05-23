import { computeGroupIntersections, computeCentroid } from '../Geolocation/LoBUtils'
import LoBList from '../Geolocation/LoBList'

const DEVICE_LABELS = { dmr: 'DMR', imei: 'IMEI', imsi: 'IMSI', mac: 'MAC', callsign: 'Callsign', other: 'ID' }
const fmtM = m => (m >= 1000 ? `~${(m / 1000).toFixed(1)} km` : `~${Math.round(m)} m`)

function rmsM(inters, centroid) {
  if (!centroid || inters.length === 0) return null
  const mpdLon = 111320 * Math.cos((centroid.lat * Math.PI) / 180)
  const dists = inters.map(p => Math.sqrt(((p.lat - centroid.lat) * 111320) ** 2 + ((p.lon - centroid.lon) * mpdLon) ** 2))
  return Math.sqrt(dists.reduce((s, d) => s + d * d, 0) / dists.length)
}

/**
 * The "Emitter Summary" bottom-panel tab: propagation emitters (the primary TX +
 * any extras), the lines-of-bearing list, and the geolocated emitters (Cuts/Fixes
 * from groups of ≥2 LoBs, with the estimated location and RMS).
 *
 * Each propagation emitter has an "Edit" affordance — clicking it opens the
 * sidebar (if collapsed) and expands + scrolls to the matching TransmitterPanel,
 * so the full parameter form is the single source of truth for editing.
 */
export default function EmitterSummary({ txActive, txLabel, tx, extraTxList, lobs, lobGroups, onRemoveLoB, onEditLoB, onEditEmitter, onDeleteEmitter, onDeleteGeoEmitter, onDismissLiveFix, onSimulatePropagationFromFix, onToggleGeoAutoCoverage, isGeoAutoCovered, onInterference, onSuperLayer, isSimulating = false, autoCoverage, onToggleAutoCoverage, sdrFixes = [] }) {
  const propEmitters = [
    txActive ? {
      id: 'primary', label: txLabel,
      lat: tx.lat, lon: tx.lon, freq: tx.frequency_hz,
      type: 'propagation', color: '#00b4d8',
    } : null,
    ...extraTxList.map(e => ({
      id: e.id, label: e.label,
      lat: e.tx?.lat ?? e.lat, lon: e.tx?.lon ?? e.lon, freq: e.tx?.frequency_hz ?? e.frequency_hz,
      type: 'propagation', color: e.color || '#00b4d8',
    })),
  ].filter(Boolean)
  const geoEmitters = lobGroups.filter(g => g.lobs.length >= 2).map(grp => {
    const inters = computeGroupIntersections(grp)
    const centroid = computeCentroid(inters)
    return { grp, inters, centroid, rms: rmsM(inters, centroid) }
  })
  // Live Cuts/Fixes streaming from DF hardware (KrakenSDR, a live-DF Pluto, …),
  // lifted from the SDR console. Already solved server-side — just render + offer
  // the same "Simulate propagation" action.
  const liveFixes = (sdrFixes || []).filter(f => f?.centroid)
  return (
    <div style={{
      padding: '12px 16px', display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(220px, 1fr))',
      gap: 16, overflowY: 'auto', flex: 1, minHeight: 0, alignContent: 'start',
    }}>
      {/* Propagation emitters */}
      <div style={{ minWidth: 0 }}>
        <div style={{ fontSize: 10, fontWeight: 700, color: '#00b4d8', marginBottom: 8, textTransform: 'uppercase', letterSpacing: 0.8 }}>
          Propagation Emitters ({propEmitters.length})
        </div>
        {propEmitters.length === 0 && <div style={{ fontSize: 11, color: '#484f58' }}>No emitter placed</div>}
        {propEmitters.map(e => {
          const color = e.color || '#00b4d8'
          return (
            <div key={e.id} style={{ background: '#0d1117', border: `1px solid ${color}30`, borderLeft: `3px solid ${color}`, borderRadius: 4, padding: '7px 10px', marginBottom: 6 }}>
              <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', gap: 6 }}>
                <div style={{ fontSize: 12, fontWeight: 700, color, minWidth: 0, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{e.label}</div>
                <div style={{ display: 'flex', gap: 4, flexShrink: 0 }}>
                  {onEditEmitter && (
                    <button
                      type="button"
                      onClick={() => onEditEmitter(e.id)}
                      title="Open this emitter's parameters in the left sidebar"
                      style={{
                        padding: '2px 7px', fontSize: 10, background: 'transparent',
                        color: '#8b949e', border: '1px solid #21262d', borderRadius: 3, cursor: 'pointer',
                      }}>
                      Edit
                    </button>
                  )}
                  {onDeleteEmitter && (
                    <button
                      type="button"
                      onClick={() => onDeleteEmitter(e.id)}
                      title="Delete this propagation emitter"
                      style={{
                        padding: '2px 6px', fontSize: 11, lineHeight: 1, background: 'transparent',
                        color: '#fca5a5', border: '1px solid #3f1d1d', borderRadius: 3, cursor: 'pointer',
                      }}>
                      ×
                    </button>
                  )}
                </div>
              </div>
              <div style={{ fontSize: 10, color: '#8b949e' }}>{e.lat?.toFixed(5)}, {e.lon?.toFixed(5)}</div>
              {e.freq && <div style={{ fontSize: 10, color: '#484f58' }}>{(e.freq / 1e6).toFixed(3)} MHz</div>}
            </div>
          )
        })}
        {/* Layer-combination analyses (moved here from the header menu). Both
            operate on the computed coverage layers across these emitters. */}
        {(onInterference || onSuperLayer) && (
          <div style={{ marginTop: 4, display: 'flex', flexDirection: 'column', gap: 6 }}>
            {onInterference && (
              <button type="button" disabled={isSimulating} onClick={onInterference}
                title="Compute SNR between the first two TX coverage layers"
                style={{ padding: '5px 8px', fontSize: 11, textAlign: 'left',
                  background: '#1a1233', color: '#c4b5fd', border: '1px solid #5b21b6',
                  borderRadius: 4, cursor: isSimulating ? 'not-allowed' : 'pointer', opacity: isSimulating ? 0.5 : 1 }}>
                ▤ Interference Analysis
              </button>
            )}
            {onSuperLayer && (
              <button type="button" disabled={isSimulating} onClick={onSuperLayer}
                title="Merge all coverage layers into a single best-signal layer"
                style={{ padding: '5px 8px', fontSize: 11, textAlign: 'left',
                  background: '#06281f', color: '#6ee7b7', border: '1px solid #0f766e',
                  borderRadius: 4, cursor: isSimulating ? 'not-allowed' : 'pointer', opacity: isSimulating ? 0.5 : 1 }}>
                ⌥ Super Layer
              </button>
            )}
          </div>
        )}
      </div>
      {/* Lines of bearing */}
      <div style={{ minWidth: 0 }}>
        <div style={{ fontSize: 10, fontWeight: 700, color: '#a78bfa', marginBottom: 8, textTransform: 'uppercase', letterSpacing: 0.8 }}>
          Lines of Bearing ({lobs.length})
        </div>
        <LoBList lobs={lobs} onRemoveLoB={onRemoveLoB} onEditLoB={onEditLoB} emptyHint="No bearings recorded yet" />
      </div>
      {/* Geolocated emitters */}
      <div style={{ minWidth: 0 }}>
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 8, gap: 8 }}>
          <div style={{ fontSize: 10, fontWeight: 700, color: '#a78bfa', textTransform: 'uppercase', letterSpacing: 0.8 }}>
            Geolocated Emitters ({geoEmitters.length + liveFixes.length})
          </div>
          {onToggleAutoCoverage && (
            <label title="Automatically run a propagation/coverage simulation from every geolocated emitter, updating as its fix moves"
                   style={{ display: 'inline-flex', alignItems: 'center', gap: 4, fontSize: 10, color: autoCoverage ? '#7dd3fc' : '#6e7681', cursor: 'pointer', whiteSpace: 'nowrap' }}>
              <input type="checkbox" checked={!!autoCoverage} onChange={(e) => onToggleAutoCoverage(e.target.checked)} />
              auto-coverage
            </label>
          )}
        </div>
        {geoEmitters.length === 0 && liveFixes.length === 0 && <div style={{ fontSize: 11, color: '#484f58' }}>No cuts or fixes yet (need ≥2 LoBs)</div>}
        {liveFixes.map((fx, i) => {
          const isFix = (fx.kind || 'fix') === 'fix'
          const color = isFix ? '#ef4444' : '#06d6a0'
          const cep = fx.cep?.cep_m ?? fx.cep?.radius_m ?? (typeof fx.cep === 'number' ? fx.cep : null)
          return (
            <div key={`sdr-${i}`} style={{ background: '#0d1117', border: `1px solid ${color}30`, borderLeft: `3px solid ${color}`, borderRadius: 4, padding: '7px 10px', marginBottom: 6 }}>
              <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', gap: 6 }}>
                <span style={{ fontSize: 12, fontWeight: 700, color }}>{isFix ? 'FIX' : 'CUT'} · {(fx.frequency_hz / 1e6).toFixed(3)} MHz</span>
                <div style={{ display: 'flex', alignItems: 'center', gap: 6, flexShrink: 0 }}>
                  <span style={{ fontSize: 9, color: '#7dd3fc', border: '1px solid #1e3a5f', borderRadius: 3, padding: '0 4px' }}>LIVE SDR</span>
                  {onDismissLiveFix && (
                    <button type="button" onClick={() => onDismissLiveFix({ frequency_hz: fx.frequency_hz, device_id: '' })}
                      title="Delete this live fix from the table (dismiss until cleared)"
                      style={{ padding: '2px 6px', fontSize: 11, lineHeight: 1, background: 'transparent',
                        color: '#fca5a5', border: '1px solid #3f1d1d', borderRadius: 3, cursor: 'pointer' }}>×</button>
                  )}
                </div>
              </div>
              <div style={{ fontSize: 10, color: '#8b949e', marginTop: 2 }}>Location: {fx.centroid.lat.toFixed(5)}, {fx.centroid.lon.toFixed(5)}</div>
              {cep != null && <div style={{ fontSize: 10, color: '#484f58' }}>CEP: {fmtM(cep)}{fx.n_lobs ? ` · ${fx.n_lobs} LoBs` : ''}</div>}
              {(() => {
                const summary = { frequency_hz: fx.frequency_hz, device_id: '', device_type: 'sdr',
                                  n_lobs: fx.n_lobs, kind: fx.kind || 'fix' }
                const auto = isGeoAutoCovered?.(summary)
                return (
                  <div style={{ display: 'flex', gap: 4, marginTop: 6 }}>
                    {onSimulatePropagationFromFix && (
                      <button type="button"
                        onClick={() => onSimulatePropagationFromFix(summary, fx.centroid.lat, fx.centroid.lon)}
                        style={{ flex: 1, padding: '4px 6px', fontSize: 11,
                          background: '#0d2438', color: '#7dd3fc', border: '1px solid #1e3a5f', borderRadius: 4, cursor: 'pointer' }}>
                        📡 Simulate
                      </button>
                    )}
                    {onToggleGeoAutoCoverage && (
                      <button type="button" onClick={() => onToggleGeoAutoCoverage(summary)}
                        title="Auto-run coverage from this emitter and keep it updated as the fix moves"
                        style={{ flexShrink: 0, padding: '4px 8px', fontSize: 11,
                          background: auto ? '#0d2f24' : 'transparent', color: auto ? '#6ee7b7' : '#6e7681',
                          border: `1px solid ${auto ? '#0f766e' : '#21262d'}`, borderRadius: 4, cursor: 'pointer' }}>
                        {auto ? '🔄 auto ✓' : '🔄 auto'}
                      </button>
                    )}
                  </div>
                )
              })()}
            </div>
          )
        })}
        {geoEmitters.map(({ grp, centroid, rms }, i) => {
          const isFix = grp.lobs.length >= 3
          const color = isFix ? '#ef4444' : '#06d6a0'
          const avgConf = Math.round(grp.lobs.reduce((s, l) => s + l.confidence_pct, 0) / grp.lobs.length)
          return (
            <div key={i} style={{ background: '#0d1117', border: `1px solid ${color}30`, borderLeft: `3px solid ${color}`, borderRadius: 4, padding: '7px 10px', marginBottom: 6 }}>
              <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', gap: 6 }}>
                <span style={{ fontSize: 12, fontWeight: 700, color }}>{isFix ? 'FIX' : 'CUT'} · {(grp.frequency_hz / 1e6).toFixed(3)} MHz</span>
                <div style={{ display: 'flex', alignItems: 'center', gap: 6, flexShrink: 0 }}>
                  <span style={{ fontSize: 10, color: '#8b949e' }}>{grp.lobs.length} LoBs</span>
                  {onDeleteGeoEmitter && (
                    <button type="button" onClick={() => onDeleteGeoEmitter(grp)}
                      title="Delete this geolocated emitter (removes its lines of bearing)"
                      style={{ padding: '2px 6px', fontSize: 11, lineHeight: 1, background: 'transparent',
                        color: '#fca5a5', border: '1px solid #3f1d1d', borderRadius: 3, cursor: 'pointer' }}>×</button>
                  )}
                </div>
              </div>
              {grp.device_id && (
                <div style={{ fontSize: 10, color: '#a78bfa', marginTop: 2 }}>{DEVICE_LABELS[grp.device_type] || 'ID'}: {grp.device_id}</div>
              )}
              {centroid
                ? <div style={{ fontSize: 10, color: '#8b949e', marginTop: 2 }}>Location: {centroid.lat.toFixed(5)}, {centroid.lon.toFixed(5)}</div>
                : <div style={{ fontSize: 10, color: '#ef4444', marginTop: 2 }}>No intersection (parallel bearings?)</div>}
              {rms != null && <div style={{ fontSize: 10, color: '#484f58' }}>Location accuracy: {fmtM(rms)} RMS</div>}
              <div style={{ fontSize: 10, color: '#484f58' }}>Mean confidence: {avgConf}%</div>
              {centroid && (() => {
                const summary = {
                  frequency_hz: grp.frequency_hz, device_id: grp.device_id || '',
                  device_type: grp.device_type || '', n_lobs: grp.lobs.length,
                  kind: isFix ? 'fix' : 'cut',
                }
                const auto = isGeoAutoCovered?.(summary)
                return (
                  <div style={{ display: 'flex', gap: 4, marginTop: 6 }}>
                    {onSimulatePropagationFromFix && (
                      <button type="button"
                        onClick={() => onSimulatePropagationFromFix(summary, centroid.lat, centroid.lon)}
                        style={{ flex: 1, padding: '4px 6px', fontSize: 11,
                          background: '#0d2438', color: '#7dd3fc', border: '1px solid #1e3a5f',
                          borderRadius: 4, cursor: 'pointer' }}>
                        📡 Simulate
                      </button>
                    )}
                    {onToggleGeoAutoCoverage && (
                      <button type="button"
                        onClick={() => onToggleGeoAutoCoverage(summary)}
                        title="Auto-run coverage from this emitter and keep it updated as the fix moves"
                        style={{ flexShrink: 0, padding: '4px 8px', fontSize: 11,
                          background: auto ? '#0d2f24' : 'transparent',
                          color: auto ? '#6ee7b7' : '#6e7681',
                          border: `1px solid ${auto ? '#0f766e' : '#21262d'}`,
                          borderRadius: 4, cursor: 'pointer' }}>
                        {auto ? '🔄 auto ✓' : '🔄 auto'}
                      </button>
                    )}
                  </div>
                )
              })()}
            </div>
          )
        })}
      </div>
    </div>
  )
}
