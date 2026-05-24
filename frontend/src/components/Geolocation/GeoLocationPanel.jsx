// SPDX-License-Identifier: MIT OR Apache-2.0
// Copyright (c) 2026 Ares

/**
 * Geolocation sidebar panel — Line of Bearing (LoB) tool.
 *
 * Allows operators to enter DF observations (azimuth, RSSI, frequency, location,
 * confidence) which are rendered on the map as bearing lines. Groups of same-
 * frequency LoBs are automatically classified as Cut (2) or Fix (3+), and an
 * optional Circular Area of Probability (CAP) ellipse can be toggled per group.
 */
import { useState, useEffect, useRef } from 'react'
import { Plus, MapPin, Target, Navigation, Pencil, X, ChevronDown, ChevronRight } from 'lucide-react'
import {
  estimateDistance,
  computeGroupIntersections,
  computeCentroid,
  lobGroupKey,
  ENVIRONMENT_PRESETS,
  DEFAULT_LOB_ALGORITHM,
  initialBearing,
  effectiveRxHPBW,
} from './LoBUtils'
import { POLAR_PATTERNS, patternsByCategory } from '../../utils/polarPatterns'
import { autoDetectEnvironment, estimateDistanceFromTerrain } from './LoBAutoDetect'
import {
  coordSystemLabel,
  coordInputPlaceholder,
  parseCoordinateInput,
  autoParseCoordinate,
  formatCoordinate,
} from '../../utils/units'

export const LOB_COLORS = [
  '#f59e0b', '#00b4d8', '#a78bfa', '#ef4444',
  '#06d6a0', '#f97316', '#ec4899', '#facc15',
]

const FREQ_PRESETS = [
  { label: 'MW (1 MHz)',     hz: 1e6 },
  { label: 'HF-Low (5 MHz)', hz: 5e6 },
  { label: 'HF (10 MHz)',    hz: 10e6 },
  { label: 'VHF (100 MHz)',  hz: 100e6 },
  { label: '2m (144 MHz)',   hz: 144e6 },
  { label: '70cm (433 MHz)', hz: 433e6 },
  { label: 'UHF (869 MHz)',  hz: 869e6 },
  { label: 'L-Band (1.2G)', hz: 1200e6 },
  { label: '2.4 GHz',       hz: 2400e6 },
  { label: '5.8 GHz',       hz: 5800e6 },
]

// TX power unit definitions and conversion to dBm
const TX_POWER_UNITS = [
  { value: 'dBm', label: 'dBm' },
  { value: 'dBW', label: 'dBW' },
  { value: 'W',   label: 'W'   },
  { value: 'mW',  label: 'mW'  },
  { value: 'kW',  label: 'kW'  },
]

function toDbm(value, unit) {
  if (value === null || isNaN(value)) return null
  switch (unit) {
    case 'dBm': return value
    case 'dBW': return value + 30
    case 'W':   return value > 0 ? 10 * Math.log10(value) + 30 : null
    case 'mW':  return value > 0 ? 10 * Math.log10(value)      : null
    case 'kW':  return value > 0 ? 10 * Math.log10(value) + 60 : null
    default:    return value
  }
}

export const DEVICE_TYPES = [
  { value: '',          label: 'None' },
  { value: 'dmr',       label: 'DMR Handset ID' },
  { value: 'imei',      label: 'IMEI' },
  { value: 'imsi',      label: 'IMSI' },
  { value: 'mac',       label: 'MAC Address' },
  { value: 'callsign',  label: 'Callsign' },
  { value: 'other',     label: 'Other ID' },
]

const DEFAULT_FORM = {
  lat: '',
  lon: '',
  frequency_mhz: '433',
  azimuth_deg: '',
  rssi_dbm: '-75',
  tx_power_dbm: '',
  confidence_pct: 80,
  time: new Date().toISOString().slice(0, 16),
  device_type: '',
  device_id: '',
  environment: 'suburban',
  clutter_height_m: 0,
  observer_height_m: 1.5,
}

function FieldRow({ label, children }) {
  return (
    <div style={{ display: 'flex', alignItems: 'center', gap: 4, marginBottom: 5 }}>
      <label style={{ fontSize: 10, color: '#8b949e', width: 68, flexShrink: 0 }}>{label}</label>
      {children}
    </div>
  )
}

function NumInput({ value, onChange, onBlur, placeholder, min, max, step, style }) {
  return (
    <input
      type="number"
      value={value}
      onChange={e => onChange(e.target.value)}
      onBlur={onBlur ? (e => onBlur(e.target.value)) : undefined}
      placeholder={placeholder}
      min={min}
      max={max}
      step={step ?? 'any'}
      style={{
        flex: 1, background: '#0d1117', border: '1px solid #30363d',
        borderRadius: 4, color: '#e6edf3', fontSize: 11, padding: '4px 6px',
        ...style,
      }}
    />
  )
}

const LOB_ALGORITHMS = [
  { value: 'fixed',        label: 'Fixed length' },
  { value: 'step',         label: 'Step (RSSI → anchored length)' },
  { value: 'intersection', label: 'Intersection (extend until crossed)' },
]

// Linear units used by the LoB length algorithm. Storage stays in meters;
// these factors convert displayed value → meters.
const LOB_DIST_UNITS = [
  { value: 'm',   toMeters: 1 },
  { value: 'km',  toMeters: 1000 },
  { value: 'ft',  toMeters: 0.3048 },
  { value: 'mi',  toMeters: 1609.344 },
  { value: 'nmi', toMeters: 1852 },
]
const unitToMeters = (u) => LOB_DIST_UNITS.find(x => x.value === u)?.toMeters ?? 1000

