/**
 * Propagation model and simulation parameters panel.
 */
import { useState, useRef } from 'react'
import { createPortal } from 'react-dom'
import { Activity, ChevronDown, ChevronUp } from 'lucide-react'

const KM_PER_MILE = 1.60934

const WAVE_TYPES = [
  {
    id: 'auto',
    name: 'Auto',
    desc: 'System selects based on frequency and geometry',
  },
  {
    id: 'los',
    name: 'Line-of-Sight (LOS)',
    desc: 'Direct path only — blocks anything beyond radio horizon',
    freqHint: 'All bands',
    rangeHint: '< horizon (~4.1·(√hTX + √hRX) km)',
  },
  {
    id: 'ground_wave',
    name: 'Ground Wave',
    desc: 'Surface wave following Earth\'s curvature — ground conductivity dependent',
    freqHint: 'LF / MF / lower HF (< 10 MHz)',
    rangeHint: 'Tens–hundreds km depending on ground type',
  },
  {
    id: 'skywave',
    name: 'Skywave (Ionospheric)',
    desc: 'Ionospheric reflection via F2 layer — long-distance HF skip propagation',
    freqHint: 'HF only (3–30 MHz)',
    rangeHint: '500–4 000 km per hop; skip zone < ~300 km',
  },
  {
    id: 'troposcatter',
    name: 'Troposcatter',
    desc: 'Tropospheric scatter beyond the horizon — high TX power and directional antennas needed',
    freqHint: 'UHF / SHF (100 MHz – 10 GHz)',
    rangeHint: '100–2 000 km',
  },
]

const MODELS = [
  { id: 'auto',         name: 'Auto-Assign',              desc: 'Best model for current parameters', hasContext: false },
  { id: 'itm',          name: 'Longley-Rice ITM',         desc: 'Terrain-based (recommended)', hasContext: false },
  { id: 'fspl',         name: 'Free Space',               desc: 'LOS only, no terrain', hasContext: false },
  { id: 'hata_urban',   name: 'Okumura-Hata Urban',       desc: '150–1500 MHz urban', hasContext: false },
  { id: 'hata_suburban',name: 'Okumura-Hata Suburban',    desc: '150–1500 MHz suburban', hasContext: false },
  { id: 'hata_rural',   name: 'Okumura-Hata Rural/Open',  desc: '150–1500 MHz rural', hasContext: false },
  { id: 'cost231_hata', name: 'COST-231 Hata',            desc: '1.5–2 GHz urban/suburban', hasContext: true,
    ctx: ['Urban (large city, +3 dB)', 'Suburban / average', 'Rural / open'] },
  { id: 'cost231_wi',   name: 'COST-231 Walfisch-Ikegami', desc: '0.8–2 GHz, rooftop/street diffraction', hasContext: true,
    ctx: ['Metropolitan centre', 'Medium city / suburban', 'Medium city / suburban'] },
  { id: 'ecc33',        name: 'ECC-33 (Hata-Okumura ext.)', desc: '0.7–3.5 GHz fixed wireless', hasContext: true,
    ctx: ['Large city', 'Medium city / suburban', 'Medium city / suburban'] },
  { id: 'itu_p452',     name: 'ITU-R P.452 (interference)', desc: 'Clear-air: FSPL + gas + diffraction', hasContext: false },
  { id: 'ericsson',     name: 'Ericsson 9999',             desc: '150–1900 MHz, configurable', hasContext: true,
    ctx: ['Urban / conservative', 'Suburban / average', 'Rural / optimistic'] },
  { id: 'itu_p1546',    name: 'ITU-R P.1546',             desc: '30–3000 MHz, P-to-area', hasContext: true,
    ctx: ['Urban', 'Suburban', 'Rural / open'] },
  { id: 'sui',          name: 'SUI (WiMAX)',               desc: '2–11 GHz', hasContext: true,
    ctx: ['Terrain A — hilly/moderate veg.', 'Terrain B — intermediate', 'Terrain C — flat/light veg.'] },
  { id: 'nvis_hf',      name: 'HF NVIS',                  desc: '2–30 MHz, near-vertical ionospheric', hasContext: true,
    ctx: ['D layer (< 5 MHz, day, high absorption)', 'E layer (5–10 MHz, day)', 'F layer (5–30 MHz, day/night)'],
    isNvis: true },
  { id: 'radar',        name: 'Radar (two-way)',           desc: 'Target detection — requires RCS', hasContext: false },
  { id: 'two_ray',      name: 'Two-Ray Ground',           desc: 'Ground reflection', hasContext: false },
  { id: 'itu_p528',     name: 'ITU-R P.528 (Aero)',       desc: 'Air-ground, up to 30k ft', hasContext: false },
  { id: 'egli',         name: 'Egli',                     desc: 'Rural empirical (40–900 MHz)', hasContext: false },
  { id: 'plane_earth',  name: 'Plane Earth',              desc: '4th power law', hasContext: false },
]

