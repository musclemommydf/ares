import { useEffect, useMemo, useRef, useState } from 'react'
import { Sigma, Crosshair, Send, FileUp, Trash2, RefreshCw, ListChecks } from 'lucide-react'
import {
  algorithmsList, algorithmsFeasibility,
  algoRssPathLoss, algoRssGradient, algoDopplerCpa, algoFdoaTrack,
  algoSyntheticAperture, algoPhaseInterferometry, algoTdoaMultiReceiver,
  algoMlGridFusion, algoEkfTrack,
} from '../../api/client'

// ─── styles ──────────────────────────────────────────────────────────────────
const card = { background: '#0d1117', border: '1px solid #21262d', borderRadius: 8, padding: 10, marginBottom: 10 }
const th = { textAlign: 'left', fontSize: 10, color: '#8b949e', fontWeight: 600, padding: '3px 6px', whiteSpace: 'nowrap' }
const td = { fontSize: 11, color: '#c9d1d9', padding: '3px 6px', borderTop: '1px solid #161b22', whiteSpace: 'nowrap' }
const lblSty = { fontSize: 11, color: '#8b949e', display: 'flex', flexDirection: 'column', gap: 2 }
const inputSty = { fontSize: 11, background: '#0d1117', color: '#c9d1d9', border: '1px solid #21262d', borderRadius: 4, padding: '3px 6px' }
const sectionH = { fontSize: 12, color: '#e6edf3', fontWeight: 600, marginBottom: 6, display: 'flex', alignItems: 'center', gap: 6 }

// ─── method catalogue (kept as a fallback if /algorithms/methods fails offline) ─
const METHOD_LIST = [
  { id: 'rss_path_loss',         name: 'RSS log-distance ML',              kinds: ['rss'], producesFix: true },
  { id: 'rss_gradient',          name: 'RSS-gradient bearing',             kinds: ['rss'], producesFix: false },
  { id: 'doppler_cpa',           name: 'Doppler closest-point-of-approach', kinds: ['doppler'], producesFix: true },
  { id: 'fdoa_track',            name: 'FDOA multi-pose grid',             kinds: ['doppler'], producesFix: true },
  { id: 'synthetic_aperture',    name: 'Kinematic synthetic-aperture DoA', kinds: ['iq'], producesFix: false },
  { id: 'phase_interferometry',  name: 'Phase-Δ along-track DoA',          kinds: ['iq'], producesFix: false },
  { id: 'tdoa_multi_receiver',   name: 'Multi-receiver TDOA',              kinds: ['tdoa'], producesFix: true },
  { id: 'ml_grid_fusion',        name: 'ML grid fusion (universal)',       kinds: ['aoa','rss','doppler','tdoa'], producesFix: true },
  { id: 'ekf_track',             name: 'EKF kinematic tracker',            kinds: ['aoa','rss','doppler'], producesFix: true },
]

