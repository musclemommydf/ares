// SPDX-License-Identifier: MIT OR Apache-2.0
// Copyright (c) 2026 Ares

/**
 * Transmitter configuration panel.
 * Controls: position (with coordinate format display), height, altitude,
 * frequency, power, and device presets.
 */
import { useState, useEffect, useRef } from 'react'
import { Radio, ChevronDown, ChevronUp, Cpu } from 'lucide-react'
import { FREQ_PRESETS, POWER_PRESETS, formatFreq, getDevicePresets } from '../../api/client'
import { formatCoordinate, coordSystemLabel } from '../../utils/units'

// Group devices by category for the dropdown
function groupByCategory(devices) {
  const groups = {}
  for (const d of devices) {
    if (!groups[d.category]) groups[d.category] = []
    groups[d.category].push(d)
  }
  return groups
}

const POWER_UNITS = [
  { label: 'kW', mwFactor: 1e6 },
  { label: 'W',  mwFactor: 1e3 },
  { label: 'mW', mwFactor: 1 },
  { label: 'µW', mwFactor: 1e-3 },
]

function dbmToMw(dbm) { return 10 ** (dbm / 10) }
function mwToDbm(mw)   { return 10 * Math.log10(mw) }
function mwToDisplay(mw, unit) { return (mw / unit.mwFactor).toPrecision(4).replace(/\.?0+$/, '') }

