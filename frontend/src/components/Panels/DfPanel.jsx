/**
 * DfPanel — the "DF" tab in the bottom panel (Workstream D).
 *
 * Left ≈ half: one (single-channel SDR) or vertically-stacked (multi-channel)
 * spectrum viewers — scroll-zoom, fixed-y, click-to-tune. Middle: a compass showing
 * the bearings of the current Lines of Bearing / LoB. Right: the DF options/params —
 * the tuner readout (the frequency to DF), gain, AGC, demodulate + listen, the
 * threshold (minimum power to count a bin "active" and shoot a LoB), and per-spectrum
 * frequency min/max (span/centre). More channels ⇒ tighter LoBs; the device's
 * accuracy estimate is shown.
 *
 * What's live vs. scaffolded: the spectrum frames come from `GET /sdr/devices/{id}/spectrum`
 * (synthetic until a SoapySDR/rtl-sdr capture layer is wired); audio decode (DMR/P25/
 * TETRA/NXDN/…) is dispatched to an installed open-source decoder via `POST .../audio`
 * — the panel reports whether one is present. LoBs that arrive (via the WebSocket)
 * are already plotted on the map automatically using your GPS location.
 */
import { useEffect, useMemo, useRef, useState } from 'react'
import SpectrumViewer from './SpectrumViewer'
import { getSdrSpectrum, getDfAccuracyEstimate, getAudioModes, startSdrAudio, updateSdrDevice, getSdrState, getCompassModes, calibrateCompass, solveAoaLive, algoMlGridFusion, algoEkfTrack, identifyPtt } from '../../api/client'
import { useDfAlerts } from '../../store/dfAlerts'
import DfAlertsSettings from '../Geolocation/DfAlertsSettings'
import BearingTimeScope from './BearingTimeScope'
import { dfTrackerStep, dfTrackerState, dfTrackerReset } from '../../api/client'

const _WATERFALL_MAX = 140   // rows of waterfall history kept per channel
const FREQ_UNITS = { Hz: 1, kHz: 1e3, MHz: 1e6, GHz: 1e9 }   // multiplier → Hz

const inp = { background: '#0d1117', border: '1px solid #30363d', borderRadius: 4, color: '#e6edf3', fontSize: 11, padding: '3px 5px', width: 86 }
const lab = { fontSize: 10, color: '#8b949e', display: 'block', marginBottom: 2 }
const btn = { background: '#21262d', border: '1px solid #30363d', borderRadius: 4, color: '#c9d1d9', padding: '3px 8px', cursor: 'pointer', fontSize: 11 }