// ─── sample template observations to make first-run obvious ──────────────────
const SAMPLE = {
  rss_path_loss: [
    { lat: 37.7700, lon: -122.4200, rssi_dbm: -45.2 },
    { lat: 37.7702, lon: -122.4205, rssi_dbm: -52.7 },
    { lat: 37.7695, lon: -122.4198, rssi_dbm: -48.9 },
    { lat: 37.7708, lon: -122.4210, rssi_dbm: -56.4 },
    { lat: 37.7692, lon: -122.4208, rssi_dbm: -50.1 },
    { lat: 37.7704, lon: -122.4196, rssi_dbm: -47.5 },
  ],
  rss_gradient: [
    { lat: 37.7700, lon: -122.4200, rssi_dbm: -45.2 },
    { lat: 37.7710, lon: -122.4200, rssi_dbm: -50.4 },
    { lat: 37.7700, lon: -122.4210, rssi_dbm: -53.0 },
  ],
  doppler_cpa: Array.from({ length: 14 }, (_, k) => {
    const t = -7 + k
    const v = 30, r0 = 200, f0 = 433e6, c = 299792458
    const x = v * t
    const df = -(f0 / c) * v * (-x) / Math.hypot(x, r0)
    return { t, frequency_offset_hz: df, v_mps: v, lat: 37.77 + x / 111132.92, lon: -122.42 }
  }),
  fdoa_track: Array.from({ length: 8 }, (_, k) => {
    const ang = k * (Math.PI * 2 / 8)
    const f0 = 433e6, c = 299792458
    const vx = 25 * Math.cos(ang), vy = 25 * Math.sin(ang)
    const dx = -100, dy = 200, d = Math.hypot(dx, dy)
    return { lat: 37.77 + k * 1e-5, lon: -122.42, vx_mps: vx, vy_mps: vy,
              frequency_offset_hz: -(f0 / c) * (vx * dx / d + vy * dy / d) }
  }),
  synthetic_aperture: Array.from({ length: 12 }, (_, k) => {
    const f0 = 433e6, c = 299792458, lam = c / f0
    const yk = k * 0.6
    const phase = -2 * Math.PI * Math.hypot(1000, yk) / lam
    return { x_m: 0, y_m: yk, iq_re: Math.cos(phase), iq_im: Math.sin(phase) }
  }),
  tdoa_multi_receiver: [
    { id: 'rx1', lat: 37.7700, lon: -122.4200, t_arrival_s: 0.00000000 },
    { id: 'rx2', lat: 37.7800, lon: -122.4200, t_arrival_s: 0.00003337 },
    { id: 'rx3', lat: 37.7700, lon: -122.4100, t_arrival_s: 0.00002700 },
  ],
  ml_grid_fusion: [
    { kind: 'aoa', lat: 37.7700, lon: -122.4200, bearing_deg: 35, sigma_deg: 3 },
    { kind: 'aoa', lat: 37.7780, lon: -122.4150, bearing_deg: 280, sigma_deg: 3 },
    { kind: 'rss', lat: 37.7720, lon: -122.4180, rssi_dbm: -40 },
  ],
  ekf_track: [
    { kind: 'aoa', lat: 37.7700, lon: -122.4200, bearing_deg: 35 },
    { kind: 'aoa', lat: 37.7720, lon: -122.4180, bearing_deg: 25 },
    { kind: 'aoa', lat: 37.7740, lon: -122.4160, bearing_deg: 10 },
  ],
}

// Auto-select priority: pick the most *specific* feasible method for the
// given observations. IQ-based DoA beats Doppler beats RSS beats the
// universal fallbacks. The universal fallbacks (ml_grid_fusion, ekf_track)
// are last so we only land there when nothing else fits.
const METHOD_PRIORITY = [
  'synthetic_aperture', 'phase_interferometry',
  'doppler_cpa', 'fdoa_track',
  'tdoa_multi_receiver',
  'rss_path_loss', 'rss_gradient',
  'ml_grid_fusion', 'ekf_track',
]

function pickBestMethod(feasibility) {
  if (!feasibility) return null
  for (const m of METHOD_PRIORITY) {
    if (feasibility[m]?.feasible) return m
  }
  return null
}

const KIND_HELP = {
  rss:     'lat, lon, rssi_dbm',
  doppler: 'lat, lon, t, frequency_offset_hz, v_mps (or vx_mps + vy_mps)',
  iq:      'x_m, y_m, iq_re, iq_im (one IQ sample per snapshot)',
  aoa:     'lat, lon, bearing_deg [, sigma_deg]',
  tdoa:    'id, lat, lon, t_arrival_s (or pass tdoa_pairs separately)',
}

