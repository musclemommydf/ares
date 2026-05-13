/**
 * Antenna configuration panel — TX and RX antennas.
 * Includes all antenna presets from the catalogue.
 */
import { useState, useEffect } from 'react'
import { Antenna, ChevronDown, ChevronUp } from 'lucide-react'
import {
  POLAR_PATTERNS,
  patternsByCategory,
  computePatternBeamwidths,
} from '../../utils/polarPatterns'
import { getAntennaPresets } from '../../api/client'

/**
 * Apply a manufacturer antenna preset to an antenna config.
 * Overwrites every field the preset speaks to, leaving everything else
 * intact (so user edits to e.g. azimuth_deg / tilt_deg / dish diameter
 * survive a preset swap).
 */
export function applyAntennaPreset(prevAntenna, preset) {
  if (!preset) return prevAntenna
  return {
    ...prevAntenna,
    type: preset.antenna_type ?? prevAntenna.type,
    gain_dbi: preset.peak_gain_dbi ?? prevAntenna.gain_dbi,
    polarization: preset.polarization ?? prevAntenna.polarization,
    polar_pattern: preset.polar_pattern ?? prevAntenna.polar_pattern ?? 'omni',
    polar_peak_gain_dbi: preset.peak_gain_dbi ?? prevAntenna.polar_peak_gain_dbi,
    preset_id: preset.id,
  }
}

function formatFreqRange(minHz, maxHz) {
  const fmt = (hz) => {
    if (hz >= 1e9) return `${(hz / 1e9).toFixed(hz >= 10e9 ? 0 : 1)} GHz`
    if (hz >= 1e6) return `${(hz / 1e6).toFixed(hz >= 100e6 ? 0 : 1)} MHz`
    if (hz >= 1e3) return `${(hz / 1e3).toFixed(0)} kHz`
    return `${hz.toFixed(0)} Hz`
  }
  return `${fmt(minHz)} – ${fmt(maxHz)}`
}

const ANTENNA_TYPES = [
  { id: 'isotropic',         name: 'Isotropic (0 dBi)',          category: 'Reference' },
  { id: 'dipole_half_wave',  name: 'Half-Wave Dipole (2.15 dBi)', category: 'Dipoles' },
  { id: 'dipole_full_wave',  name: 'Full-Wave Dipole (3.8 dBi)',  category: 'Dipoles' },
  { id: 'dipole_quarter_wave',name: 'Quarter-Wave Monopole (5.19 dBi)', category: 'Dipoles' },
  { id: 'whip_quarter_wave', name: 'Whip Antenna (5.19 dBi)',     category: 'Dipoles' },
  { id: 'ground_plane',      name: 'Ground Plane (5.19 dBi)',     category: 'Dipoles' },
  { id: 'collinear_2el',     name: 'Collinear 2-el (5 dBi)',      category: 'Omni' },
  { id: 'collinear_4el',     name: 'Collinear 4-el (8 dBi)',      category: 'Omni' },
  { id: 'omnidirectional',   name: 'Omnidirectional (2.15 dBi)',  category: 'Omni' },
  { id: 'omni_5dbi',         name: 'Omni 5 dBi',                  category: 'Omni' },
  { id: 'omni_9dbi',         name: 'Omni 9 dBi',                  category: 'Omni' },
  { id: 'yagi_3el',          name: 'Yagi 3-element (8 dBi)',      category: 'Directional' },
  { id: 'yagi_5el',          name: 'Yagi 5-element (10 dBi)',     category: 'Directional' },
  { id: 'yagi_9el',          name: 'Yagi 9-element (14 dBi)',     category: 'Directional' },
  { id: 'yagi_15el',         name: 'Yagi 15-element (17 dBi)',    category: 'Directional' },
  { id: 'log_periodic',      name: 'Log-Periodic (9 dBi)',        category: 'Directional' },
  { id: 'sector_60',         name: 'Sector 60° (18 dBi)',         category: 'Sector' },
  { id: 'sector_90',         name: 'Sector 90° (16 dBi)',         category: 'Sector' },
  { id: 'sector_120',        name: 'Sector 120° (14 dBi)',        category: 'Sector' },
  { id: 'patch',             name: 'Patch / Microstrip (7 dBi)',  category: 'Planar' },
  { id: 'horn',              name: 'Horn Antenna',                category: 'Aperture' },
  { id: 'parabolic_dish',    name: 'Parabolic Dish',              category: 'Aperture' },
  { id: 'helical',           name: 'Helical / Axial Mode',        category: 'Circular Pol' },
  { id: 'crossed_dipole',    name: 'Crossed Dipole (CP)',         category: 'Circular Pol' },
  { id: 'loop',              name: 'Loop Antenna',                category: 'Loop' },
  { id: 'phased_array',      name: 'Phased Array (5G/mmWave)',    category: 'Array' },
  { id: 'custom',            name: 'Custom Pattern (JSON/NEC)',    category: 'Custom' },
]

