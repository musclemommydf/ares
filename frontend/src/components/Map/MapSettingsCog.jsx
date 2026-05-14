/**
 * MapSettingsCog — the ⚙ button + popup on the map's floating toolbar.
 * Holds ALL the map options (no more bottom-panel "Map Options" tab):
 *   Map View (2D/3D) · Distance & altitude units · Coordinate system ·
 *   Compass rose · Map brightness · Coverage render (3D only) · Feature colours.
 *
 * Used by BOTH the 2D Leaflet map (MapView) and the 3D Cesium globe (GlobeView)
 * so the controls are identical in either view. View-mode comes from the shared
 * `useViewMode` store; basemap / feature-colours / coverage-render from
 * `useMapPrefs`; units / coord-system / compass / brightness are passed in from
 * App (which still owns those — they feed lots of other components).
 *
 * The popup is scrollable (maxHeight) so it scales as more options are added.
 */
import { useEffect, useRef, useState } from 'react'
import { Settings } from 'lucide-react'
import { useMapPrefs } from './mapPrefs'
import { coordSystemLabel } from '../../utils/units'

const HDR = { fontSize: 11, fontWeight: 700, color: '#8b949e', marginBottom: 8, textTransform: 'uppercase', letterSpacing: 0.8 }
const SECTION = { marginBottom: 14 }