// ─── heatmap canvas (log-likelihood from ml_grid_fusion) ─────────────────────
function HeatmapView({ heatmap, peakXY = null }) {
  const canvasRef = useRef(null)
  useEffect(() => {
    if (!heatmap || !heatmap.rel_log_likelihood) return
    const c = canvasRef.current
    if (!c) return
    const grid = heatmap.rel_log_likelihood
    const ny = grid.length, nx = grid[0]?.length || 0
    if (!nx || !ny) return
    const ctx = c.getContext('2d')
    const img = ctx.createImageData(nx, ny)
    // grid values are ≤ 0; map exp(grid) ∈ [0,1] → magma-ish colour ramp
    for (let j = 0; j < ny; j++) {
      for (let i = 0; i < nx; i++) {
        const t = Math.min(1, Math.max(0, Math.exp(grid[ny - 1 - j][i])))
        const r = Math.round(255 * Math.min(1, t * 1.3))
        const g = Math.round(255 * Math.pow(t, 1.8))
        const b = Math.round(255 * Math.pow(t, 4.0))
        const idx = (j * nx + i) * 4
        img.data[idx] = r; img.data[idx + 1] = g; img.data[idx + 2] = b; img.data[idx + 3] = 220
      }
    }
    c.width = nx; c.height = ny
    ctx.putImageData(img, 0, 0)
    if (peakXY && heatmap.x_m && heatmap.y_m) {
      const ix = Math.round((peakXY[0] - heatmap.x_m[0]) / (heatmap.x_m[1] - heatmap.x_m[0]))
      const iy = ny - 1 - Math.round((peakXY[1] - heatmap.y_m[0]) / (heatmap.y_m[1] - heatmap.y_m[0]))
      ctx.strokeStyle = '#22d3ee'
      ctx.lineWidth = 1.5
      ctx.beginPath(); ctx.arc(ix + 0.5, iy + 0.5, 4, 0, Math.PI * 2); ctx.stroke()
    }
  }, [heatmap, peakXY])
  if (!heatmap) return null
  return (
    <div style={{ ...card, padding: 8 }}>
      <div style={{ fontSize: 10, color: '#8b949e', marginBottom: 4 }}>
        log-likelihood heat-map ({heatmap.rel_log_likelihood?.[0]?.length || 0} × {heatmap.rel_log_likelihood?.length || 0} cells)
      </div>
      <canvas ref={canvasRef} style={{ width: '100%', maxWidth: 360, imageRendering: 'pixelated', background: '#000', borderRadius: 4 }} />
    </div>
  )
}

// ─── spectrum chart for synthetic-aperture ──────────────────────────────────
function SpectrumChart({ azimuthDeg, spectrum, peaks }) {
  const canvasRef = useRef(null)
  useEffect(() => {
    if (!azimuthDeg || !spectrum || !canvasRef.current) return
    const c = canvasRef.current
    const w = c.width = c.clientWidth || 360
    const h = c.height = 120
    const ctx = c.getContext('2d')
    ctx.fillStyle = '#000'; ctx.fillRect(0, 0, w, h)
    ctx.strokeStyle = '#22d3ee'
    ctx.lineWidth = 1.5
    ctx.beginPath()
    const N = spectrum.length
    for (let i = 0; i < N; i++) {
      const x = (i / (N - 1)) * w
      const y = h - spectrum[i] * (h - 4) - 2
      if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y)
    }
    ctx.stroke()
    if (peaks && peaks.length) {
      ctx.fillStyle = '#f59e0b'
      for (const p of peaks) {
        const ix = (azimuthDeg.indexOf(p.azimuth_deg) >= 0)
          ? azimuthDeg.indexOf(p.azimuth_deg) : Math.round((p.azimuth_deg - azimuthDeg[0]) / (azimuthDeg[1] - azimuthDeg[0]))
        const x = (ix / (N - 1)) * w
        const y = h - p.magnitude * (h - 4) - 2
        ctx.beginPath(); ctx.arc(x, y, 3, 0, Math.PI * 2); ctx.fill()
        ctx.fillStyle = '#e6edf3'; ctx.font = '10px monospace'
        ctx.fillText(`${p.azimuth_deg.toFixed(1)}°`, x + 4, y - 2)
        ctx.fillStyle = '#f59e0b'
      }
    }
    // x-axis labels
    ctx.fillStyle = '#6e7681'; ctx.font = '10px monospace'
    ctx.fillText(`${azimuthDeg[0]}°`, 2, h - 2)
    ctx.fillText(`${azimuthDeg[azimuthDeg.length - 1]}°`, w - 30, h - 2)
  }, [azimuthDeg, spectrum, peaks])
  return <canvas ref={canvasRef} style={{ width: '100%', height: 120, display: 'block' }} />
}