/**
 * Estimate horizontal beamwidth (degrees) for auto mode display.
 * Mirrors the backend get_antenna_beamwidth() logic.
 */
function estimateBeamwidth(antennaType, frequencyHz, diameterM, arrayElements) {
  const omni = new Set([
    'isotropic', 'omnidirectional', 'omni_5dbi', 'omni_9dbi',
    'dipole_half_wave', 'dipole_full_wave', 'dipole_quarter_wave',
    'whip_quarter_wave', 'crossed_dipole', 'collinear_2el', 'collinear_4el',
    'loop', 'ground_plane',
  ])
  if (omni.has(antennaType)) return null
  const yagiBw = { yagi_3el: 80 / Math.sqrt(3), yagi_5el: 80 / Math.sqrt(5), yagi_9el: 80 / Math.sqrt(9), yagi_15el: 80 / Math.sqrt(15) }
  if (yagiBw[antennaType] !== undefined) return Math.round(yagiBw[antennaType])
  if (antennaType === 'log_periodic') return 60
  if (antennaType === 'sector_60') return 60
  if (antennaType === 'sector_90') return 90
  if (antennaType === 'sector_120') return 120
  if (antennaType === 'parabolic_dish' || antennaType === 'horn') {
    const lam = 3e8 / Math.max(1, frequencyHz)
    const factor = antennaType === 'horn' ? 55 : 70
    return Math.round(factor * lam / Math.max(0.01, diameterM))
  }
  if (antennaType === 'patch') return 80
  if (antennaType === 'helical') return 52
  if (antennaType === 'phased_array') return Math.max(2, Math.round(100 / Math.sqrt(Math.max(1, arrayElements))))
  return null
}

// Antenna types that are omnidirectional in azimuth (full 360° regardless of mode)
const OMNI_TYPES = new Set([
  'isotropic', 'omnidirectional', 'omni_5dbi', 'omni_9dbi',
  'dipole_half_wave', 'dipole_full_wave', 'dipole_quarter_wave',
  'whip_quarter_wave', 'crossed_dipole', 'collinear_2el', 'collinear_4el',
  'loop', 'ground_plane',
])