export default function MapSettingsCog({
  kind = '2d',                                  // '2d' | '3d' — only changes which extras show
  distUnit, setDistUnit,
  coordSystem, setCoordSystem,
  showCompassRose, setShowCompassRose,
  mapBrightness, setMapBrightness,
  trigger,                                       // optional (open, toggle) => ReactNode for the ⚙ button
}) {
  const mapColors = useMapPrefs((s) => s.mapColors)
  const setMapColors = useMapPrefs((s) => s.setMapColors)
  const resetMapColors = useMapPrefs((s) => s.resetMapColors)
  const coverageMode = useMapPrefs((s) => s.coverageMode)
  const setCoverageMode = useMapPrefs((s) => s.setCoverageMode)
  const nightVision = useMapPrefs((s) => s.nightVision)
  const setNightVision = useMapPrefs((s) => s.setNightVision)

  const [open, setOpen] = useState(false)
  const [anchor, setAnchor] = useState(null)   // {top,right} viewport coords below the trigger — popup is position:fixed so .map-container's overflow:hidden can't clip it
  const ref = useRef(null)
  const place = () => {
    const r = ref.current?.getBoundingClientRect()
    if (r) setAnchor({ top: Math.round(r.bottom + 4), right: Math.round(Math.max(8, window.innerWidth - r.right)) })
  }
  useEffect(() => {
    if (!open) return
    place()
    const onDoc = (e) => { if (ref.current && !ref.current.contains(e.target)) setOpen(false) }
    const onWin = () => place()
    document.addEventListener('mousedown', onDoc)
    window.addEventListener('resize', onWin)
    window.addEventListener('scroll', onWin, true)
    return () => { document.removeEventListener('mousedown', onDoc); window.removeEventListener('resize', onWin); window.removeEventListener('scroll', onWin, true) }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open])

  const seg = (active) => ({
    flex: 1, fontSize: 11, padding: '5px 6px', border: 'none', borderRadius: 0, cursor: 'pointer',
    background: active ? '#1f6feb' : '#161b22', color: '#e6edf3',
  })

  const toggle = () => setOpen((o) => !o)
  return (
    <div ref={ref} style={{ position: 'relative' }}>
      {trigger
        ? trigger(open, toggle)
        : (
          <button className={open ? 'btn btn-primary' : 'btn btn-ghost'} style={{ padding: '3px 8px', fontSize: 13 }}
            title="Map settings" onClick={toggle}>
            <Settings size={14} />
          </button>
        )}
      {open && (
        <div onClick={(e) => e.stopPropagation()} style={{
          position: 'fixed', top: (anchor?.top ?? 56), right: (anchor?.right ?? 12), zIndex: 9999,
          background: '#161b22', border: '1px solid #30363d', borderRadius: 8,
          boxShadow: '0 6px 20px rgba(0,0,0,0.7)', padding: '12px 14px',
          width: 260, maxHeight: `calc(100vh - ${(anchor?.top ?? 56) + 14}px)`, overflowY: 'auto',
        }}>
          {/* (the 2D/3D toggle lives on the toolbar, not here) */}
          {/* Coverage render (3D globe only) */}
          {kind === '3d' && (
            <div style={SECTION}>
              <div style={HDR}>Coverage Render</div>
              <div style={{ display: 'flex', overflow: 'hidden', borderRadius: 6, border: '1px solid #30363d' }}>
                {['auto', 'raster', 'points'].map((m) => (
                  <button key={m} onClick={() => setCoverageMode(m)} style={seg(coverageMode === m)}>{m}</button>
                ))}
              </div>
            </div>
          )}

          {/* Units */}
          <div style={SECTION}>
            <div style={HDR}>Distance &amp; Altitude Units</div>
            <div style={{ display: 'flex', overflow: 'hidden', borderRadius: 6, border: '1px solid #30363d' }}>
              {['metric', 'imperial'].map((u) => (
                <button key={u} onClick={() => setDistUnit?.(u)} style={seg(distUnit === u)}>
                  {u === 'metric' ? 'Metric (m/km)' : 'Imperial (ft/mi)'}
                </button>
              ))}
            </div>
          </div>

          {/* Coordinate system */}
          <div style={SECTION}>
            <div style={HDR}>Coordinate System</div>
            <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 6 }}>
              {['latlon', 'latlon_dms', 'mgrs', 'utm'].map((sys) => (
                <button key={sys} className={`btn ${coordSystem === sys ? 'btn-primary' : 'btn-secondary'}`}
                  style={{ fontSize: 11, padding: '5px 4px' }} onClick={() => setCoordSystem?.(sys)}>{coordSystemLabel(sys)}</button>
              ))}
            </div>
          </div>

          {/* Compass rose */}
          <div style={SECTION}>
            <div style={HDR}>Compass Rose</div>
            <label style={{ display: 'flex', alignItems: 'center', gap: 10, cursor: 'pointer' }}>
              <div onClick={() => setShowCompassRose?.((v) => !v)} style={{
                width: 36, height: 20, borderRadius: 10, cursor: 'pointer', flexShrink: 0,
                background: showCompassRose ? 'var(--accent-blue)' : '#30363d', position: 'relative', transition: 'background 0.2s',
              }}>
                <div style={{ position: 'absolute', top: 2, left: showCompassRose ? 18 : 2, width: 16, height: 16, borderRadius: '50%', background: '#fff', transition: 'left 0.2s' }} />
              </div>
              <span style={{ fontSize: 12, color: '#c9d1d9' }}>Show on map</span>
            </label>
          </div>

          {/* Brightness */}
          <div style={SECTION}>
            <div style={HDR}>Map Brightness — {mapBrightness}%</div>
            <input type="range" min={20} max={150} value={mapBrightness} onChange={(e) => setMapBrightness?.(Number(e.target.value))}
              style={{ width: '100%', cursor: 'pointer', accentColor: 'var(--accent-blue)' }} />
            <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: 10, color: '#484f58', marginTop: 2 }}>
              <span>20%</span><span>100%</span><span>150%</span>
            </div>
          </div>

          {/* Night vision */}
          <div style={SECTION}>
            <div style={HDR}>Night Vision</div>
            <label style={{ display: 'flex', alignItems: 'center', gap: 10, cursor: 'pointer' }}>
              <div onClick={() => setNightVision(!nightVision)} style={{
                width: 36, height: 20, borderRadius: 10, cursor: 'pointer', flexShrink: 0,
                background: nightVision ? '#ef4444' : '#30363d', position: 'relative', transition: 'background 0.2s',
              }}>
                <div style={{ position: 'absolute', top: 2, left: nightVision ? 18 : 2, width: 16, height: 16, borderRadius: '50%', background: '#fff', transition: 'left 0.2s' }} />
              </div>
              <span style={{ fontSize: 12, color: '#c9d1d9' }}>Red palette (preserves dark adaptation)</span>
            </label>
          </div>

          {/* Feature colours */}
          <div>
            <div style={{ ...HDR, marginBottom: 10 }}>Feature Colours</div>
            {[['ruler', 'Ruler'], ['emitter', 'Emitter marker'], ['lobCut', 'LoB Cut marker'], ['lobFix', 'LoB Fix marker'], ['draw', 'Draw tools']].map(([key, label]) => (
              <div key={key} style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 8 }}>
                <span style={{ fontSize: 12, color: '#c9d1d9' }}>{label}</span>
                <input type="color" value={mapColors[key]} onChange={(e) => setMapColors((p) => ({ ...p, [key]: e.target.value }))}
                  style={{ width: 32, height: 22, border: '1px solid #30363d', padding: 1, cursor: 'pointer', borderRadius: 3, background: 'none' }} />
              </div>
            ))}
            <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 8 }}>
              <span style={{ fontSize: 12, color: '#c9d1d9' }}>Override LoB lines</span>
              <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
                <input type="checkbox" checked={mapColors.lobLineOverride !== null}
                  onChange={(e) => setMapColors((p) => ({ ...p, lobLineOverride: e.target.checked ? '#f59e0b' : null }))} style={{ cursor: 'pointer' }} />
                {mapColors.lobLineOverride !== null && (
                  <input type="color" value={mapColors.lobLineOverride} onChange={(e) => setMapColors((p) => ({ ...p, lobLineOverride: e.target.value }))}
                    style={{ width: 28, height: 20, border: '1px solid #30363d', padding: 1, cursor: 'pointer', borderRadius: 3, background: 'none' }} />
                )}
              </div>
            </div>
            <button className="btn btn-secondary" style={{ width: '100%', fontSize: 11, marginTop: 2 }} onClick={resetMapColors}>Reset colours</button>
          </div>
        </div>
      )}
    </div>
  )
}