// ─── ObservationEditor: JSON textarea with sample button + validation ───────
function ObservationEditor({ value, onChange, sample, helpHint }) {
  const [err, setErr] = useState('')
  const text = useMemo(() => {
    try { return JSON.stringify(value, null, 2) } catch { return '[]' }
  }, [value])
  const [draft, setDraft] = useState(text)
  useEffect(() => { setDraft(text) }, [text])
  const commit = (txt) => {
    setDraft(txt)
    try {
      const parsed = JSON.parse(txt)
      if (!Array.isArray(parsed)) throw new Error('expected an array of observations')
      onChange(parsed)
      setErr('')
    } catch (e) {
      setErr(String(e.message || e))
    }
  }
  return (
    <div>
      <div style={{ display: 'flex', alignItems: 'center', gap: 6, fontSize: 11, color: '#8b949e', marginBottom: 4 }}>
        Observations (JSON array of objects — fields: {helpHint})
        <button className="btn btn-ghost" style={{ fontSize: 10, padding: '2px 6px' }} onClick={() => onChange(sample)}>
          <FileUp size={11} /> Load sample
        </button>
        <button className="btn btn-ghost" style={{ fontSize: 10, padding: '2px 6px' }} onClick={() => onChange([])}>
          <Trash2 size={11} /> Clear
        </button>
      </div>
      <textarea value={draft} onChange={(e) => commit(e.target.value)} rows={8}
                style={{ width: '100%', fontSize: 10, fontFamily: 'monospace',
                          background: '#0a0e13', color: '#c9d1d9', border: '1px solid #21262d',
                          borderRadius: 4, padding: 6, resize: 'vertical' }} />
      {err && <div style={{ fontSize: 10, color: '#f0883e', marginTop: 4 }}>parse: {err}</div>}
    </div>
  )
}

// ─── method-specific knob editors (carrier_hz, sigma, grid…) ────────────────
function NumberInput({ label, value, onChange, step = 1, suffix = '' }) {
  return (
    <label style={lblSty}>
      <span>{label}{suffix && <span style={{ color: '#6e7681' }}> ({suffix})</span>}</span>
      <input type="number" value={value ?? ''} step={step}
             onChange={(e) => onChange(e.target.value === '' ? null : Number(e.target.value))}
             style={{ ...inputSty, width: 130 }} />
    </label>
  )
}

