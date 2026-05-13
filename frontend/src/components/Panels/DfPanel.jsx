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
import { getSdrSpectrum, getDfAccuracyEstimate, getAudioModes, startSdrAudio, updateSdrDevice, getSdrState, getCompassModes, calibrateCompass } from '../../api/client'

const _WATERFALL_MAX = 140   // rows of waterfall history kept per channel

const inp = { background: '#0d1117', border: '1px solid #30363d', borderRadius: 4, color: '#e6edf3', fontSize: 11, padding: '3px 5px', width: 86 }
const lab = { fontSize: 10, color: '#8b949e', display: 'block', marginBottom: 2 }
const btn = { background: '#21262d', border: '1px solid #30363d', borderRadius: 4, color: '#c9d1d9', padding: '3px 8px', cursor: 'pointer', fontSize: 11 }

export default function DfPanel() {
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
  const [expandPerCh, setExpandPerCh] = useState(false)   // by default show one spectrum per *device*; toggle to one per channel
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
    setCenter(dev.frequency_hz || 433.92e6); setTuneHz(dev.frequency_hz || 433.92e6); setThreshold(dev.df_threshold_dbm ?? -90)
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
          getSdrSpectrum(dev.id, { center_hz: center, span_hz: span, n_bins: 1024, channel: ch }).catch(() => null)))
        if (stop) return
        setFrames(got)
        setHist(prev => got.map((fr, ch) => fr ? [...((prev[ch] || [])).slice(-(_WATERFALL_MAX - 1)), fr] : (prev[ch] || [])))
      } catch { /* ignore */ }
    }
    tick(); const h = setInterval(tick, 700)
    return () => { stop = true; clearInterval(h) }
  }, [devId, center, span, nCh])  // eslint-disable-line

  const recentLobs = useMemo(() => {
    const cut = Date.now() / 1000 - 90
    return (lobs || []).filter(l => (l.t || 0) >= cut).slice(-12)
  }, [lobs])

  const applyThreshold = async (v) => {
    setThreshold(v)
    if (dev) updateSdrDevice(dev.id, { df_threshold_dbm: Number(v) }).catch(() => {})
  }
  const listen = async () => {
    if (!dev) return
    try { setAudioStatus(await startSdrAudio(dev.id, tuneHz, demod)) }
    catch (e) { setAudioStatus({ status: 'error', detail: String(e?.response?.data?.detail || e?.message || e) }) }
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
    <div style={{ display: 'flex', height: '100%', minHeight: 0, gap: 8, padding: 8, fontSize: 12, color: '#e6edf3' }}>
      {/* LEFT ≈ 50% — stacked spectrum viewers (one per channel) */}
      <div style={{ flex: '1 1 50%', minWidth: 0, overflowY: 'auto', display: 'flex', flexDirection: 'column', gap: 6 }}>
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
          <label style={{ color: '#8b949e' }}>centre <input style={inp} type="number" value={center} onChange={e => setCenter(Number(e.target.value) || center)} /> Hz</label>
          <label style={{ color: '#8b949e' }}>span <input style={inp} type="number" value={span} onChange={e => setSpan(Math.max(1e3, Number(e.target.value) || span))} /> Hz</label>
          <span style={{ color: '#6e7681', fontSize: 10 }}>min {((center - span / 2) / 1e6).toFixed(3)} – max {((center + span / 2) / 1e6).toFixed(3)} MHz</span>
        </div>
        {(() => {
          // by default show one spectrum per *device* (channel 0); expand to one per channel on demand
          const allFrames = frames.length ? frames : [null]
          const shown = (nCh > 1 && !expandPerCh) ? [allFrames[0] ?? null] : allFrames
          const labelFor = (i) => (nCh > 1 && expandPerCh) ? `ch${i}` : (nCh > 1 ? `${nCh}ch — ch0` : '')
          const heightFor = () => (shown.length > 1) ? Math.max(90, Math.floor(360 / shown.length)) : 200
          return shown.map((fr, i) => (
            <SpectrumViewer key={i} frame={fr ? { ...fr, df_threshold_dbm: threshold } : null}
              label={labelFor(i)} tuneHz={tuneHz} onTune={setTuneHz} history={hist[i] || []} height={heightFor()} />
          ))
        })()}
      </div>

      {/* MIDDLE — compass of LoB bearings */}
      <div style={{ flex: '0 0 200px', display: 'flex', flexDirection: 'column', alignItems: 'center', justifyContent: 'flex-start', gap: 6 }}>
        <Compass lobs={recentLobs} dev={dev} />
        <div style={{ fontSize: 10, color: '#8b949e', textAlign: 'center' }}>
          {canDf ? `${recentLobs.length} active LoB(s)` : 'single-channel — no LoBs (DF needs ≥2 coherent channels)'}
          {dev?.azimuth_reference === 'relative' && <div>(clock = off the antenna front, heading {Math.round(dev.antenna_heading_deg || 0)}°)</div>}
        </div>
      </div>

      {/* RIGHT — DF options & parameters */}
      <div style={{ flex: '0 0 230px', overflowY: 'auto', display: 'flex', flexDirection: 'column', gap: 8, borderLeft: '1px solid #21262d', paddingLeft: 8 }}>
        <div>
          <span style={lab}>DF tune frequency (drop the tuner on a signal in the spectrum, or type)</span>
          <input style={{ ...inp, width: 130 }} type="number" value={Math.round(tuneHz)} onChange={e => setTuneHz(Number(e.target.value) || tuneHz)} /> Hz
          <div style={{ fontSize: 10, color: '#6e7681' }}>{(tuneHz / 1e6).toFixed(5)} MHz</div>
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
          <select style={{ ...inp, width: 170 }} value={demod} onChange={e => setDemod(e.target.value)}>
            {(audioModes.length ? audioModes : [{ id: 'nfm', label: 'Narrowband FM' }]).map(m =>
              <option key={m.id} value={m.id}>{m.label}{m.ready === false ? ' (decoder not installed)' : ''}</option>)}
          </select>
          <button style={{ ...btn, marginLeft: 6 }} onClick={listen}>▶ Listen</button>
          {audioStatus && <div style={{ fontSize: 10, color: (audioStatus.status === 'error' || (audioStatus.detail || '').startsWith('⚠')) ? '#f85149' : '#8b949e', marginTop: 3 }}>{audioStatus.detail || audioStatus.status}</div>}
        </div>
        {/* Compass mode (3) + calibration */}
        {dev && dev.source_class !== 'single_channel' && (
          <div style={{ borderTop: '1px solid #21262d', paddingTop: 6 }}>
            <span style={lab}>compass mode</span>
            <select style={{ ...inp, width: 200 }} value={dev.azimuth_reference || 'absolute'} onChange={e => setCompassMode(e.target.value)}>
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
        <div style={{ fontSize: 10, color: '#6e7681', borderTop: '1px solid #21262d', paddingTop: 6 }}>
          GPS: {gps ? `${gps.lat.toFixed(5)}, ${gps.lon.toFixed(5)} (${gps.source})` : 'not set — set it in the SDR console; LoBs plot from your location'}
        </div>
      </div>
    </div>
  )
}

function Compass({ lobs = [], dev = null }) {
  const S = 180, c = S / 2, R = c - 14
  const heading = dev?.azimuth_reference === 'relative' ? (dev.antenna_heading_deg || 0) : 0
  return (
    <svg width={S} height={S} style={{ marginTop: 6 }}>
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
