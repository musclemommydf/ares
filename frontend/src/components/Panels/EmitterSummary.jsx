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
 */
export default function EmitterSummary({ txActive, txLabel, tx, extraTxList, lobs, lobGroups, onRemoveLoB, onEditLoB, onSimulatePropagationFromFix }) {
  const propEmitters = [
    txActive ? { id: 'primary', label: txLabel, lat: tx.lat, lon: tx.lon, freq: tx.frequency_hz, type: 'propagation' } : null,
    ...extraTxList.map(e => ({ id: e.id, label: e.label, lat: e.tx?.lat ?? e.lat, lon: e.tx?.lon ?? e.lon, freq: e.tx?.frequency_hz ?? e.frequency_hz, type: 'propagation' })),
  ].filter(Boolean)
  const geoEmitters = lobGroups.filter(g => g.lobs.length >= 2).map(grp => {
    const inters = computeGroupIntersections(grp)
    const centroid = computeCentroid(inters)
    return { grp, inters, centroid, rms: rmsM(inters, centroid) }
  })
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
        {propEmitters.map(e => (
          <div key={e.id} style={{ background: '#0d1117', border: '1px solid #21262d', borderLeft: '3px solid #00b4d8', borderRadius: 4, padding: '7px 10px', marginBottom: 6 }}>
            <div style={{ fontSize: 12, fontWeight: 700, color: '#00b4d8' }}>{e.label}</div>
            <div style={{ fontSize: 10, color: '#8b949e' }}>{e.lat?.toFixed(5)}, {e.lon?.toFixed(5)}</div>
            {e.freq && <div style={{ fontSize: 10, color: '#484f58' }}>{(e.freq / 1e6).toFixed(3)} MHz</div>}
          </div>
        ))}
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
        <div style={{ fontSize: 10, fontWeight: 700, color: '#a78bfa', marginBottom: 8, textTransform: 'uppercase', letterSpacing: 0.8 }}>
          Geolocated Emitters ({geoEmitters.length})
        </div>
        {geoEmitters.length === 0 && <div style={{ fontSize: 11, color: '#484f58' }}>No cuts or fixes yet (need ≥2 LoBs)</div>}
        {geoEmitters.map(({ grp, centroid, rms }, i) => {
          const isFix = grp.lobs.length >= 3
          const color = isFix ? '#ef4444' : '#06d6a0'
          const avgConf = Math.round(grp.lobs.reduce((s, l) => s + l.confidence_pct, 0) / grp.lobs.length)
          return (
            <div key={i} style={{ background: '#0d1117', border: `1px solid ${color}30`, borderLeft: `3px solid ${color}`, borderRadius: 4, padding: '7px 10px', marginBottom: 6 }}>
              <div style={{ display: 'flex', justifyContent: 'space-between' }}>
                <span style={{ fontSize: 12, fontWeight: 700, color }}>{isFix ? 'FIX' : 'CUT'} · {(grp.frequency_hz / 1e6).toFixed(3)} MHz</span>
                <span style={{ fontSize: 10, color: '#8b949e' }}>{grp.lobs.length} LoBs</span>
              </div>
              {grp.device_id && (
                <div style={{ fontSize: 10, color: '#a78bfa', marginTop: 2 }}>{DEVICE_LABELS[grp.device_type] || 'ID'}: {grp.device_id}</div>
              )}
              {centroid
                ? <div style={{ fontSize: 10, color: '#8b949e', marginTop: 2 }}>Location: {centroid.lat.toFixed(5)}, {centroid.lon.toFixed(5)}</div>
                : <div style={{ fontSize: 10, color: '#ef4444', marginTop: 2 }}>No intersection (parallel bearings?)</div>}
              {rms != null && <div style={{ fontSize: 10, color: '#484f58' }}>Location accuracy: {fmtM(rms)} RMS</div>}
              <div style={{ fontSize: 10, color: '#484f58' }}>Mean confidence: {avgConf}%</div>
              {onSimulatePropagationFromFix && centroid && (
                <button
                  type="button"
                  onClick={() => onSimulatePropagationFromFix({
                    frequency_hz: grp.frequency_hz,
                    device_id: grp.device_id || '',
                    device_type: grp.device_type || '',
                    n_lobs: grp.lobs.length,
                    kind: isFix ? 'fix' : 'cut',
                  }, centroid.lat, centroid.lon)}
                  style={{
                    marginTop: 6, width: '100%', padding: '4px 6px', fontSize: 11,
                    background: '#0d2438', color: '#7dd3fc', border: '1px solid #1e3a5f',
                    borderRadius: 4, cursor: 'pointer',
                  }}>
                  📡 Simulate propagation
                </button>
              )}
            </div>
          )
        })}
      </div>
    </div>
  )
}
