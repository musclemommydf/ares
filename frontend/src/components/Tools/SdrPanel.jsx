// SPDX-License-Identifier: MIT OR Apache-2.0
// Copyright (c) 2026 Ares

/**
 * SDR console (Workstream D).
 *
 * Operator UI for connecting physical direction-finding radios — KrakenSDR
 * (krakensdr_doa CSV polling), Epiq Matchstiq X40 (external DF pipeline pushes
 * JSON-lines over TCP), or any "generic" JSON-lines source — and watching the
 * server's live DF picture: bearings stream in, the geolocation solver groups
 * them by frequency and computes Cut/Fix/CEP, ATAK gets CoT for free, and a
 * coverage simulation can rerun on every confirmed fix.
 *
 * Reads/writes via `/api/v1/sdr/*` and subscribes to `WS /api/v1/sdr/stream`.
 * Lifts the live `features` and the latest auto-coverage GeoJSON up to App.jsx
 * so the 2D + 3D maps render them through the existing `geolocationGeoJSON`
 * pipeline (no map code changes needed).
 */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { X, Plus, RefreshCw, Trash2, Wifi, WifiOff, AlertCircle, Activity, Radio, Save, Cpu, Network, Crosshair, Terminal, Copy, Pencil } from 'lucide-react'
import CellularPanel from './CellularPanel'
import PentestTools from '../Cyber/PentestTools'
import {
  listSdrDevices, createSdrDevice, updateSdrDevice, deleteSdrDevice, testSdrDevice,
  getDfAccuracyEstimate, getGpsFix, setGpsFix,
  getGpsSource, setGpsSource, getDfIqBackend,
  addSdrPeer, removeSdrPeer, getSdrPeers,
  getDfDrivers, startLiveDf, updateLiveDf, dfAntennas, dfSaveAntenna, dfDeleteAntenna, dfArrayEstimate, dfLiveCalibrate,
  listSdrNics, createSdrNic, deleteSdrNic,
} from '../../api/client'

// DF method labels (super-resolution + the classic ALARIS-class estimators)
const DF_METHODS = [
  { id: 'music', label: 'MUSIC (super-resolution)' },
  { id: 'capon', label: 'Capon / MVDR' },
  { id: 'bartlett', label: 'Bartlett (beamformer)' },
  { id: 'correlative', label: 'Correlative DF (CDF / CIDF)' },
  { id: 'watson_watt', label: 'Watson-Watt (Adcock)' },
  { id: 'doppler', label: 'Pseudo-Doppler (phase-mode)' },
]

// One-line "what it is + when to pick it" for each DF method, shown under the
// method picker (and expanded as a comparison list via the ? toggle).
const DF_METHOD_HELP = {
  music: 'Super-resolution eigenstructure method — sharpest bearings and the only one that cleanly splits two co-channel emitters. Needs a calibrated coherent array (≥3 ch) and decent SNR. The default for KrakenSDR / coherent-USRP DF.',
  capon: 'MVDR adaptive beamformer — higher resolution than Bartlett, more forgiving than MUSIC when you don’t know how many signals are present. A solid general-purpose choice on a coherent array with mild calibration error.',
  bartlett: 'Conventional beamformer — lowest resolution but the most tolerant of calibration/model error and low SNR. Pick it as a robust fallback, for one dominant emitter, or when MUSIC/Capon look jumpy.',
  correlative: 'Correlative interferometry (CDF/CIDF) — matches measured phases against a stored array manifold, so it handles arbitrary/large arrays and wideband signals. Pick it for ALARIS-style heads and when you have a measured calibration table.',
  watson_watt: 'Watson-Watt on a crossed Adcock pair (+ sense) — the compact low-channel classic for HF/VHF. Single-emitter only and lower accuracy, but works with just 2–3 channels and a small antenna.',
  doppler: 'Pseudo-Doppler / phase-mode — emulates a spinning antenna from a switched ring. Cheap and fast to acquire but coarse and single-emitter; good for quick bearings on a UCA when precision isn’t critical.',
}

// Per-level colours for the event/error console.
const LOG_COLOR = { ok: '#3fb950', info: '#8b949e', warn: '#f0883e', error: '#f85149' }

// Drivers whose constructor takes a connection string, and the kwarg name for it.
// (plutosdr/antsdr expect `uri`, UHD expects `args`; others have no addressable URI.)
const DRIVER_URI_KEY = { plutosdr: 'uri', antsdr_e200: 'uri', uhd_usrp: 'args' }
const DRIVER_URI_PLACEHOLDER = { plutosdr: 'ip:192.168.2.1  (or usb:)', antsdr_e200: 'ip:192.168.1.10', uhd_usrp: 'addr=192.168.10.2' }

const GPS_SOURCES = [
  { id: 'manual', label: 'Manual (type a position)' },
  { id: 'browser', label: 'This computer (browser geolocation)' },
  { id: 'gpsd', label: 'USB GPS via gpsd (localhost:2947)' },
  { id: 'serial', label: 'USB GPS — raw serial NMEA (/dev/ttyUSB0…)' },
  { id: 'sdr', label: "SDR's GPSDO / GNSS sensors" },
]

const DEVICE_TYPES = [
  { id: 'krakensdr',     label: 'KrakenSDR (krakensdr_doa)',          defaultPort: 8080 },
  { id: 'matchstiq_x40', label: 'Epiq Matchstiq X40 (external DF)',   defaultPort: 8401 },
  { id: 'generic',       label: 'Generic JSON-lines TCP',             defaultPort: 8400 },
]

const inputStyle = { background: '#0d1117', border: '1px solid #30363d', borderRadius: 4, color: '#e6edf3', fontSize: 12, padding: '4px 6px', width: '100%', minWidth: 0, boxSizing: 'border-box' }
const btn = { background: '#21262d', border: '1px solid #30363d', borderRadius: 4, color: '#c9d1d9', padding: '4px 8px', cursor: 'pointer', fontSize: 12, display: 'inline-flex', alignItems: 'center', gap: 4 }

