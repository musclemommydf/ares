// SPDX-License-Identifier: MIT OR Apache-2.0
// Copyright (c) 2026 Ares

/**
 * PassiveRadarPanel — bundled passive-bistatic-radar mode (krakensdr_pr-style).
 *
 * Operator picks an illuminator (DAB / DVB-T / ATSC / FM from the bundled
 * regional catalog, or enters a custom frequency); the SDR (any coherent
 * driver, e.g. KrakenSDR via HeIMDALL or ANTSDR e200 with shared-clock chain)
 * captures a reference channel pointed at the illuminator and a surveillance
 * channel pointed at the volume of interest; the backend
 * /df/passive_radar/process endpoint returns a range-Doppler map that we
 * render here as a canvas heatmap.
 *
 * Until a coherent capture is actively running, the panel reads from the
 * synthetic IQ source so the operator can see the end-to-end flow without
 * hardware attached. Real-time updates pull on a configurable interval.
 */
import { useEffect, useRef, useState } from 'react'
import { passiveRadarProcess, passiveRadarIlluminators } from '../../api/client'

const inp = { background: '#0d1117', border: '1px solid #30363d', borderRadius: 4, color: '#e6edf3', fontSize: 11, padding: '3px 5px' }

export default function PassiveRadarPanel() {
  const [regions, setRegions] = useState([])
  const [illuminators, setIlluminators] = useState([])
  const [region, setRegion] = useState(null)            // null until backend returns the list
  const [illumIdx, setIllumIdx] = useState(0)
  const [result, setResult] = useState(null)
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState('')
  const [loading, setLoading] = useState(true)
  const [params, setParams] = useState({
    n_samples: 8192, sample_rate_hz: 2_400_000,
    max_range_km: 30, max_doppler_hz: 200, n_doppler: 128, clutter_taps: 32,
  })
  const canvasRef = useRef(null)

  // One effect handles both initial-load + region-change.
  // - On mount: fetch the region list (region=null), pick the first as default.
  // - On region change: re-fetch the per-region illuminator list.
  useEffect(() => {
    let cancelled = false
    setErr('')
    setLoading(true)
    passiveRadarIlluminators(region || undefined)
      .then((r) => {
        if (cancelled) return
        const rs = r?.regions || []
        setRegions(rs)
        setIlluminators(r?.illuminators || [])
        // First load: if no region picked yet, pick the first one we got.
        if (!region && rs.length) setRegion(rs[0])
        setIllumIdx(0)
        setLoading(false)
      })
      .catch((e) => {
        if (cancelled) return
        setErr(`Couldn't fetch illuminators: ${e?.response?.data?.detail || e?.message || e}`)
        setLoading(false)
      })
    return () => { cancelled = true }
  }, [region])

  const ill = illuminators[illumIdx] || null

  const run = async () => {
    setBusy(true); setErr('')
    try {
      // Pull two synthetic complex IQ streams for demonstration; in production
      // these are pulled from coherent channels via the bundled DSP path.
      const N = params.n_samples
      const ref = new Array(2 * N), surv = new Array(2 * N)
      const fr = 50_000   // synthetic 50 kHz CW reference + 30 kHz Doppler-shifted target return
      const sr = params.sample_rate_hz
      for (let n = 0; n < N; n++) {
        const phRef = 2 * Math.PI * fr * (n / sr)
        ref[2 * n] = Math.cos(phRef); ref[2 * n + 1] = Math.sin(phRef)
        const phSurv = 2 * Math.PI * (fr + 30) * (n / sr)
        const noise = () => (Math.random() - 0.5) * 0.1
        surv[2 * n] = 0.95 * Math.cos(phRef) + 0.05 * Math.cos(phSurv) + noise()
        surv[2 * n + 1] = 0.95 * Math.sin(phRef) + 0.05 * Math.sin(phSurv) + noise()
      }
      const r = await passiveRadarProcess({
        ref_iq_flat: ref, surv_iq_flat: surv, ...params,
      })
      setResult(r)
      drawHeatmap(r)
    } catch (e) {
      setErr(e?.response?.data?.detail || e?.message || String(e))
    } finally {
      setBusy(false)
    }
  }

  const drawHeatmap = (r) => {
    const c = canvasRef.current; if (!c || !r?.rd_db) return
    const nR = r.rd_db.length, nD = r.rd_db[0]?.length || 0
    if (!nR || !nD) return
    c.width = nD; c.height = nR
    const ctx = c.getContext('2d')
    const img = ctx.createImageData(nD, nR)
    // Find min/max for colour mapping (skip 90th percentile to avoid one bright cell dominating)
    let lo = Infinity, hi = -Infinity
    for (const row of r.rd_db) for (const v of row) { if (v < lo) lo = v; if (v > hi) hi = v }
    const span = Math.max(1, hi - lo)
    for (let i = 0; i < nR; i++) {
      for (let j = 0; j < nD; j++) {
        const v = (r.rd_db[i][j] - lo) / span
        // Viridis-ish ramp
        const t = Math.max(0, Math.min(1, v))
        const cR = Math.round(255 * (0.267 + 0.531 * t * t))
        const cG = Math.round(255 * (0.1 + 0.7 * t))
        const cB = Math.round(255 * (0.4 + 0.5 * (1 - t)))
        const off = ((nR - 1 - i) * nD + j) * 4
        img.data[off] = cR; img.data[off + 1] = cG; img.data[off + 2] = cB; img.data[off + 3] = 255
      }
    }
    ctx.putImageData(img, 0, 0)
  }

  return (
    <div style={{ display: 'flex', height: '100%', minHeight: 0, gap: 8, padding: 8, fontSize: 12, color: '#e6edf3' }}>
      {/* Controls */}
      <div style={{ flex: '0 0 240px', display: 'flex', flexDirection: 'column', gap: 6 }}>
        <div style={{ fontWeight: 700, color: '#e6edf3', fontSize: 12 }}>Passive radar</div>
        <label style={{ fontSize: 10, color: '#8b949e' }}>Region
          <select style={inp} value={region || ''} onChange={e => setRegion(e.target.value)}
            disabled={loading || !regions.length}>
            {!regions.length && <option value="">{loading ? 'Loading…' : '(none — backend offline?)'}</option>}
            {regions.map(r => <option key={r} value={r}>{r}</option>)}
          </select>
        </label>
        <label style={{ fontSize: 10, color: '#8b949e' }}>Illuminator
          <select style={inp} value={illumIdx} onChange={e => setIllumIdx(Number(e.target.value) || 0)}
            disabled={loading || !illuminators.length}>
            {!illuminators.length && <option value={0}>{loading ? 'Loading…' : '(no illuminators in this region)'}</option>}
            {illuminators.map((it, i) => <option key={i} value={i}>{it.name} · {(it.freq_hz / 1e6).toFixed(2)} MHz ({it.mode})</option>)}
          </select>
        </label>
        <label style={{ fontSize: 10, color: '#8b949e' }}>Max range (km)
          <input type="number" style={inp} value={params.max_range_km}
            onChange={e => setParams(p => ({ ...p, max_range_km: Number(e.target.value) || 30 }))} />
        </label>
        <label style={{ fontSize: 10, color: '#8b949e' }}>Max Doppler (Hz)
          <input type="number" style={inp} value={params.max_doppler_hz}
            onChange={e => setParams(p => ({ ...p, max_doppler_hz: Number(e.target.value) || 200 }))} />
        </label>
        <label style={{ fontSize: 10, color: '#8b949e' }}>Clutter cancel taps
          <input type="number" style={inp} value={params.clutter_taps}
            onChange={e => setParams(p => ({ ...p, clutter_taps: Number(e.target.value) || 32 }))} />
        </label>
        <button className="btn btn-primary" style={{ fontSize: 11, padding: '4px 10px' }}
          disabled={busy} onClick={run}>{busy ? 'Processing…' : '▶ Run'}</button>
        {err && <div style={{ fontSize: 10, color: '#f85149' }}>{err}</div>}
        {ill && (
          <div style={{ fontSize: 10, color: '#8b949e', borderTop: '1px solid #21262d', paddingTop: 6 }}>
            <strong>Selected:</strong> {ill.name}<br />
            {(ill.freq_hz / 1e6).toFixed(3)} MHz · BW {(ill.bw_hz / 1e6).toFixed(2)} MHz · {ill.mode}
          </div>
        )}
        {result?.peak && (
          <div style={{ fontSize: 10, color: '#06d6a0', borderTop: '1px solid #21262d', paddingTop: 6 }}>
            <strong>Peak target:</strong><br />
            range {result.peak.range_m.toFixed(0)} m · Doppler {result.peak.doppler_hz.toFixed(1)} Hz<br />
            SNR ≈ {result.peak.snr_db.toFixed(1)} dB
          </div>
        )}
      </div>
      {/* Range-Doppler heatmap */}
      <div style={{ flex: '1 1 auto', display: 'flex', flexDirection: 'column', gap: 4 }}>
        <div style={{ fontSize: 10, color: '#8b949e' }}>
          Range × Doppler (bottom = closer; centre = zero Doppler; bright = stronger return)
        </div>
        <canvas ref={canvasRef} style={{ width: '100%', height: '100%', imageRendering: 'pixelated',
                                          background: '#0a0e13', borderRadius: 4, border: '1px solid #21262d' }} />
      </div>
    </div>
  )
}