function AntennaConfig({ label, ant, setAnt, prefix, showBeamControls, frequencyHz, antennaPresets = [] }) {
  const update = (field, val) => setAnt(prev => ({
    ...prev,
    antenna: { ...prev.antenna, [field]: val }
  }))

  const applyPreset = (preset) => {
    setAnt(prev => ({ ...prev, antenna: applyAntennaPreset(prev.antenna, preset) }))
  }

  const showDish = ant.antenna.type === 'parabolic_dish' || ant.antenna.type === 'horn'
  const showYagi = ant.antenna.type.startsWith('yagi')
  const showArray = ant.antenna.type === 'phased_array'
  const showCustom = ant.antenna.type === 'custom'
  const isOmni = OMNI_TYPES.has(ant.antenna.type)
  const autoBw = estimateBeamwidth(ant.antenna.type, frequencyHz, ant.antenna.diameter_m, ant.antenna.array_elements)

  // Group presets by manufacturer for the dropdown
  const presetsByMfg = antennaPresets.reduce((acc, p) => {
    (acc[p.manufacturer] = acc[p.manufacturer] || []).push(p)
    return acc
  }, {})

  return (
    <div style={{ marginBottom: 12 }}>
      <div className="panel-title">{label}</div>

      {antennaPresets.length > 0 && (
        <div className="field-row single">
          <div className="field">
            <label>Antenna preset</label>
            <select
              value={ant.antenna.preset_id ?? ''}
              onChange={e => {
                if (!e.target.value) {
                  // "Custom (no preset)" — clear the preset_id but leave other fields intact
                  update('preset_id', undefined)
                  return
                }
                const p = antennaPresets.find(x => x.id === e.target.value)
                if (p) applyPreset(p)
              }}
              title="Datasheet-backed antenna presets. Selecting one overwrites peak gain, polar pattern, polarization, and antenna type."
            >
              <option value="">— Custom (no preset) —</option>
              {Object.entries(presetsByMfg).map(([mfg, items]) => (
                <optgroup key={mfg} label={mfg}>
                  {items.map(p => (
                    <option key={p.id} value={p.id}>
                      {p.model} · {formatFreqRange(p.freq_min_hz, p.freq_max_hz)} · {p.peak_gain_dbi} dBi
                    </option>
                  ))}
                </optgroup>
              ))}
            </select>
            {ant.antenna.preset_id && (() => {
              const p = antennaPresets.find(x => x.id === ant.antenna.preset_id)
              return p ? (
                <div style={{ fontSize: 10, color: 'var(--text-secondary)', marginTop: 3, lineHeight: 1.4 }}>
                  {p.notes}
                </div>
              ) : null
            })()}
          </div>
        </div>
      )}

      <div className="field-row single">
        <div className="field">
          <label>Antenna Type</label>
          <select
            value={ant.antenna.type}
            onChange={e => update('type', e.target.value)}
          >
            {Object.entries(
              ANTENNA_TYPES.reduce((acc, a) => {
                (acc[a.category] = acc[a.category] || []).push(a)
                return acc
              }, {})
            ).map(([cat, ants]) => (
              <optgroup key={cat} label={cat}>
                {ants.map(a => (
                  <option key={a.id} value={a.id}>{a.name}</option>
                ))}
              </optgroup>
            ))}
          </select>
        </div>
      </div>

      <div className="field-row">
        <div className="field">
          <label>Gain override (dBi)</label>
          <input
            type="number" step="0.1" placeholder="Auto"
            value={ant.antenna.gain_dbi ?? ''}
            onChange={e => update('gain_dbi', e.target.value === '' ? null : parseFloat(e.target.value))}
          />
        </div>
        <div className="field">
          <label title="Mechanical elevation tilt — positive = down, negative = up">Tilt (°)</label>
          <input
            type="number" min="-90" max="90" step="0.5"
            value={ant.antenna.tilt_deg}
            onChange={e => update('tilt_deg', parseFloat(e.target.value))}
          />
        </div>
      </div>

      <div className="field-row">
        <div className="field">
          <label>Azimuth (°)</label>
          <input
            type="number" min="0" max="360" step="1"
            value={ant.antenna.azimuth_deg}
            onChange={e => update('azimuth_deg', parseFloat(e.target.value))}
          />
        </div>
        <div className="field">
          <label>Polarization</label>
          <select
            value={ant.antenna.polarization}
            onChange={e => update('polarization', e.target.value)}
          >
            <option value="vertical">Vertical</option>
            <option value="horizontal">Horizontal</option>
            <option value="circular">Circular (CP)</option>
            <option value="cross">Cross (±45°)</option>
          </select>
        </div>
      </div>

      {showBeamControls && (() => {
        const polarPattern = ant.antenna.polar_pattern ?? 'omni'
        const patternMeta = POLAR_PATTERNS[polarPattern] ?? POLAR_PATTERNS.omni
        const { hpbw3, hpbw6 } = computePatternBeamwidths(polarPattern)
        const groups = patternsByCategory()
        return (
          <>
            <div className="field-row">
              <div className="field">
                <label>Polar pattern</label>
                <select
                  value={polarPattern}
                  onChange={e => update('polar_pattern', e.target.value)}
                  title={patternMeta.description}
                >
                  {Object.entries(groups).map(([cat, items]) => (
                    <optgroup key={cat} label={cat}>
                      {items.map(p => (
                        <option key={p.id} value={p.id}>{p.label}</option>
                      ))}
                    </optgroup>
                  ))}
                </select>
              </div>
              <div className="field">
                <label>Peak gain (dBi)</label>
                <input
                  type="number" step="0.1" placeholder="auto"
                  value={ant.antenna.polar_peak_gain_dbi ?? ''}
                  onChange={e => update('polar_peak_gain_dbi',
                    e.target.value === '' ? null : parseFloat(e.target.value))}
                  title="Peak antenna gain. Leave blank to use the gain override / antenna type default."
                />
              </div>
            </div>
            <div className="field-row">
              <div className="field">
                <label>-3 dB width</label>
                <input
                  type="text" readOnly
                  value={hpbw3 === null ? '360° (omni)' : `${hpbw3}°`}
                  style={{ color: 'var(--text-secondary)', cursor: 'default' }}
                />
              </div>
              <div className="field">
                <label>-6 dB width</label>
                <input
                  type="text" readOnly
                  value={hpbw6 === null ? '360° (omni)' : `${hpbw6}°`}
                  style={{ color: 'var(--text-secondary)', cursor: 'default' }}
                />
              </div>
            </div>
            <div style={{ fontSize: 10, color: 'var(--text-secondary)', paddingLeft: 2, marginTop: -4, marginBottom: 4, lineHeight: 1.4 }}>
              {patternMeta.description}
              {' '}Side / back lobes follow the pattern shape — no hard cutoffs.
            </div>
            <div className="field-row">
              <div className="field">
                <label>Sweep arc (°)</label>
                <input
                  type="number" min="0" max="360" step="1"
                  value={ant.antenna.sweep_deg ?? 0}
                  onChange={e => update('sweep_deg', Math.max(0, Math.min(360, parseFloat(e.target.value) || 0)))}
                  title="Scanning radar sweep arc, centered on Azimuth. 0 = focused (no sweep). 360 = energy spread evenly over all directions. Other values average the pattern across the swept arc."
                />
              </div>
              <div className="field">
                <label style={{ visibility: 'hidden' }}>placeholder</label>
                <div style={{ fontSize: 10, color: 'var(--text-secondary)', lineHeight: 1.4, paddingTop: 4 }}>
                  {(() => {
                    const sw = ant.antenna.sweep_deg ?? 0
                    if (sw <= 0) return 'No sweep — fixed boresight pattern.'
                    if (sw >= 360) return 'Effectively omni — energy spread over 360°.'
                    return `Beam scans ±${(sw / 2).toFixed(0)}° around boresight.`
                  })()}
                </div>
              </div>
            </div>
          </>
        )
      })()}

      {showDish && (
        <div className="field-row">
          <div className="field">
            <label>Diameter (m)</label>
            <input
              type="number" min="0.1" max="100" step="0.1"
              value={ant.antenna.diameter_m}
              onChange={e => update('diameter_m', parseFloat(e.target.value))}
            />
          </div>
          <div className="field">
            <label>Efficiency</label>
            <input
              type="number" min="0.1" max="1" step="0.05"
              value={ant.antenna.efficiency}
              onChange={e => update('efficiency', parseFloat(e.target.value))}
            />
          </div>
        </div>
      )}

      {showArray && (
        <div className="field-row single">
          <div className="field">
            <label>Array elements</label>
            <input
              type="number" min="4" max="1024" step="4"
              value={ant.antenna.array_elements}
              onChange={e => update('array_elements', parseInt(e.target.value))}
            />
          </div>
        </div>
      )}

      {showCustom && (
        <div className="field-row single">
          <div className="field">
            <label>Pattern JSON (azimuth/elevation/gain_dbi)</label>
            <textarea
              rows={3}
              placeholder='{"azimuth":[0,...,359],"elevation":[-90,...,90],"gain_dbi":[[...]]}'
              value={ant.antenna.custom_pattern_json ?? ''}
              onChange={e => update('custom_pattern_json', e.target.value)}
              style={{
                background: 'var(--bg-tertiary)',
                border: '1px solid var(--border)',
                borderRadius: 4,
                color: 'var(--text-primary)',
                fontSize: 11,
                padding: 6,
                fontFamily: 'monospace',
                resize: 'vertical',
              }}
            />
          </div>
        </div>
      )}
    </div>
  )
}

export default function AntennaPanel({ tx, setTx, rx, setRx, txFrequencyHz }) {
  const [open, setOpen] = useState(false)
  const [antennaPresets, setAntennaPresets] = useState([])

  useEffect(() => {
    getAntennaPresets()
      .then(d => setAntennaPresets(d.presets || []))
      .catch(() => setAntennaPresets([]))
  }, [])

  return (
    <div>
      <button
        className={`accordion-trigger ${open ? 'open' : ''}`}
        onClick={() => setOpen(o => !o)}
      >
        <span style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
          <span style={{ fontSize: 14 }}>📡</span>
          Antenna Patterns
        </span>
        {open ? <ChevronUp size={14} /> : <ChevronDown size={14} />}
      </button>

      {open && (
        <div className="panel-section">
          <AntennaConfig label="TX Antenna" ant={tx} setAnt={setTx} prefix="tx" showBeamControls frequencyHz={txFrequencyHz} antennaPresets={antennaPresets} />
          <AntennaConfig label="RX Antenna" ant={rx} setAnt={setRx} prefix="rx" showBeamControls={false} frequencyHz={txFrequencyHz} antennaPresets={antennaPresets} />
        </div>
      )}
    </div>
  )
}