const DIFFRACTION_MODELS = [
  { id: 'none',               name: 'None', desc: 'No diffraction correction' },
  { id: 'single_knife_edge',  name: 'Single Knife Edge', desc: 'Basic Huygens formula — single worst obstacle. Optimistic.' },
  { id: 'epstein_peterson',   name: 'Epstein-Peterson \'53', desc: 'Sequential multi-obstacle model. Can be conservative.' },
  { id: 'bullington',         name: 'Bullington \'77', desc: 'Fast multi-obstacle approximation. Good speed/accuracy trade-off. Recommended for VHF/UHF.' },
  { id: 'giovanelli',         name: 'Giovanelli \'84', desc: 'Multi-obstacle with combining factor. Good accuracy.' },
  { id: 'deygout',            name: 'Deygout \'94', desc: 'Priority-based multi-obstacle. Most accurate. CloudRF default.' },
]

const TOOLTIPS = {
  model: 'The mathematical model used to estimate path loss. ITM (Longley-Rice) is the most accurate for terrain-based links. Auto-Assign picks the best fit based on frequency and geometry.',
  wave_type: 'The physical propagation mechanism. Auto lets the system decide. Skywave uses ionospheric reflection for long-distance HF paths but has a skip zone where no signal arrives. Troposcatter reaches beyond the horizon at UHF via atmospheric scattering.',
  context: 'Environmental context for the model. Code 1 = urban/conservative, 2 = average/suburban, 3 = rural/optimistic. For HF NVIS: 1 = D layer, 2 = E layer, 3 = F layer. Different contexts produce significantly different outputs — pick the one that matches your environment.',
  diffraction: 'Diffraction shows coverage beyond terrain obstacles. The radio shadow size varies by frequency and obstacle distance/height — low frequency means low diffraction angle. Disable for microwave LOS links. Enable for sub-GHz (VHF/UHF-L). Recommended model: Bullington.',
  diffraction_model: 'Bullington (1977) is the recommended default — fast and accurate for most VHF/UHF scenarios. Deygout (1994) is the most accurate (CloudRF default) but slower. Single Knife Edge is optimistic. Epstein-Peterson can be conservative for multiple obstacles.',
  radius: 'How far from the transmitter to simulate. Larger values take longer to compute. For Skywave, this must exceed the skip distance (typically 300–1000 km) or nothing will appear on the map.',
  min_signal: 'The weakest signal level that counts as "covered". Points below this threshold are not drawn on the map. Typical receiver sensitivity is −100 to −120 dBm.',
  radials: 'Number of directions (spokes) swept out from the transmitter. More radials give a smoother, higher-resolution coverage shape but increase computation time linearly.',
  points_per_radial: 'How many distance samples are computed along each radial spoke. Higher values increase detail at long range but also increase compute time.',
  raster: 'Per-pixel raster coverage — runs one path calculation per grid cell instead of sweeping radial spokes. Gives even coverage everywhere (no thinning at range) at the cost of more compute. When on, the Radials / Points-per-radial settings are ignored.',
  terrain_resolution: 'The resolution of the SRTM elevation data used for terrain-aware models. SRTM 30 m is more detailed but slower to download and compute. SRTM 90 m is faster and sufficient for most use cases.',
  space_weather: 'Fetches live solar flux, K-index, and geomagnetic data from NOAA SWPC and applies corrections to HF path loss. Adds a small network delay. Disable for offline use.',
  buildings: 'Downloads OpenStreetMap building footprints and uses them as additional diffraction obstacles. Only meaningful at short ranges (< 5 km) and higher frequencies.',
  gpu: 'Uses CUDA (NVIDIA GPU) to batch-compute path loss in parallel, significantly speeding up large coverage areas. Requires a supported GPU with CuPy installed.',
  clutter: 'Additional height added uniformly to the terrain profile to simulate land cover — trees, low buildings, urban clutter. 0 = bare earth. 5–15 m is typical for forest/suburban. This is a uniform approximation; use OSM buildings for accurate urban clutter.',
  rcs: 'Radar Cross Section in m². Only used with the Radar (two-way) model. Typical values: person ~0.5 m², car ~10 m², aircraft ~5 m², stealth ~0.001 m², ship ~5000 m².',
}

