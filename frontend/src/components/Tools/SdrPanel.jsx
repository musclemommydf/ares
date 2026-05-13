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
import { useEffect, useMemo, useState } from 'react'
import { X, Plus, RefreshCw, Trash2, Wifi, WifiOff, AlertCircle, Activity, Radio, Save } from 'lucide-react'
import {
  listSdrDevices, createSdrDevice, updateSdrDevice, deleteSdrDevice, testSdrDevice,
  getSdrState, createSdrSocket, getDfAccuracyEstimate, getGpsFix, setGpsFix,
  getGpsSource, setGpsSource, getDfIqBackend, solveAoaLive,
  addSdrPeer, removeSdrPeer, getSdrPeers,
} from '../../api/client'

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

const inputStyle = { background: '#0d1117', border: '1px solid #30363d', borderRadius: 4, color: '#e6edf3', fontSize: 12, padding: '4px 6px' }
const btn = { background: '#21262d', border: '1px solid #30363d', borderRadius: 4, color: '#c9d1d9', padding: '4px 8px', cursor: 'pointer', fontSize: 12, display: 'inline-flex', alignItems: 'center', gap: 4 }

export default function SdrPanel({ onClose, mapCenter, onSdrFeatures, onSdrCoverage }) {
  const [devices, setDevices] = useState([])
  const [lobs, setLobs] = useState([])
  const [fixes, setFixes] = useState([])
  const [gps, setGps] = useState(null)
  const [gpsInput, setGpsInput] = useState({ lat: '', lon: '' })
  const [gpsSrc, setGpsSrc] = useState(null)              // backend GPS-source status
  const [gpsSrcForm, setGpsSrcForm] = useState({ kind: 'manual', host: '127.0.0.1', port: 2947, path: '/dev/ttyUSB0', baud: 9600, device_args: '' })
  const [gpsWatchId, setGpsWatchId] = useState(null)      // navigator.geolocation.watchPosition id (browser source)
  const [iqBackend, setIqBackend] = useState(null)        // /df/iq_backend — native IQ capture status + SDR(s) seen by SoapySDR
  const [aoaForm, setAoaForm] = useState({ device_id: '', frequency_hz: '433920000', method: 'music', n_snapshots: 4096, array_type: 'uca', n: 5, spacing_wavelengths: 0.4 })
  const [aoaResult, setAoaResult] = useState(null)
  const [aoaBusy, setAoaBusy] = useState(false)
  const [mesh, setMesh] = useState(null)
  const [peerInput, setPeerInput] = useState('')
  const [wsState, setWsState] = useState('connecting')
  const [errText, setErrText] = useState(null)
  const [adding, setAdding] = useState(false)
  const [form, setForm] = useState(() => blankForm(mapCenter))
  const [accEst, setAccEst] = useState(null)
  // refresh the LoB-accuracy estimate as the array config changes (in the add-device form)
  useEffect(() => {
    if (!adding || form.source_class !== 'multi_channel') { setAccEst(null); return }
    let stop = false
    getDfAccuracyEstimate({ channels: form.channels, array_type: form.array_type,
      spacing_wavelengths: form.array_spacing_wavelengths, frequency_hz: Number(form.frequency_hz) || 433.92e6 })
      .then(r => { if (!stop) setAccEst(r) }).catch(() => { if (!stop) setAccEst(null) })
    return () => { stop = true }
  }, [adding, form.source_class, form.channels, form.array_type, form.array_spacing_wavelengths, form.frequency_hz])

  // initial state + WS subscription (auto-reconnect handled by the helper)
  useEffect(() => {
    let cancelled = false
    ;(async () => {
      try {
        const s = await getSdrState()
        if (cancelled) return
        setDevices(s.devices || [])
        setLobs(s.lobs || [])
        setFixes(s.fixes || [])
        setGps(s.gps || null); setMesh(s.mesh || null)
        publishFeatures(s.fixes || [], onSdrFeatures)
      } catch (e) { setErrText(String(e?.message || e)) }
      try {
        const g = await getGpsSource()
        if (!cancelled) { setGpsSrc(g); if (g?.kind) setGpsSrcForm(f => ({ ...f, kind: g.kind, ...(g.config || {}) })) }
      } catch { /* GPS-source endpoint optional */ }
      try { const b = await getDfIqBackend(); if (!cancelled) setIqBackend(b) } catch { /* IQ-backend endpoint optional */ }
    })()
    const sock = createSdrSocket(
      (m) => {
        if (cancelled) return
        setWsState('open')
        if (m.type === 'snapshot') {
          setDevices(m.devices || [])
          setLobs(m.lobs || [])
          setFixes(m.fixes || [])
          setGps(m.gps || null)
          publishFeatures(m.fixes || [], onSdrFeatures)
        } else if (m.type === 'gps') {
          setGps(m.fix || null)
        } else if (m.type === 'device_status') {
          setDevices(prev => prev.map(d => d.id === m.device.id ? m.device : d))
        } else if (m.type === 'lob' || m.type === 'lob_rejected') {
          if (m.type === 'lob') setLobs(prev => [...prev.slice(-127), m.lob])
          if (m.device) setDevices(prev => prev.map(d => d.id === m.device.id ? m.device : d))
        } else if (m.type === 'fix') {
          setFixes(prev => {
            const next = [...prev, m].slice(-32)
            publishFeatures(next, onSdrFeatures)
            return next
          })
        } else if (m.type === 'coverage') {
          onSdrCoverage?.({ geojson: m.geojson, frequency_hz: m.frequency_hz, centroid: m.centroid })
        }
      },
      () => setWsState('error'),
    )
    const meshTimer = setInterval(() => { getSdrPeers().then(r => { if (!cancelled) setMesh({ node_id: r.node_id, node_label: r.node_label, peers: r.status || [] }) }).catch(() => {}) }, 5000)
    return () => { cancelled = true; sock.close(); clearInterval(meshTimer); onSdrFeatures?.([]); onSdrCoverage?.(null) }
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  const submit = async () => {
    setErrText(null)
    try {
      await createSdrDevice({ ...form, port: Number(form.port) || 0, lat: Number(form.lat) || 0, lon: Number(form.lon) || 0,
        frequency_hz: Number(form.frequency_hz) || 0, channels: Number(form.channels) || (form.source_class === 'single_channel' ? 1 : 5),
        array_spacing_wavelengths: Number(form.array_spacing_wavelengths) || 0.4,
        antenna_heading_deg: Number(form.antenna_heading_deg) || 0, df_threshold_dbm: Number(form.df_threshold_dbm) || -90 })
      setAdding(false)
      setForm(blankForm(mapCenter))
      const s = await listSdrDevices(); setDevices(s.devices || [])
    } catch (e) { setErrText(String(e?.response?.data?.detail || e?.message || e)) }
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
    if (!navigator.geolocation) { setErrText('this browser has no Geolocation API'); return }
    stopBrowserWatch()
    if (track) {
      const id = navigator.geolocation.watchPosition(pushBrowserFix, e => setErrText('geolocation: ' + e.message),
        { enableHighAccuracy: true, maximumAge: 5000, timeout: 15000 })
      setGpsWatchId(id)
      setGpsSrc(s => ({ ...(s || {}), kind: 'browser', running: true }))
    } else {
      navigator.geolocation.getCurrentPosition(pushBrowserFix, e => setErrText('geolocation: ' + e.message),
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
  const runAoaLive = async () => {
    const f = aoaForm
    const freq = Number(f.frequency_hz)
    if (!freq || freq <= 0) { setErrText('AoA: enter a frequency in Hz'); return }
    setAoaBusy(true); setAoaResult(null)
    try {
      const array = f.array_type === 'ula'
        ? { type: 'ula', n: Number(f.n) || 4, spacing_m: (Number(f.spacing_wavelengths) || 0.4) * (299_792_458 / freq) }
        : { type: 'uca', n: Number(f.n) || 5, radius_m: ((Number(f.spacing_wavelengths) || 0.4) * (299_792_458 / freq)) / (2 * Math.sin(Math.PI / (Number(f.n) || 5))) }
      const r = await solveAoaLive({
        array, frequency_hz: freq, device_id: f.device_id || undefined,
        method: f.method, n_snapshots: Number(f.n_snapshots) || 4096,
        sample_rate_hz: 2_400_000,
      })
      setAoaResult(r)
    } catch (e) {
      setErrText('AoA live failed: ' + (e?.response?.data?.detail || e?.message || e))
    } finally { setAoaBusy(false) }
  }
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

  return (
    <div style={{ position: 'fixed', inset: 0, background: 'rgba(0,0,0,0.55)', zIndex: 2000,
                  display: 'flex', alignItems: 'flex-start', justifyContent: 'center', padding: '5vh 20px', overflowY: 'auto' }}>
      <div style={{ background: '#0d1117', border: '1px solid #30363d', borderRadius: 8, width: 720, maxWidth: '100%',
                    color: '#e6edf3', boxShadow: '0 20px 60px rgba(0,0,0,0.7)' }}>
        <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', padding: 12, borderBottom: '1px solid #21262d' }}>
          <h3 style={{ margin: 0, fontSize: 14, display: 'flex', alignItems: 'center', gap: 8 }}>
            <Radio size={16} /> SDR console
            <span title="WebSocket stream" style={{ fontSize: 11, color: wsState === 'open' ? '#3fb950' : wsState === 'error' ? '#f85149' : '#d29922' }}>
              {wsState === 'open' ? '● live' : wsState === 'error' ? '● error' : '● connecting'}
            </span>
          </h3>
          <button style={btn} onClick={onClose}><X size={14} /></button>
        </div>

        <div style={{ padding: 12, display: 'flex', flexDirection: 'column', gap: 14 }}>

          <Section title="Devices">
            {devices.length === 0
              ? <div style={{ fontSize: 12, color: '#8b949e' }}>No SDR devices registered. Add one below to start streaming bearings.</div>
              : devices.map(d => (
                  <div key={d.id} style={{ display: 'flex', alignItems: 'center', gap: 8, padding: '4px 0', borderBottom: '1px solid #21262d', fontSize: 12 }}>
                    <span style={{ background: '#1f2937', color: '#9ca3af', borderRadius: 3, padding: '1px 5px', fontSize: 10, textTransform: 'uppercase' }}>{d.type}</span>
                    <span style={{ flex: 1 }}>
                      <strong>{d.name}</strong>
                      <span style={{ color: '#6e7681' }}> · {d.host}{d.port ? `:${d.port}` : ''}{d.frequency_hz ? ` · ${(d.frequency_hz / 1e6).toFixed(3)} MHz` : ''}</span>
                    </span>
                    <span title={d.last_error || ''} style={{ display: 'inline-flex', alignItems: 'center', gap: 3,
                                  color: d.status === 'streaming' ? '#3fb950' : d.status === 'error' ? '#f85149' : '#d29922' }}>
                      {d.status === 'streaming' ? <Wifi size={12} /> : d.status === 'error' ? <AlertCircle size={12} /> : <WifiOff size={12} />}
                      {d.status} ({(lobsByDev[d.id] || []).length} LoBs)
                    </span>
                    <label style={{ color: '#8b949e', fontSize: 11, display: 'inline-flex', alignItems: 'center', gap: 3 }}>
                      <input type="checkbox" checked={d.enabled} onChange={(e) => toggle(d, { enabled: e.target.checked })} /> on
                    </label>
                    <label style={{ color: '#8b949e', fontSize: 11, display: 'inline-flex', alignItems: 'center', gap: 3 }} title="Run a /simulate/coverage from every new fix that includes this device">
                      <input type="checkbox" checked={d.auto_coverage} onChange={(e) => toggle(d, { auto_coverage: e.target.checked })} /> auto-cov
                    </label>
                    <button style={btn} title="Probe TCP connection" onClick={() => probe(d.id)}><Activity size={12} /></button>
                    <button style={btn} title="Remove device" onClick={() => remove(d.id)}><Trash2 size={12} color="#f85149" /></button>
                  </div>
                ))}
            {adding ? (
              <div style={{ marginTop: 8, padding: 10, background: '#0b0f14', border: '1px solid #21262d', borderRadius: 6, display: 'grid', gridTemplateColumns: 'auto 1fr auto 1fr', gap: 6, alignItems: 'center', fontSize: 12 }}>
                <span style={{ color: '#8b949e' }}>name</span>
                <input style={inputStyle} value={form.name} onChange={e => setForm({ ...form, name: e.target.value })} placeholder="Kraken-1" />
                <span style={{ color: '#8b949e' }}>type</span>
                <select style={inputStyle} value={form.type} onChange={e => {
                  const t = DEVICE_TYPES.find(t => t.id === e.target.value)
                  setForm({ ...form, type: e.target.value, port: t?.defaultPort || 0 })
                }}>
                  {DEVICE_TYPES.map(t => <option key={t.id} value={t.id}>{t.label}</option>)}
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
                  <span style={{ color: '#8b949e' }}>auto-coverage</span>
                  <label style={{ display: 'inline-flex', alignItems: 'center', gap: 4 }}>
                    <input type="checkbox" checked={form.auto_coverage} onChange={e => setForm({ ...form, auto_coverage: e.target.checked })} /> rerun /simulate/coverage on each new fix
                  </label>
                  <span style={{ color: '#8b949e', gridColumn: '1' }}>est. LoB accuracy</span>
                  <span style={{ gridColumn: '2 / -1', fontSize: 11, color: '#6e7681' }}>{accEst ? accEst.note : '…'}</span>
                </>}
                <div style={{ gridColumn: '1 / -1', display: 'flex', gap: 6, marginTop: 4 }}>
                  <button style={{ ...btn, background: '#1f6feb', borderColor: '#1f6feb' }} onClick={submit}><Save size={12} /> Save</button>
                  <button style={btn} onClick={() => setAdding(false)}>Cancel</button>
                </div>
              </div>
            ) : (
              <button style={{ ...btn, marginTop: 8 }} onClick={() => setAdding(true)}><Plus size={12} /> Add device</button>
            )}
          </Section>

          <Section title="Native IQ capture / live DF">
            <div style={{ fontSize: 12, color: '#c9d1d9', marginBottom: 6 }}>
              Backend: <strong>{iqBackend?.backend || '…'}</strong>
              {iqBackend?.available ? <span style={{ color: '#3fb950' }}> · SoapySDR present</span>
                : <span style={{ color: '#f0883e' }}> · SoapySDR not installed — synthetic IQ only (install <code>soapysdr</code> + the device module: <code>SoapySDR_SignalHound</code> / <code>SoapyUHD</code> / <code>SoapySidekiq</code> / <code>SoapyRTLSDR</code>)</span>}
              {iqBackend?.devices?.length > 0 && <> · {iqBackend.devices.length} SDR(s) seen</>}
              <button style={{ ...btn, marginLeft: 6, fontSize: 10, padding: '1px 6px' }} onClick={refreshIqBackend}>↻</button>
            </div>
            {iqBackend?.devices?.length > 0 && (
              <div style={{ background: '#0d1117', border: '1px solid #21262d', borderRadius: 4, padding: 6, marginBottom: 6, maxHeight: 110, overflowY: 'auto' }}>
                {iqBackend.devices.map(d => (
                  <div key={d.id || d.args} style={{ display: 'flex', gap: 8, fontSize: 11, padding: '2px 4px', borderBottom: '1px solid #161b22' }}>
                    <span style={{ color: '#8b949e', minWidth: 90 }}>{d.kind}</span>
                    <span style={{ color: '#c9d1d9', flex: 1, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }} title={d.args}>{d.label || d.id}</span>
                    <span style={{ color: '#6e7681' }}>{d.channels}ch{d.coherent_rx ? ' coherent' : ''}</span>
                    <button style={{ ...btn, fontSize: 10, padding: '1px 6px' }} onClick={() => setAoaForm(f => ({ ...f, device_id: d.id || '' }))}>use for AoA</button>
                  </div>
                ))}
              </div>
            )}
            <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6, alignItems: 'center', marginBottom: 4 }}>
              <span style={{ fontSize: 11, color: '#8b949e' }}>Solve AoA live:</span>
              <input style={{ ...inputStyle, width: 100 }} placeholder="freq (Hz)" value={aoaForm.frequency_hz}
                     onChange={e => setAoaForm(f => ({ ...f, frequency_hz: e.target.value }))} />
              <select style={{ ...inputStyle, fontSize: 11 }} value={aoaForm.method}
                      onChange={e => setAoaForm(f => ({ ...f, method: e.target.value }))}>
                <option value="music">MUSIC</option><option value="capon">Capon / MVDR</option><option value="bartlett">Bartlett</option>
              </select>
              <select style={{ ...inputStyle, fontSize: 11 }} value={aoaForm.array_type}
                      onChange={e => setAoaForm(f => ({ ...f, array_type: e.target.value }))}>
                <option value="uca">UCA</option><option value="ula">ULA</option>
              </select>
              <input style={{ ...inputStyle, width: 40 }} title="elements" value={aoaForm.n}
                     onChange={e => setAoaForm(f => ({ ...f, n: e.target.value }))} />
              <input style={{ ...inputStyle, width: 60 }} title="spacing / radius (λ)" value={aoaForm.spacing_wavelengths}
                     onChange={e => setAoaForm(f => ({ ...f, spacing_wavelengths: e.target.value }))} />
              <input style={{ ...inputStyle, width: 70 }} title="snapshots" value={aoaForm.n_snapshots}
                     onChange={e => setAoaForm(f => ({ ...f, n_snapshots: e.target.value }))} />
              <input style={{ ...inputStyle, width: 140 }} placeholder="device id (blank = first)" value={aoaForm.device_id}
                     onChange={e => setAoaForm(f => ({ ...f, device_id: e.target.value }))} />
              <button style={{ ...btn, background: '#1f6feb', borderColor: '#1f6feb' }} disabled={aoaBusy} onClick={runAoaLive}>
                {aoaBusy ? 'Solving…' : 'Solve AoA Live'}
              </button>
            </div>
            {aoaResult && (
              <div style={{ background: '#0d1117', border: '1px solid #21262d', borderRadius: 4, padding: 6, fontSize: 11 }}>
                <div>
                  <strong style={{ color: '#06d6a0' }}>{aoaResult.azimuth_deg != null ? `${aoaResult.azimuth_deg.toFixed(1)}°` : '—'}</strong>
                  {aoaResult.azimuth_sigma_deg != null && <span style={{ color: '#6e7681' }}> ±{aoaResult.azimuth_sigma_deg.toFixed(2)}°</span>}
                  {aoaResult.elevation_deg != null && <span> · el {aoaResult.elevation_deg.toFixed(1)}°</span>}
                  {aoaResult.snr_db != null && <span> · SNR {aoaResult.snr_db.toFixed(1)} dB</span>}
                </div>
                <div style={{ color: '#6e7681', fontSize: 10 }}>
                  {aoaResult.method?.toUpperCase()} · {aoaResult.snapshots} snapshots × {aoaResult.channels} ch · {aoaResult.iq_source}
                  {aoaResult.synthetic ? ' (synthetic IQ — install SoapySDR + the device module to go live)' : ''}
                  {aoaResult.ambiguities?.length > 0 && ` · alt: ${aoaResult.ambiguities.map(a => `${a.az_deg?.toFixed?.(0)}°`).join(', ')}`}
                </div>
              </div>
            )}
          </Section>

          <Section title="Live DF picture">
            <div style={{ fontSize: 12, color: '#c9d1d9' }}>
              {lobs.length} LoB(s) buffered · {fixes.length} fix update(s) · LoBs and fixes appear on the 2D / 3D map automatically.
              {fixes.length > 0 && (() => {
                const last = fixes[fixes.length - 1]
                const c = last.centroid
                return c ? <>  Latest fix: <strong>{last.kind?.toUpperCase()}</strong> @ {c.lat.toFixed(5)}, {c.lon.toFixed(5)}{last.cep ? ` (CEP ${Math.round(last.cep.semiMajorM)} m)` : ''}{last.frequency_hz ? ` · ${(last.frequency_hz / 1e6).toFixed(3)} MHz` : ''}</> : null
              })()}
            </div>
          </Section>

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

          <Section title="CoT push (→ ATAK / TAK Server)">
            <div style={{ fontSize: 12, color: '#8b949e' }}>
              CoT push targets — where LoBs &amp; fixes are sent (UDP multicast / TCP / TLS) — are configured on the
              <strong> ATAK / Server</strong> console (the 🖥 button in the header), alongside the other TAK-server options.
            </div>
          </Section>

          {errText && (
            <div style={{ fontSize: 11, color: errText.startsWith('✓') ? '#3fb950' : '#f85149',
                          background: '#0b0f14', border: '1px solid #21262d', borderRadius: 6, padding: 6 }}>{errText}</div>
          )}
        </div>
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

// Translate the server's solver FeatureCollection into the flat feature list
// App.jsx merges into `geolocationGeoJSON`. Server tags are `type:lob/cep_ellipse/
// suspected_emitter`; App.jsx maps them to the `glx` style the renderers expect.
function publishFeatures(fixes, onSdrFeatures) {
  if (!onSdrFeatures) return
  const last = fixes[fixes.length - 1]
  const gj = last?.geojson
  onSdrFeatures(Array.isArray(gj?.features) ? gj.features : [])
}