// ─── main panel ──────────────────────────────────────────────────────────────
export default function AlgorithmsPanel({ onSendToMap }) {
  const [methodId, setMethodId] = useState('rss_path_loss')
  const [observations, setObservations] = useState(SAMPLE.rss_path_loss)
  const [carrierMHz, setCarrierMHz] = useState(433.0)
  const [pathLossN, setPathLossN] = useState(3.0)
  const [pTxDbm, setPTxDbm] = useState(null)
  const [sigmaAoa, setSigmaAoa] = useState(3.0)
  const [sigmaRss, setSigmaRss] = useState(6.0)
  const [sigmaHz, setSigmaHz] = useState(5.0)
  const [sigmaNs, setSigmaNs] = useState(50.0)
  const [gridSpanKm, setGridSpanKm] = useState(50.0)
  const [gridStepM, setGridStepM] = useState(100.0)
  const [synMethod, setSynMethod] = useState('bartlett')
  const [synSources, setSynSources] = useState(1)
  const [azStep, setAzStep] = useState(1.0)
  const [priorAz, setPriorAz] = useState(null)
  const [busy, setBusy] = useState(false)
  const [result, setResult] = useState(null)
  const [err, setErr] = useState('')
  const [methodsCatalog, setMethodsCatalog] = useState(METHOD_LIST)
  const [feasibility, setFeasibility] = useState(null)

  // fetch catalogue once
  useEffect(() => {
    algorithmsList().then(d => d?.methods && setMethodsCatalog(d.methods)).catch(() => {})
  }, [])

  // recompute feasibility whenever observations change
  useEffect(() => {
    if (!observations.length) { setFeasibility(null); return }
    let alive = true
    algorithmsFeasibility({ observations })
      .then(d => { if (alive) setFeasibility(d) })
      .catch(() => alive && setFeasibility(null))
    return () => { alive = false }
  }, [observations])

  // Auto-switch to the highest-priority feasible method when the currently
  // selected one stops fitting the observations (e.g. user pasted RSS-only
  // data while we were on Doppler-CPA). If the current method is still
  // feasible, respect the user's pick.
  useEffect(() => {
    if (!feasibility) return
    const cur = feasibility[methodId]
    if (cur && cur.feasible === false) {
      const best = pickBestMethod(feasibility)
      if (best && best !== methodId) setMethodId(best)
    }
  }, [feasibility, methodId])

  const method = methodsCatalog.find(m => m.id === methodId) || METHOD_LIST[0]
  const helpHint = method.kinds?.map(k => KIND_HELP[k] || k).join(' | ') || 'any'
  const sample = SAMPLE[methodId] || []

  // map method-id → request body + handler
  const run = async () => {
    setBusy(true); setErr(''); setResult(null)
    try {
      let r
      const carrierHz = (carrierMHz != null) ? Number(carrierMHz) * 1e6 : null
      if (methodId === 'rss_path_loss') {
        r = await algoRssPathLoss({ observations, path_loss_n: pathLossN, p_tx_dbm: pTxDbm,
                                     sigma_db: sigmaRss, grid_m: gridStepM, grid_span_m: gridSpanKm * 1000 })
      } else if (methodId === 'rss_gradient') {
        r = await algoRssGradient({ observations })
      } else if (methodId === 'doppler_cpa') {
        r = await algoDopplerCpa({ observations, carrier_hz: carrierHz })
      } else if (methodId === 'fdoa_track') {
        r = await algoFdoaTrack({ observations, carrier_hz: carrierHz, sigma_hz: sigmaHz,
                                    grid_span_m: gridSpanKm * 1000, grid_step_m: gridStepM })
      } else if (methodId === 'synthetic_aperture') {
        r = await algoSyntheticAperture({ snapshots: observations, carrier_hz: carrierHz,
                                            method: synMethod, n_sources: synSources, az_step_deg: azStep,
                                            az_start_deg: -180, az_end_deg: 180 })
      } else if (methodId === 'phase_interferometry') {
        r = await algoPhaseInterferometry({ snapshots: observations, carrier_hz: carrierHz, prior_az_deg: priorAz })
      } else if (methodId === 'tdoa_multi_receiver') {
        r = await algoTdoaMultiReceiver({ receivers: observations, sigma_ns: sigmaNs,
                                            grid_span_m: gridSpanKm * 1000, grid_step_m: gridStepM })
      } else if (methodId === 'ml_grid_fusion') {
        r = await algoMlGridFusion({ observations, path_loss_n: pathLossN, p_tx_dbm: pTxDbm,
                                       carrier_hz: carrierHz, sigma_aoa_deg: sigmaAoa,
                                       sigma_rss_db: sigmaRss, sigma_hz: sigmaHz, sigma_ns: sigmaNs,
                                       grid_span_m: gridSpanKm * 1000, grid_step_m: gridStepM })
      } else if (methodId === 'ekf_track') {
        r = await algoEkfTrack({ observations, path_loss_n: pathLossN, p_tx_dbm: pTxDbm,
                                  carrier_hz: carrierHz, sigma_aoa_deg: sigmaAoa,
                                  sigma_rss_db: sigmaRss, sigma_hz: sigmaHz })
      }
      setResult(r)
      if (r && !r.ok) setErr(r.error || 'method returned ok=false')
    } catch (e) {
      setErr(e?.response?.data?.detail || e?.message || String(e))
    } finally { setBusy(false) }
  }

  const sendToMap = () => {
    if (!result?.estimate) return
    const { lat, lon } = result.estimate
    const cep = result.uncertainty?.cep_m
    const label = `Algo: ${method.name} (CEP ${cep ? Math.round(cep) + 'm' : '—'})`
    onSendToMap?.({
      lat, lon, label,
      origin: 'algorithm',
      method_id: methodId, method_name: method.name,
      cep_m: cep, raw: result,
    })
  }

  return (
    <div style={{ height: '100%', overflowY: 'auto', padding: 12 }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 8 }}>
        <Sigma size={16} color="#22d3ee" />
        <b style={{ color: '#e6edf3' }}>Algorithms — single-channel & multi-method geolocation</b>
      </div>
      <div style={{ fontSize: 11, color: '#8b949e', marginBottom: 12 }}>
        Built so a single SDR (plus motion) can still locate emitters, and so a DF head's output can be fused with RSS / Doppler / TDOA.
      </div>

      <div style={card}>
        <div style={sectionH}><ListChecks size={13} /> Method
          <button className="btn btn-ghost" style={{ marginLeft: 'auto', fontSize: 10, padding: '2px 8px' }}
                   title="Pick the most specific method that fits the current observations"
                   disabled={!feasibility}
                   onClick={() => { const best = pickBestMethod(feasibility); if (best) setMethodId(best) }}>
            🎯 Auto-select
          </button>
        </div>
        <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap', marginBottom: 6 }}>
          {methodsCatalog.map(m => {
            const feas = feasibility?.[m.id]
            const disabled = feas && feas.feasible === false
            const isAuto = pickBestMethod(feasibility) === m.id
            return (
              <button key={m.id}
                       className={`btn ${methodId === m.id ? 'btn-primary' : 'btn-ghost'}`}
                       style={{ fontSize: 10, padding: '3px 8px',
                                  opacity: disabled ? 0.5 : 1,
                                  outline: (isAuto && methodId !== m.id) ? '1px dashed #22d3ee' : 'none',
                                  outlineOffset: 1 }}
                       title={(feas?.requires || '') + (isAuto ? ' · auto-selected for the current observations' : '')}
                       onClick={() => { setMethodId(m.id); setObservations(SAMPLE[m.id] || []) }}>
                {m.name}{isAuto && methodId !== m.id ? ' ✨' : ''}{feas && !feas.feasible ? ' ⚠' : ''}
              </button>
            )
          })}
        </div>
        <div style={{ fontSize: 10, color: '#6e7681' }}>
          {feasibility
            ? <>obs: {feasibility.n_observations} · span: {feasibility.spatial_span_m?.toFixed(1)} m
                {pickBestMethod(feasibility) && pickBestMethod(feasibility) !== methodId &&
                  <> · <span style={{ color: '#22d3ee' }}>auto-pick: {methodsCatalog.find(m => m.id === pickBestMethod(feasibility))?.name}</span></>}
              </>
            : 'paste/load observations below to check feasibility'}
        </div>
      </div>

      <div style={card}>
        <div style={sectionH}>Observations</div>
        <ObservationEditor value={observations} onChange={setObservations} sample={sample} helpHint={helpHint} />
      </div>

      <div style={card}>
        <div style={sectionH}>Parameters</div>
        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(150px, 1fr))', gap: 8 }}>
          {(method.kinds || []).includes('doppler') || methodId === 'synthetic_aperture' || methodId === 'phase_interferometry' ? (
            <NumberInput label="Carrier" value={carrierMHz} onChange={setCarrierMHz} step={0.001} suffix="MHz" />
          ) : null}
          {['rss_path_loss','ml_grid_fusion','ekf_track'].includes(methodId) && (
            <>
              <NumberInput label="Path-loss n" value={pathLossN} onChange={setPathLossN} step={0.1} />
              <NumberInput label="P_tx" value={pTxDbm} onChange={setPTxDbm} step={1} suffix="dBm (blank=fit)" />
            </>
          )}
          {['rss_path_loss','ml_grid_fusion','ekf_track'].includes(methodId) && (
            <NumberInput label="σ_rss" value={sigmaRss} onChange={setSigmaRss} step={0.5} suffix="dB" />
          )}
          {['fdoa_track','ml_grid_fusion','ekf_track'].includes(methodId) && (
            <NumberInput label="σ_Doppler" value={sigmaHz} onChange={setSigmaHz} step={0.5} suffix="Hz" />
          )}
          {['ml_grid_fusion','ekf_track'].includes(methodId) && (
            <NumberInput label="σ_AoA" value={sigmaAoa} onChange={setSigmaAoa} step={0.1} suffix="°" />
          )}
          {['tdoa_multi_receiver','ml_grid_fusion'].includes(methodId) && (
            <NumberInput label="σ_TDOA" value={sigmaNs} onChange={setSigmaNs} step={1} suffix="ns" />
          )}
          {['rss_path_loss','fdoa_track','tdoa_multi_receiver','ml_grid_fusion'].includes(methodId) && (
            <>
              <NumberInput label="Grid span" value={gridSpanKm} onChange={setGridSpanKm} step={1} suffix="km" />
              <NumberInput label="Grid step" value={gridStepM} onChange={setGridStepM} step={5} suffix="m" />
            </>
          )}
          {methodId === 'synthetic_aperture' && (
            <>
              <label style={lblSty}>
                Algorithm
                <select value={synMethod} onChange={(e) => setSynMethod(e.target.value)} style={inputSty}>
                  <option value="bartlett">Bartlett (beamform)</option>
                  <option value="capon">Capon (MVDR)</option>
                  <option value="music">MUSIC (subspace)</option>
                </select>
              </label>
              <NumberInput label="# sources" value={synSources} onChange={setSynSources} step={1} />
              <NumberInput label="Az step" value={azStep} onChange={setAzStep} step={0.5} suffix="°" />
            </>
          )}
          {methodId === 'phase_interferometry' && (
            <NumberInput label="Prior bearing" value={priorAz} onChange={setPriorAz} step={1} suffix="° (optional)" />
          )}
        </div>
        <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginTop: 8 }}>
          <button className="btn btn-primary" disabled={busy || observations.length === 0} onClick={run}
                   style={{ gap: 6 }}>
            {busy ? <><RefreshCw size={13} className="spin" /> Running…</> : <><Crosshair size={13} /> Run {method.name}</>}
          </button>
          {result?.estimate && (
            <button className="btn btn-primary" onClick={sendToMap} style={{ gap: 6 }}>
              <Send size={13} /> Send fix to map (algorithm)
            </button>
          )}
          {err && <span style={{ fontSize: 11, color: '#f0883e' }}>{err}</span>}
        </div>
      </div>

      {result && (
        <div style={card}>
          <div style={sectionH}>Result</div>
          {result.estimate && (
            <table style={{ width: '100%', borderCollapse: 'collapse', marginBottom: 8 }}>
              <tbody>
                <tr><td style={th}>Position</td><td style={td}>{result.estimate.lat?.toFixed(6)}, {result.estimate.lon?.toFixed(6)}</td></tr>
                {result.estimate.p_tx_dbm != null && <tr><td style={th}>P_tx</td><td style={td}>{result.estimate.p_tx_dbm?.toFixed(1)} dBm</td></tr>}
                {result.estimate.path_loss_n != null && <tr><td style={th}>n (path-loss)</td><td style={td}>{result.estimate.path_loss_n?.toFixed(2)}</td></tr>}
                {result.uncertainty?.cep_m != null && <tr><td style={th}>CEP₅₀</td><td style={td}>{result.uncertainty.cep_m.toFixed(0)} m</td></tr>}
                {result.uncertainty?.ellipse_axes_m && <tr><td style={th}>Error ellipse (1σ)</td><td style={td}>{result.uncertainty.ellipse_axes_m[0]?.toFixed(0)} × {result.uncertainty.ellipse_axes_m[1]?.toFixed(0)} m @ {result.uncertainty.ellipse_bearing_deg?.toFixed(0)}°</td></tr>}
                {result.fit?.log_likelihood != null && <tr><td style={th}>log-likelihood</td><td style={td}>{result.fit.log_likelihood.toFixed(2)}</td></tr>}
                {result.fit?.n_observations != null && <tr><td style={th}>N observations</td><td style={td}>{result.fit.n_observations}</td></tr>}
              </tbody>
            </table>
          )}

          {result.bearing_deg != null && (
            <div style={{ fontSize: 12, color: '#c9d1d9', marginBottom: 4 }}>
              Bearing: <b>{result.bearing_deg.toFixed(1)}°</b> · gradient {result.gradient_db_per_km?.toFixed(2)} dB/km · RMS {result.rms_residual_db?.toFixed(2)} dB
            </div>
          )}

          {result.candidates && (
            <div style={{ fontSize: 11, color: '#8b949e', marginBottom: 4 }}>
              <b>Doppler CPA candidates (left/right ambiguity):</b>
              {result.candidates.map((c, i) => (
                <div key={i}>· {c.side}: {c.lat.toFixed(6)}, {c.lon.toFixed(6)}</div>
              ))}
              <div style={{ marginTop: 2 }}>CPA dist {result.fit.cpa_distance_m?.toFixed(0)} m · t_CPA {result.fit.cpa_time_s?.toFixed(2)} s · RMS residual {result.fit.rms_residual_hz?.toFixed(1)} Hz</div>
            </div>
          )}

          {result.azimuth_deg && result.pseudo_spectrum && (
            <>
              <div style={{ fontSize: 11, color: '#8b949e', marginBottom: 4 }}>
                Synthetic aperture: {result.n_elements} elements · span {result.aperture_span_m.toFixed(2)} m · res ≈ {result.effective_resolution_deg.toFixed(1)}°
                {result.peaks?.length ? ` · peak at ${result.peaks[0].azimuth_deg.toFixed(1)}°` : ''}
              </div>
              <SpectrumChart azimuthDeg={result.azimuth_deg} spectrum={result.pseudo_spectrum} peaks={result.peaks} />
              {result.warnings?.length > 0 && <div style={{ fontSize: 10, color: '#f0883e', marginTop: 4 }}>⚠ {result.warnings.join('; ')}</div>}
            </>
          )}

          {result.bearings && Array.isArray(result.bearings) && (
            <div style={{ fontSize: 11, color: '#8b949e', marginTop: 4 }}>
              Phase interferometry: mean {result.mean_bearing_deg?.toFixed(1)}° · {result.n_baselines} baselines · λ {result.wavelength_m?.toFixed(3)} m
              <div style={{ maxHeight: 100, overflowY: 'auto' }}>
                {result.bearings.map((b, i) => (
                  <div key={i}>· pair {b.pair.join('-')}: {b.bearing_deg.toFixed(1)}° (baseline {b.baseline_m.toFixed(2)} m)</div>
                ))}
              </div>
            </div>
          )}

          {result.heatmap && (
            <HeatmapView heatmap={result.heatmap}
                         peakXY={result.estimate ? [result.estimate.x_m, result.estimate.y_m] : null} />
          )}

          {result.note && <div style={{ fontSize: 10, color: '#6e7681', marginTop: 4 }}>{result.note}</div>}
        </div>
      )}

      <div style={{ ...card, background: '#0a0e13' }}>
        <div style={{ fontSize: 10, color: '#6e7681' }}>
          Use a DF head (KrakenSDR / ANTSDR e200 / multi-channel coherent array) for instantaneous AoA at a single point;
          use these algorithms when you only have a single SDR and motion, or when you want to fuse heterogeneous observations
          (AoA from a DF head + RSS + Doppler) into one ML fix.
        </div>
      </div>
    </div>
  )
}