// ── Tooltip component ─────────────────────────────────────────────────────────

const TOOLTIP_WIDTH = 230
const TOOLTIP_MARGIN = 8

function FieldTooltip({ text }) {
  const [pos, setPos] = useState(null)
  const ref = useRef(null)

  const handleMouseEnter = () => {
    if (!ref.current) return
    const rect = ref.current.getBoundingClientRect()
    // Center horizontally on the icon, clamped to viewport
    let x = rect.left + rect.width / 2 - TOOLTIP_WIDTH / 2
    x = Math.max(TOOLTIP_MARGIN, Math.min(x, window.innerWidth - TOOLTIP_WIDTH - TOOLTIP_MARGIN))
    setPos({ x, y: rect.top - 6 })
  }

  return (
    <span
      ref={ref}
      style={{ display: 'inline-flex', marginLeft: 4, verticalAlign: 'middle', cursor: 'help' }}
      onMouseEnter={handleMouseEnter}
      onMouseLeave={() => setPos(null)}
    >
      <span style={{
        width: 13, height: 13, borderRadius: '50%',
        background: '#21262d', border: '1px solid #30363d',
        color: '#8b949e', fontSize: 9, fontWeight: 700,
        display: 'inline-flex', alignItems: 'center', justifyContent: 'center',
        userSelect: 'none', flexShrink: 0,
      }}>?</span>
      {pos && createPortal(
        <div style={{
          position: 'fixed',
          top: pos.y,
          left: pos.x,
          transform: 'translateY(-100%)',
          background: '#1c2128',
          border: '1px solid #30363d',
          borderRadius: 6,
          padding: '7px 10px',
          fontSize: 11,
          color: '#c9d1d9',
          whiteSpace: 'normal',
          width: TOOLTIP_WIDTH,
          zIndex: 99999,
          lineHeight: 1.6,
          pointerEvents: 'none',
          boxShadow: '0 4px 16px rgba(0,0,0,0.5)',
        }}>
          {text}
        </div>,
        document.body
      )}
    </span>
  )
}

// ── Main panel ────────────────────────────────────────────────────────────────