export default function SdrPanel({ onClose, mapCenter, sdr, onPickLocation, mapFeatures = [], hidden = false }) {
  // Shared, always-on SDR feed (the WS subscription lives in App via useSdrStream).
  const { devices, setDevices, lobs, gps, setGps, mesh, setMesh, wsState, wsError } = sdr
  const [gpsInput, setGpsInput] = useState({ lat: '', lon: '' })
  const [gpsSrc, setGpsSrc] = useState(null)              // backend GPS-source status
  const [gpsSrcForm, setGpsSrcForm] = useState({ kind: 'manual', host: '127.0.0.1', port: 2947, path: '/dev/ttyUSB0', baud: 9600, device_args: '' })
  const [gpsWatchId, setGpsWatchId] = useState(null)      // navigator.geolocation.watchPosition id (browser source)
  const [iqBackend, setIqBackend] = useState(null)        // /df/iq_backend — native IQ capture status + SDR(s) seen by SoapySDR
  const [peerInput, setPeerInput] = useState('')
  const [errText, setErrTextRaw] = useState(null)        // transient banner (latest message)
  // ── event/error console ──
  // Every message that flows through setErrText, plus WS-stream lifecycle and
  // per-device streaming errors, is appended here so the operator can scroll
  // back through everything instead of seeing only the last line.
  const [log, setLog] = useState([])                     // [{ id, t, level, msg }] (level: ok|info|warn|error)
  const [logOpen, setLogOpen] = useState(true)
  const [logErrorsOnly, setLogErrorsOnly] = useState(false)
  const logRef = useRef(null)
  const seenDevErr = useRef({})                          // device.id → last logged last_error (dedupe)
  const pushLog = useCallback((msg, level = 'error') => {
    if (!msg) return
    setLog(prev => {
      const last = prev[prev.length - 1]
      if (last && last.msg === msg && last.level === level) return prev   // drop consecutive dupes
      return [...prev.slice(-299), { id: `${Date.now()}-${Math.random().toString(36).slice(2, 7)}`, t: Date.now(), level, msg: String(msg) }]
    })
  }, [])
  // Wrap setErrText so every existing call site both shows the banner AND logs.
  const setErrText = useCallback((msg) => {
    setErrTextRaw(msg)
    if (msg) pushLog(msg, msg.startsWith('✓') ? 'ok' : msg.startsWith('⚠') ? 'warn' : 'error')
  }, [pushLog])
  const copyLog = () => {
    const text = log.map(e => `${new Date(e.t).toISOString()} ${e.level.toUpperCase().padEnd(5)} ${e.msg}`).join('\n')
    if (navigator.clipboard?.writeText) navigator.clipboard.writeText(text).then(() => setErrTextRaw(`✓ copied ${log.length} log lines`)).catch(() => setErrTextRaw('⚠ clipboard unavailable'))
    else setErrTextRaw('⚠ clipboard unavailable')
  }
  const clearLog = () => { setLog([]); seenDevErr.current = {} }
  const [adding, setAdding] = useState(false)
  const [addChooser, setAddChooser] = useState(false)    // unified "Add device" → pick a role/mode
  const [form, setForm] = useState(() => blankForm(mapCenter))
  const [editId, setEditId] = useState(null)             // editing an external-pipeline device (else null = adding)
  const [accEst, setAccEst] = useState(null)
  // ── live-DF (built-in driver → in-process bearing) ──
  const [drivers, setDrivers] = useState([])              // /df/drivers registry
  const [liveAdding, setLiveAdding] = useState(false)
  const [liveForm, setLiveForm] = useState(() => blankLiveForm(mapCenter))
  const [editLiveId, setEditLiveId] = useState(null)     // editing a live-DF device (else null = adding)
  const [liveAccEst, setLiveAccEst] = useState(null)
  const [methodHelp, setMethodHelp] = useState(false)    // expand the DF-method comparison
  const [antennas, setAntennas] = useState([])           // /df/antennas catalog (ALARIS + others)
  const liveDriver = useMemo(() => drivers.find(d => d.id === liveForm.driver_id) || null, [drivers, liveForm.driver_id])
  // ── SDR-as-NIC (TAP/TUN over RF) ──
  const [nicInfo, setNicInfo] = useState(null)            // { supported, reason, nics, tx_drivers }
  const [nicAdding, setNicAdding] = useState(false)
  const [nicForm, setNicForm] = useState(() => blankNicForm())
  const nicDriver = useMemo(() => drivers.find(d => d.id === nicForm.driver_id) || null, [drivers, nicForm.driver_id])
  // refresh the LoB-accuracy estimate as the array config changes (in the add-device form)
  useEffect(() => {
    if (!adding || form.source_class !== 'multi_channel') { setAccEst(null); return }
    let stop = false
    getDfAccuracyEstimate({ channels: form.channels, array_type: form.array_type,
      spacing_wavelengths: form.array_spacing_wavelengths, frequency_hz: Number(form.frequency_hz) || 433.92e6 })
      .then(r => { if (!stop) setAccEst(r) }).catch(() => { if (!stop) setAccEst(null) })
    return () => { stop = true }
  }, [adding, form.source_class, form.channels, form.array_type, form.array_spacing_wavelengths, form.frequency_hz])

  // accuracy estimate for the live-DF form (channels/array/freq drive expected LoB σ)
  useEffect(() => {
    if (!liveAdding) { setLiveAccEst(null); return }
    let stop = false
    getDfAccuracyEstimate({ channels: liveForm.channels, array_type: liveForm.array_type,
      spacing_wavelengths: liveForm.array_spacing_wavelengths, frequency_hz: Number(liveForm.frequency_mhz) * 1e6 || 433.92e6 })
      .then(r => { if (!stop) setLiveAccEst(r) }).catch(() => { if (!stop) setLiveAccEst(null) })
    return () => { stop = true }
  }, [liveAdding, liveForm.channels, liveForm.array_type, liveForm.array_spacing_wavelengths, liveForm.frequency_mhz])

  // Log WS-stream lifecycle transitions (the header chip only shows the latest).
  useEffect(() => {
    if (wsState === 'open') pushLog('WebSocket stream connected', 'ok')
    else if (wsState === 'error') pushLog(`WebSocket stream error — ${wsError?.detail || 'connection failed or dropped'} (auto-retrying)`, 'error')
    // 'connecting' (initial) is left out to avoid noise on first mount
  }, [wsState, wsError, pushLog])

  // Log per-device streaming errors as they change (devices update often via the
  // LoB stream, so only append when a device's last_error actually changes).
  useEffect(() => {
    for (const d of devices) {
      const cur = d.last_error || ''
      if (seenDevErr.current[d.id] === cur) continue
      seenDevErr.current[d.id] = cur
      if (cur) pushLog(`${d.name || d.id}: ${cur}`, d.status === 'error' ? 'error' : 'warn')
    }
  }, [devices, pushLog])

  // Panel-specific one-shot loads + the NIC link-stat poll. The live SDR feed
  // (devices/LoBs/fixes/GPS + map features) is the always-on App-level
  // `useSdrStream` subscription — this panel just reads it via `sdr`.
  useEffect(() => {
    let cancelled = false
    ;(async () => {
      try {
        const g = await getGpsSource()
        if (!cancelled) { setGpsSrc(g); if (g?.kind) setGpsSrcForm(f => ({ ...f, kind: g.kind, ...(g.config || {}) })) }
      } catch { /* GPS-source endpoint optional */ }
      try { const b = await getDfIqBackend(); if (!cancelled) setIqBackend(b) } catch { /* IQ-backend endpoint optional */ }
      try { const d = await getDfDrivers(); if (!cancelled) setDrivers(d.drivers || []) } catch { /* drivers endpoint optional */ }
      try { const a = await dfAntennas(); if (!cancelled) setAntennas(a.antennas || []) } catch { /* antenna catalog optional */ }
      try { const n = await listSdrNics(); if (!cancelled) setNicInfo(n) } catch { /* nic endpoint optional */ }
    })()
    // poll NIC link stats (tx/rx frame counters) — there's no WS for these
    const nicTimer = setInterval(() => { if (document.hidden) return; listSdrNics().then(n => { if (!cancelled) setNicInfo(n) }).catch(() => {}) }, 3000)
    return () => { cancelled = true; clearInterval(nicTimer) }
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  const submit = async () => {
    setErrText(null)
    const payload = { ...form, port: Number(form.port) || 0, lat: Number(form.lat) || 0, lon: Number(form.lon) || 0,
      frequency_hz: Number(form.frequency_hz) || 0, channels: Number(form.channels) || (form.source_class === 'single_channel' ? 1 : 5),
      array_spacing_wavelengths: Number(form.array_spacing_wavelengths) || 0.4,
      antenna_heading_deg: Number(form.antenna_heading_deg) || 0, df_threshold_dbm: Number(form.df_threshold_dbm) || -90 }
    try {
      if (editId) {
        const { type, ...patch } = payload     // type is immutable on update
        await updateSdrDevice(editId, patch)
      } else {
        await createSdrDevice(payload)
      }
      setAdding(false); setEditId(null)
      setForm(blankForm(mapCenter))
      const s = await listSdrDevices(); setDevices(s.devices || [])
      setErrText(editId ? `✓ updated ${payload.name || editId}` : `✓ added ${payload.name}`)
    } catch (e) { setErrText(String(e?.response?.data?.detail || e?.message || e)) }
  }

  // Apply a catalogue antenna (e.g. an ALARIS DF head) to the live-DF form:
  // sets the array geometry, channel count, recommended DF method + a sane
  // start frequency, so an operator picks the antenna and the rig is configured.
  const applyAntenna = (ant) => {
    if (!ant) { setLiveForm(f => ({ ...f, antenna_id: '' })); return }
    const isCustom = ant.geometry === 'custom' && Array.isArray(ant.positions_m)
    const geom = isCustom ? 'custom' : (ant.geometry === 'adcock' ? 'adcock' : (ant.geometry === 'ula' ? 'ula' : 'uca'))
    const ring = Number(ant.n_elements) || (geom === 'adcock' ? 4 : 5)
    const channels = isCustom ? ant.positions_m.length : (geom === 'adcock' ? ring + (ant.sense ? 1 : 0) : ring)
    const fMid = (Number(ant.freq_min_hz || 0) + Number(ant.freq_max_hz || 0)) / 2e6
    setLiveForm(f => ({
      ...f, antenna_id: ant.id,
      array_type: geom,
      array_sense: ant.sense !== false,
      array_radius_m: ant.radius_m || '',
      custom_positions: isCustom ? ant.positions_m.map(p => [p[0], p[1], p[2] || 0]) : f.custom_positions,
      channels: Math.max(2, channels),
      method: DF_METHODS.some(m => m.id === ant.recommended_method) ? ant.recommended_method
              : (ant.df_methods || []).find(m => DF_METHODS.some(x => x.id === m)) || f.method,
      frequency_mhz: fMid ? String(Math.round(fMid)) : f.frequency_mhz,
    }))
  }

  const calibrateDevice = async (id) => {
    try { await dfLiveCalibrate(id); setErrText('✓ calibration requested') }
    catch (e) { setErrText(String(e?.response?.data?.detail || e?.message || e)) }
  }
  const refreshAntennas = async () => { try { const a = await dfAntennas(); setAntennas(a.antennas || []) } catch { /* optional */ } }
  const removeAntenna = async (id) => {
    try { await dfDeleteAntenna(id); await refreshAntennas(); setLiveForm(f => ({ ...f, antenna_id: '' })); setErrText(`✓ deleted antenna ${id}`) }
    catch (e) { setErrText(String(e?.response?.data?.detail || e?.message || e)) }
  }

  const submitLive = async () => {
    setErrText(null)
    const f = liveForm
    const uriKey = DRIVER_URI_KEY[f.driver_id]
    const driver_args = (uriKey && f.uri.trim()) ? { [uriKey]: f.uri.trim() } : {}
    const customPos = (f.array_type === 'custom' && f.custom_positions?.length >= 2)
      ? f.custom_positions.map(p => [Number(p[0]) || 0, Number(p[1]) || 0, Number(p[2]) || 0]) : null
    if (f.array_type === 'custom' && !customPos) { setErrText('custom array needs ≥2 elements — add element positions first'); return }
    const body = {
      driver_id: f.driver_id, name: f.name || `live-${f.driver_id}`,
      frequency_hz: Number(f.frequency_mhz) * 1e6 || 0,
      channels: customPos ? customPos.length : Math.max(2, Number(f.channels) || 2),
      array_type: f.array_type, array_spacing_wavelengths: Number(f.array_spacing_wavelengths) || 0.4,
      array_sense: f.array_sense !== false,
      array_radius_m: f.array_radius_m ? Number(f.array_radius_m) : null,
      array_positions_m: customPos,
      sample_rate_hz: Number(f.sample_rate_mhz) * 1e6 || 2.4e6,
      gain_db: f.gain_db === '' ? null : Number(f.gain_db),
      method: f.method, dwell_s: Number(f.dwell_s) || 1.0,
      antenna_heading_deg: Number(f.antenna_heading_deg) || 0,
      lat: Number(f.lat) || 0, lon: Number(f.lon) || 0,
      use_gps: f.use_gps,
      min_snr_db: Number(f.min_snr_db), min_quality: Number(f.min_quality),
      auto_squelch: f.auto_squelch, auto_calibrate: f.auto_calibrate,
      cal_interval_s: Number(f.cal_interval_s) || 300,
      vfos: (f.vfos || []).filter(v => v.offset_mhz !== '' && v.offset_mhz != null).map((v, i) => ({
        name: v.name || `vfo${i}`, offset_hz: Number(v.offset_mhz) * 1e6 || 0,
        bandwidth_hz: Number(v.bw_khz) * 1e3 || 0,
        squelch_db: (v.squelch_db === '' || v.squelch_db == null) ? null : Number(v.squelch_db),
      })),
      driver_args,
    }
    try {
      const r = editLiveId ? await updateLiveDf(editLiveId, body) : await startLiveDf(body)
      setLiveAdding(false); setEditLiveId(null)
      setLiveForm(blankLiveForm(mapCenter))
      const s = await listSdrDevices(); setDevices(s.devices || [])
      setErrText(editLiveId ? `✓ live DF updated: ${r.device?.name || body.name}` : `✓ live DF started on ${r.device?.name || f.driver_id}`)
    } catch (e) { setErrText(String(e?.response?.data?.detail || e?.message || e)) }
  }

  // Open the appropriate add-form pre-filled with a device's current parameters,
  // in edit mode — Save then PUTs an update (and re-spawns) instead of creating.
  const openEditDevice = (d) => {
    setErrText(null)
    setAddChooser(false); setNicAdding(false)
    if (d.type === 'live_df') {
      const md = d.metadata || {}
      const arr = md.array || {}
      const isCustom = arr.type === 'custom' && Array.isArray(arr.positions_m)
      setEditId(null); setAdding(false)
      setLiveForm({
        ...blankLiveForm(mapCenter),
        driver_id: md.driver_id || 'plutosdr',
        name: d.name || '',
        uri: (md.driver_args && (md.driver_args.uri || md.driver_args.args)) || '',
        antenna_id: '',
        array_type: isCustom ? 'custom' : (arr.type === 'adcock' ? 'adcock' : (d.array_type || 'uca')),
        array_sense: arr.sense !== false,
        array_radius_m: arr.radius_m != null ? String(arr.radius_m) : '',
        custom_positions: isCustom ? arr.positions_m.map(p => [p[0], p[1], p[2] || 0]) : [],
        frequency_mhz: d.frequency_hz ? String(d.frequency_hz / 1e6) : '433.92',
        channels: Math.max(2, Number(d.channels) || 2),
        array_spacing_wavelengths: d.array_spacing_wavelengths ?? 0.4,
        sample_rate_mhz: (Number(md.sample_rate_hz) || 2.4e6) / 1e6,
        gain_db: md.gain_db == null ? '' : md.gain_db,
        method: md.method || 'music',
        dwell_s: md.dwell_s ?? 1.0,
        antenna_heading_deg: d.antenna_heading_deg ?? 0,
        min_snr_db: md.min_snr_db ?? 3,
        min_quality: md.min_quality ?? 0.1,
        auto_squelch: !!md.auto_squelch,
        auto_calibrate: !!md.auto_calibrate,
        cal_interval_s: md.cal_interval_s ?? 300,
        vfos: (md.vfos || []).map((v, i) => ({
          name: v.name || `vfo${i}`,
          offset_mhz: v.offset_hz != null ? v.offset_hz / 1e6 : '',
          bw_khz: v.bandwidth_hz != null ? v.bandwidth_hz / 1e3 : '',
          squelch_db: v.squelch_db == null ? '' : v.squelch_db,
        })),
        lat: d.lat ?? mapCenter?.lat ?? 0,
        lon: d.lon ?? mapCenter?.lon ?? 0,
        use_gps: d.use_gps !== false,
      })
      setEditLiveId(d.id)
      setLiveAdding(true)
    } else {
      setEditLiveId(null); setLiveAdding(false)
      setForm({
        ...blankForm(mapCenter),
        name: d.name || '', type: d.type || 'generic', host: d.host || '', port: d.port || 0,
        source_class: d.source_class || 'multi_channel',
        channels: d.channels ?? 5,
        array_type: d.array_type || 'uca',
        array_spacing_wavelengths: d.array_spacing_wavelengths ?? 0.4,
        azimuth_reference: (d.azimuth_reference === 'relative' || d.azimuth_reference === 'clock') ? 'relative' : 'true',
        antenna_heading_deg: d.antenna_heading_deg ?? 0,
        lat: d.lat ?? mapCenter?.lat ?? 0,
        lon: d.lon ?? mapCenter?.lon ?? 0,
        frequency_hz: d.frequency_hz ?? 0,
        df_threshold_dbm: d.df_threshold_dbm ?? -90,
        use_gps: d.use_gps !== false,
      })
      setEditId(d.id)
      setAdding(true)
    }
  }

  const submitNic = async () => {
    setErrText(null)
    const f = nicForm
    const uriKey = DRIVER_URI_KEY[f.driver_id]
    const driver_args = (uriKey && f.uri.trim()) ? { [uriKey]: f.uri.trim() } : {}
    try {
      const r = await createSdrNic({
        name: f.name || undefined, driver_id: f.driver_id, driver_args,
        mode: f.mode, ifname: f.ifname || 'ares-nic%d',
        ip_cidr: f.ip_cidr.trim() || null,
        frequency_hz: Number(f.frequency_mhz) * 1e6 || 433.92e6,
        sample_rate_hz: Number(f.sample_rate_mhz) * 1e6 || 2.4e6,
        gain_db: f.gain_db === '' ? null : Number(f.gain_db),
        sps: Number(f.sps) || 8, mtu: Number(f.mtu) || 1400,
      })
      setNicAdding(false)
      setNicForm(blankNicForm())
      const n = await listSdrNics(); setNicInfo(n)
      setErrText(`✓ NIC up: ${r.ifname}${r.config_warning ? ` (config: ${r.config_warning})` : ''}`)
    } catch (e) { setErrText(String(e?.response?.data?.detail || e?.message || e)) }
  }

  const removeNic = async (id) => {
    try { await deleteSdrNic(id); const n = await listSdrNics(); setNicInfo(n) }
    catch (e) { setErrText(String(e?.response?.data?.detail || e?.message || e)) }
  }

  const sendGps = async () => {
    const lat = parseFloat(gpsInput.lat), lon = parseFloat(gpsInput.lon)
    if (isNaN(lat) || isNaN(lon)) { setErrText('GPS: enter a lat and lon'); return }
    try { const r = await setGpsFix({ lat, lon, source: 'manual' }); setGps(r.fix); setErrText(`✓ GPS set: ${lat.toFixed(5)}, ${lon.toFixed(5)}`) }
    catch (e) { setErrText(String(e?.response?.data?.detail || e?.message || e)) }
  }
  const useMapCenterAsGps = () => { if (mapCenter) setGpsInput({ lat: String(mapCenter.lat), lon: String(mapCenter.lon) }) }

  // ── GPS source picker (this computer / USB GPS via gpsd or serial NMEA / an SDR's GPSDO) ──
  const pushBrowserFix = (pos) => {
    const c = pos?.coords; if (!c) return
    setGpsFix({ lat: c.latitude, lon: c.longitude, alt_m: c.altitude || 0,
                heading_deg: (c.heading != null && !isNaN(c.heading)) ? c.heading : undefined,
                speed_mps: (c.speed != null && !isNaN(c.speed)) ? c.speed : undefined, source: 'browser' })
      .then(r => { setGps(r.fix); setErrText(`✓ device GPS: ${c.latitude.toFixed(5)}, ${c.longitude.toFixed(5)} (±${Math.round(c.accuracy || 0)} m)`) })
      .catch(e => setErrText(String(e?.response?.data?.detail || e?.message || e)))
  }
  const stopBrowserWatch = () => { if (gpsWatchId != null && navigator.geolocation) { navigator.geolocation.clearWatch(gpsWatchId); setGpsWatchId(null) } }
  const useThisDevice = (track) => {
    if (!navigator.geolocation) { setErrText('this browser has no Geolocation API — use Manual, a USB GPS (gpsd/serial), or an SDR GPSDO below'); return }
    stopBrowserWatch()
    let watchId = null
    const onErr = (e) => {
      setErrText(geoErrMsg(e))
      // PERMISSION_DENIED / POSITION_UNAVAILABLE won't recover on their own (offline
      // box or the desktop app have no network-location service) — stop the watch so
      // it doesn't spam, and drop the picker out of the "running" state.
      if (e?.code === 1 || e?.code === 2) {
        if (watchId != null && navigator.geolocation) navigator.geolocation.clearWatch(watchId)
        setGpsWatchId(null)
        setGpsSrc(s => ({ ...(s || {}), kind: 'browser', running: false }))
      }
    }
    if (track) {
      watchId = navigator.geolocation.watchPosition(pushBrowserFix, onErr,
        { enableHighAccuracy: true, maximumAge: 5000, timeout: 15000 })
      setGpsWatchId(watchId)
      setGpsSrc(s => ({ ...(s || {}), kind: 'browser', running: true }))
    } else {
      navigator.geolocation.getCurrentPosition(pushBrowserFix, onErr,
        { enableHighAccuracy: true, timeout: 15000 })
    }
  }
  const applyGpsSource = async () => {
    const f = gpsSrcForm
    stopBrowserWatch()
    if (f.kind === 'browser') { useThisDevice(true); return }
    try {
      const r = await setGpsSource({ kind: f.kind, host: f.host, port: Number(f.port) || 2947,
        path: f.path, baud: Number(f.baud) || 9600, device_args: f.device_args })
      setGpsSrc(r)
      setErrText(r.kind === 'off' || r.kind === 'manual'
        ? `GPS source: ${r.kind}` : `GPS source started: ${r.kind}${r.last_error ? ` — ${r.last_error}` : ''}`)
    } catch (e) { setErrText(String(e?.response?.data?.detail || e?.message || e)) }
  }

  // ── Native AoA from a connected SDR (or the synthetic coherent fallback) ──
  const refreshIqBackend = async () => { try { setIqBackend(await getDfIqBackend()) } catch {} }

  const addPeer = async () => {
    const u = peerInput.trim(); if (!u) return
    try { const r = await addSdrPeer(u); setPeerInput(''); const p = await getSdrPeers(); setMesh({ node_id: p.node_id, node_label: p.node_label, peers: p.status || [] }); setErrText(`✓ peer added: ${r.added}`) }
    catch (e) { setErrText(String(e?.response?.data?.detail || e?.message || e)) }
  }
  const delPeer = async (url) => {
    try { await removeSdrPeer(url); const p = await getSdrPeers(); setMesh({ node_id: p.node_id, node_label: p.node_label, peers: p.status || [] }) }
    catch (e) { setErrText(String(e?.message || e)) }
  }

  const toggle = async (d, patch) => {
    try {
      const updated = await updateSdrDevice(d.id, patch)
      setDevices(prev => prev.map(x => x.id === d.id ? updated : x))
    } catch (e) { setErrText(String(e?.message || e)) }
  }

  const remove = async (id) => {
    if (!confirm(`Remove SDR device ${id}?`)) return
    try { await deleteSdrDevice(id); setDevices(prev => prev.filter(d => d.id !== id)) }
    catch (e) { setErrText(String(e?.message || e)) }
  }

  const probe = async (id) => {
    setErrText(null)
    try {
      const r = await testSdrDevice(id)
      setErrText(r.ok ? `✓ ${id}: reachable at ${r.host}:${r.port}` : `⚠ ${id}: ${r.error || 'unreachable'} (${r.host}:${r.port})`)
    } catch (e) { setErrText(String(e?.message || e)) }
  }

  const lobsByDev = useMemo(() => {
    const out = {}
    for (const l of lobs) (out[l.device_id] ||= []).push(l)
    return out
  }, [lobs])

  // Drop-a-point / attach-to-feature controls shared by the live + external forms.
  // `apply(lat, lon)` writes the picked coords into the right form.
  const locationControls = (apply) => (
    <span style={{ display: 'inline-flex', gap: 6, alignItems: 'center', flexWrap: 'wrap' }}>
      {onPickLocation && (
        <button type="button" style={btn} title="Hide this panel, click the map to drop the device position"
                onClick={() => onPickLocation((lat, lon) => apply(lat, lon))}>📍 Drop on map</button>
      )}
      {mapFeatures.length > 0 && (
        <select style={{ ...inputStyle, width: 'auto' }} value=""
                onChange={e => { const ft = mapFeatures[Number(e.target.value)]; if (ft) apply(ft.lat, ft.lon) }}
                title="Attach the device position to an existing map feature">
          <option value="">attach to feature…</option>
          {mapFeatures.map((ft, i) => <option key={i} value={i}>{ft.label} ({ft.lat.toFixed(3)}, {ft.lon.toFixed(3)})</option>)}
        </select>
      )}
    </span>
  )

  return (
    <div style={{ position: 'fixed', inset: 0, background: 'rgba(0,0,0,0.55)', zIndex: 2000,
                  display: hidden ? 'none' : 'flex', alignItems: 'flex-start', justifyContent: 'center', padding: '5vh 20px', overflowY: 'auto' }}>
      <div style={{ background: '#0d1117', border: '1px solid #30363d', borderRadius: 8, width: 720, maxWidth: '100%',
                    color: '#e6edf3', boxShadow: '0 20px 60px rgba(0,0,0,0.7)' }}>
        <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', padding: 12, borderBottom: '1px solid #21262d' }}>
          <h3 style={{ margin: 0, fontSize: 14, display: 'flex', alignItems: 'center', gap: 8 }}>
            <Radio size={16} /> SDR console
            <span title={wsState === 'error' && wsError?.detail ? `WebSocket stream: ${wsError.detail} — click for the event log` : 'WebSocket stream — click for the event log'}
                  onClick={() => { setLogOpen(true); setTimeout(() => logRef.current?.scrollIntoView({ behavior: 'smooth', block: 'nearest' }), 60) }}
                  style={{ fontSize: 11, cursor: 'pointer', color: wsState === 'open' ? '#3fb950' : wsState === 'error' ? '#f85149' : '#d29922' }}>
              {wsState === 'open' ? '● live' : wsState === 'error' ? '● error' : '● connecting'}
            </span>
            {log.some(e => e.level === 'error') && (
              <span title="errors logged — click for the event log"
                    onClick={() => { setLogOpen(true); setTimeout(() => logRef.current?.scrollIntoView({ behavior: 'smooth', block: 'nearest' }), 60) }}
                    style={{ fontSize: 10, cursor: 'pointer', color: '#f85149', border: '1px solid #f85149', borderRadius: 10, padding: '0 6px' }}>
                {log.filter(e => e.level === 'error').length} ⚠
              </span>
            )}
          </h3>
          <button style={btn} onClick={onClose}><X size={14} /></button>
        </div>

        <div style={{ padding: 12, display: 'flex', flexDirection: 'column', gap: 14 }}>

          <Section title="Devices">
            {devices.length === 0
              ? <div style={{ fontSize: 12, color: '#8b949e' }}>No SDR devices registered. Add one below to start streaming bearings.</div>
              : devices.map(d => {
                  const cal = d.metadata?.cal, vstat = d.metadata?.vfo_status
                  const calCapable = d.type === 'live_df' && drivers.find(x => x.id === d.metadata?.driver_id)?.cal_source
                  return (
                  <div key={d.id} style={{ padding: '4px 0', borderBottom: '1px solid #21262d', fontSize: 12 }}>
                   <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                    <span style={{ background: '#1f2937', color: '#9ca3af', borderRadius: 3, padding: '1px 5px', fontSize: 10, textTransform: 'uppercase' }}>{d.type}</span>
                    <span style={{ flex: 1 }}>
                      <strong>{d.name}</strong>
                      <span style={{ color: '#6e7681' }}> · {d.host}{d.port ? `:${d.port}` : ''}{d.frequency_hz ? ` · ${(d.frequency_hz / 1e6).toFixed(3)} MHz` : ''}</span>
                    </span>
                    {cal && <span title={`inter-channel correction: ${cal.max_phase_deg}° / ${cal.max_amp_db} dB${cal.age_s != null ? ` · ${cal.age_s}s ago` : ''}`}
                                  style={{ fontSize: 10, color: cal.state === 'calibrated' ? '#3fb950' : '#d29922', border: '1px solid #30363d', borderRadius: 3, padding: '0 4px' }}>
                      CAL {cal.state === 'calibrated' ? '✓' : '…'}</span>}
                    <span title={d.last_error || ''} style={{ display: 'inline-flex', alignItems: 'center', gap: 3,
                                  color: d.status === 'streaming' ? '#3fb950' : d.status === 'error' ? '#f85149' : '#d29922' }}>
                      {d.status === 'streaming' ? <Wifi size={12} /> : d.status === 'error' ? <AlertCircle size={12} /> : <WifiOff size={12} />}
                      {d.status} ({(lobsByDev[d.id] || []).length} LoBs)
                    </span>
                    <label style={{ color: '#8b949e', fontSize: 11, display: 'inline-flex', alignItems: 'center', gap: 3 }}>
                      <input type="checkbox" checked={d.enabled} onChange={(e) => toggle(d, { enabled: e.target.checked })} /> on
                    </label>
                    {calCapable && <button style={btn} title="Force coherence (re)calibration" onClick={() => calibrateDevice(d.id)}><Crosshair size={12} /></button>}
                    {d.type !== 'live_df' && <button style={btn} title="Probe TCP connection" onClick={() => probe(d.id)}><Activity size={12} /></button>}
                    <button style={btn} title="Edit device parameters" onClick={() => openEditDevice(d)}><Pencil size={12} /></button>
                    <button style={btn} title="Remove device" onClick={() => remove(d.id)}><Trash2 size={12} color="#f85149" /></button>
                   </div>
                   {vstat?.length > 0 && (
                     <div style={{ display: 'flex', flexWrap: 'wrap', gap: 5, marginTop: 3, paddingLeft: 4 }}>
                       {vstat.map(v => (
                         <span key={v.name} title={`thr ${v.threshold_db ?? '—'} dBFS`}
                               style={{ fontSize: 10, borderRadius: 3, padding: '0 5px',
                                        background: v.open ? '#11271a' : '#1c1c22',
                                        color: v.bearing_deg != null ? '#3fb950' : (v.open ? '#d29922' : '#6e7681'),
                                        border: '1px solid #21262d' }}>
                           {v.name} {(v.freq_hz / 1e6).toFixed(3)} {v.open ? `${v.power_db}dBFS` : '🔇'}{v.bearing_deg != null ? ` ∠${v.bearing_deg}°` : ''}
                         </span>
                       ))}
                     </div>
                   )}
                  </div>
                )})}
            {adding ? (
              <div style={{ marginTop: 8, padding: 10, background: '#0b0f14', border: '1px solid #21262d', borderRadius: 6, display: 'grid', gridTemplateColumns: 'auto minmax(0,1fr) auto minmax(0,1fr)', gap: 6, alignItems: 'center', fontSize: 12 }}>
                {editId && <div style={{ gridColumn: '1 / -1', fontSize: 11, color: '#58a6ff' }}>Editing <strong>{form.name || editId}</strong> — saving re-applies these settings.</div>}
                <span style={{ color: '#8b949e' }}>name</span>
                <input style={inputStyle} value={form.name} onChange={e => setForm({ ...form, name: e.target.value })} placeholder="Kraken-1" />
                <span style={{ color: '#8b949e' }}>type</span>
                <select style={inputStyle} value={form.type} disabled={!!editId}
                        title={editId ? 'device type is fixed — remove and re-add to change it' : ''} onChange={e => {
                  const v = e.target.value
                  if (v.startsWith('live:')) {
                    // a built-in driver (Pluto, USRP, …) → configure it in the Live DF
                    // form below (in-process IQ→bearing, not an external host/port pipeline)
                    setAdding(false)
                    setLiveForm(f => ({ ...blankLiveForm(mapCenter), driver_id: v.slice(5) }))
                    setLiveAdding(true)
                    return
                  }
                  const t = DEVICE_TYPES.find(t => t.id === v)
                  setForm({ ...form, type: v, port: t?.defaultPort || 0 })
                }}>
                  <optgroup label="External DF pipeline (host / port)">
                    {DEVICE_TYPES.map(t => <option key={t.id} value={t.id}>{t.label}</option>)}
                  </optgroup>
                  {drivers.length > 0 && (
                    <optgroup label="Built-in driver — in-process DF (no daemon)">
                      {drivers.map(d => <option key={'live:' + d.id} value={'live:' + d.id}>{d.name}{d.coherent ? '' : ' (single-ch)'}</option>)}
                    </optgroup>
                  )}
                </select>
                <span style={{ color: '#8b949e' }}>source</span>
                <select style={inputStyle} value={form.source_class} onChange={e => setForm({ ...form, source_class: e.target.value, channels: e.target.value === 'single_channel' ? 1 : Math.max(2, form.channels) })}>
                  <option value="single_channel">Single channel — spectrum / audio only (no DF)</option>
                  <option value="multi_channel">Multi channel — DF (lines of bearing)</option>
                </select>
                {form.source_class === 'multi_channel' && <>
                  <span style={{ color: '#8b949e' }}>channels</span>
                  <input style={inputStyle} type="number" min={2} max={64} value={form.channels} onChange={e => setForm({ ...form, channels: Math.max(2, Math.min(64, Number(e.target.value) || 2)) })} />
                  <span style={{ color: '#8b949e' }}>array</span>
                  <select style={inputStyle} value={form.array_type} onChange={e => setForm({ ...form, array_type: e.target.value })}><option value="uca">circular (UCA)</option><option value="ula">linear (ULA)</option><option value="custom">custom</option></select>
                </>}
                <span style={{ color: '#8b949e' }}>host</span>
                <input style={inputStyle} value={form.host} onChange={e => setForm({ ...form, host: e.target.value })} placeholder="kraken.lan or 192.168.1.42" />
                <span style={{ color: '#8b949e' }}>port</span>
                <input style={inputStyle} value={form.port} onChange={e => setForm({ ...form, port: e.target.value })} placeholder="8080" />
                <span style={{ color: '#8b949e' }}>lat</span>
                <input style={inputStyle} value={form.lat} onChange={e => setForm({ ...form, lat: e.target.value })} placeholder="51.5" />
                <span style={{ color: '#8b949e' }}>lon</span>
                <input style={inputStyle} value={form.lon} onChange={e => setForm({ ...form, lon: e.target.value })} placeholder="-0.1" />
                <span style={{ color: '#8b949e' }}>set from</span>
                {locationControls((lat, lon) => setForm(f => ({ ...f, lat: lat.toFixed(6), lon: lon.toFixed(6) })))}
                <span style={{ color: '#8b949e' }}>freq Hz</span>
                <input style={inputStyle} value={form.frequency_hz} onChange={e => setForm({ ...form, frequency_hz: e.target.value })} placeholder="433920000" />
                {form.source_class === 'multi_channel' && <>
                  <span style={{ color: '#8b949e' }}>azimuth ref</span>
                  <select style={inputStyle} value={form.azimuth_reference} onChange={e => setForm({ ...form, azimuth_reference: e.target.value })}>
                    <option value="true">true north (degrees)</option>
                    <option value="relative">clock position off the antenna front</option>
                  </select>
                  {form.azimuth_reference === 'relative' && <>
                    <span style={{ color: '#8b949e' }}>antenna heading °</span>
                    <input style={inputStyle} type="number" value={form.antenna_heading_deg} onChange={e => setForm({ ...form, antenna_heading_deg: Number(e.target.value) || 0 })} />
                  </>}
                </>}
                <span style={{ color: '#8b949e' }}>use GPS</span>
                <label style={{ display: 'inline-flex', alignItems: 'center', gap: 4 }}>
                  <input type="checkbox" checked={form.use_gps} onChange={e => setForm({ ...form, use_gps: e.target.checked })} /> use the live GPS fix as this device's position
                </label>
                {form.source_class === 'multi_channel' && <>
                  <span style={{ color: '#8b949e', gridColumn: '1' }}>est. LoB accuracy</span>
                  <span style={{ gridColumn: '2 / -1', fontSize: 11, color: '#6e7681' }}>{accEst ? accEst.note : '…'}</span>
                </>}
                <div style={{ gridColumn: '1 / -1', display: 'flex', gap: 6, marginTop: 4 }}>
                  <button style={{ ...btn, background: '#1f6feb', borderColor: '#1f6feb' }} onClick={submit}><Save size={12} /> {editId ? 'Save changes' : 'Save'}</button>
                  <button style={btn} onClick={() => { setAdding(false); setEditId(null) }}>Cancel</button>
                </div>
              </div>
            ) : addChooser ? (
              <div style={{ marginTop: 8, padding: 10, background: '#0b0f14', border: '1px solid #21262d', borderRadius: 6, display: 'flex', flexDirection: 'column', gap: 6 }}>
                <div style={{ fontSize: 11, color: '#8b949e' }}>What should this device do?</div>
                <button style={{ ...btn, justifyContent: 'flex-start', textAlign: 'left' }}
                        onClick={() => { setAddChooser(false); setEditLiveId(null); setLiveForm(blankLiveForm(mapCenter)); setLiveAdding(true) }}>
                  <Cpu size={13} /> <span><strong>Direction finding — built-in driver</strong><br />
                    <span style={{ color: '#6e7681', fontSize: 10 }}>Ares pulls IQ off the radio (Pluto / USRP / Kraken / …) and runs MUSIC/Capon/Bartlett DF in-process. No external daemon.</span></span>
                </button>
                <button style={{ ...btn, justifyContent: 'flex-start', textAlign: 'left' }}
                        onClick={() => { setAddChooser(false); setEditId(null); setForm(blankForm(mapCenter)); setAdding(true) }}>
                  <Radio size={13} /> <span><strong>Direction finding — external pipeline</strong><br />
                    <span style={{ color: '#6e7681', fontSize: 10 }}>Ingest bearings from a KrakenSDR / Matchstiq / JSON-lines DF process over the network (host:port).</span></span>
                </button>
                <button style={{ ...btn, justifyContent: 'flex-start', textAlign: 'left' }}
                        disabled={nicInfo && !nicInfo.supported} title={nicInfo && !nicInfo.supported ? nicInfo.reason : ''}
                        onClick={() => { setAddChooser(false); setNicForm(blankNicForm()); setNicAdding(true) }}>
                  <Network size={13} /> <span><strong>Network bridge — SDR as a NIC</strong><br />
                    <span style={{ color: '#6e7681', fontSize: 10 }}>Carry a TAP/TUN kernel interface over RF via the built-in modem (not DF){nicInfo && !nicInfo.supported ? ' — unsupported on this host' : ''}.</span></span>
                </button>
                <button style={{ ...btn, alignSelf: 'flex-start' }} onClick={() => setAddChooser(false)}>Cancel</button>
              </div>
            ) : (
              <button style={{ ...btn, marginTop: 8 }} onClick={() => setAddChooser(true)}><Plus size={12} /> Add device</button>
            )}
          </Section>

          <Section title="Live DF — built-in driver (IQ → bearing, no external daemon)">
            <div style={{ fontSize: 11, color: '#8b949e', marginBottom: 6 }}>
              Pick a bundled SDR driver and Ares pulls coherent IQ off it and runs its own MUSIC/Capon/Bartlett solver
              in-process — bearings + fixes stream into the picture above (and to ATAK). Needs ≥2 coherent channels
              (e.g. a KrakenSDR/ANTSDR chain, a coherent USRP set, or a Pluto with the 2R2T mod). Started runs appear
              under <strong>Devices</strong>.
            </div>
            {liveAdding ? (
              <div style={{ padding: 10, background: '#0b0f14', border: '1px solid #21262d', borderRadius: 6, display: 'grid', gridTemplateColumns: 'auto minmax(0,1fr) auto minmax(0,1fr)', gap: 6, alignItems: 'center', fontSize: 12 }}>
                {editLiveId && <div style={{ gridColumn: '1 / -1', fontSize: 11, color: '#58a6ff' }}>Editing <strong>{liveForm.name || editLiveId}</strong> — saving re-applies these settings and restarts the capture.</div>}
                <span style={{ color: '#8b949e' }}>driver</span>
                <select style={inputStyle} value={liveForm.driver_id} onChange={e => {
                  const drv = drivers.find(x => x.id === e.target.value)
                  setLiveForm(f => ({ ...f, driver_id: e.target.value,
                    channels: Math.max(2, Math.min(drv?.max_channels || 2, f.channels)) }))
                }}>
                  {drivers.length === 0 && <option value="plutosdr">plutosdr</option>}
                  {drivers.map(d => <option key={d.id} value={d.id}>{d.name}{d.coherent ? '' : ' (single-channel)'}</option>)}
                </select>
                <span style={{ color: '#8b949e' }}>name</span>
                <input style={inputStyle} value={liveForm.name} onChange={e => setLiveForm(f => ({ ...f, name: e.target.value }))} placeholder={`live-${liveForm.driver_id}`} />

                {liveDriver && (
                  <div style={{ gridColumn: '1 / -1', fontSize: 10, color: liveDriver.coherent ? '#6e7681' : '#f0883e' }}>
                    {liveDriver.coherent ? `coherent · up to ${liveDriver.max_channels} ch` : '⚠ single-channel driver — cannot DF without a coherent multi-RX variant'}
                    {liveDriver.notes ? ` · ${liveDriver.notes}` : ''}
                  </div>
                )}

                {DRIVER_URI_KEY[liveForm.driver_id] && <>
                  <span style={{ color: '#8b949e' }}>device URI</span>
                  <input style={inputStyle} value={liveForm.uri} onChange={e => setLiveForm(f => ({ ...f, uri: e.target.value }))}
                         placeholder={DRIVER_URI_PLACEHOLDER[liveForm.driver_id] || ''} />
                </>}

                <span style={{ color: '#8b949e' }}>antenna</span>
                <select style={inputStyle} value={liveForm.antenna_id || ''}
                        onChange={e => applyAntenna(antennas.find(a => a.id === e.target.value))}>
                  <option value="">— custom / none —</option>
                  {antennas.filter(a => a.geometry !== 'directional').map(a => (
                    <option key={a.id} value={a.id}>
                      {(a.manufacturer || '').startsWith('ALARIS') ? `${a.model} · ` : ''}{a.name?.replace(/^ALARIS [^—]*— /, '') || a.id}
                    </option>
                  ))}
                </select>
                {liveForm.antenna_id && (() => {
                  const a = antennas.find(x => x.id === liveForm.antenna_id)
                  return a ? (
                    <div style={{ gridColumn: '1 / -1', fontSize: 10, color: '#6e7681', display: 'flex', alignItems: 'center', gap: 6 }}>
                      <span>{a.model} · {a.geometry}{a.sense ? '+sense' : ''} · {(a.freq_min_hz / 1e6).toFixed(0)}–{(a.freq_max_hz / 1e6).toFixed(0)} MHz ·
                      methods: {(a.df_methods || []).join(', ')}{a.representative_geometry ? ' · representative geometry — calibrate before use' : ''}</span>
                      {a.custom && <button style={{ ...btn, padding: '0 5px', fontSize: 10 }} title="Delete this saved antenna" onClick={() => removeAntenna(a.id)}><Trash2 size={11} color="#f85149" /></button>}
                    </div>
                  ) : null
                })()}

                <span style={{ color: '#8b949e' }}>freq MHz</span>
                <input style={inputStyle} type="number" value={liveForm.frequency_mhz} onChange={e => setLiveForm(f => ({ ...f, frequency_mhz: e.target.value }))} placeholder="433.92" />
                <span style={{ color: '#8b949e' }}>channels</span>
                <input style={inputStyle} type="number" min={2} max={liveDriver?.max_channels || 64} value={liveForm.channels}
                       onChange={e => setLiveForm(f => ({ ...f, channels: Math.max(2, Math.min(liveDriver?.max_channels || 64, Number(e.target.value) || 2)) }))} />

                <span style={{ color: '#8b949e' }}>array</span>
                <select style={inputStyle} value={liveForm.array_type} onChange={e => setLiveForm(f => ({ ...f, array_type: e.target.value }))}>
                  <option value="uca">circular (UCA)</option>
                  <option value="adcock">Adcock (crossed + sense)</option>
                  <option value="ula">linear (ULA)</option>
                  <option value="custom">custom / combination…</option>
                </select>
                <span style={{ color: '#8b949e' }}>{liveForm.array_type === 'custom' ? 'elements' : 'spacing λ'}</span>
                {liveForm.array_type === 'custom'
                  ? <input style={{ ...inputStyle, opacity: 0.6 }} value={`${liveForm.custom_positions?.length || 0} placed`} readOnly />
                  : <input style={inputStyle} type="number" step={0.05} value={liveForm.array_spacing_wavelengths} onChange={e => setLiveForm(f => ({ ...f, array_spacing_wavelengths: e.target.value }))} />}

                {liveForm.array_type === 'custom' && (
                  <div style={{ gridColumn: '1 / -1' }}>
                    <CustomArrayBuilder
                      positions={liveForm.custom_positions || []}
                      onChange={pos => setLiveForm(f => ({ ...f, custom_positions: pos, channels: Math.max(2, pos.length), antenna_id: '' }))}
                      antennas={antennas}
                      frequencyMhz={liveForm.frequency_mhz}
                      onSaved={async (p) => { await refreshAntennas(); setErrText(`✓ saved antenna ${p.id} (${p.n_elements} el)`) }}
                    />
                  </div>
                )}

                <span style={{ color: '#8b949e' }}>sample MHz</span>
                <input style={inputStyle} type="number" step={0.1} value={liveForm.sample_rate_mhz} onChange={e => setLiveForm(f => ({ ...f, sample_rate_mhz: e.target.value }))} />
                <span style={{ color: '#8b949e' }}>gain dB</span>
                <input style={inputStyle} value={liveForm.gain_db} onChange={e => setLiveForm(f => ({ ...f, gain_db: e.target.value }))} placeholder="blank = AGC" />

                <span style={{ color: '#8b949e', display: 'inline-flex', alignItems: 'center', gap: 4 }}>
                  method
                  <button type="button" style={{ ...btn, padding: '0 5px', lineHeight: 1.4 }} title="Compare the DF algorithms"
                          onClick={() => setMethodHelp(v => !v)}>?</button>
                </span>
                <select style={inputStyle} value={liveForm.method} onChange={e => setLiveForm(f => ({ ...f, method: e.target.value }))}>
                  {DF_METHODS.map(m => <option key={m.id} value={m.id}>{m.label}</option>)}
                </select>
                <span style={{ color: '#8b949e' }}>dwell s</span>
                <input style={inputStyle} type="number" step={0.1} value={liveForm.dwell_s} onChange={e => setLiveForm(f => ({ ...f, dwell_s: e.target.value }))} />
                {/* one-line hint for the selected method (updates as you change it) */}
                <span style={{ gridColumn: '1 / -1', fontSize: 10, color: '#6e7681', marginTop: -2 }}>
                  {DF_METHOD_HELP[liveForm.method]}
                </span>
                {methodHelp && (
                  <div style={{ gridColumn: '1 / -1', background: '#0d1117', border: '1px solid #21262d', borderRadius: 6,
                                padding: 8, display: 'flex', flexDirection: 'column', gap: 6, fontSize: 10, color: '#8b949e' }}>
                    <div style={{ color: '#6e7681' }}>Which one? Higher resolution needs more coherent channels, tighter calibration and SNR; the classic methods trade accuracy for working on cheaper/smaller arrays.</div>
                    {DF_METHODS.map(m => (
                      <div key={m.id} style={{ display: 'flex', gap: 6, alignItems: 'baseline', opacity: liveForm.method === m.id ? 1 : 0.8 }}>
                        <strong style={{ color: liveForm.method === m.id ? '#58a6ff' : '#c9d1d9', minWidth: 132, flexShrink: 0 }}>{m.label}</strong>
                        <span>{DF_METHOD_HELP[m.id]}</span>
                      </div>
                    ))}
                  </div>
                )}

                <span style={{ color: '#8b949e' }}>antenna heading °</span>
                <input style={inputStyle} type="number" value={liveForm.antenna_heading_deg} onChange={e => setLiveForm(f => ({ ...f, antenna_heading_deg: e.target.value }))} />
                <span style={{ color: '#8b949e' }} title="Don't shoot a bearing unless SNR / peak quality clear these">gate snr/qual</span>
                <span style={{ display: 'inline-flex', gap: 4 }}>
                  <input style={{ ...inputStyle, width: '50%' }} type="number" value={liveForm.min_snr_db} onChange={e => setLiveForm(f => ({ ...f, min_snr_db: e.target.value }))} title="min SNR dB" />
                  <input style={{ ...inputStyle, width: '50%' }} type="number" step={0.05} value={liveForm.min_quality} onChange={e => setLiveForm(f => ({ ...f, min_quality: e.target.value }))} title="min quality 0–1" />
                </span>

                <span style={{ color: '#8b949e' }}>location</span>
                <select style={inputStyle} value={liveForm.use_gps ? 'gps' : 'fixed'}
                        onChange={e => setLiveForm(f => ({ ...f, use_gps: e.target.value === 'gps' }))}>
                  <option value="gps">Track live GPS (follows the operator fix)</option>
                  <option value="fixed">Fixed coordinates</option>
                </select>
                {liveForm.use_gps
                  ? <span style={{ gridColumn: '2 / -1', fontSize: 10, color: '#6e7681' }}>Position follows the live GPS fix (set the source under <strong>GPS</strong> below) and moves with the operator.</span>
                  : <>
                      <span style={{ color: '#8b949e' }}>lat</span>
                      <input style={inputStyle} value={liveForm.lat} onChange={e => setLiveForm(f => ({ ...f, lat: e.target.value }))} placeholder="51.5" />
                      <span style={{ color: '#8b949e' }}>lon</span>
                      <input style={inputStyle} value={liveForm.lon} onChange={e => setLiveForm(f => ({ ...f, lon: e.target.value }))} placeholder="-0.1" />
                      <span style={{ color: '#8b949e' }}>set from</span>
                      {locationControls((lat, lon) => setLiveForm(f => ({ ...f, lat: lat.toFixed(6), lon: lon.toFixed(6) })))}
                    </>}

                {/* coherence auto-calibration (needs a driver with a noise source) */}
                <span style={{ color: '#8b949e' }} title="Periodically switch in the coherence source and correct inter-channel phase/gain drift">auto-cal</span>
                <span style={{ display: 'inline-flex', gap: 8, alignItems: 'center', fontSize: 11 }}>
                  <label style={{ display: 'inline-flex', alignItems: 'center', gap: 4 }}>
                    <input type="checkbox" disabled={!liveDriver?.cal_source}
                           checked={liveForm.auto_calibrate && !!liveDriver?.cal_source}
                           onChange={e => setLiveForm(f => ({ ...f, auto_calibrate: e.target.checked }))} />
                    {liveDriver?.cal_source ? 'recalibrate every' : 'driver has no calibration source'}
                  </label>
                  {liveDriver?.cal_source && <>
                    <input style={{ ...inputStyle, width: 64 }} type="number" value={liveForm.cal_interval_s}
                           onChange={e => setLiveForm(f => ({ ...f, cal_interval_s: e.target.value }))} /> s
                  </>}
                </span>

                {/* multi-VFO: DF several channels from one capture, with squelch */}
                <span style={{ color: '#8b949e' }} title="Direction-find several narrowband channels carved from one wideband capture">VFOs</span>
                <div style={{ fontSize: 11 }}>
                  <label style={{ display: 'inline-flex', alignItems: 'center', gap: 4, color: '#8b949e' }}>
                    <input type="checkbox" checked={liveForm.auto_squelch} onChange={e => setLiveForm(f => ({ ...f, auto_squelch: e.target.checked }))} />
                    auto-squelch (gate dead channels)
                  </label>
                  {(liveForm.vfos || []).map((v, i) => (
                    <div key={i} style={{ display: 'flex', gap: 4, marginTop: 3, alignItems: 'center' }}>
                      <input style={{ ...inputStyle, width: 54 }} placeholder="name" value={v.name || ''}
                             onChange={e => setLiveForm(f => { const vs = [...f.vfos]; vs[i] = { ...vs[i], name: e.target.value }; return { ...f, vfos: vs } })} />
                      <input style={{ ...inputStyle, width: 70 }} type="number" step={0.1} placeholder="offMHz" value={v.offset_mhz ?? ''}
                             onChange={e => setLiveForm(f => { const vs = [...f.vfos]; vs[i] = { ...vs[i], offset_mhz: e.target.value }; return { ...f, vfos: vs } })} title="offset from centre (MHz)" />
                      <input style={{ ...inputStyle, width: 64 }} type="number" placeholder="bwkHz" value={v.bw_khz ?? ''}
                             onChange={e => setLiveForm(f => { const vs = [...f.vfos]; vs[i] = { ...vs[i], bw_khz: e.target.value }; return { ...f, vfos: vs } })} title="bandwidth (kHz)" />
                      <input style={{ ...inputStyle, width: 60 }} type="number" placeholder="sqlch" value={v.squelch_db ?? ''}
                             onChange={e => setLiveForm(f => { const vs = [...f.vfos]; vs[i] = { ...vs[i], squelch_db: e.target.value }; return { ...f, vfos: vs } })} title="manual squelch (dBFS); blank = auto" />
                      <button style={{ ...btn, padding: '0 5px' }} onClick={() => setLiveForm(f => ({ ...f, vfos: f.vfos.filter((_, k) => k !== i) }))}><Trash2 size={11} color="#f85149" /></button>
                    </div>
                  ))}
                  <button style={{ ...btn, marginTop: 3 }} onClick={() => setLiveForm(f => ({ ...f, vfos: [...(f.vfos || []), { name: `vfo${f.vfos.length}`, offset_mhz: 0, bw_khz: 200, squelch_db: '' }] }))}>
                    <Plus size={11} /> VFO
                  </button>
                  {(liveForm.vfos || []).length === 0 && <span style={{ color: '#484f58', marginLeft: 6 }}>none → single full-band channel at the tune freq</span>}
                </div>

                <span style={{ color: '#8b949e', gridColumn: '1' }}>est. LoB accuracy</span>
                <span style={{ gridColumn: '2 / -1', fontSize: 11, color: '#6e7681' }}>{liveAccEst ? liveAccEst.note : '…'}</span>

                <div style={{ gridColumn: '1 / -1', display: 'flex', gap: 6, marginTop: 4 }}>
                  <button style={{ ...btn, background: '#238636', borderColor: '#238636' }} onClick={submitLive}>
                    {editLiveId ? <><Save size={12} /> Save changes</> : <><Cpu size={12} /> Start live DF</>}
                  </button>
                  <button style={btn} onClick={() => { setLiveAdding(false); setEditLiveId(null) }}>Cancel</button>
                </div>
              </div>
            ) : (
              <div style={{ fontSize: 11, color: '#6e7681' }}>Add one via <strong>＋ Add device</strong> → "Direction finding — built-in driver".</div>
            )}
          </Section>

          <Section title="SDR as a NIC — TAP/TUN over RF">
            <div style={{ fontSize: 11, color: '#8b949e', marginBottom: 6 }}>
              Bridge a kernel network interface to RF: Ares creates a TAP (L2 Ethernet) or TUN (L3 IP) device and
              carries its frames over the radio with a built-in DBPSK modem — the OS sees a normal NIC you can ping,
              route, or <code>tcpdump</code>. A transmit-capable SDR gives a full-duplex link; a receive-only one gives a
              monitor NIC. Bringing the interface up / assigning an IP needs <code>CAP_NET_ADMIN</code>.
            </div>
            {nicInfo && !nicInfo.supported && (
              <div style={{ fontSize: 11, color: '#f0883e', background: '#0b0f14', border: '1px solid #21262d', borderRadius: 6, padding: 6, marginBottom: 6 }}>
                ⚠ TAP/TUN unavailable here: {nicInfo.reason}
              </div>
            )}

            {(nicInfo?.nics?.length > 0) && (
              <div style={{ marginBottom: 6 }}>
                {nicInfo.nics.map(n => (
                  <div key={n.id} style={{ background: '#0d1117', border: '1px solid #21262d', borderRadius: 4, padding: 6, marginBottom: 4 }}>
                    <div style={{ display: 'flex', alignItems: 'center', gap: 8, fontSize: 12 }}>
                      <span style={{ color: n.status === 'up' ? '#3fb950' : n.status === 'error' ? '#f85149' : '#d29922', display: 'inline-flex' }}>
                        <Network size={13} />
                      </span>
                      <strong>{n.ifname || n.name}</strong>
                      <span style={{ color: '#6e7681', fontSize: 11 }}>{n.mode.toUpperCase()} · {n.driver_id} · {(n.frequency_hz / 1e6).toFixed(3)} MHz · {Math.round(n.bitrate_bps / 1000)} kbit/s · {n.tx_capable ? 'full-duplex' : 'rx-only'}</span>
                      <span style={{ flex: 1 }} />
                      <button style={{ ...btn, padding: '2px 6px' }} title="Tear down NIC" onClick={() => removeNic(n.id)}><Trash2 size={12} color="#f85149" /></button>
                    </div>
                    <div style={{ fontSize: 10, color: '#6e7681', marginTop: 3, display: 'flex', gap: 12, flexWrap: 'wrap' }}>
                      <span>{n.ip_cidr || 'no IP'}</span>
                      <span>TX {n.stats.tx_frames} fr / {n.stats.tx_bytes} B{n.stats.tx_errors ? ` · ${n.stats.tx_errors} err` : ''}</span>
                      <span>RX {n.stats.rx_frames} fr / {n.stats.rx_bytes} B</span>
                      <span>up {n.stats.uptime_s}s</span>
                      {n.config_warning && <span style={{ color: '#f0883e' }}>cfg: {n.config_warning}</span>}
                      {n.last_error && <span style={{ color: '#f85149' }}>{n.last_error}</span>}
                    </div>
                  </div>
                ))}
              </div>
            )}

            {nicAdding ? (
              <div style={{ padding: 10, background: '#0b0f14', border: '1px solid #21262d', borderRadius: 6, display: 'grid', gridTemplateColumns: 'auto minmax(0,1fr) auto minmax(0,1fr)', gap: 6, alignItems: 'center', fontSize: 12 }}>
                <span style={{ color: '#8b949e' }}>driver</span>
                <select style={inputStyle} value={nicForm.driver_id} onChange={e => setNicForm(f => ({ ...f, driver_id: e.target.value }))}>
                  {drivers.length === 0 && <option value="synthetic">synthetic</option>}
                  {drivers.map(d => <option key={d.id} value={d.id}>{d.name}{d.tx_capable ? '' : ' (rx-only)'}</option>)}
                </select>
                <span style={{ color: '#8b949e' }}>name</span>
                <input style={inputStyle} value={nicForm.name} onChange={e => setNicForm(f => ({ ...f, name: e.target.value }))} placeholder={`sdr-nic`} />

                {nicDriver && (
                  <div style={{ gridColumn: '1 / -1', fontSize: 10, color: nicDriver.tx_capable ? '#6e7681' : '#f0883e' }}>
                    {nicDriver.tx_capable ? 'transmit-capable → full-duplex NIC' : '⚠ receive-only driver → monitor NIC (frames in only, no uplink)'}
                  </div>
                )}

                {DRIVER_URI_KEY[nicForm.driver_id] && <>
                  <span style={{ color: '#8b949e' }}>device URI</span>
                  <input style={inputStyle} value={nicForm.uri} onChange={e => setNicForm(f => ({ ...f, uri: e.target.value }))}
                         placeholder={DRIVER_URI_PLACEHOLDER[nicForm.driver_id] || ''} />
                </>}

                <span style={{ color: '#8b949e' }}>mode</span>
                <select style={inputStyle} value={nicForm.mode} onChange={e => setNicForm(f => ({ ...f, mode: e.target.value }))}>
                  <option value="tap">TAP (L2 Ethernet)</option><option value="tun">TUN (L3 IP)</option>
                </select>
                <span style={{ color: '#8b949e' }}>ifname</span>
                <input style={inputStyle} value={nicForm.ifname} onChange={e => setNicForm(f => ({ ...f, ifname: e.target.value }))} placeholder="ares-nic%d" />

                <span style={{ color: '#8b949e' }}>IP/CIDR</span>
                <input style={inputStyle} value={nicForm.ip_cidr} onChange={e => setNicForm(f => ({ ...f, ip_cidr: e.target.value }))} placeholder="10.77.0.1/24 (needs CAP_NET_ADMIN)" />
                <span style={{ color: '#8b949e' }}>freq MHz</span>
                <input style={inputStyle} type="number" value={nicForm.frequency_mhz} onChange={e => setNicForm(f => ({ ...f, frequency_mhz: e.target.value }))} placeholder="433.92" />

                <span style={{ color: '#8b949e' }}>sample MHz</span>
                <input style={inputStyle} type="number" step={0.1} value={nicForm.sample_rate_mhz} onChange={e => setNicForm(f => ({ ...f, sample_rate_mhz: e.target.value }))} />
                <span style={{ color: '#8b949e' }} title="samples per symbol — bitrate = sample_rate / sps">sps</span>
                <input style={inputStyle} type="number" min={2} max={64} value={nicForm.sps} onChange={e => setNicForm(f => ({ ...f, sps: e.target.value }))} />

                <span style={{ color: '#8b949e' }}>gain dB</span>
                <input style={inputStyle} value={nicForm.gain_db} onChange={e => setNicForm(f => ({ ...f, gain_db: e.target.value }))} placeholder="blank = AGC" />
                <span style={{ color: '#8b949e' }}>MTU</span>
                <input style={inputStyle} type="number" min={256} max={2000} value={nicForm.mtu} onChange={e => setNicForm(f => ({ ...f, mtu: e.target.value }))} />

                <span style={{ color: '#8b949e', gridColumn: '1' }}>link rate</span>
                <span style={{ gridColumn: '2 / -1', fontSize: 11, color: '#6e7681' }}>
                  ≈ {Math.round((Number(nicForm.sample_rate_mhz) * 1e6 || 2.4e6) / (Number(nicForm.sps) || 8) / 1000)} kbit/s (DBPSK, 1 bit/symbol)
                </span>

                <div style={{ gridColumn: '1 / -1', display: 'flex', gap: 6, marginTop: 4 }}>
                  <button style={{ ...btn, background: '#238636', borderColor: '#238636' }} onClick={submitNic}><Network size={12} /> Bring up NIC</button>
                  <button style={btn} onClick={() => setNicAdding(false)}>Cancel</button>
                </div>
              </div>
            ) : (
              <div style={{ fontSize: 11, color: '#6e7681' }}>Add one via <strong>＋ Add device</strong> → "Network bridge — SDR as a NIC".</div>
            )}
          </Section>

          <Section title="Native IQ capture backend">
            <div style={{ fontSize: 12, color: '#c9d1d9', marginBottom: 6 }}>
              Backend: <strong>{iqBackend?.backend || '…'}</strong>
              {iqBackend?.available ? <span style={{ color: '#3fb950' }}> · SoapySDR present</span>
                : <span style={{ color: '#f0883e' }}> · SoapySDR not installed — synthetic IQ only (install <code>soapysdr</code> + the device module: <code>SoapySDR_SignalHound</code> / <code>SoapyUHD</code> / <code>SoapySidekiq</code> / <code>SoapyRTLSDR</code>)</span>}
              {iqBackend?.devices?.length > 0 && <> · {iqBackend.devices.length} SDR(s) seen</>}
              <button style={{ ...btn, marginLeft: 6, fontSize: 10, padding: '1px 6px' }} onClick={refreshIqBackend}>↻</button>
            </div>
            {iqBackend?.devices?.length > 0 && (
              <div style={{ background: '#0d1117', border: '1px solid #21262d', borderRadius: 4, padding: 6, marginBottom: 6, maxHeight: 130, overflowY: 'auto' }}>
                {iqBackend.devices.map(d => (
                  <div key={d.id || d.args} style={{ display: 'flex', gap: 8, fontSize: 11, padding: '2px 4px', borderBottom: '1px solid #161b22' }}>
                    <span style={{ color: '#8b949e', minWidth: 90 }}>{d.kind}</span>
                    <span style={{ color: '#c9d1d9', flex: 1, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }} title={d.args}>{d.label || d.id}</span>
                    <span style={{ color: '#6e7681' }}>{d.channels}ch{d.coherent_rx ? ' coherent' : ''}</span>
                  </div>
                ))}
              </div>
            )}
            <div style={{ fontSize: 10, color: '#6e7681' }}>
              Live-AoA solve, LoB workflow and the DF picture live on the <strong>DF</strong> tab — this section is now just SoapySDR /
              SDR-discovery status so the SDR console stays a setup surface.
            </div>
          </Section>

          <Section title="Pentest tools">
            <PentestTools />
          </Section>

          <CellularPanel devices={iqBackend?.devices || []} />

          <Section title="GPS — operator location">
            <div style={{ fontSize: 12, color: '#c9d1d9' }}>
              {gps ? <>Current fix: <strong>{gps.lat.toFixed(5)}, {gps.lon.toFixed(5)}</strong> ({gps.source}{gps.heading_deg != null ? `, heading ${Math.round(gps.heading_deg)}°` : ''}{gps.speed_mps != null ? `, ${(gps.speed_mps * 1.94384).toFixed(1)} kt` : ''}) — shown on the map; the observer position for LoBs and the SDR-device position.</>
                   : <>No GPS fix yet. Pick a source below — this computer, a USB GPS (gpsd or serial NMEA), an SDR's GPSDO — or type one.</>}
            </div>

            {/* GPS source picker */}
            <div style={{ background: '#0d1117', border: '1px solid #21262d', borderRadius: 4, padding: 8, marginTop: 6 }}>
              <div style={{ display: 'flex', gap: 6, alignItems: 'center', flexWrap: 'wrap' }}>
                <span style={{ fontSize: 11, color: '#8b949e' }}>Live source:</span>
                <select style={{ ...inputStyle, fontSize: 11 }} value={gpsSrcForm.kind}
                        onChange={e => setGpsSrcForm(f => ({ ...f, kind: e.target.value }))}>
                  {GPS_SOURCES.map(s => <option key={s.id} value={s.id}>{s.label}</option>)}
                </select>
                {gpsSrcForm.kind === 'browser' && (
                  <>
                    <button style={btn} onClick={() => useThisDevice(false)}>📍 Use this device once</button>
                    {gpsWatchId == null
                      ? <button style={{ ...btn, background: '#1f6feb', borderColor: '#1f6feb' }} onClick={() => useThisDevice(true)}>▶ Track this device</button>
                      : <button style={btn} onClick={stopBrowserWatch}>⏹ Stop tracking</button>}
                  </>
                )}
                {gpsSrcForm.kind === 'gpsd' && <>
                  <input style={{ ...inputStyle, width: 120, fontSize: 11 }} value={gpsSrcForm.host} onChange={e => setGpsSrcForm(f => ({ ...f, host: e.target.value }))} placeholder="gpsd host" />
                  <input style={{ ...inputStyle, width: 64, fontSize: 11 }} value={gpsSrcForm.port} onChange={e => setGpsSrcForm(f => ({ ...f, port: e.target.value }))} placeholder="2947" />
                </>}
                {gpsSrcForm.kind === 'serial' && <>
                  <input style={{ ...inputStyle, width: 160, fontSize: 11 }} value={gpsSrcForm.path} onChange={e => setGpsSrcForm(f => ({ ...f, path: e.target.value }))} placeholder="/dev/ttyUSB0" />
                  <input style={{ ...inputStyle, width: 70, fontSize: 11 }} value={gpsSrcForm.baud} onChange={e => setGpsSrcForm(f => ({ ...f, baud: e.target.value }))} placeholder="9600" />
                </>}
                {gpsSrcForm.kind === 'sdr' && <input style={{ ...inputStyle, width: 200, fontSize: 11 }} value={gpsSrcForm.device_args} onChange={e => setGpsSrcForm(f => ({ ...f, device_args: e.target.value }))} placeholder="SoapySDR args, blank = first device" />}
                {gpsSrcForm.kind !== 'browser' && <button style={{ ...btn, background: '#1f6feb', borderColor: '#1f6feb' }} onClick={applyGpsSource}>{gpsSrcForm.kind === 'off' || gpsSrcForm.kind === 'manual' ? 'Apply' : 'Start'}</button>}
                {(gpsSrc?.running || gpsSrc?.kind === 'browser') && gpsSrcForm.kind !== 'browser' && <button style={btn} onClick={() => { setGpsSrcForm(f => ({ ...f, kind: 'off' })); setGpsSource({ kind: 'off' }).then(setGpsSrc).catch(() => {}) }}>Stop</button>}
              </div>
              {gpsSrc && (gpsSrc.running || gpsSrc.last_error || gpsSrc.kind !== 'off') && (
                <div style={{ fontSize: 10, color: gpsSrc.last_error ? '#f0883e' : '#6e7681', marginTop: 4 }}>
                  source: <strong>{gpsSrc.kind}</strong>{gpsSrc.running ? ' · running' : ''}{gpsSrc.last_error ? ` · ${gpsSrc.last_error}` : ''}
                  {gpsSrc.available && (gpsSrc.available.serial === false || gpsSrc.available.sdr === false) && (
                    <span> · {gpsSrc.available.serial === false ? 'serial needs pyserial' : ''}{gpsSrc.available.serial === false && gpsSrc.available.sdr === false ? '; ' : ''}{gpsSrc.available.sdr === false ? 'SDR-GPSDO needs SoapySDR' : ''}</span>
                  )}
                </div>
              )}
              <div style={{ fontSize: 10, color: '#484f58', marginTop: 4 }}>
                gpsd: install <code>gpsd gpsd-clients</code>, plug the dongle in (works with any gpsd-supported receiver). Serial NMEA reads <code>/dev/ttyUSB*</code>/<code>/dev/ttyACM*</code> directly. SDR-GPSDO reads <code>gps_*</code> sensors (USRP/Sidekiq/SignalHound with a GNSS module). Nothing runs automatically.
              </div>
            </div>

            {/* manual entry */}
            <div style={{ display: 'flex', gap: 6, alignItems: 'center', marginTop: 6, flexWrap: 'wrap' }}>
              <span style={{ fontSize: 11, color: '#8b949e' }}>Or type a position:</span>
              <input style={{ ...inputStyle, width: 100 }} placeholder="lat" value={gpsInput.lat} onChange={e => setGpsInput(g => ({ ...g, lat: e.target.value }))} />
              <input style={{ ...inputStyle, width: 100 }} placeholder="lon" value={gpsInput.lon} onChange={e => setGpsInput(g => ({ ...g, lon: e.target.value }))} />
              <button style={{ ...btn, background: '#1f6feb', borderColor: '#1f6feb' }} onClick={sendGps}><Save size={12} /> Set GPS</button>
              {mapCenter && <button style={btn} onClick={useMapCenterAsGps}>use map centre</button>}
            </div>
          </Section>

          <Section title="Distributed sensing — mesh peers">
            <div style={{ fontSize: 12, color: '#c9d1d9' }}>
              This node: <strong>{mesh?.node_label || '…'}</strong> <span style={{ color: '#6e7681' }}>({mesh?.node_id || ''})</span>.
              Add peer Ares nodes on the MANET and their LoBs/fixes are fused into the same geolocation picture here (and yours into theirs).
              Multiple SDRs on <em>this</em> server already cross-fuse automatically — just register them all under Devices above.
            </div>
            {mesh?.peers?.length ? (
              <div style={{ marginTop: 6 }}>
                {mesh.peers.map(p => (
                  <div key={p.url} style={{ display: 'flex', alignItems: 'center', gap: 8, fontSize: 12, padding: '3px 0', borderBottom: '1px solid #21262d' }}>
                    <span style={{ display: 'inline-flex', alignItems: 'center', gap: 3, color: p.connected ? '#3fb950' : '#f85149' }}>
                      {p.connected ? <Wifi size={12} /> : <WifiOff size={12} />}
                    </span>
                    <span style={{ flex: 1 }}>{p.url}{p.node_id ? <span style={{ color: '#6e7681' }}> · {p.label || p.node_id}</span> : null}</span>
                    <span style={{ color: '#6e7681', fontSize: 11 }}>{p.lob_count ?? 0} LoBs in{p.error ? ` · ${p.error}` : ''}</span>
                    <button style={{ ...btn, padding: '2px 6px' }} title="Remove peer" onClick={() => delPeer(p.url)}><Trash2 size={12} color="#f85149" /></button>
                  </div>
                ))}
              </div>
            ) : <div style={{ fontSize: 11, color: '#8b949e', marginTop: 4 }}>No peers — running standalone (or set <code>ARES_MESH_PEERS</code>).</div>}
            <div style={{ display: 'flex', gap: 6, alignItems: 'center', marginTop: 6 }}>
              <input style={{ ...inputStyle, flex: 1 }} placeholder="http://node2.lan:8000  (peer Ares node base URL)" value={peerInput} onChange={e => setPeerInput(e.target.value)} onKeyDown={e => { if (e.key === 'Enter') addPeer() }} />
              <button style={{ ...btn, background: '#1f6feb', borderColor: '#1f6feb' }} onClick={addPeer}><Plus size={12} /> Add peer</button>
            </div>
          </Section>

          {/* CoT push targets are configured on the ATAK / Server console (🖥 in the header);
              the empty signpost that used to live here was removed — see AtakServerPanel. */}

          {errText && (
            <div style={{ fontSize: 11, color: errText.startsWith('✓') ? '#3fb950' : errText.startsWith('⚠') ? '#f0883e' : '#f85149',
                          background: '#0b0f14', border: '1px solid #21262d', borderRadius: 6, padding: 6 }}>{errText}</div>
          )}

          {/* Event / error console — accumulates everything (API results, WS-stream
              lifecycle, per-device streaming errors) so the operator can scroll back. */}
          <div ref={logRef}>
            <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 6 }}>
              <span style={{ fontSize: 11, fontWeight: 700, color: '#8b949e', textTransform: 'uppercase', letterSpacing: 0.8, display: 'inline-flex', alignItems: 'center', gap: 6 }}>
                <Terminal size={12} /> Event log
              </span>
              <span style={{ fontSize: 10, color: '#6e7681' }}>
                {log.length} event{log.length === 1 ? '' : 's'}
                {log.filter(e => e.level === 'error').length > 0 && <span style={{ color: '#f85149' }}> · {log.filter(e => e.level === 'error').length} error{log.filter(e => e.level === 'error').length === 1 ? '' : 's'}</span>}
              </span>
              <span style={{ flex: 1 }} />
              <label style={{ fontSize: 10, color: '#8b949e', display: 'inline-flex', alignItems: 'center', gap: 3 }}>
                <input type="checkbox" checked={logErrorsOnly} onChange={e => setLogErrorsOnly(e.target.checked)} /> errors only
              </label>
              <button style={{ ...btn, padding: '2px 6px' }} title="Copy log to clipboard" onClick={copyLog}><Copy size={12} /></button>
              <button style={{ ...btn, padding: '2px 6px' }} title="Clear log" onClick={clearLog}><Trash2 size={12} color="#f85149" /></button>
              <button style={{ ...btn, padding: '2px 6px' }} title={logOpen ? 'Collapse' : 'Expand'} onClick={() => setLogOpen(o => !o)}>{logOpen ? '▾' : '▸'}</button>
            </div>
            {logOpen && (
              <div style={{ background: '#0b0f14', border: '1px solid #21262d', borderRadius: 6, padding: 6, maxHeight: 220, overflowY: 'auto',
                            fontFamily: 'ui-monospace, SFMono-Regular, Menlo, monospace', fontSize: 11, lineHeight: 1.5 }}>
                {(() => {
                  const rows = logErrorsOnly ? log.filter(e => e.level === 'error' || e.level === 'warn') : log
                  if (rows.length === 0) return <div style={{ color: '#484f58' }}>{logErrorsOnly ? 'No errors logged.' : 'No events yet.'}</div>
                  return [...rows].reverse().map(e => (
                    <div key={e.id} style={{ color: LOG_COLOR[e.level] || '#8b949e', whiteSpace: 'pre-wrap', wordBreak: 'break-word' }}>
                      <span style={{ color: '#484f58' }}>{new Date(e.t).toLocaleTimeString()}</span>{' '}
                      <span style={{ opacity: 0.85 }}>{e.level.toUpperCase().padEnd(5)}</span>{' '}
                      {e.msg}
                    </div>
                  ))
                })()}
              </div>
            )}
          </div>
        </div>
      </div>
    </div>
  )
}

// Build explicit element positions (E,N,U metres) from a catalogue antenna so it
// can be merged into a custom array — enables "combination of antennas".
function antToPositions(a, freqMhz) {
  if (Array.isArray(a.positions_m)) return a.positions_m.map(p => [p[0], p[1], p[2] || 0])
  const lam = 299.792458 / (Number(freqMhz) || 433.92)        // metres
  const n = Math.max(2, Number(a.n_elements) || 5)
  if (a.geometry === 'ula') {
    const s = Number(a.spacing_m) || (Number(a.array_spacing_wavelengths) || 0.5) * lam
    return Array.from({ length: n }, (_, i) => [0, +((i - (n - 1) / 2) * s).toFixed(4), 0])
  }
  const r = Number(a.radius_m) || 0.4 * lam / (2 * Math.sin(Math.PI / Math.max(3, n)))
  const pts = Array.from({ length: n }, (_, i) => { const ang = 2 * Math.PI * i / n; return [+(r * Math.sin(ang)).toFixed(4), +(r * Math.cos(ang)).toFixed(4), 0] })
  if (a.geometry === 'adcock' && a.sense) pts.push([0, 0, 0])
  return pts
}

// Custom / arbitrary antenna-array builder: generate a base shape, hand-edit
// element positions, merge in catalogue antennas, see the expected accuracy, and
// save the layout as a reusable antenna. Lets Ares DF on any array or combination.
function CustomArrayBuilder({ positions, onChange, antennas, frequencyMhz, onSaved }) {
  const [shape, setShape] = useState('uca')
  const [genN, setGenN] = useState(5)
  const [genSize, setGenSize] = useState(0.2)        // metres (radius for circular, spacing for linear/grid)
  const [importId, setImportId] = useState('')
  const [saveName, setSaveName] = useState('')
  const [est, setEst] = useState(null)
  const small = { ...inputStyle, padding: '2px 4px', fontSize: 11 }

  useEffect(() => {
    if (!positions || positions.length < 2) { setEst(null); return }
    const t = setTimeout(() => {
      dfArrayEstimate({ array: { type: 'custom', positions_m: positions.map(p => [Number(p[0]) || 0, Number(p[1]) || 0, Number(p[2]) || 0]) },
                        frequency_hz: (Number(frequencyMhz) || 433.92) * 1e6 })
        .then(setEst).catch(() => setEst(null))
    }, 400)
    return () => clearTimeout(t)
  }, [positions, frequencyMhz])

  const generate = () => {
    const n = Math.max(2, Number(genN) || 2), s = Number(genSize) || 0.2
    let pts = []
    if (shape === 'uca' || shape === 'adcock') {
      pts = Array.from({ length: n }, (_, i) => { const a = 2 * Math.PI * i / n; return [+(s * Math.sin(a)).toFixed(4), +(s * Math.cos(a)).toFixed(4), 0] })
      if (shape === 'adcock') pts.push([0, 0, 0])
    } else if (shape === 'ula') {
      pts = Array.from({ length: n }, (_, i) => [0, +((i - (n - 1) / 2) * s).toFixed(4), 0])
    } else if (shape === 'grid') {
      const c = Math.ceil(Math.sqrt(n))
      for (let i = 0; i < n; i++) pts.push([(i % c) * s, Math.floor(i / c) * s, 0])
      const me = pts.reduce((a, p) => a + p[0], 0) / pts.length, mn = pts.reduce((a, p) => a + p[1], 0) / pts.length
      pts = pts.map(p => [+(p[0] - me).toFixed(4), +(p[1] - mn).toFixed(4), 0])
    }
    onChange(pts)
  }
  const setRow = (i, j, v) => { const next = positions.map(r => [...r]); next[i][j] = v; onChange(next) }
  const addRow = () => onChange([...(positions || []), [0, 0, 0]])
  const delRow = (i) => onChange(positions.filter((_, k) => k !== i))
  const importAntenna = () => {
    const a = antennas.find(x => x.id === importId)
    if (!a || a.geometry === 'directional') return
    onChange([...(positions || []), ...antToPositions(a, frequencyMhz)])
  }
  const save = () => {
    if (!saveName.trim() || !positions || positions.length < 2) return
    dfSaveAntenna({ name: saveName.trim(), geometry: 'custom',
      positions_m: positions.map(p => [Number(p[0]) || 0, Number(p[1]) || 0, Number(p[2]) || 0]),
      df_methods: ['music', 'correlative', 'watson_watt'], freq_min_hz: 20e6, freq_max_hz: 6e9 })
      .then(p => { setSaveName(''); onSaved?.(p) }).catch(() => {})
  }

  return (
    <div style={{ background: '#0b0f14', border: '1px solid #21262d', borderRadius: 6, padding: 8, marginTop: 4, fontSize: 11 }}>
      <div style={{ color: '#8b949e', marginBottom: 6 }}>
        Custom array — place any elements (E/N/U metres from the array centre), generate a base shape, or merge in a catalogue antenna to build a combination. One coherent SDR channel is needed per element.
      </div>
      {/* generator + import */}
      <div style={{ display: 'flex', gap: 5, alignItems: 'center', flexWrap: 'wrap', marginBottom: 6 }}>
        <select style={small} value={shape} onChange={e => setShape(e.target.value)}>
          <option value="uca">UCA</option><option value="adcock">Adcock</option>
          <option value="ula">ULA</option><option value="grid">grid</option>
        </select>
        <input style={{ ...small, width: 48 }} type="number" min={2} value={genN} onChange={e => setGenN(e.target.value)} title="elements" />
        <input style={{ ...small, width: 60 }} type="number" step={0.01} value={genSize} onChange={e => setGenSize(e.target.value)} title="radius/spacing (m)" />
        <button style={btn} onClick={generate}>Generate</button>
        <span style={{ color: '#30363d' }}>|</span>
        <select style={small} value={importId} onChange={e => setImportId(e.target.value)}>
          <option value="">merge antenna…</option>
          {antennas.filter(a => a.geometry !== 'directional').map(a => <option key={a.id} value={a.id}>{a.model || a.id}</option>)}
        </select>
        <button style={btn} disabled={!importId} onClick={importAntenna}><Plus size={11} /> merge</button>
      </div>
      {/* positions table */}
      <div style={{ maxHeight: 150, overflowY: 'auto', border: '1px solid #161b22', borderRadius: 4 }}>
        <div style={{ display: 'grid', gridTemplateColumns: '24px 1fr 1fr 1fr 22px', gap: 0, color: '#6e7681', padding: '2px 4px', position: 'sticky', top: 0, background: '#0d1117' }}>
          <span>#</span><span>east m</span><span>north m</span><span>up m</span><span></span>
        </div>
        {(positions || []).map((p, i) => (
          <div key={i} style={{ display: 'grid', gridTemplateColumns: '24px 1fr 1fr 1fr 22px', gap: 2, padding: '1px 4px', alignItems: 'center' }}>
            <span style={{ color: '#6e7681' }}>{i}</span>
            {[0, 1, 2].map(j => (
              <input key={j} style={{ ...small, width: '100%' }} type="number" step={0.01} value={p[j]}
                     onChange={e => setRow(i, j, e.target.value)} />
            ))}
            <button style={{ ...btn, padding: '0 4px' }} onClick={() => delRow(i)}><Trash2 size={10} color="#f85149" /></button>
          </div>
        ))}
        {(!positions || positions.length === 0) && <div style={{ color: '#484f58', padding: 6 }}>No elements yet — Generate a shape or merge an antenna.</div>}
      </div>
      <div style={{ display: 'flex', gap: 5, alignItems: 'center', marginTop: 6, flexWrap: 'wrap' }}>
        <button style={btn} onClick={addRow}><Plus size={11} /> element</button>
        <span style={{ flex: 1, color: est?.can_df === false ? '#f0883e' : '#6e7681' }}>
          {positions?.length || 0} elements{est ? ` · ${est.note}` : ''}
        </span>
        <input style={{ ...small, width: 130 }} placeholder="save as antenna…" value={saveName} onChange={e => setSaveName(e.target.value)} onKeyDown={e => { if (e.key === 'Enter') save() }} />
        <button style={{ ...btn, background: '#1f6feb', borderColor: '#1f6feb' }} disabled={!saveName.trim() || (positions?.length || 0) < 2} onClick={save}><Save size={11} /> Save</button>
      </div>
    </div>
  )
}

function Section({ title, children }) {
  return (
    <div>
      <div style={{ fontSize: 11, fontWeight: 700, color: '#8b949e', textTransform: 'uppercase', letterSpacing: 0.8, marginBottom: 6 }}>{title}</div>
      {children}
    </div>
  )
}

function blankForm(mapCenter) {
  return {
    name: '', type: 'krakensdr', host: '', port: 8080,
    source_class: 'multi_channel', channels: 5, array_type: 'uca', array_spacing_wavelengths: 0.4,
    azimuth_reference: 'true', antenna_heading_deg: 0,
    lat: mapCenter?.lat ?? 0, lon: mapCenter?.lon ?? 0,
    altitude_m: 0, observer_height_m: 1.5,
    frequency_hz: 433920000, df_threshold_dbm: -90,
    enabled: true, use_gps: true, auto_coverage: false,
  }
}

function blankLiveForm(mapCenter) {
  return {
    driver_id: 'plutosdr', name: '', uri: '',
    antenna_id: '', array_sense: true, array_radius_m: '', custom_positions: [],
    frequency_mhz: 433.92, channels: 2,
    array_type: 'ula', array_spacing_wavelengths: 0.5,
    vfos: [], auto_squelch: false, auto_calibrate: false, cal_interval_s: 300,
    sample_rate_mhz: 2.4, gain_db: '', method: 'music', dwell_s: 1.0,
    antenna_heading_deg: 0, min_snr_db: 3, min_quality: 0.1,
    lat: mapCenter?.lat ?? 0, lon: mapCenter?.lon ?? 0,
    use_gps: true, auto_coverage: false,
  }
}

// Map a GeolocationPositionError to actionable guidance. The browser's network
// location service is unavailable on an offline box and in the Electron desktop
// app (no Google geolocation key) — code 2 (POSITION_UNAVAILABLE) is the usual
// "Failed to query location from network service" case. Steer to a real GPS source.
function geoErrMsg(e) {
  const tail = ' — use Manual, a USB GPS (gpsd/serial NMEA), or an SDR GPSDO below'
  if (e?.code === 1) return 'Browser location permission denied' + tail
  if (e?.code === 2) return 'Browser network-location unavailable (normal offline / in the desktop app)' + tail
  if (e?.code === 3) return 'Browser location timed out — try again' + tail
  return 'Browser geolocation failed: ' + (e?.message || 'unknown') + tail
}

function blankNicForm() {
  return {
    driver_id: 'plutosdr', name: '', uri: '',
    mode: 'tap', ifname: 'ares-nic%d', ip_cidr: '',
    frequency_mhz: 433.92, sample_rate_mhz: 2.4, sps: 8,
    gain_db: 40, mtu: 1400,
  }
}