export default function DfPanel({ onSendAlgorithmFixToMap = null } = {}) {
  // self-contained: polls /sdr/state for devices / LoBs / GPS (independent of the SDR-console WS)
  const [devices, setDevices] = useState([])
  const [lobs, setLobs] = useState([])
  const [gps, setGps] = useState(null)
  useEffect(() => {
    let stop = false
    const tick = async () => { try { const s = await getSdrState(); if (!stop) { setDevices(s.devices || []); setLobs(s.lobs || []); setGps(s.gps || null) } } catch { /* ignore */ } }
    tick(); const h = setInterval(tick, 1500)
    return () => { stop = true; clearInterval(h) }
  }, [])
  const dfDevices = devices.filter(d => d && (d.source_class === 'single_channel' || d.source_class === 'multi_channel' || true))
  const [devId, setDevId] = useState(dfDevices[0]?.id || null)
  useEffect(() => { if (!devId && dfDevices[0]) setDevId(dfDevices[0].id) }, [devices])  // eslint-disable-line
  const dev = devices.find(d => d.id === devId) || null
  const nCh = Math.max(1, Number(dev?.channels ?? 1))
  const canDf = !!dev && dev.source_class !== 'single_channel' && nCh >= 2

  const [center, setCenter] = useState(dev?.frequency_hz || 433.92e6)
  const [span, setSpan] = useState(2.4e6)
  const [tuneHz, setTuneHz] = useState(dev?.frequency_hz || 433.92e6)
  const [threshold, setThreshold] = useState(dev?.df_threshold_dbm ?? -90)
  const [gain, setGain] = useState(30)
  const [unit, setUnit] = useState('MHz')                 // frequency display/entry unit (default MHz)
  const [perChFreq, setPerChFreq] = useState({})          // {channelIdx: center_hz} — per-channel spectrum centre override
  const [tuning, setTuning] = useState(false)             // a device retune (frequency_hz) is in flight
  const [expandPerCh, setExpandPerCh] = useState(false)   // by default show one spectrum per *device*; toggle to one per channel
  // Frequency unit helpers: store everything in Hz internally, show/enter in `unit`.
  const uf = FREQ_UNITS[unit] || 1e6
  const toHz = (v) => Number(v) * uf
  const inU = (hz) => { const v = Number(hz) / uf; return Number.isFinite(v) ? +v.toPrecision(12) : '' }
  const [agc, setAgc] = useState(true)
  const [demod, setDemod] = useState('nfm')
  const [audioStatus, setAudioStatus] = useState(null)
  const [audioModes, setAudioModes] = useState([])
  const [frames, setFrames] = useState([])     // one PSD frame per channel
  const [hist, setHist] = useState([])         // per-channel rolling array of frames (for the waterfall)
  const [acc, setAcc] = useState(null)
  const [compass, setCompass] = useState(null) // {modes, calibration}
  const [calOpen, setCalOpen] = useState(false)
  const [calIn, setCalIn] = useState({ true_bearing: '', relative_lob: '' })
  useEffect(() => { getAudioModes().then(m => setAudioModes(m.modes || [])).catch(() => {}) }, [])
  useEffect(() => { getCompassModes().then(setCompass).catch(() => {}) }, [])
  useEffect(() => {
    if (!dev) { setAcc(null); return }
    setCenter(dev.frequency_hz || 433.92e6); setTuneHz(dev.frequency_hz || 433.92e6); setThreshold(dev.df_threshold_dbm ?? -90); setPerChFreq({})
    getDfAccuracyEstimate({ channels: nCh, array_type: dev.array_type || 'uca',
      spacing_wavelengths: dev.array_spacing_wavelengths || 0.4, frequency_hz: dev.frequency_hz || 433.92e6 })
      .then(setAcc).catch(() => setAcc(null))
  }, [devId])  // eslint-disable-line
  // poll the spectrum for each channel + accumulate the per-channel waterfall history
  useEffect(() => {
    if (!dev) { setFrames([]); setHist([]); return }
    setHist([])   // reset history when device / centre / span / channel-count changes
    let stop = false
    const tick = async () => {
      try {
        const got = await Promise.all(Array.from({ length: nCh }, (_, ch) =>
          getSdrSpectrum(dev.id, { center_hz: perChFreq[ch] ?? center, span_hz: span, n_bins: 1024, channel: ch }).catch(() => null)))
        if (stop) return
        setFrames(got)
        setHist(prev => got.map((fr, ch) => fr ? [...((prev[ch] || [])).slice(-(_WATERFALL_MAX - 1)), fr] : (prev[ch] || [])))
      } catch { /* ignore */ }
    }
    tick(); const h = setInterval(tick, 700)
    return () => { stop = true; clearInterval(h) }
  }, [devId, center, span, nCh, perChFreq])  // eslint-disable-line

  const recentLobs = useMemo(() => {
    const cut = Date.now() / 1000 - 90
    return (lobs || []).filter(l => (l.t || 0) >= cut).slice(-12)
  }, [lobs])

  // ── DF alerts: fire a `newLoB` whenever a fresh LoB lands in the SDR feed.
  // Tracked by id (or by t+azimuth if id is missing) so re-polling /sdr/state
  // doesn't re-fire on every tick — only on genuinely new arrivals.
  const fireDfAlert = useDfAlerts((s) => s.fire)
  const seenLobIdsRef = useRef(new Set())
  useEffect(() => {
    if (!lobs?.length) return
    let fired = 0
    for (const l of lobs) {
      const key = l.id || `${l.t}|${l.azimuth_deg}|${l.frequency_hz}`
      if (seenLobIdsRef.current.has(key)) continue
      seenLobIdsRef.current.add(key)
      // On first observation of a non-empty feed we DON'T want to flood with
      // alerts for every historical LoB — so skip the burst when this is the
      // initial population pass.
      if (seenLobIdsRef.current.size === lobs.length && lobs.length > 1) break
      const fMHz = Number.isFinite(l.frequency_hz) && l.frequency_hz > 0
        ? `${(l.frequency_hz / 1e6).toFixed(3)} MHz` : '—'
      fireDfAlert('newLoB', `${fMHz} · ${Number(l.azimuth_deg ?? 0).toFixed(1)}°`)
      if (++fired >= 3) break                  // throttle: at most 3 alerts per poll cycle
    }
    // Garbage-collect stale ids so the Set doesn't grow forever during long sessions.
    if (seenLobIdsRef.current.size > 4096) seenLobIdsRef.current = new Set(
      lobs.map(l => l.id || `${l.t}|${l.azimuth_deg}|${l.frequency_hz}`)
    )
  }, [lobs, fireDfAlert])
  const [alertSettingsOpen, setAlertSettingsOpen] = useState(false)

  // ── Live-AoA solver (moved here from the SDR console) ───────────────────
  // Triggers a one-shot AoA solve on the currently-selected coherent SDR.
  // Belongs on the DF tab because the result IS the DF observable — and the
  // array geometry / method belongs alongside the LoB workflow it feeds.
  const [aoaForm, setAoaForm] = useState({ device_id: '', frequency_hz: '433920000', method: 'music',
                                            n_snapshots: 4096, array_type: 'uca', n: 5, spacing_wavelengths: 0.4 })
  const [aoaResult, setAoaResult] = useState(null)
  const [aoaBusy, setAoaBusy] = useState(false)
  const [aoaErr, setAoaErr] = useState('')
  useEffect(() => { if (dev?.id && !aoaForm.device_id) setAoaForm(f => ({ ...f, device_id: dev.id })) }, [dev?.id])  // eslint-disable-line
  // ── ML-grid fusion of the current LoB list (more robust than pair-intersection
  // when LoB σ is non-Gaussian or baselines are oblique). Optionally drops the
  // MAP fix on the map as an algorithm-origin emitter.
  const [advBusy, setAdvBusy] = useState(false)
  const [advFix, setAdvFix] = useState(null)
  const [advErr, setAdvErr] = useState('')
  const [advMode, setAdvMode] = useState('ml_grid')   // 'ml_grid' | 'ekf'

  const lobToAoaObs = (l) => {
    // Each LoB carries {observer_lat, observer_lon, true_bearing_deg, sigma_deg}
    const lat = l.observer_lat ?? l.lat ?? l.observer?.lat
    const lon = l.observer_lon ?? l.lon ?? l.observer?.lon
    const brg = l.true_bearing_deg ?? l.bearing_deg ?? l.bearing
    const sig = l.sigma_deg ?? l.bearing_sigma_deg ?? 3.0
    if (lat == null || lon == null || brg == null) return null
    return { kind: 'aoa', lat: Number(lat), lon: Number(lon),
              bearing_deg: Number(brg), sigma_deg: Number(sig) }
  }

  const runAdvFusion = async () => {
    setAdvBusy(true); setAdvFix(null); setAdvErr('')
    try {
      const obs = lobs.map(lobToAoaObs).filter(Boolean)
      if (obs.length < 2) throw new Error('Need at least 2 LoBs with observer + bearing to fuse')
      let r
      if (advMode === 'ml_grid') {
        r = await algoMlGridFusion({ observations: obs, sigma_aoa_deg: obs[0].sigma_deg || 3.0,
                                       grid_span_m: 30_000, grid_step_m: 50 })
      } else {
        r = await algoEkfTrack({ observations: obs, sigma_aoa_deg: obs[0].sigma_deg || 3.0 })
      }
      if (!r?.ok) throw new Error(r?.error || 'fusion failed')
      setAdvFix(r)
    } catch (e) {
      setAdvErr(String(e?.response?.data?.detail || e?.message || e))
    } finally { setAdvBusy(false) }
  }

  const sendAdvToMap = () => {
    if (!advFix?.estimate || !onSendAlgorithmFixToMap) return
    onSendAlgorithmFixToMap({
      lat: advFix.estimate.lat, lon: advFix.estimate.lon,
      label: `DF→${advMode === 'ml_grid' ? 'ML-grid' : 'EKF'} (${lobs.length} LoB${lobs.length === 1 ? '' : 's'}, CEP ${Math.round(advFix.uncertainty?.cep_m || 0)}m)`,
      method_id: advMode === 'ml_grid' ? 'ml_grid_fusion' : 'ekf_track',
      method_name: advMode === 'ml_grid' ? 'ML grid fusion (DF LoBs)' : 'EKF kinematic (DF LoBs)',
      cep_m: advFix.uncertainty?.cep_m, raw: advFix,
    })
  }

  const runAoaLive = async () => {
    const freq = Number(aoaForm.frequency_hz)
    if (!freq || freq <= 0) { setAoaErr('Enter a frequency in Hz'); return }
    setAoaBusy(true); setAoaResult(null); setAoaErr('')
    try {
      const f = aoaForm
      const array = f.array_type === 'ula'
        ? { type: 'ula', n: Number(f.n) || 4, spacing_m: (Number(f.spacing_wavelengths) || 0.4) * (299_792_458 / freq) }
        : { type: 'uca', n: Number(f.n) || 5, radius_m: ((Number(f.spacing_wavelengths) || 0.4) * (299_792_458 / freq)) / (2 * Math.sin(Math.PI / (Number(f.n) || 5))) }
      const r = await solveAoaLive({ array, frequency_hz: freq, device_id: f.device_id || undefined,
                                       method: f.method, n_snapshots: Number(f.n_snapshots) || 4096,
                                       sample_rate_hz: 2_400_000 })
      setAoaResult(r)
    } catch (e) {
      setAoaErr('AoA live failed: ' + (e?.response?.data?.detail || e?.message || e))
    } finally { setAoaBusy(false) }
  }

  const applyThreshold = async (v) => {
    setThreshold(v)
    if (dev) updateSdrDevice(dev.id, { df_threshold_dbm: Number(v) }).catch(() => {})
  }
  // Retune the radio: persist frequency_hz (live-DF re-spawns + re-tunes; external
  // adapters pick it up) and re-centre the spectrum on the new band. This is what
  // makes typing a frequency actually move the receiver.
  const applyTune = async (hz) => {
    const f = Math.round(Number(hz) || tuneHz)
    if (!f || f <= 0 || !dev) return
    setTuneHz(f); setCenter(f); setPerChFreq({})       // new band → drop per-channel overrides
    setTuning(true)
    try {
      const u = await updateSdrDevice(dev.id, { frequency_hz: f })
      setDevices(prev => prev.map(d => d.id === dev.id ? u : d))
      setAudioStatus({ status: 'ok', detail: `✓ tuned ${(f / 1e6).toFixed(5)} MHz` })
    } catch (e) { setAudioStatus({ status: 'error', detail: 'tune failed: ' + (e?.response?.data?.detail || e?.message || e) }) }
    finally { setTuning(false) }
  }
  const listen = async () => {
    if (!dev) return
    try { setAudioStatus(await startSdrAudio(dev.id, tuneHz, demod)) }
    catch (e) { setAudioStatus({ status: 'error', detail: String(e?.response?.data?.detail || e?.message || e) }) }
  }
  // Auto-detect PTT standard at the current tune frequency and update the
  // mode dropdown to the recognised standard. Surfaces the classifier's
  // verdict + alternatives in the status line.
  const [autoBusy, setAutoBusy] = useState(false)
  const [autoVerdict, setAutoVerdict] = useState(null)
  const autoDetectPtt = async () => {
    if (!dev) { setAudioStatus({ status: 'error', detail: 'Select an SDR first' }); return }
    setAutoBusy(true); setAutoVerdict(null)
    try {
      const r = await identifyPtt({ device_id: dev.id, frequency_hz: tuneHz, capture_seconds: 0.5 })
      if (!r?.ok) {
        setAudioStatus({ status: 'error', detail: r?.error || 'identify failed' })
        return
      }
      const v = r.verdict
      setAutoVerdict(r)
      // Set the dropdown to the detected mode (the option may not exist if the
      // catalogue is empty; fall back to nfm so listen() still works).
      const candidate = v.audio_mode || 'nfm'
      const exists = (audioModes.length ? audioModes : [{ id: 'nfm' }]).some(m => m.id === candidate)
      setDemod(exists ? candidate : 'nfm')
      const decAvail = r.decoder_available ? '✓' : '✗ (not installed)'
      const fallback = r.fallback_decoder ? ` · fallback decoder: ${r.fallback_decoder}` : ''
      setAudioStatus({
        status: 'auto_detected',
        detail: `→ ${v.label} (${(v.confidence * 100).toFixed(0)}%) · decoder: ${v.decoder} ${decAvail}${fallback}`,
      })
    } catch (e) {
      setAudioStatus({ status: 'error', detail: 'auto-detect failed: ' + (e?.response?.data?.detail || e?.message || e) })
    } finally { setAutoBusy(false) }
  }
  const setCompassMode = async (mode) => {
    if (!dev) return
    try { const u = await updateSdrDevice(dev.id, { azimuth_reference: mode }); setDevices(prev => prev.map(d => d.id === dev.id ? u : d)) }
    catch (e) { setAudioStatus({ status: 'error', detail: String(e?.message || e) }) }
  }
  const runCalibrate = async () => {
    if (!dev) return
    const tb = parseFloat(calIn.true_bearing)
    if (isNaN(tb)) { setAudioStatus({ status: 'error', detail: 'enter the known true bearing of the reference' }); return }
    const body = { known_true_bearing_deg: tb }
    if (calIn.relative_lob !== '' && !isNaN(parseFloat(calIn.relative_lob))) body.measured_relative_lob_deg = parseFloat(calIn.relative_lob)
    try {
      const r = await calibrateCompass(dev.id, body)
      setAudioStatus({ status: 'ok', detail: `✓ calibrated: heading = ${r.antenna_heading_deg}° (true ${r.known_true_bearing_deg}° − relative ${r.measured_relative_lob_deg}°)${r.used_last_lob ? ' [used last LoB]' : ''}` })
      setCalOpen(false); setCalIn({ true_bearing: '', relative_lob: '' })
      const s = await getSdrState(); setDevices(s.devices || [])
    } catch (e) { setAudioStatus({ status: 'error', detail: String(e?.response?.data?.detail || e?.message || e) }) }
  }

  if (!devices.length) {
    return <div style={{ padding: 16, color: '#8b949e', fontSize: 12 }}>No SDR registered yet. Open the SDR console (📡 in the header) and add a single-channel (spectrum/audio) or multi-channel (DF) source.</div>
  }

  return (
    <div style={{
      display: 'flex', height: '100%', minHeight: 0, gap: 8, padding: 8,
      fontSize: 12, color: '#e6edf3',
      // Wrap onto multiple rows when the panel is narrower than ~720 px so the
      // right-hand controls never get crushed below their minimum legible width.
      flexWrap: 'wrap', alignContent: 'stretch', overflowY: 'auto',
    }}>
      {/* LEFT — stacked spectrum viewers (one per channel). Flexible width;
          falls below the 320 px floor on very narrow viewports because of wrap. */}
      <div style={{ flex: '2 1 360px', minWidth: 0, overflowY: 'auto', display: 'flex', flexDirection: 'column', gap: 6 }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap' }}>
          <select style={{ ...inp, width: 'auto' }} value={devId || ''} onChange={e => setDevId(e.target.value)}>
            {devices.map(d => <option key={d.id} value={d.id}>{d.name} · {d.source_class === 'single_channel' ? '1ch' : `${d.channels}ch`}{d.source_class === 'single_channel' ? ' (no DF)' : ''}</option>)}
          </select>
          {nCh > 1 && (
            <button type="button"
              title={expandPerCh ? 'Showing one spectrum per channel — click to collapse to one per device' : `Showing one spectrum for this device — click to expand to all ${nCh} channels`}
              onClick={() => setExpandPerCh(v => !v)}
              style={{ ...inp, cursor: 'pointer', width: 'auto', padding: '4px 8px' }}>
              {expandPerCh ? `▾ all ${nCh} ch` : `▸ ch0 only`}
            </button>
          )}
          <label style={{ color: '#8b949e' }}>units&nbsp;
            <select style={{ ...inp, width: 'auto' }} value={unit} onChange={e => setUnit(e.target.value)}>
              {Object.keys(FREQ_UNITS).map(u => <option key={u} value={u}>{u}</option>)}
            </select>
          </label>
          <label style={{ color: '#8b949e' }}>centre <input style={inp} type="number" step="any" value={inU(center)} onChange={e => setCenter(toHz(e.target.value) || center)} /> {unit}</label>
          <label style={{ color: '#8b949e' }}>span <input style={inp} type="number" step="any" value={inU(span)} onChange={e => setSpan(Math.max(1e3, toHz(e.target.value) || span))} /> {unit}</label>
          <span style={{ color: '#6e7681', fontSize: 10 }}>min {inU(center - span / 2)} – max {inU(center + span / 2)} {unit}</span>
        </div>
        {(() => {
          // by default show one spectrum per *device* (channel 0); expand to one per channel on demand
          const allFrames = frames.length ? frames : [null]
          const shown = (nCh > 1 && !expandPerCh) ? [allFrames[0] ?? null] : allFrames
          const labelFor = (i) => (nCh > 1 && expandPerCh) ? `ch${i}` : (nCh > 1 ? `${nCh}ch — ch0` : '')
          const heightFor = () => (shown.length > 1) ? Math.max(90, Math.floor(360 / shown.length)) : 200
          const perCh = nCh > 1 && expandPerCh
          return shown.map((fr, i) => {
            const chHz = perChFreq[i] ?? center
            const overridden = perChFreq[i] != null && perChFreq[i] !== center
            return (
              <div key={i}>
                {perCh && (
                  <div style={{ display: 'flex', alignItems: 'center', gap: 6, fontSize: 10, color: '#8b949e', marginBottom: 2 }}>
                    <span style={{ fontWeight: 700, color: overridden ? '#58a6ff' : '#8b949e' }}>ch{i}</span>
                    <label title="This channel's spectrum centre — pans within the captured band (= sample rate). Use the global Tune to move the radio.">
                      freq <input style={{ ...inp, width: 84 }} type="number" step="any" value={inU(chHz)}
                        onChange={e => { const hz = toHz(e.target.value); setPerChFreq(p => ({ ...p, [i]: hz || center })) }} /> {unit}
                    </label>
                    {overridden && <button style={{ ...btn, padding: '1px 6px' }} title="follow the global centre"
                      onClick={() => setPerChFreq(p => { const n = { ...p }; delete n[i]; return n })}>↺</button>}
                  </div>
                )}
                <SpectrumViewer frame={fr ? { ...fr, df_threshold_dbm: threshold } : null}
                  label={labelFor(i)} tuneHz={tuneHz} onTune={setTuneHz} history={hist[i] || []} height={heightFor()} />
              </div>
            )
          })
        })()}
      </div>

      {/* MIDDLE — compass + B-scope. Bounded width so it neither overgrows on
          big screens nor crushes the spectra on narrow ones. */}
      <div style={{
        flex: '1 1 220px', minWidth: 200, maxWidth: 320,
        display: 'flex', flexDirection: 'column', alignItems: 'stretch', gap: 6,
      }}>
        <div style={{ display: 'flex', justifyContent: 'center' }}>
          <Compass lobs={recentLobs} dev={dev} />
        </div>
        <div style={{ fontSize: 10, color: '#8b949e', textAlign: 'center' }}>
          {canDf ? `${recentLobs.length} active LoB(s)` : 'single-channel — no LoBs (DF needs ≥2 coherent channels)'}
          {dev?.azimuth_reference === 'relative' && <div>(clock = off the antenna front, heading {Math.round(dev.antenna_heading_deg || 0)}°)</div>}
        </div>
        <div>
          <div style={{ fontSize: 10, fontWeight: 700, color: '#8b949e', letterSpacing: 0.6, marginBottom: 2, textTransform: 'uppercase' }}>
            B-scope (bearing × time)
          </div>
          <BearingTimeScope lobs={lobs} height={140} windowSec={60} />
        </div>
      </div>

      {/* RIGHT — DF options & parameters. Flex with a minimum so it never goes
          below readable width; wraps to its own row below ~720 px panel width. */}
      <div style={{
        flex: '1 1 240px', minWidth: 220, maxWidth: 360,
        overflowY: 'auto', display: 'flex', flexDirection: 'column', gap: 8,
        borderLeft: '1px solid #21262d', paddingLeft: 8,
      }}>
        <div>
          <span style={lab}>DF tune frequency (type & press Enter / Tune to move the radio, or click a signal in the spectrum)</span>
          <div style={{ display: 'flex', alignItems: 'center', gap: 4, flexWrap: 'wrap' }}>
            <input style={{ ...inp, width: 110 }} type="number" step="any" value={inU(tuneHz)}
              onChange={e => setTuneHz(toHz(e.target.value) || tuneHz)}
              onKeyDown={e => { if (e.key === 'Enter') applyTune(tuneHz) }} />
            <span style={{ color: '#8b949e', fontSize: 11 }}>{unit}</span>
            <button style={{ ...btn, background: '#1f6feb', borderColor: '#1f6feb', color: '#fff' }}
              disabled={!dev || tuning} onClick={() => applyTune(tuneHz)}>{tuning ? 'Tuning…' : '⤵ Tune'}</button>
          </div>
          <div style={{ fontSize: 10, color: '#6e7681' }}>
            {(tuneHz / 1e6).toFixed(5)} MHz{dev?.frequency_hz != null ? ` · radio at ${(dev.frequency_hz / 1e6).toFixed(5)} MHz` : ''}
          </div>
        </div>
        <div>
          <span style={lab}>threshold (min power for a bin to count active → shoot a LoB)</span>
          <input style={inp} type="number" value={threshold} onChange={e => applyThreshold(e.target.value)} /> dBm
        </div>
        <div style={{ display: 'flex', gap: 8 }}>
          <div><span style={lab}>gain</span><input style={inp} type="number" value={gain} onChange={e => setGain(Number(e.target.value))} disabled={agc} /> dB</div>
          <label style={{ display: 'flex', alignItems: 'flex-end', gap: 3, color: '#8b949e', fontSize: 11 }}>
            <input type="checkbox" checked={agc} onChange={e => setAgc(e.target.checked)} /> AGC
          </label>
        </div>
        <div>
          <span style={lab}>demodulate / decode &amp; listen</span>
          <div style={{ display: 'flex', gap: 4, alignItems: 'center', flexWrap: 'wrap' }}>
            <select style={{ ...inp, flex: 1, minWidth: 120, maxWidth: 220 }} value={demod} onChange={e => setDemod(e.target.value)}>
              {(audioModes.length ? audioModes : [{ id: 'nfm', label: 'Narrowband FM' }]).map(m =>
                <option key={m.id} value={m.id}>{m.label}{m.ready === false ? ' (decoder not installed)' : ''}</option>)}
            </select>
            <button style={btn} onClick={listen}>▶ Listen</button>
            <button style={{ ...btn, background: '#1f6feb', borderColor: '#1f6feb', color: '#fff' }}
                    disabled={autoBusy || !dev} onClick={autoDetectPtt}
                    title="Capture a short IQ window at the tuned frequency and pick the right decoder automatically (DMR / P25 / TETRA / NXDN / D-STAR / YSF / M17 / …)">
              {autoBusy ? '…' : '🎯 Auto-detect'}
            </button>
          </div>
          {audioStatus && <div style={{ fontSize: 10, color: (audioStatus.status === 'error' || (audioStatus.detail || '').startsWith('⚠')) ? '#f85149' : '#8b949e', marginTop: 3 }}>{audioStatus.detail || audioStatus.status}</div>}
          {autoVerdict?.candidates?.length > 1 && (
            <div style={{ marginTop: 4, fontSize: 10, color: '#6e7681' }}>
              alt: {autoVerdict.candidates.slice(1, 4).map((c, i) =>
                <span key={i}>{i > 0 ? ' · ' : ''}{c.label} {(c.score * 100).toFixed(0)}%{c.decoder_installed ? '' : ' ✗'}</span>
              )}
            </div>
          )}
          {autoVerdict?.evidence && (
            <div style={{ marginTop: 2, fontSize: 9, color: '#6e7681' }}>
              evidence: bw {Math.round(autoVerdict.evidence.bandwidth_hz)} Hz · sym {Math.round(autoVerdict.evidence.symbol_rate_hz)} Hz (c={(autoVerdict.evidence.symbol_rate_confidence || 0).toFixed(2)}) · fam {autoVerdict.evidence.family_detected}
            </div>
          )}
        </div>
        {/* Compass mode (3) + calibration */}
        {dev && dev.source_class !== 'single_channel' && (
          <div style={{ borderTop: '1px solid #21262d', paddingTop: 6 }}>
            <span style={lab}>compass mode</span>
            <select style={{ ...inp, width: '100%' }} value={dev.azimuth_reference || 'absolute'} onChange={e => setCompassMode(e.target.value)}>
              {(compass?.modes || [{ id: 'absolute', label: 'Absolute LOB (true north)' }, { id: 'relative', label: 'Relative LOB (off the antenna front)' }, { id: 'clock', label: 'Clock position (off the antenna front)' }]).map(m => <option key={m.id} value={m.id}>{m.label}</option>)}
            </select>
            <div style={{ fontSize: 10, color: '#6e7681', marginTop: 2 }}>antenna heading: {Math.round(dev.antenna_heading_deg || 0)}° · Absolute LOB = (0 + heading) + Relative LOB</div>
            <button style={{ ...btn, marginTop: 4 }} onClick={() => setCalOpen(v => !v)}>⌖ {calOpen ? 'cancel' : 'Calibrate compass…'}</button>
            {calOpen && (
              <div style={{ marginTop: 6, padding: 6, background: '#0b0f14', border: '1px solid #21262d', borderRadius: 5 }}>
                <ol style={{ margin: '0 0 6px', paddingLeft: 16, fontSize: 9.5, color: '#8b949e', lineHeight: 1.4 }}>
                  {(compass?.calibration?.steps || [
                    '1. Know the TRUE bearing from you to a reference emitter/landmark (map / corrected compass / known beacon).',
                    '2. Aim the DF antenna front (its 0° mark) however it mounts — it need not point at the reference.',
                    '3. Tune the reference signal, shoot a LoB, read the RELATIVE LOB (deg off the antenna front).',
                    '4. Enter the values below → heading = (true − relative). Switch to Absolute LOB and re-shoot to verify.',
                  ]).map((s, i) => <li key={i}>{s}</li>)}
                </ol>
                <div style={{ display: 'flex', gap: 6, alignItems: 'center', flexWrap: 'wrap' }}>
                  <label style={{ fontSize: 10, color: '#8b949e' }}>true bearing° <input style={{ ...inp, width: 64 }} value={calIn.true_bearing} onChange={e => setCalIn(c => ({ ...c, true_bearing: e.target.value }))} /></label>
                  <label style={{ fontSize: 10, color: '#8b949e' }}>relative LOB° <input style={{ ...inp, width: 64 }} placeholder="last shot" value={calIn.relative_lob} onChange={e => setCalIn(c => ({ ...c, relative_lob: e.target.value }))} /></label>
                  <button style={{ ...btn, background: '#1f6feb', borderColor: '#1f6feb' }} onClick={runCalibrate}>Calibrate</button>
                </div>
              </div>
            )}
          </div>
        )}
        {acc && (
          <div style={{ fontSize: 10, color: '#8b949e', borderTop: '1px solid #21262d', paddingTop: 6 }}>
            <strong>LoB accuracy estimate:</strong> {acc.can_df
              ? `≈${acc.sigma_az_deg}° 1-σ (${acc.channels} ch, ${acc.array_type}), CEP@1km ≈ ${acc.cep_at_1km_m} m`
              : 'single-channel — no DF'}
            <div style={{ color: '#6e7681' }}>{acc.note}</div>
          </div>
        )}
        {/* Advanced fusion across LoBs (ML grid / EKF) — produces a richer fix
            than the simple pair-intersection in df/fusion.fuse_aoa_aoa. */}
        <div style={{ borderTop: '1px solid #21262d', paddingTop: 6 }}>
          <div style={{ fontSize: 11, color: '#8b949e', marginBottom: 4 }}>
            Advanced fusion — {lobs.length} live LoB(s) → joint emitter fix (in-process, no external service)
          </div>
          <div style={{ display: 'flex', flexWrap: 'wrap', gap: 4, alignItems: 'center' }}>
            <select style={{ ...inp, width: 130 }} value={advMode} onChange={e => setAdvMode(e.target.value)}>
              <option value="ml_grid">ML grid fusion</option>
              <option value="ekf">EKF kinematic</option>
            </select>
            <button style={{ ...btn, background: '#1f6feb', borderColor: '#1f6feb' }} disabled={advBusy || lobs.length < 2} onClick={runAdvFusion}>
              {advBusy ? 'Fusing…' : `Fuse ${lobs.length} LoB${lobs.length === 1 ? '' : 's'}`}
            </button>
            {advFix?.estimate && onSendAlgorithmFixToMap && (
              <button style={{ ...btn, background: '#238636', borderColor: '#238636' }} onClick={sendAdvToMap}>
                Send to map
              </button>
            )}
          </div>
          {advFix?.estimate && (
            <div style={{ marginTop: 4, fontSize: 11 }}>
              <strong style={{ color: '#06d6a0' }}>
                {advFix.estimate.lat.toFixed(5)}, {advFix.estimate.lon.toFixed(5)}
              </strong>
              {advFix.uncertainty?.cep_m != null && <span style={{ color: '#6e7681' }}> · CEP {Math.round(advFix.uncertainty.cep_m)} m</span>}
              {advFix.fit?.log_likelihood != null && <span style={{ color: '#6e7681' }}> · log-L {advFix.fit.log_likelihood.toFixed(2)}</span>}
            </div>
          )}
          {advErr && <div style={{ marginTop: 4, fontSize: 10, color: '#f85149' }}>{advErr}</div>}
          <div style={{ marginTop: 2, fontSize: 9, color: '#6e7681' }}>
            ML grid: brute-force joint MAP over a 2-D grid; better than pair-intersection when LoBs are oblique or σ is large.
            EKF: sequential Kalman update over LoBs; lets a moving DF head refine as it gathers bearings.
          </div>
        </div>
        <div style={{ borderTop: '1px solid #21262d', paddingTop: 6 }}>
          <div style={{ fontSize: 11, color: '#8b949e', marginBottom: 4 }}>Live AoA solver — array + method + frequency, run a one-shot solve on the selected SDR</div>
          <div style={{ display: 'flex', flexWrap: 'wrap', gap: 4, alignItems: 'center' }}>
            <input style={{ ...inp, width: 110 }} placeholder="freq (Hz)" value={aoaForm.frequency_hz}
                   onChange={e => setAoaForm(f => ({ ...f, frequency_hz: e.target.value }))} />
            <select style={{ ...inp, width: 88 }} value={aoaForm.method}
                    onChange={e => setAoaForm(f => ({ ...f, method: e.target.value }))}>
              <option value="music">MUSIC</option><option value="capon">Capon</option><option value="bartlett">Bartlett</option>
            </select>
            <select style={{ ...inp, width: 64 }} value={aoaForm.array_type}
                    onChange={e => setAoaForm(f => ({ ...f, array_type: e.target.value }))}>
              <option value="uca">UCA</option><option value="ula">ULA</option>
            </select>
            <input style={{ ...inp, width: 40 }} title="elements" value={aoaForm.n}
                   onChange={e => setAoaForm(f => ({ ...f, n: e.target.value }))} />
            <input style={{ ...inp, width: 60 }} title="spacing/radius (λ)" value={aoaForm.spacing_wavelengths}
                   onChange={e => setAoaForm(f => ({ ...f, spacing_wavelengths: e.target.value }))} />
            <input style={{ ...inp, width: 70 }} title="snapshots" value={aoaForm.n_snapshots}
                   onChange={e => setAoaForm(f => ({ ...f, n_snapshots: e.target.value }))} />
            <button style={{ ...btn, background: '#1f6feb', borderColor: '#1f6feb' }} disabled={aoaBusy} onClick={runAoaLive}>
              {aoaBusy ? 'Solving…' : 'Solve AoA'}
            </button>
          </div>
          {aoaResult && (
            <div style={{ marginTop: 4, fontSize: 11 }}>
              <strong style={{ color: '#06d6a0' }}>{aoaResult.azimuth_deg != null ? `${aoaResult.azimuth_deg.toFixed(1)}°` : '—'}</strong>
              {aoaResult.azimuth_sigma_deg != null && <span style={{ color: '#6e7681' }}> ±{aoaResult.azimuth_sigma_deg.toFixed(2)}°</span>}
              {aoaResult.elevation_deg != null && <span> · el {aoaResult.elevation_deg.toFixed(1)}°</span>}
              {aoaResult.snr_db != null && <span> · SNR {aoaResult.snr_db.toFixed(1)} dB</span>}
              <div style={{ color: '#6e7681', fontSize: 10 }}>
                {aoaResult.method?.toUpperCase()} · {aoaResult.snapshots} snapshots × {aoaResult.channels} ch · {aoaResult.iq_source}
                {aoaResult.synthetic ? ' (synthetic IQ — install SoapySDR to go live)' : ''}
                {aoaResult.ambiguities?.length > 0 && ` · alt: ${aoaResult.ambiguities.map(a => `${a.az_deg?.toFixed?.(0)}°`).join(', ')}`}
              </div>
            </div>
          )}
          {aoaErr && <div style={{ marginTop: 4, fontSize: 10, color: '#f85149' }}>{aoaErr}</div>}
        </div>
        <div style={{ borderTop: '1px solid #21262d', paddingTop: 6 }}>
          <button style={btn} onClick={() => setAlertSettingsOpen(v => !v)}>
            🔔 {alertSettingsOpen ? 'Hide' : 'DF alerts…'}
          </button>
          {alertSettingsOpen && <div style={{ marginTop: 6 }}><DfAlertsSettings /></div>}
        </div>
        <div style={{ fontSize: 10, color: '#6e7681', borderTop: '1px solid #21262d', paddingTop: 6 }}>
          GPS: {gps ? `${gps.lat.toFixed(5)}, ${gps.lon.toFixed(5)} (${gps.source})` : 'not set — set it in the SDR console; LoBs plot from your location'}
        </div>
      </div>
    </div>
  )
}

function Compass({ lobs = [], dev = null, maxSize = 200 }) {
  // SVG draws in a fixed 180-unit viewBox; the CSS width sizes it to the parent
  // with a hard cap of maxSize so it never bloats on very wide panels.
  const S = 180, c = S / 2, R = c - 14
  const heading = dev?.azimuth_reference === 'relative' ? (dev.antenna_heading_deg || 0) : 0
  return (
    <svg viewBox={`0 0 ${S} ${S}`} preserveAspectRatio="xMidYMid meet"
         style={{ width: '100%', maxWidth: maxSize, height: 'auto', marginTop: 6 }}>
      <circle cx={c} cy={c} r={R} fill="#0a0e13" stroke="#30363d" />
      {[0, 90, 180, 270].map(a => {
        const rad = (a - 90) * Math.PI / 180
        return <line key={a} x1={c} y1={c} x2={c + R * Math.cos(rad)} y2={c + R * Math.sin(rad)} stroke="#1c2530" />
      })}
      {['N', 'E', 'S', 'W'].map((l, i) => {
        const a = i * 90, rad = (a - 90) * Math.PI / 180
        return <text key={l} x={c + (R - 6) * Math.cos(rad)} y={c + (R - 6) * Math.sin(rad) + 3} fill="#5b6b7a" fontSize="9" textAnchor="middle">{l}</text>
      })}
      {/* the antenna-front reference (relative / clock modes) */}
      {dev && dev.azimuth_reference !== 'absolute' && (() => { const rad = (heading - 90) * Math.PI / 180; return <line x1={c} y1={c} x2={c + R * Math.cos(rad)} y2={c + R * Math.sin(rad)} stroke="#6e7681" strokeDasharray="3 3" /> })()}
      {lobs.map((l, i) => {
        const az = (l.azimuth_deg || 0)            // Absolute LOB (the dial is true-north up)
        const rad = (az - 90) * Math.PI / 180
        const x2 = c + R * Math.cos(rad), y2 = c + R * Math.sin(rad)
        const hot = i === lobs.length - 1
        return (
          <g key={l.id || i}>
            <line x1={c} y1={c} x2={x2} y2={y2} stroke={hot ? '#06d6a0' : 'rgba(6,214,160,0.45)'} strokeWidth={hot ? 2 : 1.2} />
            {hot && <text x={x2} y={y2 - 2} fill="#06d6a0" fontSize="9" textAnchor="middle">{az.toFixed(0)}°</text>}
          </g>
        )
      })}
      <circle cx={c} cy={c} r={3} fill="#06d6a0" />
      {/* latest LoB in all three representations */}
      {lobs.length > 0 && (() => {
        const az = lobs[lobs.length - 1].azimuth_deg || 0
        const rel = ((az - heading) % 360 + 360) % 360
        const ch = (Math.round(rel / 30) % 12) || 12
        return <text x={c} y={S - 4} fill="#8b949e" fontSize="9" textAnchor="middle">abs {az.toFixed(0)}° · rel {rel.toFixed(0)}° · {ch} o'clock</text>
      })()}
    </svg>
  )
}