export default function PropagationPanel({
  propagation, setPropagation, resolvedModel, distUnit = 'metric',
  activeTab = 'coverage', coverageRaster = false, onSetRaster,
}) {
  const [open, setOpen] = useState(true)
  const update = (field, value) => setPropagation(prev => ({ ...prev, [field]: value }))

  const selectedWave = WAVE_TYPES.find(w => w.id === propagation.wave_type) || WAVE_TYPES[0]
  const selectedModel = MODELS.find(m => m.id === propagation.model) || MODELS[0]
  const diffractionEnabled = propagation.diffraction_model && propagation.diffraction_model !== 'none'

  const isImperial = distUnit === 'imperial'

  // Radius display: stored as km, shown as miles when imperial
  const radiusDisplay = isImperial
    ? parseFloat((propagation.radius_km / KM_PER_MILE).toFixed(1))
    : propagation.radius_km
  const radiusUnit = isImperial ? 'mi' : 'km'

  const handleRadiusChange = (rawVal) => {
    const parsed = parseFloat(rawVal)
    if (isNaN(parsed)) return
    update('radius_km', isImperial ? parsed * KM_PER_MILE : parsed)
  }

  return (
    <div>
      <button
        className={`accordion-trigger ${open ? 'open' : ''}`}
        onClick={() => setOpen(o => !o)}
      >
        <span style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
          <Activity size={14} color="var(--accent-blue)" />
          Propagation & Coverage
        </span>
        {open ? <ChevronUp size={14} /> : <ChevronDown size={14} />}
      </button>

      {open && (
        <div className="panel-section">
          {/* Model */}
          <div className="field-row single">
            <div className="field">
              <label>
                Propagation Model
                <FieldTooltip text={TOOLTIPS.model} />
              </label>
              <select
                value={propagation.model}
                onChange={e => update('model', e.target.value)}
              >
                {MODELS.map(m => (
                  <option key={m.id} value={m.id}>{m.name} — {m.desc}</option>
                ))}
              </select>
              {propagation.model === 'auto' && resolvedModel && (() => {
                const m = MODELS.find(x => x.id === resolvedModel)
                return (
                  <div style={{ fontSize: 10, color: 'var(--text-muted)', marginTop: 4 }}>
                    Using: {m ? m.name : resolvedModel}
                  </div>
                )
              })()}
            </div>
          </div>

          {/* Context — only shown for models that support it */}
          {selectedModel?.hasContext && (
            <div className="field-row single">
              <div className="field">
                <label>
                  {selectedModel.isNvis ? 'Reflective Layer' : 'Model Context'}
                  <FieldTooltip text={TOOLTIPS.context} />
                </label>
                <select
                  value={propagation.context ?? 2}
                  onChange={e => update('context', parseInt(e.target.value))}
                >
                  {(selectedModel.ctx || ['Urban / conservative', 'Suburban / average', 'Rural / optimistic']).map((label, i) => (
                    <option key={i + 1} value={i + 1}>{i + 1} — {label}</option>
                  ))}
                </select>
              </div>
            </div>
          )}

          {/* Radar RCS — only shown for radar model */}
          {propagation.model === 'radar' && (
            <div className="field-row single">
              <div className="field">
                <label>
                  Radar Cross Section (m²)
                  <FieldTooltip text={TOOLTIPS.rcs} />
                </label>
                <input
                  type="number" min="0.0001" max="100000" step="0.1"
                  value={propagation.rcs_m2 ?? 1.0}
                  onChange={e => update('rcs_m2', parseFloat(e.target.value))}
                />
                <div style={{ fontSize: 10, color: 'var(--text-muted)', marginTop: 3 }}>
                  Person ≈ 0.5 · Car ≈ 10 · Aircraft ≈ 5 · Ship ≈ 5000 · Stealth ≈ 0.001
                </div>
              </div>
            </div>
          )}

          {/* Wave type */}
          <div className="field-row single">
            <div className="field">
              <label>
                Wave Type
                <FieldTooltip text={TOOLTIPS.wave_type} />
              </label>
              <select
                value={propagation.wave_type}
                onChange={e => {
                  const wt = e.target.value
                  update('wave_type', wt)
                  // Skywave skip zone starts at 200–1010 km; bump radius so coverage is visible
                  if (wt === 'skywave' && propagation.radius_km < 500) {
                    update('radius_km', 1500)
                  }
                }}
              >
                {WAVE_TYPES.map(w => (
                  <option key={w.id} value={w.id}>{w.name}</option>
                ))}
              </select>
              {selectedWave.id !== 'auto' && (
                <div style={{ marginTop: 4, fontSize: 10, color: 'var(--text-muted)', lineHeight: 1.5 }}>
                  {selectedWave.desc}
                  {selectedWave.freqHint && (
                    <><br /><span style={{ color: 'var(--accent-blue)' }}>Freq: </span>{selectedWave.freqHint}</>
                  )}
                  {selectedWave.rangeHint && (
                    <><br /><span style={{ color: 'var(--accent-green)' }}>Range: </span>{selectedWave.rangeHint}</>
                  )}
                </div>
              )}
              {selectedWave.id === 'skywave' && propagation.radius_km < 500 && (
                <div style={{ marginTop: 6, fontSize: 10, color: '#f59e0b', lineHeight: 1.5 }}>
                  ⚠ Radius is below the skip zone (~300–1000 km). Increase radius to 500 km+ or nothing will appear on the map.
                </div>
              )}
            </div>
          </div>

          {/* Radius + Min Signal */}
          <div className="field-row">
            <div className="field">
              <label>
                Radius ({radiusUnit})
                <FieldTooltip text={TOOLTIPS.radius} />
              </label>
              <input
                type="number" min="1" max={isImperial ? 1243 : 2000} step={isImperial ? 1 : 1}
                value={radiusDisplay}
                onChange={e => handleRadiusChange(e.target.value)}
              />
            </div>
            <div className="field">
              <label>
                Min Signal (dBm)
                <FieldTooltip text={TOOLTIPS.min_signal} />
              </label>
              <input
                type="number" min="-160" max="0" step="1"
                value={propagation.min_signal_dbm}
                onChange={e => update('min_signal_dbm', parseFloat(e.target.value))}
              />
            </div>
          </div>

          {/* Radials + Points per radial */}
          <div className="field-row">
            <div className="field">
              <label>
                Radials
                <FieldTooltip text={TOOLTIPS.radials} />
              </label>
              <input
                type="number" min="8" max="3600" step="8"
                value={propagation.num_radials}
                onChange={e => update('num_radials', parseInt(e.target.value))}
                disabled={activeTab === 'coverage' && coverageRaster}
              />
            </div>
            <div className="field">
              <label>
                Points / radial
                <FieldTooltip text={TOOLTIPS.points_per_radial} />
              </label>
              <input
                type="number" min="10" max="2000" step="10"
                value={propagation.points_per_radial}
                onChange={e => update('points_per_radial', parseInt(e.target.value))}
                disabled={activeTab === 'coverage' && coverageRaster}
              />
            </div>
          </div>

          {/* Per-pixel raster coverage (coverage tab only) */}
          {activeTab === 'coverage' && (
            <label style={{ display: 'flex', alignItems: 'center', gap: 8, fontSize: 12,
                            cursor: 'pointer', userSelect: 'none', marginTop: 2,
                            color: coverageRaster ? '#06d6a0' : undefined }}>
              <input
                type="checkbox"
                checked={coverageRaster}
                onChange={e => onSetRaster?.(e.target.checked)}
              />
              Per-pixel raster coverage
              <FieldTooltip text={TOOLTIPS.raster} />
            </label>
          )}

          {/* Terrain resolution + Clutter */}
          <div className="field-row">
            <div className="field">
              <label>
                Terrain Resolution
                <FieldTooltip text={TOOLTIPS.terrain_resolution} />
              </label>
              <select
                value={propagation.terrain_resolution}
                onChange={e => update('terrain_resolution', e.target.value)}
              >
                <option value="srtm3">SRTM 90m (fast)</option>
                <option value="srtm1">SRTM 30m (detail)</option>
              </select>
            </div>
            <div className="field">
              <label>
                Clutter Height (m)
                <FieldTooltip text={TOOLTIPS.clutter} />
              </label>
              <input
                type="number" min="0" max="100" step="1"
                value={propagation.clutter_height_m ?? 0}
                onChange={e => update('clutter_height_m', parseFloat(e.target.value) || 0)}
              />
            </div>
          </div>

          {/* Diffraction */}
          <div style={{ borderTop: '1px solid #21262d', paddingTop: 8, marginTop: 4 }}>
            <div style={{ display: 'flex', alignItems: 'center', gap: 6, marginBottom: 6 }}>
              <label style={{ display: 'flex', alignItems: 'center', gap: 8, fontSize: 12,
                              cursor: 'pointer', userSelect: 'none', flex: 1 }}>
                <input
                  type="checkbox"
                  checked={diffractionEnabled}
                  onChange={e => update('diffraction_model', e.target.checked ? 'deygout' : 'none')}
                />
                Terrain Diffraction
                <FieldTooltip text={TOOLTIPS.diffraction} />
              </label>
            </div>
            {diffractionEnabled && (
              <div className="field">
                <label>
                  Diffraction Model
                  <FieldTooltip text={TOOLTIPS.diffraction_model} />
                </label>
                <select
                  value={propagation.diffraction_model}
                  onChange={e => update('diffraction_model', e.target.value)}
                >
                  {DIFFRACTION_MODELS.filter(d => d.id !== 'none').map(d => (
                    <option key={d.id} value={d.id}>{d.name} — {d.desc}</option>
                  ))}
                </select>
                {propagation.model === 'itm' && (
                  <div style={{ fontSize: 10, color: '#f59e0b', marginTop: 4 }}>
                    ITM already includes terrain diffraction. This setting applies to other models only.
                  </div>
                )}
              </div>
            )}
          </div>

          {/* Toggles */}
          <div style={{ display: 'flex', flexDirection: 'column', gap: 6, marginTop: 4 }}>
            <label style={{ display: 'flex', alignItems: 'center', gap: 8,
                            fontSize: 12, cursor: 'pointer', userSelect: 'none' }}>
              <input
                type="checkbox"
                checked={propagation.fetch_space_weather}
                onChange={e => update('fetch_space_weather', e.target.checked)}
              />
              Live space weather corrections (NOAA SWPC)
              <FieldTooltip text={TOOLTIPS.space_weather} />
            </label>

            <div style={{ display: 'flex', flexDirection: 'column', gap: 4,
                          borderTop: '1px solid #21262d', paddingTop: 6, marginTop: 2 }}>
              <label style={{ display: 'flex', alignItems: 'center', gap: 8,
                              fontSize: 12, cursor: 'pointer', userSelect: 'none' }}>
                <input
                  type="checkbox"
                  checked={propagation.include_buildings}
                  onChange={e => update('include_buildings', e.target.checked)}
                />
                OSM building data
                <FieldTooltip text={TOOLTIPS.buildings} />
              </label>
              {propagation.include_buildings && (
                <div style={{ paddingLeft: 22, display: 'flex', flexDirection: 'column', gap: 4 }}>
                  <div>
                    <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: 2 }}>
                      <label style={{ fontSize: 11, color: '#8b949e' }}>Fetch radius</label>
                      <span style={{ fontSize: 11, color: '#c9d1d9' }}>
                        {(propagation.buildings_radius_m ?? 500).toFixed(0)} m
                      </span>
                    </div>
                    <input
                      type="range" min={100} max={5000} step={100}
                      value={propagation.buildings_radius_m ?? 500}
                      onChange={e => update('buildings_radius_m', Number(e.target.value))}
                      style={{ width: '100%' }}
                    />
                    <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: 10, color: '#444d56' }}>
                      <span>100 m</span><span>5 km</span>
                    </div>
                  </div>
                  <label style={{ display: 'flex', alignItems: 'center', gap: 8,
                                  fontSize: 11, cursor: 'pointer', userSelect: 'none' }}>
                    <input
                      type="checkbox"
                      checked={propagation.show_buildings_layer ?? false}
                      onChange={e => update('show_buildings_layer', e.target.checked)}
                    />
                    Show footprints on map
                  </label>
                </div>
              )}
            </div>

            <label style={{ display: 'flex', alignItems: 'center', gap: 8,
                            fontSize: 12, cursor: 'pointer', userSelect: 'none' }}>
              <input
                type="checkbox"
                checked={propagation.use_gpu}
                onChange={e => update('use_gpu', e.target.checked)}
              />
              <span>
                GPU acceleration (CUDA)
                <span style={{ color: 'var(--text-muted)', fontSize: 10, marginLeft: 4 }}>
                  RTX 3070 Ti+
                </span>
              </span>
              <FieldTooltip text={TOOLTIPS.gpu} />
            </label>
          </div>
        </div>
      )}
    </div>
  )
}