export default function TransmitterPanel({ tx, setTx, coordSystem = 'latlon', distUnit = 'metric', setRx, expandSignal = 0 }) {
  const [open, setOpen] = useState(true)
  // External "Edit this emitter" signal from the Emitter Summary tab: whenever
  // expandSignal bumps to a new non-zero value, force the accordion open and
  // scroll into view. Stable across re-renders that don't change expandSignal.
  const rootRef = useRef(null)
  useEffect(() => {
    if (!expandSignal) return
    setOpen(true)
    const el = rootRef.current
    if (!el) return
    // Defer a tick so the just-opened section has its height measured.
    requestAnimationFrame(() => {
      el.scrollIntoView({ behavior: 'smooth', block: 'start' })
    })
  }, [expandSignal])
  const [freqMhz, setFreqMhz] = useState((tx.frequency_hz / 1e6).toFixed(4))
  const [devices, setDevices] = useState([])
  const [deviceGroups, setDeviceGroups] = useState({})
  const [powerUnit, setPowerUnit] = useState(POWER_UNITS[1]) // default W
  const [powerDisplay, setPowerDisplay] = useState(
    () => mwToDisplay(dbmToMw(tx.power_dbm), POWER_UNITS[1])
  )
  const [dbmDisplay, setDbmDisplay] = useState(() => tx.power_dbm.toFixed(1))

  useEffect(() => {
    getDevicePresets()
      .then(d => {
        setDevices(d.devices)
        setDeviceGroups(groupByCategory(d.devices))
      })
      .catch(() => {})
  }, [])

  const update = (field, value) => setTx(prev => ({ ...prev, [field]: value }))

  const handleFreqChange = (val) => {
    setFreqMhz(val)
    const hz = parseFloat(val) * 1e6
    if (!isNaN(hz) && hz > 0) update('frequency_hz', hz)
  }

  const applyFreqPreset = (hz) => {
    update('frequency_hz', hz)
    setFreqMhz((hz / 1e6).toFixed(6).replace(/\.?0+$/, ''))
  }

  const handlePowerChange = (val) => {
    setPowerDisplay(val)
    const mw = parseFloat(val) * powerUnit.mwFactor
    if (!isNaN(mw) && mw > 0) {
      const dbm = mwToDbm(mw)
      update('power_dbm', dbm)
      setDbmDisplay(dbm.toFixed(1))
    }
  }

  const handleDbmChange = (val) => {
    setDbmDisplay(val)
    const dbm = parseFloat(val)
    if (!isNaN(dbm)) {
      update('power_dbm', dbm)
      setPowerDisplay(mwToDisplay(dbmToMw(dbm), powerUnit))
    }
  }

  const handlePowerUnitChange = (unit) => {
    setPowerUnit(unit)
    setPowerDisplay(mwToDisplay(dbmToMw(tx.power_dbm), unit))
  }

  const applyPowerPreset = (dbm) => {
    update('power_dbm', dbm)
    setPowerDisplay(mwToDisplay(dbmToMw(dbm), powerUnit))
    setDbmDisplay(dbm.toFixed(1))
  }

  const applyDevicePreset = (device) => {
    // Build the antenna patch once and apply it to both TX and RX so the
    // simulator runs against a single, datasheet-consistent antenna config.
    const antennaPatch = {
      type: device.antenna_type || 'dipole_half_wave',
      gain_dbi: device.antenna_gain_dbi ?? null,
      polarization: device.antenna_polarization || 'vertical',
      tilt_deg: device.antenna_tilt_deg ?? 0,
      polar_pattern: device.polar_pattern ?? 'omni',
      polar_peak_gain_dbi: device.antenna_gain_dbi ?? null,
      sweep_deg: device.sweep_deg ?? 0,
      // Clear any previously-applied antenna preset id; this is a radio
      // preset, not an antenna preset.
      preset_id: undefined,
    }

    setTx(prev => ({
      ...prev,
      frequency_hz: device.frequency_hz,
      power_dbm: device.power_dbm,
      height_m: device.height_m ?? prev.height_m,
      altitude_m: device.altitude_m ?? prev.altitude_m,   // e.g. the GNSS space-segment presets carry the MEO altitude
      antenna: {
        ...prev.antenna,
        ...antennaPatch,
        height_m: device.height_m ?? prev.antenna.height_m,
        frequency_hz: device.frequency_hz,
      },
    }))
    if (setRx) {
      setRx(prev => ({
        ...prev,
        sensitivity_dbm: device.rx_sensitivity_dbm ?? prev.sensitivity_dbm,
        noise_figure_db: device.rx_noise_figure_db ?? prev.noise_figure_db,
        antenna: {
          ...prev.antenna,
          ...antennaPatch,
          height_m: prev.antenna.height_m,  // RX antenna height is independent
          frequency_hz: device.frequency_hz,
        },
      }))
    }
    setFreqMhz((device.frequency_hz / 1e6).toFixed(4))
    setPowerDisplay(mwToDisplay(dbmToMw(device.power_dbm), powerUnit))
    setDbmDisplay(device.power_dbm.toFixed(1))
  }

  const heightLabel = distUnit === 'imperial' ? 'ft AGL' : 'm AGL'
  const heightFactor = distUnit === 'imperial' ? 3.28084 : 1
  const heightDisplay = v => (v * heightFactor).toFixed(1)
  const heightInput = v => v / heightFactor

  return (
    <div ref={rootRef}>
      <button
        className={`accordion-trigger ${open ? 'open' : ''}`}
        onClick={() => setOpen(o => !o)}
      >
        <span style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
          <Radio size={14} color="var(--accent-blue)" />
          Transmitter
        </span>
        {open ? <ChevronUp size={14} /> : <ChevronDown size={14} />}
      </button>

      {open && (
        <div className="panel-section">
          {/* Device preset */}
          {devices.length > 0 && (
            <>
              <div className="panel-title">
                <span style={{ display: 'flex', alignItems: 'center', gap: 5 }}>
                  <Cpu size={12} />
                  Device Preset
                </span>
              </div>
              <div className="field-row single">
                <div className="field">
                  <select onChange={e => {
                    const d = devices.find(x => x.id === e.target.value)
                    if (d) applyDevicePreset(d)
                    e.target.value = ''
                  }} defaultValue="">
                    <option value="" disabled>— Select device —</option>
                    {Object.entries(deviceGroups).map(([cat, devs]) => (
                      <optgroup key={cat} label={cat}>
                        {devs.map(d => (
                          <option key={d.id} value={d.id}>
                            {d.label} ({formatFreq(d.frequency_hz)}, {d.power_dbm} dBm)
                          </option>
                        ))}
                      </optgroup>
                    ))}
                  </select>
                </div>
              </div>
            </>
          )}

          {/* Position */}
          <div className="panel-title">Position</div>
          {coordSystem !== 'latlon' && (
            <div style={{ marginBottom: 6, fontSize: 11, color: 'var(--text-muted)', fontFamily: 'monospace' }}>
              {coordSystemLabel(coordSystem)}: {formatCoordinate(tx.lat, tx.lon, coordSystem)}
            </div>
          )}
          <div className="field-row">
            <div className="field">
              <label>Latitude</label>
              <input
                type="number" step="0.00001" min="-90" max="90"
                value={tx.lat}
                onChange={e => update('lat', parseFloat(e.target.value))}
              />
            </div>
            <div className="field">
              <label>Longitude</label>
              <input
                type="number" step="0.00001" min="-180" max="180"
                value={tx.lon}
                onChange={e => update('lon', parseFloat(e.target.value))}
              />
            </div>
          </div>

          <div className="field-row">
            <div className="field">
              <label>Height AGL ({distUnit === 'imperial' ? 'ft' : 'm'})</label>
              <input
                type="number" min="0" max={distUnit === 'imperial' ? 32808 : 10000} step="1"
                value={heightDisplay(tx.height_m)}
                onChange={e => update('height_m', heightInput(parseFloat(e.target.value)))}
              />
            </div>
            <div className="field">
              <label>Site Alt ASL ({distUnit === 'imperial' ? 'ft' : 'm'})</label>
              <input
                type="number" min="0" max={distUnit === 'imperial' ? 30000 : 9144} step="1"
                value={heightDisplay(tx.altitude_m)}
                onChange={e => update('altitude_m', heightInput(parseFloat(e.target.value)))}
                title="Transmitter site altitude above sea level"
              />
            </div>
          </div>

          <div className="panel-title" style={{ marginTop: 6 }}>
            Beam Height Range
            <span style={{ fontSize: 10, color: 'var(--text-muted)', marginLeft: 6, fontWeight: 400 }}>
              AGL · for 3D view
            </span>
          </div>
          <div className="field-row">
            <div className="field">
              <label>Min ({distUnit === 'imperial' ? 'ft' : 'm'})</label>
              <input
                type="number" min="0" max={distUnit === 'imperial' ? 32808 : 10000} step="1"
                value={heightDisplay(tx.antenna?.beam_height_min_m ?? 2)}
                onChange={e => setTx(prev => ({
                  ...prev,
                  antenna: { ...prev.antenna, beam_height_min_m: heightInput(parseFloat(e.target.value)) }
                }))}
                title="Minimum receiver height AGL for 3D beam volume visualisation"
              />
            </div>
            <div className="field">
              <label>Max ({distUnit === 'imperial' ? 'ft' : 'm'})</label>
              <input
                type="number" min="0" max={distUnit === 'imperial' ? 32808 : 10000} step="1"
                value={heightDisplay(tx.antenna?.beam_height_max_m ?? 50)}
                onChange={e => setTx(prev => ({
                  ...prev,
                  antenna: { ...prev.antenna, beam_height_max_m: heightInput(parseFloat(e.target.value)) }
                }))}
                title="Maximum receiver height AGL for 3D beam volume visualisation"
              />
            </div>
          </div>

          {/* Frequency */}
          <div className="panel-title" style={{ marginTop: 8 }}>Frequency</div>
          <div className="field-row single">
            <div className="field">
              <label>
                Frequency (MHz)
                <span className="field-unit" style={{ marginLeft: 6 }}>
                  = {formatFreq(tx.frequency_hz)}
                </span>
              </label>
              <input
                type="number"
                min="0.000001" max="300000"
                step="any"
                value={freqMhz}
                onChange={e => handleFreqChange(e.target.value)}
              />
            </div>
          </div>

          <div className="field-row single">
            <div className="field">
              <label>Quick select</label>
              <select onChange={e => applyFreqPreset(parseFloat(e.target.value))}
                      value="">
                <option value="" disabled>— Common frequencies —</option>
                {FREQ_PRESETS.map(p => (
                  <option key={p.hz} value={p.hz}>{p.label}</option>
                ))}
              </select>
            </div>
          </div>

          {/* Power */}
          <div className="panel-title" style={{ marginTop: 8 }}>Power</div>
          <div className="field-row">
            <div className="field">
              <label>TX Power</label>
              <div style={{ display: 'flex', gap: 4 }}>
                <input
                  type="number" min="0" step="any"
                  style={{ flex: 1, minWidth: 0 }}
                  value={powerDisplay}
                  onChange={e => handlePowerChange(e.target.value)}
                />
                <select
                  value={powerUnit.label}
                  onChange={e => handlePowerUnitChange(POWER_UNITS.find(u => u.label === e.target.value))}
                  style={{ width: 58 }}
                >
                  {POWER_UNITS.map(u => (
                    <option key={u.label} value={u.label}>{u.label}</option>
                  ))}
                </select>
              </div>
            </div>
            <div className="field">
              <label>= dBm</label>
              <input
                type="number" step="0.1"
                value={dbmDisplay}
                onChange={e => handleDbmChange(e.target.value)}
              />
            </div>
          </div>

          <div className="field-row single">
            <div className="field">
              <label>Quick select</label>
              <select
                onChange={e => applyPowerPreset(parseFloat(e.target.value))}
                value={tx.power_dbm}
              >
                {POWER_PRESETS.map(p => (
                  <option key={p.dbm} value={p.dbm}>{p.label}</option>
                ))}
              </select>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