function DistanceInput({ value_m, unit, onChange, step, min }) {
  const factor = unitToMeters(unit)
  const display = value_m / factor
  // Display 4 sig-figs max, but keep raw if user is mid-edit
  return (
    <>
      <NumInput
        value={Number.isFinite(display) ? +display.toPrecision(8) : ''}
        onChange={v => {
          const n = parseFloat(v)
          const meters = isNaN(n) ? 0 : Math.max(1, n * unitToMeters(unit))
          onChange({ value_m: meters, unit })
        }}
        step={step ?? 'any'}
        min={min}
      />
      <select
        value={unit}
        onChange={e => onChange({ value_m, unit: e.target.value })}
        style={{
          width: 44, background: '#0d1117', border: '1px solid #30363d',
          borderRadius: 4, color: '#8b949e', fontSize: 9, padding: '2px 4px',
          cursor: 'pointer', flexShrink: 0,
        }}
        title="Distance unit"
      >
        {LOB_DIST_UNITS.map(u => (
          <option key={u.value} value={u.value}>{u.value}</option>
        ))}
      </select>
    </>
  )
}

export default function GeoLocationPanel({
  lobs,
  onAddLoB,
  onUpdateLoB,
  capGroups,       // { [groupKey]: boolean } — missing key = default true
  onToggleCAP,     // (groupKey) => void
  lobGroups,
  pickedLocation,
  onClearPickedLocation,
  onStartPickLocation,
  isPickingLocation,
  pickedAzimuthTarget,
  onClearPickedAzimuthTarget,
  onStartPickAzimuth,
  isPickingAzimuth,
  editLobRequestId,
  onClearEditLobRequest,
  lobAlgorithm,
  onChangeLobAlgorithm,
}) {
  const algo = lobAlgorithm || DEFAULT_LOB_ALGORITHM
  const [algoOpen, setAlgoOpen] = useState(true)

  const setAlgoType = (type) => onChangeLobAlgorithm({ ...algo, type })
  const setAlgoStep = (patch) => onChangeLobAlgorithm({ ...algo, step: { ...algo.step, ...patch } })
  const setAlgoFixed = (patch) => onChangeLobAlgorithm({ ...algo, fixed: { ...algo.fixed, ...patch } })
  const setAlgoTopLevel = (patch) => onChangeLobAlgorithm({ ...algo, ...patch })
  const setAlgoRx = (patch) => onChangeLobAlgorithm({
    ...algo,
    receiver_accuracy: { ...(algo.receiver_accuracy || DEFAULT_LOB_ALGORITHM.receiver_accuracy), ...patch },
  })
  const [form, setForm] = useState(DEFAULT_FORM)
  const [formError, setFormError] = useState('')
  const [txPowerUnit, setTxPowerUnit] = useState('dBm')
  const [editingLobId, setEditingLobId] = useState(null)
  const [locCoordSystem, setLocCoordSystem] = useState('mgrs')
  const [locInput, setLocInput] = useState('')  // raw text for the unified coord input
  const [locError, setLocError] = useState('')
  const [envDetecting, setEnvDetecting] = useState(false)
  const [envSource, setEnvSource] = useState('default') // 'auto' | 'manual' | 'default'
  const detectSeqRef = useRef(0)  // cancel stale detect results
  const [pendingTerrainIds, setPendingTerrainIds] = useState(new Set())

  const triggerAutoDetect = (lat, lon) => {
    const seq = ++detectSeqRef.current
    setEnvDetecting(true)
    autoDetectEnvironment(lat, lon).then(result => {
      if (seq !== detectSeqRef.current) return  // stale — a newer detection was started
      setEnvDetecting(false)
      if (result) {
        setForm(f => ({ ...f, environment: result.environment, clutter_height_m: result.clutter_height_m }))
        setEnvSource('auto')
      } else {
        setEnvSource('default')
      }
    })
  }

  const triggerTerrainEstimate = (lobId, lobData) => {
    if (lobData.tx_power_dbm === null) return  // can't estimate without TX power
    setPendingTerrainIds(prev => new Set([...prev, lobId]))
    estimateDistanceFromTerrain({
      observer_lat: lobData.lat,
      observer_lon: lobData.lon,
      observer_height_m: lobData.observer_height_m ?? 1.5,
      azimuth_deg: lobData.azimuth_deg,
      frequency_hz: lobData.frequency_hz,
      tx_power_dbm: lobData.tx_power_dbm,
      observed_rssi_dbm: lobData.rssi_dbm,
      clutter_height_m: lobData.clutter_height_m ?? 0,
      diffraction_model: 'deygout',
      terrain_resolution: 'srtm1',
      context: 2,
    }).then(result => {
      setPendingTerrainIds(prev => { const n = new Set(prev); n.delete(lobId); return n })
      if (result) {
        onUpdateLoB({ ...lobData, id: lobId, estimated_distance_m: result.estimated_distance_m, distance_method: 'terrain' })
      }
    }).catch(() => {
      setPendingTerrainIds(prev => { const n = new Set(prev); n.delete(lobId); return n })
    })
  }

  const startEdit = (lob) => {
    setEditingLobId(lob.id)
    setTxPowerUnit('dBm')
    setFormError('')
    setLocInput(formatCoordinate(lob.lat, lob.lon, locCoordSystem))
    setLocError('')
    setEnvSource('manual')  // editing an existing LoB — treat stored values as manually set
    setForm({
      lat: lob.lat.toString(),
      lon: lob.lon.toString(),
      frequency_mhz: (lob.frequency_hz / 1e6).toFixed(3),
      azimuth_deg: lob.azimuth_deg.toString(),
      rssi_dbm: lob.rssi_dbm.toString(),
      tx_power_dbm: lob.tx_power_dbm !== null ? lob.tx_power_dbm.toString() : '',
      confidence_pct: lob.confidence_pct,
      time: lob.time || new Date().toISOString().slice(0, 16),
      device_type: lob.device_type || '',
      device_id: lob.device_id || '',
      environment: lob.environment || 'suburban',
      clutter_height_m: lob.clutter_height_m ?? 0,
      observer_height_m: lob.observer_height_m ?? 1.5,
    })
  }

  const cancelEdit = () => {
    setEditingLobId(null)
    setForm(DEFAULT_FORM)
    setTxPowerUnit('dBm')
    setFormError('')
    setLocInput('')
    setLocError('')
    setEnvSource('default')
    setEnvDetecting(false)
  }

  // Apply location picked from map — format in the currently selected coord system
  useEffect(() => {
    if (pickedLocation) {
      const { lat, lon } = pickedLocation
      setForm(f => ({ ...f, lat: lat.toString(), lon: lon.toString() }))
      setLocInput(formatCoordinate(lat, lon, locCoordSystem))
      setLocError('')
      onClearPickedLocation()
      triggerAutoDetect(lat, lon)
    }
  }, [pickedLocation])

  // External edit request (e.g. from the bottom-panel LoB list)
  useEffect(() => {
    if (!editLobRequestId) return
    const lob = lobs.find(l => l.id === editLobRequestId)
    if (lob) startEdit(lob)
    onClearEditLobRequest?.()
  }, [editLobRequestId])

  // Apply azimuth target picked from map — compute bearing from observer → target
  useEffect(() => {
    if (!pickedAzimuthTarget) return
    const obsLat = parseFloat(form.lat)
    const obsLon = parseFloat(form.lon)
    if (isFinite(obsLat) && isFinite(obsLon)) {
      const az = initialBearing(obsLat, obsLon, pickedAzimuthTarget.lat, pickedAzimuthTarget.lon)
      if (isFinite(az)) {
        setForm(f => ({ ...f, azimuth_deg: az.toFixed(1) }))
      }
    }
    onClearPickedAzimuthTarget()
  }, [pickedAzimuthTarget])

  const upd = (field, value) => setForm(f => ({ ...f, [field]: value }))

  // Parse the coord input and sync to form lat/lon. Returns {lat,lon} or null.
  // First tries the selected system; if that fails, auto-detects the format.
  const resolveLocation = () => {
    const parsed = parseCoordinateInput(locInput, locCoordSystem)
      ?? autoParseCoordinate(locInput)
    if (!parsed) {
      setLocError('Unrecognised coordinate format')
      return null
    }
    setLocError('')
    setForm(f => ({ ...f, lat: parsed.lat.toString(), lon: parsed.lon.toString() }))
    return parsed
  }

  // When the user finishes typing a location, silently resolve and auto-detect environment
  const handleLocBlur = () => {
    if (envSource === 'manual') return  // user has already made a manual choice
    const parsed = parseCoordinateInput(locInput, locCoordSystem) ?? autoParseCoordinate(locInput)
    if (parsed) triggerAutoDetect(parsed.lat, parsed.lon)
  }

  const txPowerKnown = form.tx_power_dbm.trim() !== ''
  const terrainOpts = { environment: form.environment, clutter_height_m: parseFloat(form.clutter_height_m) || 0 }
  const previewDist = (() => {
    const rssiRaw = parseFloat(form.rssi_dbm)
    const rssi = isFinite(rssiRaw) ? -Math.abs(rssiRaw) : NaN
    const fhz = parseFloat(form.frequency_mhz) * 1e6
    if (isNaN(rssi) || isNaN(fhz) || fhz <= 0) return null
    if (!txPowerKnown) return null
    const pwr_dbm = toDbm(parseFloat(form.tx_power_dbm), txPowerUnit)
    if (pwr_dbm === null || isNaN(pwr_dbm)) return null
    return estimateDistance(rssi, fhz, pwr_dbm, terrainOpts)
  })()

  const handleAdd = () => {
    setFormError('')
    const loc = resolveLocation()
    if (!loc) return
    const { lat, lon } = loc
    const az = parseFloat(form.azimuth_deg)
    const rssiRaw = parseFloat(form.rssi_dbm)
    const rssi = isFinite(rssiRaw) ? -Math.abs(rssiRaw) : NaN
    const fhz = parseFloat(form.frequency_mhz) * 1e6
    const pwrRaw = form.tx_power_dbm.trim()
    const pwrEntered = pwrRaw === '' ? null : parseFloat(pwrRaw)
    const pwr_dbm = pwrEntered !== null ? toDbm(pwrEntered, txPowerUnit) : null
    const conf = parseFloat(form.confidence_pct)

    if (isNaN(az) || az < 0 || az > 360) { setFormError('Azimuth must be 0 – 360°.'); return }
    if (isNaN(rssi)) { setFormError('Enter RSSI in dBm.'); return }
    if (isNaN(fhz) || fhz <= 0) { setFormError('Enter a valid frequency.'); return }
    if (pwrEntered !== null && pwr_dbm === null) { setFormError('TX power must be greater than 0 for this unit.'); return }

    // When TX power is unknown use a 10 km fallback so the bearing line
    // still appears on the map — the endpoint tick is hidden in this case.
    const estimated_distance_m = pwr_dbm !== null
      ? estimateDistance(rssi, fhz, pwr_dbm, terrainOpts)
      : 10_000
    const color = LOB_COLORS[lobs.length % LOB_COLORS.length]

    const newLobId = Date.now()
    const newLob = {
      id: newLobId,
      label: `LoB ${lobs.length + 1}`,
      lat, lon,
      frequency_hz: fhz,
      azimuth_deg: ((az % 360) + 360) % 360,
      rssi_dbm: rssi,
      tx_power_dbm: pwr_dbm,
      confidence_pct: Math.max(1, Math.min(100, isNaN(conf) ? 80 : conf)),
      time: form.time,
      estimated_distance_m,
      color,
      device_type: form.device_type || '',
      device_id: form.device_id || '',
      environment: form.environment,
      clutter_height_m: parseFloat(form.clutter_height_m) || 0,
      observer_height_m: parseFloat(form.observer_height_m) || 0,
    }
    onAddLoB(newLob)
    if (pwr_dbm !== null) triggerTerrainEstimate(newLobId, newLob)

    // Fields are preserved intentionally — user can enter the next LoB without re-filling shared params
  }

  const handleUpdate = () => {
    setFormError('')
    const lob = lobs.find(l => l.id === editingLobId)
    if (!lob) return
    const loc = resolveLocation()
    if (!loc) return
    const { lat, lon } = loc
    const az = parseFloat(form.azimuth_deg)
    const rssiRaw = parseFloat(form.rssi_dbm)
    const rssi = isFinite(rssiRaw) ? -Math.abs(rssiRaw) : NaN
    const fhz = parseFloat(form.frequency_mhz) * 1e6
    const pwrRaw = form.tx_power_dbm.trim()
    const pwrEntered = pwrRaw === '' ? null : parseFloat(pwrRaw)
    const pwr_dbm = pwrEntered !== null ? toDbm(pwrEntered, txPowerUnit) : null
    const conf = parseFloat(form.confidence_pct)

    if (isNaN(az) || az < 0 || az > 360) { setFormError('Azimuth must be 0 – 360°.'); return }
    if (isNaN(rssi)) { setFormError('Enter RSSI in dBm.'); return }
    if (isNaN(fhz) || fhz <= 0) { setFormError('Enter a valid frequency.'); return }
    if (pwrEntered !== null && pwr_dbm === null) { setFormError('TX power must be greater than 0 for this unit.'); return }

    const estimated_distance_m = pwr_dbm !== null
      ? estimateDistance(rssi, fhz, pwr_dbm, terrainOpts)
      : 10_000

    onUpdateLoB({
      ...lob,
      lat, lon,
      frequency_hz: fhz,
      azimuth_deg: ((az % 360) + 360) % 360,
      rssi_dbm: rssi,
      tx_power_dbm: pwr_dbm,
      confidence_pct: Math.max(1, Math.min(100, isNaN(conf) ? 80 : conf)),
      time: form.time,
      estimated_distance_m,
      device_type: form.device_type || '',
      device_id: form.device_id || '',
      environment: form.environment,
      clutter_height_m: parseFloat(form.clutter_height_m) || 0,
      observer_height_m: parseFloat(form.observer_height_m) || 0,
    })
    if (pwr_dbm !== null) triggerTerrainEstimate(lob.id, { ...lob, lat, lon, frequency_hz: fhz, azimuth_deg: ((az % 360) + 360) % 360, rssi_dbm: rssi, tx_power_dbm: pwr_dbm, clutter_height_m: parseFloat(form.clutter_height_m) || 0, observer_height_m: parseFloat(form.observer_height_m) || 0, environment: form.environment })
    cancelEdit()
  }

  return (
    <div style={{ display: 'flex', flexDirection: 'column' }}>

      {/* ── Header ─────────────────────────────────────────────────────── */}
      <div style={{ padding: '8px 12px 6px', borderBottom: '1px solid #21262d' }}>
        <div style={{ fontSize: 11, fontWeight: 700, color: '#a78bfa', letterSpacing: 1, marginBottom: 3 }}>
          LINE OF BEARING (LoB)
        </div>
        <div style={{ fontSize: 10, color: '#444d56', lineHeight: 1.4 }}>
          Add bearing observations to geolocate an emitter.
          2 LoBs → <span style={{ color: '#06d6a0' }}>Cut</span> ·
          3+ LoBs → <span style={{ color: '#ef4444' }}>Fix</span>
        </div>
      </div>

      {/* ── LoB length algorithm ───────────────────────────────────────── */}
      <div style={{ padding: '6px 12px 8px', borderBottom: '1px solid #21262d' }}>
        <button
          onClick={() => setAlgoOpen(o => !o)}
          className="btn btn-ghost"
          style={{
            width: '100%', justifyContent: 'space-between', padding: '4px 0',
            fontSize: 10, fontWeight: 600, color: '#8b949e', letterSpacing: 0.5,
          }}
          title="Choose how each bearing line's length is rendered on the map"
        >
          <span style={{ display: 'flex', alignItems: 'center', gap: 4 }}>
            {algoOpen ? <ChevronDown size={11} /> : <ChevronRight size={11} />}
            LINE LENGTH ALGORITHM
          </span>
          <span style={{ color: '#a78bfa', fontWeight: 700, textTransform: 'uppercase', fontSize: 9 }}>
            {LOB_ALGORITHMS.find(a => a.value === algo.type)?.label.split(' ')[0] ?? algo.type}
          </span>
        </button>
        {algoOpen && (
          <div style={{ marginTop: 4 }}>
            <select
              value={algo.type}
              onChange={e => setAlgoType(e.target.value)}
              style={{
                width: '100%', background: '#0d1117', border: '1px solid #30363d',
                borderRadius: 4, color: '#e6edf3', fontSize: 11,
                padding: '4px 6px', cursor: 'pointer', marginBottom: 6,
              }}
            >
              {LOB_ALGORITHMS.map(a => (
                <option key={a.value} value={a.value}>{a.label}</option>
              ))}
            </select>

            <div style={{ display: 'flex', alignItems: 'center', gap: 6, marginBottom: 6 }}>
              <input
                type="checkbox"
                id="lob-terrain-aware"
                checked={!!algo.terrain_aware}
                onChange={e => setAlgoTopLevel({ terrain_aware: e.target.checked })}
              />
              <label htmlFor="lob-terrain-aware" style={{ fontSize: 10, color: '#8b949e', cursor: 'pointer' }}
                title="When ON, each LoB renders at its terrain-derived range estimate (overrides the algorithm above)">
                Terrain-aware
              </label>
            </div>

            <FieldRow label="Scale">
              <select
                value={algo.step?.interpolation ?? 'exponential'}
                onChange={e => setAlgoStep({ interpolation: e.target.value })}
                disabled={algo.type !== 'step'}
                style={{
                  flex: 1, background: '#0d1117', border: '1px solid #30363d',
                  borderRadius: 4, color: algo.type === 'step' ? '#e6edf3' : '#484f58',
                  fontSize: 11, padding: '4px 6px',
                  cursor: algo.type === 'step' ? 'pointer' : 'not-allowed',
                }}
                title={algo.type === 'step'
                  ? 'How length is interpolated between RSSI anchors'
                  : 'Only applies to the Step algorithm'}
              >
                <option value="exponential">Exponential</option>
                <option value="linear">Linear</option>
              </select>
            </FieldRow>

            {algo.type === 'intersection' && (
              <div style={{ fontSize: 10, color: '#444d56', lineHeight: 1.4 }}>
                Each line extends until it meets another LoB in the same
                frequency / device group. Lines that never cross are drawn long.
              </div>
            )}

            {algo.type === 'step' && (
              <>
                <FieldRow label="Min RSSI">
                  <NumInput
                    value={algo.step.min_rssi_dbm}
                    onChange={v => setAlgoStep({ min_rssi_dbm: parseFloat(v) })}
                    step="1"
                  />
                  <span style={{ fontSize: 9, color: '#484f58', width: 26, textAlign: 'right' }}>dBm</span>
                </FieldRow>
                <FieldRow label="@ Distance">
                  <DistanceInput
                    value_m={algo.step.min_rssi_distance_m}
                    unit={algo.step.min_rssi_distance_unit ?? 'km'}
                    onChange={({ value_m, unit }) => setAlgoStep({
                      min_rssi_distance_m: value_m,
                      min_rssi_distance_unit: unit,
                    })}
                    step="0.1"
                    min={0}
                  />
                </FieldRow>

                <div style={{ display: 'flex', alignItems: 'center', gap: 6, margin: '6px 0 4px' }}>
                  <input
                    type="checkbox"
                    id="lob-step-mid"
                    checked={!!algo.step.middle_enabled}
                    onChange={e => setAlgoStep({ middle_enabled: e.target.checked })}
                  />
                  <label htmlFor="lob-step-mid" style={{ fontSize: 10, color: '#8b949e', cursor: 'pointer' }}>
                    Middle step
                  </label>
                </div>
                {algo.step.middle_enabled && (
                  <>
                    <FieldRow label="Mid RSSI">
                      <NumInput
                        value={algo.step.middle_rssi_dbm}
                        onChange={v => setAlgoStep({ middle_rssi_dbm: parseFloat(v) })}
                        step="1"
                      />
                      <span style={{ fontSize: 9, color: '#484f58', width: 26, textAlign: 'right' }}>dBm</span>
                    </FieldRow>
                    <FieldRow label="@ Distance">
                      <DistanceInput
                        value_m={algo.step.middle_distance_m}
                        unit={algo.step.middle_distance_unit ?? 'km'}
                        onChange={({ value_m, unit }) => setAlgoStep({
                          middle_distance_m: value_m,
                          middle_distance_unit: unit,
                        })}
                        step="0.1"
                        min={0}
                      />
                    </FieldRow>
                  </>
                )}

                <FieldRow label="Max RSSI">
                  <NumInput
                    value={algo.step.max_rssi_dbm}
                    onChange={v => setAlgoStep({ max_rssi_dbm: parseFloat(v) })}
                    step="1"
                  />
                  <span style={{ fontSize: 9, color: '#484f58', width: 26, textAlign: 'right' }}>dBm</span>
                </FieldRow>
                <FieldRow label="@ Distance">
                  <DistanceInput
                    value_m={algo.step.max_rssi_distance_m}
                    unit={algo.step.max_rssi_distance_unit ?? 'km'}
                    onChange={({ value_m, unit }) => setAlgoStep({
                      max_rssi_distance_m: value_m,
                      max_rssi_distance_unit: unit,
                    })}
                    step="0.1"
                    min={0}
                  />
                </FieldRow>

                <div style={{ fontSize: 9, color: '#484f58', lineHeight: 1.4, marginTop: 2 }}>
                  {(algo.step.interpolation ?? 'exponential') === 'linear'
                    ? 'Length scales linearly between anchors; RSSI outside the bounds is clamped.'
                    : 'Length scales exponentially between anchors; RSSI outside the bounds is clamped.'}
                </div>
              </>
            )}

            {algo.type === 'fixed' && (
              <FieldRow label="Length">
                <DistanceInput
                  value_m={algo.fixed.length_m}
                  unit={algo.fixed.length_unit ?? 'km'}
                  onChange={({ value_m, unit }) => setAlgoFixed({
                    length_m: value_m,
                    length_unit: unit,
                  })}
                  step="0.1"
                  min={0}
                />
              </FieldRow>
            )}

            {/* ── Receiver accuracy (drives CAP angular uncertainty) ─── */}
            <div style={{ marginTop: 8, paddingTop: 6, borderTop: '1px solid #21262d' }}>
              <div style={{ fontSize: 10, fontWeight: 600, color: '#8b949e', marginBottom: 4, letterSpacing: 0.5 }}>
                RECEIVER ACCURACY
              </div>
              {(() => {
                const rx = algo.receiver_accuracy || DEFAULT_LOB_ALGORITHM.receiver_accuracy
                const eff = effectiveRxHPBW(rx)
                const groups = patternsByCategory()
                return (
                  <>
                    <FieldRow label="Source">
                      <select
                        value={rx.mode}
                        onChange={e => setAlgoRx({ mode: e.target.value })}
                        style={{
                          flex: 1, background: '#0d1117', border: '1px solid #30363d',
                          borderRadius: 4, color: '#e6edf3', fontSize: 11,
                          padding: '4px 6px', cursor: 'pointer',
                        }}
                        title="Where the receiver beamwidth comes from"
                      >
                        <option value="manual">Manual −3 dB BW</option>
                        <option value="pattern">Polar pattern</option>
                        <option value="gain">Gain (dBi)</option>
                      </select>
                    </FieldRow>
                    {rx.mode === 'manual' && (
                      <FieldRow label="−3 dB BW">
                        <NumInput
                          value={rx.hpbw_deg}
                          onChange={v => setAlgoRx({ hpbw_deg: parseFloat(v) || 0 })}
                          min={1}
                          max={360}
                          step="1"
                        />
                        <span style={{ fontSize: 9, color: '#484f58', width: 26, textAlign: 'right' }}>°</span>
                      </FieldRow>
                    )}
                    {rx.mode === 'pattern' && (
                      <FieldRow label="Pattern">
                        <select
                          value={rx.pattern_id}
                          onChange={e => setAlgoRx({ pattern_id: e.target.value })}
                          style={{
                            flex: 1, background: '#0d1117', border: '1px solid #30363d',
                            borderRadius: 4, color: '#e6edf3', fontSize: 11,
                            padding: '4px 6px', cursor: 'pointer',
                          }}
                        >
                          {Object.entries(groups).map(([cat, items]) => (
                            <optgroup key={cat} label={cat}>
                              {items.map(p => (
                                <option key={p.id} value={p.id}>{p.label}</option>
                              ))}
                            </optgroup>
                          ))}
                        </select>
                      </FieldRow>
                    )}
                    {rx.mode === 'gain' && (
                      <FieldRow label="Gain">
                        <NumInput
                          value={rx.gain_dbi}
                          onChange={v => setAlgoRx({ gain_dbi: parseFloat(v) || 0 })}
                          min={0}
                          max={50}
                          step="0.5"
                        />
                        <span style={{ fontSize: 9, color: '#484f58', width: 26, textAlign: 'right' }}>dBi</span>
                      </FieldRow>
                    )}
                    <div style={{ fontSize: 9, color: '#484f58', lineHeight: 1.4, marginTop: 2 }}>
                      {eff != null
                        ? <>Effective HPBW: <span style={{ color: '#8b949e' }}>{eff.toFixed(1)}°</span> — wider beam → larger CAP.</>
                        : 'Omnidirectional / undefined — receiver contributes no DF resolution.'}
                    </div>
                  </>
                )
              })()}
            </div>
          </div>
        )}
      </div>

      {/* ── Add / Edit LoB form ─────────────────────────────────────────── */}
      <div style={{ padding: '8px 12px', borderBottom: '1px solid #21262d',
        background: editingLobId ? 'rgba(167,139,250,0.04)' : undefined }}>
        {editingLobId && (
          <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 6 }}>
            <span style={{ fontSize: 10, fontWeight: 700, color: '#a78bfa', letterSpacing: 0.5 }}>
              EDITING {lobs.find(l => l.id === editingLobId)?.label}
            </span>
            <button className="btn btn-ghost" style={{ padding: '1px 4px' }} onClick={cancelEdit} title="Cancel edit">
              <X size={11} />
            </button>
          </div>
        )}

        <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 6 }}>
          <div style={{ fontSize: 10, fontWeight: 600, color: '#8b949e' }}>OBSERVER LOCATION</div>
          <select
            value={locCoordSystem}
            onChange={e => { setLocCoordSystem(e.target.value); setLocInput(''); setLocError('') }}
            style={{
              background: '#0d1117', border: '1px solid #30363d', borderRadius: 4,
              color: '#8b949e', fontSize: 9, padding: '2px 4px', cursor: 'pointer',
            }}
          >
            {['latlon', 'latlon_dms', 'mgrs', 'utm'].map(sys => (
              <option key={sys} value={sys}>{coordSystemLabel(sys)}</option>
            ))}
          </select>
        </div>

        {/* Unified coordinate input */}
        <div style={{ display: 'flex', gap: 4, marginBottom: locError ? 2 : 5 }}>
          <input
            value={locInput}
            onChange={e => { setLocInput(e.target.value); setLocError('') }}
            onBlur={handleLocBlur}
            placeholder={coordInputPlaceholder(locCoordSystem)}
            style={{
              flex: 1, background: '#0d1117', border: `1px solid ${locError ? '#ef4444' : '#30363d'}`,
              borderRadius: 4, color: '#e6edf3', fontSize: 11, padding: '4px 6px',
            }}
          />
          <button
            className={`btn ${isPickingLocation ? 'btn-primary' : 'btn-ghost'}`}
            style={{ padding: '4px 8px', flexShrink: 0 }}
            title="Click on the map to pick observer location"
            onClick={onStartPickLocation}
          >
            <MapPin size={12} />
          </button>
        </div>
        {locError && (
          <div style={{ fontSize: 10, color: '#ef4444', marginBottom: 5 }}>{locError}</div>
        )}

        {isPickingLocation && (
          <div style={{ fontSize: 10, color: '#a78bfa', marginBottom: 6, display: 'flex', alignItems: 'center', gap: 4 }}>
            <Navigation size={10} /> Click on the map to set observer location…
          </div>
        )}

        <div style={{ fontSize: 10, fontWeight: 600, color: '#8b949e', marginBottom: 6, marginTop: 2 }}>
          BEARING &amp; SIGNAL
        </div>

        {/* Frequency */}
        <FieldRow label="Freq (MHz)">
          <NumInput
            value={form.frequency_mhz}
            onChange={v => upd('frequency_mhz', v)}
            min={0.001}
            step="0.001"
          />
          <select
            value=""
            onChange={e => {
              if (e.target.value)
                upd('frequency_mhz', (parseFloat(e.target.value) / 1e6).toFixed(3))
            }}
            style={{
              background: '#0d1117', border: '1px solid #30363d', borderRadius: 4,
              color: '#8b949e', fontSize: 10, padding: '4px 3px', flexShrink: 0,
            }}
          >
            <option value="">⋯</option>
            {FREQ_PRESETS.map(p => (
              <option key={p.label} value={p.hz}>{p.label}</option>
            ))}
          </select>
        </FieldRow>

        {/* Azimuth */}
        <FieldRow label="Azimuth (°)">
          <NumInput
            value={form.azimuth_deg}
            onChange={v => upd('azimuth_deg', v)}
            placeholder="0 – 360° true N"
            min={0}
            max={360}
            step="0.1"
          />
          {(() => {
            const obsLat = parseFloat(form.lat)
            const obsLon = parseFloat(form.lon)
            const ready = isFinite(obsLat) && isFinite(obsLon)
            return (
              <button
                className={`btn ${isPickingAzimuth ? 'btn-primary' : 'btn-ghost'}`}
                style={{ padding: '4px 8px', flexShrink: 0, opacity: ready ? 1 : 0.5 }}
                disabled={!ready}
                title={ready
                  ? 'Click on the map to set bearing target — azimuth is observer → target'
                  : 'Set observer location first'}
                onClick={onStartPickAzimuth}
              >
                <MapPin size={12} />
              </button>
            )
          })()}
        </FieldRow>
        {isPickingAzimuth && (
          <div style={{ fontSize: 10, color: '#a78bfa', margin: '0 0 5px 72px' }}>
            Click target on map…
          </div>
        )}

        {/* RSSI — typed as positive magnitude, stored as negative dBm */}
        <FieldRow label="RSSI (dBm)">
          <span style={{ color: '#8b949e', fontSize: 12, fontWeight: 600, paddingRight: 2 }}>−</span>
          <NumInput
            value={(() => {
              if (!form.rssi_dbm) return ''
              const n = parseFloat(form.rssi_dbm)
              return isFinite(n) ? String(Math.abs(n)) : ''
            })()}
            onChange={v => {
              if (v === '') { upd('rssi_dbm', ''); return }
              const n = parseFloat(v)
              if (!isFinite(n)) return
              upd('rssi_dbm', String(-Math.abs(n)))
            }}
            min={0}
            step="1"
          />
        </FieldRow>

        {/* TX power — optional, with unit selector */}
        <FieldRow label="TX Power">
          <NumInput
            value={form.tx_power_dbm}
            onChange={v => upd('tx_power_dbm', v)}
            placeholder="unknown"
            step="any"
          />
          <select
            value={txPowerUnit}
            onChange={e => { setTxPowerUnit(e.target.value); upd('tx_power_dbm', '') }}
            style={{
              background: '#0d1117', border: '1px solid #30363d', borderRadius: 4,
              color: '#e6edf3', fontSize: 10, padding: '4px 2px',
              cursor: 'pointer', flexShrink: 0, width: 44,
            }}
          >
            {TX_POWER_UNITS.map(u => (
              <option key={u.value} value={u.value}>{u.label}</option>
            ))}
          </select>
        </FieldRow>

        {/* Confidence slider */}
        <div style={{ marginBottom: 5 }}>
          <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: 3 }}>
            <label style={{ fontSize: 10, color: '#8b949e' }}>Confidence</label>
            <span style={{ fontSize: 10, color: '#e6edf3', fontWeight: 600 }}>{form.confidence_pct}%</span>
          </div>
          <input
            type="range"
            min={1}
            max={100}
            step={1}
            value={form.confidence_pct}
            onChange={e => upd('confidence_pct', Number(e.target.value))}
            style={{ width: '100%' }}
          />
        </div>

        {/* Time */}
        <FieldRow label="Time">
          <input
            type="datetime-local"
            value={form.time}
            onChange={e => upd('time', e.target.value)}
            style={{
              flex: 1, background: '#0d1117', border: '1px solid #30363d', borderRadius: 4,
              color: '#e6edf3', fontSize: 10, padding: '4px 6px', colorScheme: 'dark',
            }}
          />
        </FieldRow>

        {/* Device identifier */}
        <div style={{ fontSize: 10, fontWeight: 600, color: '#8b949e', marginBottom: 4, marginTop: 4 }}>
          DEVICE IDENTIFIER <span style={{ fontWeight: 400, color: '#484f58' }}>(optional)</span>
        </div>
        <FieldRow label="Type">
          <select
            value={form.device_type}
            onChange={e => { upd('device_type', e.target.value); upd('device_id', '') }}
            style={{
              flex: 1, background: '#0d1117', border: '1px solid #30363d', borderRadius: 4,
              color: '#e6edf3', fontSize: 11, padding: '4px 6px', cursor: 'pointer',
            }}
          >
            {DEVICE_TYPES.map(t => <option key={t.value} value={t.value}>{t.label}</option>)}
          </select>
        </FieldRow>
        {form.device_type && (
          <FieldRow label={DEVICE_TYPES.find(t => t.value === form.device_type)?.label || 'ID'}>
            <input
              value={form.device_id}
              onChange={e => upd('device_id', e.target.value)}
              placeholder="Enter identifier…"
              style={{
                flex: 1, background: '#0d1117', border: '1px solid #30363d', borderRadius: 4,
                color: '#e6edf3', fontSize: 11, padding: '4px 6px',
              }}
            />
          </FieldRow>
        )}

        {/* Terrain & Clutter */}
        <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 4, marginTop: 4 }}>
          <div style={{ fontSize: 10, fontWeight: 600, color: '#8b949e' }}>
            TERRAIN &amp; CLUTTER
          </div>
          <div style={{ fontSize: 9, color: envDetecting ? '#f59e0b' : envSource === 'auto' ? '#06d6a0' : '#484f58',
            fontStyle: envSource === 'default' ? 'italic' : 'normal' }}>
            {envDetecting ? '⟳ detecting…' : envSource === 'auto' ? '✓ auto-detected' : envSource === 'manual' ? 'manual override' : 'set location to auto-detect'}
          </div>
        </div>
        <FieldRow label="Environment">
          <select
            value={form.environment}
            onChange={e => { upd('environment', e.target.value); setEnvSource('manual') }}
            style={{
              flex: 1, background: '#0d1117', border: '1px solid #30363d', borderRadius: 4,
              color: '#e6edf3', fontSize: 11, padding: '4px 6px', cursor: 'pointer',
            }}
          >
            {ENVIRONMENT_PRESETS.map(p => (
              <option key={p.id} value={p.id}>{p.label}</option>
            ))}
          </select>
        </FieldRow>
        <FieldRow label="Clutter (m)">
          <NumInput
            value={form.clutter_height_m}
            onChange={v => { upd('clutter_height_m', v); setEnvSource('manual') }}
            placeholder="0"
            min={0}
            max={100}
            step="1"
          />
        </FieldRow>
        <FieldRow label="Obs AGL (m)">
          <NumInput
            value={form.observer_height_m}
            onChange={v => upd('observer_height_m', v)}
            placeholder="1.5"
            min={0}
            max={500}
            step="0.5"
          />
        </FieldRow>

        {/* Distance preview */}
        <div style={{ fontSize: 10, color: '#444d56', marginBottom: 6, marginTop: 2 }}>
          Est. range:{' '}
          {previewDist !== null
            ? <span style={{ color: '#8b949e' }}>
                {previewDist >= 1000
                  ? `${(previewDist / 1000).toFixed(2)} km`
                  : `${Math.round(previewDist)} m`}
              </span>
            : <span style={{ color: '#484f58', fontStyle: 'italic' }}>
                {txPowerKnown ? '—' : 'unknown (no TX power)'}
              </span>
          }
        </div>

        {formError && (
          <div style={{ fontSize: 10, color: '#ef4444', marginBottom: 6 }}>{formError}</div>
        )}

        {editingLobId ? (
          <div style={{ display: 'flex', gap: 6 }}>
            <button className="btn btn-primary" style={{ flex: 1, gap: 6, fontSize: 12 }} onClick={handleUpdate}>
              <Pencil size={13} /> Update LoB
            </button>
            <button className="btn btn-secondary" style={{ gap: 6, fontSize: 12, padding: '6px 12px' }} onClick={cancelEdit}>
              Cancel
            </button>
          </div>
        ) : (
          <button className="btn btn-primary" style={{ width: '100%', gap: 6, fontSize: 12 }} onClick={handleAdd}>
            <Plus size={13} /> Add LoB
          </button>
        )}
      </div>

      {/* ── Group / emitter summary with per-group CAP ──────────────────── */}
      {lobGroups.some(g => g.lobs.length >= 2) && (
        <div style={{ padding: '8px 12px' }}>
          <div style={{ fontSize: 10, fontWeight: 600, color: '#8b949e', marginBottom: 6 }}>
            EMITTER ESTIMATES
          </div>
          {lobGroups.map((grp, i) => {
            if (grp.lobs.length < 2) return null
            const inters = computeGroupIntersections(grp)
            const centroid = computeCentroid(inters)
            const isFix = grp.lobs.length >= 3
            const label = isFix ? 'FIX' : 'CUT'
            const color = isFix ? '#ef4444' : '#06d6a0'
            const gKey = lobGroupKey(grp)
            const capVisible = capGroups[gKey] !== false  // default true
            return (
              <div
                key={i}
                style={{
                  background: '#0d1117',
                  border: `1px solid ${color}30`,
                  borderLeft: `3px solid ${color}`,
                  borderRadius: 4, padding: '6px 8px', marginBottom: 4,
                }}
              >
                <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 2 }}>
                  <span style={{ fontSize: 11, fontWeight: 700, color }}>
                    {label} · {(grp.frequency_hz / 1e6).toFixed(3)} MHz
                  </span>
                  <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
                    <span style={{ fontSize: 10, color: '#8b949e' }}>{grp.lobs.length} LoBs</span>
                    <button
                      className={`btn ${capVisible ? 'btn-primary' : 'btn-ghost'}`}
                      style={{ padding: '1px 6px', fontSize: 10 }}
                      title={capVisible ? 'Hide CAP ellipse' : 'Show CAP ellipse'}
                      onClick={() => onToggleCAP(gKey)}
                    >
                      <Target size={10} style={{ display: 'inline', marginRight: 3 }} />CAP
                    </button>
                  </div>
                </div>
                {grp.device_id && (
                  <div style={{ fontSize: 10, color: '#a78bfa', marginBottom: 2 }}>
                    {DEVICE_TYPES.find(t => t.value === grp.device_type)?.label || 'ID'}: {grp.device_id}
                  </div>
                )}
                {centroid ? (
                  <div style={{ fontSize: 10, color: '#8b949e' }}>
                    Est. {centroid.lat.toFixed(5)}, {centroid.lon.toFixed(5)}
                  </div>
                ) : (
                  <div style={{ fontSize: 10, color: '#ef4444' }}>
                    No intersection found (parallel bearings?)
                  </div>
                )}
              </div>
            )
          })}
        </div>
      )}

    </div>
  )
}
