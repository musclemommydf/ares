import { useEffect, useRef, useState } from 'react'
import { X, RefreshCw, Radio, Crosshair, MapPin, Layers, FileSearch } from 'lucide-react'
import {
  getUasFeedTypes, scanUas, startUasDecode, getUasSessions, getUasSessionMetadata,
  deleteUasSession, exploitUasSession, parseRid,
} from '../../api/client'

const MHz = (hz) => (hz / 1e6).toFixed(3)
const card = { background: '#0d1117', border: '1px solid #21262d', borderRadius: 8 }
const th = { textAlign: 'left', fontSize: 10, color: '#8b949e', fontWeight: 600, padding: '4px 8px', whiteSpace: 'nowrap' }
const td = { fontSize: 11, color: '#c9d1d9', padding: '4px 8px', borderTop: '1px solid #161b22', whiteSpace: 'nowrap' }

export default function UasVideoPanel({ onClose, mapCenter, onLoadGeoJSON, onLocate, embedded = false }) {
  const [feedTypes, setFeedTypes] = useState([])
  const [deviceId, setDeviceId] = useState('')
  const [startMHz, setStartMHz] = useState('5645')
  const [stopMHz, setStopMHz] = useState('5945')
  const [scanning, setScanning] = useState(false)
  const [scanErr, setScanErr] = useState('')
  const [detections, setDetections] = useState([])
  const [session, setSession] = useState(null)
  const [metadata, setMetadata] = useState(null)
  const [exploit, setExploit] = useState(null)
  const [showRef, setShowRef] = useState(false)
  const [decodeFreqMHz, setDecodeFreqMHz] = useState('5800')
  const [ridHex, setRidHex] = useState('')
  const [ridResult, setRidResult] = useState(null)
  const [ridErr, setRidErr] = useState('')
  const pollRef = useRef(null)

  const doParseRid = async () => {
    setRidErr(''); setRidResult(null)
    const hex = ridHex.replace(/[^0-9a-fA-F]/g, '')
    if (!hex) { setRidErr('paste the hex bytes of a captured Remote-ID / DroneID message'); return }
    try { setRidResult(await parseRid(hex, 'auto')) }
    catch (e) { setRidErr('Parse failed: ' + (e?.response?.data?.detail || e?.message || e)) }
  }
  const ridSum = ridResult?.parsed?.summary || ridResult?.parsed
  const [frameTick, setFrameTick] = useState(0)        // cache-buster for the decoded-video <img>
  const frameRef = useRef(null)

  useEffect(() => {
    getUasFeedTypes().then(d => setFeedTypes(d.feed_types || [])).catch(() => {})
    getUasSessions().then(d => { const s = (d.sessions || [])[0]; if (s) setSession(s) }).catch(() => {})
    return () => { if (pollRef.current) clearInterval(pollRef.current); if (frameRef.current) clearInterval(frameRef.current) }
  }, [])

  // refresh the decoded raster-frame image while an analog-video session is live
  useEffect(() => {
    if (frameRef.current) { clearInterval(frameRef.current); frameRef.current = null }
    if (!session?.video_url) return
    frameRef.current = setInterval(() => setFrameTick(t => t + 1), 900)
    return () => { if (frameRef.current) clearInterval(frameRef.current) }
  }, [session?.video_url])

  // poll the decoded MISB metadata while a session with a metadata_url is live
  useEffect(() => {
    if (pollRef.current) { clearInterval(pollRef.current); pollRef.current = null }
    if (!session?.id || !session?.metadata_url) { setMetadata(null); return }
    const tick = () => getUasSessionMetadata(session.id).then(setMetadata).catch(() => {})
    tick()
    pollRef.current = setInterval(tick, 2000)
    return () => { if (pollRef.current) clearInterval(pollRef.current) }
  }, [session?.id])

  const doScan = async () => {
    setScanning(true); setScanErr(''); setDetections([])
    try {
      const r = await scanUas({
        device_id: deviceId || undefined,
        start_hz: Number(startMHz) * 1e6, stop_hz: Number(stopMHz) * 1e6,
        step_hz: 20e6, use_iq: false,
      })
      setDetections(r.detections || [])
      if (!r.detections?.length) setScanErr('No occupied channels found in that band.')
    } catch (e) { setScanErr('Scan failed: ' + (e?.response?.data?.detail || e?.message || e)) }
    finally { setScanning(false) }
  }

  const decode = async (d) => {
    setExploit(null); setMetadata(null)
    try {
      const s = await startUasDecode({
        device_id: deviceId || undefined, frequency_hz: d.center_hz, feed_type: d.feed_type,
        bandwidth_hz: d.bandwidth_hz, label: d.feed_name, push_to_atak: true,
      })
      setSession(s)
    } catch (e) { setScanErr('Decode start failed: ' + (e?.response?.data?.detail || e?.message || e)) }
  }
  const decodeAtFreq = async () => {
    setExploit(null); setMetadata(null); setScanErr('')
    try {
      const s = await startUasDecode({ device_id: deviceId || undefined, frequency_hz: Number(decodeFreqMHz) * 1e6, bandwidth_hz: 8e6, push_to_atak: true })
      if (s.error) setScanErr(s.error); else setSession(s)
    } catch (e) { setScanErr('Decode failed: ' + (e?.response?.data?.detail || e?.message || e)) }
  }
  const stopSession = async () => { if (session?.id) { try { await deleteUasSession(session.id) } catch {} } setSession(null); setMetadata(null); setExploit(null) }
  const runExploit = async () => { if (!session?.id) return; try { setExploit(await exploitUasSession(session.id)) } catch (e) { setExploit({ error: e?.response?.data?.detail || String(e) }) } }
  const addToMap = () => {
    const fc = exploit?.geojson || metadata?.geojson
    if (fc?.features?.length) onLoadGeoJSON?.('UAS: ' + (metadata?.klv?.platform_call_sign || session?.label || 'feed'), fc)
  }
  const klv = metadata?.klv

  const body = (
    <>
        <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 10 }}>
          <Radio size={16} color="#22d3ee" />
          <b style={{ fontSize: 14, color: '#e6edf3' }}>UAS Video — downlink scanner · decoder · exploitation</b>
          {!embedded && <button className="btn btn-ghost" style={{ marginLeft: 'auto', padding: '2px 6px' }} onClick={onClose}><X size={14} /></button>}
        </div>

        {/* scan controls */}
        <div style={{ ...card, padding: 10, marginBottom: 12 }}>
          <div style={{ display: 'flex', flexWrap: 'wrap', gap: 8, alignItems: 'flex-end' }}>
            <label style={{ fontSize: 11, color: '#8b949e' }}>SDR device id<br /><input value={deviceId} onChange={e => setDeviceId(e.target.value)} placeholder="(registered SDR · blank = synthetic)" style={{ width: 220, fontSize: 11 }} /></label>
            <label style={{ fontSize: 11, color: '#8b949e' }}>Start (MHz)<br /><input type="number" value={startMHz} onChange={e => setStartMHz(e.target.value)} style={{ width: 100, fontSize: 11 }} /></label>
            <label style={{ fontSize: 11, color: '#8b949e' }}>Stop (MHz)<br /><input type="number" value={stopMHz} onChange={e => setStopMHz(e.target.value)} style={{ width: 100, fontSize: 11 }} /></label>
            <button className="btn btn-primary" disabled={scanning} onClick={doScan} style={{ gap: 6 }}>
              {scanning ? <><RefreshCw size={13} className="spin" /> Scanning…</> : <><FileSearch size={13} /> Scan band</>}
            </button>
            <span style={{ fontSize: 10, color: '#6e7681' }}>presets:&nbsp;
              <a onClick={() => { setStartMHz('5645'); setStopMHz('5945') }} style={{ cursor: 'pointer', color: '#58a6ff' }}>5.8 GHz FPV</a> ·&nbsp;
              <a onClick={() => { setStartMHz('1040'); setStopMHz('1360') }} style={{ cursor: 'pointer', color: '#58a6ff' }}>1.2 GHz</a> ·&nbsp;
              <a onClick={() => { setStartMHz('2370'); setStopMHz('2510') }} style={{ cursor: 'pointer', color: '#58a6ff' }}>2.4 GHz</a>
            </span>
          </div>
          <div style={{ display: 'flex', flexWrap: 'wrap', gap: 8, alignItems: 'flex-end', marginTop: 8, borderTop: '1px solid #21262d', paddingTop: 8 }}>
            <label style={{ fontSize: 11, color: '#8b949e' }}>Or — tune & let Ares auto-detect:<br /><input type="number" value={decodeFreqMHz} onChange={e => setDecodeFreqMHz(e.target.value)} style={{ width: 100, fontSize: 11 }} /> MHz</label>
            <button className="btn btn-primary" onClick={decodeAtFreq} style={{ gap: 6 }}>Detect & decode @ {decodeFreqMHz} MHz</button>
            <span style={{ fontSize: 10, color: '#6e7681' }}>scans a window around the tune freq, classifies it (or falls back to the channel plan), then opens the decode session + video pane.</span>
          </div>
          {scanErr && <div style={{ fontSize: 11, color: '#f0883e', marginTop: 6 }}>{scanErr}</div>}
        </div>

        {/* detections */}
        {detections.length > 0 && (
          <div style={{ ...card, marginBottom: 12, maxHeight: 260, overflowY: 'auto' }}>
            <table style={{ width: '100%', borderCollapse: 'collapse' }}>
              <thead><tr style={{ position: 'sticky', top: 0, background: '#0d1117' }}>
                <th style={th}>Centre</th><th style={th}>BW</th><th style={th}>Feed</th><th style={th}>Modulation</th>
                <th style={th}>RSSI</th><th style={th}>Conf.</th><th style={th}>KLV</th><th style={th}></th>
              </tr></thead>
              <tbody>{detections.map((d, i) => (
                <tr key={i}>
                  <td style={td}>{MHz(d.center_hz)} MHz</td>
                  <td style={td}>{(d.bandwidth_hz / 1e6).toFixed(2)} MHz</td>
                  <td style={td}>{d.feed_name}</td>
                  <td style={{ ...td, color: '#8b949e' }}>{d.modulation}</td>
                  <td style={td}>{d.rssi_dbm} dBm</td>
                  <td style={td}>{Math.round((d.confidence || 0) * 100)}%</td>
                  <td style={td}>{d.carries_klv ? '✓' : '—'}</td>
                  <td style={td}><button className={`btn ${d.decodable ? 'btn-primary' : 'btn-ghost'}`} style={{ fontSize: 10, padding: '2px 8px' }} onClick={() => decode(d)}>{d.decodable ? 'Decode' : 'Characterise'}</button></td>
                </tr>
              ))}</tbody>
            </table>
          </div>
        )}

        {/* active session + metadata + exploitation */}
        {session && (
          <div style={{ ...card, padding: 10, marginBottom: 12 }}>
            <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 6, flexWrap: 'wrap' }}>
              <b style={{ fontSize: 12, color: '#e6edf3' }}>{session.feed_name}</b>
              <span style={{ fontSize: 11, color: '#8b949e' }}>@ {MHz(session.frequency_hz)} MHz · {session.transport}</span>
              <span style={{ fontSize: 10, padding: '1px 6px', borderRadius: 4, background: session.status === 'started' ? '#1f6f3f' : session.status === 'characterize_only' ? '#7a5b16' : '#5a1d1d', color: '#fff' }}>{session.status}</span>
              <span style={{ fontSize: 10, color: '#6e7681' }}>capture: {session.capture_backend}</span>
              <button className="btn btn-ghost" style={{ marginLeft: 'auto', fontSize: 10, padding: '2px 8px' }} onClick={stopSession}>Stop</button>
            </div>
            {session.message && <div style={{ fontSize: 11, color: '#8b949e', marginBottom: 6 }}>{session.message}</div>}
            {session.pipeline?.length > 0 && <div style={{ fontSize: 10, color: '#6e7681', marginBottom: 6 }}>pipeline: {session.pipeline.join(' → ')}</div>}
            {session.auto_detected && <div style={{ fontSize: 10, color: '#3fb950', marginBottom: 6 }}>auto-detected{session.auto_detected.from ? ` (${session.auto_detected.from})` : ''} — {Math.round((session.auto_detected.confidence || 0) * 100)}% confident{session.auto_detected.alternatives?.length ? `; alt: ${session.auto_detected.alternatives.map(a => a.feed_type).join(', ')}` : ''}</div>}

            {/* decoded video / demod readout */}
            {session.video_url ? (
              // analog feed: the native FM/composite demod recovered raster frame(s) — re-fetch periodically
              <img alt="decoded video frame" src={`${session.video_url}?i=${frameTick}`}
                   style={{ width: '100%', maxHeight: 300, objectFit: 'contain', background: '#000', borderRadius: 6, marginBottom: 8, imageRendering: 'pixelated' }}
                   onError={e => { e.currentTarget.style.display = 'none' }} />
            ) : (
              <div style={{ ...card, background: '#000', borderRadius: 6, marginBottom: 8, padding: 14, minHeight: 110, display: 'flex', flexDirection: 'column', justifyContent: 'center', textAlign: 'center', gap: 6 }}>
                {session.transport === 'proprietary'
                  ? <div style={{ fontSize: 12, color: '#8b949e' }}>Proprietary / encrypted feed — characterise &amp; geolocate only (no decryptable video).</div>
                  : session.demod?.ok
                    ? <div style={{ fontSize: 11, color: '#22d3ee' }}>
                        Native {session.demod.kind} demod{session.demod.fft_len ? ` · ${session.demod.fft_len}/${session.demod.cp_len}` : ''}{session.demod.modulation ? ` · ${session.demod.modulation}` : ''}
                        {session.demod.n_symbols != null ? ` · ${session.demod.n_symbols.toLocaleString()} sym` : ''}
                        {session.demod.evm_pct != null ? ` · EVM ${session.demod.evm_pct}%` : ''}
                        {session.demod.cfo_hz_est != null ? ` · CFO ${session.demod.cfo_hz_est} Hz` : ''}
                        {session.demod.ts?.ts_sync ? ` · TS sync (${session.demod.ts.klv_units} KLV)` : ' · PHY symbols recovered (no TS sync — inner FEC stage / cleaner link needed)'}
                      </div>
                    : <div style={{ fontSize: 12, color: '#8b949e' }}>{session.demod?.error || session.demod?.reason || 'Capture + native software demod running…'}</div>}
                {klv && <div style={{ fontSize: 11, color: '#22d3ee' }}>live MISB: {klv.platform_call_sign || 'UAS'} @ {klv.sensor_lat_deg?.toFixed(4)}, {klv.sensor_lon_deg?.toFixed(4)} · {klv.sensor_true_alt_m?.toFixed(0)} m · slant {klv.slant_range_m?.toFixed(0)} m{klv._synthetic ? ' (synthetic)' : ''}</div>}
              </div>
            )}

            {klv && (
              <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(170px, 1fr))', gap: 6, fontSize: 11, marginBottom: 6 }}>
                <div><span style={{ color: '#8b949e' }}>Platform&nbsp;</span>{klv.platform_call_sign || klv.platform_designation || 'UAS'}{klv._synthetic ? ' (synthetic)' : ''}</div>
                <div><span style={{ color: '#8b949e' }}>Position&nbsp;</span>{klv.sensor_lat_deg?.toFixed(5)}, {klv.sensor_lon_deg?.toFixed(5)}</div>
                <div><span style={{ color: '#8b949e' }}>Alt&nbsp;</span>{klv.sensor_true_alt_m?.toFixed(0)} m · hdg {klv.platform_heading_deg?.toFixed(0)}°</div>
                <div><span style={{ color: '#8b949e' }}>Slant range&nbsp;</span>{klv.slant_range_m?.toFixed(0)} m · HFOV {klv.sensor_hfov_deg?.toFixed(1)}°</div>
                <div><span style={{ color: '#8b949e' }}>Frame centre&nbsp;</span>{klv.frame_center_lat_deg?.toFixed(5)}, {klv.frame_center_lon_deg?.toFixed(5)}</div>
                <div><span style={{ color: '#8b949e' }}>Footprint&nbsp;</span>{(metadata?.footprint?.length || 0)} pts</div>
              </div>
            )}
            <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap' }}>
              {klv && <button className="btn btn-ghost" style={{ fontSize: 10, padding: '3px 8px', gap: 4 }} onClick={() => onLocate?.(klv.sensor_lat_deg, klv.sensor_lon_deg)}><Crosshair size={12} /> Fly to platform</button>}
              {(metadata?.geojson || exploit?.geojson) && <button className="btn btn-ghost" style={{ fontSize: 10, padding: '3px 8px', gap: 4 }} onClick={addToMap}><Layers size={12} /> Add platform/footprint to map</button>}
              <button className="btn btn-primary" style={{ fontSize: 10, padding: '3px 8px', gap: 4 }} onClick={runExploit}><MapPin size={12} /> Exploit — demux TS / KLV track / modulation ID</button>
            </div>

            {exploit && !exploit.error && (
              <div style={{ ...card, padding: 8, marginTop: 8, fontSize: 11 }}>
                <b style={{ color: '#e6edf3' }}>Exploitation result</b>
                <div style={{ color: '#8b949e', marginTop: 4 }}>
                  TS: {exploit.demux?.streams?.length || 0} stream(s) — {(exploit.video_codecs || []).join(', ') || '—'} video, {exploit.klv_track_len} KLV frame(s).
                  &nbsp;Signal: <b style={{ color: '#c9d1d9' }}>{exploit.signal_characterization?.family}</b>{exploit.signal_characterization?.likely_system ? ` — ${exploit.signal_characterization.likely_system}` : ''}{exploit.signal_characterization?.ofdm_fft_len ? ` (FFT ${exploit.signal_characterization.ofdm_fft_len}, GI ${exploit.signal_characterization.ofdm_guard_fraction})` : ''}.
                </div>
                <div style={{ color: '#6e7681', marginTop: 4 }}>Frames: {exploit.frame_exploit?.available ? 'ffmpeg available' : (exploit.frame_exploit?.pipeline || []).join(' · ')}.</div>
              </div>
            )}
            {exploit?.error && <div style={{ fontSize: 11, color: '#f0883e', marginTop: 6 }}>Exploit failed: {exploit.error}</div>}
          </div>
        )}

        {/* feed-type reference */}
        <div>
          <a onClick={() => setShowRef(v => !v)} style={{ cursor: 'pointer', fontSize: 11, color: '#58a6ff' }}>{showRef ? '▾' : '▸'} Feed types Ares recognises ({feedTypes.length})</a>
          {showRef && (
            <div style={{ ...card, marginTop: 6, maxHeight: 220, overflowY: 'auto' }}>
              <table style={{ width: '100%', borderCollapse: 'collapse' }}>
                <thead><tr><th style={th}>Feed</th><th style={th}>Transport</th><th style={th}>Modulation</th><th style={th}>KLV</th><th style={th}>Decodable</th></tr></thead>
                <tbody>{feedTypes.map(f => (
                  <tr key={f.id}>
                    <td style={td} title={f.notes}>{f.name}</td>
                    <td style={{ ...td, color: '#8b949e' }}>{f.transport}</td>
                    <td style={{ ...td, color: '#8b949e' }}>{f.modulation}</td>
                    <td style={td}>{f.carries_klv ? '✓' : '—'}</td>
                    <td style={td}>{f.decodable ? '✓' : 'characterise-only'}</td>
                  </tr>
                ))}</tbody>
              </table>
            </div>
          )}
        </div>
        {/* Remote ID / DJI DroneID — parse a captured telemetry-beacon message */}
        <div style={{ ...card, padding: 10, marginTop: 12 }}>
          <b style={{ fontSize: 12, color: '#e6edf3' }}>Remote ID / DJI DroneID — decode a captured beacon</b>
          <div style={{ fontSize: 10, color: '#6e7681', margin: '4px 0 6px' }}>The unencrypted broadcast every modern drone emits (WiFi/BT for ASTM F3411; an OFDM burst for DJI DroneID) — drone serial, position, and the operator/pilot location. Paste the message bytes (hex) — or run a /uas/rid/decode session from the API for a live WiFi/BT capture.</div>
          <textarea value={ridHex} onChange={e => setRidHex(e.target.value)} placeholder="e.g. f2 19 03 02 31 35 38 31 …  (an F3411 message / Message Pack, or a de-framed DJI DroneID payload)"
                    rows={2} style={{ width: '100%', fontSize: 10, fontFamily: 'monospace', resize: 'vertical' }} />
          <div style={{ display: 'flex', gap: 6, alignItems: 'center', marginTop: 6 }}>
            <button className="btn btn-primary" style={{ fontSize: 10, padding: '3px 10px' }} onClick={doParseRid}>Decode</button>
            {ridErr && <span style={{ fontSize: 10, color: '#f0883e' }}>{ridErr}</span>}
          </div>
          {ridSum && (
            <div style={{ ...card, padding: 8, marginTop: 8, fontSize: 11 }}>
              <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(190px, 1fr))', gap: 6 }}>
                <div><span style={{ color: '#8b949e' }}>Format&nbsp;</span>{ridSum.format || ridResult.format}</div>
                <div><span style={{ color: '#8b949e' }}>Serial&nbsp;</span>{ridSum.serial || '—'} {ridSum.ua_type ? `(${ridSum.ua_type})` : ''}</div>
                <div><span style={{ color: '#8b949e' }}>Drone&nbsp;</span>{ridSum.drone_lat != null ? `${ridSum.drone_lat.toFixed(5)}, ${ridSum.drone_lon.toFixed(5)}` : '—'}{ridSum.drone_alt_m != null ? ` · ${ridSum.drone_alt_m} m` : ''}{ridSum.operational_status ? ` · ${ridSum.operational_status}` : ''}</div>
                <div><span style={{ color: '#8b949e' }}>Operator&nbsp;</span>{ridSum.operator_lat != null ? `${ridSum.operator_lat.toFixed(5)}, ${ridSum.operator_lon.toFixed(5)}` : '—'}{ridSum.operator_id ? ` · ${ridSum.operator_id}` : ''}</div>
                {ridSum.drone_speed_m_s != null && <div><span style={{ color: '#8b949e' }}>Speed&nbsp;</span>{ridSum.drone_speed_m_s} m/s{ridSum.drone_track_deg != null ? ` @ ${ridSum.drone_track_deg}°` : ''}</div>}
                {ridSum.area_radius_m ? <div><span style={{ color: '#8b949e' }}>Op. area&nbsp;</span>{ridSum.area_radius_m} m radius</div> : null}
                {ridSum.note && <div style={{ gridColumn: '1/-1', color: '#8b949e' }}>{ridSum.note}</div>}
              </div>
              <div style={{ display: 'flex', gap: 6, marginTop: 6 }}>
                {ridSum.drone_lat != null && <button className="btn btn-ghost" style={{ fontSize: 10, padding: '3px 8px', gap: 4 }} onClick={() => onLocate?.(ridSum.drone_lat, ridSum.drone_lon)}><Crosshair size={12} /> Fly to drone</button>}
                {ridSum.operator_lat != null && <button className="btn btn-ghost" style={{ fontSize: 10, padding: '3px 8px', gap: 4 }} onClick={() => onLocate?.(ridSum.operator_lat, ridSum.operator_lon)}><Crosshair size={12} /> Fly to operator</button>}
                {ridResult?.geojson?.features?.length > 0 && <button className="btn btn-ghost" style={{ fontSize: 10, padding: '3px 8px', gap: 4 }} onClick={() => onLoadGeoJSON?.('Remote ID: ' + (ridSum.serial || ridSum.operator_id || 'UAS'), ridResult.geojson)}><Layers size={12} /> Add drone + operator + area to map</button>}
              </div>
            </div>
          )}
        </div>

        <div style={{ fontSize: 10, color: '#6e7681', marginTop: 8 }}>
          Ares demodulates the downlink in-process (its own software demod — FM/VSB composite video, OFDM/COFDM, single-carrier PSK/QAM → MPEG-TS demux + STANAG-4609 KLV); no SoapySDR / leandvb / DVB-T(2) receiver / SDRangel / ffmpeg / TSDuck is required. A wired IQ provider feeds it real baseband; otherwise a synthetic snapshot drives the offline demo. PHY only — the broadcast inner FEC (DVB Viterbi+RS / DVB-S2 LDPC+BCH) is the next stage; H.264/H.265 elementary-stream decode of a recovered TS still benefits from ffmpeg when present.
        </div>
    </>
  )

  if (embedded) {
    return <div style={{ height: '100%', overflowY: 'auto', padding: '12px 14px' }}>{body}</div>
  }
  return (
    <div onClick={onClose} style={{ position: 'fixed', inset: 0, background: 'rgba(0,0,0,0.55)', zIndex: 10000, display: 'flex', alignItems: 'flex-start', justifyContent: 'center', padding: '40px 16px', overflowY: 'auto' }}>
      <div onClick={e => e.stopPropagation()} style={{ ...card, width: 'min(960px, 100%)', background: '#161b22', padding: 16 }}>{body}</div>
    </div>
  )
}
